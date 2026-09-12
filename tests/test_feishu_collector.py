from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from group_info_system.application.collection import (
    WINDOW_ALREADY_SYNCED,
    collect_into_store,
    collect_into_store_with_outcome,
)
from group_info_system.collectors.feishu import (
    FeishuCollectionError,
    FeishuCollector,
    clean_content,
    sender_fields,
)
from group_info_system.db.models import CollectionRunRow, MessageVersionRow
from group_info_system.db.repositories import MessageRepository
from group_info_system.db.session import session_factory
from group_info_system.domain.messages import RawMessage, SyncCursor


class StubFeishuCollector(FeishuCollector):
    def __init__(self, payload: dict):
        super().__init__()
        self.payload = payload
        self.request: list[str] = []

    def _run(self, args: list[str]) -> dict:
        self.request = args
        return self.payload


def test_normalizes_media_and_prefers_localized_sender() -> None:
    assert clean_content("[Image: img_1] <folder key=x/>") == "[图片] [文件夹]"
    assert sender_fields(
        {"id": "ou_1", "name": "默认名", "sender_i18n_names": {"zh_cn": "群昵称"}}
    ) == ("ou_1", "群昵称")


def test_outputs_raw_messages_and_preserves_system_and_deleted() -> None:
    collector = StubFeishuCollector(
        {
            "messages": [
                {
                    "message_id": "om_1",
                    "create_time": "2026-09-10 09:00:00",
                    "msg_type": "text",
                    "sender": {"id": "ou_1", "name": "成员"},
                    "content": "正文",
                },
                {
                    "message_id": "om_2",
                    "create_time": "2026-09-10 09:01:00",
                    "msg_type": "system",
                    "content": "加入群聊",
                },
                {
                    "message_id": "om_3",
                    "create_time": "2026-09-10 09:02:00",
                    "msg_type": "text",
                    "deleted": True,
                    "content": "",
                },
            ]
        }
    )
    start = datetime(2026, 9, 10, tzinfo=UTC)
    end = start + timedelta(days=1)
    batch = collector.fetch(chat_id="oc_demo", since=start, until=end)
    assert all(isinstance(row, RawMessage) for row in batch.messages)
    assert len(batch.messages) == 3
    assert batch.messages[1].is_system is True
    assert batch.messages[2].is_deleted is True
    assert "--page-all" in collector.request


def test_incremental_cursor_uses_overlap_and_returns_new_watermark() -> None:
    collector = StubFeishuCollector(
        {
            "messages": [
                {
                    "message_id": "old",
                    "create_time": "2026-09-10 09:54:00+00:00",
                    "msg_type": "text",
                    "content": "重叠范围之前",
                },
                {
                    "message_id": "overlap",
                    "create_time": "2026-09-10 09:58:00+00:00",
                    "msg_type": "text",
                    "content": "重叠范围内",
                },
                {
                    "message_id": "new",
                    "create_time": "2026-09-10 10:03:00+00:00",
                    "msg_type": "text",
                    "content": "新消息",
                },
            ]
        }
    )
    cursor = SyncCursor(
        provider="feishu",
        chat_id="oc_demo",
        watermark=datetime(2026, 9, 10, 10, 0, tzinfo=UTC),
    )
    batch = collector.fetch(
        chat_id="oc_demo",
        since=datetime(2026, 9, 1, tzinfo=UTC),
        until=datetime(2026, 9, 10, 11, 0, tzinfo=UTC),
        cursor=cursor,
    )
    assert [row.platform_message_id for row in batch.messages] == ["overlap", "new"]
    assert batch.mode == "incremental"
    assert batch.next_cursor and batch.next_cursor.watermark == datetime(
        2026, 9, 10, 10, 3, tzinfo=UTC
    )
    assert collector.request[collector.request.index("--start") + 1] == "2026-09-10T09:55:00+00:00"
    assert collector.request[collector.request.index("--end") + 1] == "2026-09-10T11:00:00+00:00"


def test_malformed_provider_message_is_not_silently_dropped() -> None:
    collector = StubFeishuCollector(
        {"messages": [{"create_time": "2026-09-10 09:00:00", "content": "缺 ID"}]}
    )
    with pytest.raises(FeishuCollectionError, match="message_id"):
        collector.fetch(
            chat_id="oc_demo",
            since=datetime(2026, 9, 10, tzinfo=UTC),
            until=datetime(2026, 9, 11, tzinfo=UTC),
        )


