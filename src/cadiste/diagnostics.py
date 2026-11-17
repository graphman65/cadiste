"""Observable process, source, skill and served-asset generations (not geometry identities)."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any

_PACKAGE = Path(__file__).resolve().parent


def _digest(files: list[Path], base: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        try:
            digest.update(str(path.relative_to(base)).encode())
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"unavailable")
    return digest.hexdigest()


def source_generation() -> str:
    return _digest(list(_PACKAGE.glob("*.py")), _PACKAGE)


_RUNNING_SOURCE = source_generation()


def runtime_info(
    root: Path,
    *,
    local_url: str | None = None,
    web_dir: Path | None = None,
    skill_digest: str | None = None,
    mcp_generation: str | None = None,
) -> dict[str, Any]:
    """The parent passes the actual served web directory, including installed-package layouts."""
    source = source_generation()
    skill = _PACKAGE / "skills" / "cadiste" / "SKILL.md"
    disk_skill = hashlib.sha256(skill.read_bytes()).hexdigest() if skill.exists() else None
    web = Path(web_dir) if web_dir is not None else _PACKAGE / "static"
    assets = sorted(path for path in web.rglob("*") if path.is_file()) if web.exists() else []
    frontend = _PACKAGE.parent.parent / "web" / "src"
    source_files = [path for path in frontend.rglob("*") if path.is_file()] if frontend.exists() else []
    newest_source = max((path.stat().st_mtime for path in source_files), default=0)
    oldest_bundle = max((path.stat().st_mtime for path in assets), default=0)
    warnings = []
    if source != _RUNNING_SOURCE:
        warnings.append("Backend files changed since this process started; restart Cadiste.")
    if not (web / "index.html").exists():
        warnings.append("The built frontend is unavailable; build/install the web UI before captures.")
    elif newest_source > oldest_bundle:
        warnings.append("Frontend source is newer than the served bundle; rebuild the web UI.")
    if skill_digest is not None and disk_skill != skill_digest:
        warnings.append("MCP skill generation differs from disk; reconnect the MCP server.")
    if mcp_generation is not None and mcp_generation != source:
        warnings.append("MCP tool implementation differs from disk; reconnect the MCP server.")
    return {
        "pid": os.getpid(),
        "python": sys.executable,
        "package": str(_PACKAGE),
        "root": str(root),
        "url": local_url,
        "backend": {"running": _RUNNING_SOURCE, "disk": source, "matches": source == _RUNNING_SOURCE},
        "frontend": {
            "directory": str(web),
            "bundle_id": _digest(assets, web),
            "source_id": _digest(source_files, frontend),
            "stale": newest_source > oldest_bundle,
            "assets": ["/" + str(path.relative_to(web)) for path in assets],
        },
        "skill": {"disk": disk_skill, "mcp": skill_digest},
        "mcp_generation": mcp_generation,
        "renderer_protocol": "cadiste-renderer-v2",
        "warnings": warnings,
    }
