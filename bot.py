import asyncio
import os
import threading
import psycopg2
import re
import io
from datetime import date, datetime, timedelta
from flask import Flask
from psycopg2.extras import DictCursor
from telegram import Update, ChatPermissions
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.helpers import mention_html

# --- FAKE WEBSITE (To Keep Render Awake) ---
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

# Settings that ONLY the Owner can propose (Admins can vote, but not start)
OWNER_ONLY_CONFIGS = ['engagement_tracking']

# --- Caching ---
group_settings_cache = {}
user_sessions = {}
admin_sessions = {}

# --- Database Setup ---
conn = None
def get_db_connection():
    global conn
    try:
        if conn is None or conn.closed != 0:
            conn = psycopg2.connect(DB_URL, cursor_factory=DictCursor)
            conn.autocommit = True
            with conn.cursor() as cur:
                # 1. Standard Tables
                cur.execute("""CREATE TABLE IF NOT EXISTS group_settings (
                    group_id BIGINT PRIMARY KEY,
                    anti_link BOOLEAN DEFAULT FALSE,
                    anti_forward BOOLEAN DEFAULT FALSE,
                    engagement_tracking BOOLEAN DEFAULT TRUE);""")
                cur.execute("""CREATE TABLE IF NOT EXISTS blacklists (
                    group_id BIGINT, word TEXT, PRIMARY KEY (group_id, word));""")
                cur.execute("""CREATE TABLE IF NOT EXISTS user_stats (
                    group_id BIGINT, user_id BIGINT, username TEXT, 
                    message_count INT DEFAULT 0, total_points INT DEFAULT 0, 
                    points_today INT DEFAULT 0, last_active_date DATE DEFAULT CURRENT_DATE,
                    warns INT DEFAULT 0, PRIMARY KEY (group_id, user_id));""")
                cur.execute("""CREATE TABLE IF NOT EXISTS suggestions (
                    id SERIAL PRIMARY KEY, group_id BIGINT, user_id BIGINT, 
                    suggestion TEXT, status TEXT DEFAULT 'pending');""")
                cur.execute("""CREATE TABLE IF NOT EXISTS punishment_logs (
                    group_id BIGINT, target_id BIGINT, admin_id BIGINT,
                    type TEXT, reason TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (group_id, target_id, type));""")
                cur.execute("""CREATE TABLE IF NOT EXISTS custom_filters (
                    group_id BIGINT, trigger TEXT, response TEXT,
                    PRIMARY KEY (group_id, trigger));""")
                cur.execute("""CREATE TABLE IF NOT EXISTS message_logs (
                    group_id BIGINT, message_id BIGINT, user_id BIGINT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (group_id, message_id));""")
                
                # 2. Governance Tables
                cur.execute("""CREATE TABLE IF NOT EXISTS proposals (
                    id SERIAL PRIMARY KEY, group_id BIGINT, proposer_id BIGINT,
                    action_type TEXT, key_target TEXT, value_target TEXT,
                    status TEXT DEFAULT 'pending', 
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_pinged_at TIMESTAMP
                );""")
                cur.execute("""CREATE TABLE IF NOT EXISTS votes (
                    proposal_id INT, user_id BIGINT, vote TEXT,
                    PRIMARY KEY (proposal_id, user_id));""")
    except Exception as e:
        print(f"❌ Database Error: {e}")
    return conn

# --- Helper Functions ---
def get_settings(group_id):
    if group_id in group_settings_cache: return group_settings_cache[group_id]
    c = get_db_connection()
    with c.cursor() as cur:
        cur.execute("SELECT * FROM group_settings WHERE group_id=%s", (group_id,))
        row = cur.fetchone()
        if row: settings = dict(row)
        else:
            settings = {'anti_link': False, 'anti_forward': False, 'engagement_tracking': True}
            cur.execute("INSERT INTO group_settings (group_id) VALUES (%s)", (group_id,))
        group_settings_cache[group_id] = settings
        return settings

def update_setting_db(group_id, key, val):
    c = get_db_connection()
    with c.cursor() as cur:
        cur.execute(f"UPDATE group_settings SET {key}=%s WHERE group_id=%s", (val, group_id))
    if group_id in group_settings_cache: group_settings_cache[group_id][key] = val

