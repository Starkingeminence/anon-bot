import asyncio
import os
import threading
import asyncpg
import re
import io
import logging
from datetime import date, datetime, timedelta
from flask import Flask
from telegram import Update, ChatPermissions
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.helpers import mention_html

# --- Logging Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- FAKE WEBSITE ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Eminence DAO Bot Online"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- Configuration ---
TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DATABASE_URL")
SUPPORT_USER = os.getenv("SUPPORT_USER", "admin")
BOT_CREATOR_ID = int(os.getenv("BOT_CREATOR_ID", "0"))
DAILY_POINT_CAP = 50
DAILY_VIOLATION_CAP = 15

OWNER_ONLY_CONFIGS = ['engagement_tracking']

# --- Caching ---
group_settings_cache = {}
user_sessions = {}
admin_sessions = {}

# --- Database Setup (Async with asyncpg) ---
async def get_db_connection():
    try:
        conn = await asyncpg.connect(DB_URL)
        # Initialize Tables (run once or on startup)
        await conn.execute("""CREATE TABLE IF NOT EXISTS group_settings (
            group_id BIGINT PRIMARY KEY,
            anti_link BOOLEAN DEFAULT FALSE,
            anti_forward BOOLEAN DEFAULT FALSE,
            engagement_tracking BOOLEAN DEFAULT TRUE);""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS blacklists (
            group_id BIGINT, word TEXT, PRIMARY KEY (group_id, word));""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS user_stats (
            group_id BIGINT, user_id BIGINT, username TEXT, 
            message_count INT DEFAULT 0, total_points INT DEFAULT 0, 
            points_today INT DEFAULT 0, last_active_date DATE DEFAULT CURRENT_DATE,
            warns INT DEFAULT 0, violations_today INT DEFAULT 0, last_anon_link_date DATE DEFAULT NULL, PRIMARY KEY (group_id, user_id));""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS suggestions (
            id SERIAL PRIMARY KEY, group_id BIGINT, user_id BIGINT, 
            suggestion TEXT, status TEXT DEFAULT 'pending', type TEXT);""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS punishment_logs (
            group_id BIGINT, target_id BIGINT, admin_id BIGINT,
            type TEXT, reason TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (group_id, target_id, type));""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS custom_filters (
            group_id BIGINT, trigger TEXT, response TEXT,
            PRIMARY KEY (group_id, trigger));""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS message_logs (
            group_id BIGINT, message_id BIGINT, user_id BIGINT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (group_id, message_id));""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS proposals (
            id SERIAL PRIMARY KEY, group_id BIGINT, proposer_id BIGINT,
            action_type TEXT, key_target TEXT, value_target TEXT,
            status TEXT DEFAULT 'pending', 
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_pinged_at TIMESTAMP
        );""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS votes (
            proposal_id INT, user_id BIGINT, vote TEXT,
            PRIMARY KEY (proposal_id, user_id));""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS user_connections (
            user_id BIGINT PRIMARY KEY, group_id BIGINT);""")
        logger.info("✅ Database Connected & Tables Ready.")
        return conn
    except Exception as e:
        logger.error(f"❌ CRITICAL DATABASE ERROR: {e}")
        return None

# --- Helper Functions (Async) ---
async def get_settings(group_id):
    if group_id in group_settings_cache: return group_settings_cache[group_id]
    conn = await get_db_connection()
    if not conn: return {'anti_link': False, 'anti_forward': False, 'engagement_tracking': True}
    row = await conn.fetchrow("SELECT * FROM group_settings WHERE group_id=$1", group_id)
    if row:
        settings = dict(row)
    else:
        settings = {'anti_link': False, 'anti_forward': False, 'engagement_tracking': True}
        await conn.execute("INSERT INTO group_settings (group_id) VALUES ($1)", group_id)
    group_settings_cache[group_id] = settings
    await conn.close()
    return settings

async def update_setting_db(group_id, key, val):
    allowed_keys = ['anti_link', 'anti_forward', 'engagement_tracking']
    if key not in allowed_keys: return
    conn = await get_db_connection()
    if not conn: return
    await conn.execute(f"UPDATE group_settings SET {key} = $1 WHERE group_id = $2", val, group_id)
    if group_id in group_settings_cache: group_settings_cache[group_id][key] = val
    await conn.close()

async def update_user_stats(group_id, user_id, username, points=0, warn_inc=0, violation_inc=0):
    conn = await get_db_connection()
    if not conn: return 0, 0
    today = date.today()
    row = await conn.fetchrow("SELECT * FROM user_stats WHERE group_id=$1 AND user_id=$2", group_id, user_id)
    p_today = row['points_today'] if row else 0
    v_today = row['violations_today'] if row else 0
    if row and row['last_active_date'] != today:
        p_today = 0
        v_today = 0
    final_points = points if (points > 0 and p_today < DAILY_POINT_CAP) else 0
    final_violations = v_today + violation_inc
    if row:
        await conn.execute("""UPDATE user_stats SET message_count = message_count + 1,
            total_points = total_points + $1, points_today = $2, last_active_date = $3,
            warns = warns + $4, username = COALESCE($5, username), violations_today = $6
            WHERE group_id=$7 AND user_id=$8""", 
            final_points, p_today + final_points, today, warn_inc, username, final_violations, group_id, user_id)
        await conn.close()
        return row['warns'] + warn_inc, final_violations
    else:
        await conn.execute("""INSERT INTO user_stats (group_id, user_id, username, message_count, total_points, points_today, last_active_date, warns, violations_today)
            VALUES ($1, $2, $3, 1, $4, $5, $6, $7, $8)""", group_id, user_id, username, final_points, final_points, today, warn_inc, violation_inc)
        await conn.close()
        return warn_inc, violation_inc

async def log_punishment(group_id, target_id, admin_id, p_type, reason):
    conn = await get_db_connection()
    if not conn: return
    await conn.execute("""INSERT INTO punishment_logs (group_id, target_id, admin_id, type, reason) VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (group_id, target_id, type) DO UPDATE SET admin_id=$3, reason=$5, timestamp=CURRENT_TIMESTAMP""",
        group_id, target_id, admin_id, p_type, reason)
    await conn.close()

async def get_punisher(group_id, target_id, p_type):
    conn = await get_db_connection()
    if not conn: return None
    row = await conn.fetchrow("SELECT admin_id FROM punishment_logs WHERE group_id=$1 AND target_id=$2 AND type=$3", group_id, target_id, p_type)
    await conn.close()
    return row['admin_id'] if row else None

async def log_anon_message(group_id, message_id, user_id):
    conn = await get_db_connection()
    if not conn: return
    await conn.execute("INSERT INTO message_logs (group_id, message_id, user_id) VALUES ($1, $2, $3)", group_id, message_id, user_id)
    await conn.close()

async def get_original_sender(group_id, message_id):
    conn = await get_db_connection()
    if not conn: return None
    row = await conn.fetchrow("SELECT user_id FROM message_logs WHERE group_id = $1 AND message_id = $2", group_id, message_id)
    await conn.close()
    return row['user_id'] if row else None

async def save_suggestion(group_id, user_id, text, status='pending', s_type='suggestion'):
    conn = await get_db_connection()
    if not conn: return
    await conn.execute("INSERT INTO suggestions (group_id, user_id, suggestion, status, type) VALUES ($1, $2, $3, $4, $5)", group_id, user_id, text, status, s_type)
    await conn.close()

async def get_suggestions(group_id, s_type='suggestion'):
    conn = await get_db_connection()
    if not conn: return []
    rows = await conn.fetch("SELECT * FROM suggestions WHERE group_id=$1 AND type=$2 ORDER BY id DESC", group_id, s_type)
    await conn.close()
    return rows

async def delete_suggestion(s_id):
    conn = await get_db_connection()
    if not conn: return
    await conn.execute("DELETE FROM suggestions WHERE id=$1", s_id)
    await conn.close()

