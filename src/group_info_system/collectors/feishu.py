from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from group_info_system.domain.messages import CollectionBatch, RawMessage, SyncCursor

MEDIA_RULES = (
    (re.compile(r"\[Image\s*:\s*[^\]]*\]", re.IGNORECASE), "[图片]"),
    (re.compile(r"\[Sticker(?:\s*:\s*[^\]]*)?\]", re.IGNORECASE), "[表情]"),
    (re.compile(r"\[Audio(?:\s*:\s*[^\]]*)?\]", re.IGNORECASE), "[语音]"),
    (re.compile(r"\[Video(?:\s*:\s*[^\]]*)?\]", re.IGNORECASE), "[视频]"),
    (re.compile(r"\[File(?:\s*:\s*[^\]]*)?\]", re.IGNORECASE), "[文件]"),
    (re.compile(r"<folder\b[^>]*/?>", re.IGNORECASE), "[文件夹]"),
)

ERROR_HINTS = {
    230002: "机器人不在群里；请确认使用用户身份且当前用户是群成员",
    230027: "缺少群消息读取权限；请重新执行 lark-cli 用户身份授权",
    231203: "群类型不支持，可能启用了禁止复制或导出",
    231204: "当前应用配置不支持以用户身份读取该群",
    230073: "当前用户无权查看该时间段的话题历史",
}


class FeishuCollectionError(RuntimeError):
    pass


def parse_time(value: Any, local_timezone: ZoneInfo) -> datetime:
    if isinstance(value, (int, float)) or str(value).isdigit():
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=UTC)
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        raise ValueError("missing create_time")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_timezone)
    return parsed.astimezone(UTC)


def clean_content(value: Any) -> str:
    content = str(value or "")
    for pattern, replacement in MEDIA_RULES:
        content = pattern.sub(replacement, content)
    return re.sub(r"\s+", " ", content).strip()


def sender_fields(sender: Any) -> tuple[str, str]:
    if not isinstance(sender, dict):
        return "", str(sender or "未知成员")
    names = sender.get("sender_i18n_names") or sender.get("i18n_names") or {}
    localized = ""
    if isinstance(names, dict):
        localized = names.get("zh_cn") or names.get("zh-CN") or names.get("en_us") or ""
    sender_id = str(sender.get("id") or "")
    name = re.sub(r"[:：]+", "·", str(localized or sender.get("name") or "").strip())
    if not name:
        name = f"成员{sender_id[-6:]}" if sender_id else "未知成员"
    return sender_id, name


