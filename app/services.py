"""Business logic shared by the bot handlers and the admin panel."""
import logging
import random
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .config import settings
from .crm import push_lead
from .database import SessionLocal
from .models import (
    AiSetting,
    BotMessage,
    ChatState,
    ChatHistory,
    Course,
    Employee,
    Group,
    Lead,
    LeadNote,
    Payment,
    ProcessedUpdate,
    Setting,
    Student,
    StudentNote,
    Teacher,
    utcnow,
)

log = logging.getLogger("bot.services")

TZ = ZoneInfo("Asia/Tashkent")

# ---------------------------------------------------------------------------
# CRM dictionaries (labels shown in the admin panel, keys stored in the DB)
# ---------------------------------------------------------------------------

SOURCES = {
    "telegram_bot": "Telegram bot",
    "instagram": "Instagram",
    "meta": "Meta reklama",
    "tavsiya": "Tavsiya",
    "yurish": "Yurish",
    "boshqa": "Boshqa",
}
# Old values saved by earlier versions of the bot also mean "Telegram bot".
SOURCE_ALIASES = {"telegram": "telegram_bot", "claude": "telegram_bot", "": "boshqa"}

ROLES = {
    "admin": "Admin",
    "operator": "Operator",
    "teacher": "O'qituvchi",
    "accountant": "Buxgalter",
}
# Roles with no page visibility yet — prepared for a later stage.
ROLE_ACTIVE = {"admin"}

REJECT_REASONS = {
    "narx": "Narx qimmat",
    "boshqa_markaz": "Boshqa markazga ketdi",
    "javob_yoq": "Javob bermadi",
    "vaqt_yoq": "Vaqti yo'q",
    "boshqa": "Boshqa",
}

STUDENT_STATUSES = {
    "faol": "Faol",
    "tugatgan": "Tugatgan",
    "toxtatgan": "To'xtatgan",
    "navbatda": "Navbatda",
}

GROUP_STATUSES = {
    "ochiq": "Ochiq",
    "toldi": "To'ldi",
    "tugagan": "Tugagan",
}


def source_label(value: str | None) -> str:
    """Human-readable source name for a stored key."""
    key = SOURCE_ALIASES.get(value or "", value or "")
    return SOURCES.get(key, key or "—")

# Default bot message templates. Stored in the DB on first start and
# editable in the admin panel (Messages page). {first_name} and {course}
# are placeholders replaced with real values.
DEFAULT_MESSAGES: dict[str, str] = {
    "welcome": (
        "Assalomu alaykum {first_name}, Inventor innovation centerga xush kelibsiz! "
        "Qanday yordam bera olaman? Agar kursda o'qimoqchi bo'lsangiz, kurslardan birini tanlang:\n"
        "1. Kompyuter savodxonlik\n"
        "2. Buxgalteriya 0 dan\n"
        "3. Glavbuxgalteriya\n"
        "4. Trading\n"
        "5. Sun'iy intellekt\n\n"
        "Kursni tanlash uchun quyidagi tugmalardan birini bosing yoki raqamini yozing."
    ),
    "after_course": "Javobingiz uchun rahmat! Ismingizni yozib qoldiring.",
    "after_name": "Telefon raqamingizni qoldiring, admin tez orada aloqaga chiqadi.",
    "after_phone": (
        "Kechirasiz, telefon raqamini tushunmadim. Iltimos, raqamni quyidagi "
        "formatlardan birida yuboring: +998901234567, 998901234567 yoki 901234567."
    ),
    "name_invalid": (
        "Kechirasiz, ismni to'g'ri yozing (2 dan 50 gacha harf, raqam va havolasiz)."
    ),
    "name_edited": (
        "Ismingiz tahrirlandi ✅ Endi telefon raqamingizni yuboring."
    ),
    "finish": "Rahmat! Ma'lumotlaringiz qabul qilindi, admin tez orada bog'lanadi.",
    "out_of_hours": "Hozir ish vaqtimiz tugagan, ertaga javob beramiz.",
}


