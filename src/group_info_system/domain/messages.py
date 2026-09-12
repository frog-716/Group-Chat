from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RawMessage(BaseModel):
    """Provider-neutral message emitted by collectors.

    Collectors may clean enough data to populate common fields, but must retain the
    exact provider payload in ``raw_payload``. Downstream analysis never accepts a
    provider-specific response directly.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    chat_id: str
    chat_name: str | None = None
    platform_message_id: str
    sent_at: datetime
    sender_id: str = ""
    sender_name: str = "未知成员"
    message_type: str = "text"
    content: str = ""
    is_system: bool = False
    is_deleted: bool = False
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sent_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("sent_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("provider", "chat_id", "platform_message_id")
    @classmethod
    def identity_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message identity fields cannot be empty")
        return value.strip()


class SyncCursor(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    chat_id: str
    watermark: datetime

    @field_validator("watermark")
    @classmethod
    def normalize_watermark(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("watermark must be timezone-aware")
        return value.astimezone(UTC)


class CollectionBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    chat_id: str
    messages: tuple[RawMessage, ...]
    next_cursor: SyncCursor | None = None
    mode: Literal["full", "incremental"] = "full"