class FeishuCollector:
    """Fetch Feishu messages and emit provider-neutral RawMessage objects only.

    Incrementality is watermark-based. The application persists the returned cursor.
    Each run intentionally re-fetches a short overlap window; the append-only store
    removes identical payloads and retains edited payloads as new versions.
    """

    provider = "feishu"

    def __init__(
        self,
        *,
        cli_path: str = "lark-cli",
        chat_name: str | None = None,
        local_timezone: str = "Asia/Shanghai",
        overlap: timedelta = timedelta(minutes=5),
        timeout_seconds: int = 120,
    ):
        self.cli_path = cli_path
        self.chat_name = chat_name
        self.local_timezone = ZoneInfo(local_timezone)
        self.overlap = overlap
        self.timeout_seconds = timeout_seconds

    def _resolve_cli(self) -> str:
        explicit = Path(self.cli_path).expanduser()
        if explicit.parent != Path(".") or "/" in self.cli_path:
            if not explicit.is_file():
                raise FeishuCollectionError(f"指定的 lark-cli 不存在：{explicit}")
            return str(explicit)
        found = shutil.which(self.cli_path)
        if not found:
            raise FeishuCollectionError("未找到 lark-cli；请先安装并完成用户身份授权")
        return found

    def _run(self, args: list[str]) -> dict[str, Any]:
        env = dict(os.environ)
        env.update(
            {
                "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
                "LARK_CLI_NO_PROXY_WARN": "1",
            }
        )
        try:
            proc = subprocess.run(
                [self._resolve_cli(), *args],
                capture_output=True,
                text=True,
                env=env,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FeishuCollectionError("lark-cli 取数超时") from exc
        if not proc.stdout.strip():
            raise FeishuCollectionError("lark-cli 未返回 JSON；授权可能已过期")
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise FeishuCollectionError("lark-cli 输出不是合法 JSON") from exc
        if envelope.get("ok") is not True:
            error = envelope.get("error") or {}
            code = error.get("code")
            hint = ERROR_HINTS.get(code, "请使用 lark-cli doctor 检查授权和环境")
            raise FeishuCollectionError(f"飞书接口错误 {code}：{hint}")
        return envelope.get("data") or {}

    def _to_raw(self, item: dict[str, Any], chat_id: str) -> RawMessage:
        platform_id = str(item.get("message_id") or item.get("id") or "").strip()
        if not platform_id:
            raise FeishuCollectionError("飞书响应包含缺少 message_id 的记录，已停止避免静默丢失")
        try:
            sent_at = parse_time(item.get("create_time"), self.local_timezone)
        except (TypeError, ValueError) as exc:
            raise FeishuCollectionError(
                f"飞书消息 {platform_id} 缺少可识别的 create_time，已停止避免静默丢失"
            ) from exc
        sender_id, sender_name = sender_fields(item.get("sender"))
        is_system = item.get("msg_type") == "system"
        is_deleted = bool(item.get("deleted"))
        content = clean_content(item.get("content"))
        if is_system and not content:
            content = "[系统事件]"
        if is_deleted and not content:
            content = "[消息已删除]"
        return RawMessage(
            provider=self.provider,
            chat_id=chat_id,
            chat_name=self.chat_name,
            platform_message_id=platform_id,
            sent_at=sent_at,
            sender_id=sender_id,
            sender_name="系统" if is_system else sender_name,
            message_type=str(item.get("msg_type") or "unknown"),
            content=content,
            is_system=is_system,
            is_deleted=is_deleted,
            raw_payload=item,
        )

    def fetch(
        self,
        *,
        chat_id: str,
        since: datetime,
        until: datetime,
        cursor: SyncCursor | None = None,
    ) -> CollectionBatch:
        if since.tzinfo is None or until.tzinfo is None:
            raise ValueError("since and until must be timezone-aware")
        if since >= until:
            raise ValueError("since must be earlier than until")
        if cursor and (cursor.provider != self.provider or cursor.chat_id != chat_id):
            raise ValueError("cursor does not belong to this provider and chat")
        effective_since = max(since, cursor.watermark - self.overlap) if cursor else since
        # Current lark-cli accepts ISO 8601. Pass the exact incremental interval and
        # still enforce it locally to guard against provider boundary differences.
        request_start = effective_since.isoformat()
        request_end = until.isoformat()
        data = self._run(
            [
                "im",
                "+chat-messages-list",
                "--as",
                "user",
                "--chat-id",
                chat_id,
                "--start",
                request_start,
                "--end",
                request_end,
                "--order",
                "asc",
                "--page-all",
                "--page-size",
                "50",
                "--page-limit",
                "1000",
                "--no-reactions",
                "--format",
                "json",
            ]
        )
        if data.get("has_more"):
            raise FeishuCollectionError("飞书分页结果未取完，已停止避免把部分数据当成完整同步")
        source_messages = data.get("messages") or []
        if not isinstance(source_messages, list):
            raise FeishuCollectionError("飞书响应 messages 字段不是数组")
        converted = [self._to_raw(item, chat_id) for item in source_messages]
        messages = sorted(
            (item for item in converted if effective_since <= item.sent_at < until),
            key=lambda row: (row.sent_at, row.platform_message_id),
        )
        baseline = cursor.watermark if cursor else until
        watermark = max((baseline, *(row.sent_at for row in messages)))
        return CollectionBatch(
            provider=self.provider,
            chat_id=chat_id,
            messages=tuple(messages),
            next_cursor=SyncCursor(provider=self.provider, chat_id=chat_id, watermark=watermark),
            mode="incremental" if cursor else "full",
        )