# --- Permission Checks ---
async def is_admin(update: Update, user_id=None):
    if not user_id: user_id = update.message.from_user.id
    if user_id == BOT_CREATOR_ID: return True 
    chat = update.effective_chat
    if chat.type == 'private': return False
    try:
        member = await chat.get_member(user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except: return False

async def is_group_owner(update: Update, user_id=None):
    if not user_id: user_id = update.message.from_user.id
    if user_id == BOT_CREATOR_ID: return True
    try:
        member = await update.effective_chat.get_member(user_id)
        return member.status == ChatMemberStatus.OWNER
    except: return False

async def get_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message: return update.message.reply_to_message.from_user
    if context.args:
        username = context.args[0].replace("@", "")
        conn = await get_db_connection()
        if conn:
            row = await conn.fetchrow("SELECT user_id FROM user_stats WHERE group_id=$1 AND username=$2", update.effective_chat.id, username)
            if row:
                try:
                    member = await update.effective_chat.get_member(row['user_id'])
                    await conn.close()
                    return member.user
                except:
                    await conn.close()
    return None

# --- GOVERNANCE LOGIC ---

async def create_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE, type, target, value):
    gid = update.effective_chat.id
    uid = update.message.from_user.id
    
    if type == 'config' and target in OWNER_ONLY_CONFIGS:
        if not await is_group_owner(update):
            await update.message.reply_text(f"❌ **Denied.** Only the Owner can propose changes to `{target}`.")
            return

    conn = await get_db_connection()
    if not conn: return await update.message.reply_text("❌ Database Error.")
    
    pid = await conn.fetchval("""INSERT INTO proposals (group_id, proposer_id, action_type, key_target, value_target)
        VALUES ($1, $2, $3, $4, $5) RETURNING id""", gid, uid, type, target, value)
    await conn.close()
    
    val_disp = value if value else target
    proposal_text = (
        f"🗳 **New Proposal #{pid}**\n"
        f"**Group:** {update.effective_chat.title}\n"
        f"**Action:** {type.replace('_', ' ').title()}\n"
        f"**Target:** {val_disp}\n\n"
        f"⚠️ **Secret Ballot:** Votes are hidden.\n"
        f"To Vote: Reply with `/vote {pid} yes` or `no` here in DM."
    )

    await update.message.reply_text(f"✅ **Proposal #{pid} Created.**\nI am sending DMs to all admins now.")
    
    admins = await update.effective_chat.get_administrators()
    for a in admins:
        if not a.user.is_bot:
            try: await context.bot.send_message(chat_id=a.user.id, text=proposal_text, parse_mode=ParseMode.MARKDOWN)
            except: pass 
            
    await cast_vote(update, context, pid, 'yes', auto=True, group_context=update.effective_chat)

async def cast_vote(update: Update, context: ContextTypes.DEFAULT_TYPE, pid, vote, auto=False, group_context=None):
    conn = await get_db_connection()
    if not conn: 
        if not auto: await update.message.reply_text("❌ Database Error.")
        return

    user_id = update.message.from_user.id
    
    prop = await conn.fetchrow("SELECT * FROM proposals WHERE id=$1 AND status='pending'", pid)
    
    if not prop:
        if not auto: await update.message.reply_text("❌ Proposal invalid or closed.")
        await conn.close()
        return

    age = datetime.now() - prop['created_at']
    if age > timedelta(days=30):
        await conn.execute("UPDATE proposals SET status='expired' WHERE id=$1", pid)
        if not auto: await update.message.reply_text("❌ Proposal expired.")
        await conn.close()
        return

    await conn.execute("INSERT INTO votes (proposal_id, user_id, vote) VALUES ($1, $2, $3) ON CONFLICT (proposal_id, user_id) DO UPDATE SET vote=$3",
                   pid, user_id, vote)
    
    if not auto: await update.message.reply_text(f"✅ Vote saved for #{pid}.")

    try:
        if group_context: chat = group_context
        else: chat = await context.bot.get_chat(prop['group_id'])
        current_admins = await chat.get_administrators()
    except: 
        await conn.close()
        return

    current_admin_ids = [a.user.id for a in current_admins if not a.user.is_bot]
    
    voted_ids = [r['user_id'] for r in await conn.fetch("SELECT user_id FROM votes WHERE proposal_id=$1", pid)]
    
    valid_votes_count = len([uid for uid in voted_ids if uid in current_admin_ids])
    missing = len(current_admin_ids) - valid_votes_count
    
    if missing > 0: 
        await conn.close()
        return 

    owner_id = next((a.user.id for a in current_admins if a.status == ChatMemberStatus.OWNER), None)
    admins_except_owner = [a for a in current_admins if a.user.id != owner_id and not a.user.is_bot]
    admin_pool_count = len(admins_except_owner)
    
    if prop['action_type'].startswith('blacklist_'):
        # For blacklist, exclude owner from required voters/weights for faster process
        owner_weight = 0
        admin_weight = 100.0 / admin_pool_count if admin_pool_count > 0 else 0
    else:
        if admin_pool_count == 0 and owner_id:
            owner_weight = 100.0
            admin_weight = 0
        elif not owner_id:
            owner_weight = 0
            admin_weight = 100.0 / admin_pool_count if admin_pool_count > 0 else 0
        else:
            owner_weight = 50.0
            admin_weight = 50.0 / admin_pool_count if admin_pool_count > 0 else 0
    
    score_yes = 0.0
    
    all_votes = await conn.fetch("SELECT user_id, vote FROM votes WHERE proposal_id=$1", pid)
        
    for v in all_votes:
        uid, choice = v['user_id'], v['vote']
        if uid in current_admin_ids and choice == 'yes':
            if uid == owner_id and prop['action_type'].startswith('blacklist_') == False:
                score_yes += owner_weight
            elif uid != owner_id:
                score_yes += admin_weight
            
    if score_yes >= 50.0:
        await execute_proposal(context, chat, prop)
    else:
        await conn.execute("UPDATE proposals SET status='rejected' WHERE id=$1", pid)
        await context.bot.send_message(prop['group_id'], f"❌ **Proposal #{pid} Rejected.**\nConsensus was not reached.", parse_mode=ParseMode.MARKDOWN)
    await conn.close()

async def execute_proposal(context: ContextTypes.DEFAULT_TYPE, chat, prop):
    conn = await get_db_connection()
    if not conn: return
    gid = prop['group_id']
    target = prop['key_target']
    val = prop['value_target']
    
    msg = ""
    if prop['action_type'] == 'config':
        await update_setting_db(gid, target, val == 'true')
        msg = f"✅ **Proposal #{prop['id']} Passed!**\nSetting `{target}` updated."
    elif prop['action_type'] == 'blacklist_add':
        await conn.execute("INSERT INTO blacklists VALUES ($1, $2) ON CONFLICT DO NOTHING", gid, val)
        msg = f"✅ **Proposal #{prop['id']} Passed!**\nBlacklist updated."
    elif prop['action_type'] == 'blacklist_remove':
        await conn.execute("DELETE FROM blacklists WHERE group_id=$1 AND word=$2", gid, val)
        msg = f"✅ **Proposal #{prop['id']} Passed!**\nBlacklist updated."
    elif prop['action_type'] == 'suggestion_status':
        await delete_suggestion(target)  # target is suggestion ID
        msg = f"✅ **Proposal #{prop['id']} Passed!**\nSuggestion #{target} marked as {val} and deleted."
    elif prop['action_type'] == 'report_status':
        await delete_suggestion(target)
        msg = f"✅ **Proposal #{prop['id']} Passed!**\nReport #{target} marked as taken care of and deleted."

    await conn.execute("UPDATE proposals SET status='passed' WHERE id=$1", prop['id'])
    await context.bot.send_message(gid, msg, parse_mode=ParseMode.MARKDOWN)
    await conn.close()

async def ping_voters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not context.args: return await update.message.reply_text("Usage: `/ping <proposal_id>`")
    try: pid = int(context.args[0])
    except: return
    
    conn = await get_db_connection()
    if not conn: return await update.message.reply_text("❌ Database Error.")
    
    prop = await conn.fetchrow("SELECT * FROM proposals WHERE id=$1 AND status='pending'", pid)
    
    if not prop: 
        await conn.close()
        return await update.message.reply_text("❌ Invalid proposal.")
    if prop['proposer_id'] != user_id: 
        await conn.close()
        return await update.message.reply_text("❌ Only the proposer can ping.")
    
    last_ping = prop['last_pinged_at']
    if last_ping and datetime.now() - last_ping < timedelta(hours=24):
        await conn.close()
        return await update.message.reply_text("⏳ You can only ping once every 24 hours.")

    try: chat = await context.bot.get_chat(prop['group_id'])
    except: 
        await conn.close()
        return await update.message.reply_text("❌ Cannot access group.")
    
    current_admins = await chat.get_administrators()
    
    voted_ids = [r['user_id'] for r in await conn.fetch("SELECT user_id FROM votes WHERE proposal_id=$1", pid)]
        
    missing_admins = [a for a in current_admins if a.user.id not in voted_ids and not a.user.is_bot]
    if not missing_admins: 
        await conn.close()
        return await update.message.reply_text("✅ Everyone has voted!")
    
    sent_count = 0
    for ma in missing_admins:
        try:
            await context.bot.send_message(ma.user.id, f"🔔 **Reminder:** Vote on Proposal #{pid}.\nGroup: {chat.title}\nReply `/vote {pid} yes/no`.")
            sent_count += 1
        except: pass
        
    await conn.execute("UPDATE proposals SET last_pinged_at=CURRENT_TIMESTAMP WHERE id=$1", pid)
    await conn.close()
    await update.message.reply_text(f"✅ Reminders sent to {sent_count} people.")

