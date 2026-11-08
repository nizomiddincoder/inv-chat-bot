"""Telegram reminders — sent only inside working hours (Tashkent time).

Three kinds, each deduplicated per day in the reminder_log journal:
  - group_start: to every active student 1 day before the group starts
  - trial:       to the lead 1 day before the trial lesson
  - debt:        weekly (Mondays) to students with a debt

A message can only be delivered when we know the recipient's chat id —
that is the Telegram user id of the original lead (bot chats). Students
added by hand (no Telegram) are logged as "skipped".

The whole feature is toggled by the "reminders_enabled" setting in the
admin panel (Sozlamalar page); the test button posts to the owner chat.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone

from . import telegram_client as tg
from .database import SessionLocal
from .models import Course, Group, Lead, Payment, ReminderLog, Student
from .services import (
    TZ,
    get_owner_notify_chat_id,
    is_work_hours,
    monthly_price,
    student_debt,
)

log = logging.getLogger("bot.reminders")

CHECK_INTERVAL_SECONDS = 20 * 60


def reminders_enabled() -> bool:
    from .services import get_setting

    return get_setting("reminders_enabled", "1") == "1"


def _fmt_sum(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " so'm"


def _student_chat_id(session, student: Student) -> int | None:
    """Chat id of a student = Telegram user id of their original lead."""
    if not student.lead_id:
        return None
    lead = session.get(Lead, student.lead_id)
    if lead is None or not lead.telegram_user_id or lead.telegram_user_id <= 0:
        return None
    return lead.telegram_user_id


def _already_sent_today(kind: str, target_id: int) -> bool:
    """Tashkent midnight converted to naive UTC (how sqlite stores it)."""
    midnight_tz = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_utc = midnight_tz.astimezone(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as session:
        row = (
            session.query(ReminderLog)
            .filter(
                ReminderLog.kind == kind,
                ReminderLog.target_id == target_id,
                ReminderLog.status == "sent",
                ReminderLog.created_at >= cutoff_utc,
            )
            .first()
        )
    return row is not None


def _send_and_log(kind: str, target_id: int, chat_id: int | None, text: str) -> bool:
    """Send one reminder and write the attempt to the journal. True = sent."""
    with SessionLocal() as session:
        if _already_sent_today(kind, target_id):
            session.add(ReminderLog(kind=kind, target_id=target_id, chat_id=chat_id,
                                    status="skipped", detail="bugun allaqachon yuborilgan"))
            session.commit()
            return False
    if chat_id is None or chat_id <= 0:
        with SessionLocal() as session:
            session.add(ReminderLog(kind=kind, target_id=target_id, chat_id=chat_id,
                                    status="skipped", detail="chat id yo'q (Telegram orqali kelgan lid emas)"))
            session.commit()
        log.info("Reminder %s/%s skipped: no chat id", kind, target_id)
        return False
    ok = tg.send_message(chat_id, text) is not None
    with SessionLocal() as session:
        session.add(
            ReminderLog(
                kind=kind,
                target_id=target_id,
                chat_id=chat_id,
                status="sent" if ok else "failed",
                detail=None if ok else "Telegram API xatosi (loglarda batafsil)",
            )
        )
        session.commit()
    return ok


def send_test_message() -> tuple[bool, str]:
    """The 'Test xabar yuborish' button: one message to the owner chat."""
    owner_chat_id = get_owner_notify_chat_id()
    if owner_chat_id is None:
        return False, "Owner chat ID sozlanmagan (Sozlamalar bo'limini to'ldiring)."
    ok = tg.send_message(
        owner_chat_id,
        "Test xabari — eslatmalar ishlayapti. Bu xabar Markaz CRM'dan yuborildi.",
    )
    with SessionLocal() as session:
        session.add(
            ReminderLog(
                kind="test",
                target_id=owner_chat_id,
                chat_id=owner_chat_id,
                status="sent" if ok else "failed",
                detail=None if ok else "Telegram API xatosi",
            )
        )
        session.commit()
    if ok:
        return True, f"Test xabari yuborildi (chat {owner_chat_id})."
    return False, "Yuborib bo'lmadi — Railway loglarida xatoni tekshiring."


def check_reminders() -> None:
    """Run once: all three reminder kinds (guarded by toggle + work hours)."""
    if not reminders_enabled():
        log.debug("Reminders disabled in settings — skipping")
        return
    if not is_work_hours():
        log.debug("Outside working hours — reminders postponed")
        return

    now = datetime.now(TZ)
    tomorrow = (now + timedelta(days=1)).date()
    today = now.date()

    with SessionLocal() as session:
        # 1. group starts tomorrow -> each active student
        starting = (
            session.query(Group)
            .filter(Group.starts_on == tomorrow, Group.is_enabled.is_(True))
            .all()
        )
        for group in starting:
            students = (
                session.query(Student)
                .filter(Student.group_id == group.id, Student.status == "faol")
                .all()
            )
            for student in students:
                text = (
                    f"Eslatma: \"{group.name}\" guruhingiz ertaga boshlanadi"
                    + (f" ({group.schedule})." if group.schedule else ".")
                    + " Ko'rishguncha!"
                )
                _send_and_log("group_start", student.id, _student_chat_id(session, student), text)

        # 2. trial lesson tomorrow -> the lead
        trials = (
            session.query(Lead)
            .filter(Lead.trial_date == tomorrow, Lead.status == "trial")
            .all()
        )
        for lead in trials:
            text = (
                f"Eslatma: \"Inventor Innovation Center\"dagi sinov darsingiz ertaga"
                f" ({lead.trial_date.strftime('%d.%m')}). Ko'rishguncha!"
            )
            chat_id = lead.telegram_user_id if lead.telegram_user_id and lead.telegram_user_id > 0 else None
            _send_and_log("trial", lead.id, chat_id, text)

        # 3. debtors — once a week (Mondays)
        if now.weekday() == 0:
            students = (
                session.query(Student)
                .filter(Student.status.in_(["faol", "navbatda"]))
                .all()
            )
            courses = {c.id: c for c in session.query(Course).all()}
            groups = {g.id: g for g in session.query(Group).all()}
            for student in students:
                course = courses.get(
                    groups[student.group_id].course_id
                    if student.group_id in groups else None
                )
                price = monthly_price(student, course)
                if price <= 0:
                    continue
                info = student_debt(student, course)
                if info["debt"] <= 0:
                    continue
                group = groups.get(student.group_id or 0)
                text = (
                    "Eslatma: o'quv markazimizda to'lov qarzingiz mavjud — "
                    f"{_fmt_sum(info['debt'])}"
                    + (f" ({group.name} guruhi)." if group else ".")
                    + " To'lov uchun markazga murojaat qiling."
                )
                _send_and_log("debt", student.id, _student_chat_id(session, student), text)


def _loop() -> None:
    while True:
        try:
            check_reminders()
        except Exception:
            log.exception("Reminder check failed")
        threading.Event().wait(CHECK_INTERVAL_SECONDS)


def start_reminders_thread() -> None:
    thread = threading.Thread(target=_loop, daemon=True, name="reminders")
    thread.start()
    log.info("Reminders thread started (checks every %s minutes)", CHECK_INTERVAL_SECONDS // 60)
