"""Thin wrapper around the Telegram Bot API using httpx (no bot framework)."""
import logging

import httpx

from .config import settings

log = logging.getLogger("telegram")

_client = httpx.Client(timeout=20)


def _base_url() -> str:
    return f"https://api.telegram.org/bot{settings.BOT_TOKEN}"


def _post(method: str, payload: dict) -> tuple[dict | None, str | None]:
    """Low-level API call. Returns (result, error_description)."""
    if not settings.BOT_TOKEN:
        log.error("BOT_TOKEN is not set — cannot call Telegram API")
        return None, "BOT_TOKEN is not set"
    try:
        response = _client.post(f"{_base_url()}/{method}", json=payload)
        data = response.json()
        if not data.get("ok"):
            # Telegram always explains the problem in "description" — show it in logs.
            description = data.get("description", str(data))
            return None, description
        return data.get("result"), None
    except Exception:
        log.exception("Telegram API %s request error", method)
        return None, "request error (see exception above)"


def call(method: str, payload: dict) -> dict | None:
    """Call a Telegram Bot API method. Returns the result dict, or None on error."""
    result, error = _post(method, payload)
    if error:
        log.error("Telegram API %s failed: %s", method, error)
    return result


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


BUSINESS_ERRORS = ("BUSINESS_PEER_INVALID", "BUSINESS_CONNECTION_INVALID")


def _diagnose_business_connection(connection_id: str) -> None:
    """After a failed business send, check the connection and log its rights.

    The owner can see the result in Railway logs and fix the permission in
    Telegram (Settings -> Business -> Chatbots -> enable 'Reply to messages').
    """
    result, error = _post("getBusinessConnection", {"business_connection_id": connection_id})
    if result is None:
        log.error(
            "getBusinessConnection failed: %s (connection_id=%s). "
            "The connection is likely stale — remove and re-add the bot in "
            "Telegram: Settings -> Business -> Chatbots.",
            error,
            connection_id,
        )
        return
    rights = result.get("rights") or {}
    can_reply = rights.get("can_reply", result.get("can_reply"))
    log.error(
        "Business connection status: is_enabled=%s can_reply=%s (connection_id=%s). "
        "If can_reply is False: open Telegram -> Settings -> Business -> Chatbots, "
        "tap the bot and enable the 'Reply to messages' permission (or remove and "
        "re-add the bot). Note: replies are only possible to chats that wrote to "
        "the account in the last 24 hours.",
        result.get("is_enabled"),
        can_reply,
        connection_id,
    )


def send_message(
    chat_id: int | str,
    text: str,
    business_connection_id: str | None = None,
    reply_markup: dict | None = None,
) -> dict | None:
    """Send a message. business_connection_id is REQUIRED for business chats.

    If Telegram answers BUSINESS_PEER_INVALID / BUSINESS_CONNECTION_INVALID, the
    message is retried once WITHOUT the connection id (harmless: a 400 response
    means nothing was delivered, so no duplicate can happen). If the retry also
    fails, the bot logs the connection's actual permission status.
    """
    payload: dict = {"chat_id": chat_id, "text": text}
    if business_connection_id:
        payload["business_connection_id"] = business_connection_id
    if reply_markup:
        payload["reply_markup"] = reply_markup

    result, error = _post("sendMessage", payload)

    if result is None and business_connection_id and error and any(e in error for e in BUSINESS_ERRORS):
        log.error(
            "sendMessage to chat %s failed: %s (business_connection_id=%s). "
            "Retrying once WITHOUT the connection id...",
            chat_id,
            error,
            business_connection_id,
        )
        payload.pop("business_connection_id", None)
        result, error = _post("sendMessage", payload)
        if result is None:
            _diagnose_business_connection(business_connection_id)

    if result is None and error:
        if "bot was blocked by the user" in error:
            log.error(
                "Telegram API sendMessage failed: %s (chat_id=%s). "
                "HINT: this user has BLOCKED the bot in Telegram. That also blocks "
                "business-mode replies (BUSINESS_PEER_INVALID). Ask the user to open "
                "the bot's chat, press Unblock (or Start), then send /start.",
                error,
                chat_id,
            )
        elif "can't initiate conversation with a user" in error:
            log.error(
                "Telegram API sendMessage failed: %s (chat_id=%s). "
                "HINT: this user has never pressed Start in the bot's chat, so the "
                "bot cannot message them directly. (Business-mode replies do not "
                "require Start — if those fail too, see the BUSINESS_PEER_INVALID "
                "hint above.)",
                error,
                chat_id,
            )
        else:
            log.error("Telegram API sendMessage failed: %s (chat_id=%s)", error, chat_id)
    return result


def answer_callback(callback_query_id: int | str, text: str | None = None) -> dict | None:
    """Acknowledge an inline-button press (stops the 'loading' animation)."""
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return call("answerCallbackQuery", payload)
