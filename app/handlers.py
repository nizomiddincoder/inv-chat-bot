"""Handles every Telegram update: business messages, private messages, callbacks.

Main conversation flow (state stored in DB per chat_id):
  step 0 -> welcome + course buttons
  step 1 -> course chosen, waiting for the user's name
  step 2 -> name saved, waiting for the phone number -> lead created

Owner-only admin commands (/lidlar, /kurslar, /yordam) are gated by
OWNER_CHAT_ID and work both in the business chat and in the bot's own chat.
The OWNER_CHAT_ID restriction applies ONLY to these admin commands — the
normal lead flow is never blocked or changed by it.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import func

from . import claude_client
from . import telegram_client as tg
from .database import SessionLocal
from .models import BusinessConnectionRecord, Course, Lead, Teacher, utcnow
from .services import (
    TZ,
    add_history,
    clear_history,
    get_enabled_courses,
    get_history,
    get_owner_chat_id,
    get_owner_notify_chat_id,
    get_state,
    is_work_hours,
    match_course,
    normalize_phone,
    render,
    set_state,
    source_label,
    upsert_lead,
    validate_name,
)

log = logging.getLogger("bot.handlers")

# Words that restart the flow from any step
RESET_COMMANDS = ("/start", "bekor qilish")

# Commands only the owner (OWNER_CHAT_ID) can use
ADMIN_COMMANDS = ("/lidlar", "/statistika", "/kurslar", "/yordam", "/admin")

ADMIN_HELP = (
    "🛠 Admin buyruqlari:\n"
    "/lidlar — lidlar statistikasi va oxirgi lidlar\n"
    "/kurslar — kurslar ro'yxati\n"
    "/yordam — shu yordam xabari\n\n"
    "Bu buyruqlar faqat OWNER_CHAT_ID egasiga ishlaydi."
)

STATUS_LABELS_UZ = {
    "new": "Yangi",
    "contacted": "Aloqaga chiqildi",
    "trial": "Sinov darsi",
    "enrolled": "O'qishga yozildi",
    "rejected": "Rad etildi",
}


def handle_update(update: dict) -> None:
    """Dispatch one raw Telegram update to the right handler."""
    if "business_connection" in update:
        _handle_business_connection(update["business_connection"])
    elif "business_message" in update:
        _handle_business_message(update["business_message"])
    elif "edited_business_message" in update:
        _handle_edited_business_message(update["edited_business_message"])
    elif "message" in update:
        _handle_private_message(update["message"])
    elif "callback_query" in update:
        _handle_callback_query(update["callback_query"])
    else:
        log.debug("Ignoring unsupported update keys: %s", list(update.keys()))


# ---------------------------------------------------------------------------
# business_connection: the bot learns which business account it is attached to
# ---------------------------------------------------------------------------

def _handle_business_connection(bc: dict) -> None:
    connection_id = bc.get("id")
    if not connection_id:
        return
    with SessionLocal() as session:
        record = session.get(BusinessConnectionRecord, connection_id)
        if record is None:
            record = BusinessConnectionRecord(connection_id=connection_id)
            session.add(record)
        record.owner_user_id = bc.get("user_chat_id", 0)
        record.is_enabled = bool(bc.get("is_enabled", True))
        record.updated_at = utcnow()
        session.commit()
    log.info(
        "Business connection updated: id=%s owner=%s enabled=%s",
        connection_id,
        bc.get("user_chat_id"),
        bc.get("is_enabled"),
    )


# ---------------------------------------------------------------------------
# Incoming messages
# ---------------------------------------------------------------------------

def _handle_business_message(message: dict) -> None:
    """A message written to the owner's account, forwarded by Telegram Business."""
    connection_id = message.get("business_connection_id")
    chat = message.get("chat", {})

    # Only private chats, never groups/channels
    if chat.get("type") != "private":
        log.info("Ignoring business message from non-private chat type=%s", chat.get("type"))
        return

    user = message.get("from") or {}
    if user.get("is_bot"):
        return

    text = message.get("text")

    # Never run the lead flow for the business owner's own messages.
    # Exception: admin commands, gated by OWNER_CHAT_ID (see below).
    if connection_id:
        with SessionLocal() as session:
            record = session.get(BusinessConnectionRecord, connection_id)
        owner_id = record.owner_user_id if record else None
        if owner_id and user.get("id") == owner_id:
            if text:
                _maybe_handle_admin_command(chat["id"], text, connection_id)
            else:
                log.debug("Ignoring owner's own non-text message")
            return
    else:
        log.warning("business_message without connection id — cannot verify owner")

    if not text:
        tg.send_message(
            chat.get("id"),
            "Iltimos, matn ko'rinishida yozing 🙏",
            business_connection_id=connection_id,
        )
        return

    # Claude answers business messages (knowledge from knowledge/ files).
    # Without an API key the built-in button flow keeps working.
    if claude_client.is_enabled():
        _process_with_claude(chat["id"], user, text, connection_id)
    else:
        _process_flow(chat["id"], user, text, connection_id)


