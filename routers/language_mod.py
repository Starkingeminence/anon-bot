"""
routers/language_mod.py
=======================
Phase 5 — Section 7: Automated Language Enforcement

Pipeline (executes BEFORE the economy router):
  1. Pre-filter: skip short text, URLs, wallet addresses, bot commands.
  2. Detect language via langdetect (local, zero network I/O).
  3. Act only when confidence strictly > 0.70 AND detected lang != 'en'.
  4. Delete message → DM user in their native language (ISO dict fallback).
  5. Write 'warn' row to `penalties` table (admin_id = 0 = system).
  6. Increment `warning_count` in `users` table.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import ~Command
from aiogram.types import Message
from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

# ── Determinism: langdetect is non-deterministic by default ──────────────────
DetectorFactory.seed = 0

logger = logging.getLogger(__name__)

router = Router(name="language_mod")

# ── Constants ─────────────────────────────────────────────────────────────────
CONFIDENCE_THRESHOLD = 0.70
MIN_TEXT_LENGTH = 12   # messages shorter than this are never evaluated

# Regex patterns for false-positive suppression
_RE_URL = re.compile(
    r"https?://\S+|www\.\S+",
    re.IGNORECASE,
)
_RE_EVM_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
# Acki Nacki / TON-style addresses: <workchain>:<64 hex chars>
_RE_ACKI_ADDRESS = re.compile(r"\b-?\d+:[0-9a-fA-F]{64}\b")

# ── Pre-translated warning dictionary (ISO 639-1 → native-language string) ───
# Format: "Your message was removed because this group is English-only."
# Extend this dict with additional languages as needed.
LANG_WARNINGS: dict[str, str] = {
    "en": (
        "⚠️ Your message was removed from the group because it was not in "
        "<b>English</b>.\n\nPlease use English in the main chat. Thank you!"
    ),
    "es": (
        "⚠️ Tu mensaje fue eliminado del grupo porque no estaba en "
        "<b>inglés</b>.\n\nPor favor, usa el inglés en el chat principal. ¡Gracias!"
    ),
    "fr": (
        "⚠️ Votre message a été supprimé du groupe car il n'était pas en "
        "<b>anglais</b>.\n\nVeuillez utiliser l'anglais dans le chat principal. Merci !"
    ),
    "de": (
        "⚠️ Deine Nachricht wurde aus der Gruppe entfernt, da sie nicht auf "
        "<b>Englisch</b> war.\n\nBitte verwende im Hauptchat Englisch. Danke!"
    ),
    "pt": (
        "⚠️ Sua mensagem foi removida do grupo porque não estava em "
        "<b>inglês</b>.\n\nPor favor, use o inglês no chat principal. Obrigado!"
    ),
    "ru": (
        "⚠️ Ваше сообщение было удалено из группы, так как оно было не на "
        "<b>английском</b> языке.\n\nПожалуйста, общайтесь в основном чате на английском. Спасибо!"
    ),
    "ar": (
        "⚠️ تمت إزالة رسالتك من المجموعة لأنها لم تكن باللغة "
        "<b>الإنجليزية</b>.\n\nيُرجى استخدام اللغة الإنجليزية في المحادثة الرئيسية. شكرًا!"
    ),
    "zh-cn": (
        "⚠️ 您的消息已从群组中删除，因为它不是用<b>英语</b>写的。\n\n"
        "请在主群中使用英语。谢谢！"
    ),
    "zh-tw": (
        "⚠️ 您的訊息已從群組中刪除，因為它不是以<b>英語</b>撰寫的。\n\n"
        "請在主群組中使用英語。謝謝！"
    ),
    "ja": (
        "⚠️ メッセージがグループから削除されました。<b>英語</b>以外の言語が使用されていたためです。\n\n"
        "メインチャットでは英語をご使用ください。ありがとうございます！"
    ),
    "ko": (
        "⚠️ 메시지가 <b>영어</b>로 작성되지 않아 그룹에서 삭제되었습니다.\n\n"
        "메인 채팅에서는 영어를 사용해 주세요. 감사합니다!"
    ),
    "tr": (
        "⚠️ Mesajınız <b>İngilizce</b> olmadığı için gruptan kaldırıldı.\n\n"
        "Lütfen ana sohbette İngilizce kullanın. Teşekkürler!"
    ),
    "id": (
        "⚠️ Pesan Anda dihapus dari grup karena tidak menggunakan "
        "<b>bahasa Inggris</b>.\n\nHarap gunakan bahasa Inggris di obrolan utama. Terima kasih!"
    ),
    "hi": (
        "⚠️ आपका संदेश समूह से हटा दिया गया क्योंकि यह <b>अंग्रेज़ी</b> में नहीं था।\n\n"
        "कृपया मुख्य चैट में अंग्रेज़ी का उपयोग करें। धन्यवाद!"
    ),
    "bn": (
        "⚠️ আপনার বার্তাটি গ্রুপ থেকে সরানো হয়েছে কারণ এটি <b>ইংরেজি</b>তে ছিল না।\n\n"
        "অনুগ্রহ করে মূল চ্যাটে ইংরেজি ব্যবহার করুন। ধন্যবাদ!"
    ),
    "ur": (
        "⚠️ آپ کا پیغام گروپ سے ہٹا دیا گیا کیونکہ یہ <b>انگریزی</b> میں نہیں تھا۔\n\n"
        "براہ کرم مرکزی چیٹ میں انگریزی استعمال کریں۔ شکریہ!"
    ),
    "vi": (
        "⚠️ Tin nhắn của bạn đã bị xóa khỏi nhóm vì không được viết bằng "
        "<b>tiếng Anh</b>.\n\nVui lòng sử dụng tiếng Anh trong cuộc trò chuyện chính. Cảm ơn!"
    ),
    "th": (
        "⚠️ ข้อความของคุณถูกลบออกจากกลุ่มเนื่องจากไม่ได้เขียนเป็น<b>ภาษาอังกฤษ</b>\n\n"
        "กรุณาใช้ภาษาอังกฤษในห้องแชทหลัก ขอบคุณ!"
    ),
    "fa": (
        "⚠️ پیام شما از گروه حذف شد زیرا به <b>زبان انگلیسی</b> نبود.\n\n"
        "لطفاً در چت اصلی از زبان انگلیسی استفاده کنید. متشکریم!"
    ),
    "uk": (
        "⚠️ Ваше повідомлення було видалено з групи, оскільки воно не було "
        "<b>англійською</b> мовою.\n\nБудь ласка, спілкуйтеся в основному чаті англійською. Дякуємо!"
    ),
    "pl": (
        "⚠️ Twoja wiadomość została usunięta z grupy, ponieważ nie była napisana "
        "po <b>angielsku</b>.\n\nProszę używać języka angielskiego na głównym czacie. Dziękujemy!"
    ),
    "nl": (
        "⚠️ Je bericht is verwijderd uit de groep omdat het niet in het "
        "<b>Engels</b> was.\n\nGebruik alsjeblieft Engels in de hoofdchat. Bedankt!"
    ),
    "it": (
        "⚠️ Il tuo messaggio è stato rimosso dal gruppo perché non era in "
        "<b>inglese</b>.\n\nUsa l'inglese nella chat principale. Grazie!"
    ),
    "ro": (
        "⚠️ Mesajul tău a fost eliminat din grup deoarece nu era în "
        "<b>engleză</b>.\n\nTe rugăm să folosești engleza în chat-ul principal. Mulțumim!"
    ),
    "hu": (
        "⚠️ Az üzeneted el lett távolítva a csoportból, mert nem <b>angolul</b> "
        "volt írva.\n\nKérjük, használj angolt a fő chatben. Köszönjük!"
    ),
    "cs": (
        "⚠️ Vaše zpráva byla odstraněna ze skupiny, protože nebyla napsána "
        "<b>anglicky</b>.\n\nProsím používejte angličtinu v hlavním chatu. Děkujeme!"
    ),
    "sv": (
        "⚠️ Ditt meddelande togs bort från gruppen eftersom det inte var på "
        "<b>engelska</b>.\n\nAnvänd engelska i huvudchatten. Tack!"
    ),
    "fi": (
        "⚠️ Viestisi poistettiin ryhmästä, koska se ei ollut <b>englanniksi</b>.\n\n"
        "Käytä englantia pääkeskustelussa. Kiitos!"
    ),
    "no": (
        "⚠️ Meldingen din ble fjernet fra gruppen fordi den ikke var på "
        "<b>engelsk</b>.\n\nVennligst bruk engelsk i hovedchatten. Takk!"
    ),
    "da": (
        "⚠️ Din besked blev fjernet fra gruppen, fordi den ikke var på "
        "<b>engelsk</b>.\n\nBrug venligst engelsk i hovedchatten. Tak!"
    ),
    "el": (
        "⚠️ Το μήνυμά σου αφαιρέθηκε από την ομάδα επειδή δεν ήταν στα "
        "<b>αγγλικά</b>.\n\nΠαρακαλώ χρησιμοποίησε αγγλικά στην κύρια συνομιλία. Ευχαριστώ!"
    ),
}


# ── Utility helpers ───────────────────────────────────────────────────────────

def _strip_noise(text: str) -> str:
    """
    Remove URLs and wallet addresses before language detection so they do not
    skew the detector toward a false positive.
    """
    text = _RE_URL.sub("", text)
    text = _RE_EVM_ADDRESS.sub("", text)
    text = _RE_ACKI_ADDRESS.sub("", text)
    return text.strip()


def _get_warning_text(language_code: str | None) -> str:
    """
    Return the pre-translated warning string for the given ISO 639-1 code.
    Falls back to English if the code is absent or not in the dictionary.
    """
    if language_code:
        # Normalise: Telegram sometimes sends 'zh-hans', 'pt-br', etc.
        code = language_code.lower().split("-")[0]
        # Special case: retain zh-cn / zh-tw granularity when available
        full = language_code.lower()
        if full in LANG_WARNINGS:
            return LANG_WARNINGS[full]
        if code in LANG_WARNINGS:
            return LANG_WARNINGS[code]
    return LANG_WARNINGS["en"]


def _is_foreign_with_confidence(clean_text: str) -> tuple[bool, str, float]:
    """
    Run langdetect and return (is_foreign, detected_lang, confidence).
    Returns (False, 'en', 0.0) on any detection failure so the bot
    defaults to trusting the user — consistent with the <70% leniency rule.
    """
    try:
        results = detect_langs(clean_text)   # returns list[Language], sorted desc
        if not results:
            return False, "en", 0.0

        top = results[0]
        lang: str = top.lang          # e.g. 'ru', 'ar', 'en'
        confidence: float = top.prob  # 0.0–1.0

        # Trust the user if confidence is at or below the threshold
        if confidence <= CONFIDENCE_THRESHOLD:
            return False, lang, confidence

        # English (or close English variant) → no action
        if lang == "en":
            return False, lang, confidence

        return True, lang, confidence

    except LangDetectException:
        return False, "en", 0.0


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _write_system_warning(pool, user_id: int, reason: str) -> None:
    """
    1. Insert a 'warn' row into `penalties` with admin_id = 0 (system).
    2. Increment `warning_count` in `users`.
    Both operations run inside a single acquired connection.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO penalties (id, user_id, admin_id, action, reason, timestamp)
            VALUES ($1, $2, 0, 'warn', $3, $4)
            """,
            uuid.uuid4(),
            user_id,
            reason,
            datetime.now(timezone.utc),
        )
        await conn.execute(
            """
            UPDATE users
            SET warning_count = warning_count + 1
            WHERE user_id = $1
            """,
            user_id,
        )


# ── Filter: only fire on plain-text messages in group/supergroup chats ────────

class _GroupTextFilter:
    """
    Reusable pre-check filter. Passes only when:
      - The message is in a group or supergroup.
      - The message has text (not None).
      - The text is NOT a bot command.
      - The stripped clean text (after noise removal) is ≥ MIN_TEXT_LENGTH.
    """

    def __call__(self, message: Message) -> bool:
        if message.chat.type not in ("group", "supergroup"):
            return False
        if not message.text:
            return False
        if message.text.startswith("/"):
            return False
        clean = _strip_noise(message.text)
        if len(clean) < MIN_TEXT_LENGTH:
            return False
        return True


_group_text_filter = _GroupTextFilter()


# ── Core handler ─────────────────────────────────────────────────────────────

@router.message(F.chat.type.in_({"group", "supergroup"}), F.text)
async def enforce_english(
    message: Message,
    bot: Bot,
    db_pool,       # injected via dispatcher.workflow_data["db_pool"]
    **kwargs,
) -> None:
    """
    Main language enforcement handler.

    Guards are applied in order of cheapness (short-circuit evaluation):
      1. Command check   — O(1) string prefix
      2. Length check    — O(1) len()
      3. Noise strip     — O(n) regex
      4. NLP detection   — O(n) langdetect (CPU-only, no I/O)
    """
    text: str = message.text  # guaranteed non-None by F.text filter

    # ── Guard 1: skip bot commands ────────────────────────────────────────────
    if text.startswith("/"):
        return

    # ── Guard 2: skip short text ──────────────────────────────────────────────
    clean_text = _strip_noise(text)
    if len(clean_text) < MIN_TEXT_LENGTH:
        return

    # ── Guard 3: language detection ───────────────────────────────────────────
    is_foreign, detected_lang, confidence = _is_foreign_with_confidence(clean_text)

    if not is_foreign:
        return   # English, low-confidence, or detection error → trust the user

    # ── Action: delete the offending message ─────────────────────────────────
    user = message.from_user
    chat_id = message.chat.id
    msg_id = message.message_id

    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception as exc:
        # Bot may lack delete permissions — log and abort gracefully
        logger.warning(
            "language_mod: could not delete message %d in chat %d: %s",
            msg_id, chat_id, exc,
        )
        return

    logger.info(
        "language_mod: deleted message %d from user %d "
        "(lang=%s, confidence=%.2f)",
        msg_id, user.id, detected_lang, confidence,
    )

    # ── Action: DM the user in their native language ──────────────────────────
    tg_lang = user.language_code   # e.g. 'ru', 'es', 'zh-hans', None
    warning_text = _get_warning_text(tg_lang)

    try:
        await bot.send_message(
            chat_id=user.id,
            text=warning_text,
            parse_mode="HTML",
        )
    except Exception as exc:
        # User may have never started the bot — non-fatal
        logger.warning(
            "language_mod: could not DM user %d: %s", user.id, exc
        )

    # ── Action: audit log to PostgreSQL ──────────────────────────────────────
    reason = (
        f"[AUTO] Non-English message detected in chat {chat_id}. "
        f"Detected language: {detected_lang} (confidence: {confidence:.0%})."
    )
    try:
        await _write_system_warning(db_pool, user.id, reason)
    except Exception as exc:
        logger.error(
            "language_mod: DB write failed for user %d: %s", user.id, exc
        )
