from __future__ import annotations

from datetime import datetime
from typing import Protocol

from group_info_system.domain.messages import CollectionBatch, SyncCursor


class Collector(Protocol):
    provider: str

    def fetch(
        self,
        *,
        chat_id: str,
        since: datetime,
        until: datetime,
        cursor: SyncCursor | None = None,
    ) -> CollectionBatch: ...
