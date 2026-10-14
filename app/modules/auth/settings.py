"""Sign-in configuration, from the environment (.env). Nothing here is shown to users."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Where people reach the app, e.g. https://jobs.example.com -- used for links in
    # emails and as the Google/Facebook redirect address. Must match what you
    # register with them.
    public_url: str = Field(default="http://127.0.0.1:8000", alias="PUBLIC_URL")
    # Comma-separated: these accounts are admins (platform settings, costs, health).
    admin_emails: str = Field(default="", alias="ADMIN_EMAILS")

    google_client_id: str | None = Field(default=None, alias="GOOGLE_CLIENT_ID")
    google_client_secret: str | None = Field(default=None, alias="GOOGLE_CLIENT_SECRET")
    facebook_client_id: str | None = Field(default=None, alias="FACEBOOK_CLIENT_ID")
    facebook_client_secret: str | None = Field(default=None, alias="FACEBOOK_CLIENT_SECRET")
    facebook_graph_version: str = Field(default="v23.0", alias="FACEBOOK_GRAPH_VERSION")

    # (SMTP settings: app/integrations/email.py, EmailSettings.)

    # Signs the six-digit email codes (HMAC; app/modules/auth/otp.py). Unset: a
    # random key per process, so a restart invalidates codes in flight.
    app_secret_key: str | None = Field(default=None, alias="APP_SECRET_KEY")

    # Cookies only over HTTPS. Defaults to on when PUBLIC_URL is https.
    cookie_secure: bool | None = Field(default=None, alias="COOKIE_SECURE")

    @property
    def admins(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def secure_cookies(self) -> bool:
        return self.cookie_secure if self.cookie_secure is not None else self.public_url.startswith("https://")

    @property
    def base_url(self) -> str:
        return self.public_url.rstrip("/")

    def provider_enabled(self, provider: str) -> bool:
        if provider == "google":
            return bool(self.google_client_id and self.google_client_secret)
        if provider == "facebook":
            return bool(self.facebook_client_id and self.facebook_client_secret)
        return False
