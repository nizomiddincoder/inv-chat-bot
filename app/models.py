"""SQLAlchemy ORM models."""
from datetime import date, datetime, timezone

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    """Current UTC time with timezone info."""
    return datetime.now(timezone.utc)


class Teacher(Base):
    """A teacher who can be attached to courses and groups."""

    __tablename__ = "teachers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Short professional bio (experience, certificates) — also shown to Claude.
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Course(Base):
    """A course the bot offers. Buttons in the bot are built from this table."""

    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # CRM details (also fed to Claude as knowledge):
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[str | None] = mapped_column(String(60), nullable=True)
    duration: Mapped[str | None] = mapped_column(String(60), nullable=True)
    teacher_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Group(Base):
    """A study group: one course, one teacher, a schedule and a start date."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    course_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    teacher_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Free-text schedule like "Du-Chor-Ju 16:00" and start date "1-dekabr".
    schedule: Mapped[str | None] = mapped_column(String(200), nullable=True)
    start_date: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Seats available; NULL/0 = no limit set yet.
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ochiq | toldi | tugagan ("toldi" is set automatically when seats fill up)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Employee(Base):
    """A staff member. Roles prepare per-role visibility for later stages."""

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    login: Mapped[str] = mapped_column(String(60), unique=True)
    # admin | operator | teacher | accountant (only "admin" is active now)
    role: Mapped[str] = mapped_column(String(20), default="operator")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Lead(Base):
    """A person who asked about a course."""

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Telegram user id — unique, used to avoid duplicate leads. 0 for leads
    # added manually from the admin panel.
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    course: Mapped[str | None] = mapped_column(String(120), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # new | contacted | enrolled | rejected
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    # Where the lead came from: telegram_bot | instagram | meta | tavsiya | yurish | boshqa
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Employee responsible for this lead (employees.id).
    assigned_to: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Next planned call date; overdue ones are highlighted red.
    follow_up_at: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    # Filled when the lead is rejected: narx | boshqa_markaz | javob_yoq | vaqt_yoq | boshqa
    reject_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Trial lesson: the lead can be moved to status "trial" with a date.
    trial_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Group chosen when the lead is enrolled (groups.id).
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # How many times this person applied again (phone dedup marker).
    repeat_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Set after the bot sends the one-time 24h follow-up to the owner.
    followup_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LeadNote(Base):
    """One entry in a lead's comment history (date, time and author kept)."""

    __tablename__ = "lead_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(Integer, index=True)
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Student(Base):
    """An enrolled student — created automatically from a lead on enrollment."""

    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Where this student originally came from (copied from the lead).
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # faol | tugatgan | toxtatgan | navbatda (waiting list of a full group)
    status: Mapped[str] = mapped_column(String(20), default="faol", index=True)
    # The lead this student was created from — keeps the history link.
    lead_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Payments are planned for the next stage; free-form note for now.
    payment_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enrolled_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StudentNote(Base):
    """One entry in a student's comment history."""

    __tablename__ = "student_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(Integer, index=True)
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BotMessage(Base):
    """Editable bot message templates (admin panel -> Messages page)."""

    __tablename__ = "bot_messages"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    """Simple key/value settings (work hours, owner chat id)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


class ChatState(Base):
    """Conversation state per chat (which step of the lead flow we are on)."""

    __tablename__ = "chat_states"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # 0 = idle (show welcome) | 1 = waiting for name | 2 = waiting for phone
    step: Mapped[int] = mapped_column(Integer, default=0)
    course: Mapped[str | None] = mapped_column(String(120), nullable=True)
    temp_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BusinessConnectionRecord(Base):
    """Stored Telegram Business connection (which bot is connected to which account)."""

    __tablename__ = "business_connections"

    connection_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    # The business account owner's Telegram user id (their "user_chat_id").
    owner_user_id: Mapped[int] = mapped_column(BigInteger)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProcessedUpdate(Base):
    """update_id deduplication: every Telegram update handled once.

    Telegram retries webhook deliveries on network hiccups — a row here means
    "already processed, skip the retry".
    """

    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatHistory(Base):
    """Recent conversation per chat — context for Claude-generated replies."""

    __tablename__ = "chat_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # "user" or "assistant"
    role: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AiSetting(Base):
    """Editable AI settings (admin panel -> AI Javoblar page).

    Long texts (Claude style prompt, extra knowledge) that don't fit the
    200-char Setting.value column.
    """

    __tablename__ = "ai_settings"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