# ---------------------------------------------------------------------------
# Settings (DB with env fallback)
# ---------------------------------------------------------------------------

def get_setting(key: str, default: str | None = None) -> str | None:
    """Read a setting from the DB, fall back to the env default."""
    with SessionLocal() as session:
        row = session.get(Setting, key)
        if row is not None and row.value is not None:
            return row.value
    # Env fallbacks for known keys
    env_map = {
        "work_start": settings.WORK_START,
        "work_end": settings.WORK_END,
        "owner_chat_id": str(settings.OWNER_CHAT_ID) if settings.OWNER_CHAT_ID else "",
    }
    return env_map.get(key, default)


def set_setting(key: str, value: str) -> None:
    with SessionLocal() as session:
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
        session.commit()


def get_owner_chat_id() -> int | None:
    """Owner chat id: DB setting first, then the OWNER_CHAT_ID env variable."""
    value = get_setting("owner_chat_id", "")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def get_owner_notify_chat_id() -> int | None:
    """Where to send notifications to the owner (new leads, follow-ups).

    OWNER_NOTIFY_CHAT_ID env first; when not set, falls back to the regular
    owner chat id (admin panel setting or OWNER_CHAT_ID).
    """
    if settings.OWNER_NOTIFY_CHAT_ID:
        return settings.OWNER_NOTIFY_CHAT_ID
    return get_owner_chat_id()


# ---------------------------------------------------------------------------
# update_id deduplication (Telegram retries must never be processed twice)
# ---------------------------------------------------------------------------

UPDATE_TTL_DAYS = 7  # how long processed update_ids are remembered


def claim_update(update_id: int) -> bool:
    """Try to mark an update_id as processed. False -> duplicate, skip it.

    The insert doubles as the lock: a concurrent request for the same
    update_id hits the primary key and gets IntegrityError.
    """
    with SessionLocal() as session:
        try:
            session.add(ProcessedUpdate(update_id=update_id))
            session.commit()
        except IntegrityError:
            session.rollback()
            log.info("Duplicate update_id %s skipped", update_id)
            return False
        # Occasionally drop old rows so the table stays tiny.
        if random.random() < 0.02:
            cutoff = utcnow() - timedelta(days=UPDATE_TTL_DAYS)
            deleted = (
                session.query(ProcessedUpdate)
                .filter(ProcessedUpdate.received_at < cutoff)
                .delete(synchronize_session=False)
            )
            if deleted:
                session.commit()
                log.debug("Cleaned %s old processed_updates rows", deleted)
    return True


# ---------------------------------------------------------------------------
# Chat history (context for Claude-generated replies)
# ---------------------------------------------------------------------------

HISTORY_KEEP_PER_CHAT = 50  # hard cap stored per chat


def add_history(chat_id: int, role: str, text: str) -> None:
    """Store one message of the conversation and prune rows beyond the cap."""
    with SessionLocal() as session:
        session.add(ChatHistory(chat_id=chat_id, role=role, text=text[:4000]))
        session.commit()
        keep = (
            session.query(ChatHistory.id)
            .filter(ChatHistory.chat_id == chat_id)
            .order_by(ChatHistory.id.desc())
            .limit(HISTORY_KEEP_PER_CHAT)
            .all()
        )
        if len(keep) == HISTORY_KEEP_PER_CHAT:  # only prune when over the cap
            session.query(ChatHistory).filter(
                ChatHistory.chat_id == chat_id,
                ChatHistory.id.notin_([row[0] for row in keep]),
            ).delete(synchronize_session=False)
            session.commit()


def get_history(chat_id: int, limit: int = 16) -> list[dict]:
    """Last `limit` messages of a chat, oldest first: [{"role", "text"}, ...]."""
    with SessionLocal() as session:
        rows = (
            session.query(ChatHistory)
            .filter(ChatHistory.chat_id == chat_id)
            .order_by(ChatHistory.id.desc())
            .limit(limit)
            .all()
        )
    return [{"role": row.role, "text": row.text} for row in reversed(rows)]


