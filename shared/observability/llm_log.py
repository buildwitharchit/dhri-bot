# shared/observability/llm_log.py
#
# Single writer for v5.llm_calls. Slice 3 wires this into VARC; later slices
# wire it into planner / mentor / extractor. Always best-effort — a logging
# failure must NOT sink the user-facing response (Principle 5).

import logging
from typing import Optional

from shared.db.client import db
from shared.llm.openrouter import LLMCallResult

logger = logging.getLogger(__name__)


async def record_llm_call(
    *,
    service: str,
    purpose: str,
    result: Optional[LLMCallResult] = None,
    success: bool = True,
    error_message: Optional[str] = None,
    fallback_model: Optional[str] = None,
    student_id: Optional[str] = None,
    session_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> None:
    """Persist a row to v5.llm_calls. On `success=True`, `result` is required.
    On `success=False`, `result` may be None — supply `fallback_model` so we
    still record which model the failed attempt targeted."""
    if success:
        if result is None:
            logger.warning("record_llm_call(success=True) called without result; skipping")
            return
        model = result.model
        input_tokens = result.input_tokens
        output_tokens = result.output_tokens
        cost_usd = result.cost_usd
        latency_ms = result.latency_ms
    else:
        model = (result.model if result else None) or fallback_model or "unknown"
        input_tokens = result.input_tokens if result else 0
        output_tokens = result.output_tokens if result else 0
        cost_usd = result.cost_usd if result else 0.0
        latency_ms = result.latency_ms if result else 0

    try:
        await db.execute(
            """
            INSERT INTO v5.llm_calls
              (student_id, session_id, message_id,
               service, model, purpose,
               input_tokens, output_tokens, cost_usd, latency_ms,
               success, error_message)
            VALUES ($1::uuid, $2::uuid, $3::uuid,
                    $4, $5, $6,
                    $7, $8, $9, $10,
                    $11, $12)
            """,
            student_id, session_id, message_id,
            service, model, purpose,
            input_tokens, output_tokens, cost_usd, latency_ms,
            success, (error_message or None),
        )
    except Exception:
        logger.exception("failed to record llm_calls row (continuing)")
