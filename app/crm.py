"""CRM adapter — the single place where leads are pushed to external systems.

Right now the lead is only saved to our own PostgreSQL database (the bot does
that before calling push_lead). When the owner is ready to connect an external
CRM, only this file needs to change.
"""
import logging

log = logging.getLogger("crm")


def push_lead(lead) -> None:
    """Called every time a lead is created or updated.

    The `lead` is a SQLAlchemy Lead object that is already saved in our DB.
    Currently this function does nothing except log — the DB save itself
    already happened in services.upsert_lead().

    TODO(owner): connect your external CRM here. Ask your CRM provider for
    their "webhook URL" or API documentation, then uncomment and adapt the
    example below.

    Example (commented out) — sending the lead to an external CRM webhook:

        import httpx

        CRM_WEBHOOK_URL = "https://crm.example.com/api/leads"   # <- from your CRM
        CRM_API_KEY = "your-secret-api-key"                     # <- from your CRM

        payload = {
            "name": lead.full_name,
            "phone": lead.phone,
            "course": lead.course,
            "username": lead.username,
            "telegram_user_id": lead.telegram_user_id,
            "status": lead.status,
            "created_at": lead.created_at.isoformat(),
        }
        try:
            response = httpx.post(
                CRM_WEBHOOK_URL,
                json=payload,
                headers={"Authorization": f"Bearer {CRM_API_KEY}"},
                timeout=10,
            )
            response.raise_for_status()
            log.info("Lead %s pushed to CRM: %s", lead.id, response.status_code)
        except Exception:
            # Never crash the bot because the CRM is down — just log it.
            log.exception("Failed to push lead %s to CRM", lead.id)
    """
    log.info(
        "CRM adapter: lead id=%s (%s, %s, course=%s) stored locally. "
        "External CRM is not connected yet (see crm.py).",
        lead.id,
        lead.full_name,
        lead.phone,
        lead.course,
    )