def clear_history(chat_id: int) -> None:
    """Forget the whole conversation (used when the visitor resets the chat)."""
    with SessionLocal() as session:
        session.query(ChatHistory).filter(ChatHistory.chat_id == chat_id).delete(
            synchronize_session=False
        )
        session.commit()


# ---------------------------------------------------------------------------
# AI settings (admin panel -> AI Javoblar page)
# ---------------------------------------------------------------------------

AI_STYLE_KEY = "style_prompt"        # how Claude writes its answers
AI_KNOWLEDGE_KEY = "extra_knowledge"  # extra facts appended to knowledge/ files


def get_ai_text(key: str) -> str | None:
    """Read a long AI setting text (style prompt, extra knowledge) or None."""
    with SessionLocal() as session:
        row = session.get(AiSetting, key)
        if row is not None and row.text:
            return row.text
    return None


def set_ai_text(key: str, text: str) -> None:
    """Save a long AI setting text (empty string = fall back to the default)."""
    with SessionLocal() as session:
        row = session.get(AiSetting, key)
        if row is None:
            session.add(AiSetting(key=key, text=text))
        else:
            row.text = text
            row.updated_at = utcnow()
        session.commit()


def get_work_hours() -> tuple[time, time]:
    start = get_setting("work_start", settings.WORK_START) or "09:00"
    end = get_setting("work_end", settings.WORK_END) or "20:00"
    try:
        return time.fromisoformat(start), time.fromisoformat(end)
    except ValueError:
        log.warning("Invalid WORK_START/WORK_END values (%s, %s), using 09:00-20:00", start, end)
        return time(9, 0), time(20, 0)


def is_work_hours() -> bool:
    """True if the current time in Tashkent is inside working hours."""
    start, end = get_work_hours()
    now = datetime.now(TZ).time()
    if start <= end:
        return start <= now <= end
    # Overnight shift (e.g. 20:00 -> 09:00)
    return now >= start or now <= end


# ---------------------------------------------------------------------------
# Message templates
# ---------------------------------------------------------------------------

def render(key: str, **placeholders: object) -> str:
    """Get a bot message template from the DB and fill placeholders.

    Plain string replace (not str.format) so a stray '{' typed by the admin
    can never crash the bot.
    """
    text = DEFAULT_MESSAGES.get(key, key)
    with SessionLocal() as session:
        row = session.get(BotMessage, key)
        if row is not None and row.text:
            text = row.text
    for name, value in placeholders.items():
        text = text.replace("{" + name + "}", str(value))
    return text


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------

def get_enabled_courses() -> list[Course]:
    with SessionLocal() as session:
        return (
            session.query(Course)
            .filter(Course.is_enabled.is_(True))
            .order_by(Course.sort_order, Course.id)
            .all()
        )


def get_all_courses() -> list[Course]:
    with SessionLocal() as session:
        return session.query(Course).order_by(Course.sort_order, Course.id).all()


# ---------------------------------------------------------------------------
# Teachers and groups (CRM -> Claude knowledge)
# ---------------------------------------------------------------------------

def get_teachers(enabled_only: bool = False) -> list[Teacher]:
    with SessionLocal() as session:
        query = session.query(Teacher).order_by(Teacher.full_name, Teacher.id)
        if enabled_only:
            query = query.filter(Teacher.is_enabled.is_(True))
        return query.all()


def get_all_groups() -> list[Group]:
    with SessionLocal() as session:
        return session.query(Group).order_by(Group.id).all()


