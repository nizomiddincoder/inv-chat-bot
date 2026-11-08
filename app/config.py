"""Application configuration read from environment variables."""
import os


def normalize_db_url(url: str) -> str:
    """Convert Railway's DATABASE_URL to SQLAlchemy + psycopg 3 format.

    Railway gives "postgres://..." or "postgresql://..."; the engine must be
    told which driver to use, so both become "postgresql+psycopg://...".
    """
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Settings:
    """All settings come from environment variables (set in Railway).

    Defaults are safe for local testing only.
    """

    def __init__(self) -> None:
        # Telegram bot token from @BotFather
        self.BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()

        # Public HTTPS URL of the Railway app, e.g. https://xxx.up.railway.app
        self.PUBLIC_URL: str = os.getenv("PUBLIC_URL", "").strip().rstrip("/")

        # Random secret that Telegram sends back in the
        # X-Telegram-Bot-Api-Secret-Token header on every webhook call.
        self.WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "").strip()

        # Admin panel (Basic Auth) credentials
        self.ADMIN_USER: str = os.getenv("ADMIN_USER", "admin").strip()
        self.ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "").strip()

        # PostgreSQL connection string from Railway.
        # Railway may give "postgres://..." or "postgresql://..."; SQLAlchemy needs
        # an explicit driver, so we normalize to "postgresql+psycopg://" (psycopg 3).
        self.DATABASE_URL: str = normalize_db_url(
            os.getenv("DATABASE_URL", "").strip()
        ) or "sqlite:///./inventor_local.db"

        # Working hours (Asia/Tashkent). Can be changed later in the admin panel.
        self.WORK_START: str = os.getenv("WORK_START", "09:00").strip()
        self.WORK_END: str = os.getenv("WORK_END", "20:00").strip()

        # Telegram chat id of the owner (to receive new-lead notifications).
        owner = os.getenv("OWNER_CHAT_ID", "").strip()
        try:
            self.OWNER_CHAT_ID: int | None = int(owner) if owner else None
        except ValueError:
            self.OWNER_CHAT_ID = None

        # Separate chat id for notifications to the owner (new leads, follow-ups).
        # When not set, notifications fall back to OWNER_CHAT_ID / the admin panel
        # owner_chat_id setting.
        notify = os.getenv("OWNER_NOTIFY_CHAT_ID", "").strip()
        try:
            self.OWNER_NOTIFY_CHAT_ID: int | None = int(notify) if notify else None
        except ValueError:
            self.OWNER_NOTIFY_CHAT_ID = None

        # --- Claude (Anthropic) API: AI answers for business messages ---
        # ANTHROPIC_API_KEY or CLAUDE_API_KEY — either name works.
        self.ANTHROPIC_API_KEY: str = (
            os.getenv("ANTHROPIC_API_KEY", "").strip()
            or os.getenv("CLAUDE_API_KEY", "").strip()
        )
        # Which LLM adapter answers (see app/llm_adapter.py). "claude" today,
        # your own model later — prompts/tools/business logic never change.
        self.LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "claude").strip().lower()
        # Model id, e.g. "claude-sonnet-4-5" (docs.claude.com -> models).
        self.CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5").strip()
        try:
            self.CLAUDE_MAX_TOKENS: int = int(os.getenv("CLAUDE_MAX_TOKENS", "1024"))
        except ValueError:
            self.CLAUDE_MAX_TOKENS = 1024

    def check(self) -> list[str]:
        """Return a list of missing-but-important variable names."""
        missing = []
        for name in ("BOT_TOKEN", "PUBLIC_URL", "WEBHOOK_SECRET", "ADMIN_PASSWORD"):
            if not getattr(self, name):
                missing.append(name)
        return missing


settings = Settings()
