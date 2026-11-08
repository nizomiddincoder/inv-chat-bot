"""Daily database backups at 03:00 (Asia/Tashkent) + admin panel controls.

Every meaningful table is serialized to one JSON file under backups/.
Only the latest KEEP_BACKUPS files are kept; older ones are deleted
automatically. A failed backup sends a Telegram message to the owner.

Note: on Railway the filesystem is ephemeral (files survive until the next
deploy), so the owner should download important copies from
Boshqaruv > Zaxira from time to time.
"""
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import telegram_client as tg
from .database import SessionLocal
from .models import utcnow
from .services import TZ, get_owner_notify_chat_id, log_audit

log = logging.getLogger("bot.backup")

BACKUP_DIR = Path(__file__).resolve().parent.parent / "backups"
KEEP_BACKUPS = 14


def _serialize_value(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def create_backup(actor: str = "system") -> tuple[str | None, str | None]:
    """Dump every backup table to backups/backup-<ts>.json. Returns (filename, error)."""
    from . import models

    try:
        BACKUP_DIR.mkdir(exist_ok=True)
        dump: dict = {"created_at": utcnow().isoformat(), "tables": {}}
        table_models = {
            table: getattr(models, model_name)
            for table, model_name in (
                ("courses", "Course"), ("groups", "Group"), ("teachers", "Teacher"),
                ("employees", "Employee"), ("leads", "Lead"), ("students", "Student"),
                ("payments", "Payment"), ("lead_notes", "LeadNote"),
                ("student_notes", "StudentNote"), ("settings", "Setting"),
                ("bot_messages", "BotMessage"), ("ai_settings", "AiSetting"),
                ("message_templates", "MessageTemplate"), ("audit_log", "AuditLog"),
            )
        }
        with SessionLocal() as session:
            for table, model in table_models.items():
                rows = session.query(model).all()
                columns = [c.name for c in model.__table__.columns]
                dump["tables"][table] = [
                    {col: _serialize_value(getattr(row, col)) for col in columns}
                    for row in rows
                ]

        timestamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
        filename = f"backup-{timestamp}.json"
        (BACKUP_DIR / filename).write_text(
            json.dumps(dump, ensure_ascii=False), encoding="utf-8"
        )
        _prune_old_backups()
        log_audit(actor, "backup", 0, "yaratildi", None, None, filename)
        log.info("Backup created: %s", filename)
        return filename, None
    except Exception as exc:  # noqa: BLE001 — any failure must reach the owner
        log.exception("Backup failed")
        return None, f"{type(exc).__name__}: {exc}"


def _prune_old_backups() -> None:
    """Keep only the newest KEEP_BACKUPS files."""
    files = sorted(BACKUP_DIR.glob("backup-*.json"))
    for old in files[: max(len(files) - KEEP_BACKUPS, 0)]:
        try:
            old.unlink()
            log.info("Old backup removed: %s", old.name)
        except OSError:
            pass


def list_backups() -> list[dict]:
    """Backup files for the admin page, newest first."""
    if not BACKUP_DIR.is_dir():
        return []
    result = []
    for path in sorted(BACKUP_DIR.glob("backup-*.json"), reverse=True):
        stat = path.stat()
        result.append(
            {
                "name": path.name,
                "size_kb": max(round(stat.st_size / 1024), 1),
                "created": datetime.fromtimestamp(stat.st_mtime, TZ).strftime("%d.%m.%Y %H:%M"),
            }
        )
    return result


def _notify_backup_failure(error: str) -> None:
    owner_chat_id = get_owner_notify_chat_id()
    if owner_chat_id is None:
        log.error("Backup failed and OWNER chat id is not set: %s", error)
        return
    tg.send_message(
        owner_chat_id,
        "Xatolik: avtomatik zaxira nusxa olinmadi.\n"
        f"Sabab: {error[:300]}\n"
        "Railway loglarini tekshiring va qo'lda zaxira oling "
        "(Boshqaruv > Zaxira).",
    )


def _seconds_until_next_0300() -> float:
    now = datetime.now(TZ)
    target = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _loop() -> None:
    while True:
        try:
            time.sleep(max(_seconds_until_next_0300(), 1))
            filename, error = create_backup("system (03:00)")
            if error:
                _notify_backup_failure(error)
        except Exception:
            log.exception("Backup loop failed")
            try:
                _notify_backup_failure("backup loop xatosi")
            except Exception:
                pass
        threading.Event().wait(60)


def start_backup_thread() -> None:
    thread = threading.Thread(target=_loop, daemon=True, name="backups")
    thread.start()
    log.info("Backup thread started (daily at 03:00 Tashkent)")
