from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="GROUP_INFO_",
        extra="ignore",
    )

    database_url: str = "sqlite:///var/group_info.db"
    timezone: str = "Asia/Shanghai"
    output_dir: Path = Path("var/output")
    lark_cli: str = "lark-cli"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
