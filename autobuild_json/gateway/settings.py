from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTOBUILD_GATEWAY_", extra="forbid")

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=8788, ge=1, le=65535)
    database_url: SecretStr | None = None
    master_key_file: Path | None = None
    request_timeout: int = Field(default=180, ge=1, le=600)

    @field_validator("database_url")
    @classmethod
    def postgres_url(cls, value):
        if value is not None:
            parsed = urlsplit(value.get_secret_value())
            if parsed.scheme != "postgresql+psycopg" or not parsed.hostname or not parsed.path.strip("/"):
                raise ValueError("A PostgreSQL psycopg URL is required")
        return value

    @model_validator(mode="after")
    def enabled_config(self):
        if self.enabled and (self.database_url is None or self.master_key_file is None):
            raise ValueError("Enabled gateway requires database and master-key configuration")
        return self
