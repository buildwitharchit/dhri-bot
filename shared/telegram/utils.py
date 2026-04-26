# bot/utils.py
#
# HTML escaping, message splitting, reply helpers, IST time helpers.

import html
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Union

from telegram import CallbackQuery, InlineKeyboardMarkup, Message
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)

MAX_TELEGRAM_MESSAGE_CHARS = 4000

IST_OFFSET = timedelta(hours=5, minutes=30)


def escape_html(text: Optional[str]) -> str:
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def get_ist_date() -> str:
    """YYYY-MM-DD in IST."""
    now_ist = datetime.now(timezone.utc) + IST_OFFSET
    return now_ist.strftime("%Y-%m-%d")


def get_seconds_until_ist_midnight() -> int:
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc + IST_OFFSET
    next_ist_midnight = (now_ist + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    delta = next_ist_midnight - now_ist
    return max(1, int(delta.total_seconds()))


def format_ago(timestamp: Optional[datetime]) -> str:
    if timestamp is None:
        return "unknown"
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - timestamp
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def format_skill(skill: str) -> str:
    from config import SKILL_DISPLAY_NAMES
    return SKILL_DISPLAY_NAMES.get(skill, skill.replace("_", " ").title())


def _split_on_boundary(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at <= 0:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def send_long_message(
    chat_id: int,
    text: str,
    bot,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = ParseMode.HTML,
) -> None:
    """Split on paragraph boundary and send each chunk. Keyboard on last."""
    chunks = _split_on_boundary(text, MAX_TELEGRAM_MESSAGE_CHARS)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode=parse_mode,
            reply_markup=reply_markup if is_last else None,
        )


async def reply_to(
    message_or_callback: Union[Message, CallbackQuery],
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = ParseMode.HTML,
) -> None:
    """Works for either a Telegram Message or a CallbackQuery."""
    if isinstance(message_or_callback, CallbackQuery):
        target_message = message_or_callback.message
    else:
        target_message = message_or_callback

    if target_message is None:
        logger.warning("reply_to called with no target message")
        return

    await target_message.reply_text(
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )


_ERROR_MESSAGES = {
    "spend_cap": (
        "I'm out of LLM budget for today. Try again after midnight, "
        "or use /stats and /resume which don't need the LLM."
    ),
    "rate_limit": (
        "You've hit today's message cap (50). Rolls over at midnight IST. "
        "Commands and buttons still work."
    ),
    "generic": "Something went wrong on my end. Try again in a moment.",
}


async def edit_telegram_keyboard(
    bot,
    chat_id: int,
    message_id: int,
    new_keyboard: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    """editMessageReplyMarkup. Pass new_keyboard=None to clear the inline keyboard.

    Used by the v5 message bus to enforce Principle 2 (close old keyboards
    when a new question is served). Never raises — returns False on failure
    so the caller can log and continue.
    """
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=new_keyboard,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "edit_telegram_keyboard failed chat_id=%s message_id=%s: %s",
            chat_id, message_id, e,
        )
        return False


async def send_error_to_update(update, error_key: str, bot) -> None:
    text = _ERROR_MESSAGES.get(error_key, _ERROR_MESSAGES["generic"])
    chat_id = None
    if getattr(update, "effective_chat", None):
        chat_id = update.effective_chat.id
    if chat_id is None:
        return
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.warning(f"failed to send error to chat {chat_id}: {e}")