def build_dynamic_knowledge() -> str:
    """Build the CRM knowledge block for Claude from the database.

    Courses (price, duration, teacher, description), teachers (bio) and
    groups (schedule, start date) are joined into one markdown text, so
    every newly opened course is instantly known to the bot — no redeploy.
    """
    with SessionLocal() as session:
        courses = session.query(Course).order_by(Course.sort_order, Course.id).all()
        teachers = {t.id: t for t in session.query(Teacher).all()}
        groups = session.query(Group).order_by(Group.id).all()

    teacher_name = lambda tid: (  # noqa: E731
        teachers[tid].full_name if tid in teachers else None
    )

    lines: list[str] = []
    if courses:
        lines.append("### Kurslar (markaz bazasidan, eng yangi ma'lumot)")
        for course in courses:
            parts = [f"**{course.name}**"]
            if course.teacher_id:
                tname = teacher_name(course.teacher_id)
                if tname:
                    parts.append(f"o'qituvchi: {tname}")
            if course.price_sum:
                parts.append(f"narx: {course.price_sum:,} so'm/oy".replace(",", " "))
            elif course.price:
                parts.append(f"narx: {course.price}")
            if course.duration:
                parts.append(f"davomiylik: {course.duration}")
            line = " — ".join(parts)
            if course.description:
                line += f"\n  {course.description.strip()}"
            if not course.is_enabled:
                line += "\n  (hozircha o'chirilgan — yangi o'quvchilarni taklif qilmang)"
            lines.append(f"- {line}")
    if teachers:
        lines.append("### O'qituvchilar")
        for teacher in teachers.values():
            if not teacher.is_enabled:
                continue
            info = f"- **{teacher.full_name}**"
            if teacher.phone:
                info += f" (tel: {teacher.phone})"
            if teacher.bio:
                info += f" — {teacher.bio.strip()}"
            lines.append(info)
    if groups:
        lines.append("### Guruhlar")
        for group in groups:
            if not group.is_enabled:
                continue
            parts = [f"**{group.name}**"]
            if group.course_id:
                course = next((c for c in courses if c.id == group.course_id), None)
                if course:
                    parts.append(f"kurs: {course.name}")
            if group.teacher_id:
                tname = teacher_name(group.teacher_id)
                if tname:
                    parts.append(f"o'qituvchi: {tname}")
            if group.schedule:
                parts.append(f"jadval: {group.schedule}")
            if group.start_date:
                parts.append(f"boshlanish: {group.start_date}")
            if group.capacity:
                parts.append(f"joylar: {group.capacity}")
            if group.status == "toldi":
                parts.append("GURUH TO'LDI — yangi o'quvchini navbatga yozing")
            elif group.status == "tugagan":
                parts.append("guruh tugagan — taklif qilmang")
            lines.append(f"- {' — '.join(parts)}")
    return "\n".join(lines).strip()


def match_course(text: str, courses: list[Course]) -> Course | None:
    """Match typed text ('1', '2)', 'trading', ...) against the course list."""
    t = text.strip().lower().rstrip(".):")
    if not t:
        return None
    if t.isdigit():
        index = int(t)
        if 1 <= index <= len(courses):
            return courses[index - 1]
        return None
    for course in courses:
        if t == course.name.lower():
            return course
    # Substring match only for reasonably long queries to avoid false hits
    if len(t) >= 4:
        for course in courses:
            if t in course.name.lower():
                return course
    return None


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------

def get_state(chat_id: int) -> dict:
    with SessionLocal() as session:
        state = session.get(ChatState, chat_id)
        if state is None:
            return {"step": 0, "course": None, "temp_name": None}
        return {"step": state.step, "course": state.course, "temp_name": state.temp_name}


def set_state(chat_id: int, step: int, course: str | None, temp_name: str | None) -> None:
    with SessionLocal() as session:
        state = session.get(ChatState, chat_id)
        if state is None:
            state = ChatState(chat_id=chat_id)
            session.add(state)
        state.step = step
        state.course = course
        state.temp_name = temp_name
        state.updated_at = utcnow()
        session.commit()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_name(text: str) -> str | None:
    """Name must be 2-50 letters, no digits or links. Returns cleaned name or None."""
    name = text.strip()
    if not 2 <= len(name) <= 50:
        return None
    if re.search(r"[\d@/\\]|https?://|www\.|t\.me", name, re.IGNORECASE):
        return None
    # Allow letters (any language), spaces, apostrophes and hyphens (O'g'ilbek ...)
    if not re.sub(r"[\s'\-`]", "", name).isalpha():
        return None
    return name


