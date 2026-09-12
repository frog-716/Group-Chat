from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

SUPPORTED_VERSION = "group-info/groups/v1"
SUPPORTED_PROVIDERS = frozenset({"feishu"})
SUPPORTED_STATUSES = frozenset({"active", "paused", "archived"})
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class GroupRegistryError(ValueError):
    """A stable, user-actionable Registry validation failure."""

    error_code = "GROUPS_CONFIG_INVALID"


class NoActiveGroupsError(GroupRegistryError):
    error_code = "GROUPS_NO_ACTIVE"


@dataclass(frozen=True)
class GroupEntry:
    key: str
    provider: str
    external_id: str
    display_name: str
    status: str

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "provider": self.provider,
            "external_id": self.external_id,
            "display_name": self.display_name,
            "status": self.status,
        }

    def as_dict_without_status(self) -> dict[str, str]:
        return {
            "key": self.key,
            "provider": self.provider,
            "external_id": self.external_id,
            "display_name": self.display_name,
        }


@dataclass(frozen=True)
class GroupRegistry:
    version: str
    timezone: str
    day_cutoff: str
    groups: tuple[GroupEntry, ...]
    source: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "defaults": {
                "timezone": self.timezone,
                "day_cutoff": self.day_cutoff,
            },
            "groups": [group.as_dict() for group in self.groups],
        }

    @property
    def active_groups(self) -> tuple[GroupEntry, ...]:
        return tuple(group for group in self.groups if group.status == "active")


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GroupRegistryError(f"{name} 必须是 mapping")
    return value


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GroupRegistryError(f"{name} 必须是非空字符串")
    return value.strip()


def _validate_timezone(value: Any, name: str) -> str:
    timezone = _require_string(value, name)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise GroupRegistryError(f"{name} 不是合法时区：{timezone}") from exc
    return timezone


def _validate_cutoff(value: Any, name: str) -> str:
    cutoff = _require_string(value, name)
    if _TIME_PATTERN.fullmatch(cutoff) is None:
        raise GroupRegistryError(f"{name} 格式必须为 HH:MM：{cutoff}")
    return cutoff


def load_group_registry(path: Path) -> GroupRegistry:
    """Load and validate the declarative Group Registry without touching the DB."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GroupRegistryError(f"配置文件不存在：{path}") from exc
    except OSError as exc:
        raise GroupRegistryError(f"无法读取配置文件：{path}") from exc
    except yaml.YAMLError as exc:
        raise GroupRegistryError(f"YAML 格式错误：{exc}") from exc

    root = _require_mapping(raw, "根节点")
    if root.get("version") != SUPPORTED_VERSION:
        raise GroupRegistryError(
            f"不支持的 version：{root.get('version')!r}，要求 {SUPPORTED_VERSION}"
        )

    defaults = _require_mapping(root.get("defaults"), "defaults")
    timezone = _validate_timezone(defaults.get("timezone"), "defaults.timezone")
    day_cutoff = _validate_cutoff(defaults.get("day_cutoff"), "defaults.day_cutoff")

    groups_raw = root.get("groups")
    if not isinstance(groups_raw, list):
        raise GroupRegistryError("groups 必须是 list")

    groups: list[GroupEntry] = []
    keys: set[str] = set()
    external_ids: set[str] = set()
    allowed_group_fields = {"key", "provider", "external_id", "display_name", "status"}
    for index, value in enumerate(groups_raw):
        item = _require_mapping(value, f"groups[{index}]")
        unknown = set(item) - allowed_group_fields
        if unknown:
            raise GroupRegistryError(
                f"groups[{index}] 包含未知字段：{', '.join(sorted(unknown))}"
            )
        key = _require_string(item.get("key"), f"groups[{index}].key")
        provider = _require_string(item.get("provider"), f"groups[{index}].provider")
        external_id = _require_string(
            item.get("external_id"), f"groups[{index}].external_id"
        )
        display_name = _require_string(
            item.get("display_name"), f"groups[{index}].display_name"
        )
        status = _require_string(item.get("status"), f"groups[{index}].status")

        if key in keys:
            raise GroupRegistryError(f"key 重复：{key}")
        if external_id in external_ids:
            raise GroupRegistryError(f"external_id 重复：{external_id}")
        if provider not in SUPPORTED_PROVIDERS:
            raise GroupRegistryError(f"不支持的 provider：{provider}")
        if status not in SUPPORTED_STATUSES:
            raise GroupRegistryError(f"不支持的 status：{status}")

        keys.add(key)
        external_ids.add(external_id)
        groups.append(
            GroupEntry(
                key=key,
                provider=provider,
                external_id=external_id,
                display_name=display_name,
                status=status,
            )
        )

    if not any(group.status == "active" for group in groups):
        raise NoActiveGroupsError("至少需要一个 active 群")

    return GroupRegistry(
        version=SUPPORTED_VERSION,
        timezone=timezone,
        day_cutoff=day_cutoff,
        groups=tuple(groups),
        source=path,
    )
