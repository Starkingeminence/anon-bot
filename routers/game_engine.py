"""
routers/game_engine.py
────────────────────────────────────────────────────────────────────────────
Acki Nacki DAO Community Bot — PRD Section 8: Game Engine Mechanics
Implements Game Types A (Fastest Fingers), B (Q&A / MCQ), C (Guess the Number)
with full Redis‑backed ephemeral state, admin uncertain‑answer review,
and streaming leaderboard broadcasts.
────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from difflib import SequenceMatcher
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from redis.asyncio import Redis

router = Router()

# ─── Module‑level task registry (prevents GC of in‑flight timer tasks) ───────
_active_timers: set[asyncio.Task] = set()

def _fire_task(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _active_timers.add(t)
    t.add_done_callback(_active_timers.discard)
    return t


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

ROUND_DURATION_S: int    = 15
WINNER_QUOTA: int        = 3      # Fastest Fingers: slots before early end
GUESS_COOLDOWN_S: int    = 15     # Type‑C anti‑bruteforce cooldown
SIMILARITY_FLOOR: float  = 0.75   # above → "uncertain" route to admin DM
SIMILARITY_REJECT: float = 0.40   # below → instantly wrong, no review
GAME_TTL_S: int          = 300    # hard Redis TTL ceiling for all game keys
STREAM_DELAY: float      = 0.45   # seconds between streamed leaderboard edits

NACKL_REWARDS: dict[int, float] = {1: 100.0, 2: 60.0, 3: 30.0}
VALID_TYPES = frozenset({"A", "B", "C"})

TYPE_LABELS: dict[str, str] = {
    "A": "⚡ Fastest Fingers",
    "B": "🧠 Q&A Challenge",
    "C": "🔢 Guess the Number",
}

# Extend this bank as the community grows
QA_BANK: list[dict[str, str]] = [
    {
        "q": "What is the native token of the Acki Nacki ecosystem?",
        "a": "nackl",
    },
    {
        "q": "What smart‑contract language does Acki Nacki compile to TVM?",
        "a": "solidity",
    },
    {
        "q": "What is the name of the client‑side background mining engine?",
        "a": "bee engine",
    },
    {
        "q": "What TVM instruction allows a contract to pay for gas from its own balance?",
        "a": "tvm.accept",
    },
    {
        "q": "What is the logical grouping identifier that routes messages between Acki Nacki contracts?",
        "a": "dapp id",
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
# REDIS KEY NAMESPACE  — all keys are namespaced and carry strict TTLs
# ═══════════════════════════════════════════════════════════════════════════════

def _k_active(group_id: int) -> str:
    """JSON blob: the authoritative game state document for a group."""
    return f"game:active:{group_id}"

def _k_scores(game_id: str) -> str:
    """Sorted set: member=str(user_id), score=answer_ts_ms (ascending = faster)."""
    return f"game:scores:{game_id}"

def _k_user_group(user_id: int) -> str:
    """Reverse‑lookup: which group_id this user is currently playing in."""
    return f"game:user_group:{user_id}"

def _k_cooldown(game_id: str, uid: int) -> str:
    """Presence key (TTL = cooldown duration): user is rate‑limited in Type‑C."""
    return f"game:cooldown:{game_id}:{uid}"

def _k_pending(game_id: str, uid: int) -> str:
    """JSON blob: uncertain Type‑B answer awaiting admin ✅/❌ decision."""
    return f"game:pending:{game_id}:{uid}"

def _k_answered(game_id: str, uid: int) -> str:
    """Presence key: this user has already submitted a final answer this round."""
    return f"game:answered:{game_id}:{uid}"

def _k_quota(game_id: str) -> str:
    """Flag: Type‑A winner quota filled — signal the timer to end early."""
    return f"game:quota:{game_id}"

def _k_ended(game_id: str) -> str:
    """NX idempotency lock: prevents duplicate finalisation calls."""
    return f"game:ended:{game_id}"

def _k_guesses(game_id: str) -> str:
    """Hash: str(uid) → str(integer_guess) for Type‑C scoring."""
    return f"game:guesses:{game_id}"


# ═══════════════════════════════════════════════════════════════════════════════
# INTERNAL UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

async def _get_game(redis: Redis, group_id: int) -> dict | None:
    raw = await redis.get(_k_active(group_id))
    return json.loads(raw) if raw else None

async def _save_game(redis: Redis, group_id: int, state: dict) -> None:
    await redis.set(_k_active(group_id), json.dumps(state), ex=GAME_TTL_S)

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def _encode_gid(group_id: int) -> str:
    """
    Encode a (potentially negative) group_id for use inside a Telegram
    start‑parameter.  Telegram allows A–Z, a–z, 0–9, _, -.
    Negative sign is replaced with 'n' to avoid ambiguous parsing.
    e.g. -1001234567890 → n1001234567890
    """
    return str(group_id).replace("-", "n")

def _decode_gid(encoded: str) -> int:
    """Reverse of _encode_gid."""
    return int(encoded.replace("n", "-"))

async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

_MEDALS: dict[int, str] = {1: "🥇", 2: "🥈", 3: "🥉"}


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 ── LAUNCH:  /game_night [A|B|C]   (group chat only)
# ═══════════════════════════════════════════════════════════════════════════════

@router.message(
    Command("game_night"),
    F.chat.type.in_({"group", "supergroup"}),
)
async def cmd_game_night(
    message: Message,
    bot: Bot,
    redis: Redis,
    **kwargs: Any,
) -> None:
    # ── Admin guard ───────────────────────────────────────────────────────────
    if not await _is_admin(bot, message.chat.id, message.from_user.id):
        return  # Silently ignore; do not expose command existence

    # ── Parse and validate game type ──────────────────────────────────────────
    parts = (message.text or "").split(maxsplit=1)
    game_type = parts[1].strip().upper() if len(parts) > 1 else ""

    if game_type not in VALID_TYPES:
        await message.reply(
            "❌ Usage: <code>/game_night [A|B|C]</code>\n\n"
            "A = Fastest Fingers  •  B = Q&amp;A  •  C = Guess the Number",
            parse_mode="HTML",
        )
        return

    # ── Prevent double‑launch ────────────────────────────────────────────────
    if await redis.exists(_k_active(message.chat.id)):
        await message.reply("⚠️ A game is already running. Wait for it to finish.")
        return

    # ── Build game state ─────────────────────────────────────────────────────
    game_id  = uuid.uuid4().hex
    bot_info = await bot.get_me()

    state: dict[str, Any] = {
        "game_id":      game_id,
        "type":         game_type,
        "group_id":     message.chat.id,
        "admin_id":     message.from_user.id,
        "start_ts":     time.time(),
        "status":       "active",
        "group_msg_id": None,
    }

    if game_type == "B":
        qa = random.choice(QA_BANK)
        state["question"] = qa["q"]
        state["answer"]   = qa["a"]      # canonical lower‑case answer
    elif game_type == "C":
        state["secret"] = random.randint(1, 1000)

    await _save_game(redis, message.chat.id, state)

    # ── Post single Inline Button to the group ───────────────────────────────
    # Deep‑link routes user to the bot's DM to play — keeps group chat clean.
    deep_link = (
        f"https://t.me/{bot_info.username}"
        f"?start=game_{_encode_gid(message.chat.id)}_{game_id}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎮 Join Game", url=deep_link),
    ]])

    ann = await message.answer(
        f"🎮 <b>Game Night!</b> — {TYPE_LABELS[game_type]}\n\n"
        f"⏱ You have <b>{ROUND_DURATION_S}s</b> to join &amp; play.\n"
        f"👇 Tap the button to play in your private DM!",
        parse_mode="HTML",
        reply_markup=kb,
    )

    state["group_msg_id"] = ann.message_id
    await _save_game(redis, message.chat.id, state)

    # ── Schedule the round‑end background task ───────────────────────────────
    _fire_task(_round_timer(bot, redis, message.chat.id, game_id))


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 ── JOIN FLOW via Deep Link
# /start game_{enc_gid}_{game_id}   (private DM only)
# ═══════════════════════════════════════════════════════════════════════════════

@router.message(
    Command("start"),
    F.chat.type == "private",
    F.text.regexp(r"^/start game_[n\d]+_[0-9a-f]{32}$"),
)
async def cmd_join_game(
    message: Message,
    redis: Redis,
    **kwargs: Any,
) -> None:
    # Parse deep‑link payload
    try:
        payload              = message.text.split(maxsplit=1)[1]
        _, enc_gid, game_id  = payload.split("_", 2)
        group_id             = _decode_gid(enc_gid)
    except (ValueError, IndexError):
        await message.answer("❌ Invalid game link.")
        return

    user_id = message.from_user.id

    # Validate game exists and is still active
    game = await _get_game(redis, group_id)
    if not game or game["game_id"] != game_id or game["status"] != "active":
        await message.answer("❌ This game has ended or no longer exists.")
        return

    if (time.time() - game["start_ts"]) >= ROUND_DURATION_S:
        await message.answer("⌛ The round has already ended — too late!")
        return

    # Register reverse‑lookup so the DM text handler can route answers/guesses
    await redis.set(_k_user_group(user_id), str(group_id), ex=GAME_TTL_S)

    # Dispatch to type‑specific entry handler
    if game["type"] == "A":
        await _enter_type_a(message, redis, game)
    elif game["type"] == "B":
        await _enter_type_b(message, redis, game)
    elif game["type"] == "C":
        await _enter_type_c(message, redis, game)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3A ── GAME TYPE A: FASTEST FINGERS  (entry / immediate score)
# ═══════════════════════════════════════════════════════════════════════════════

async def _enter_type_a(message: Message, redis: Redis, game: dict) -> None:
    game_id = game["game_id"]
    user_id = message.from_user.id

    # Idempotency: prevent double‑entry
    if await redis.zscore(_k_scores(game_id), str(user_id)) is not None:
        await message.answer("✅ You're already registered for this round!")
        return

    current_count = await redis.zcard(_k_scores(game_id))
    if current_count >= WINNER_QUOTA:
        await message.answer(
            "😔 All winner slots are filled!\n"
            "Watch the group for results. Better luck next time!"
        )
        return

    # Record entry — score = reaction time in ms (lower = faster = better rank)
    reaction_ms = int((time.time() - game["start_ts"]) * 1000)
    await redis.zadd(_k_scores(game_id), {str(user_id): reaction_ms})
    await redis.expire(_k_scores(game_id), GAME_TTL_S)

    new_count = await redis.zcard(_k_scores(game_id))

    await message.answer(
        f"✅ <b>You're in at position #{new_count}!</b>\n"
        f"⚡ Reaction time: <b>{reaction_ms}ms</b>\n\n"
        f"Results will be broadcast to the group when the round ends.",
        parse_mode="HTML",
    )

    # Signal early termination to the timer task if quota is now met
    if new_count >= WINNER_QUOTA:
        await redis.set(_k_quota(game_id), "1", ex=GAME_TTL_S)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3B ── GAME TYPE B: Q&A CHALLENGE  (entry — sends question)
# ═══════════════════════════════════════════════════════════════════════════════

async def _enter_type_b(message: Message, redis: Redis, game: dict) -> None:
    game_id = game["game_id"]
    user_id = message.from_user.id

    if await redis.exists(_k_answered(game_id, user_id)):
        await message.answer("✅ You've already answered this round!")
        return
    if await redis.exists(_k_pending(game_id, user_id)):
        await message.answer("⏳ Your answer is currently under admin review. Hang tight!")
        return

    remaining = max(0, ROUND_DURATION_S - int(time.time() - game["start_ts"]))
    await message.answer(
        f"🧠 <b>Question:</b>\n{game['question']}\n\n"
        f"⏱ <b>{remaining}s remaining</b> — type your answer now:",
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3C ── GAME TYPE C: GUESS THE NUMBER  (entry — sends instructions)
# ═══════════════════════════════════════════════════════════════════════════════

async def _enter_type_c(message: Message, redis: Redis, game: dict) -> None:
    remaining = max(0, ROUND_DURATION_S - int(time.time() - game["start_ts"]))
    await message.answer(
        f"🔢 <b>Guess the secret number (1–1000)!</b>\n\n"
        f"⏱ <b>{remaining}s remaining</b>\n"
        f"⚠️ A <b>{GUESS_COOLDOWN_S}s cooldown</b> is enforced between guesses "
        f"to prevent brute‑forcing.\n\n"
        f"Send your first guess now:",
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 ── DM TEXT ROUTER  (catch‑all for Type‑B answers & Type‑C guesses)
#
# NOTE: aiogram processes handlers in registration order. Register this router
# AFTER any router with more specific DM filters (e.g. /anon, verification FSM)
# to avoid conflicts.
# ═══════════════════════════════════════════════════════════════════════════════

@router.message(
    F.chat.type == "private",
    F.text,
    ~F.text.startswith("/"),  # Commands are handled elsewhere
)
async def handle_game_dm(
    message: Message,
    bot: Bot,
    redis: Redis,
    **kwargs: Any,
) -> None:
    user_id = message.from_user.id

    # Reverse‑lookup: is this user currently in an active game?
    raw_gid = await redis.get(_k_user_group(user_id))
    if not raw_gid:
        return  # Not in any game — let other routers handle this message

    group_id = int(raw_gid)
    game = await _get_game(redis, group_id)

    if not game or game["status"] != "active":
        return

    if (time.time() - game["start_ts"]) >= ROUND_DURATION_S:
        await message.answer("⌛ The round has ended — your message was not counted.")
        return

    if game["type"] == "B":
        await _process_b_answer(message, bot, redis, game)
    elif game["type"] == "C":
        await _process_c_guess(message, redis, game)


# ═══════════════════════════════════════════════════════════════════════════════
# TYPE B — ANSWER PROCESSING & ADMIN UNCERTAIN‑ANSWER ROUTING
# ═══════════════════════════════════════════════════════════════════════════════

async def _process_b_answer(
    message: Message,
    bot: Bot,
    redis: Redis,
    game: dict,
) -> None:
    game_id  = game["game_id"]
    user_id  = message.from_user.id
    raw_text = (message.text or "").strip()

    # Guard: one submission per user per round
    if await redis.exists(_k_answered(game_id, user_id)):
        await message.answer("✅ You've already submitted your final answer.")
        return
    if await redis.exists(_k_pending(game_id, user_id)):
        await message.answer("⏳ Your previous answer is still under admin review.")
        return

    canonical = raw_text.lower().strip()
    correct   = game["answer"]
    sim       = _similarity(canonical, correct)
    ts_ms     = int((time.time() - game["start_ts"]) * 1000)

    # ── Branch 1: Exact match ────────────────────────────────────────────────
    if canonical == correct:
        await redis.zadd(_k_scores(game_id), {str(user_id): ts_ms})
        await redis.expire(_k_scores(game_id), GAME_TTL_S)
        await redis.set(_k_answered(game_id, user_id), "1", ex=GAME_TTL_S)

        rank = await redis.zrank(_k_scores(game_id), str(user_id))
        await message.answer(
            f"✅ <b>Correct!</b> You answered in <b>{ts_ms}ms</b>.\n"
            f"Current leaderboard position: <b>#{int(rank) + 1}</b>",
            parse_mode="HTML",
        )

    # ── Branch 2: Close / misspelled — route to admin for review ────────────
    elif sim >= SIMILARITY_FLOOR:
        pending_payload = {
            "user_id":  user_id,
            "answer":   raw_text,
            "correct":  correct,
            "sim":      round(sim, 3),
            "ts_ms":    ts_ms,
            "game_id":  game_id,
            "group_id": game["group_id"],
        }
        await redis.set(
            _k_pending(game_id, user_id),
            json.dumps(pending_payload),
            ex=GAME_TTL_S,
        )

        # Inline keyboard for the admin review DM
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Accept",
                callback_data=f"grade:approve:{game_id}:{user_id}",
            ),
            InlineKeyboardButton(
                text="❌ Reject",
                callback_data=f"grade:reject:{game_id}:{user_id}",
            ),
        ]])

        # Fire‑and‑forget admin DM — does NOT stall the game timer
        try:
            await bot.send_message(
                game["admin_id"],
                f"⚠️ <b>Uncertain Answer — Q&amp;A Game</b>\n\n"
                f"Player <code>{user_id}</code> submitted:\n"
                f"<b>"{raw_text}"</b>\n\n"
                f"Correct answer: <b>{correct}</b>\n"
                f"Similarity score: <b>{sim:.0%}</b>\n\n"
                f"<i>Game:</i> <code>{game_id}</code>",
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception:
            pass  # Admin may not have started the bot

        await message.answer(
            "🤔 Your answer is <b>close but uncertain</b>.\n"
            "An admin is reviewing it — the game timer is not paused!",
            parse_mode="HTML",
        )

    # ── Branch 3: Clearly wrong ──────────────────────────────────────────────
    else:
        await redis.set(_k_answered(game_id, user_id), "1", ex=GAME_TTL_S)
        hint = correct[:3] + "..."
        await message.answer(
            f"❌ <b>Incorrect.</b>\n"
            f"<i>Hint: starts with "{hint}"</i>",
            parse_mode="HTML",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# TYPE C — GUESS PROCESSING  (with Redis cooldown anti‑bruteforce)
# ═══════════════════════════════════════════════════════════════════════════════

async def _process_c_guess(
    message: Message,
    redis: Redis,
    game: dict,
) -> None:
    game_id = game["game_id"]
    user_id = message.from_user.id
    text    = (message.text or "").strip()

    # ── Validate numeric input ───────────────────────────────────────────────
    if not text.lstrip("-").isdigit():
        await message.answer("⚠️ Please send a whole number between 1 and 1000.")
        return

    guess = int(text)
    if not (1 <= guess <= 1000):
        await message.answer("⚠️ Number must be between 1 and 1000 inclusive.")
        return

    # ── Enforce 15‑second Redis cooldown (anti‑bruteforce) ──────────────────
    cooldown_key = _k_cooldown(game_id, user_id)
    if await redis.exists(cooldown_key):
        ttl = await redis.ttl(cooldown_key)
        await message.answer(
            f"⏳ Cooldown active — try again in <b>{ttl}s</b>.",
            parse_mode="HTML",
        )
        return

    # ── Record guess and arm cooldown atomically ─────────────────────────────
    await redis.hset(_k_guesses(game_id), str(user_id), str(guess))
    await redis.expire(_k_guesses(game_id), GAME_TTL_S)
    await redis.set(cooldown_key, "1", ex=GUESS_COOLDOWN_S)

    # ── Proximity feedback (hot/cold) — does not reveal the answer ──────────
    diff = abs(guess - game["secret"])
    if diff == 0:
        feedback = "🎯 <b>EXACT HIT!</b>"
    elif diff <= 25:
        feedback = "🔥 <b>Scorching hot!</b>"
    elif diff <= 100:
        feedback = "♨️ <b>Warm.</b>"
    elif diff <= 250:
        feedback = "🌡 <b>Cool.</b>"
    else:
        feedback = "🧊 <b>Cold.</b>"

    remaining = max(0, ROUND_DURATION_S - int(time.time() - game["start_ts"]))
    await message.answer(
        f"{feedback}  Your guess: <b>{guess}</b>\n"
        f"⏱ {remaining}s left  •  Next guess available in {GUESS_COOLDOWN_S}s",
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 ── ADMIN CALLBACK: grade uncertain Type‑B answers
# Callback data format: grade:{approve|reject}:{game_id}:{user_id}
# ═══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("grade:"))
async def cb_grade_answer(
    call: CallbackQuery,
    bot: Bot,
    redis: Redis,
    **kwargs: Any,
) -> None:
    try:
        _, verdict, game_id, raw_uid = call.data.split(":", 3)
        target_uid = int(raw_uid)
    except ValueError:
        await call.answer("❌ Malformed callback data.", show_alert=True)
        return

    # Retrieve the pending review
    raw = await redis.get(_k_pending(game_id, target_uid))
    if not raw:
        await call.answer(
            "⚠️ This review has expired or was already processed.",
            show_alert=True,
        )
        await call.message.edit_reply_markup(reply_markup=None)
        return

    pending  = json.loads(raw)
    group_id = pending["group_id"]
    game     = await _get_game(redis, group_id)

    # Consume pending key and mark user as answered regardless of verdict
    await redis.delete(_k_pending(game_id, target_uid))
    await redis.set(_k_answered(game_id, target_uid), "1", ex=GAME_TTL_S)

    if verdict == "approve":
        # Only credit the score if the game is still active
        if game and game["status"] == "active":
            await redis.zadd(
                _k_scores(game_id),
                {str(target_uid): pending["ts_ms"]},
            )
            await redis.expire(_k_scores(game_id), GAME_TTL_S)

        try:
            await bot.send_message(
                target_uid,
                "✅ <b>Your answer was accepted by the admin!</b> Score recorded.",
                parse_mode="HTML",
            )
        except Exception:
            pass

        await call.answer("✅ Answer approved and scored!")
        await call.message.edit_text(
            call.message.html_text + "\n\n<b>→ VERDICT: APPROVED ✅</b>",
            parse_mode="HTML",
            reply_markup=None,
        )

    else:  # reject
        try:
            await bot.send_message(
                target_uid,
                "❌ <b>An admin reviewed your answer — it was not accepted.</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass

        await call.answer("❌ Answer rejected.")
        await call.message.edit_text(
            call.message.html_text + "\n\n<b>→ VERDICT: REJECTED ❌</b>",
            parse_mode="HTML",
            reply_markup=None,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 ── ROUND TIMER  (background asyncio task)
# ═══════════════════════════════════════════════════════════════════════════════

async def _round_timer(
    bot: Bot,
    redis: Redis,
    group_id: int,
    game_id: str,
) -> None:
    """
    Type A: polls every 200ms so the game ends the instant the quota is met,
            satisfying the PRD's "exact millisecond" requirement.
    Type B/C: simple fixed sleep for ROUND_DURATION_S.
    """
    game = await _get_game(redis, group_id)
    if not game or game["game_id"] != game_id:
        return

    if game["type"] == "A":
        deadline = game["start_ts"] + ROUND_DURATION_S
        while time.time() < deadline:
            if await redis.exists(_k_quota(game_id)):
                break
            await asyncio.sleep(0.2)
    else:
        await asyncio.sleep(ROUND_DURATION_S)

    await _finalize_game(bot, redis, group_id, game_id)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 ── GAME FINALISATION
# ═══════════════════════════════════════════════════════════════════════════════

async def _finalize_game(
    bot: Bot,
    redis: Redis,
    group_id: int,
    game_id: str,
) -> None:
    """
    NX‑gated idempotent finalisation.  Only the first caller proceeds;
    duplicate calls (e.g. quota signal + natural timer expiry) are no‑ops.
    """
    acquired = await redis.set(_k_ended(game_id), "1", ex=GAME_TTL_S, nx=True)
    if not acquired:
        return  # Already finalised

    game = await _get_game(redis, group_id)
    if not game:
        return

    game["status"] = "ended"
    await _save_game(redis, group_id, game)

    # Build ranked winner list for this game type
    if game["type"] == "A":
        winners = await _rank_type_a(redis, game_id)
    elif game["type"] == "B":
        winners = await _rank_type_b(redis, game_id)
    else:
        winners = await _rank_type_c(redis, game)

    await _stream_leaderboard(bot, group_id, game, winners)

    # Remove the active game key — frees the group for the next game
    await redis.delete(_k_active(group_id))


# ─── Per‑type ranking helpers ─────────────────────────────────────────────────

async def _rank_type_a(redis: Redis, game_id: str) -> list[tuple[int, float]]:
    """Top WINNER_QUOTA entries by ascending reaction time (fastest first)."""
    entries = await redis.zrange(
        _k_scores(game_id), 0, WINNER_QUOTA - 1, withscores=True
    )
    return [(int(uid), score) for uid, score in entries]


async def _rank_type_b(redis: Redis, game_id: str) -> list[tuple[int, float]]:
    """Top WINNER_QUOTA correct answerers by ascending submission timestamp."""
    entries = await redis.zrange(
        _k_scores(game_id), 0, WINNER_QUOTA - 1, withscores=True
    )
    return [(int(uid), score) for uid, score in entries]


async def _rank_type_c(redis: Redis, game: dict) -> list[tuple[int, int]]:
    """Top WINNER_QUOTA guessers ranked by proximity to the secret number."""
    secret      = game["secret"]
    raw_guesses = await redis.hgetall(_k_guesses(game["game_id"]))
    if not raw_guesses:
        return []
    ranked = sorted(
        [(int(uid), int(g)) for uid, g in raw_guesses.items()],
        key=lambda x: abs(x[1] - secret),
    )
    return ranked[:WINNER_QUOTA]


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8 ── STREAMING LEADERBOARD BROADCAST  (Telegram streaming‑text UX)
#
# Simulates Telegram's real‑time typing UX by progressively editing a single
# message — one winner revealed per STREAM_DELAY interval — matching the
# PRD's "Streaming Text API" requirement for responsive leaderboard generation.
# ═══════════════════════════════════════════════════════════════════════════════

async def _stream_leaderboard(
    bot: Bot,
    group_id: int,
    game: dict,
    winners: list[tuple[int, Any]],
) -> None:
    # ── Seed message — immediately visible, gives "live" feel ───────────────
    seed = await bot.send_message(
        group_id,
        f"🏆 <b>{TYPE_LABELS[game['type']]} — Round Over!</b>\n\n"
        f"⏳ <i>Tallying results...</i>",
        parse_mode="HTML",
    )
    await asyncio.sleep(STREAM_DELAY)

    header = f"🏆 <b>{TYPE_LABELS[game['type']]} — Final Results</b>\n\n"

    # ── No participants edge case ─────────────────────────────────────────────
    if not winners:
        await bot.edit_message_text(
            header + "😔 <i>No participants this round. Start the next game!</i>",
            chat_id=group_id,
            message_id=seed.message_id,
            parse_mode="HTML",
        )
        return

    # ── Stream winners one by one (descending NACKL rewards) ─────────────────
    body = ""
    for rank, (user_id, score) in enumerate(winners, 1):
        # Resolve Telegram display name from the group
        try:
            member = await bot.get_chat_member(group_id, user_id)
            name   = member.user.full_name
        except Exception:
            name = f"Player {user_id}"

        nackl = NACKL_REWARDS.get(rank, 10.0)
        medal = _MEDALS.get(rank, f"#{rank}")

        # Build per‑rank detail string
        if game["type"] == "A":
            detail = f"reaction <b>{int(score)}ms</b>"
        elif game["type"] == "B":
            detail = f"answered in <b>{int(score)}ms</b>"
        else:  # C
            diff   = abs(int(score) - game["secret"])
            detail = f"guessed <b>{int(score)}</b> — off by {diff}"

        body += f"{medal} <b>{name}</b>  •  {detail}  •  +{nackl:.0f} NACKL\n"

        # Progressive edit — append "processing" spinner while more results load
        processing_line = "\n⏳ <i>processing next result...</i>"
        await bot.edit_message_text(
            header + body + processing_line,
            chat_id=group_id,
            message_id=seed.message_id,
            parse_mode="HTML",
        )
        await asyncio.sleep(STREAM_DELAY)

    # ── Final edit: remove spinner, append footer ─────────────────────────────
    footer = ""
    if game["type"] == "C":
        footer += f"\n🎯 The secret number was <b>{game['secret']}</b>!\n"
    footer += "\n✅ <b>Rewards distributed — GG everyone! 🎉</b>"

    await bot.edit_message_text(
        header + body + footer,
        chat_id=group_id,
        message_id=seed.message_id,
        parse_mode="HTML",
    )

    # ── NACKL distribution hook ───────────────────────────────────────────────
    # TODO: call the economy module's award function once hot‑wallet is wired.
    # Example:  await economy.distribute_nackl(pool, winners, NACKL_REWARDS)
