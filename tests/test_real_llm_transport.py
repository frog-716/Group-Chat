from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from group_info_system.analysis.codex_transport import (
    CodexCLITransport,
    CodexCLITransportError,
)


def test_codex_transport_runs_in_isolated_read_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        invocation["command"] = command
        invocation.update(kwargs)
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text('{"events":[]}', encoding="utf-8")
        schema_index = command.index("--output-schema") + 1
        schema = json.loads(Path(command[schema_index]).read_text(encoding="utf-8"))
        assert schema["type"] == "object"
        event_schema = schema["$defs"]["EventCandidate"]
        assert set(event_schema["required"]) == set(event_schema["properties"])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("shutil.which", lambda executable: f"/bin/{executable}")
    monkeypatch.setattr(subprocess, "run", fake_run)

    output = CodexCLITransport().complete(
        model="real-model",
        system_prompt="contract",
        user_prompt='{"messages":[]}',
    )

    assert output == '{"events":[]}'
    command = invocation["command"]
    assert isinstance(command, list)
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "real-model"
    assert invocation["cwd"] != Path.cwd()


def test_codex_transport_reports_cli_failure_without_echoing_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda executable: f"/bin/{executable}")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 7, stdout="sensitive output", stderr="sensitive error"
        ),
    )

    with pytest.raises(CodexCLITransportError, match="status 7") as exc_info:
        CodexCLITransport().complete(
            model="real-model",
            system_prompt="contract",
            user_prompt='{"messages":[]}',
        )
    assert "sensitive" not in str(exc_info.value)