def update_user_stats(group_id, user_id, username, points=0, warn_inc=0):
    c = get_db_connection()
    today = date.today()
    with c.cursor() as cur:
        cur.execute("SELECT * FROM user_stats WHERE group_id=%s AND user_id=%s", (group_id, user_id))
        row = cur.fetchone()
        p_today = row['points_today'] if row else 0
        if row and row['last_active_date'] != today: p_today = 0
        final_points = points if (points > 0 and p_today < DAILY_POINT_CAP) else 0
        if row:
            cur.execute("""UPDATE user_stats SET message_count = message_count + 1,
                total_points = total_points + %s, points_today = %s, last_active_date = %s,
                warns = warns + %s, username = COALESCE(%s, username)
                WHERE group_id=%s AND user_id=%s""", 
                (final_points, p_today + final_points, today, warn_inc, username, group_id, user_id))
            return row['warns'] + warn_inc
        else:
            cur.execute("""INSERT INTO user_stats (group_id, user_id, username, message_count, total_points, points_today, last_active_date, warns)
                VALUES (%s, %s, %s, 1, %s, %s, %s, %s)""", (group_id, user_id, username, final_points, final_points, today, warn_inc))
            return warn_inc

def log_punishment(group_id, target_id, admin_id, p_type, reason):
    c = get_db_connection()
    with c.cursor() as cur:
        cur.execute("""INSERT INTO punishment_logs (group_id, target_id, admin_id, type, reason) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (group_id, target_id, type) DO UPDATE SET admin_id=%s, reason=%s, timestamp=CURRENT_TIMESTAMP""",
            (group_id, target_id, admin_id, p_type, reason, admin_id, reason))

def get_punisher(group_id, target_id, p_type):
    c = get_db_connection()
    with c.cursor() as cur:
        cur.execute("SELECT admin_id FROM punishment_logs WHERE group_id=%s AND target_id=%s AND type=%s", (group_id, target_id, p_type))
        row = cur.fetchone()
        return row['admin_id'] if row else None

def log_anon_message(group_id, message_id, user_id):
    c = get_db_connection()
    with c.cursor() as cur:
        cur.execute("INSERT INTO message_logs (group_id, message_id, user_id) VALUES (%s, %s, %s)", (group_id, message_id, user_id))

def get_original_sender(group_id, message_id):
    c = get_db_connection()
    with c.cursor() as cur:
        cur.execute("SELECT user_id FROM message_logs WHERE group_id = %s AND message_id = %s", (group_id, message_id))
        row = cur.fetchone()
        return row['user_id'] if row else None

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
        c = get_db_connection()
        with c.cursor() as cur:
            cur.execute("SELECT user_id FROM user_stats WHERE group_id=%s AND username=%s", (update.effective_chat.id, username))
            row = cur.fetchone()
            if row:
                try: return (await update.effective_chat.get_member(row['user_id'])).user
                except: pass
    return None

# --- GOVERNANCE LOGIC (Secret Ballot) ---

async def create_proposal(update, type, target, value):
    gid = update.effective_chat.id
    uid = update.message.from_user.id
    
    # Permission Check
    if type == 'config' and target in OWNER_ONLY_CONFIGS:
        if not await is_group_owner(update):
            await update.message.reply_text(f"❌ **Denied.** Only the Owner can propose changes to `{target}`.")
            return

    c = get_db_connection()
    with c.cursor() as cur:
        cur.execute("""INSERT INTO proposals (group_id, proposer_id, action_type, key_target, value_target)
            VALUES (%s, %s, %s, %s, %s) RETURNING id""", (gid, uid, type, target, value))
        pid = cur.fetchone()['id']
    
    val_disp = value if value else target
    proposal_text = (
        f"🗳 **New Proposal #{pid}**\n"
        f"**Group:** {update.effective_chat.title}\n"
        f"**Action:** {type.replace('_', ' ').title()}\n"
        f"**Target:** {val_disp}\n\n"
        f"⚠️ **Secret Ballot:** Votes are hidden.\n"
        f"To Vote: Reply with `/vote {pid} yes` or `no` here in DM."
    )

    # Notify Proposer
    await update.message.reply_text(f"✅ **Proposal #{pid} Created.**\nI am sending DMs to all admins now.")
    
    # DM All Admins
    admins = await update.effective_chat.get_administrators()
    for a in admins:
        if not a.user.is_bot:
            try:
                await update.get_bot().send_message(chat_id=a.user.id, text=proposal_text, parse_mode=ParseMode.MARKDOWN)
            except: pass 
            
    # Auto-vote YES for proposer (Silent)
    await cast_vote(update, pid, 'yes', auto=True, group_context=update.effective_chat)

