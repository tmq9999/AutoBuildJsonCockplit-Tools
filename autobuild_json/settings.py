from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTOBUILD_", env_file=".env", extra="ignore")
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)
    data_dir: Path = Path("data")
    checklive_path: Path = Field(default=Path(".deps/Check-Account-ChatGPT"), validation_alias="CHECKLIVE_PATH")
    app_version: str = "26.820.60940"
    installation_id: str = ""
    admin_token: SecretStr = SecretStr("")
