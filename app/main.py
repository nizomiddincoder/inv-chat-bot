"""FastAPI application: webhook endpoint, health check, admin panel, startup tasks."""
import logging
import re
import traceback as tb_module
from contextlib import asynccontextmanager

from fastapi import Body, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import claude_client
from .admin.routes import router as admin_router
from .config import settings
from .database import init_db
from .followup import start_followup_thread
from .handlers import handle_update
from .reminders import start_reminders_thread
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
    start_reminders_thread()  # group/trial/debt Telegram reminders, background thread
    from .backup import start_backup_thread
    start_backup_thread()     # daily 03:00 Tashkent DB backups, background thread
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
from fastapi.responses import HTMLResponse, Response  # noqa: E402

app = FastAPI(title="Inventor Innovation Center Bot", lifespan=lifespan)
app.include_router(admin_router)

# ---------------------------------------------------------------------------
# Error journal: every unhandled (500) error lands in error_log with secrets
# scrubbed, viewable in Boshqaruv > Xatolar.
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    (re.compile(r"\d{8,10}:AA[\w-]{30,}"), "***bot-token***"),
    (re.compile(r"sk-[A-Za-z0-9\-_]{10,}"), "***api-key***"),
    (re.compile(r"(password|parol|token|secret|api[-_]?key|authorization)=[^&\s'\"]+", re.I), r"\1=***"),
]


def _sanitize(text: str) -> str:
    """Remove tokens, passwords and keys from an error text."""
    for secret_name in (
        "BOT_TOKEN", "ADMIN_PASSWORD", "WEBHOOK_SECRET",
        "ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "SESSION_SECRET",
    ):
        secret = getattr(settings, secret_name, "") or ""
        if len(secret) >= 6 and secret in text:
            text = text.replace(secret, "***")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:4000]


def _log_error_to_db(request: Request, exc: Exception) -> None:
    from .auth import verify_session
    from .database import SessionLocal
    from .models import ErrorLog

    try:
        full = "".join(tb_module.format_exception(type(exc), exc, exc.__traceback__))
        login = verify_session(request.cookies.get("markaz_session", "")) or ""
        with SessionLocal() as session:
            session.add(
                ErrorLog(
                    method=request.method,
                    path=request.url.path[:200],
                    user=login,
                    message=_sanitize(f"{type(exc).__name__}: {exc}")[:500],
                    traceback_text=_sanitize(full),
                )
            )
            session.commit()
    except Exception:
        logging.getLogger("errors").exception("Could not store error in error_log")


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    """Catch every unhandled exception: journal it (sanitized), answer 500."""
    logging.getLogger("errors").error(
        "Unhandled error on %s %s: %s", request.method, request.url.path, exc
    )
    _log_error_to_db(request, exc)
    if request.url.path.startswith("/admin/") and "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(
            "<h1>Server xatosi</h1><p>Xatolik jurnalga yozildi. "
            "Boshqaruv &gt; Xatolar sahifasida ko'rishingiz mumkin.</p>",
            status_code=500,
        )
    return JSONResponse({"ok": False, "error": "Server xatosi"}, status_code=500)


# ---------------------------------------------------------------------------
# PWA: installable admin app ("Ekranga qo'shish"), offline shows a notice
# ---------------------------------------------------------------------------

_APP_ICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'>"
    "<circle cx='256' cy='256' r='256' fill='#5B6CF0'/>"
    "<text x='256' y='345' font-family='Arial,sans-serif' font-size='250' "
    "font-weight='700' fill='white' text-anchor='middle'>M</text></svg>"
)

_SW_JS = """
// Minimal service worker: online-first, offline shows "Internet yo'q".
var OFFLINE_HTML = "<!doctype html><html lang='uz'><head><meta charset='utf-8'>" +
  "<meta name='viewport' content='width=device-width, initial-scale=1'>" +
  "<style>body{font-family:system-ui,Arial,sans-serif;background:#F4F5FD;color:#1E2235;" +
  "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}" +
  "div{text-align:center}h1{font-size:20px}p{color:#6B7180}</style></head><body>" +
  "<div><h1>Internet yo'q</h1><p>Internetga ulanib, sahifani qayta yuklang.</p></div></body></html>";

self.addEventListener("install", function (event) {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", function (event) {
  if (event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request).catch(function () {
      if (event.request.mode === "navigate") {
        return new Response(OFFLINE_HTML, {
          status: 503,
          headers: { "Content-Type": "text/html; charset=utf-8" }
        });
      }
      return Response.error();
    })
  );
});
"""


@app.get("/manifest.webmanifest")
def pwa_manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "Markaz CRM — Inventor Innovation Center",
            "short_name": "Markaz",
            "start_url": "/admin/kanban",
            "display": "standalone",
            "background_color": "#F4F5FD",
            "theme_color": "#5B6CF0",
            "icons": [
                {
                    "src": "/app-icon.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any",
                }
            ],
        }
    )


@app.get("/app-icon.svg")
def pwa_icon() -> Response:
    return Response(content=_APP_ICON_SVG, media_type="image/svg+xml")


@app.get("/sw.js")
def pwa_service_worker() -> Response:
    return Response(content=_SW_JS, media_type="application/javascript")


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
