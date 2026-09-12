from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from group_info_system.domain.messages import CollectionBatch, RawMessage, SyncCursor


class SimulatedCollector:
    provider = "simulated"

    def __init__(self, fixture_path: Path, chat_name: str = "MVP 产品群（模拟）"):
        self.fixture_path = fixture_path
        self.chat_name = chat_name

    def fetch(
        self,
        *,
        chat_id: str,
        since: datetime,
        until: datetime,
        cursor: SyncCursor | None = None,
    ) -> CollectionBatch:
        payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        messages = []
        for item in payload["messages"]:
            raw = RawMessage(
                provider=self.provider,
                chat_id=chat_id,
                chat_name=self.chat_name,
                platform_message_id=item["message_id"],
                sent_at=datetime.fromisoformat(item["sent_at"]),
                sender_id=item["sender_id"],
                sender_name=item["sender_name"],
                message_type=item.get("message_type", "text"),
                content=item.get("content", ""),
                is_system=item.get("is_system", False),
                is_deleted=item.get("is_deleted", False),
                raw_payload=item,
            )
            if since <= raw.sent_at < until:
                messages.append(raw)
        messages.sort(key=lambda row: (row.sent_at, row.platform_message_id))
        next_cursor = (
            SyncCursor(provider=self.provider, chat_id=chat_id, watermark=messages[-1].sent_at)
            if messages
            else cursor
        )
        return CollectionBatch(
            provider=self.provider,
            chat_id=chat_id,
            messages=tuple(messages),
            next_cursor=next_cursor,
            mode="incremental" if cursor else "full",
        )
