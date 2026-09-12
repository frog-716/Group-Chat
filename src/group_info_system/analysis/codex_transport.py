from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .llm import LLMEventPayload


class CodexCLITransportError(RuntimeError):
    pass


def _strict_structured_output_schema(schema: dict[str, object]) -> dict[str, object]:
    """Adapt Pydantic JSON Schema to the API's strict structured-output subset."""

    def visit(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties)
                node["additionalProperties"] = False
            node.pop("default", None)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema


class CodexCLITransport:
    """Run one isolated structured model call through an authenticated Codex CLI.

    The transport owns only invocation mechanics. It receives prompt text and returns
    response text; it has no database, collector, or report dependencies.
    """

    def __init__(
        self,
        *,
        executable: str = "codex",
        reasoning_effort: str = "low",
        timeout_seconds: int = 180,
    ) -> None:
        self.executable = executable
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds

    def complete(self, *, model: str, system_prompt: str, user_prompt: str) -> str:
        executable = shutil.which(self.executable)
        if executable is None:
            raise CodexCLITransportError(f"Codex CLI not found: {self.executable}")

        invocation_prompt = (
            "Follow the event extraction contract below. Do not use tools or inspect files.\n\n"
            "<contract>\n"
            f"{system_prompt}"
            "\n</contract>\n\n"
            "<input>\n"
            f"{user_prompt}"
            "\n</input>"
        )
        with tempfile.TemporaryDirectory(prefix="group-info-llm-eval-") as temp_dir:
            temp_path = Path(temp_dir)
            schema_path = temp_path / "event-v2-output-schema.json"
            output_path = temp_path / "response.json"
            schema_path.write_text(
                json.dumps(
                    _strict_structured_output_schema(LLMEventPayload.model_json_schema()),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            command = [
                executable,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                model,
                "--config",
                f'model_reasoning_effort="{self.reasoning_effort}"',
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            try:
                completed = subprocess.run(
                    command,
                    input=invocation_prompt,
                    text=True,
                    cwd=temp_path,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise CodexCLITransportError(
                    f"Codex CLI timed out after {self.timeout_seconds} seconds"
                ) from exc

            if completed.returncode != 0:
                raise CodexCLITransportError(
                    f"Codex CLI exited with status {completed.returncode}"
                )
            if not output_path.is_file():
                raise CodexCLITransportError("Codex CLI did not produce a response file")
            return output_path.read_text(encoding="utf-8")