def normalize_phone(text: str) -> str | None:
    """Accept +998XXXXXXXXX, 998XXXXXXXXX or 9XXXXXXXX. Return +998XXXXXXXXX or None."""
    raw = text.strip()
    digits = re.sub(r"\D", "", raw.lstrip("+"))
    if raw.startswith("+"):
        # Must be a full international number
        if len(digits) == 12 and digits.startswith("998"):
            return "+" + digits
        return None
    if len(digits) == 12 and digits.startswith("998"):
        return "+" + digits
    if len(digits) == 9 and digits.startswith("9"):
        return "+998" + digits
    return None


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

def upsert_lead(
    telegram_user_id: int,
    username: str | None,
    first_name: str | None,
    course: str | None,
    full_name: str,
    phone: str,
    source: str = "telegram_bot",
) -> Lead:
    """Create a lead, or update the existing one for the same user/phone.

    Deduplication rule: same telegram_user_id OR same phone -> update the
    existing row (never a duplicate) and mark it as a repeat request
    ("takroriy murojaat", repeat_count > 0).
    """
    now = utcnow()
    with SessionLocal() as session:
        lead = session.query(Lead).filter(Lead.telegram_user_id == telegram_user_id).first()
        if lead is None:
            lead = session.query(Lead).filter(Lead.phone == phone).first()

        if lead is not None:
            lead.full_name = full_name
            lead.phone = phone
            if course:
                lead.course = course
            if username:
                lead.username = username
            if first_name:
                lead.first_name = first_name
            lead.updated_at = now
            lead.repeat_count = (lead.repeat_count or 0) + 1
            # Allow the 24h follow-up timer again for a re-opened lead.
            lead.followup_sent_at = None
            if lead.status == "rejected":
                lead.status = "new"
                lead.reject_reason = None
            log.info(
                "Lead updated (repeat #%s): id=%s name=%s phone=%s",
                lead.repeat_count, lead.id, full_name, phone,
            )
        else:
            lead = Lead(
                telegram_user_id=telegram_user_id,
                username=username,
                first_name=first_name,
                course=course,
                full_name=full_name,
                phone=phone,
                status="new",
                source=source,
                created_at=now,
                updated_at=now,
            )
            session.add(lead)
            log.info("New lead created: name=%s phone=%s course=%s", full_name, phone, course)

        session.commit()
        session.refresh(lead)
        # CRM adapter hook (currently a no-op stub, see crm.py)
        push_lead(lead)
        return lead


def upsert_manual_lead(
    full_name: str,
    phone: str,
    course: str | None = None,
    source: str = "boshqa",
    assigned_to: int | None = None,
) -> Lead:
    """Lead added manually from the admin panel.

    Duplicate rule (spec): same phone -> update the existing lead and mark
    it as a repeat request instead of creating a second row.
    """
    now = utcnow()
    with SessionLocal() as session:
        lead = session.query(Lead).filter(Lead.phone == phone).first()
        if lead is not None:
            lead.full_name = full_name or lead.full_name
            if course:
                lead.course = course
            lead.source = source
            if assigned_to:
                lead.assigned_to = assigned_to
            lead.updated_at = now
            lead.repeat_count = (lead.repeat_count or 0) + 1
            lead.followup_sent_at = None
            if lead.status == "rejected":
                lead.status = "new"
                lead.reject_reason = None
            log.info(
                "Manual lead updated (repeat #%s): id=%s phone=%s",
                lead.repeat_count, lead.id, phone,
            )
        else:
            lead = Lead(
                # Manual leads have no Telegram account — unique negative id.
                telegram_user_id=0,  # assigned below
                full_name=full_name,
                phone=phone,
                course=course,
                status="new",
                source=source,
                assigned_to=assigned_to or None,
                created_at=now,
                updated_at=now,
            )
            session.add(lead)
            session.flush()
            lead.telegram_user_id = -lead.id  # manual ids: negative, always unique
            log.info("Manual lead created: name=%s phone=%s source=%s", full_name, phone, source)
        session.commit()
        session.refresh(lead)
        push_lead(lead)
        return lead


