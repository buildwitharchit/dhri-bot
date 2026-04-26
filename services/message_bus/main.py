# services/message_bus/main.py
#
# Thin Telegram boundary for v5.
#   1. Parse the raw Update payload (now also extracts update_id for Fix 7).
#   2. Send "🤔 Thinking..." (capture message_id).
#   3. Refresh chatAction("typing") every 4s while orchestrator runs.
#   4. Close the previous question's inline keyboard if requires_keyboard_close
#      is set on the response (Fix 3 / Principle 2).
#   5. editMessageText on completion (fall back to send if edit fails).
#   6. If the response served a new question, persist the delivered message_id
#      and attempt_id into Redis active session state (Fix 3 / Principle 2).
#
# Bus owns: Telegram I/O + the post-delivery state-write coupling that
# Principle 2 needs. No DB writes, no business logic.

import asyncio
import logging
from typing import Any, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError

from services.orchestrator.main import handle_message
from shared.redis.client import update_state
from shared.telegram.utils import edit_telegram_keyboard

logger = logging.getLogger(__name__)

THINKING_TEXT = "🤔 Thinking..."
TYPING_REFRESH_INTERVAL_SECONDS = 4.0
TELEGRAM_MAX_CHARS = 4_000  # leave headroom under the 4096 hard limit


async def handle_telegram_update(update: dict, bot: Bot) -> None:
    """Entry point: called from the FastAPI v5 webhook with the raw Update dict."""
    parsed = _parse(update)
    if parsed is None:
        logger.debug("v5 bus: ignoring update with no actionable content")
        return

    chat_id = parsed["chat_id"]

    try:
        thinking = await bot.send_message(chat_id=chat_id, text=THINKING_TEXT)
    except TelegramError as e:
        logger.warning("v5 bus: failed to send thinking message: %s", e)
        return
    thinking_message_id = thinking.message_id

    typing_task = asyncio.create_task(_keep_typing(bot, chat_id))

    try:
        response = await handle_message(
            tg_id=parsed["tg_id"],
            content=parsed["content"],
            content_type=parsed["content_type"],
            source_metadata=parsed["source_metadata"],
        )
    except Exception:
        logger.exception("v5 bus: orchestrator raised; sending generic error")
        response = {
            "content": "Something went wrong — try again in a moment.",
            "keyboard": None,
        }
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    # Fix 3 / Principle 2: close prior question's keyboard before serving new.
    if response.get("requires_keyboard_close"):
        prev_msg_id = (response.get("meta") or {}).get("previous_question_message_id")
        if prev_msg_id:
            await edit_telegram_keyboard(bot, chat_id, prev_msg_id, None)

    delivered_message_id = await _deliver(bot, chat_id, thinking_message_id, response)

    # Fix 3 / Principle 2: persist the new question's tg_message_id so the
    # next question_serve can close it.
    track_attempt_id = response.get("track_question_attempt_id")
    if track_attempt_id and delivered_message_id is not None:
        try:
            await update_state(
                parsed["tg_id"],
                last_question_message_id=delivered_message_id,
                last_question_attempt_id=track_attempt_id,
            )
        except Exception:
            logger.exception(
                "v5 bus: failed to persist active-session state tg_id=%s", parsed["tg_id"],
            )


# ─── parsing ────────────────────────────────────────────────────────────────


def _parse(update: dict) -> Optional[dict]:
    """Normalize the Telegram Update into the v5 payload shape."""
    update_id = update.get("update_id")

    if "callback_query" in update:
        cq = update["callback_query"]
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        return {
            "tg_id": cq["from"]["id"],
            "chat_id": chat.get("id"),
            "content": cq.get("data", ""),
            "content_type": "button",
            "source_metadata": {
                "tg_update_id": update_id,
                "tg_chat_id": chat.get("id"),
                "tg_callback_query_id": cq.get("id"),
                "first_name": cq["from"].get("first_name"),
            },
        }

    msg = update.get("message")
    if not msg:
        return None
    text = (msg.get("text") or "").strip()
    if not text:
        # voice / photo / sticker — declined silently in slice 2.5
        return None
    chat = msg.get("chat") or {}
    return {
        "tg_id": msg["from"]["id"],
        "chat_id": chat.get("id"),
        "content": text,
        "content_type": "text",
        "source_metadata": {
            "tg_update_id": update_id,
            "tg_message_id": msg.get("message_id"),
            "tg_chat_id": chat.get("id"),
            "first_name": msg["from"].get("first_name"),
        },
    }


# ─── delivery ───────────────────────────────────────────────────────────────


def _build_reply_markup(keyboard: Optional[dict]) -> Optional[InlineKeyboardMarkup]:
    if not keyboard or "inline_keyboard" not in keyboard:
        return None
    rows = [
        [
            InlineKeyboardButton(text=btn["text"], callback_data=btn["callback_data"])
            for btn in row
        ]
        for row in keyboard["inline_keyboard"]
    ]
    return InlineKeyboardMarkup(rows)


async def _deliver(
    bot: Bot,
    chat_id: int,
    thinking_message_id: int,
    response: dict,
) -> Optional[int]:
    """Edit the thinking message in place. Returns the delivered message_id
    (so the caller can stash it for keyboard-close coordination)."""
    text = response.get("content") or "(no content)"
    if len(text) > TELEGRAM_MAX_CHARS:
        text = text[: TELEGRAM_MAX_CHARS - 1] + "…"
    reply_markup = _build_reply_markup(response.get("keyboard"))

    try:
        edited = await _safe_edit_text(
            bot, chat_id, thinking_message_id, text, reply_markup,
        )
        # editMessageText returns the edited Message object on text edits.
        return getattr(edited, "message_id", thinking_message_id)
    except TelegramError as e:
        logger.warning("v5 bus: edit failed (%s); falling back to new message", e)

    try:
        sent = await _safe_send_text(bot, chat_id, text, reply_markup)
        return sent.message_id
    except TelegramError as e:
        logger.error("v5 bus: send fallback also failed: %s", e)
        return None


# ─── Markdown-with-fallback wrappers ────────────────────────────────────────
#
# Telegram's "Markdown" (legacy) parser supports `*bold*` / `_italic_` / etc.
# but rejects unbalanced or escaped characters. LLM-generated content
# occasionally produces payloads the parser can't handle — when that happens
# we retry the same call without parse_mode so the user always gets the text
# (even if the markdown stops bolding correctly that one time).


def _is_parse_entities_error(err: BaseException) -> bool:
    return "can't parse" in str(err).lower()


async def _safe_edit_text(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup],
) -> Any:
    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
        )
    except BadRequest as e:
        if not _is_parse_entities_error(e):
            raise
        logger.warning(
            "v5 bus: markdown parse failed on edit; retrying as plain text: %s", e,
        )
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )


async def _safe_send_text(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup],
) -> Any:
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
        )
    except BadRequest as e:
        if not _is_parse_entities_error(e):
            raise
        logger.warning(
            "v5 bus: markdown parse failed on send; retrying as plain text: %s", e,
        )
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )


# ─── typing refresh ─────────────────────────────────────────────────────────


async def _keep_typing(bot: Bot, chat_id: int) -> None:
    """Refresh the typing indicator every 4s. Cancelled when response ready."""
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except TelegramError as e:
                logger.debug("v5 bus: chatAction failed (%s); continuing", e)
            await asyncio.sleep(TYPING_REFRESH_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        raise