def _handle_edited_business_message(message: dict) -> None:
    """The user edited one of their messages.

    Only one case is acted on: while the bot waits for the phone number
    (step 2), editing the name fixes the stored name. Other edits are logged
    and ignored so they can never re-trigger the flow or create duplicates.
    """
    connection_id = message.get("business_connection_id")
    chat = message.get("chat", {})
    if chat.get("type") != "private":
        return
    user = message.get("from") or {}
    if user.get("is_bot"):
        return

    # Owner edits are never part of the lead flow.
    if connection_id:
        with SessionLocal() as session:
            record = session.get(BusinessConnectionRecord, connection_id)
        owner_id = record.owner_user_id if record else None
        if owner_id and user.get("id") == owner_id:
            return

    text = (message.get("text") or "").strip()
    if not text:
        return

    chat_id = chat["id"]
    state = get_state(chat_id)
    if state["step"] == 2:
        name = validate_name(text)
        if name and name != state["temp_name"]:
            set_state(chat_id, step=2, course=state["course"], temp_name=name)
            tg.send_message(
                chat_id,
                render("name_edited"),
                business_connection_id=connection_id,
            )
            log.info("Edited name accepted for chat %s -> %s", chat_id, name)
        return
    log.info(
        "edited_business_message from chat %s ignored (step=%s)", chat_id, state["step"]
    )


def _handle_private_message(message: dict) -> None:
    """A message sent directly to the bot (not through the business account)."""
    chat = message.get("chat", {})
    if chat.get("type") != "private":
        # Spec: ignore normal messages from groups and channels.
        return
    user = message.get("from") or {}
    if user.get("is_bot"):
        return
    text = message.get("text")
    if not text:
        tg.send_message(chat.get("id"), "Iltimos, matn ko'rinishida yozing 🙏")
        return
    # Admin commands (OWNER_CHAT_ID only) are intercepted before the flow.
    if _maybe_handle_admin_command(chat["id"], text, business_connection_id=None):
        return
    _process_flow(chat["id"], user, text, business_connection_id=None)


# ---------------------------------------------------------------------------
# The conversation flow itself
# ---------------------------------------------------------------------------

