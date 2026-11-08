"""Thin wrapper around the Telegram Bot API using httpx (no bot framework)."""
import logging

import httpx

from .config import settings

log = logging.getLogger("telegram")

_client = httpx.Client(timeout=20)


def _base_url() -> str:
    return f"https://api.telegram.org/bot{settings.BOT_TOKEN}"


def call(method: str, payload: dict) -> dict | None:
    """Call a Telegram Bot API method. Returns the result dict, or None on error."""
    if not settings.BOT_TOKEN:
        log.error("BOT_TOKEN is not set — cannot call Telegram API")
        return None
    try:
        response = _client.post(f"{_base_url()}/{method}", json=payload)
        data = response.json()
        if not data.get("ok"):
            # Telegram always explains the problem in "description" — show it in logs.
            log.error("Telegram API %s failed: %s", method, data.get("description", data))
            return None
        return data.get("result")
    except Exception:
        log.exception("Telegram API %s request error", method)
        return None


def set_webhook() -> bool:
    """Register the webhook on startup (PUBLIC_URL + /webhook)."""
    if not settings.PUBLIC_URL or not settings.WEBHOOK_SECRET:
        log.error("PUBLIC_URL or WEBHOOK_SECRET is missing — webhook NOT set")
        return False
    payload = {
        "url": f"{settings.PUBLIC_URL}/webhook",
        "secret_token": settings.WEBHOOK_SECRET,
        # "callback_query" is needed so button presses reach the bot.
        "allowed_updates": [
            "message",
            "business_connection",
            "business_message",
            "edited_business_message",
            "callback_query",
        ],
        "drop_pending_updates": False,
    }
    result = call("setWebhook", payload)
    if result is not None:
        log.info("Webhook set successfully: %s", payload["url"])
        return True
    return False


def send_message(
    chat_id: int | str,
    text: str,
    business_connection_id: str | None = None,
    reply_markup: dict | None = None,
) -> dict | None:
    """Send a message. business_connection_id is REQUIRED for business chats."""
    payload: dict = {"chat_id": chat_id, "text": text}
    if business_connection_id:
        payload["business_connection_id"] = business_connection_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return call("sendMessage", payload)


def answer_callback(callback_query_id: int | str, text: str | None = None) -> dict | None:
    """Acknowledge an inline-button press (stops the 'loading' animation)."""
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return call("answerCallbackQuery", payload)
