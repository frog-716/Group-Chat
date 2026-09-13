from __future__ import annotations

import os
import re
import tempfile
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


class GroupRegistryWriteError(GroupRegistryError):
    error_code = "GROUPS_CONFIG_WRITE_FAILED"


class GroupStatusTransitionError(GroupRegistryError):
    error_code = "GROUP_STATUS_TRANSITION_INVALID"


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


def load_group_registry(path: Path, *, require_active: bool = True) -> GroupRegistry:
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

    if require_active and not any(group.status == "active" for group in groups):
        raise NoActiveGroupsError("至少需要一个 active 群")

    return GroupRegistry(
        version=SUPPORTED_VERSION,
        timezone=timezone,
        day_cutoff=day_cutoff,
        groups=tuple(groups),
        source=path,
    )


def write_group_registry_atomic(path: Path, registry: GroupRegistry) -> None:
    """Rewrite the Registry with an atomic same-directory replace."""

    serialized = yaml.safe_dump(
        registry.as_dict(),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise GroupRegistryWriteError(f"无法原子写入配置文件：{path}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def add_group(
    path: Path,
    *,
    provider: str,
    external_id: str,
    key: str,
    display_name: str,
    status: str = "paused",
) -> GroupEntry:
    registry = load_group_registry(path, require_active=False)
    provider = _require_string(provider, "provider")
    external_id = _require_string(external_id, "external_id")
    key = _require_string(key, "key")
    display_name = _require_string(display_name, "display_name")
    if provider not in SUPPORTED_PROVIDERS:
        raise GroupRegistryError(f"不支持的 provider：{provider}")
    if status not in SUPPORTED_STATUSES:
        raise GroupRegistryError(f"不支持的 status：{status}")
    if any(group.key == key for group in registry.groups):
        raise GroupRegistryError(f"key 重复：{key}")
    if any(
        group.provider == provider and group.external_id == external_id
        for group in registry.groups
    ):
        raise GroupRegistryError(f"provider+external_id 重复：{provider}+{external_id}")
    entry = GroupEntry(
        key=key,
        provider=provider,
        external_id=external_id,
        display_name=display_name,
        status=status,
    )
    updated = GroupRegistry(
        version=registry.version,
        timezone=registry.timezone,
        day_cutoff=registry.day_cutoff,
        groups=(*registry.groups, entry),
        source=registry.source,
    )
    # Validate the complete candidate before touching the original file.
    # Multiple active groups are valid; Execution Scope freezes the run set.
    write_group_registry_atomic(path, updated)
    return entry


def transition_group(path: Path, *, key: str, target_status: str) -> GroupEntry:
    registry = load_group_registry(path, require_active=False)
    try:
        current = next(group for group in registry.groups if group.key == key)
    except StopIteration as exc:
        raise GroupRegistryError(f"群不存在：{key}") from exc

    allowed = {
        "paused": {"active"},
        "active": {"paused"},
        "archived": {"active", "paused"},
    }
    if current.status not in allowed[target_status]:
        raise GroupStatusTransitionError(
            f"不允许状态转换：{current.status} -> {target_status}（{key}）"
        )
    updated_groups = tuple(
        GroupEntry(
            key=group.key,
            provider=group.provider,
            external_id=group.external_id,
            display_name=group.display_name,
            status=target_status if group.key == key else group.status,
        )
        for group in registry.groups
    )
    updated = GroupRegistry(
        version=registry.version,
        timezone=registry.timezone,
        day_cutoff=registry.day_cutoff,
        groups=updated_groups,
        source=registry.source,
    )
    write_group_registry_atomic(path, updated)
    return next(group for group in updated.groups if group.key == key)
