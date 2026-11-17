"""Bounded local workspace and lifecycle ownership for independent project servers.

Profiles only describe configuration. Opening one explicitly executes trusted project Python
in its own interpreter/process, never in the workspace server.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from .profiles import PROFILE_NAME, Profile, create_starter


class WorkspaceError(ValueError):
    """An actionable workspace request failure."""


@dataclass
class _Child:
    id: str
    profile_path: str
    profile: Profile
    process: subprocess.Popen[bytes]
    output: BinaryIO
    resources: contextlib.ExitStack
    temporary: tempfile.TemporaryDirectory[str]
    started: float
    status: str = "starting"
    url: str | None = None
    error: str | None = None
    finished_logs: str | None = None


class Workspace:
    def __init__(
        self,
        root: Path,
        *,
        state_dir: Path | None = None,
        launcher_url: str | None = None,
        startup_timeout: float = 45,
    ):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise WorkspaceError(f"workspace root is not a directory: {self.root}")
        self.launcher_url = launcher_url
        self.startup_timeout = startup_timeout
        self._lock = threading.RLock()
        self._children: dict[str, _Child] = {}
        self._closed = False
        self._ports: dict[str, int] = {}
        self._session_path: Path | None = None
        self._recovery_errors: list[str] = []
        self._state = Path(state_dir) if state_dir is not None else self.root / ".cadiste-workspace"
        if self._state.is_symlink():
            raise WorkspaceError("workspace state directory must not be a symlink")
        self._state.mkdir(parents=True, exist_ok=True)
        self._recent_path = self._state / "recent.json"
        self._recent: list[str] = []
        self._history_error: str | None = None
        self._discovered: dict[str, dict[str, Any]] = {}
        self._discovery_expires = 0.0
        self._discovery_errors: list[str] = []
        try:
            if self._recent_path.is_symlink():
                raise WorkspaceError("workspace recent-project file must not be a symlink")
            if self._recent_path.exists():
                with self._recent_path.open("rb") as stream:
                    raw = stream.read(65537)
                if len(raw) > 65536:
                    raise ValueError("recent-project file exceeds 64 KiB")
                entries = json.loads(raw)
                if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
                    raise ValueError("expected a list of profile paths")
                self._recent = entries[:30]
        except (OSError, ValueError) as error:
            self._history_error = f"Cannot read recent projects: {error}"

    def restore(self) -> None:
        """Restore this launcher's previously open trusted projects on their recorded ports."""
        if not self.launcher_url or self._session_path is not None:
            raise WorkspaceError("workspace restoration requires a launcher URL and runs only once")
        identity = uuid.uuid5(uuid.NAMESPACE_URL, self.launcher_url.rstrip("/")).hex
        self._session_path = self._state / f"servers-{identity}.json"
        try:
            if self._session_path.is_symlink():
                raise WorkspaceError("workspace server-state file must not be a symlink")
            if not self._session_path.exists():
                return
            with self._session_path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("server state exceeds 64 KiB")
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("projects"), list):
                raise ValueError("expected version-1 project server state")
            ports: dict[str, int] = {}
            used: set[int] = set()
            for item in data["projects"]:
                if not isinstance(item, dict):
                    raise ValueError("expected project server entries")
                profile, port = item.get("profile_path"), item.get("port")
                if not isinstance(profile, str) or type(port) is not int or not 1 <= port <= 65535:
                    raise ValueError("expected a profile path and TCP port")
                if port in used:
                    raise ValueError("expected distinct project ports")
                path = str(self._path(profile, exists=False))
                if path in ports:
                    raise ValueError("duplicate project profile")
                ports[path] = port
                used.add(port)
            self._ports = ports
        except (OSError, ValueError, TypeError) as error:
            self._recovery_errors.append(f"Cannot restore project servers: {error}")
            return
        for profile in sorted(self._ports):
            try:
                self.open(profile)
            except (OSError, ValueError, RuntimeError) as error:
                self._recovery_errors.append(f"Cannot reopen {profile}: {error}")

    def _save_session(self) -> None:
        if self._session_path is None:
            return
        data = {
            "version": 1,
            "projects": [{"profile_path": profile, "port": port} for profile, port in self._ports.items()],
        }
        encoded = json.dumps(data, indent=2) + "\n"
        if len(encoded.encode()) > 65536:
            raise WorkspaceError("project server state exceeds 64 KiB")
        with tempfile.NamedTemporaryFile("w", dir=self._state, encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
        try:
            os.replace(temporary, self._session_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _path(self, value: str | Path, *, directory: bool = False, exists: bool = True) -> Path:
        if not isinstance(value, (str, Path)) or "\0" in str(value):
            raise WorkspaceError("path must be a string without NUL characters")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        # Reject '..' and symlink traversal even when it would resolve back inside root.
        if ".." in candidate.parts:
            raise WorkspaceError("parent-directory traversal is not allowed")
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as error:
            raise WorkspaceError(f"path must stay within workspace root {self.root}") from error
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise WorkspaceError(f"symlink traversal is not allowed: {current}")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise WorkspaceError("path escapes workspace root")
        if exists and not (resolved.is_dir() if directory else resolved.is_file()):
            kind = "directory" if directory else "file"
            raise WorkspaceError(f"{kind} does not exist: {resolved}")
        return resolved

    def _profile(self, value: str | Path) -> tuple[Path, Profile]:
        path = self._path(value)
        profile = Profile.load(path)
        self._path(profile.root, directory=True)
        # Profile.load resolves roots; check the original path as well, before resolution.
        raw = json.loads(path.read_text(encoding="utf-8"))
        root = Path(raw["root"])
        self._path(root if root.is_absolute() else path.parent / root, directory=True)
        return path, profile

    def _remember(self, path: Path) -> None:
        value = str(path)
        with self._lock:
            self._recent = [value, *(item for item in self._recent if item != value)][:30]
            with tempfile.NamedTemporaryFile("w", dir=self._state, encoding="utf-8", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self._recent, stream, indent=2)
                stream.write("\n")
            try:
                os.replace(temporary, self._recent_path)
            finally:
                temporary.unlink(missing_ok=True)
            self._history_error = None

    def _project_id(self, path: Path) -> str:
        return uuid.uuid5(uuid.NAMESPACE_URL, path.as_uri()).hex

    def _registered(self, path: Path, profile: Profile) -> dict[str, Any]:
        return {"project_id": self._project_id(path), "profile_path": str(path), "profile": profile.to_dict()}

    def _discover(self) -> dict[str, dict[str, Any]]:
        """Read declarative profiles once per 30 seconds; never import project code."""
        if time.monotonic() < self._discovery_expires:
            return self._discovered
        projects: dict[str, dict[str, Any]] = {}
        errors: list[str] = []

        def inaccessible(error: OSError) -> None:
            errors.append(f"Cannot search workspace directory {error.filename}: {error.strerror}")

        for directory, directories, files in os.walk(self.root, onerror=inaccessible, followlinks=False):
            base = Path(directory)
            directories[:] = [
                name
                for name in directories
                if not name.startswith(".")
                and name not in {"node_modules", "__pycache__", "test", "tests", "fixtures", "__fixtures__"}
                and not (base / name).is_symlink()
            ]
            if PROFILE_NAME not in files:
                continue
            path = base / PROFILE_NAME
            if path.is_symlink():
                continue
            try:
                resolved, profile = self._profile(path)
                projects[str(path)] = self._registered(resolved, profile)
            except (OSError, ValueError) as error:
                projects[str(path)] = {"profile_path": str(path), "error": str(error)}
        self._discovered = projects
        self._discovery_errors = errors
        self._discovery_expires = time.monotonic() + 30
        return projects

    def browse(self, path: str = "", offset: int = 0, limit: int = 100) -> dict[str, Any]:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise WorkspaceError("browse offset must be nonnegative and limit between 1 and 100")
        directory = self._path(path, directory=True)
        entries: list[dict[str, Any]] = []
        try:
            # Only immediate directories and declarative profiles, never Python file discovery.
            for entry in directory.iterdir():
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if entry.is_dir() or (entry.is_file() and entry.suffix == ".json"):
                    entries.append(
                        {
                            "name": entry.name,
                            "path": str(entry.relative_to(self.root)),
                            "kind": "directory" if entry.is_dir() else "profile",
                        }
                    )
        except OSError as error:
            raise WorkspaceError(f"cannot browse {directory}: {error}") from error
        entries.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold()))
        return {
            "root": str(self.root),
            "path": str(directory.relative_to(self.root)),
            "parent": str(directory.parent.relative_to(self.root)) if directory != self.root else None,
            "entries": entries[offset : offset + limit],
            "total": len(entries),
            "offset": offset,
            "next_offset": offset + limit if offset + limit < len(entries) else None,
        }

    def create(self, path: str, name: str, interpreter: str | None = None) -> dict[str, Any]:
        directory = self._path(path, exists=False)
        self._path(directory.parent, directory=True)
        profile_path = create_starter(directory, name=name, interpreter=interpreter)
        self._remember(profile_path)
        return self._registered(profile_path, Profile.load(profile_path))

    def register(
        self,
        profile: str | None = None,
        configuration: dict[str, Any] | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        if profile is not None:
            if configuration is not None or path is not None:
                raise WorkspaceError("supply either an existing profile or configuration plus its new path")
            profile_path, parsed = self._profile(profile)
        else:
            if configuration is None or path is None:
                raise WorkspaceError("supply an existing profile, or explicit configuration and a new profile path")
            profile_path = self._path(path, exists=False)
            self._path(profile_path.parent, directory=True)
            # Validate original root before Profile normalizes it.
            if not isinstance(configuration, dict) or not isinstance(configuration.get("root"), str):
                raise WorkspaceError("configuration must include an explicit project root")
            root = Path(configuration["root"])
            self._path(root if root.is_absolute() else profile_path.parent / root, directory=True)
            parsed = Profile.from_dict(configuration, base=profile_path.parent)
            parsed.write(profile_path)
        self._remember(profile_path)
        return self._registered(profile_path, parsed)

    def _logs(self, child: _Child) -> str:
        if child.finished_logs is not None:
            return child.finished_logs
        try:
            # A file, not a pipe: a noisy child cannot deadlock waiting for a reader.
            size = os.fstat(child.output.fileno()).st_size
            return os.pread(child.output.fileno(), 16384, max(0, size - 16384)).decode(errors="replace").strip()
        except (OSError, ValueError):
            return ""

    def _child_data(self, child: _Child) -> dict[str, Any]:
        returncode = child.process.poll()
        if returncode is not None and child.status in {"starting", "running"}:
            child.status = "failed"
            child.error = f"Project server exited with code {returncode}.\n{self._logs(child)}".strip()
        return {
            "id": child.id,
            "project_id": self._project_id(Path(child.profile_path)),
            "profile_path": child.profile_path,
            "name": child.profile.name,
            "root": str(child.profile.root),
            "pid": child.process.pid,
            "status": child.status,
            "url": child.url if child.status == "running" else None,
            "error": child.error,
            "returncode": returncode,
            "started": child.started,
            "logs": self._logs(child),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            children = [self._child_data(child) for child in self._children.values()]
            discovered = self._discover()
            projects = []
            paths = dict.fromkeys([*self._recent, *(child["profile_path"] for child in children), *discovered])
            for value in paths:
                child = next((child for child in reversed(children) if child["profile_path"] == value), None)
                try:
                    path = self._path(value, exists=False)
                    if not path.is_file() and (child is None or child["status"] not in {"running", "starting"}):
                        continue
                    if value in discovered:
                        item = dict(discovered[value])
                    else:
                        path, profile = self._profile(path)
                        item = self._registered(path, profile)
                except (ValueError, OSError) as error:
                    item = {"profile_path": value, "error": str(error)}
                item["child"] = child
                projects.append(item)
            projects.sort(
                key=lambda item: (
                    item.get("profile", {}).get("name", item["profile_path"]).casefold(),
                    item["profile_path"],
                )
            )
            errors = [self._history_error, *self._recovery_errors, *self._discovery_errors]
            return {
                "root": str(self.root),
                "projects": projects,
                "children": children,
                "error": "\n".join(error for error in errors if error) or None,
            }

    def status(self, id: str) -> dict[str, Any]:
        with self._lock:
            child = self._children.get(id)
            if child is None:
                raise WorkspaceError("unknown child; this workspace can only manage processes it started")
            return self._child_data(child)

    def open(self, profile: str) -> dict[str, Any]:
        profile_path, parsed = self._profile(profile)
        with self._lock:
            if self._closed:
                raise WorkspaceError("workspace is shutting down")
            for child in self._children.values():
                if child.profile_path == str(profile_path) and child.process.poll() is None:
                    if child.profile.to_dict() != parsed.to_dict():
                        raise WorkspaceError("profile configuration changed; stop its running server before reopening")
                    return self._child_data(child)
            self._remember(profile_path)
            temporary = tempfile.TemporaryDirectory(prefix="cadiste-workspace-")
            ready = Path(temporary.name) / "ready.json"
            token = secrets.token_urlsafe(32)
            port = self._ports.get(str(profile_path), 0)
            command = [
                parsed.python,
                "-m",
                "cadiste",
                "serve",
                "--profile",
                str(profile_path),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--ready-file",
                str(ready),
                f"--ready-token={token}",
                "--workspace-root",
                str(self.root),
            ]
            if self.launcher_url:
                command.extend(["--workspace-url", self.launcher_url])
            environment = dict(os.environ)
            imports = [str(Path(__file__).resolve().parent.parent), str(parsed.root)]
            if (parsed.root / "src").is_dir():
                imports.append(str(parsed.root / "src"))
            if environment.get("PYTHONPATH"):
                imports.append(environment["PYTHONPATH"])
            environment["PYTHONPATH"] = os.pathsep.join(imports)
            environment["PYTHONUNBUFFERED"] = "1"
            with contextlib.ExitStack() as pending:
                output = pending.enter_context(tempfile.TemporaryFile("w+b"))
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=parsed.root,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                except OSError as error:
                    temporary.cleanup()
                    raise WorkspaceError(
                        f"cannot start {parsed.python}: {error}; check the profile interpreter and dependencies"
                    ) from error
                child = _Child(
                    id=uuid.uuid4().hex,
                    profile_path=str(profile_path),
                    profile=parsed,
                    process=process,
                    output=output,
                    resources=pending.pop_all(),
                    temporary=temporary,
                    started=time.time(),
                )
                self._children[child.id] = child
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            with self._lock:
                if child.status != "starting" or child.process.poll() is not None:
                    return self._child_data(child)
            if ready.exists():
                try:
                    with ready.open("rb") as stream:
                        data = json.loads(stream.read(4096))
                    url = urlsplit(data["url"])
                    bound_port = url.port
                    if (
                        data.get("token") != token
                        or data.get("pid") != process.pid
                        or url.scheme != "http"
                        or url.hostname != "127.0.0.1"
                        or not bound_port
                        or (port != 0 and bound_port != port)
                        or url.username
                        or url.password
                        or url.path not in {"", "/"}
                        or url.query
                        or url.fragment
                    ):
                        raise ValueError("readiness identity or loopback URL mismatch")
                except (OSError, ValueError, KeyError, TypeError) as error:
                    self._stop(child.id)
                    with self._lock:
                        child.status = "failed"
                        child.error = f"Invalid project readiness handshake: {error}.\n{self._logs(child)}".strip()
                        return self._child_data(child)
                with self._lock:
                    if child.status == "starting" and process.poll() is None:
                        child.url = data["url"].rstrip("/")
                        child.status = "running"
                        self._ports[str(profile_path)] = bound_port
                        self._save_session()
                    return self._child_data(child)
            time.sleep(0.05)
        self._stop(child.id)
        with self._lock:
            child.status = "failed"
            child.error = (
                f"Project did not become ready within {self.startup_timeout:g}s. Check its interpreter, "
                f"installed cadiste/build123d dependencies, targets and startup output.\n{self._logs(child)}"
            ).strip()
            return self._child_data(child)

    def restart(self, id: str) -> dict[str, Any]:
        """Replace only an owned child, retaining its project identity and port."""
        with self._lock:
            child = self._children.get(id)
            if child is None:
                raise WorkspaceError("unknown child; external processes are never restarted")
            if self._closed:
                raise WorkspaceError("workspace is shutting down")
            if any(other is not child and other.profile_path == child.profile_path
                   and other.process.poll() is None for other in self._children.values()):
                raise WorkspaceError("stale child identity; refresh workspace status before restarting this project")
            # Validate edited configuration before interrupting the currently running server.
            self._profile(child.profile_path)
            self._stop(id)
            return self.open(child.profile_path)

    def stop(self, id: str) -> dict[str, Any]:
        with self._lock:
            result = self._stop(id)
            profile = result["profile_path"]
            if not any(
                child.profile_path == profile and child.process.poll() is None for child in self._children.values()
            ):
                self._ports.pop(profile, None)
                self._save_session()
            return result

    def _stop(self, id: str) -> dict[str, Any]:
        with self._lock:
            child = self._children.get(id)
            if child is None:
                raise WorkspaceError("unknown child; external processes are never stopped")
            if child.process.poll() is None:
                # Let the server close its owned renderer/runtime before escalating to its process group.
                with contextlib.suppress(ProcessLookupError):
                    child.process.terminate()
                try:
                    child.process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.process.pid, signal.SIGKILL)
                    child.process.wait(timeout=4)
            child.status = "stopped"
            child.url = None
            child.temporary.cleanup()
            child.finished_logs = self._logs(child)
            child.resources.close()
            return self._child_data(child)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for child in self._children.values():
                self._stop(child.id)
