from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VersionedPrompt:
    version: str
    sha256: str
    text: str


EVENT_EXTRACTION_PROMPT_VERSION = "event-extraction-v2"
EVENT_EXTRACTION_PROMPT_SHA256 = "031e043bea4b48bb2ff972336fbb481bd25c77400eea7a76f069442be4a753e2"


def load_event_extraction_prompt() -> VersionedPrompt:
    path = Path(__file__).with_name("prompts") / "event_extraction_v2.txt"
    text = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != EVENT_EXTRACTION_PROMPT_SHA256:
        raise RuntimeError(
            "Event extraction prompt changed without a new version and checksum"
        )
    return VersionedPrompt(
        version=EVENT_EXTRACTION_PROMPT_VERSION,
        sha256=digest,
        text=text,
    )