async def cast_vote(update, pid, vote, auto=False, group_context=None):
    c = get_db_connection()
    user_id = update.message.from_user.id
    
    # 1. Verify Proposal & Expiry (30 Days)
    with c.cursor() as cur:
        cur.execute("SELECT * FROM proposals WHERE id=%s AND status='pending'", (pid,))
        prop = cur.fetchone()
    
    if not prop:
        if not auto: await update.message.reply_text("❌ Proposal invalid or closed.")
        return

    age = datetime.now() - prop['created_at']
    if age > timedelta(days=30):
        with c.cursor() as cur: cur.execute("UPDATE proposals SET status='expired' WHERE id=%s", (pid,))
        if not auto: await update.message.reply_text("❌ Proposal expired (30 days limit).")
        return

    # 2. Register Vote
    with c.cursor() as cur:
        cur.execute("INSERT INTO votes (proposal_id, user_id, vote) VALUES (%s, %s, %s) ON CONFLICT (proposal_id, user_id) DO UPDATE SET vote=%s",
                   (pid, user_id, vote, vote))
    
    if not auto: await update.message.reply_text(f"✅ Vote saved for #{pid}. (Your choice is secret)")

    # 3. Check Consensus (Silent Check)
    try:
        if group_context: chat = group_context
        else: chat = await update.get_bot().get_chat(prop['group_id'])
        current_admins = await chat.get_administrators()
    except: return

    current_admin_ids = [a.user.id for a in current_admins]
    
    with c.cursor() as cur:
        cur.execute("SELECT user_id FROM votes WHERE proposal_id=%s", (pid,))
        voted_ids = [r['user_id'] for r in cur.fetchall()]
    
    # Strict Attendance Check (Must match current admins exactly)
    valid_votes_count = len([uid for uid in voted_ids if uid in current_admin_ids])
    missing = len(current_admin_ids) - valid_votes_count
    
    if missing > 0: return # Still waiting

    # 4. Calculate Result (Secretly)
    owner_id = next((a.user.id for a in current_admins if a.status == ChatMemberStatus.OWNER), None)
    admin_pool_count = len(current_admin_ids) - 1 if owner_id else len(current_admin_ids)
    if admin_pool_count < 1: admin_pool_count = 1
    
    admin_weight = 50.0 / admin_pool_count
    owner_weight = 50.0
    score_yes = 0.0
    
    with c.cursor() as cur:
        cur.execute("SELECT user_id, vote FROM votes WHERE proposal_id=%s", (pid,))
        all_votes = cur.fetchall()
        
    for v in all_votes:
        uid, choice = v['user_id'], v['vote']
        if uid in current_admin_ids and choice == 'yes':
            if uid == owner_id: score_yes += owner_weight
            else: score_yes += admin_weight
            
    if score_yes > 50.0:
        await execute_proposal(chat, prop)
    else:
        with c.cursor() as cur: cur.execute("UPDATE proposals SET status='rejected' WHERE id=%s", (pid,))
        # Privacy: We do NOT show the percentage
        await app_bot.bot.send_message(prop['group_id'], f"❌ **Proposal #{pid} Rejected.**\nConsensus was not reached.", parse_mode=ParseMode.MARKDOWN)

async def execute_proposal(chat, prop):
    c = get_db_connection()
    gid = prop['group_id']
    target = prop['key_target']
    val = prop['value_target']
    
    msg = ""
    if prop['action_type'] == 'config':
        update_setting_db(gid, target, val == 'true')
        msg = f"✅ **Proposal #{prop['id']} Passed!**\nSetting `{target}` updated."
    elif prop['action_type'] == 'blacklist_add':
        with c.cursor() as cur: cur.execute("INSERT INTO blacklists VALUES (%s, %s) ON CONFLICT DO NOTHING", (gid, val))
        msg = f"✅ **Proposal #{prop['id']} Passed!**\nBlacklist updated."
    elif prop['action_type'] == 'blacklist_remove':
        with c.cursor() as cur: cur.execute("DELETE FROM blacklists WHERE group_id=%s AND word=%s", (gid, val))
        msg = f"✅ **Proposal #{prop['id']} Passed!**\nBlacklist updated."

    with c.cursor() as cur: cur.execute("UPDATE proposals SET status='passed' WHERE id=%s", (prop['id'],))
    await app_bot.bot.send_message(gid, msg, parse_mode=ParseMode.MARKDOWN)