def _is_reset_command(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered.startswith("/start") or any(cmd in lowered for cmd in RESET_COMMANDS[1:])


def _process_flow(chat_id: int, user: dict, text: str, business_connection_id: str | None) -> None:
    text = text.strip()
    state = get_state(chat_id)

    # "/start" or "bekor qilish" restarts the flow at any step.
    if _is_reset_command(text):
        set_state(chat_id, step=0, course=None, temp_name=None)
        _send_welcome(chat_id, user, business_connection_id)
        return

    if state["step"] == 1:
        # Waiting for the name. But if the user typed another course name,
        # treat it as a course change instead.
        course = match_course(text, get_enabled_courses())
        if course is not None:
            set_state(chat_id, step=1, course=course.name, temp_name=None)
            tg.send_message(
                chat_id,
                render("after_course", first_name=user.get("first_name") or "do'st", course=course.name),
                business_connection_id=business_connection_id,
            )
            return
        name = validate_name(text)
        if name is None:
            tg.send_message(
                chat_id,
                render("name_invalid"),
                business_connection_id=business_connection_id,
            )
            return
        set_state(chat_id, step=2, course=state["course"], temp_name=name)
        tg.send_message(
            chat_id,
            render("after_name"),
            business_connection_id=business_connection_id,
        )
        return

    if state["step"] == 2:
        # Waiting for the phone number.
        phone = normalize_phone(text)
        if phone is None:
            tg.send_message(
                chat_id,
                render("after_phone"),
                business_connection_id=business_connection_id,
            )
            return
        lead = upsert_lead(
            telegram_user_id=user["id"],
            username=user.get("username"),
            first_name=user.get("first_name"),
            course=state["course"],
            full_name=state["temp_name"] or (user.get("first_name") or "Noma'lum"),
            phone=phone,
        )
        set_state(chat_id, step=0, course=None, temp_name=None)
        tg.send_message(
            chat_id,
            render("finish", first_name=user.get("first_name") or "do'st"),
            business_connection_id=business_connection_id,
        )
        _notify_owner_new_lead(lead)
        return

    # step 0 (idle): welcome, or treat the text as a course selection.
    course = match_course(text, get_enabled_courses())
    if course is not None:
        set_state(chat_id, step=1, course=course.name, temp_name=None)
        tg.send_message(
            chat_id,
            render("after_course", first_name=user.get("first_name") or "do'st", course=course.name),
            business_connection_id=business_connection_id,
        )
    else:
        _send_welcome(chat_id, user, business_connection_id)


# ---------------------------------------------------------------------------
# Claude-powered answers (business messages)
# ---------------------------------------------------------------------------

# How many recent messages of the chat are sent to Claude as context.
CLAUDE_HISTORY_MESSAGES = 16


def _process_with_claude(
    chat_id: int, user: dict, text: str, business_connection_id: str | None
) -> None:
    """Answer a business message with Claude (CLAUDE_MODEL, knowledge/ files).

    When the API call fails, the built-in flow takes over so the visitor
    always gets a reply. A [LEAD]...[/LEAD] block in Claude's answer creates
    a lead and notifies the owner (OWNER_NOTIFY_CHAT_ID).
    """
    text = text.strip()

    # "/start" / "bekor qilish" still restart everything (and wipe history).
    if _is_reset_command(text):
        clear_history(chat_id)
        set_state(chat_id, step=0, course=None, temp_name=None)
        _send_welcome(chat_id, user, business_connection_id)
        return

    add_history(chat_id, "user", text)
    reply = claude_client.generate_reply(
        get_history(chat_id, CLAUDE_HISTORY_MESSAGES)
    )
    if reply is None:
        log.warning(
            "Claude unavailable — answering chat %s with the built-in flow", chat_id
        )
        _process_flow(chat_id, user, text, business_connection_id)
        return

    visible, lead_name, lead_phone, lead_course = claude_client.extract_lead(reply)
    if not visible:
        # Claude sent only the lead marker — show the standard finish text.
        visible = render("finish", first_name=user.get("first_name") or "do'st")
    add_history(chat_id, "assistant", visible)
    tg.send_message(chat_id, visible, business_connection_id=business_connection_id)

    if lead_phone:
        _save_claude_lead(user, lead_name, lead_phone, lead_course)


def _save_claude_lead(
    user: dict, name: str | None, phone: str | None, course: str | None
) -> None:
    """Create/update the lead Claude collected and notify the owner."""
    normalized = normalize_phone(phone or "")
    if normalized is None:
        log.warning("Claude sent an invalid phone %r — lead not saved", phone)
        return
    full_name = (
        validate_name(name or "")
        or (name or "").strip()[:50]
        or (user.get("first_name") or "Noma'lum")
    )
    matched = match_course(course or "", get_enabled_courses())
    course_name = matched.name if matched else ((course or "").strip()[:120] or None)
    lead = upsert_lead(
        telegram_user_id=user["id"],
        username=user.get("username"),
        first_name=user.get("first_name"),
        course=course_name,
        full_name=full_name,
        phone=normalized,
        source="telegram_bot",
    )
    _notify_owner_new_lead(lead)


# ---------------------------------------------------------------------------
# Owner-only admin commands (gated strictly by OWNER_CHAT_ID)
# ---------------------------------------------------------------------------

def _extract_command(text: str) -> str:
    """/lidlar@MyBot -> /lidlar"""
    return text.strip().split()[0].split("@")[0].lower()


def _maybe_handle_admin_command(chat_id: int, text: str, business_connection_id: str | None) -> bool:
    """Run an admin command if it is one. Returns True when the text was a command.

    The OWNER_CHAT_ID restriction applies ONLY here — the normal lead flow is
    never affected by it.
    """
    command = _extract_command(text)
    if command not in ADMIN_COMMANDS:
        return False

    owner_chat_id = get_owner_chat_id()
    if owner_chat_id is None:
        log.warning(
            "Admin command %s received but OWNER_CHAT_ID is not set (env or admin "
            "Settings page) — ignoring", command,
        )
        return True
    if chat_id != owner_chat_id:
        log.info("Admin command %s rejected: chat %s is not OWNER_CHAT_ID", command, chat_id)
        tg.send_message(
            chat_id,
            "Bu buyruq faqat admin uchun 🙅",
            business_connection_id=business_connection_id,
        )
        return True

    if command in ("/lidlar", "/statistika"):
        reply = _build_leads_summary()
    elif command == "/kurslar":
        reply = _build_courses_summary()
    else:  # /yordam, /admin
        reply = ADMIN_HELP

    if chat_id == owner_chat_id:
        # The owner's own chat is NOT a business peer — sending there with a
        # business_connection_id fails with BUSINESS_PEER_INVALID. The owner
        # started the bot, so a direct message always works.
        business_connection_id = None

    tg.send_message(chat_id, reply, business_connection_id=business_connection_id)
    log.info("Admin command %s executed for chat %s", command, chat_id)
    return True


def _build_leads_summary() -> str:
    """Statistics for the /lidlar command."""
    today_start_utc = (
        datetime.now(TZ)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )
    with SessionLocal() as session:
        total = session.query(Lead).count()
        today = session.query(Lead).filter(Lead.created_at >= today_start_utc).count()
        status_rows = session.query(Lead.status, func.count(Lead.id)).group_by(Lead.status).all()
        last_leads = session.query(Lead).order_by(Lead.created_at.desc()).limit(5).all()

    counts = {status: 0 for status in STATUS_LABELS_UZ}
    for status, count in status_rows:
        counts[status] = count

    lines = [
        "📊 Lidlar statistikasi",
        f"Jami: {total} | Bugun: {today}",
        " | ".join(f"{label}: {counts.get(status, 0)}" for status, label in STATUS_LABELS_UZ.items()),
    ]
    if last_leads:
        lines.append("\nOxirgi lidlar:")
        for index, lead in enumerate(last_leads, start=1):
            created = lead.created_at.astimezone(TZ).strftime("%d.%m %H:%M")
            lines.append(f"{index}. {lead.full_name or '—'} — {lead.phone or '—'} — {lead.course or '—'} ({created})")
    else:
        lines.append("\nHozircha lid yo'q.")
    return "\n".join(lines)


def _build_courses_summary() -> str:
    """Course list for the /kurslar command (with price, duration, teacher)."""
    with SessionLocal() as session:
        courses = session.query(Course).order_by(Course.sort_order, Course.id).all()
        teachers = {t.id: t.full_name for t in session.query(Teacher).all()}
    lines = ["📚 Kurslar:"]
    for course in courses:
        mark = "✅" if course.is_enabled else "⛔"
        details = []
        if course.price:
            details.append(f"narx: {course.price}")
        if course.duration:
            details.append(f"muddat: {course.duration}")
        if course.teacher_id and course.teacher_id in teachers:
            details.append(f"o'qituvchi: {teachers[course.teacher_id]}")
        line = f"{mark} {course.name}"
        if details:
            line += " — " + ", ".join(details)
        lines.append(line)
    if not courses:
        lines.append("Hozircha kurs yo'q. Admin panelda qo'shing.")
    return "\n".join(lines)


def _send_welcome(chat_id: int, user: dict, business_connection_id: str | None) -> None:
    """Step 0 reply: greeting, out-of-hours line if needed, course buttons."""
    first_name = user.get("first_name") or "do'st"
    text = render("welcome", first_name=first_name)

    if not is_work_hours():
        text += "\n\n" + render("out_of_hours")

    # Inline keyboard: one button per enabled course, straight from the DB.
    keyboard = {
        "inline_keyboard": [
            [{"text": course.name, "callback_data": f"course:{course.id}"}]
            for course in get_enabled_courses()
        ]
    }
    tg.send_message(chat_id, text, business_connection_id=business_connection_id, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Inline button presses
# ---------------------------------------------------------------------------

def _handle_callback_query(callback: dict) -> None:
    data = callback.get("data", "")
    if not data.startswith("course:"):
        tg.answer_callback(callback.get("id", ""))
        return

    user = callback.get("from") or {}
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    # Button presses on business messages carry the connection id too.
    connection_id = callback.get("business_connection_id") or message.get("business_connection_id")

    if not chat_id:
        tg.answer_callback(callback.get("id", ""))
        return

    try:
        course_id = int(data.split(":", 1)[1])
    except ValueError:
        tg.answer_callback(callback.get("id", ""))
        return

    courses = {c.id: c for c in get_enabled_courses()}
    course = courses.get(course_id)
    tg.answer_callback(callback.get("id", ""), "Tanlandi ✅")
    if course is None:
        # Course was disabled or deleted — show the welcome screen again.
        _send_welcome(chat_id, user, connection_id)
        return

    set_state(chat_id, step=1, course=course.name, temp_name=None)
    tg.send_message(
        chat_id,
        render("after_course", first_name=user.get("first_name") or "do'st", course=course.name),
        business_connection_id=connection_id,
    )


# ---------------------------------------------------------------------------
# Owner notifications
# ---------------------------------------------------------------------------

def _notify_owner_new_lead(lead) -> None:
    """Tell the owner in a private Telegram message that a new lead arrived.

    Goes to OWNER_NOTIFY_CHAT_ID when set, otherwise the regular owner chat
    id. Note: the owner must have pressed Start in the bot chat at least once,
    otherwise Telegram does not allow the bot to message them.
    """
    owner_chat_id = get_owner_notify_chat_id()
    if owner_chat_id is None:
        log.warning(
            "Neither OWNER_NOTIFY_CHAT_ID nor OWNER_CHAT_ID is set (env or admin "
            "Settings page) — cannot notify about lead %s",
            lead.id,
        )
        return

    username = f"@{lead.username}" if lead.username else "yo'q"
    created = lead.created_at.astimezone(TZ).strftime("%d.%m.%Y %H:%M")
    repeat = (lead.repeat_count or 0) > 0
    text = (
        "🆕 Takroriy murojaat!\n" if repeat else "🆕 Yangi lid!\n"
    )
    text += (
        f"Ism: {lead.full_name}\n"
        f"Telefon: {lead.phone}\n"
        f"Kurs: {lead.course or '—'}\n"
        f"Manba: {source_label(lead.source)}\n"
        f"Username: {username}\n"
        f"Vaqt: {created}"
    )
    tg.send_message(owner_chat_id, text)
