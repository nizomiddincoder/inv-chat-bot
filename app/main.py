"""FastAPI application: webhook endpoint, health check, admin panel, startup tasks."""
import logging
from contextlib import asynccontextmanager

from fastapi import Body, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import claude_client
from .admin.routes import router as admin_router
from .config import settings
from .database import init_db
from .followup import start_followup_thread
from .handlers import handle_update
from .services import claim_update
from .telegram_client import set_webhook

# Show useful logs in the Railway "Logs" tab.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(_: object):
    """Runs once when the app starts (and once when it stops)."""
    missing = settings.check()
    if missing:
        logging.getLogger("startup").error(
            "Missing environment variables: %s — the bot will not work until they are set!",
            ", ".join(missing),
        )
    init_db()          # create tables + seed defaults (safe to run every start)
    set_webhook()      # tell Telegram where to deliver updates
    start_followup_thread()  # 24h follow-up reminders, background thread
    if claude_client.is_enabled():
        logging.getLogger("startup").info(
            "Claude mode ON: model=%s, knowledge files: %d",
            settings.CLAUDE_MODEL,
            claude_client.knowledge_file_count(),
        )
    else:
        logging.getLogger("startup").info(
            "Claude mode OFF (ANTHROPIC_API_KEY not set) — "
            "business chats use the built-in button flow"
        )
    yield


from fastapi import BackgroundTasks, FastAPI  # noqa: E402  (import after logging setup)

app = FastAPI(title="Inventor Innovation Center Bot", lifespan=lifespan)
app.include_router(admin_router)


@app.get("/")
@app.get("/health")
def health() -> JSONResponse:
    """Railway pings this to check the app is alive."""
    return JSONResponse({"status": "ok", "service": "inventor-bot"})


@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    """Telegram sends every update here.

    Security: Telegram includes our secret in the X-Telegram-Bot-Api-Secret-Token
    header. If it does not match WEBHOOK_SECRET, the request is rejected.

    The response is sent immediately (200) and the update is processed in the
    background — Telegram must never wait for Claude or the Telegram API.
    Duplicate deliveries of the same update_id are skipped (Telegram retries
    on network hiccups).
    """
    if not settings.WEBHOOK_SECRET or x_telegram_bot_api_secret_token != settings.WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        update = await request.json()
    except Exception:
        logging.getLogger("webhook").warning("Webhook received non-JSON body")
        return JSONResponse({"ok": True})

    # Dedup by update_id: False means this update was already processed.
    update_id = update.get("update_id")
    if isinstance(update_id, int) and not claim_update(update_id):
        return JSONResponse({"ok": True})

    background_tasks.add_task(_process_update, update)
    return JSONResponse({"ok": True})


def _process_update(update: dict) -> None:
    """Background worker: handle one update, never raising (logged instead)."""
    try:
        handle_update(update)
    except Exception:
        # Never fail the webhook — Telegram would retry forever. Log loudly instead.
        logging.getLogger("webhook").exception("Error while processing update: %s", update)


@app.post("/webhook/test")
def webhook_test(payload: dict = Body(default={})) -> JSONResponse:
    """Simple endpoint to verify the app is reachable. Not used by Telegram."""
    return JSONResponse({"ok": True, "received": payload})