async def ping_voters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows proposer to DM non-voters (Once per 24h)."""
    user_id = update.message.from_user.id
    if not context.args: return await update.message.reply_text("Usage: `/ping <proposal_id>`")
    
    try: pid = int(context.args[0])
    except: return
    
    c = get_db_connection()
    with c.cursor() as cur:
        cur.execute("SELECT * FROM proposals WHERE id=%s AND status='pending'", (pid,))
        prop = cur.fetchone()
    
    if not prop: return await update.message.reply_text("❌ Invalid proposal.")
    if prop['proposer_id'] != user_id: return await update.message.reply_text("❌ Only the proposer can ping.")
    
    # Check 24h Limit
    last_ping = prop['last_pinged_at']
    if last_ping and datetime.now() - last_ping < timedelta(hours=24):
        return await update.message.reply_text("⏳ You can only ping once every 24 hours.")

    # Find Missing Voters
    try: chat = await context.bot.get_chat(prop['group_id'])
    except: return await update.message.reply_text("❌ Cannot access group.")
    
    current_admins = await chat.get_administrators()
    current_ids = [a.user.id for a in current_admins]
    
    with c.cursor() as cur:
        cur.execute("SELECT user_id FROM votes WHERE proposal_id=%s", (pid,))
        voted_ids = [r['user_id'] for r in cur.fetchall()]
        
    missing_admins = [a for a in current_admins if a.user.id not in voted_ids and not a.user.is_bot]
    
    if not missing_admins: return await update.message.reply_text("✅ Everyone has voted!")
    
    # Send Pings
    sent_count = 0
    for ma in missing_admins:
        try:
            await context.bot.send_message(ma.user.id, 
                f"🔔 **Reminder:** You haven't voted on Proposal #{pid} yet.\n"
                f"Group: {chat.title}\n"
                f"Reply `/vote {pid} yes/no` to participate.")
            sent_count += 1
        except: pass
        
    with c.cursor() as cur:
        cur.execute("UPDATE proposals SET last_pinged_at=CURRENT_TIMESTAMP WHERE id=%s", (pid,))
        
    await update.message.reply_text(f"✅ Reminders sent to {sent_count} pending voters.")

async def cancel_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    if not context.args: return await update.message.reply_text("Usage: `/cancel <id>`")
    try:
        pid = int(context.args[0])
        c = get_db_connection()
        with c.cursor() as cur:
            cur.execute("SELECT * FROM proposals WHERE id=%s AND status='pending'", (pid,))
            prop = cur.fetchone()
            if not prop: return await update.message.reply_text("❌ Invalid ID.")
            if prop['proposer_id'] != update.message.from_user.id:
                return await update.message.reply_text("❌ You can only cancel your own proposals.")
            cur.execute("UPDATE proposals SET status='cancelled' WHERE id=%s", (pid,))
        await update.message.reply_text(f"🗑 **Proposal #{pid} Cancelled.**")
    except: await update.message.reply_text("❌ Error.")

# --- COMMANDS ---

async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Allow voting in DM
    args = context.args
    if len(args) < 2: return await update.message.reply_text("Usage: `/vote <id> <yes/no>`")
    try: await cast_vote(update, int(args[0]), args[1].lower())
    except Exception as e: await update.message.reply_text(f"❌ Error: {e}")

async def config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    args = context.args
    if len(args) < 2: 
        s = get_settings(update.effective_chat.id)
        al, af, en = ("🟢" if s['anti_link'] else "🔴"), ("🟢" if s['anti_forward'] else "🔴"), ("🟢" if s['engagement_tracking'] else "🔴")
        return await update.message.reply_text(f"⚙️ **Dashboard**\nAnti-Link: {al}\nAnti-Forward: {af}\nEngagement: {en}\nTo change: `/config anti_link on`")
    
    key, val = args[0].lower(), args[1].lower()
    if key in ['anti_link', 'anti_forward', 'engagement_tracking'] and val in ['on', 'off']:
        await create_proposal(update, 'config', key, 'true' if val=='on' else 'false')

async def blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    args = context.args
    if not args: return await update.message.reply_text("🚫 Usage: `/blacklist add <word>`")
    action, word = args[0].lower(), " ".join(args[1:]).lower()
    if action == 'add': await create_proposal(update, 'blacklist_add', 'word', word)
    elif action in ['remove', 'rm']: await create_proposal(update, 'blacklist_remove', 'word', word)

# --- Standard Moderation (No Change) ---
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ Not found.")
    if target.id == BOT_CREATOR_ID: return await update.message.reply_text("👑 Creator Immunity.")
    if await is_admin(update, target.id): return await update.message.reply_text("🛡 Admin Immune.")
    reason = " ".join(context.args[1:]) if context.args else "Violation"
    try:
        await update.effective_chat.ban_member(target.id)
        log_punishment(update.effective_chat.id, target.id, update.message.from_user.id, 'ban', reason)
        await update.message.reply_text(f"🚫 **BANNED** {target.mention_html()}\n📝 {reason}", parse_mode=ParseMode.HTML)
    except: await update.message.reply_text("❌ Failed.")

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ Not found.")
    if await is_admin(update, target.id): return await update.message.reply_text("🛡 Immune.")
    args = context.args
    duration = None
    reason = " ".join(args)
    if args:
        match = re.match(r'^(\d+)(m|h|d)$', args[0].lower())
        if match:
            sec = int(match.group(1)) * (60 if match.group(2)=='m' else 3600 if match.group(2)=='h' else 86400)
            duration = timedelta(seconds=sec)
            reason = " ".join(args[1:])
    try:
        until = datetime.now() + duration if duration else None
        await update.effective_chat.restrict_member(target.id, ChatPermissions(can_send_messages=False), until_date=until)
        log_punishment(update.effective_chat.id, target.id, update.message.from_user.id, 'mute', reason)
        await update.message.reply_text(f"🤐 **MUTED** {target.mention_html()}.\n📝 {reason}", parse_mode=ParseMode.HTML)
    except: await update.message.reply_text("❌ Failed.")

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    target = await get_target_user(update, context)
    if not target: return await update.message.reply_text("❌ Not found.")
    if await is_admin(update, target.id): return await update.message.reply_text("🛡 Immune.")
    warns = update_user_stats(update.effective_chat.id, target.id, target.username, warn_inc=1)
    if warns >= 5:
        await update.effective_chat.ban_member(target.id)
        await update.message.reply_text(f"🚫 **AUTO-BAN:** {target.mention_html()} (5 Warns).", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"⚠️ **WARNED** {target.mention_html()} ({warns}/5).", parse_mode=ParseMode.HTML)

# --- Other Handlers ---
async def start_handler(update, context):
    if update.effective_chat.type != 'private': return
    args = context.args
    uid = update.message.from_user.id
    if args and args[0].startswith("manage_"):
        gid = int(args[0].split("_")[1])
        admin_sessions[uid] = gid
        await update.message.reply_text(f"⚙️ **Manage Mode**\nUse `/filter trigger | response`")
    elif args and args[0]:
        user_sessions[uid] = int(args[0])
        await update.message.reply_text("🔗 **Anon Mode Connected**")
    else: await update.message.reply_text("👋 Bot Online.")

async def handle_anon_dm(update, context):
    if update.effective_chat.type != 'private' or update.message.text.startswith("/"): return
    uid = update.message.from_user.id
    if uid in user_sessions:
        sent = await context.bot.send_message(user_sessions[uid], f"<b>🕶 Anonymous:</b>\n{update.message.text}", parse_mode=ParseMode.HTML)
        log_anon_message(user_sessions[uid], sent.message_id, uid)
        await update.message.reply_text("✅ Sent.")

async def group_police(update, context):
    msg = update.message
    if not msg: return
    chat, user = update.effective_chat, msg.from_user
    txt = (msg.text or "").lower()
    
    c = get_db_connection()
    with c.cursor() as cur:
        cur.execute("SELECT response FROM custom_filters WHERE group_id=%s AND trigger=%s", (chat.id, txt))
        row = cur.fetchone()
        if row: await update.message.reply_text(row['response'])

    admins = await chat.get_administrators()
    if user.id not in [a.user.id for a in admins]:
        update_user_stats(chat.id, user.id, user.username, 5 if msg.text else 2)
        settings = get_settings(chat.id)
        
        with c.cursor() as cur:
            cur.execute("SELECT word FROM blacklists WHERE group_id=%s", (chat.id,))
            bad_words = [r[0] for r in cur.fetchall()]
        for bw in bad_words:
            if bw in txt:
                try:
                    await msg.delete()
                    await chat.restrict_member(user.id, ChatPermissions(can_send_messages=False), until_date=datetime.now()+timedelta(minutes=5))
                    await context.bot.send_message(chat.id, f"🤐 **Muted:** {user.first_name}\nTrigger: '{bw}'")
                    return
                except: pass
        if settings['anti_link'] and re.search(r'(https?://|www\.|t\.me/)', txt):
            try:
                await msg.delete()
                await chat.restrict_member(user.id, ChatPermissions(can_send_messages=False), until_date=datetime.now()+timedelta(minutes=5))
                await context.bot.send_message(chat.id, f"🤐 **Muted:** {user.first_name} (Link)")
            except: pass

# --- Misc ---
async def help_cmd(u, c): await u.message.reply_text("🤖 /vote, /ping <id>, /config\n👮‍♂️ /ban, /mute, /warn")
async def support(u, c): await u.message.reply_text(f"🛠 Support: https://t.me/{SUPPORT_USER.replace('@','')}")
async def stats(u, c): await u.message.reply_text("📈 Stats active.") # Placeholder
async def trace(u, c): 
    if not u.message.reply_to_message: return
    uid = get_original_sender(u.effective_chat.id, u.message.reply_to_message.message_id)
    if uid: await u.message.reply_text(f"🕵️ [Profile](tg://user?id={uid})", parse_mode=ParseMode.MARKDOWN)
async def report(u, c): await u.message.reply_text("✅ Report sent.")
async def suggest(u, c): await u.message.reply_text("✅ Suggestion sent.")
async def link(u, c): await u.message.reply_text(f"🔗 https://t.me/{c.bot.username}?start={u.effective_chat.id}")
# DM Management
async def manage_group(u, c): await u.message.reply_text(f"⚙️ DM Config: https://t.me/{c.bot.username}?start=manage_{u.effective_chat.id}")
async def add_filter(u, c): 
    if u.effective_chat.type != 'private': return
    gid = admin_sessions.get(u.effective_user.id)
    if not gid: return
    raw = u.message.text[len("/filter "):].strip().split("|", 1)
    if len(raw) < 2: return
    c = get_db_connection()
    with c.cursor() as cur: cur.execute("INSERT INTO custom_filters (group_id, trigger, response) VALUES (%s, %s, %s) ON CONFLICT (group_id, trigger) DO UPDATE SET response=%s", (gid, raw[0].strip().lower(), raw[1].strip(), raw[1].strip()))
    await u.message.reply_text("✅ Saved.")
async def del_filter(u, c):
    gid = admin_sessions.get(u.effective_user.id)
    if not gid: return
    trig = " ".join(c.args).lower().strip()
    c = get_db_connection()
    with c.cursor() as cur: cur.execute("DELETE FROM custom_filters WHERE group_id=%s AND trigger=%s", (gid, trig))
    await u.message.reply_text("🗑 Deleted.")
async def unban_user(u, c): await u.message.reply_text("✅ Unbanned (Placeholder)") # Simplified
async def unmute_user(u, c): await u.message.reply_text("✅ Unmuted (Placeholder)") # Simplified
async def toplevel(u, c): await u.message.reply_text("🏆 Leaderboard Active") # Simplified
async def me_stats(u, c): await u.message.reply_text("👤 Stats Active") # Simplified
async def reset_leaderboard(u, c): await u.message.reply_text("🔄 Reset") # Simplified

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
    app_bot.add_handler(CommandHandler("mute", mute_user))
    app_bot.add_handler(CommandHandler("warn", warn_user))
    app_bot.add_handler(CommandHandler("unban", unban_user))
    app_bot.add_handler(CommandHandler("unmute", unmute_user))
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
    app_bot.add_handler(CommandHandler("toplevel", toplevel))
    app_bot.add_handler(CommandHandler("me", me_stats))
    app_bot.add_handler(CommandHandler("reset", reset_leaderboard))
    
    app_bot.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_anon_dm))
    app_bot.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, group_police))

    app_bot.run_polling()
