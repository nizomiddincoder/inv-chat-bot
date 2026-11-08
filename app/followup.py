"""24-hour follow-up reminder — sent to the OWNER (never to the user).

A background thread checks every 30 minutes for open leads (status "new" or
"contacted") that have had no update for more than 24 hours and sends exactly
one reminder per lead to the owner.
"""
import logging
import threading
from datetime import timedelta

from . import telegram_client as tg
from .database import SessionLocal
from .models import Lead, utcnow
from .services import get_owner_chat_id

log = logging.getLogger("bot.followup")

CHECK_INTERVAL_SECONDS = 30 * 60  # every 30 minutes
FOLLOWUP_AFTER_HOURS = 24


def check_open_leads() -> None:
    """Send one follow-up reminder to the owner for each stale open lead."""
    owner_chat_id = get_owner_chat_id()
    if owner_chat_id is None:
        log.debug("Follow-up skipped: OWNER_CHAT_ID is not set")
        return

    cutoff = utcnow() - timedelta(hours=FOLLOWUP_AFTER_HOURS)
    with SessionLocal() as session:
        stale_leads = (
            session.query(Lead)
            .filter(
                Lead.status.in_(["new", "contacted"]),
                Lead.followup_sent_at.is_(None),
                Lead.updated_at < cutoff,
            )
            .all()
        )
        for lead in stale_leads:
            text = (
                "⏰ Eslatma (24 soatlik follow-up):\n"
                f"Lidga hali javob berilmagan.\n"
                f"Ism: {lead.full_name}\n"
                f"Telefon: {lead.phone}\n"
                f"Kurs: {lead.course}\n"
                f"Holat: {lead.status}"
            )
            if tg.send_message(owner_chat_id, text) is not None:
                lead.followup_sent_at = utcnow()
                session.commit()
                log.info("Follow-up sent to owner for lead id=%s", lead.id)
            else:
                # Sending failed; try again on the next check.
                log.warning("Follow-up send failed for lead id=%s — will retry", lead.id)


def _loop() -> None:
    while True:
        try:
            check_open_leads()
        except Exception:
            log.exception("Follow-up check failed")
        threading.Event().wait(CHECK_INTERVAL_SECONDS)


def start_followup_thread() -> None:
    thread = threading.Thread(target=_loop, daemon=True, name="followup")
    thread.start()
    log.info("Follow-up thread started (checks every %s minutes)", CHECK_INTERVAL_SECONDS // 60)
