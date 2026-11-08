"""Database engine, session factory and startup table creation + seeding."""
import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

log = logging.getLogger("db")

connect_args = {}
# SQLite is only used for local testing without PostgreSQL.
if settings.DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,  # avoids stale connections on Railway
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base class for all ORM models."""


# Default course list inserted the first time the app starts.
DEFAULT_COURSES = [
    "Kompyuter savodxonlik",
    "Buxgalteriya 0 dan",
    "Glavbuxgalteriya",
    "Trading",
    "Sun'iy intellekt",
]


def _seed_defaults() -> None:
    """Insert default courses, message templates and settings on first run.

    Existing rows are never overwritten — the admin panel owns them after that.
    """
    from .models import BotMessage, Course, MessageTemplate, Setting
    from .services import DEFAULT_MESSAGES, DEFAULT_TEMPLATES

    with SessionLocal() as session:
        if session.query(Course).count() == 0:
            for index, name in enumerate(DEFAULT_COURSES, start=1):
                session.add(Course(name=name, sort_order=index, is_enabled=True))
            log.info("Seeded default courses: %s", ", ".join(DEFAULT_COURSES))

        for key, text in DEFAULT_MESSAGES.items():
            if session.get(BotMessage, key) is None:
                session.add(BotMessage(key=key, text=text))

        for key, (title, text) in DEFAULT_TEMPLATES.items():
            if session.get(MessageTemplate, key) is None:
                session.add(MessageTemplate(key=key, title=title, text=text))

        if session.get(Setting, "work_start") is None:
            session.add(Setting(key="work_start", value=settings.WORK_START))
        if session.get(Setting, "work_end") is None:
            session.add(Setting(key="work_end", value=settings.WORK_END))

        session.commit()


def _migrate_schema() -> None:
    """Add columns to existing tables that create_all won't touch.

    create_all only creates MISSING tables — columns added to existing
    tables later must be added manually, or existing deployments (Railway
    PostgreSQL) would crash on the new fields. Simple ALTER TABLE ADD COLUMN
    works on both PostgreSQL and SQLite.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    additions = {
        # NOTE: one entry per table — a duplicate dict key silently replaces
        # the earlier columns list (that is how courses lost its first block).
        "courses": {
            "description": "TEXT",
            "price": "VARCHAR(60)",
            "duration": "VARCHAR(60)",
            "teacher_id": "INTEGER",
            "price_sum": "INTEGER",
        },
        "leads": {
            "assigned_to": "INTEGER",
            "follow_up_at": "DATE",
            "reject_reason": "VARCHAR(60)",
            "repeat_count": "INTEGER",
            "trial_date": "DATE",
            "group_id": "INTEGER",
            "is_test": "BOOLEAN",
        },
        "groups": {
            "capacity": "INTEGER",
            "status": "VARCHAR(20)",
            "starts_on": "DATE",
        },
        "students": {
            "discount_type": "VARCHAR(10)",
            "discount_value": "INTEGER",
            "is_test": "BOOLEAN",
        },
    }
    with engine.begin() as conn:
        for table, columns in additions.items():
            if table not in table_names:
                continue  # first start — create_all builds the full table anyway
            existing = {col["name"] for col in inspector.get_columns(table)}
            for column, col_type in columns.items():
                if column not in existing:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                    )
                    log.info("Migration: added %s.%s column", table, column)


def _migrate_data() -> None:
    """One-time data migrations. Never deletes old data.

    - the old free-text lead "notes" field moves into the lead_notes history
      (with its last-update timestamp) and the field is cleared;
    - leads without a source get "telegram_bot" (they all arrived via the bot).
    """
    from .models import Lead, LeadNote, utcnow

    with SessionLocal() as session:
        moved = 0
        for lead in session.query(Lead).filter(
            Lead.notes.isnot(None), Lead.notes != ""
        ).all():
            session.add(
                LeadNote(
                    lead_id=lead.id,
                    text=lead.notes,
                    author="Eski izoh",
                    created_at=lead.updated_at or utcnow(),
                )
            )
            lead.notes = None
            moved += 1

        source_set = (
            session.query(Lead)
            .filter(Lead.source.is_(None))
            .update({Lead.source: "telegram_bot"}, synchronize_session=False)
        )
        if moved or source_set:
            session.commit()
            log.info(
                "Data migration: %s note(s) moved to history, %s lead(s) got a source",
                moved,
                source_set,
            )


def init_db() -> None:
    """Create all tables (if missing), migrate and seed defaults. Runs on startup."""
    from . import models  # noqa: F401  (models must be imported to be registered)

    log.info(
        "Using DB driver: %s (dialect: %s)",
        engine.dialect.driver,
        engine.dialect.name,
    )
    try:
        Base.metadata.create_all(engine)
        _migrate_schema()
        _migrate_data()
    except Exception:
        # Log the full error so the owner can see the exact problem in Railway logs.
        log.exception(
            "FAILED to create database tables. Check DATABASE_URL and that the "
            "PostgreSQL plugin is added in Railway. The app will keep running, "
            "but the bot cannot work without the database."
        )
        raise
    _seed_defaults()
    log.info("Database ready: %s", engine.url.render_as_string(hide_password=True))
