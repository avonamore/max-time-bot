from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: SecretStr = SecretStr("postgresql://maxbot:maxbot@localhost:5432/maxbot")
    max_bot_token: SecretStr = SecretStr("")
    max_api_url: str = "https://platform-api2.max.ru"
    max_webhook_secret: SecretStr = SecretStr("")
    max_webhook_url: str = ""
    initial_admin_max_id: int = Field(default=0, ge=0)
    org_timezone: str = "Asia/Omsk"
    # Upload origins are returned by MAX. Configure exact hosts after inspecting your bot's response.
    max_upload_allowed_hosts: str = ""
    max_ca_bundle: str = ""

    @field_validator("org_timezone")
    @classmethod
    def valid_timezone(cls, value):
        ZoneInfo(value)
        return value

    @field_validator("max_api_url")
    @classmethod
    def valid_api(cls, value):
        if value not in ("https://platform-api2.max.ru", "https://platform-api.max.ru"):
            raise ValueError("Разрешены только официальные HTTPS-серверы MAX")
        return value

    def validate_webhook(self):
        import re

        secret = self.max_webhook_secret.get_secret_value()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret):
            raise ValueError("MAX_WEBHOOK_SECRET: используйте 32–256 символов A-Z, a-z, 0-9, _ или -")


@lru_cache
def settings():
    return Settings()
