# agent/llm.py
#
# OpenRouter-backed LLM + embedding wrappers.
# Checks daily spend cap before each call; records approximate spend after.

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from openai import AsyncOpenAI, RateLimitError

from config import settings
from memory.session import redis

logger = logging.getLogger(__name__)


class SpendCapExceededError(Exception):
    """Raised when the daily USD spend cap would be crossed by this call."""


_client: Optional[AsyncOpenAI] = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url=settings.OPENROUTER_BASE_URL,
        )
    return _client


# ─── SPEND TRACKING ─────────────────────────────────────────────────────────

# Approximate USD per 1M tokens. These are rough input-side prices used purely
# as a cap heuristic — not for billing. Conservative upper bounds.
_APPROX_USD_PER_MTOK = {
    "google/gemini-flash-1.5":       0.30,
    "anthropic/claude-haiku-4-5":    1.00,
    "anthropic/claude-sonnet-4-5":   3.00,
    "openai/text-embedding-3-small": 0.02,
}

_DEFAULT_USD_PER_MTOK = 1.00


def _price(model: str) -> float:
    return _APPROX_USD_PER_MTOK.get(model, _DEFAULT_USD_PER_MTOK)


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _spend_key() -> str:
    return f"spend:{_today_iso()}"


async def _get_spend_today() -> float:
    raw = await redis.get(_spend_key())
    try:
        return float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


async def _add_spend(delta_usd: float) -> None:
    current = await _get_spend_today()
    new_val = round(current + delta_usd, 6)
    await redis.set(_spend_key(), str(new_val), ex=3024000)


async def _check_spend_cap(estimated_usd: float) -> None:
    current = await _get_spend_today()
    if current + estimated_usd > settings.DAILY_LLM_SPEND_CAP_USD:
        raise SpendCapExceededError(
            f"daily cap ${settings.DAILY_LLM_SPEND_CAP_USD} "
            f"would be exceeded (current ${current:.4f}, est +${estimated_usd:.4f})"
        )


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ─── LLM CALLS ──────────────────────────────────────────────────────────────

_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0


async def _chat_completion(model: str, messages: list[dict]) -> str:
    est_tokens = sum(_estimate_tokens(m.get("content", "")) for m in messages) + 400
    est_usd = (est_tokens / 1_000_000) * _price(model)
    await _check_spend_cap(est_usd)

    last_err: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await _get_client().chat.completions.create(
                model=model,
                messages=messages,
            )
            content = resp.choices[0].message.content or ""
            usage = getattr(resp, "usage", None)
            if usage is not None and getattr(usage, "total_tokens", None):
                actual_usd = (usage.total_tokens / 1_000_000) * _price(model)
                await _add_spend(actual_usd)
            else:
                await _add_spend(est_usd)
            return content
        except RateLimitError as e:
            last_err = e
            await asyncio.sleep(_BASE_BACKOFF * (2 ** attempt))
        except Exception as e:
            last_err = e
            logger.warning(f"LLM call failed (attempt {attempt+1}): {e}")
            if attempt == _MAX_RETRIES - 1:
                break
            await asyncio.sleep(_BASE_BACKOFF * (2 ** attempt))
    raise RuntimeError(f"LLM call failed after {_MAX_RETRIES} attempts: {last_err}")


async def llm_call_with_retry(system: str, user: str, model: str) -> str:
    return await _chat_completion(
        model,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )


async def llm_call_with_retry_messages(
    system: str, messages: list[dict], model: str
) -> str:
    """`messages` is an array of {role, content} already excluding system."""
    full = [{"role": "system", "content": system}] + list(messages)
    return await _chat_completion(model, full)


# ─── EMBEDDINGS ─────────────────────────────────────────────────────────────

async def embed(text: str) -> list[float]:
    est_tokens = _estimate_tokens(text)
    est_usd = (est_tokens / 1_000_000) * _price(settings.MODEL_EMBEDDING)
    await _check_spend_cap(est_usd)

    last_err: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await _get_client().embeddings.create(
                model=settings.MODEL_EMBEDDING,
                input=text,
            )
            vec = resp.data[0].embedding
            usage = getattr(resp, "usage", None)
            actual_tokens = (
                getattr(usage, "total_tokens", est_tokens) if usage else est_tokens
            )
            actual_usd = (actual_tokens / 1_000_000) * _price(settings.MODEL_EMBEDDING)
            await _add_spend(actual_usd)
            return vec
        except RateLimitError as e:
            last_err = e
            await asyncio.sleep(_BASE_BACKOFF * (2 ** attempt))
        except Exception as e:
            last_err = e
            logger.warning(f"embed call failed (attempt {attempt+1}): {e}")
            if attempt == _MAX_RETRIES - 1:
                break
            await asyncio.sleep(_BASE_BACKOFF * (2 ** attempt))
    raise RuntimeError(f"embed call failed after {_MAX_RETRIES} attempts: {last_err}")
