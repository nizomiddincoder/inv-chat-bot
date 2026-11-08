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
    from .models import BotMessage, Course, Setting
    from .services import DEFAULT_MESSAGES

    with SessionLocal() as session:
        if session.query(Course).count() == 0:
            for index, name in enumerate(DEFAULT_COURSES, start=1):
                session.add(Course(name=name, sort_order=index, is_enabled=True))
            log.info("Seeded default courses: %s", ", ".join(DEFAULT_COURSES))

        for key, text in DEFAULT_MESSAGES.items():
            if session.get(BotMessage, key) is None:
                session.add(BotMessage(key=key, text=text))

        if session.get(Setting, "work_start") is None:
            session.add(Setting(key="work_start", value=settings.WORK_START))
        if session.get(Setting, "work_end") is None:
            session.add(Setting(key="work_end", value=settings.WORK_END))

        session.commit()


def init_db() -> None:
    """Create all tables (if missing) and seed defaults. Runs on startup."""
    from . import models  # noqa: F401  (models must be imported to be registered)

    log.info(
        "Using DB driver: %s (dialect: %s)",
        engine.dialect.driver,
        engine.dialect.name,
    )
    try:
        Base.metadata.create_all(engine)
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
