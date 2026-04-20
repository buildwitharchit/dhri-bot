# main.py

import logging

from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application

from bot.router import route_update
from config import ALL_SUBSKILLS, settings
from db.client import close_db_pool, init_db_pool
from memory.session import init_redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

WEBHOOK_PATH = f"/webhook/{settings.WEBHOOK_SECRET}"

app = FastAPI()
ptb_app: Application | None = None


@app.on_event("startup")
async def startup() -> None:
    global ptb_app

    # Taxonomy consistency check — fail fast at startup (Section 24)
    from retrieval.technique_queries import SUBSKILL_TO_TECHNIQUE_QUERY
    missing = set(ALL_SUBSKILLS) - set(SUBSKILL_TO_TECHNIQUE_QUERY.keys())
    extra = set(SUBSKILL_TO_TECHNIQUE_QUERY.keys()) - set(ALL_SUBSKILLS)
    if missing or extra:
        raise RuntimeError(
            f"Taxonomy mismatch: missing={missing}, extra={extra}. "
            "ALL_SUBSKILLS and SUBSKILL_TO_TECHNIQUE_QUERY must have identical keys."
        )

    await init_db_pool()
    await init_redis()

    ptb_app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    await ptb_app.initialize()

    webhook_url = f"https://{settings.RAILWAY_PUBLIC_DOMAIN}{WEBHOOK_PATH}"
    await ptb_app.bot.set_webhook(
        url=webhook_url,
        allowed_updates=["message", "callback_query"],
    )
    logger.info(f"webhook set: {webhook_url}")


@app.on_event("shutdown")
async def shutdown() -> None:
    global ptb_app
    if ptb_app is not None:
        await ptb_app.shutdown()
    await close_db_pool()


@app.post(WEBHOOK_PATH)
async def webhook(request: Request) -> Response:
    if ptb_app is None:
        return Response(status_code=503)
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await route_update(update, ptb_app)
    return Response(status_code=200)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/admin/cleanup-sessions")
async def cleanup_endpoint(request: Request) -> Response:
    if request.headers.get("X-Admin-Secret") != settings.ADMIN_REPORTS_SECRET:
        return Response(status_code=403)
    from handlers.session_cleanup import cleanup_stale_sessions
    count = await cleanup_stale_sessions()
    return Response(content=f'{{"closed": {count}}}', media_type="application/json")


@app.post("/admin/send-reports")
async def reports_endpoint(request: Request) -> Response:
    if request.headers.get("X-Admin-Secret") != settings.ADMIN_REPORTS_SECRET:
        return Response(status_code=403)
    from handlers.weekly_reports import send_weekly_reports_to_all
    await send_weekly_reports_to_all()
    return Response(content='{"status": "ok"}', media_type="application/json")
