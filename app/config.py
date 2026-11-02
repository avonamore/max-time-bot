from functools import lru_cache
from zoneinfo import ZoneInfo
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore', hide_input_in_errors=True)
    database_url: str = 'postgresql://maxbot:maxbot@localhost:5432/maxbot'
    max_bot_token: str = ''
    max_api_base: str = 'https://platform-api2.max.ru'
    max_webhook_secret: str = Field(min_length=16, default='')
    admin_max_id: int = Field(gt=0)
    org_timezone: str = 'Asia/Omsk'
    public_base_url: str = ''
    max_ca_bundle: str | None = None
    max_upload_hosts: str = 'fu.oneme.ru,omu.okcdn.ru,omub.okcdn.ru'

    @field_validator('max_api_base')
    @classmethod
    def official_api(cls, value):
        if value.rstrip('/') != 'https://platform-api2.max.ru':
            raise ValueError('Разрешен только официальный HTTPS API MAX platform-api2.max.ru')
        return value.rstrip('/')

    @field_validator('max_ca_bundle', mode='before')
    @classmethod
    def empty_ca(cls, value):
        return value or None

    @field_validator('org_timezone')
    @classmethod
    def valid_timezone(cls, value):
        ZoneInfo(value)
        return value

    @field_validator('max_webhook_secret')
    @classmethod
    def valid_secret(cls, value):
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', value):
            raise ValueError('Секрет: 16–256 латинских букв, цифр, _ или -')
        return value


@lru_cache
def settings():
    return Settings()
