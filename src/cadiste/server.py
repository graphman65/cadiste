"""The viewer's HTTP server: the built frontend, the project's part list, a trace per request, measurements, and
the hub linking coding agents (``cadiste mcp``) to viewer tabs.

Every trace runs ``cadiste export`` in a fresh interpreter (the server's own, so the same environment): the
model's modules are imported anew, which picks up source edits, and in-process memoisation in the model (a
``functools.cache`` around its build function) cannot hand back a part that was never built under the tracer.

A build also writes its scene's shapes next to its JSON; the response names that file (``X-Cadiste-Shapes``), and
``POST /api/measure`` measures the viewer's faces, edges and vertices on it, exactly (``cadiste.measure``).

Agents: ``POST /api/agent/sessions`` opens a session, ``POST /api/agent/<session>/call/<tool>`` runs a tool
(``cadiste.agent``). Tabs: ``GET /api/events?tab=`` streams their events, ``POST /api/tabs/<tab>/state`` and
``.../reply`` answer, ``POST /api/sessions/<session>/link|decline|unlink`` are the user's link decisions
(``cadiste.hub``), ``POST /api/references`` captures standalone selection Markdown (``cadiste.references``).
"""

from __future__ import annotations

import contextlib
import importlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlsplit

from .agent import Agent, ToolError
from .cache import SHAPES_SUFFIX, TraceCache
from .hub import Hub
from .project import FolderStep
from .references import SnapshotError

if TYPE_CHECKING:
    from .measure import Shapes

STATIC = Path(__file__).resolve().parent / "static"
"""The frontend, built by ``npm run build`` in ``web/``."""

_TRACE_TIMEOUT = 900
_KEPT_SHAPES = 4
"""Shapes files kept loaded: the views open lately."""


class _ShapesGoneError(Exception):
    """The shapes file asked for is not (or no longer) there: its build was made again, or never wrote one."""


