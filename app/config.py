"""Configuration centralisée, lue depuis les variables d'environnement / .env."""
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_port: int = Field(12080, ge=12000, le=13000)
    data_dir: str = "./data"
    log_level: str = "INFO"

    jwt_secret: str
    jwt_expire_minutes: int = 720
    admin_username: str = "admin"
    admin_password_hash: str = ""
    admin_password: str = ""
    cookie_secure: bool = False

    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    twitch_bot_token: str = ""
    twitch_refresh_token: str = ""
    twitch_channel: str = ""

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        (p / "logs").mkdir(exist_ok=True)
        return p


settings = Settings()
if len(settings.jwt_secret) < 32 or settings.jwt_secret.startswith("CHANGE_ME"):
    raise RuntimeError("JWT_SECRET doit être défini (>= 32 caractères aléatoires).")
