"""SQLAlchemy ORM models."""
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    """Current UTC time with timezone info."""
    return datetime.now(timezone.utc)


class Course(Base):
    """A course the bot offers. Buttons in the bot are built from this table."""

    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Lead(Base):
    """A person who asked about a course."""

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Telegram user id — unique, used to avoid duplicate leads.
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    course: Mapped[str | None] = mapped_column(String(120), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # new | contacted | enrolled | rejected
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Set after the bot sends the one-time 24h follow-up to the owner.
    followup_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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