def test_incremental_cursor_never_moves_backwards() -> None:
    collector = StubFeishuCollector(
        {
            "messages": [
                {
                    "message_id": "overlap",
                    "create_time": "2026-09-10 09:58:00+00:00",
                    "msg_type": "text",
                    "content": "只有重叠消息",
                }
            ]
        }
    )
    cursor = SyncCursor(
        provider="feishu",
        chat_id="oc_demo",
        watermark=datetime(2026, 9, 10, 10, 0, tzinfo=UTC),
    )
    batch = collector.fetch(
        chat_id="oc_demo",
        since=datetime(2026, 9, 1, tzinfo=UTC),
        until=datetime(2026, 9, 10, 11, 0, tzinfo=UTC),
        cursor=cursor,
    )
    assert batch.next_cursor and batch.next_cursor.watermark == cursor.watermark


@pytest.mark.parametrize(
    "watermark",
    (
        datetime(2026, 9, 10, 11, 0, tzinfo=UTC),
        datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    ),
    ids=("equal-to-window-end", "later-than-window-end"),
)
def test_synced_window_is_successful_noop_without_provider_call_or_cursor_change(
    migrated_database: str,
    watermark: datetime,
) -> None:
    start = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
    end = datetime(2026, 9, 10, 11, 0, tzinfo=UTC)
    collector = StubFeishuCollector({"messages": []})
    sessions = session_factory(migrated_database)
    with sessions() as session:
        MessageRepository(session).save_cursor(
            SyncCursor(provider="feishu", chat_id="oc_demo", watermark=watermark)
        )
        session.commit()

    with sessions() as session:
        outcome = collect_into_store_with_outcome(
            session=session,
            collector=collector,
            chat_id="oc_demo",
            since=start,
            until=end,
        )

    assert collector.request == []
    assert outcome.batch.messages == ()
    assert outcome.batch.next_cursor and outcome.batch.next_cursor.watermark == watermark
    assert outcome.inserted_count == 0
    assert outcome.reason_code == WINDOW_ALREADY_SYNCED

    with sessions() as session:
        run = session.scalar(select(CollectionRunRow))
        assert run is not None
        assert run.status == "succeeded"
        assert run.fetched_count == 0
        assert run.inserted_count == 0
        assert run.error_code == WINDOW_ALREADY_SYNCED
        stored_cursor = MessageRepository(session).get_cursor("feishu", "oc_demo")
        assert stored_cursor and stored_cursor.watermark == watermark
        assert session.scalar(select(func.count()).select_from(MessageVersionRow)) == 0


def test_normal_incremental_collection_still_calls_provider_and_advances_cursor(
    migrated_database: str,
) -> None:
    start = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
    end = datetime(2026, 9, 10, 11, 0, tzinfo=UTC)
    initial_watermark = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
    collector = StubFeishuCollector(
        {
            "messages": [
                {
                    "message_id": "new",
                    "create_time": "2026-09-10T10:03:00+00:00",
                    "msg_type": "text",
                    "sender": {"id": "ou_1", "name": "成员"},
                    "content": "新的有效消息",
                }
            ]
        }
    )
    sessions = session_factory(migrated_database)
    with sessions() as session:
        MessageRepository(session).save_cursor(
            SyncCursor(
                provider="feishu",
                chat_id="oc_demo",
                watermark=initial_watermark,
            )
        )
        session.commit()

    with sessions() as session:
        outcome = collect_into_store_with_outcome(
            session=session,
            collector=collector,
            chat_id="oc_demo",
            since=start,
            until=end,
        )

    assert collector.request
    assert outcome.reason_code is None
    assert len(outcome.batch.messages) == 1
    assert outcome.inserted_count == 1
    with sessions() as session:
        run = session.scalar(select(CollectionRunRow))
        assert run is not None and run.status == "succeeded"
        assert run.error_code is None
        stored_cursor = MessageRepository(session).get_cursor("feishu", "oc_demo")
        assert stored_cursor and stored_cursor.watermark == datetime(
            2026, 9, 10, 10, 3, tzinfo=UTC
        )


def test_partial_pagination_is_rejected() -> None:
    collector = StubFeishuCollector({"messages": [], "has_more": True})
    with pytest.raises(FeishuCollectionError, match="未取完"):
        collector.fetch(
            chat_id="oc_demo",
            since=datetime(2026, 9, 10, tzinfo=UTC),
            until=datetime(2026, 9, 11, tzinfo=UTC),
        )


def test_collection_failure_is_recorded(migrated_database: str) -> None:
    collector = StubFeishuCollector(
        {"messages": [{"create_time": "2026-09-10 09:00:00", "content": "缺 ID"}]}
    )
    sessions = session_factory(migrated_database)
    with sessions() as session, pytest.raises(FeishuCollectionError):
        collect_into_store(
            session=session,
            collector=collector,
            chat_id="oc_demo",
            since=datetime(2026, 9, 10, tzinfo=UTC),
            until=datetime(2026, 9, 11, tzinfo=UTC),
        )
    with sessions() as session:
        run = session.scalar(select(CollectionRunRow))
        assert run and run.status == "failed"
        assert run.error_code == "FeishuCollectionError"
