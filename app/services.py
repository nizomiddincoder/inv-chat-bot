"""Business logic shared by the bot handlers and the admin panel."""
import logging
import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .config import settings
from .crm import push_lead
from .database import SessionLocal
from .models import BotMessage, ChatState, Course, Lead, Setting, utcnow

log = logging.getLogger("bot.services")

TZ = ZoneInfo("Asia/Tashkent")

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
    source: str = "telegram",
) -> Lead:
    """Create a lead, or update the existing one for the same user/phone.

    Deduplication rule: same telegram_user_id OR same phone -> update, no duplicate.
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
            # Allow the 24h follow-up timer again for a re-opened lead.
            lead.followup_sent_at = None
            if lead.status == "rejected":
                lead.status = "new"
            log.info("Lead updated: id=%s name=%s phone=%s", lead.id, full_name, phone)
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
