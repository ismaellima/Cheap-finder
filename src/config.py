from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    DATABASE_URL: str = "sqlite+aiosqlite:///./cheapfinder.db"

    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    ALERT_EMAIL_TO: str = ""

    DISCORD_WEBHOOK_URL: str = ""
    SLACK_WEBHOOK_URL: str = ""

    LOG_LEVEL: str = "INFO"
    PRICE_CHECK_HOUR: int = 6  # unused since price checks moved to rolling batches
    REQUEST_DELAY_SECONDS: int = 2

    # Price checks run as small rolling batches rather than one daily sweep.
    # A full sweep of the catalogue takes hours and never survived a free-tier
    # restart, so nothing past the first few thousand products was ever reached.
    # Defaults below cycle the whole catalogue roughly once a day while keeping
    # any single run well under its own interval.
    PRICE_CHECK_INTERVAL_MINUTES: int = 30
    PRICE_CHECK_BATCH_SIZE: int = 300
    SAVE_HTML_SNAPSHOTS: bool = False

    # Render deployment
    RENDER_EXTERNAL_URL: str = ""  # e.g. https://cheap-finder.onrender.com
    PORT: int = 8000

    # Dashboard auth — leave empty for open access (backwards compatible)
    DASHBOARD_PASSWORD: str = ""

    # Secret key for signing session cookies.
    # Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    # If not set, a random key is generated at startup (sessions won't survive restarts).
    SESSION_SECRET_KEY: str = ""


settings = Settings()
