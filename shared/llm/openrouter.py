# agent/llm.py
#
# OpenRouter-backed LLM + embedding wrappers.
# Checks daily spend cap before each call; records approximate spend after.

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from openai import AsyncOpenAI, RateLimitError

from config import settings
from shared.redis.client import redis

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


@dataclass
class LLMCallResult:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float
    latency_ms: int


async def _chat_completion_with_metadata(
    model: str, messages: list[dict]
) -> LLMCallResult:
    """Core chat completion. Returns content + token/cost/latency metadata.
    Spend tracking still happens here (so it stays consistent across both
    public wrappers below)."""
    est_tokens = sum(_estimate_tokens(m.get("content", "")) for m in messages) + 400
    est_usd = (est_tokens / 1_000_000) * _price(model)
    await _check_spend_cap(est_usd)

    last_err: Optional[Exception] = None
    started_monotonic = time.monotonic()
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await _get_client().chat.completions.create(
                model=model,
                messages=messages,
            )
            content = resp.choices[0].message.content or ""
            usage = getattr(resp, "usage", None)
            if usage is not None:
                input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                total_tokens = int(
                    getattr(usage, "total_tokens", input_tokens + output_tokens) or 0
                )
            else:
                input_tokens = output_tokens = total_tokens = 0
            actual_usd = (
                (total_tokens / 1_000_000) * _price(model) if total_tokens else est_usd
            )
            await _add_spend(actual_usd)
            latency_ms = int((time.monotonic() - started_monotonic) * 1000)
            return LLMCallResult(
                content=content,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cost_usd=actual_usd,
                latency_ms=latency_ms,
            )
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


async def chat_with_metadata(
    *, system: str, user: str, model: str
) -> LLMCallResult:
    """Public v5 entry point: returns full LLMCallResult so callers can
    record_llm_call() with real token counts + cost + latency."""
    return await _chat_completion_with_metadata(
        model,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )


async def llm_call_with_retry(system: str, user: str, model: str) -> str:
    """Backward-compat: content-only return. Used by v4_legacy."""
    result = await _chat_completion_with_metadata(
        model,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return result.content


async def llm_call_with_retry_messages(
    system: str, messages: list[dict], model: str
) -> str:
    """`messages` is an array of {role, content} already excluding system."""
    full = [{"role": "system", "content": system}] + list(messages)
    result = await _chat_completion_with_metadata(model, full)
    return result.content


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
