from __future__ import annotations

import json
from pathlib import Path

import pytest

import group_info_system.cli as cli_module
from group_info_system.cli import main
from group_info_system.group_registry import load_group_registry

VALID_CONFIG = """
version: group-info/groups/v1
defaults:
  timezone: Asia/Shanghai
  day_cutoff: '06:00'
groups:
  - key: management-piglet
    provider: feishu
    external_id: oc_management
    display_name: 管理猪小群
    status: active
  - key: paused-group
    provider: feishu
    external_id: oc_paused
    display_name: 暂停群
    status: paused
  - key: archived-group
    provider: feishu
    external_id: oc_archived
    display_name: 归档群
    status: archived
"""


def write_config(tmp_path: Path, content: str = VALID_CONFIG) -> Path:
    path = tmp_path / "groups.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_registry_and_non_active_statuses(tmp_path: Path) -> None:
    registry = load_group_registry(write_config(tmp_path))
    assert len(registry.groups) == 3
    assert [group.key for group in registry.active_groups] == ["management-piglet"]


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("key: management-piglet", "key 重复"),
        ("external_id: oc_management", "external_id 重复"),
        ("provider: slack", "不支持的 provider"),
        ("timezone: Mars/Olympus", "不是合法时区"),
        ("day_cutoff: '25:00'", "格式必须为 HH:MM"),
    ],
)
def test_invalid_registry_fields(tmp_path: Path, replacement: str, message: str) -> None:
    content = VALID_CONFIG
    if replacement.startswith("key:"):
        content = content.replace("key: paused-group", replacement)
    elif replacement.startswith("external_id:"):
        content = content.replace("external_id: oc_paused", replacement)
    elif replacement.startswith("provider:"):
        content = content.replace("provider: feishu\n    external_id: oc_paused", f"{replacement}\n    external_id: oc_paused")
    elif replacement.startswith("timezone:"):
        content = content.replace("timezone: Asia/Shanghai", replacement)
    else:
        content = content.replace("day_cutoff: '06:00'", replacement)
    with pytest.raises(ValueError, match=message):
        load_group_registry(write_config(tmp_path, content))


def test_no_active_groups_fails(tmp_path: Path) -> None:
    content = VALID_CONFIG.replace("status: active", "status: paused", 1)
    with pytest.raises(ValueError, match="至少需要一个 active 群"):
        load_group_registry(write_config(tmp_path, content))


def test_unsupported_version_fails(tmp_path: Path) -> None:
    content = VALID_CONFIG.replace("group-info/groups/v1", "group-info/groups/v2", 1)
    with pytest.raises(ValueError, match="不支持的 version"):
        load_group_registry(write_config(tmp_path, content))


def test_malformed_yaml_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="YAML 格式错误"):
        load_group_registry(write_config(tmp_path, "version: ["))


def test_groups_validate_cli_emits_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write_config(tmp_path)
    assert main(["groups", "validate", "--config", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "succeeded"
    assert output["group_count"] == 3
    assert output["active_count"] == 1


def test_groups_list_cli_emits_all_groups(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write_config(tmp_path)
    assert main(["groups", "list", "--config", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert [group["status"] for group in output["groups"]] == [
        "active",
        "paused",
        "archived",
    ]


def test_groups_active_cli_returns_only_active_groups(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(tmp_path)
    assert main(["groups", "active", "--config", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "groups": [
            {
                "key": "management-piglet",
                "provider": "feishu",
                "external_id": "oc_management",
                "display_name": "管理猪小群",
            }
        ]
    }


def test_groups_active_cli_returns_stable_error_without_active(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    content = VALID_CONFIG.replace("status: active", "status: paused", 1)
    path = write_config(tmp_path, content)
    assert main(["groups", "active", "--config", str(path)]) == 1
    output = json.loads(capsys.readouterr().err)
    assert output["command"] == "groups active"
    assert output["error_code"] == "GROUPS_NO_ACTIVE"


def test_groups_cli_failure_is_stable_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write_config(tmp_path, VALID_CONFIG.replace("provider: feishu", "provider: slack", 1))
    assert main(["groups", "validate", "--config", str(path)]) == 1
    output = json.loads(capsys.readouterr().err)
    assert output == {
        "command": "groups validate",
        "status": "failed",
        "error_code": "GROUPS_CONFIG_INVALID",
    }


def test_groups_commands_do_not_upgrade_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(tmp_path)
    monkeypatch.setattr(
        cli_module,
        "upgrade_database",
        lambda _database_url: pytest.fail("groups commands must not touch the database"),
    )
    assert main(["groups", "validate", "--config", str(path)]) == 0
    capsys.readouterr()
    assert main(["groups", "list", "--config", str(path)]) == 0