# ---------------------------------------------------------------------------
# Employees and lead notes
# ---------------------------------------------------------------------------

def get_employees(enabled_only: bool = False) -> list[Employee]:
    with SessionLocal() as session:
        query = session.query(Employee).order_by(Employee.full_name, Employee.id)
        if enabled_only:
            query = query.filter(Employee.is_enabled.is_(True))
        return query.all()


def get_lead_notes(lead_id: int, limit: int = 50) -> list[LeadNote]:
    """A lead's comment history, oldest first."""
    with SessionLocal() as session:
        return (
            session.query(LeadNote)
            .filter(LeadNote.lead_id == lead_id)
            .order_by(LeadNote.id.desc())
            .limit(limit)
            .all()
        )


def add_lead_note(lead_id: int, text: str, author: str | None) -> LeadNote:
    """Append one entry to a lead's comment history."""
    with SessionLocal() as session:
        note = LeadNote(lead_id=lead_id, text=text[:2000], author=author or None)
        session.add(note)
        session.commit()
        session.refresh(note)
    return note


# ---------------------------------------------------------------------------
# Students and enrollment (lead -> student)
# ---------------------------------------------------------------------------

def get_students_filtered(q: str = "", group_id: int = 0, status: str = "") -> list[Student]:
    with SessionLocal() as session:
        query = session.query(Student)
        if q:
            like = f"%{q.strip()}%"
            query = query.filter((Student.full_name.ilike(like)) | (Student.phone.ilike(like)))
        if group_id:
            query = query.filter(Student.group_id == group_id)
        if status:
            query = query.filter(Student.status == status)
        return query.order_by(Student.updated_at.desc()).limit(500).all()


def group_fill_status(group_id: int) -> str | None:
    """Auto group status: seats filled -> "toldi"; manually closed stays "tugagan"."""
    with SessionLocal() as session:
        group = session.get(Group, group_id)
        if group is None:
            return None
        if group.status == "tugagan":
            return "tugagan"
        capacity = group.capacity or 0
        if capacity > 0:
            active = (
                session.query(Student)
                .filter(Student.group_id == group_id, Student.status == "faol")
                .count()
            )
            return "toldi" if active >= capacity else "ochiq"
        return "ochiq"


def refresh_group_status(group_id: int) -> None:
    """Recompute and persist a group's auto fill status."""
    status = group_fill_status(group_id)
    if status is None:
        return
    with SessionLocal() as session:
        group = session.get(Group, group_id)
        if group and group.status != status:
            group.status = status
            session.commit()


def enroll_lead(lead_id: int, group_id: int | None = None) -> tuple[Student, bool]:
    """Create (or update) the student profile when a lead is enrolled.

    If the chosen group is full, the student goes to the waiting list
    (status "navbatda") instead of taking a seat. Returns (student, waiting).
    """
    now = utcnow()
    today = datetime.now(TZ).date()
    waiting = False
    with SessionLocal() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            raise ValueError("lead not found")

        if group_id:
            group = session.get(Group, group_id)
            if group is None:
                group_id = None

        student = session.query(Student).filter(Student.lead_id == lead_id).first()
        if student is None:
            # Also dedup by phone: the same person is one student row.
            student = session.query(Student).filter(Student.phone == lead.phone).first()
        if student is not None:
            student.full_name = lead.full_name or student.full_name
            student.phone = lead.phone or student.phone
            student.username = lead.username or student.username
            student.source = lead.source or student.source
            student.group_id = group_id or student.group_id
            student.lead_id = lead_id
            student.updated_at = now
        else:
            student = Student(
                full_name=lead.full_name,
                phone=lead.phone,
                username=lead.username,
                source=lead.source,
                group_id=group_id or None,
                status="faol",
                lead_id=lead_id,
                enrolled_at=today,
                created_at=now,
                updated_at=now,
            )
            session.add(student)

        # Full group -> waiting list instead of taking a seat.
        if student.group_id and student.status in ("faol", "navbatda"):
            group = session.get(Group, student.group_id)
            capacity = (group.capacity or 0) if group else 0
            active = (
                session.query(Student)
                .filter(
                    Student.group_id == student.group_id,
                    Student.status == "faol",
                    Student.id != student.id,
                )
                .count()
            )
            if capacity > 0 and active >= capacity:
                student.status = "navbatda"
                waiting = True
            else:
                student.status = "faol"

        lead.group_id = student.group_id
        session.commit()
        session.refresh(student)

        if student.group_id:
            refresh_group_status(student.group_id)
        log.info(
            "Lead %s enrolled -> student %s (group=%s, waiting=%s)",
            lead_id, student.id, student.group_id, waiting,
        )
    return student, waiting


