"""Application configuration read from environment variables."""
import os


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
        # Railway may give "postgres://..." — SQLAlchemy needs "postgresql://..."
        db_url = os.getenv("DATABASE_URL", "").strip()
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        # Local fallback so the owner can test on their own computer.
        self.DATABASE_URL: str = db_url or "sqlite:///./inventor_local.db"

        # Working hours (Asia/Tashkent). Can be changed later in the admin panel.
        self.WORK_START: str = os.getenv("WORK_START", "09:00").strip()
        self.WORK_END: str = os.getenv("WORK_END", "20:00").strip()

        # Telegram chat id of the owner (to receive new-lead notifications).
        owner = os.getenv("OWNER_CHAT_ID", "").strip()
        try:
            self.OWNER_CHAT_ID: int | None = int(owner) if owner else None
        except ValueError:
            self.OWNER_CHAT_ID = None

    def check(self) -> list[str]:
        """Return a list of missing-but-important variable names."""
        missing = []
        for name in ("BOT_TOKEN", "PUBLIC_URL", "WEBHOOK_SECRET", "ADMIN_PASSWORD"):
            if not getattr(self, name):
                missing.append(name)
        return missing


settings = Settings()
