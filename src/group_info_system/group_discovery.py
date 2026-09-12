from __future__ import annotations

from dataclasses import dataclass

from group_info_system.collectors.feishu import FeishuCollector


@dataclass(frozen=True)
class DiscoveredChat:
    provider: str
    external_id: str
    display_name: str

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "external_id": self.external_id,
            "display_name": self.display_name,
        }


def discover_feishu_chats(*, cli_path: str) -> tuple[DiscoveredChat, ...]:
    """Discover chats through the existing user-authenticated lark-cli wrapper."""

    collector = FeishuCollector(cli_path=cli_path)
    data = collector._run(
        ["im", "+chat-list", "--as", "user", "--page-all", "--format", "json"]
    )
    chats = data.get("chats") or []
    if not isinstance(chats, list):
        raise TypeError("飞书响应 chats 字段不是数组")

    discovered: list[DiscoveredChat] = []
    for index, item in enumerate(chats):
        if not isinstance(item, dict):
            raise TypeError(f"飞书响应 chats[{index}] 不是对象")
        external_id = str(item.get("chat_id") or item.get("external_id") or "").strip()
        if not external_id:
            raise RuntimeError(f"飞书响应 chats[{index}] 缺少 chat_id")
        display_name = str(item.get("name") or item.get("display_name") or "").strip()
        discovered.append(
            DiscoveredChat(
                provider="feishu",
                external_id=external_id,
                display_name=display_name,
            )
        )
    return tuple(sorted(discovered, key=lambda chat: chat.external_id))