def set_student_status(student_id: int, status: str) -> None:
    """Change a student's status and refresh the group fill state."""
    with SessionLocal() as session:
        student = session.get(Student, student_id)
        if student is None:
            return
        student.status = status
        student.updated_at = utcnow()
        session.commit()
        if student.group_id:
            refresh_group_status(student.group_id)


def get_student_notes(student_id: int, limit: int = 50) -> list[StudentNote]:
    with SessionLocal() as session:
        return (
            session.query(StudentNote)
            .filter(StudentNote.student_id == student_id)
            .order_by(StudentNote.id.desc())
            .limit(limit)
            .all()
        )


def add_student_note(student_id: int, text: str, author: str | None) -> StudentNote:
    with SessionLocal() as session:
        note = StudentNote(student_id=student_id, text=text[:2000], author=author or None)
        session.add(note)
        session.commit()
        session.refresh(note)
    return note


# ---------------------------------------------------------------------------
# Money (whole so'm only — no floats) and debts
# ---------------------------------------------------------------------------

PAYMENT_METHODS = {
    "naqd": "Naqd",
    "karta": "Karta",
    "click": "Click",
    "payme": "Payme",
    "boshqa": "Boshqa",
}


def _duration_months(duration_text: str | None) -> int | None:
    """Parse "4 oy" / "2 yil" into months; None when unknown."""
    if not duration_text:
        return None
    match = re.search(r"(\d+)\s*(oy|yil|month)", duration_text.lower())
    if match is None:
        return None
    months = int(match.group(1))
    if "yil" in match.group(2):
        months *= 12
    return months


def monthly_price(student: Student, course: Course | None) -> int:
    """Monthly price in so'm after the student's individual discount."""
    if course is None or not course.price_sum:
        return 0
    price = course.price_sum
    if student.discount_type == "percent" and student.discount_value:
        price -= price * student.discount_value // 100
    elif student.discount_type == "sum" and student.discount_value:
        price -= student.discount_value
    return max(price, 0)


def student_periods(student: Student, course: Course | None) -> list[dict]:
    """Month-by-month payment plan: expected / paid / status per period.

    Periods run from the enrollment month; a course duration ("4 oy") caps
    them when it can be parsed, otherwise all months up to today count.
    """
    price = monthly_price(student, course)
    if student.enrolled_at is None or price <= 0:
        return []
    start = student.enrolled_at
    today = datetime.now(TZ).date()
    months_cap = _duration_months(course.duration if course else None) or 36
    periods: list[dict] = []
    year, month = start.year, start.month
    with SessionLocal() as session:
        rows = (
            session.query(Payment.period, func.sum(Payment.amount))
            .filter(Payment.student_id == student.id)
            .group_by(Payment.period)
            .all()
        )
    paid_by_period = {period: int(total or 0) for period, total in rows if period}
    for _ in range(min(months_cap, 36)):
        period = f"{year:04d}-{month:02d}"
        if (year, month) > (today.year, today.month):
            break
        paid = paid_by_period.get(period, 0)
        if paid >= price:
            status = "tolangan"
        elif paid > 0:
            status = "qisman"
        else:
            status = "qarz"
        periods.append(
            {"period": period, "expected": price, "paid": paid, "status": status}
        )
        month += 1
        if month > 12:
            month = 1
            year += 1
    return periods