class Measurer:
    """Measures on the shapes files in ``directory``, the last few loaded kept in memory. One measurement at a
    time: OCP is not called from two threads at once."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._loaded: OrderedDict[tuple[str, int], Shapes] = OrderedDict()
        self._lock = threading.Lock()

    def _use[T](self, name: str, act: Callable[[Shapes], T]) -> T:
        from .measure import load

        file = self.directory / name
        if not re.fullmatch(rf"[\w.-]+{re.escape(SHAPES_SUFFIX)}", name) or not file.is_file():
            raise _ShapesGoneError(f"Shapes {name!r} are unavailable or expired; reload/rebuild the viewer.")
        key = (name, file.stat().st_mtime_ns)
        with self._lock:
            shapes = self._loaded.get(key)
            if shapes is None:
                shapes = self._loaded[key] = load(file)
                while len(self._loaded) > _KEPT_SHAPES:
                    self._loaded.popitem(last=False)
            self._loaded.move_to_end(key)
            return act(shapes)

    def measure(self, name: str, paths: list[str]) -> dict[str, Any]:
        from .measure import measure

        return self._use(name, lambda shapes: measure(shapes, paths))

    def adjacent(self, name: str, path: str) -> list[int] | None:
        """The faces an edge or a vertex bounds (``measure.adjacent_faces``)."""
        from .measure import adjacent_faces

        return self._use(name, lambda shapes: adjacent_faces(shapes, path))

    def corresponds(
        self,
        name: str,
        path: str,
        trace_name: str,
        trace_path: str,
        locations: list[Any],
        trace_locations: list[Any],
    ) -> bool:
        """Verify picked topology and incident faces in the recorded part coordinate frame."""
        from .measure import corresponding_entity

        source = self._use(name, lambda shapes: shapes)
        return self._use(
            trace_name,
            lambda trace: corresponding_entity(source, path, trace, trace_path, locations, trace_locations),
        )


@dataclass(frozen=True)
class Project:
    target: str
    """``module:callable`` returning the shape."""
    args: tuple[str, ...]
    """Arguments for a project without a part list."""
    parts: tuple[str, ...] | None
    """Part names, each passed to the target alone; None for a single model built with ``args``."""
    assemblies: dict[str, str] = field(default_factory=dict)
    """Assembly name -> ``module:callable`` returning it, built without the tracer; in the order given."""
    trace_env: dict[str, str] = field(default_factory=dict)
    """Environment for traced builds only (the assembly build keeps the server's)."""
    folders: dict[str, list[FolderStep]] = field(default_factory=dict)
    """Part name -> the folder it is listed in, package by package (then file by file)."""
    parts_target: str | None = None
    """``module:attribute`` naming the parts (``--parts``): an assembly's instances are matched to them."""

    @property
    def names(self) -> list[str]:
        return list(self.parts) if self.parts is not None else [" ".join([self.target, *self.args])]

    def trace_command(self, name: str) -> list[str]:
        args = [name] if self.parts is not None else list(self.args)
        return [sys.executable, "-m", "cadiste", "export", self.target, "--", *args]

    def assembly_command(self, name: str) -> list[str]:
        """Build the assembly; with a part list, also name the part each instance is of."""
        command = [sys.executable, "-m", "cadiste", "mesh"]
        if self.parts_target is not None:
            command += ["--match-parts", self.parts_target, "--part-build", self.target]
        return [*command, self.assemblies[name]]

    def folder_parts(self, path: str) -> list[str]:
        """The parts in the folder at ``path`` and the folders under it: a file's ("chassis/drive_module"), or a
        package's ("chassis", every file of it), in the order the project lists them."""
        parts = []
        for name, steps in self.folders.items():
            folder = "/".join(str(step["name"]) for step in steps)
            if folder == path or folder.startswith(f"{path}/"):
                parts.append(name)
        return parts


def _run(command: list[str], environment: dict[str, str] | None = None) -> tuple[bytes | None, dict[str, str]]:
    """The command's stdout, or None and the error to report."""
    env = None if not environment else {**os.environ, **environment}
    try:
        done = subprocess.run(command, capture_output=True, timeout=_TRACE_TIMEOUT, check=False, env=env)
    except subprocess.TimeoutExpired:
        return None, {"error": f"took over {_TRACE_TIMEOUT} s"}
    if done.returncode != 0:
        return None, {"error": "the build failed", "log": done.stderr.decode(errors="replace")}
    return done.stdout, {}


@dataclass(frozen=True)
class Build:
    body: bytes | None
    """The build's JSON; None when it failed."""
    cached: bool
    shapes: Path | None
    """Its shapes file, to measure on; None if it kept none."""
    failure: dict[str, str]
    """``{"error", "log"}`` of a failed build."""


@dataclass(frozen=True)
class Saved:
    body: bytes | None
    """The CAD file; None when the build failed."""
    stem: str
    """Its file name, without the format's suffix."""
    failure: dict[str, str]
    """``{"error", "log"}`` of a failed build."""


EXPORT_FORMATS = {"step": "model/step", "stl": "model/stl", "brep": "application/octet-stream"}
"""What ``GET /api/export`` writes (``cadiste save --format``), with its content type."""


class Builds:
    """What the viewer opens, built through the cache: a part (traced), an assembly, the parts of a folder (both
    only viewed). A target is the frontend's: ``{"kind": "part", "name"}``, ``{"kind": "assembly", "name"}`` (no
    name: the first assembly), ``{"kind": "file", "path"}``."""

    def __init__(self, project: Project, cache: TraceCache, always_fresh: bool):
        self.project = project
        self.cache = cache
        self.always_fresh = always_fresh
        """No cache (``--no-cache``): ``cache`` only holds the latest build of each, for its shapes."""

    def _recipe(self, target: dict[str, Any]) -> tuple[str, str, list[str], dict[str, str] | None, str]:
        """The cache entry's kind and name, the command (a cadiste subcommand: ``[python, -m, cadiste, <sub>, ...]``),
        its environment and what it does; LookupError for a target the project does not have."""
        project = self.project
        match target:
            case {"kind": "part", "name": str(name)}:
                if name not in project.names:
                    raise LookupError(f"unknown part {name!r}")
                return "trace", name, project.trace_command(name), project.trace_env, f"tracing {name!r}"
            case {"kind": "assembly", **rest}:
                if not project.assemblies:
                    raise LookupError("this project has no assembly (cadiste serve --assembly)")
                name = rest.get("name") or next(iter(project.assemblies))
                if name not in project.assemblies:
                    raise LookupError(f"unknown assembly {name!r}")
                return "assembly", name, project.assembly_command(name), None, f"building the assembly {name!r}"
            case {"kind": "file", "path": str(path)}:
                names = project.folder_parts(path)
                if not names:
                    raise LookupError(f"no parts in {path!r}")
                label = path.rpartition("/")[2]
                command = [sys.executable, "-m", "cadiste", "mesh", "--each", "--label", label, project.target, "--"]
                # Only viewed, not traced: the model's own part cache may serve, so the server's environment.
                return "file", path, [*command, *names], None, f"building {path!r}"
        raise LookupError(f"not something to build: {target!r}")

    def get(self, target: dict[str, Any], fresh: bool) -> Build:
        """The target's build, from the cache unless ``fresh`` (or its source changed); a failed build is not
        cached."""
        kind, name, command, environment, doing = self._recipe(target)
        failure: dict[str, str] = {}

        def make(shapes: Path) -> bytes | None:
            body, failed = _run([*command[:4], "--shapes", str(shapes), *command[4:]], environment)
            failure.update(failed)
            return body

        body, cached, shapes = self.cache.get_or_make(kind, name, make, fresh)
        if body is None:
            failure["error"] = f"{doing}: {failure.get('error', 'no output')}"
        return Build(body, cached, shapes, failure)

    def latest(self, target: dict[str, Any]) -> Build | None:
        """The target's last build, as the viewer shows it, even if its source changed since (None: never built)."""
        kind, name, *_ = self._recipe(target)
        file = self.cache.latest(kind, name)
        if file is None:
            return None
        shapes = file.with_suffix(SHAPES_SUFFIX)
        return Build(file.read_bytes(), True, shapes if shapes.is_file() else None, {})

    def save(self, target: dict[str, Any], form: str, only: list[str] | None = None) -> Saved:
        """The target built again (not traced: the model's own part cache may serve) and written as a CAD file in
        ``form`` (``EXPORT_FORMATS``); for a folder, ``only`` keeps those of its parts (the ones shown). LookupError
        for a target the project does not have."""
        project = self.project
        match target:
            case {"kind": "part", "name": str(name)}:
                if name not in project.names:
                    raise LookupError(f"unknown part {name!r}")
                if project.parts is None:
                    stem, command = project.target.rpartition(":")[2], [project.target, "--", *project.args]
                else:
                    stem, command = name, ["--label", name, project.target, "--", name]
                doing = f"exporting {name!r}"
            case {"kind": "assembly", **rest}:
                if not project.assemblies:
                    raise LookupError("this project has no assembly (cadiste serve --assembly)")
                name = rest.get("name") or next(iter(project.assemblies))
                if name not in project.assemblies:
                    raise LookupError(f"unknown assembly {name!r}")
                stem, command, doing = name, [project.assemblies[name]], f"exporting the assembly {name!r}"
            case {"kind": "file", "path": str(path)}:
                names = project.folder_parts(path)
                if not names:
                    raise LookupError(f"no parts in {path!r}")
                if only is not None:
                    if outside := [name for name in only if name not in names]:
                        raise LookupError(f"not in {path!r}: {', '.join(outside)}")
                    names = [name for name in names if name in only]
                stem = path.rpartition("/")[2]
                command, doing = ["--each", "--label", stem, project.target, "--", *names], f"exporting {path!r}"
            case _:
                raise LookupError(f"not something to export: {target!r}")
        save = [sys.executable, "-m", "cadiste", "save", "--format", form]
        with tempfile.TemporaryDirectory(prefix="cadiste-export-") as directory:
            output = Path(directory) / f"model.{form}"
            # The server's environment, as for the views: the model's own part cache may serve.
            body, failure = _run([*save, "--output", str(output), *command])
            if body is None:
                return Saved(None, stem, {**failure, "error": f"{doing}: {failure.get('error', 'no output')}"})
            return Saved(output.read_bytes(), stem, {})


_ID = re.compile(r"[\w-]{1,64}")


class _Handler(BaseHTTPRequestHandler):
    project: Project
    builds: Builds
    measurer: Measurer
    hub: Hub
    agent: Agent

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        # `latest`: the build just made for an agent, not built again even without a cache.
        fresh = query.get("fresh", ["0"])[0] == "1" or (
            self.builds.always_fresh and query.get("latest", ["0"])[0] != "1"
        )
        if url.path == "/api/project":
            self._json(
                {
                    "target": self.project.target,
                    "parts": self.project.names,
                    "folders": self.project.folders,
                    "assemblies": list(self.project.assemblies),
                    "root": Path.cwd().name,
                }
            )
        elif url.path == "/api/trace":
            self._built({"kind": "part", "name": query.get("part", [""])[0]}, fresh)
        elif url.path == "/api/assembly":
            self._built({"kind": "assembly", "name": query.get("name", [""])[0]}, fresh)
        elif url.path == "/api/file":
            self._built({"kind": "file", "path": query.get("path", [""])[0]}, fresh)
        elif url.path == "/api/export":
            self._export({key: values[0] for key, values in query.items() if key != "part"}, query.get("part"))
        elif url.path == "/api/events" and _ID.fullmatch(tab := query.get("tab", [""])[0]):
            self._events(tab)
        elif url.path.startswith("/api/"):
            self._json({"error": f"no such endpoint {url.path}"}, HTTPStatus.NOT_FOUND)
        else:
            self._static(url.path)

    def _built(self, target: dict[str, Any], fresh: bool) -> None:
        try:
            build = self.builds.get(target, fresh)
        except LookupError as error:
            # The message itself: str() of a KeyError quotes it.
            self._json({"error": str(error.args[0]) if error.args else str(error)}, HTTPStatus.NOT_FOUND)
            return
        if build.body is None:
            self._json(build.failure, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        headers = {"X-Cadiste-Cache": "hit" if build.cached else "miss"}
        if build.shapes is not None:
            headers["X-Cadiste-Shapes"] = build.shapes.name
        self._send(build.body, "application/json", headers=headers)

    def _export(self, query: dict[str, str], parts: list[str] | None) -> None:
        """``?format=step&kind=part&name=...`` (``kind=file&path=...[&part=...]``, ``kind=assembly[&name=...]``):
        the target as a file to download; ``part`` (repeated) keeps those of a folder's parts."""
        form = query.pop("format", "step")
        if form not in EXPORT_FORMATS:
            self._json({"error": f"format is one of {', '.join(EXPORT_FORMATS)}"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            saved = self.builds.save(query, form, parts)
        except LookupError as error:
            self._json({"error": str(error)}, HTTPStatus.NOT_FOUND)
            return
        if saved.body is None:
            self._json(saved.failure, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        filename = f"{saved.stem}.{form}"
        fallback = re.sub(r"[^\w .-]", "_", filename.encode("ascii", "replace").decode())
        disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"
        self._send(saved.body, EXPORT_FORMATS[form], headers={"Content-Disposition": disposition})

    def _events(self, tab: str) -> None:
        """The tab's Server-Sent Events, until it goes away or a newer stream for it replaces this one."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        stream = self.hub.event_stream(tab)
        try:
            for event in stream:
                chunk = ": keepalive\n\n" if event is None else f"event: {event[0]}\ndata: {json.dumps(event[1])}\n\n"
                self.wfile.write(chunk.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            stream.close()

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        try:
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            self._json({"error": "the body is not JSON"}, HTTPStatus.BAD_REQUEST)
            return
        steps = url.path.removeprefix("/api/").split("/")
        match steps:
            case ["measure"]:
                self._measure(request)
            case ["tabs", tab, "state"] if _ID.fullmatch(tab) and isinstance(request, dict):
                self.hub.set_state(tab, request)
                self._json({})
            case ["tabs", tab, "reply"] if _ID.fullmatch(tab) and isinstance(request, dict) and "id" in request:
                ok = request.get("ok") is True
                found = self.hub.reply(tab, str(request["id"]), ok, request.get("result" if ok else "error"))
                self._json({}, HTTPStatus.OK if found else HTTPStatus.NOT_FOUND)
            case ["sessions", session, ("link" | "decline" | "unlink") as action]:
                self._session_action(session, action, request)
            case ["references"] if isinstance(request, dict) and isinstance(request.get("state"), dict):
                tab = request.get("tab")
                try:
                    made = self.agent.snapshot(tab if isinstance(tab, str) else None, request["state"])
                except (ToolError, LookupError, SnapshotError) as error:
                    self._json({"error": str(error).strip("'\"")}, HTTPStatus.UNPROCESSABLE_ENTITY)
                else:
                    self._json(made)
            case ["agent", "sessions"]:
                client = request.get("client") if isinstance(request, dict) else None
                session = self.hub.open_session(str(client or "agent"))
                self._json({"id": session.id, "code": session.code})
            case ["agent", session, "close"]:
                self.hub.close_session(session)
                self._json({})
            case ["agent", session, "call", tool] if isinstance(request, dict):
                self._call(session, tool, request)
            case _:
                self._json({"error": f"no such endpoint {url.path}"}, HTTPStatus.NOT_FOUND)

    def _measure(self, request: Any) -> None:
        from .measure import MeasureError

        try:
            shapes, paths = request["shapes"], request["paths"]
            if not isinstance(shapes, str) or not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                raise TypeError
        except (KeyError, TypeError):
            expected = 'expected {"shapes": "<file>", "paths": ["<viewer path>", ...]}'
            self._json({"error": expected}, HTTPStatus.BAD_REQUEST)
            return
        try:
            self._json(self.measurer.measure(shapes, paths))
        except _ShapesGoneError:
            self._json({"error": "this build is no longer kept: rebuild to measure"}, HTTPStatus.GONE)
        except MeasureError as error:
            self._json({"error": str(error)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def _session_action(self, session: str, action: str, request: Any) -> None:
        try:
            if action == "link":
                tab = request.get("tab") if isinstance(request, dict) else None
                if not isinstance(tab, str) or not _ID.fullmatch(tab):
                    self._json({"error": 'expected {"tab": "<tab id>"}'}, HTTPStatus.BAD_REQUEST)
                    return
                self.hub.link(session, tab)
            elif action == "decline":
                self.hub.decline(session)
            else:
                self.hub.unlink(session)
        except KeyError:
            self._json({"error": "no such session"}, HTTPStatus.NOT_FOUND)
            return
        self._json({})

    def _call(self, session: str, tool: str, arguments: dict[str, Any]) -> None:
        try:
            self.hub.session(session)
        except KeyError:
            # The server restarted since the bridge opened its session: it opens another.
            self._json({"error": "no such session"}, HTTPStatus.NOT_FOUND)
            return
        try:
            self._json(self.agent.call(session, tool, arguments).json())
        except ToolError as error:
            self._json({"error": str(error)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def _static(self, path: str) -> None:
        index = STATIC / "index.html"
        if not index.is_file():
            message = "frontend not built: run `mise run web-build` in the cadiste repository"
            self._send(message.encode(), "text/plain; charset=utf-8", HTTPStatus.SERVICE_UNAVAILABLE)
            return
        file = (STATIC / path.lstrip("/")).resolve()
        if not file.is_relative_to(STATIC) or not file.is_file():
            file = index  # client-side routes
        content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        self._send(file.read_bytes(), content_type)

    def _json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(json.dumps(value).encode(), "application/json", status)

    def _send(
        self,
        body: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # A tab's event stream and state pushes are its heartbeat, not worth a line each.
        if self.path.startswith("/api/") and not self.path.startswith(("/api/events", "/api/tabs/")):
            super().log_message(format, *args)


def serve(
    project: Project, host: str, port: int, cache: TraceCache | None = None, lockfile: Path | None = None
) -> None:
    """Serve until interrupted; ``lockfile`` says where (``{"url", "pid"}``) for ``cadiste mcp`` to find it."""
    with contextlib.ExitStack() as stack:
        if cache is None:
            # Builds still go through a cache, in a directory of its own, so that their shapes are kept to measure.
            directory = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="cadiste-")))
            entries, where = TraceCache(directory, [], ""), "no trace cache"
        else:
            entries, where = cache, f"traces kept in {cache.directory}"
        builds = Builds(project, entries, always_fresh=cache is None)
        measurer = Measurer(entries.directory)
        hub = Hub()
        attributes = {
            "project": project,
            "builds": builds,
            "measurer": measurer,
            "hub": hub,
            "agent": Agent(hub, builds, measurer, project, Path.cwd()),
        }
        handler = type("Handler", (_Handler,), attributes)
        # Measuring needs OCP: import it now rather than on the first measurement.
        threading.Thread(target=importlib.import_module, args=(f"{__package__}.measure",), daemon=True).start()
        with ThreadingHTTPServer((host, port), handler) as server:
            url = f"http://{host}:{port}"
            if lockfile is not None:
                lockfile.parent.mkdir(parents=True, exist_ok=True)
                lockfile.write_text(json.dumps({"url": url, "pid": os.getpid()}))
                stack.callback(_remove_lockfile, lockfile)
            print(f"cadiste: {len(project.names)} part(s) from {project.target}, {where}, on {url}", flush=True)
            with contextlib.suppress(KeyboardInterrupt):
                server.serve_forever()


def _remove_lockfile(lockfile: Path) -> None:
    """Unless another server has written it since."""
    with contextlib.suppress(OSError, ValueError):
        if json.loads(lockfile.read_text()).get("pid") == os.getpid():
            lockfile.unlink()