async def cancel_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    if not context.args: return await update.message.reply_text("Usage: `/cancel <id>`")
    
    conn = await get_db_connection()
    if not conn: return await update.message.reply_text("❌ Database Error.")
    
    try:
        pid = int(context.args[0])
        prop = await conn.fetchrow("SELECT * FROM proposals WHERE id=$1 AND status='pending'", pid)
        if not prop: 
            await conn.close()
            return await update.message.reply_text("❌ Invalid ID.")
        if prop['proposer_id'] != update.message.from_user.id:
            await conn.close()
            return await update.message.reply_text("❌ You can only cancel your own proposals.")
        await conn.execute("UPDATE proposals SET status='cancelled' WHERE id=$1", pid)
        await conn.close()
        await update.message.reply_text(f"🗑 **Proposal #{pid} Cancelled.**")
    except: await update.message.reply_text("❌ Error.")

# --- COMMANDS ---

async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2: return await update.message.reply_text("Usage: `/vote <id> <yes/no>`")
    try: await cast_vote(update, context, int(args[0]), args[1].lower())
    except Exception as e: 
        logger.error(f"Vote error: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    args = context.args
    if len(args) < 2: 
        s = await get_settings(update.effective_chat.id)
        al, af, en = ("🟢" if s['anti_link'] else "🔴"), ("🟢" if s['anti_forward'] else "🔴"), ("🟢" if s['engagement_tracking'] else "🔴")
        return await update.message.reply_text(f"⚙️ **Dashboard**\nAnti-Link: {al}\nAnti-Forward: {af}\nEngagement: {en}\nTo change: `/config anti_link on`")
    
    key, val = args[0].lower(), args[1].lower()
    if key in ['anti_link', 'anti_forward', 'engagement_tracking'] and val in ['on', 'off']:
        await create_proposal(update, context, 'config', key, 'true' if val=='on' else 'false')

async def blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    args = context.args
    if not args: return await update.message.reply_text("🚫 Usage: `/blacklist add <word>`")
    action, word = args[0].lower(), " ".join(args[1:]).lower()
    if action == 'add': await create_proposal(update, context, 'blacklist_add', 'word', word)
    elif action in ['remove', 'rm']: await create_proposal(update, context, 'blacklist_remove', 'word', word)

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ Not found.")
    if target.id == BOT_CREATOR_ID: return await update.message.reply_text("👑 Creator Immunity.")
    if await is_admin(update, target.id): return await update.message.reply_text("🛡 Admin Immune.")
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "Violation"
    try:
        await update.effective_chat.ban_member(target.id)
        await log_punishment(update.effective_chat.id, target.id, update.message.from_user.id, 'ban', reason)
        await update.message.reply_text(f"🚫 **BANNED** {target.mention_html()}\n📝 {reason}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ban error: {e}")
        await update.message.reply_text("❌ Failed.")

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ Not found.")
    punisher_id = await get_punisher(update.effective_chat.id, target.id, 'ban')
    if punisher_id != update.message.from_user.id and not await is_group_owner(update):
        return await update.message.reply_text("❌ Only the admin who banned or the owner can unban.")
    try:
        await update.effective_chat.unban_member(target.id)
        conn = await get_db_connection()
        if conn:
            await conn.execute("DELETE FROM punishment_logs WHERE group_id=$1 AND target_id=$2 AND type='ban'", update.effective_chat.id, target.id)
            await conn.close()
        await update.message.reply_text(f"✅ **UNBANNED** {target.mention_html()}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Unban error: {e}")
        await update.message.reply_text("❌ Failed.")

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ Not found.")
    if await is_admin(update, target.id): return await update.message.reply_text("🛡 Immune.")
    args = context.args
    duration = None
    reason_idx = 0
    if args:
        match = re.match(r'^(\d+)(m|h|d)$', args[0].lower())
        if match:
            sec = int(match.group(1)) * (60 if match.group(2)=='m' else 3600 if match.group(2)=='h' else 86400)
            duration = timedelta(seconds=sec)
            reason_idx = 1
    reason = " ".join(args[reason_idx:]) if len(args) > reason_idx else "Violation"
    try:
        until = datetime.now() + duration if duration else None
        await update.effective_chat.restrict_member(target.id, ChatPermissions(can_send_messages=False), until_date=until)
        await log_punishment(update.effective_chat.id, target.id, update.message.from_user.id, 'mute', reason)
        await update.message.reply_text(f"🤐 **MUTED** {target.mention_html()}.\n📝 {reason}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Mute error: {e}")
        await update.message.reply_text("❌ Failed.")

async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ Not found.")
    punisher_id = await get_punisher(update.effective_chat.id, target.id, 'mute')
    if punisher_id != update.message.from_user.id and not await is_group_owner(update):
        return await update.message.reply_text("❌ Only the admin who muted or the owner can unmute.")
    try:
        await update.effective_chat.restrict_member(target.id, ChatPermissions(can_send_messages=True))
        conn = await get_db_connection()
        if conn:
            await conn.execute("DELETE FROM punishment_logs WHERE group_id=$1 AND target_id=$2 AND type='mute'", update.effective_chat.id, target.id)
            await conn.close()
        await update.message.reply_text(f"✅ **UNMUTED** {target.mention_html()}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Unmute error: {e}")
        await update.message.reply_text("❌ Failed.")

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ Not found.")
    if await is_admin(update, target.id): return await update.message.reply_text("🛡 Immune.")
    warns, _ = await update_user_stats(update.effective_chat.id, target.id, target.username, warn_inc=1)
    if warns >= 5:
        await update.effective_chat.ban_member(target.id)
        await log_punishment(update.effective_chat.id, target.id, update.message.from_user.id, 'ban', "Auto-ban after 5 warns")
        await update.message.reply_text(f"🚫 **AUTO-BAN:** {target.mention_html()} (5 Warns).", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"⚠️ **WARNED** {target.mention_html()} ({warns}/5).", parse_mode=ParseMode.HTML)

async def reset_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_owner(update): return await update.message.reply_text("❌ Only the owner can reset the leaderboard.")
    conn = await get_db_connection()
    if not conn: return await update.message.reply_text("❌ Database Error.")
    gid = update.effective_chat.id
    await conn.execute("UPDATE user_stats SET total_points = 0, points_today = 0 WHERE group_id = $1", gid)
    await conn.close()
    await update.message.reply_text("🔄 **Leaderboard Reset.** All points cleared.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return await update.message.reply_text("❌ Admins and owners only.")
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ User not found. Reply to a message or use /stats @username.")
    try:
        member = await update.effective_chat.get_member(target.id)
        if member.status == ChatMemberStatus.LEFT or member.status == ChatMemberStatus.KICKED:
            status = "🚫 Not a member"
        elif member.status == ChatMemberStatus.BANNED:
            status = "🚫 Banned"
        elif not member.can_send_messages:
            status = "🤐 Muted"
        else:
            status = "✅ Free to speak"
        await update.message.reply_text(f"📊 **Status for {target.mention_html()}:** {status}", parse_mode=ParseMode.HTML)
    except:
        await update.message.reply_text(f"📊 **Status for {target.mention_html()}:** 🚫 Not a member", parse_mode=ParseMode.HTML)

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message: return await update.message.reply_text("❌ Reply to a message to report it.")
    reported_msg = update.message.reply_to_message
    reporter_id = update.message.from_user.id
    reported_user = reported_msg.from_user
    text = f"Report from @{update.message.from_user.username or update.message.from_user.id}:\nReported user: @{reported_user.username or reported_user.id}\nMessage: {reported_msg.text or '[Non-text message]'}"
    await save_suggestion(update.effective_chat.id, reporter_id, text, status='pending', s_type='report')
    await update.message.reply_text("✅ Report saved for admins to review.")

async def toplevel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await get_db_connection()
    if not conn: return await update.message.reply_text("❌ Database Error.")
    gid = update.effective_chat.id
    rows = await conn.fetch("SELECT username, total_points FROM user_stats WHERE group_id=$1 ORDER BY total_points DESC LIMIT 10", gid)
    await conn.close()
    if not rows:
        return await update.message.reply_text("📈 No active users yet.")
    leaderboard = "\n".join([f"{i+1}. @{row['username']} - {row['total_points']} points" for i, row in enumerate(rows)])
    await update.message.reply_text(f"🏆 **Top 10 Active Participants (by points):**\n{leaderboard}")

async def me_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await get_db_connection()
    if not conn: return await update.message.reply_text("❌ Database Error.")
    gid = update.effective_chat.id
    uid = update.message.from_user.id
    row = await conn.fetchrow("SELECT total_points FROM user_stats WHERE group_id=$1 AND user_id=$2", gid, uid)
    points = row['total_points'] if row else 0
    rank = await conn.fetchval("SELECT COUNT(*) + 1 AS rank FROM user_stats WHERE group_id=$1 AND total_points > $2", gid, points)
    await conn.close()
    await update.message.reply_text(f"👤 **Your Stats:**\nPoints: {points}\nRank: #{rank}")

async def suggest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text: return await update.message.reply_text("❌ Provide a suggestion: /suggest Your idea here.")
    await save_suggestion(update.effective_chat.id, update.message.from_user.id, text, status='pending', s_type='suggestion')
    await update.message.reply_text("✅ Suggestion saved for admins to review.")

async def view_suggestions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private' or not await is_admin(update): return await update.message.reply_text("❌ Use in DM, admins only.")
    gid = admin_sessions.get(update.effective_user.id)
    if not gid: return await update.message.reply_text("❌ Enter manage mode first (/start manage_... in DM).")
    rows = await get_suggestions(gid, 'suggestion')
    if not rows:
        return await update.message.reply_text("📭 No suggestions.")
    text = "\n".join([f"#{r['id']} by {r['user_id']}: {r['suggestion']} ({r['status']})" for r in rows])
    await update.message.reply_text(f"📝 **Suggestions:**\n{text}\nOwner: Propose /config suggestion_status <id> implemented/denied")

async def view_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private' or not await is_admin(update): return await update.message.reply_text("❌ Use in DM, admins only.")
    gid = admin_sessions.get(update.effective_user.id)
    if not gid: return await update.message.reply_text("❌ Enter manage mode first.")
    rows = await get_suggestions(gid, 'report')
    if not rows:
        return await update.message.reply_text("📭 No reports.")
    text = "\n".join([f"#{r['id']} by {r['user_id']}: {r['suggestion']} ({r['status']})" for r in rows])
    await update.message.reply_text(f"📝 **Reports:**\n{text}\nPropose /config report_status <id> resolved to vote on closure.")

async def anon_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_admin(update): return await update.message.reply_text("❌ Admins use /link or send normally.")
    conn = await get_db_connection()
    if not conn: return await update.message.reply_text("❌ Database Error.")
    gid = update.effective_chat.id
    uid = update.message.from_user.id
    today = date.today()
    row = await conn.fetchrow("SELECT last_anon_link_date FROM user_stats WHERE group_id=$1 AND user_id=$2", gid, uid)
    last_date = row['last_anon_link_date'] if row else None
    if last_date == today:
        await conn.close()
        return await update.message.reply_text("❌ Once per day limit reached.")
    await conn.execute("UPDATE user_stats SET last_anon_link_date = $1 WHERE group_id=$2 AND user_id=$3", today, gid, uid)
    await conn.close()
    await update.message.reply_text(f"🔗 https://t.me/{context.bot.username}?start={gid}")

# --- Other Handlers ---
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private': return
    args = context.args
    uid = update.message.from_user.id
    if args and args[0].startswith("manage_"):
        gid = int(args[0].split("_")[1])
        admin_sessions[uid] = gid
        await update.message.reply_text(f"⚙️ **Manage Mode**\nUse `/filter trigger | response`, /view_suggestions, /view_reports")
    elif args and args[0]:
        gid = int(args[0])
        user_sessions[uid] = gid  # Keep memory for speed
        conn = await get_db_connection()
        if conn:
            await conn.execute("INSERT INTO user_connections (user_id, group_id) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET group_id=$2", uid, gid)
            await conn.close()
        await update.message.reply_text("🔗 **Anon Mode Connected**")
    else: await update.message.reply_text("👋 Bot Online.")

async def handle_anon_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private' or update.message.text.startswith("/"): return
    uid = update.message.from_user.id
    if uid not in user_sessions:
        conn = await get_db_connection()
        if conn:
            row = await conn.fetchrow("SELECT group_id FROM user_connections WHERE user_id=$1", uid)
            if row:
                user_sessions[uid] = row['group_id']  # Load from DB if not in memory
            await conn.close()
    if uid in user_sessions:
        try:
            sent = await context.bot.send_message(user_sessions[uid], f"<b>🕶 Anonymous:</b>\n{update.message.text}", parse_mode=ParseMode.HTML)
            await log_anon_message(user_sessions[uid], sent.message_id, uid)
            await update.message.reply_text("✅ Sent.")
        except Exception as e:
            logger.error(f"Anon send error: {e}")
            await update.message.reply_text("❌ Failed to send anonymous message.")

async def group_police(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return
    chat, user = update.effective_chat, msg.from_user
    txt = (msg.text or "").lower()
    
    conn = await get_db_connection()
    if not conn: return
    
    row = await conn.fetchrow("SELECT response FROM custom_filters WHERE group_id=$1 AND trigger=$2", chat.id, txt)
    if row: 
        reply_msg = await update.message.reply_text(row['response'])
        await asyncio.sleep(5)
        try:
            await msg.delete()
            await reply_msg.delete()
        except: pass

    admins = await chat.get_administrators()
    is_user_admin = user.id in [a.user.id for a in admins]
    if not is_user_admin:
        settings = await get_settings(chat.id)
        if settings['engagement_tracking']:
            await update_user_stats(chat.id, user.id, user.username, 5 if msg.text else 2)
        
        bad_words = [r['word'] for r in await conn.fetch("SELECT word FROM blacklists WHERE group_id=$1", chat.id)]
        for bw in bad_words:
            if bw in txt:
                try:
                    await msg.delete()
                    await chat.restrict_member(user.id, ChatPermissions(can_send_messages=False), until_date=datetime.now()+timedelta(minutes=5))
                    await context.bot.send_message(chat.id, f"🤐 **Muted:** {user.first_name}\nTrigger: ||{bw}||", parse_mode=ParseMode.MARKDOWN_V2)
                    return
                except Exception as e:
                    logger.error(f"Blacklist mute error: {e}")
        violation_inc = 0
        if settings['anti_link'] and re.search(r'(https?://|www\.|t\.me/)', txt):
            violation_inc += 1
            try:
                await msg.delete()
                await chat.restrict_member(user.id, ChatPermissions(can_send_messages=False), until_date=datetime.now()+timedelta(minutes=5))
                await context.bot.send_message(chat.id, f"🤐 **Muted:** {user.first_name} (Link)")
            except Exception as e:
                logger.error(f"Anti-link error: {e}")
        if settings['anti_forward'] and msg.forward_date:
            violation_inc += 1
            try:
                await msg.delete()
                await chat.restrict_member(user.id, ChatPermissions(can_send_messages=False), until_date=datetime.now()+timedelta(minutes=5))
                await context.bot.send_message(chat.id, f"🤐 **Muted:** {user.first_name} (Forward)")
            except Exception as e:
                logger.error(f"Anti-forward error: {e}")
        if violation_inc > 0:
            _, violations = await update_user_stats(chat.id, user.id, user.username, violation_inc=violation_inc)
            if violations >= DAILY_VIOLATION_CAP:
                try:
                    await chat.ban_member(user.id)
                    await context.bot.send_message(chat.id, f"🚫 **AUTO-BAN:** {user.mention_html()} (Exceeded daily violations).", parse_mode=ParseMode.HTML)
                except Exception as e:
                    logger.error(f"Auto-ban error: {e}")
    await conn.close()

# --- Misc ---
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    is_ad = await is_admin(update)
    is_own = await is_group_owner(update)
    base = "🤖 /me, /toplevel, /suggest, /report, /link, /support, /help, /anon_link"
    if is_ad: base += "\n👮‍♂️ /ban, /unban, /mute, /unmute, /warn, /stats, /blacklist, /config, /manage, /trace"
    if is_own: base += "\n👑 /reset"
    await update.message.reply_text(base)

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    await update.message.reply_text(f"🛠 Support: https://t.me/{SUPPORT_USER.replace('@','')}")

async def trace(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if not update.message.reply_to_message: return
    uid = await get_original_sender(update.effective_chat.id, update.message.reply_to_message.message_id)
    if uid: await update.message.reply_text(f"🕵️ [Profile](tg://user?id={uid})", parse_mode=ParseMode.MARKDOWN)

async def link(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    await update.message.reply_text(f"🔗 https://t.me/{context.bot.username}?start={update.effective_chat.id}")

async def manage_group(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    await update.message.reply_text(f"⚙️ DM Config: https://t.me/{context.bot.username}?start=manage_{update.effective_chat.id}")

async def add_filter(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if update.effective_chat.type != 'private': return
    gid = admin_sessions.get(update.effective_user.id)
    if not gid: return
    raw = update.message.text[len("/filter "):].strip().split("|", 1)
    if len(raw) < 2: return
    conn = await get_db_connection()
    if conn:
        await conn.execute("INSERT INTO custom_filters (group_id, trigger, response) VALUES ($1, $2, $3) ON CONFLICT (group_id, trigger) DO UPDATE SET response=$3", gid, raw[0].strip().lower(), raw[1].strip())
        await conn.close()
    await update.message.reply_text("✅ Saved.")

async def del_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = admin_sessions.get(update.effective_user.id)
    if not gid: return
    trig = " ".join(context.args).lower().strip()
    conn = await get_db_connection()
    if conn:
        await conn.execute("DELETE FROM custom_filters WHERE group_id=$1 AND trigger=$2", gid, trig)
        await conn.close()
    await update.message.reply_text("🗑 Deleted.")

global app_bot
if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    app_bot = ApplicationBuilder().token(TOKEN).build()
    
    app_bot.add_handler(CommandHandler("vote", vote_command))
    app_bot.add_handler(CommandHandler("ping", ping_voters))
    app_bot.add_handler(CommandHandler("cancel", cancel_proposal))
    app_bot.add_handler(CommandHandler("config", config))
    app_bot.add_handler(CommandHandler("blacklist", blacklist))
    app_bot.add_handler(CommandHandler("ban", ban_user))
    app_bot.add_handler(CommandHandler("unban", unban_user))
    app_bot.add_handler(CommandHandler("mute", mute_user))
    app_bot.add_handler(CommandHandler("unmute", unmute_user))
    app_bot.add_handler(CommandHandler("warn", warn_user))
    app_bot.add_handler(CommandHandler("start", start_handler))
    app_bot.add_handler(CommandHandler("help", help_cmd))
    app_bot.add_handler(CommandHandler("manage", manage_group))
    app_bot.add_handler(CommandHandler("filter", add_filter))
    app_bot.add_handler(CommandHandler("stopfilter", del_filter))
    app_bot.add_handler(CommandHandler("support", support))
    app_bot.add_handler(CommandHandler("stats", stats))
    app_bot.add_handler(CommandHandler("trace", trace))
    app_bot.add_handler(CommandHandler("report", report))
    app_bot.add_handler(CommandHandler("suggest", suggest))
    app_bot.add_handler(CommandHandler("link", link))
    app_bot.add_handler(CommandHandler("anon_link", anon_link))
    app_bot.add_handler(CommandHandler("toplevel", toplevel))
    app_bot.add_handler(CommandHandler("me", me_stats))
    app_bot.add_handler(CommandHandler("reset", reset_leaderboard))
    app_bot.add_handler(CommandHandler("view_suggestions", view_suggestions))
    app_bot.add_handler(CommandHandler("view_reports", view_reports))
    
    app_bot.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_anon_dm))
    app_bot.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, group_police))

    app_bot.run_polling()async def update_setting_db(group_id, key, val):
    allowed_keys = ['anti_link', 'anti_forward', 'engagement_tracking']
    if key not in allowed_keys: return
    conn = await get_db_connection()
    if not conn: return
    await conn.execute(f"UPDATE group_settings SET {key} = $1 WHERE group_id = $2", val, group_id)
    if group_id in group_settings_cache: group_settings_cache[group_id][key] = val
    await conn.close()

async def update_user_stats(group_id, user_id, username, points=0, warn_inc=0, violation_inc=0):
    conn = await get_db_connection()
    if not conn: return 0, 0
    today = date.today()
    row = await conn.fetchrow("SELECT * FROM user_stats WHERE group_id=$1 AND user_id=$2", group_id, user_id)
    p_today = row['points_today'] if row else 0
    v_today = row['violations_today'] if row else 0
    if row and row['last_active_date'] != today:
        p_today = 0
        v_today = 0
    final_points = points if (points > 0 and p_today < DAILY_POINT_CAP) else 0
    final_violations = v_today + violation_inc
    if row:
        await conn.execute("""UPDATE user_stats SET message_count = message_count + 1,
            total_points = total_points + $1, points_today = $2, last_active_date = $3,
            warns = warns + $4, username = COALESCE($5, username), violations_today = $6
            WHERE group_id=$7 AND user_id=$8""", 
            final_points, p_today + final_points, today, warn_inc, username, final_violations, group_id, user_id)
        await conn.close()
        return row['warns'] + warn_inc, final_violations
    else:
        await conn.execute("""INSERT INTO user_stats (group_id, user_id, username, message_count, total_points, points_today, last_active_date, warns, violations_today)
            VALUES ($1, $2, $3, 1, $4, $5, $6, $7, $8)""", group_id, user_id, username, final_points, final_points, today, warn_inc, violation_inc)
        await conn.close()
        return warn_inc, violation_inc

async def log_punishment(group_id, target_id, admin_id, p_type, reason):
    conn = await get_db_connection()
    if not conn: return
    await conn.execute("""INSERT INTO punishment_logs (group_id, target_id, admin_id, type, reason) VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (group_id, target_id, type) DO UPDATE SET admin_id=$3, reason=$5, timestamp=CURRENT_TIMESTAMP""",
        group_id, target_id, admin_id, p_type, reason)
    await conn.close()

async def get_punisher(group_id, target_id, p_type):
    conn = await get_db_connection()
    if not conn: return None
    row = await conn.fetchrow("SELECT admin_id FROM punishment_logs WHERE group_id=$1 AND target_id=$2 AND type=$3", group_id, target_id, p_type)
    await conn.close()
    return row['admin_id'] if row else None

async def log_anon_message(group_id, message_id, user_id):
    conn = await get_db_connection()
    if not conn: return
    await conn.execute("INSERT INTO message_logs (group_id, message_id, user_id) VALUES ($1, $2, $3)", group_id, message_id, user_id)
    await conn.close()

async def get_original_sender(group_id, message_id):
    conn = await get_db_connection()
    if not conn: return None
    row = await conn.fetchrow("SELECT user_id FROM message_logs WHERE group_id = $1 AND message_id = $2", group_id, message_id)
    await conn.close()
    return row['user_id'] if row else None

async def save_suggestion(group_id, user_id, text, status='pending', s_type='suggestion'):
    conn = await get_db_connection()
    if not conn: return
    await conn.execute("INSERT INTO suggestions (group_id, user_id, suggestion, status, type) VALUES ($1, $2, $3, $4, $5)", group_id, user_id, text, status, s_type)
    await conn.close()

async def get_suggestions(group_id, s_type='suggestion'):
    conn = await get_db_connection()
    if not conn: return []
    rows = await conn.fetch("SELECT * FROM suggestions WHERE group_id=$1 AND type=$2 ORDER BY id DESC", group_id, s_type)
    await conn.close()
    return rows

async def delete_suggestion(s_id):
    conn = await get_db_connection()
    if not conn: return
    await conn.execute("DELETE FROM suggestions WHERE id=$1", s_id)
    await conn.close()

# --- Permission Checks ---
async def is_admin(update: Update, user_id=None):
    if not user_id: user_id = update.message.from_user.id
    if user_id == BOT_CREATOR_ID: return True 
    chat = update.effective_chat
    if chat.type == 'private': return False
    try:
        member = await chat.get_member(user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except: return False

async def is_group_owner(update: Update, user_id=None):
    if not user_id: user_id = update.message.from_user.id
    if user_id == BOT_CREATOR_ID: return True
    try:
        member = await update.effective_chat.get_member(user_id)
        return member.status == ChatMemberStatus.OWNER
    except: return False

async def get_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message: return update.message.reply_to_message.from_user
    if context.args:
        username = context.args[0].replace("@", "")
        conn = await get_db_connection()
        if conn:
            row = await conn.fetchrow("SELECT user_id FROM user_stats WHERE group_id=$1 AND username=$2", update.effective_chat.id, username)
            if row:
                try:
                    member = await update.effective_chat.get_member(row['user_id'])
                    await conn.close()
                    return member.user
                except:
                    await conn.close()
    return None

# --- GOVERNANCE LOGIC ---

async def create_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE, type, target, value):
    gid = update.effective_chat.id
    uid = update.message.from_user.id
    
    if type == 'config' and target in OWNER_ONLY_CONFIGS:
        if not await is_group_owner(update):
            await update.message.reply_text(f"❌ **Denied.** Only the Owner can propose changes to `{target}`.")
            return

    conn = await get_db_connection()
    if not conn: return await update.message.reply_text("❌ Database Error.")
    
    pid = await conn.fetchval("""INSERT INTO proposals (group_id, proposer_id, action_type, key_target, value_target)
        VALUES ($1, $2, $3, $4, $5) RETURNING id""", gid, uid, type, target, value)
    await conn.close()
    
    val_disp = value if value else target
    proposal_text = (
        f"🗳 **New Proposal #{pid}**\n"
        f"**Group:** {update.effective_chat.title}\n"
        f"**Action:** {type.replace('_', ' ').title()}\n"
        f"**Target:** {val_disp}\n\n"
        f"⚠️ **Secret Ballot:** Votes are hidden.\n"
        f"To Vote: Reply with `/vote {pid} yes` or `no` here in DM."
    )

    await update.message.reply_text(f"✅ **Proposal #{pid} Created.**\nI am sending DMs to all admins now.")
    
    admins = await update.effective_chat.get_administrators()
    for a in admins:
        if not a.user.is_bot:
            try: await context.bot.send_message(chat_id=a.user.id, text=proposal_text, parse_mode=ParseMode.MARKDOWN)
            except: pass 
            
    await cast_vote(update, context, pid, 'yes', auto=True, group_context=update.effective_chat)

async def cast_vote(update: Update, context: ContextTypes.DEFAULT_TYPE, pid, vote, auto=False, group_context=None):
    conn = await get_db_connection()
    if not conn: 
        if not auto: await update.message.reply_text("❌ Database Error.")
        return

    user_id = update.message.from_user.id
    
    prop = await conn.fetchrow("SELECT * FROM proposals WHERE id=$1 AND status='pending'", pid)
    
    if not prop:
        if not auto: await update.message.reply_text("❌ Proposal invalid or closed.")
        await conn.close()
        return

    age = datetime.now() - prop['created_at']
    if age > timedelta(days=30):
        await conn.execute("UPDATE proposals SET status='expired' WHERE id=$1", pid)
        if not auto: await update.message.reply_text("❌ Proposal expired.")
        await conn.close()
        return

    await conn.execute("INSERT INTO votes (proposal_id, user_id, vote) VALUES ($1, $2, $3) ON CONFLICT (proposal_id, user_id) DO UPDATE SET vote=$3",
                   pid, user_id, vote)
    
    if not auto: await update.message.reply_text(f"✅ Vote saved for #{pid}.")

    try:
        if group_context: chat = group_context
        else: chat = await context.bot.get_chat(prop['group_id'])
        current_admins = await chat.get_administrators()
    except: 
        await conn.close()
        return

    current_admin_ids = [a.user.id for a in current_admins if not a.user.is_bot]
    
    voted_ids = [r['user_id'] for r in await conn.fetch("SELECT user_id FROM votes WHERE proposal_id=$1", pid)]
    
    valid_votes_count = len([uid for uid in voted_ids if uid in current_admin_ids])
    missing = len(current_admin_ids) - valid_votes_count
    
    if missing > 0: 
        await conn.close()
        return 

    owner_id = next((a.user.id for a in current_admins if a.status == ChatMemberStatus.OWNER), None)
    admins_except_owner = [a for a in current_admins if a.user.id != owner_id and not a.user.is_bot]
    admin_pool_count = len(admins_except_owner)
    
    if prop['action_type'].startswith('blacklist_'):
        # For blacklist, exclude owner from required voters/weights for faster process
        owner_weight = 0
        admin_weight = 100.0 / admin_pool_count if admin_pool_count > 0 else 0
    else:
        if admin_pool_count == 0 and owner_id:
            owner_weight = 100.0
            admin_weight = 0
        elif not owner_id:
            owner_weight = 0
            admin_weight = 100.0 / admin_pool_count if admin_pool_count > 0 else 0
        else:
            owner_weight = 50.0
            admin_weight = 50.0 / admin_pool_count if admin_pool_count > 0 else 0
    
    score_yes = 0.0
    
    all_votes = await conn.fetch("SELECT user_id, vote FROM votes WHERE proposal_id=$1", pid)
        
    for v in all_votes:
        uid, choice = v['user_id'], v['vote']
        if uid in current_admin_ids and choice == 'yes':
            if uid == owner_id and prop['action_type'].startswith('blacklist_') == False:
                score_yes += owner_weight
            elif uid != owner_id:
                score_yes += admin_weight
            
    if score_yes >= 50.0:
        await execute_proposal(context, chat, prop)
    else:
        await conn.execute("UPDATE proposals SET status='rejected' WHERE id=$1", pid)
        await context.bot.send_message(prop['group_id'], f"❌ **Proposal #{pid} Rejected.**\nConsensus was not reached.", parse_mode=ParseMode.MARKDOWN)
    await conn.close()

async def execute_proposal(context: ContextTypes.DEFAULT_TYPE, chat, prop):
    conn = await get_db_connection()
    if not conn: return
    gid = prop['group_id']
    target = prop['key_target']
    val = prop['value_target']
    
    msg = ""
    if prop['action_type'] == 'config':
        await update_setting_db(gid, target, val == 'true')
        msg = f"✅ **Proposal #{prop['id']} Passed!**\nSetting `{target}` updated."
    elif prop['action_type'] == 'blacklist_add':
        await conn.execute("INSERT INTO blacklists VALUES ($1, $2) ON CONFLICT DO NOTHING", gid, val)
        msg = f"✅ **Proposal #{prop['id']} Passed!**\nBlacklist updated."
    elif prop['action_type'] == 'blacklist_remove':
        await conn.execute("DELETE FROM blacklists WHERE group_id=$1 AND word=$2", gid, val)
        msg = f"✅ **Proposal #{prop['id']} Passed!**\nBlacklist updated."
    elif prop['action_type'] == 'suggestion_status':
        await delete_suggestion(target)  # target is suggestion ID
        msg = f"✅ **Proposal #{prop['id']} Passed!**\nSuggestion #{target} marked as {val} and deleted."
    elif prop['action_type'] == 'report_status':
        await delete_suggestion(target)
        msg = f"✅ **Proposal #{prop['id']} Passed!**\nReport #{target} marked as taken care of and deleted."

    await conn.execute("UPDATE proposals SET status='passed' WHERE id=$1", prop['id'])
    await context.bot.send_message(gid, msg, parse_mode=ParseMode.MARKDOWN)
    await conn.close()

async def ping_voters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not context.args: return await update.message.reply_text("Usage: `/ping <proposal_id>`")
    try: pid = int(context.args[0])
    except: return
    
    conn = await get_db_connection()
    if not conn: return await update.message.reply_text("❌ Database Error.")
    
    prop = await conn.fetchrow("SELECT * FROM proposals WHERE id=$1 AND status='pending'", pid)
    
    if not prop: 
        await conn.close()
        return await update.message.reply_text("❌ Invalid proposal.")
    if prop['proposer_id'] != user_id: 
        await conn.close()
        return await update.message.reply_text("❌ Only the proposer can ping.")
    
    last_ping = prop['last_pinged_at']
    if last_ping and datetime.now() - last_ping < timedelta(hours=24):
        await conn.close()
        return await update.message.reply_text("⏳ You can only ping once every 24 hours.")

    try: chat = await context.bot.get_chat(prop['group_id'])
    except: 
        await conn.close()
        return await update.message.reply_text("❌ Cannot access group.")
    
    current_admins = await chat.get_administrators()
    
    voted_ids = [r['user_id'] for r in await conn.fetch("SELECT user_id FROM votes WHERE proposal_id=$1", pid)]
        
    missing_admins = [a for a in current_admins if a.user.id not in voted_ids and not a.user.is_bot]
    if not missing_admins: 
        await conn.close()
        return await update.message.reply_text("✅ Everyone has voted!")
    
    sent_count = 0
    for ma in missing_admins:
        try:
            await context.bot.send_message(ma.user.id, f"🔔 **Reminder:** Vote on Proposal #{pid}.\nGroup: {chat.title}\nReply `/vote {pid} yes/no`.")
            sent_count += 1
        except: pass
        
    await conn.execute("UPDATE proposals SET last_pinged_at=CURRENT_TIMESTAMP WHERE id=$1", pid)
    await conn.close()
    await update.message.reply_text(f"✅ Reminders sent to {sent_count} people.")

async def cancel_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    if not context.args: return await update.message.reply_text("Usage: `/cancel <id>`")
    
    conn = await get_db_connection()
    if not conn: return await update.message.reply_text("❌ Database Error.")
    
    try:
        pid = int(context.args[0])
        prop = await conn.fetchrow("SELECT * FROM proposals WHERE id=$1 AND status='pending'", pid)
        if not prop: 
            await conn.close()
            return await update.message.reply_text("❌ Invalid ID.")
        if prop['proposer_id'] != update.message.from_user.id:
            await conn.close()
            return await update.message.reply_text("❌ You can only cancel your own proposals.")
        await conn.execute("UPDATE proposals SET status='cancelled' WHERE id=$1", pid)
        await conn.close()
        await update.message.reply_text(f"🗑 **Proposal #{pid} Cancelled.**")
    except: await update.message.reply_text("❌ Error.")

# --- COMMANDS ---

async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2: return await update.message.reply_text("Usage: `/vote <id> <yes/no>`")
    try: await cast_vote(update, context, int(args[0]), args[1].lower())
    except Exception as e: 
        logger.error(f"Vote error: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    args = context.args
    if len(args) < 2: 
        s = await get_settings(update.effective_chat.id)
        al, af, en = ("🟢" if s['anti_link'] else "🔴"), ("🟢" if s['anti_forward'] else "🔴"), ("🟢" if s['engagement_tracking'] else "🔴")
        return await update.message.reply_text(f"⚙️ **Dashboard**\nAnti-Link: {al}\nAnti-Forward: {af}\nEngagement: {en}\nTo change: `/config anti_link on`")
    
    key, val = args[0].lower(), args[1].lower()
    if key in ['anti_link', 'anti_forward', 'engagement_tracking'] and val in ['on', 'off']:
        await create_proposal(update, context, 'config', key, 'true' if val=='on' else 'false')

async def blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    args = context.args
    if not args: return await update.message.reply_text("🚫 Usage: `/blacklist add <word>`")
    action, word = args[0].lower(), " ".join(args[1:]).lower()
    if action == 'add': await create_proposal(update, context, 'blacklist_add', 'word', word)
    elif action in ['remove', 'rm']: await create_proposal(update, context, 'blacklist_remove', 'word', word)

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ Not found.")
    if target.id == BOT_CREATOR_ID: return await update.message.reply_text("👑 Creator Immunity.")
    if await is_admin(update, target.id): return await update.message.reply_text("🛡 Admin Immune.")
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "Violation"
    try:
        await update.effective_chat.ban_member(target.id)
        await log_punishment(update.effective_chat.id, target.id, update.message.from_user.id, 'ban', reason)
        await update.message.reply_text(f"🚫 **BANNED** {target.mention_html()}\n📝 {reason}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ban error: {e}")
        await update.message.reply_text("❌ Failed.")

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ Not found.")
    punisher_id = await get_punisher(update.effective_chat.id, target.id, 'ban')
    if punisher_id != update.message.from_user.id and not await is_group_owner(update):
        return await update.message.reply_text("❌ Only the admin who banned or the owner can unban.")
    try:
        await update.effective_chat.unban_member(target.id)
        conn = await get_db_connection()
        if conn:
            await conn.execute("DELETE FROM punishment_logs WHERE group_id=$1 AND target_id=$2 AND type='ban'", update.effective_chat.id, target.id)
            await conn.close()
        await update.message.reply_text(f"✅ **UNBANNED** {target.mention_html()}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Unban error: {e}")
        await update.message.reply_text("❌ Failed.")

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ Not found.")
    if await is_admin(update, target.id): return await update.message.reply_text("🛡 Immune.")
    args = context.args
    duration = None
    reason_idx = 0
    if args:
        match = re.match(r'^(\d+)(m|h|d)$', args[0].lower())
        if match:
            sec = int(match.group(1)) * (60 if match.group(2)=='m' else 3600 if match.group(2)=='h' else 86400)
            duration = timedelta(seconds=sec)
            reason_idx = 1
    reason = " ".join(args[reason_idx:]) if len(args) > reason_idx else "Violation"
    try:
        until = datetime.now() + duration if duration else None
        await update.effective_chat.restrict_member(target.id, ChatPermissions(can_send_messages=False), until_date=until)
        await log_punishment(update.effective_chat.id, target.id, update.message.from_user.id, 'mute', reason)
        await update.message.reply_text(f"🤐 **MUTED** {target.mention_html()}.\n📝 {reason}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Mute error: {e}")
        await update.message.reply_text("❌ Failed.")

async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ Not found.")
    punisher_id = await get_punisher(update.effective_chat.id, target.id, 'mute')
    if punisher_id != update.message.from_user.id and not await is_group_owner(update):
        return await update.message.reply_text("❌ Only the admin who muted or the owner can unmute.")
    try:
        await update.effective_chat.restrict_member(target.id, ChatPermissions(can_send_messages=True))
        conn = await get_db_connection()
        if conn:
            await conn.execute("DELETE FROM punishment_logs WHERE group_id=$1 AND target_id=$2 AND type='mute'", update.effective_chat.id, target.id)
            await conn.close()
        await update.message.reply_text(f"✅ **UNMUTED** {target.mention_html()}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Unmute error: {e}")
        await update.message.reply_text("❌ Failed.")

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ Not found.")
    if await is_admin(update, target.id): return await update.message.reply_text("🛡 Immune.")
    warns, _ = await update_user_stats(update.effective_chat.id, target.id, target.username, warn_inc=1)
    if warns >= 5:
        await update.effective_chat.ban_member(target.id)
        await log_punishment(update.effective_chat.id, target.id, update.message.from_user.id, 'ban', "Auto-ban after 5 warns")
        await update.message.reply_text(f"🚫 **AUTO-BAN:** {target.mention_html()} (5 Warns).", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"⚠️ **WARNED** {target.mention_html()} ({warns}/5).", parse_mode=ParseMode.HTML)

async def reset_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_owner(update): return await update.message.reply_text("❌ Only the owner can reset the leaderboard.")
    conn = await get_db_connection()
    if not conn: return await update.message.reply_text("❌ Database Error.")
    gid = update.effective_chat.id
    await conn.execute("UPDATE user_stats SET total_points = 0, points_today = 0 WHERE group_id = $1", gid)
    await conn.close()
    await update.message.reply_text("🔄 **Leaderboard Reset.** All points cleared.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return await update.message.reply_text("❌ Admins and owners only.")
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ User not found. Reply to a message or use /stats @username.")
    try:
        member = await update.effective_chat.get_member(target.id)
        if member.status == ChatMemberStatus.LEFT or member.status == ChatMemberStatus.KICKED:
            status = "🚫 Not a member"
        elif member.status == ChatMemberStatus.BANNED:
            status = "🚫 Banned"
        elif not member.can_send_messages:
            status = "🤐 Muted"
        else:
            status = "✅ Free to speak"
        await update.message.reply_text(f"📊 **Status for {target.mention_html()}:** {status}", parse_mode=ParseMode.HTML)
    except:
        await update.message.reply_text(f"📊 **Status for {target.mention_html()}:** 🚫 Not a member", parse_mode=ParseMode.HTML)

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message: return await update.message.reply_text("❌ Reply to a message to report it.")
    reported_msg = update.message.reply_to_message
    reporter_id = update.message.from_user.id
    reported_user = reported_msg.from_user
    text = f"Report from @{update.message.from_user.username or update.message.from_user.id}:\nReported user: @{reported_user.username or reported_user.id}\nMessage: {reported_msg.text or '[Non-text message]'}"
    await save_suggestion(update.effective_chat.id, reporter_id, text, status='pending', s_type='report')
    await update.message.reply_text("✅ Report saved for admins to review.")

async def toplevel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await get_db_connection()
    if not conn: return await update.message.reply_text("❌ Database Error.")
    gid = update.effective_chat.id
    rows = await conn.fetch("SELECT username, total_points FROM user_stats WHERE group_id=$1 ORDER BY total_points DESC LIMIT 10", gid)
    await conn.close()
    if not rows:
        return await update.message.reply_text("📈 No active users yet.")
    leaderboard = "\n".join([f"{i+1}. @{row['username']} - {row['total_points']} points" for i, row in enumerate(rows)])
    await update.message.reply_text(f"🏆 **Top 10 Active Participants (by points):**\n{leaderboard}")

async def me_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await get_db_connection()
    if not conn: return await update.message.reply_text("❌ Database Error.")
    gid = update.effective_chat.id
    uid = update.message.from_user.id
    row = await conn.fetchrow("SELECT total_points FROM user_stats WHERE group_id=$1 AND user_id=$2", gid, uid)
    points = row['total_points'] if row else 0
    rank = await conn.fetchval("SELECT COUNT(*) + 1 AS rank FROM user_stats WHERE group_id=$1 AND total_points > $2", gid, points)
    await conn.close()
    await update.message.reply_text(f"👤 **Your Stats:**\nPoints: {points}\nRank: #{rank}")

async def suggest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text: return await update.message.reply_text("❌ Provide a suggestion: /suggest Your idea here.")
    await save_suggestion(update.effective_chat.id, update.message.from_user.id, text, status='pending', s_type='suggestion')
    await update.message.reply_text("✅ Suggestion saved for admins to review.")

async def view_suggestions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private' or not await is_admin(update): return await update.message.reply_text("❌ Use in DM, admins only.")
    gid = admin_sessions.get(update.effective_user.id)
    if not gid: return await update.message.reply_text("❌ Enter manage mode first (/start manage_... in DM).")
    rows = await get_suggestions(gid, 'suggestion')
    if not rows:
        return await update.message.reply_text("📭 No suggestions.")
    text = "\n".join([f"#{r['id']} by {r['user_id']}: {r['suggestion']} ({r['status']})" for r in rows])
    await update.message.reply_text(f"📝 **Suggestions:**\n{text}\nOwner: Propose /config suggestion_status <id> implemented/denied")

async def view_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private' or not await is_admin(update): return await update.message.reply_text("❌ Use in DM, admins only.")
    gid = admin_sessions.get(update.effective_user.id)
    if not gid: return await update.message.reply_text("❌ Enter manage mode first.")
    rows = await get_suggestions(gid, 'report')
    if not rows:
        return await update.message.reply_text("📭 No reports.")
    text = "\n".join([f"#{r['id']} by {r['user_id']}: {r['suggestion']} ({r['status']})" for r in rows])
    await update.message.reply_text(f"📝 **Reports:**\n{text}\nPropose /config report_status <id> resolved to vote on closure.")

async def anon_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_admin(update): return await update.message.reply_text("❌ Admins use /link or send normally.")
    conn = await get_db_connection()
    if not conn: return await update.message.reply_text("❌ Database Error.")
    gid = update.effective_chat.id
    uid = update.message.from_user.id
    today = date.today()
    row = await conn.fetchrow("SELECT last_anon_link_date FROM user_stats WHERE group_id=$1 AND user_id=$2", gid, uid)
    last_date = row['last_anon_link_date'] if row else None
    if last_date == today:
        await conn.close()
        return await update.message.reply_text("❌ Once per day limit reached.")
    await conn.execute("UPDATE user_stats SET last_anon_link_date = $1 WHERE group_id=$2 AND user_id=$3", today, gid, uid)
    await conn.close()
    await update.message.reply_text(f"🔗 https://t.me/{context.bot.username}?start={gid}")

# --- Other Handlers ---
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private': return
    args = context.args
    uid = update.message.from_user.id
    if args and args[0].startswith("manage_"):
        gid = int(args[0].split("_")[1])
        admin_sessions[uid] = gid
        await update.message.reply_text(f"⚙️ **Manage Mode**\nUse `/filter trigger | response`, /view_suggestions, /view_reports")
    elif args and args[0]:
        gid = int(args[0])
        user_sessions[uid] = gid  # Keep memory for speed
        conn = await get_db_connection()
        if conn:
            await conn.execute("INSERT INTO user_connections (user_id, group_id) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET group_id=$2", uid, gid)
            await conn.close()
        await update.message.reply_text("🔗 **Anon Mode Connected**")
    else: await update.message.reply_text("👋 Bot Online.")

async def handle_anon_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private' or update.message.text.startswith("/"): return
    uid = update.message.from_user.id
    if uid not in user_sessions:
        conn = await get_db_connection()
        if conn:
            row = await conn.fetchrow("SELECT group_id FROM user_connections WHERE user_id=$1", uid)
            if row:
                user_sessions[uid] = row['group_id']  # Load from DB if not in memory
            await conn.close()
    if uid in user_sessions:
        try:
            sent = await context.bot.send_message(user_sessions[uid], f"<b>🕶 Anonymous:</b>\n{update.message.text}", parse_mode=ParseMode.HTML)
            await log_anon_message(user_sessions[uid], sent.message_id, uid)
            await update.message.reply_text("✅ Sent.")
        except Exception as e:
            logger.error(f"Anon send error: {e}")
            await update.message.reply_text("❌ Failed to send anonymous message.")

async def group_police(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return
    chat, user = update.effective_chat, msg.from_user
    txt = (msg.text or "").lower()
    
    conn = await get_db_connection()
    if not conn: return
    
    row = await conn.fetchrow("SELECT response FROM custom_filters WHERE group_id=$1 AND trigger=$2", chat.id, txt)
    if row: 
        reply_msg = await update.message.reply_text(row['response'])
        await asyncio.sleep(5)
        try:
            await msg.delete()
            await reply_msg.delete()
        except: pass

    admins = await chat.get_administrators()
    is_user_admin = user.id in [a.user.id for a in admins]
    if not is_user_admin:
        settings = await get_settings(chat.id)
        if settings['engagement_tracking']:
            await update_user_stats(chat.id, user.id, user.username, 5 if msg.text else 2)
        
        bad_words = [r['word'] for r in await conn.fetch("SELECT word FROM blacklists WHERE group_id=$1", chat.id)]
        for bw in bad_words:
            if bw in txt:
                try:
                    await msg.delete()
                    await chat.restrict_member(user.id, ChatPermissions(can_send_messages=False), until_date=datetime.now()+timedelta(minutes=5))
                    await context.bot.send_message(chat.id, f"🤐 **Muted:** {user.first_name}\nTrigger: ||{bw}||", parse_mode=ParseMode.MARKDOWN_V2)
                    return
                except Exception as e:
                    logger.error(f"Blacklist mute error: {e}")
        violation_inc = 0
        if settings['anti_link'] and re.search(r'(https?://|www\.|t\.me/)', txt):
            violation_inc += 1
            try:
                await msg.delete()
                await chat.restrict_member(user.id, ChatPermissions(can_send_messages=False), until_date=datetime.now()+timedelta(minutes=5))
                await context.bot.send_message(chat.id, f"🤐 **Muted:** {user.first_name} (Link)")
            except Exception as e:
                logger.error(f"Anti-link error: {e}")
        if settings['anti_forward'] and msg.forward_date:
            violation_inc += 1
            try:
                await msg.delete()
                await chat.restrict_member(user.id, ChatPermissions(can_send_messages=False), until_date=datetime.now()+timedelta(minutes=5))
                await context.bot.send_message(chat.id, f"🤐 **Muted:** {user.first_name} (Forward)")
            except Exception as e:
                logger.error(f"Anti-forward error: {e}")
        if violation_inc > 0:
            _, violations = await update_user_stats(chat.id, user.id, user.username, violation_inc=violation_inc)
            if violations >= DAILY_VIOLATION_CAP:
                try:
                    await chat.ban_member(user.id)
                    await context.bot.send_message(chat.id, f"🚫 **AUTO-BAN:** {user.mention_html()} (Exceeded daily violations).", parse_mode=ParseMode.HTML)
                except Exception as e:
                    logger.error(f"Auto-ban error: {e}")
    await conn.close()

# --- Misc ---
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    is_ad = await is_admin(update)
    is_own = await is_group_owner(update)
    base = "🤖 /me, /toplevel, /suggest, /report, /link, /support, /help, /anon_link"
    if is_ad: base += "\n👮‍♂️ /ban, /unban, /mute, /unmute, /warn, /stats, /blacklist, /config, /manage, /trace"
    if is_own: base += "\n👑 /reset"
    await update.message.reply_text(base)

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    await update.message.reply_text(f"🛠 Support: https://t.me/{SUPPORT_USER.replace('@','')}")

async def trace(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if not update.message.reply_to_message: return
    uid = await get_original_sender(update.effective_chat.id, update.message.reply_to_message.message_id)
    if uid: await update.message.reply_text(f"🕵️ [Profile](tg://user?id={uid})", parse_mode=ParseMode.MARKDOWN)

async def link(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    await update.message.reply_text(f"🔗 https://t.me/{context.bot.username}?start={update.effective_chat.id}")

async def manage_group(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    await update.message.reply_text(f"⚙️ DM Config: https://t.me/{context.bot.username}?start=manage_{update.effective_chat.id}")

async def add_filter(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if update.effective_chat.type != 'private': return
    gid = admin_sessions.get(update.effective_user.id)
    if not gid: return
    raw = update.message.text[len("/filter "):].strip().split("|", 1)
    if len(raw) < 2: return
    conn = await get_db_connection()
    if conn:
        await conn.execute("INSERT INTO custom_filters (group_id, trigger, response) VALUES ($1, $2, $3) ON CONFLICT (group_id, trigger) DO UPDATE SET response=$3", gid, raw[0].strip().lower(), raw[1].strip())
        await conn.close()
    await update.message.reply_text("✅ Saved.")

async def del_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = admin_sessions.get(update.effective_user.id)
    if not gid: return
    trig = " ".join(context.args).lower().strip()
    conn = await get_db_connection()
    if conn:
        await conn.execute("DELETE FROM custom_filters WHERE group_id=$1 AND trigger=$2", gid, trig)
        await conn.close()
    await update.message.reply_text("🗑 Deleted.")

global app_bot
if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    app_bot = ApplicationBuilder().token(TOKEN).build()
    
    app_bot.add_handler(CommandHandler("vote", vote_command))
    app_bot.add_handler(CommandHandler("ping", ping_voters))
    app_bot.add_handler(CommandHandler("cancel", cancel_proposal))
    app_bot.add_handler(CommandHandler("config", config))
    app_bot.add_handler(CommandHandler("blacklist", blacklist))
    app_bot.add_handler(CommandHandler("ban", ban_user))
    app_bot.add_handler(CommandHandler("unban", unban_user))
    app_bot.add_handler(CommandHandler("mute", mute_user))
    app_bot.add_handler(CommandHandler("unmute", unmute_user))
    app_bot.add_handler(CommandHandler("warn", warn_user))
    app_bot.add_handler(CommandHandler("start", start_handler))
    app_bot.add_handler(CommandHandler("help", help_cmd))
    app_bot.add_handler(CommandHandler("manage", manage_group))
    app_bot.add_handler(CommandHandler("filter", add_filter))
    app_bot.add_handler(CommandHandler("stopfilter", del_filter))
    app_bot.add_handler(CommandHandler("support", support))
    app_bot.add_handler(CommandHandler("stats", stats))
    app_bot.add_handler(CommandHandler("trace", trace))
    app_bot.add_handler(CommandHandler("report", report))
    app_bot.add_handler(CommandHandler("suggest", suggest))
    app_bot.add_handler(CommandHandler("link", link))
    app_bot.add_handler(CommandHandler("anon_link", anon_link))
    app_bot.add_handler(CommandHandler("toplevel", toplevel))
    app_bot.add_handler(CommandHandler("me", me_stats))
    app_bot.add_handler(CommandHandler("reset", reset_leaderboard))
    app_bot.add_handler(CommandHandler("view_suggestions", view_suggestions))
    app_bot.add_handler(CommandHandler("view_reports", view_reports))
    
    app_bot.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_anon_dm))
    app_bot.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, group_police))

    app_bot.run_polling()