def student_debt(student: Student, course: Course | None) -> dict:
    """Total debt and how many days late the oldest unpaid period is."""
    price = monthly_price(student, course)
    if price <= 0:
        return {"debt": 0, "days_late": 0, "paid_total": 0, "total_expected": 0}
    periods = student_periods(student, course)
    today = datetime.now(TZ).date()
    debt = sum(p["expected"] - p["paid"] for p in periods if p["expected"] > p["paid"])
    days_late = 0
    for p in periods:
        if p["status"] != "qarz":
            continue
        year, month = int(p["period"][:4]), int(p["period"][5:7])
        if month == 12:
            month_end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(year, month + 1, 1) - timedelta(days=1)
        late = (today - month_end).days
        days_late = max(days_late, late)
    paid_total = sum(p["paid"] for p in periods)
    return {
        "debt": max(debt, 0),
        "days_late": days_late,
        "paid_total": paid_total,
        "total_expected": sum(p["expected"] for p in periods),
    }


def get_debtors() -> list[dict]:
    """All active students with a debt, sorted by debt (largest first)."""
    result: list[dict] = []
    with SessionLocal() as session:
        students = (
            session.query(Student)
            .filter(Student.status.in_(["faol", "navbatda"]))
            .all()
        )
        courses = {c.id: c for c in session.query(Course).all()}
        groups = {g.id: g for g in session.query(Group).all()}
    for student in students:
        course = courses.get(
            groups[student.group_id].course_id if student.group_id in groups else None
        )
        info = student_debt(student, course)
        if info["debt"] <= 0:
            continue
        result.append(
            {
                "student": student,
                "group": groups.get(student.group_id or 0),
                "debt": info["debt"],
                "days_late": info["days_late"],
                "paid_total": info["paid_total"],
            }
        )
    result.sort(key=lambda r: r["debt"], reverse=True)
    return result


def add_payment(
    student_id: int,
    amount: int,
    paid_date: date | None,
    method: str | None,
    period: str | None,
    note: str | None,
    created_by: str | None,
) -> Payment:
    with SessionLocal() as session:
        payment = Payment(
            student_id=student_id,
            amount=int(amount),
            paid_date=paid_date,
            method=method if method in PAYMENT_METHODS else None,
            period=period or None,
            note=(note or "").strip()[:200] or None,
            created_by=created_by or None,
        )
        session.add(payment)
        session.commit()
        session.refresh(payment)
    log.info("Payment added: student=%s amount=%s period=%s", student_id, amount, period)
    return payment


# ---------------------------------------------------------------------------
# "Bugun" dashboard data
# ---------------------------------------------------------------------------

def today_overview() -> dict:
    """The four 'Bugun' blocks: calls, trials, group starts, debtors."""
    today = datetime.now(TZ).date()
    tomorrow = today + timedelta(days=1)
    with SessionLocal() as session:
        call_leads = (
            session.query(Lead)
            .filter(
                Lead.follow_up_at.isnot(None),
                Lead.follow_up_at <= today,
                Lead.status.in_(["new", "contacted", "trial"]),
            )
            .order_by(Lead.follow_up_at)
            .all()
        )
        trial_leads = (
            session.query(Lead)
            .filter(Lead.trial_date.in_([today, tomorrow]), Lead.status == "trial")
            .order_by(Lead.trial_date)
            .all()
        )
        starting_groups = (
            session.query(Group)
            .filter(Group.starts_on == today, Group.is_enabled.is_(True))
            .all()
        )
    debtor_count = len(get_debtors())
    return {
        "call_leads": call_leads,
        "trial_leads": trial_leads,
        "starting_groups": starting_groups,
        "debtor_count": debtor_count,
        "today": today,
    }
