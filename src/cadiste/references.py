"""References to what a viewer tab had selected: the user copies one (``cadiste:ref/<id>``) and pastes it to a coding
agent, whose ``cadiste mcp`` resolves it (``get_reference``) to everything cadiste knew about the selection then.

A reference is resolved when it is made, from the build the tab showed, and kept on disk with the text: it answers
the same after the part was built again or the server restarted.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PREFIX = "cadiste:ref/"
_ID = re.compile(r"[0-9a-f]{8}")
_FOUND = re.compile(rf"(?:{re.escape(PREFIX)})?([0-9a-f]{{8}})\b")
_KEPT = 500
"""References kept on disk; the oldest go first."""


def parse(text: str) -> str | None:
    """The id in a pasted reference (``cadiste:ref/1a2b3c4d (Steering Module: #7 CrossPrism)``), or a bare id."""
    found = _FOUND.search(text.strip())
    return found[1] if found else None


class References:
    def __init__(self, directory: Path):
        self.directory = directory

    def create(self, record: dict[str, Any]) -> str:
        """Keep ``record`` under a new id; the oldest references beyond the last ``_KEPT`` are dropped."""
        self.directory.mkdir(parents=True, exist_ok=True)
        while True:
            reference = secrets.token_hex(4)
            file = self.directory / f"{reference}.json"
            if not file.exists():
                break
        created = datetime.now(UTC).isoformat(timespec="seconds")
        file.write_text(json.dumps({"id": reference, "created": created, **record}))
        kept = sorted(self.directory.glob("*.json"), key=lambda path: path.stat().st_mtime_ns)
        for old in kept[:-_KEPT]:
            old.unlink(missing_ok=True)
        return reference

    def get(self, reference: str) -> dict[str, Any] | None:
        if not _ID.fullmatch(reference):
            return None
        file = self.directory / f"{reference}.json"
        return json.loads(file.read_text()) if file.is_file() else None
