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
import hashlib
import importlib
import io
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

from .agent import Agent, ToolError
from .background import BackgroundBuilds
from .cache import SHAPES_SUFFIX, TraceCache
from .diagnostics import runtime_info
from .feedback import Feedback
from .hub import Hub
from .process import locked, run
from .project import FolderStep, _CatalogError, _discover, source_roots
from .references import SnapshotError
from .ros import ConfigConflictError, Controls, InvalidConfigError

if TYPE_CHECKING:
    from .measure import Shapes

STATIC = Path(__file__).resolve().parent / "static"
"""The frontend, built by ``npm run build`` in ``web/``."""

_TRACE_TIMEOUT = 900
_KEPT_SHAPES = 4
"""Shapes files kept loaded: the views open lately."""


class _ShapesGoneError(LookupError):
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

    def check(self, name: str, pairs: list[list[str]], **options: Any) -> dict[str, Any]:
        """Run whole-solid checks under the same OCP serialization as exact measurements."""
        from .checks import check

        return self._use(name, lambda shapes: check(shapes, pairs, **options))

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
class _Catalog:
    parts: tuple[str, ...] | None
    names: tuple[str, ...]
    folders: dict[str, list[FolderStep]]

    def folder_parts(self, path: str) -> list[str]:
        """Parts in this folder and its descendants, in catalog order."""
        return [
            name
            for name, steps in self.folders.items()
            if (folder := "/".join(str(step["name"]) for step in steps)) == path or folder.startswith(f"{path}/")
        ]


class Project:
    """Fixed build configuration and one atomically replaced catalog shared by HTTP, builds and agents."""

    def __init__(
        self,
        target: str,
        args: tuple[str, ...],
        parts: tuple[str, ...] | None,
        assemblies: dict[str, str] | None = None,
        trace_env: dict[str, str] | None = None,
        folders: dict[str, list[FolderStep]] | None = None,
        parts_target: str | None = None,
        simulations: dict[str, str] | None = None,
        *,
        variants: dict[str, Project] | None = None,
        variant_labels: dict[str, str] | None = None,
        default_variant: str | None = None,
        check_sets: dict[str, dict[str, Any]] | None = None,
        manufacturing: dict[str, Any] | None = None,
        profile_path: str | None = None,
    ):
        self.target = target
        self.args = args
        self.assemblies = assemblies if assemblies is not None else {}
        self.simulations = simulations if simulations is not None else {}
        self.trace_env = trace_env if trace_env is not None else {}
        self.parts_target = parts_target
        self.check_sets = dict(check_sets or {})
        self.manufacturing = dict(manufacturing or {})
        self.profile_path = profile_path
        self._has_parts = parts_target is not None or parts is not None
        self._catalog = _Catalog(parts, parts if parts is not None else (" ".join([target, *args]),), folders or {})
        self._catalog_error: _CatalogError | None = None
        self._catalog_lock = threading.Lock()
        self.variant_projects = MappingProxyType(dict(variants or {}))
        self.default_variant = default_variant or next(iter(self.variant_projects), None)
        if self.default_variant is not None and self.default_variant not in self.variant_projects:
            raise ValueError(f"unknown variant {self.default_variant!r}")
        self.variants = tuple(
            {"id": identifier, "label": (variant_labels or {}).get(identifier, identifier)}
            for identifier in self.variant_projects
        )
        if self.variant_projects:
            # Implicit assembly requests choose the default, without mutable selection.
            order = [self.default_variant, *(key for key in self.variant_projects if key != self.default_variant)]
            self.assemblies = {
                f"{identifier}/{name}": factory
                for identifier in order
                for name, factory in self.variant_projects[identifier].assemblies.items()
            }
            self.simulations = {
                f"{identifier}/{name}": factory
                for identifier, project in self.variant_projects.items()
                for name, factory in project.simulations.items()
            }

    def resolve(self, target: dict[str, Any]) -> tuple[Project, dict[str, Any], str | None]:
        """Return the fixed effective project and local target, retaining all local slashes."""
        if not self.variant_projects:
            return self, target, None
        key = "path" if target.get("kind") == "file" else "name"
        value = target.get(key)
        if target.get("kind") == "assembly" and not value:
            value = next(iter(self.assemblies), "")
        if not isinstance(value, str):
            raise LookupError(f"not a qualified variant target: {target!r}")
        identifier, separator, local = value.partition("/")
        if identifier not in self.variant_projects or (not separator and key != "path"):
            raise LookupError(f"unknown variant target {value!r}")
        return self.variant_projects[identifier], {**target, key: local}, identifier

    def part_binding(self, name: str) -> dict[str, Any]:
        project, target, _ = self.resolve({"kind": "part", "name": name})
        return {
            "entry_target": project.target,
            "arguments": [target["name"]] if project._has_parts else list(project.args),
        }

    def build_bindings(self) -> dict[str, Any]:
        """All effective factories and arguments, used as the cache configuration salt."""
        if self.variant_projects:
            return {identifier: project.build_bindings() for identifier, project in self.variant_projects.items()}
        return {
            "target": self.target,
            "args": self.args,
            "parts": self.parts_target,
            "assemblies": self.assemblies,
            "simulations": self.simulations,
            "env": self.trace_env,
        }

    def source_targets(self) -> list[str]:
        if self.variant_projects:
            return [target for project in self.variant_projects.values() for target in project.source_targets()]
        return [
            self.target,
            *([self.parts_target] if self.parts_target else []),
            *self.assemblies.values(),
            *self.simulations.values(),
        ]

    def catalog(self, *, refresh: bool = False, cancel: threading.Event | None = None) -> _Catalog:
        """One coherent names/folders snapshot. Refreshes serialize discovery and publish only complete results.
        A failed discovery blocks stale reads until a later successful refresh; fixed single-model mode never
        imports anything. Unchanged catalogs keep the same snapshot, but discovery still runs on each refresh."""
        if self.variant_projects:
            catalogs = [
                (identifier, project.catalog(refresh=refresh, cancel=cancel))
                for identifier, project in self.variant_projects.items()
            ]
            names = tuple(f"{identifier}/{name}" for identifier, catalog in catalogs for name in catalog.names)
            folders = {
                f"{identifier}/{name}": [{"name": identifier, "file": False}, *catalog.folders.get(name, [])]
                for identifier, catalog in catalogs
                for name in catalog.names
            }
            return _Catalog(names, names, folders)
        with locked(self._catalog_lock, cancel):
            if refresh and self.parts_target is not None:
                try:
                    parts, folders = (
                        _discover(self.parts_target) if cancel is None else _discover(self.parts_target, cancel=cancel)
                    )
                except _CatalogError as error:
                    self._catalog_error = error
                    raise
                catalog = _Catalog(parts, parts, folders)
                if catalog != self._catalog:
                    self._catalog = catalog
                self._catalog_error = None
            if self._catalog_error is not None:
                raise self._catalog_error
            return self._catalog

    @property
    def parts(self) -> tuple[str, ...] | None:
        return self.catalog().parts

    @property
    def folders(self) -> dict[str, list[FolderStep]]:
        return self.catalog().folders

    @property
    def names(self) -> list[str]:
        return list(self.catalog().names)

    def trace_command(self, name: str) -> list[str]:
        if self.variant_projects:
            project, target, _ = self.resolve({"kind": "part", "name": name})
            return project.trace_command(target["name"])
        args = [name] if self._has_parts else list(self.args)
        return [sys.executable, "-m", "cadiste", "export", self.target, "--", *args]

    def assembly_command(self, name: str) -> list[str]:
        """Build the assembly; with a part list, also name the part each instance is of."""
        if self.variant_projects:
            project, target, _ = self.resolve({"kind": "assembly", "name": name})
            return project.assembly_command(target["name"])
        command = [sys.executable, "-m", "cadiste", "mesh"]
        if self.parts_target is not None:
            command += ["--match-parts", self.parts_target, "--part-build", self.target]
        return [*command, self.assemblies[name]]

    def folder_parts(self, path: str) -> list[str]:
        return self.catalog().folder_parts(path)


def _run(
    command: list[str], environment: dict[str, str] | None = None, *, cancel: threading.Event | None = None
) -> tuple[bytes | None, dict[str, str]]:
    """The command's stdout, or None and the error to report."""
    with tempfile.TemporaryDirectory(prefix="cadiste-bytecode-") as directory:
        # -B alone still reads existing .pyc files. An empty prefix bypasses those too, even for edits whose
        # size and integer-second mtime match. Keep the command layout used to inject --shapes unchanged.
        env = {
            **os.environ,
            **(environment or {}),
            "PYTHONPYCACHEPREFIX": directory,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        try:
            done = run(command, timeout=_TRACE_TIMEOUT, env=env, cancel=cancel)
        except subprocess.TimeoutExpired:
            return None, {"error": f"took over {_TRACE_TIMEOUT} s"}
        except InterruptedError as error:
            return None, {"error": str(error)}
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


EXPORT_FORMATS = {"step": "model/step", "3mf": "model/3mf", "stl": "model/stl", "brep": "application/octet-stream"}
"""Supported retained CAD exports and their media types."""


class Builds:
    """What the viewer opens, built through the cache: a part (traced), an assembly, the parts of a folder (both
    only viewed). A target is the frontend's: ``{"kind": "part", "name"}``, ``{"kind": "assembly", "name"}`` (no
    name: the first assembly), ``{"kind": "file", "path"}``."""

    def __init__(self, project: Project, cache: TraceCache, always_fresh: bool):
        self.project = project
        self.cache = cache
        self.always_fresh = always_fresh
        """No cache: successful geometry is still retained for exact captured-generation requests."""
        self.check: Callable[..., dict[str, Any]] | None = None
        self.on_build: Callable[[dict[str, Any], Build], None] | None = None
        self.geometry_lock = threading.Lock()
        from .baselines import Baselines

        self.baselines = Baselines(self)

    def _recipe(
        self, target: dict[str, Any], *, refresh: bool = False, cancel: threading.Event | None = None,
        only: list[str] | None = None,
    ) -> tuple[str, str, list[str], dict[str, str] | None, str]:
        """The cache entry's kind and name, the command (a cadiste subcommand: ``[python, -m, cadiste, <sub>, ...]``),
        its environment and what it does; LookupError for a target the project does not have."""
        project, target, variant = self.project.resolve(target)
        catalog = project.catalog(refresh=refresh, cancel=cancel)
        if only is not None and (
            target.get("kind") != "file" or not isinstance(only, list) or not only
            or any(not isinstance(name, str) or not name for name in only)
        ):
            raise ValueError("only requires a nonempty folder part-name allowlist")
        match target:
            case {"kind": "part", "name": str(name)}:
                if name not in catalog.names:
                    raise LookupError(f"unknown part {name!r}")
                qualified = f"{variant}/{name}" if variant else name
                return "trace", qualified, project.trace_command(name), project.trace_env, f"tracing {qualified!r}"
            case {"kind": "assembly", **rest}:
                if not project.assemblies:
                    raise LookupError("this project has no assembly (cadiste serve --assembly)")
                name = rest.get("name") or next(iter(project.assemblies))
                if name not in project.assemblies:
                    raise LookupError(f"unknown assembly {name!r}")
                qualified = f"{variant}/{name}" if variant else name
                return (
                    "assembly", qualified, project.assembly_command(name), None,
                    f"building the assembly {qualified!r}",
                )
            case {"kind": "file", "path": str(path)}:
                names = list(catalog.names) if variant and not path else catalog.folder_parts(path)
                if not names:
                    raise LookupError(f"no parts in {path!r}")
                if only is not None:
                    qualified_names = {f"{variant}/{name}" if variant else name: name for name in names}
                    if unknown := set(only) - qualified_names.keys():
                        raise LookupError(f"parts not captured in this folder: {', '.join(sorted(unknown))}")
                    selected = {qualified_names[name] for name in only}
                    names = [name for name in names if name in selected]
                label = path.rpartition("/")[2] or "Parts"
                command = [sys.executable, "-m", "cadiste", "mesh", "--each", "--label", label, project.target, "--"]
                # Only viewed, not traced: the model's own part cache may serve, so the server's environment.
                qualified = (f"{variant}/{path}" if path else variant) if variant else path
                if only is not None:
                    digest = hashlib.sha256(json.dumps(names).encode()).hexdigest()[:12]
                    return (
                        "file-selection",
                        f"{qualified}:{digest}",
                        [*command, *names],
                        None,
                        f"building selected parts of {qualified!r}",
                    )
                return "file", qualified, [*command, *names], None, f"building {qualified!r}"
        raise LookupError(f"not something to build: {target!r}")

    def get(
        self,
        target: dict[str, Any],
        fresh: bool,
        *,
        refresh: bool = True,
        cancel: threading.Event | None = None,
        notify: bool = True,
        only: list[str] | None = None,
    ) -> Build:
        """The target's build, from the cache unless ``fresh`` (or its source changed); a failed build is not
        cached."""
        kind, name, command, environment, doing = self._recipe(target, refresh=refresh, cancel=cancel, only=only)
        failure: dict[str, str] = {}

        def make(shapes: Path) -> bytes | None:
            before = self.cache.fingerprint()
            invocation = [*command[:4], "--shapes", str(shapes), *command[4:]]
            body, failed = (
                _run(invocation, environment) if cancel is None else _run(invocation, environment, cancel=cancel)
            )
            failure.update(failed)
            if body is None:
                return None
            if before != self.cache.fingerprint():
                failure["error"] = "source changed during the build; request the target again"
                return None
            data = json.loads(body)
            _, local_target, variant = self.project.resolve(target)
            if variant:
                scene = data.get("scene", {})
                scene["part_of"] = {
                    path: f"{variant}/{owner}" for path, owner in scene.get("part_of", {}).items()
                }
                for record in scene.get("instances", {}).values():
                    if record.get("owner"):
                        record["owner"] = f"{variant}/{record['owner']}"
                if local_target.get("kind") == "file":
                    def qualify_leaves(node: dict[str, Any]) -> None:
                        if "parts" in node:
                            for child in node["parts"]:
                                qualify_leaves(child)
                        elif owner := scene["part_of"].get(node["id"]):
                            node["name"] = owner

                    qualify_leaves(scene["data"]["shapes"])
            data["build_id"] = shapes.name.split(".shapes.", 1)[0]
            data["source_fingerprint"] = before
            if only is not None:
                data["selection"] = {"only": only}
            return json.dumps(data, separators=(",", ":")).encode()

        try:
            body, cached, shapes = self.cache.get_or_make(kind, name, make, fresh, cancel=cancel)
        except InterruptedError as error:
            body, cached, shapes = None, False, None
            failure["error"] = str(error)
        if body is None:
            failure["error"] = f"{doing}: {failure.get('error', 'no output')}"
        result = Build(body, cached, shapes, failure)
        if notify and only is None and self.on_build is not None:
            self.on_build(target, result)
        return result

    def latest(self, target: dict[str, Any], *, refresh: bool = True) -> Build | None:
        """The last retained build; snapshots can skip discovery of the live project catalog."""
        kind, name, *_ = self._recipe(target, refresh=refresh)
        file = self.cache.latest(kind, name)
        if file is None:
            return None
        shapes = file.with_suffix(SHAPES_SUFFIX)
        return Build(file.read_bytes(), True, shapes if shapes.is_file() else None, {})

    def captured(self, target: dict[str, Any], build_id: str) -> Build:
        """Read only the named retained generation of this exact target."""
        kind, name, *_ = self._recipe(target, refresh=True)
        path = self.cache.captured(kind, name, build_id)
        if path is None:
            raise _ShapesGoneError("the requested build is unavailable or expired; rebuild explicitly")
        shapes = path.with_suffix(SHAPES_SUFFIX)
        return Build(path.read_bytes(), True, shapes if shapes.is_file() else None, {})


    def _simulation_target(self, target: dict[str, str]) -> str:
        """Resolve only explicitly registered assembly adapters."""
        project, target, _ = self.project.resolve(target)
        project.catalog(refresh=True)
        if target.get("kind") != "assembly":
            raise LookupError("Gazebo export requires an assembly with a simulation adapter")
        name = target.get("name") or next(iter(project.assemblies), "")
        if name not in project.assemblies:
            raise LookupError(f"unknown assembly {name!r}")
        if name not in project.simulations:
            raise LookupError(
                f"no simulation adapter for {name!r}; configure cadiste serve --simulation NAME=module:callable"
            )
        return project.simulations[name]

    def control_config(self, target: dict[str, str]) -> dict[str, Any]:
        adapter = self._simulation_target(target)
        body, failure = _run([sys.executable, "-m", "cadiste", "controls", adapter])
        if body is None:
            detail = failure.get("log", failure.get("error", ""))
            raise RuntimeError(f"could not discover simulation controls: {detail}")
        try:
            config = json.loads(body)
        except ValueError as error:
            raise RuntimeError("simulation controls discovery returned invalid JSON") from error
        if not isinstance(config, dict):
            raise RuntimeError("simulation controls discovery did not return an object")
        return config

    def simulation_scene(self, target: dict[str, str]) -> dict[str, Any]:
        adapter = self._simulation_target(target)
        body, failure = _run([sys.executable, "-m", "cadiste", "simulation-scene", adapter])
        if body is None:
            detail = failure.get("log", failure.get("error", ""))
            raise RuntimeError(f"could not build simulation scene: {detail}")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise RuntimeError("simulation scene did not return an object")
        return data

    def gazebo(self, target: dict[str, str], output: Path) -> dict[str, str]:
        """Build an explicitly configured simulation; CAD geometry alone does not define robot joints."""
        adapter = self._simulation_target(target)
        command = [sys.executable, "-m", "cadiste", "gazebo", "--output", str(output), adapter]
        body, failure = _run(command)
        return failure if body is None else {}


_ID = re.compile(r"[\w-]{1,64}")


class _ExportError(ToolError):
    def __init__(self, details: dict[str, str], status: HTTPStatus = HTTPStatus.INTERNAL_SERVER_ERROR):
        log = "\n".join(details.get("log", "").splitlines()[-40:])
        super().__init__(
            (details.get("error", "export failed") + "\n" + log).strip(), code="export_error", details=details
        )
        self.details = details
        self.status = status


def export_artifact(
    builds: Builds,
    target: dict[str, Any],
    form: str = "step",
    only: list[str] | None = None,
    expected_revision: str | None = None,
    *,
    directory: str = "artifacts",
    hidden: list[str] | None = None,
    group: str | None = None,
    orientation: str | None = None,
    preflight: bool = True,
) -> dict[str, Any]:
    """Write the exact retained generation used by preflight, rejecting edits before publishing."""
    from .cli import _WRITERS
    from .manufacturing import prepare_export

    if form not in EXPORT_FORMATS:
        raise ValueError(f"format must be one of {', '.join(EXPORT_FORMATS)}")
    if type(preflight) is not bool:
        raise ValueError("preflight must be a boolean")
    if only is not None and hidden:
        raise ValueError("only and hidden cannot be combined; use explicit viewer paths alone")
    captured = builds.get(target, False, only=only) if only is not None else builds.get(target, False)
    if captured.body is None:
        raise _ExportError(captured.failure)
    metadata = json.loads(captured.body)
    before = builds.cache.fingerprint()
    if metadata.get("source_fingerprint") != before:
        raise ValueError("source changed while preparing export; rebuild explicitly")
    prepared = prepare_export(
        builds, target, only=only, group=group, orientation=orientation,
        expected_revision=expected_revision, hidden=hidden, analyze=preflight, prepared_build=captured,
    )
    folder = builds.cache.directory / directory
    folder.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w .-]", "_", prepared.stem).lstrip(".") or "model"
    with tempfile.NamedTemporaryFile(prefix=f"{stem}-", suffix=f".{form}", dir=folder, delete=False) as file:
        path = Path(file.name).resolve()
    try:
        with getattr(builds, "geometry_lock", contextlib.nullcontext()):
            if not _WRITERS[form](prepared.shape, path):
                raise _ExportError({"error": f"could not write retained geometry as {form.upper()}"})
        if before != builds.cache.fingerprint():
            raise ValueError("source changed during export; no artifact was published")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {
        "path": str(path),
        "url": f"/api/artifacts/{quote(path.name)}" if directory == "artifacts" else None,
        "source_revision": metadata.get("source_revision"),
        "source_fingerprint": before,
        "build_id": metadata["build_id"],
        "sha256": digest,
        "rebuilt_for_export": False,
        "exported": True,
        "launched": False,
        "import_confirmed": False,
        "preflight": prepared.report if preflight else None,
    }


def export_cncjs(
    builds: Builds,
    target: dict[str, Any],
    machining: dict[str, Any],
    only: list[str] | None = None,
    expected_revision: str | None = None,
    *,
    hidden: list[str] | None = None,
    group: str | None = None,
    orientation: str | None = None,
) -> dict[str, Any]:
    """Publish compensated router profiles for manual CNCjs loading; never contact a machine."""
    from .cncjs import write_gcode
    from .manufacturing import prepare_export

    if not isinstance(target, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in target.items()
    ):
        raise ValueError("target must be an object with string values")
    if only is not None and hidden:
        raise ValueError("only and hidden cannot be combined")
    captured = builds.get(target, False, only=only) if only is not None else builds.get(target, False)
    if captured.body is None:
        raise _ExportError(captured.failure)
    metadata = json.loads(captured.body)
    before = builds.cache.fingerprint()
    if metadata.get("source_fingerprint") != before:
        raise ValueError("source changed while preparing CNCjs export; rebuild explicitly")
    prepared = prepare_export(
        builds, target, only=only, group=group, orientation=orientation,
        expected_revision=expected_revision, hidden=hidden, analyze=False, prepared_build=captured,
    )
    folder = builds.cache.directory / "artifacts"
    folder.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w .-]", "_", prepared.stem).lstrip(".") or "model"
    with tempfile.NamedTemporaryFile(prefix=f"{stem}-", suffix=".nc", dir=folder, delete=False) as file:
        path = Path(file.name).resolve()
    try:
        with getattr(builds, "geometry_lock", contextlib.nullcontext()):
            cutting = write_gcode(prepared.shape, path, machining)
        if before != builds.cache.fingerprint():
            raise ValueError("source changed during CNCjs export; no artifact was published")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {
        "path": str(path),
        "url": f"/api/artifacts/{quote(path.name)}",
        "source_revision": metadata.get("source_revision"),
        "source_fingerprint": before,
        "build_id": metadata["build_id"],
        "sha256": digest,
        "rebuilt_for_export": False,
        "exported": True,
        "launched": False,
        "import_confirmed": False,
        "cutting": cutting,
    }


def open_bambu(
    builds: Builds,
    target: dict[str, Any],
    only: list[str] | None = None,
    expected_revision: str | None = None,
    *,
    hidden: list[str] | None = None,
    group: str | None = None,
    orientation: str | None = None,
    verify_import: bool = False,
) -> dict[str, Any]:
    executable = next(
        (found for name in ("bambustudio", "bambu-studio", "BambuStudio") if (found := shutil.which(name))), None
    )
    if executable is None:
        raise _ExportError(
            {"error": "Bambu Studio was not found on the server's PATH (bambustudio, bambu-studio or BambuStudio)"},
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if type(verify_import) is not bool:
        raise ValueError("verify_import must be a boolean")
    artifact = export_artifact(
        builds, target, "3mf", only, expected_revision, directory="bambu-studio", hidden=hidden,
        group=group, orientation=orientation,
    )
    verification = None
    if verify_import:
        from .bambu_verify import verify_import as verify_native_import

        verification = verify_native_import(artifact["path"], executable=executable)
    try:
        process = subprocess.Popen(
            [executable, artifact["path"]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        Path(artifact["path"]).unlink(missing_ok=True)
        raise _ExportError({"error": f"could not open Bambu Studio: {error}"}) from error
    threading.Thread(target=process.wait, daemon=True).start()
    return {
        **artifact, "launched": True,
        "import_confirmed": verification.get("import_confirmed", False) if verification else False,
        "import_verification": verification,
        "import_scope": "isolated native CLI load/save; not the user's interactive slicer",
    }


class _Handler(BaseHTTPRequestHandler):
    project: Project
    builds: Builds
    measurer: Measurer
    hub: Hub
    agent: Agent
    controls: Controls
    ros_command: tuple[str, ...]
    workspace: Any = None
    workspace_only = False
    workspace_url: str | None = None
    runtime: Any = None
    local_url: str | None = None
    background: BackgroundBuilds | None = None

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        # `latest`: the build just made for an agent, not built again even without a cache.
        fresh = query.get("fresh", ["0"])[0] == "1" or (
            self.builds.always_fresh and query.get("latest", ["0"])[0] != "1"
        )
        if url.path == "/api/runtime":
            self._json(
                {
                    **runtime_info(Path.cwd(), local_url=self.local_url, web_dir=STATIC),
                    "mcp_sessions": self.hub.generations(),
                    "workspace_url": self.workspace_url,
                    "workspace_only": self.workspace_only,
                }
            )
            return
        if url.path == "/api/workspace":
            self._workspace_get(query)
            return
        if url.path == "/api/workspace/browse":
            self._workspace_get(query, browse=True)
            return
        if url.path.startswith("/api/artifacts/"):
            self._artifact(unquote(url.path.removeprefix("/api/artifacts/")))
            return
        if url.path == "/api/simulation/runtime":
            self._simulation_runtime({key: values[0] for key, values in query.items()})
            return
        if url.path == "/api/builds/events":
            self._build_events({key: values[0] for key, values in query.items() if key in ("kind", "name", "path")})
            return
        if url.path == "/api/baselines":
            self._json({"baselines": self.builds.baselines.list()})
            return
        if url.path == "/api/baseline-scene":
            self._built(
                {key: values[0] for key, values in query.items() if key in ("kind", "name", "path")},
                False, query.get("build_id", [None])[0], query.get("baseline", [""])[0],
            )
            return
        if url.path == "/api/project":
            try:
                catalog = self.project.catalog(refresh=True)
            except _CatalogError as error:
                self._json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            else:
                self._json(
                    {
                        "target": self.project.target,
                        "parts": catalog.names,
                        "folders": catalog.folders,
                        "assemblies": list(self.project.assemblies),
                        "simulations": list(self.project.simulations),
                        "variants": self.project.variants,
                        "default_variant": self.project.default_variant,
                        "root": Path.cwd().name,
                        "workspace_only": self.workspace_only,
                        "workspace_url": self.workspace_url,
                        "project_root": str(Path.cwd()),
                        "profile_path": self.project.profile_path,
                        "check_sets": {
                            f"{identifier}/{name}": specification
                            for identifier, variant in self.project.variant_projects.items()
                            for name, specification in variant.check_sets.items()
                        } if self.project.variant_projects else self.project.check_sets,
                        "manufacturing": self.project.manufacturing,
                        "manufacturing_by_variant": {
                            identifier: variant.manufacturing
                            for identifier, variant in self.project.variant_projects.items()
                        },
                    }
                )
        elif url.path == "/api/trace":
            self._built(
                {"kind": "part", "name": query.get("part", [""])[0]}, fresh,
                query.get("build_id", [None])[0], query.get("baseline", [None])[0],
            )
        elif url.path == "/api/assembly":
            self._built(
                {"kind": "assembly", "name": query.get("name", [""])[0]}, fresh,
                query.get("build_id", [None])[0], query.get("baseline", [None])[0],
            )
        elif url.path == "/api/file":
            self._built(
                {"kind": "file", "path": query.get("path", [""])[0]}, fresh,
                query.get("build_id", [None])[0], query.get("baseline", [None])[0],
            )
        elif url.path == "/api/export":
            self._export(
                {key: values[0] for key, values in query.items() if key not in {"part", "hidden"}},
                query.get("part"),
                query.get("hidden"),
            )
        elif url.path == "/api/gazebo":
            self._gazebo_export({key: values[0] for key, values in query.items()})
        elif url.path == "/api/simulation/controls":
            self._simulation_controls({key: values[0] for key, values in query.items()})
        elif url.path == "/api/simulation/scene":
            self._simulation_scene({key: values[0] for key, values in query.items()})
        elif url.path == "/api/simulation/feedback":
            self._simulation_feedback({key: values[0] for key, values in query.items()})
        elif url.path == "/api/events" and _ID.fullmatch(tab := query.get("tab", [""])[0]):
            self._events(tab)
        elif url.path.startswith("/api/"):
            self._json({"error": f"no such endpoint {url.path}"}, HTTPStatus.NOT_FOUND)
        else:
            self._static(url.path)

    def _workspace_get(self, query: dict[str, list[str]], *, browse: bool = False) -> None:
        if self.workspace is None:
            self._json({"error": "no workspace is configured"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if not self._local_request():
            self._json({"error": "workspace access requires a local connection"}, HTTPStatus.FORBIDDEN)
            return
        try:
            result = (
                self.workspace.browse(
                    query.get("path", [""])[0],
                    offset=int(query.get("offset", ["0"])[0]),
                    limit=int(query.get("limit", ["100"])[0]),
                )
                if browse
                else self.workspace.snapshot()
            )
            self._json(result)
        except (ValueError, OSError, RuntimeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def _local_request(self) -> bool:
        try:
            host = urlsplit(f"http://{self.headers.get('Host', '')}").hostname
            local_host = host == "localhost" or (host is not None and ipaddress.ip_address(host).is_loopback)
            return local_host and ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _mutation_allowed(self, *, local_only: bool = False) -> bool:
        origin = self.headers.get("Origin")
        if self.headers.get_content_type() != "application/json":
            self._json({"error": "expected application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return False
        if origin is not None:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or parsed.netloc != self.headers.get("Host"):
                self._json({"error": "cross-origin mutations are not allowed"}, HTTPStatus.FORBIDDEN)
                return False
        if local_only and not self._local_request():
            self._json({"error": "workspace changes require a local connection"}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def _workspace_post(self, action: str, request: Any) -> None:
        if not self._mutation_allowed(local_only=True):
            return
        if self.workspace is None:
            self._json({"error": "no workspace is configured"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if not isinstance(request, dict):
            self._json({"error": "expected an object"}, HTTPStatus.BAD_REQUEST)
            return
        if action not in ("create", "register", "open", "stop", "restart"):
            self._json({"error": "unknown workspace action"}, HTTPStatus.NOT_FOUND)
            return
        try:
            self._json(getattr(self.workspace, action)(**request))
        except (ValueError, TypeError, OSError, RuntimeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def _simulation_runtime(self, target: dict[str, str], request: dict[str, Any] | None = None) -> None:
        if self.runtime is None:
            self._json({"error": "runtime management is unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        try:
            if request is None:
                result = self.runtime.status(target)
            else:
                action = request.get("action")
                if action == "stop":
                    result = self.runtime.stop(target)
                elif action in ("start", "relaunch"):
                    gui = request.get("gui", True)
                    if not isinstance(gui, bool):
                        raise ValueError("gui must be a boolean")
                    result = getattr(self.runtime, action)(target, gui=gui)
                else:
                    raise ValueError("action must be start, stop, or relaunch")
            status = (
                HTTPStatus.UNPROCESSABLE_ENTITY
                if request is not None and result.get("status") == "failed"
                else HTTPStatus.OK
            )
            self._json(result, status)
        except (LookupError, ValueError, OSError, RuntimeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def _artifact(self, name: str) -> None:
        if not re.fullmatch(r"[\w .-]+", name) or name.startswith("."):
            self._json({"error": "invalid artifact name"}, HTTPStatus.BAD_REQUEST)
            return
        path = self.builds.cache.directory / "artifacts" / name
        if not path.is_file():
            self._json({"error": "artifact is unavailable"}, HTTPStatus.NOT_FOUND)
            return
        self._send(
            path.read_bytes(),
            EXPORT_FORMATS.get(path.suffix.lstrip("."), "application/octet-stream"),
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
        )

    def _built(
        self, target: dict[str, Any], fresh: bool, build_id: str | None = None, baseline: str | None = None
    ) -> None:
        try:
            build = self.builds.baselines.get(baseline, target) if baseline else (
                self.builds.captured(target, build_id) if build_id else self.builds.get(target, fresh)
            )
            if baseline and build_id and build.body is not None and json.loads(build.body)["build_id"] != build_id:
                raise LookupError("the pinned baseline does not contain the requested build_id")
        except _ShapesGoneError as error:
            self._json({"error": str(error)}, HTTPStatus.GONE)
            return
        except _CatalogError as error:
            self._json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        except LookupError as error:
            # The message itself: str() of a KeyError quotes it.
            self._json({"error": str(error.args[0]) if error.args else str(error)}, HTTPStatus.NOT_FOUND)
            return
        if build.body is None:
            self._json(build.failure, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        headers = {"X-Cadiste-Cache": "hit" if build.cached else "miss"}
        if build.shapes is not None and build.shapes.parent == self.builds.cache.directory:
            headers["X-Cadiste-Shapes"] = build.shapes.name
        self._send(build.body, "application/json", headers=headers)

    def _export(
        self,
        query: dict[str, str],
        parts: list[str] | None,
        hidden: list[str] | None = None,
    ) -> None:
        """Download the same retained, optionally print-oriented geometry as MCP exports."""
        from .measure import MeasureError

        form = query.pop("format", "step")
        group, orientation = query.pop("group", None), query.pop("orientation", None)
        try:
            artifact = export_artifact(
                self.builds, query, form, parts, hidden=hidden, group=group, orientation=orientation,
            )
        except _ExportError as error:
            self._json(error.json(), error.status)
            return
        except _CatalogError as error:
            self._json({"error": str(error), "code": "catalog_error"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        except LookupError as error:
            self._json({"error": str(error), "code": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        except (MeasureError, ValueError, TypeError) as error:
            self._json({"error": str(error), "code": "invalid_arguments"}, HTTPStatus.BAD_REQUEST)
            return
        self._artifact(Path(artifact["path"]).name)

    def _gazebo_export(self, target: dict[str, str]) -> None:
        """Download a relocatable SDF, meshes and ROS 2 launch bundle."""
        with tempfile.TemporaryDirectory(prefix="cadiste-gazebo-") as directory:
            output = Path(directory)
            try:
                failure = self.builds.gazebo(target, output)
            except _CatalogError as error:
                self._json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            except LookupError as error:
                self._json({"error": str(error)}, HTTPStatus.NOT_FOUND)
                return
            if failure:
                self._json(failure, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            body = io.BytesIO()
            with ZipFile(body, "w", ZIP_DEFLATED) as archive:
                for path in sorted(output.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(output))
            self._send(
                body.getvalue(),
                "application/zip",
                headers={"Content-Disposition": 'attachment; filename="cadiste-gazebo.zip"'},
            )

    def _simulation_controls(self, target: dict[str, str]) -> None:
        try:
            config = self.controls.describe(target)
        except (InvalidConfigError, _CatalogError) as error:
            self._json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except LookupError as error:
            self._json({"error": str(error)}, HTTPStatus.NOT_FOUND)
        except (OSError, RuntimeError) as error:
            self._json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            self._json(config)

    def _simulation_snapshot(self, query: dict[str, str]) -> dict[str, Any] | None:
        config_id = query.pop("config_id", "")
        try:
            return self.controls.snapshot(query, config_id)
        except ConfigConflictError as error:
            self._json({"error": str(error)}, HTTPStatus.CONFLICT)
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        return None

    def _simulation_scene(self, query: dict[str, str]) -> None:
        config = self._simulation_snapshot(query)
        if config is None:
            return
        try:
            data = self.builds.simulation_scene(config["target"])
            expected = {key: value for key, value in config.items() if key not in ("target", "config_id")}
            if data.pop("controls", None) != expected:
                self._json(
                    {"error": "Simulation changed while building its scene; reload controls"},
                    HTTPStatus.CONFLICT,
                )
                return
        except (ValueError, OSError, RuntimeError) as error:
            self._json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except LookupError as error:
            self._json({"error": str(error)}, HTTPStatus.NOT_FOUND)
        else:
            self._json({**data, "config_id": config["config_id"]})

    def _simulation_feedback(self, query: dict[str, str]) -> None:
        config = self._simulation_snapshot(query)
        if config is None:
            return
        with contextlib.ExitStack() as stack:
            try:
                receiver = stack.enter_context(Feedback(self.ros_command, config))
            except (OSError, RuntimeError) as error:
                self._json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                for snapshot in receiver.events():
                    if self.runtime is not None:
                        self.runtime.observe_feedback(config["target"], snapshot, config_id=config["config_id"])
                    body = json.dumps(snapshot, allow_nan=False)
                    self.wfile.write(f"event: feedback\ndata: {body}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _simulation_command(self, request: Any) -> None:
        if self.headers.get_content_type() != "application/json":
            self._json({"error": "expected application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        if (
            not isinstance(request, dict)
            or not isinstance(target := request.get("target"), dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in target.items())
            or not isinstance(config_id := request.get("config_id"), str)
            or not isinstance(stop_all := request.get("stop_all", False), bool)
        ):
            self._json({"error": "expected target, config_id and command values or stop_all"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            result = self.controls.send(target, config_id, request.get("values"), stop_all=stop_all)
        except ConfigConflictError as error:
            self._json({"error": str(error)}, HTTPStatus.CONFLICT)
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except LookupError as error:
            self._json({"error": str(error)}, HTTPStatus.NOT_FOUND)
        except (OSError, RuntimeError) as error:
            self._json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
        else:
            if result["failed"]:
                failures = "\n".join(f"{joint}: {error}" for joint, error in result["failed"].items())
                self._json(
                    {**result, "error": "Some ROS 2 commands failed; see the per-joint errors", "log": failures},
                    HTTPStatus.BAD_GATEWAY,
                )
            else:
                self._json(result)

    def _gazebo_open(self, request: Any) -> None:
        """Launch ROS 2 on the server machine with a retained, independently exported model."""
        if self.headers.get_content_type() != "application/json":
            self._json({"error": "expected application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        if (
            not isinstance(request, dict)
            or not isinstance(target := request.get("target"), dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in target.items())
        ):
            self._json({"error": "expected a target object with string values"}, HTTPStatus.BAD_REQUEST)
            return
        self._simulation_runtime(target, {"action": "start", "gui": request.get("gui", True)})

    def _bambu_studio(self, request: Any) -> None:
        """Export retained geometry-only 3MF, then start the server machine's local slicer."""
        # A JSON-only POST prevents a cross-origin HTML form from launching a local application.
        if self.headers.get_content_type() != "application/json":
            self._json({"error": "expected application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        if not isinstance(request, dict) or not isinstance(target := request.get("target"), dict):
            self._json({"error": "expected a target object"}, HTTPStatus.BAD_REQUEST)
            return
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in target.items()):
            self._json({"error": "target values must be strings"}, HTTPStatus.BAD_REQUEST)
            return
        parts = request.get("parts")
        if parts is not None and (
            not isinstance(parts, list) or not parts or not all(isinstance(part, str) for part in parts)
        ):
            self._json({"error": "parts must be a non-empty list of part names"}, HTTPStatus.BAD_REQUEST)
            return
        hidden = request.get("hidden")
        if hidden is not None and (
            not isinstance(hidden, list) or not all(isinstance(path, str) and path for path in hidden)
        ):
            self._json({"error": "hidden must be a list of non-empty viewer paths"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            result = open_bambu(
                self.builds, target, parts, request.get("expected_revision"), hidden=hidden,
                group=request.get("group"), orientation=request.get("orientation"),
                verify_import=request.get("verify_import", False),
            )
        except _ExportError as error:
            self._json(error.details, error.status)
        except LookupError as error:
            self._json({"error": str(error)}, HTTPStatus.NOT_FOUND)
        except (ValueError, OSError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        else:
            self._json(result)

    def _build_events(self, target: dict[str, str]) -> None:
        if self.background is None:
            self._json({"error": "background builds are unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        stream = self.background.events(target)
        try:
            for snapshot in stream:
                chunk = ": keepalive\n\n" if snapshot is None else f"event: builds\ndata: {json.dumps(snapshot)}\n\n"
                self.wfile.write(chunk.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            stream.close()

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
        if not self._mutation_allowed():
            return
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if not 0 <= size <= 8 * 1024 * 1024:
                raise ValueError("request body exceeds 8 MiB")
            request = json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, OverflowError):
            self._json({"error": "the body is not JSON"}, HTTPStatus.BAD_REQUEST)
            return
        steps = url.path.removeprefix("/api/").split("/")
        match steps:
            case ["workspace", action]:
                self._workspace_post(action, request)
            case ["simulation", "runtime"] if isinstance(request, dict) and isinstance(request.get("target"), dict):
                self._simulation_runtime(request["target"], request)
            case ["check"]:
                self._check(request)
            case ["verify"] if isinstance(request, dict):
                from .verification import verify

                try:
                    self._json(
                        verify(
                            self.builds,
                            request.get("targets", []),
                            fresh=request.get("fresh", False),
                            checks=request.get("checks"),
                            expected_revision=request.get("expected_revision"),
                            baseline=request.get("baseline"),
                            check_sets=request.get("check_sets"),
                            assertions=request.get("assertions"),
                        )
                    )
                except (ValueError, TypeError, LookupError) as error:
                    self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            case ["export"] if isinstance(request, dict):
                from .measure import MeasureError

                try:
                    options = {key: value for key, value in request.items() if key != "format"}
                    self._json(export_artifact(self.builds, form=request.get("format", "step"), **options))
                except _ExportError as error:
                    self._json(error.json(), error.status)
                except (MeasureError, ValueError, TypeError, LookupError) as error:
                    self._json({"error": str(error), "code": "invalid_arguments"}, HTTPStatus.BAD_REQUEST)
            case ["cncjs"] if isinstance(request, dict):
                from .measure import MeasureError

                try:
                    self._json(export_cncjs(self.builds, **request))
                except _ExportError as error:
                    self._json(error.json(), error.status)
                except (MeasureError, ValueError, TypeError, LookupError) as error:
                    self._json({"error": str(error), "code": "invalid_arguments"}, HTTPStatus.BAD_REQUEST)
            case ["baselines"] if isinstance(request, dict):
                try:
                    self._json(self.agent.baseline("browser", **request).data)
                except (ToolError, ValueError, TypeError, LookupError) as error:
                    body = error.json() if isinstance(error, ToolError) else {
                        "error": str(error), "code": "invalid_arguments", "details": {},
                    }
                    self._json(body, HTTPStatus.BAD_REQUEST)
            case ["manufacturing", "preflight"] if isinstance(request, dict):
                from .manufacturing import preflight
                from .measure import MeasureError

                try:
                    self._json(preflight(self.builds, **request))
                except (MeasureError, ValueError, TypeError, LookupError) as error:
                    self._json({"error": str(error), "code": "geometry_error"}, HTTPStatus.UNPROCESSABLE_ENTITY)
            case ["measure"]:
                self._measure(request)
            case ["bambu-studio"]:
                self._bambu_studio(request)
            case ["gazebo"]:
                self._gazebo_open(request)
            case ["simulation", "command"]:
                self._simulation_command(request)
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
                session = self.hub.open_session(
                    str(client or "agent"),
                    request.get("skill_digest") if isinstance(request, dict) else None,
                    request.get("mcp_generation") if isinstance(request, dict) else None,
                )
                self._json({
                    "id": session.id, "code": session.code, "workspace_only": self.workspace_only,
                    "project": {
                        "root": str(Path.cwd()), "profile_path": self.project.profile_path,
                        "target": self.project.target, "variants": self.project.variants,
                    },
                })
            case ["agent", session, "heartbeat"]:
                try:
                    self._json(self.hub.heartbeat(session))
                except KeyError:
                    self._json({"error": "no such session", "code": "not_found"}, HTTPStatus.NOT_FOUND)
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
            self._json({"error": str(error), "code": "geometry_error"}, HTTPStatus.UNPROCESSABLE_ENTITY)

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
            self.hub.heartbeat(session)
        except KeyError:
            # The server restarted since the bridge opened its session: it opens another.
            self._json({"error": "no such session"}, HTTPStatus.NOT_FOUND)
            return
        try:
            self._json(self.agent.call(session, tool, arguments).json())
        except ToolError as error:
            self._json(error.json(), HTTPStatus.UNPROCESSABLE_ENTITY)

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
        if self.path.startswith("/api/") and not self.path.startswith(
            ("/api/events", "/api/tabs/", "/api/builds/events")
        ):
            super().log_message(format, *args)

    def _check(self, request: Any) -> None:
        from .measure import MeasureError

        if not isinstance(request, dict) or not isinstance(request.get("shapes"), str):
            self._json({"error": "expected shapes file and explicit instance pairs"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            self._json(
                self.measurer.check(
                    request["shapes"],
                    request.get("pairs", []),
                    **{
                        key: request[key]
                        for key in ("clearance", "ignore", "samples", "tolerance", "volume_tolerance", "contact")
                        if key in request
                    },
                )
            )
        except _ShapesGoneError as error:
            self._json({"error": str(error)}, HTTPStatus.GONE)
        except (MeasureError, ValueError, TypeError) as error:
            self._json({"error": str(error), "code": "geometry_error"}, HTTPStatus.UNPROCESSABLE_ENTITY)


def serve(
    project: Project | None,
    host: str,
    port: int,
    cache: TraceCache | None = None,
    lockfile: Path | None = None,
    *,
    ros_command: tuple[str, ...] = ("ros2",),
    workspace_root: Path | None = None,
    workspace_url: str | None = None,
    ready_file: Path | None = None,
    ready_token: str | None = None,
) -> None:
    """Serve a model or the workspace launcher; publish readiness only after the socket is bound."""
    from .runtime import RuntimeManager
    from .workspace import Workspace

    workspace_only = project is None
    if project is None:
        project = Project("", (), ())
    with contextlib.ExitStack() as stack:
        if threading.current_thread() is threading.main_thread():

            def terminate(_signum: int, _frame: Any) -> None:
                raise KeyboardInterrupt

            previous_signal = signal.signal(signal.SIGTERM, terminate)
            stack.callback(signal.signal, signal.SIGTERM, previous_signal)
        if cache is None:
            directory = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="cadiste-")))
            roots = source_roots(project.source_targets()) if not workspace_only else []
            salt = json.dumps(project.build_bindings(), sort_keys=True)
            entries, where = TraceCache(directory, roots, salt), "no trace cache"
        else:
            entries, where = cache, f"traces kept in {cache.directory}"
        builds = Builds(project, entries, always_fresh=cache is None)
        background = None if workspace_only else BackgroundBuilds(builds)
        if background is not None:
            builds.on_build = background.completed
            stack.callback(background.close)
        measurer = Measurer(entries.directory)
        builds.check = measurer.check
        builds.geometry_lock = measurer._lock
        hub = Hub(entries.directory / "hub-outcomes.json")
        controls = Controls(builds.control_config, ros_command=ros_command)
        runtime = RuntimeManager(builds, controls)
        workspace = Workspace((workspace_root or Path.cwd().parent).resolve())
        stack.callback(runtime.close)
        stack.callback(workspace.close)
        agent = Agent(hub, builds, measurer, project, Path.cwd())
        stack.callback(agent.close)
        agent.controls, agent.runtime = controls, runtime
        agent.export_callback = lambda target, **options: export_artifact(builds, target, **options)
        agent.bambu_callback = lambda target, **options: open_bambu(builds, target, **options)
        agent.cncjs_callback = lambda target, **options: export_cncjs(builds, target, **options)
        attributes = {
            "project": project,
            "builds": builds,
            "background": background,
            "measurer": measurer,
            "hub": hub,
            "agent": agent,
            "controls": controls,
            "ros_command": ros_command,
            "runtime": runtime,
            "workspace": workspace,
            "workspace_only": workspace_only,
            "workspace_url": workspace_url,
        }
        handler = type("Handler", (_Handler,), attributes)
        threading.Thread(target=importlib.import_module, args=(f"{__package__}.measure",), daemon=True).start()
        with ThreadingHTTPServer((host, port), handler) as server:
            local_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
            url = f"http://{local_host}:{server.server_port}"
            agent.local_url = url
            handler.local_url = url
            workspace.launcher_url = workspace_url or url
            handler.workspace_url = workspace_url or url
            if lockfile is not None:
                lockfile.parent.mkdir(parents=True, exist_ok=True)
                lockfile.write_text(json.dumps({"url": url, "pid": os.getpid()}))
                stack.callback(_remove_lockfile, lockfile)
            if ready_file is not None:
                ready_file.parent.mkdir(parents=True, exist_ok=True)
                staged = ready_file.with_suffix(".tmp")
                staged.write_text(json.dumps({"token": ready_token, "pid": os.getpid(), "url": url}))
                staged.replace(ready_file)
            print(
                f"cadiste: {len(project.names)} part(s) from {project.target or 'workspace'}, {where}, on {url}",
                flush=True,
            )
            if workspace_only:
                workspace.restore()
            if background is not None:
                background.start()
            with contextlib.suppress(KeyboardInterrupt):
                server.serve_forever()


def _remove_lockfile(lockfile: Path) -> None:
    """Unless another server has written it since."""
    with contextlib.suppress(OSError, ValueError):
        if json.loads(lockfile.read_text()).get("pid") == os.getpid():
            lockfile.unlink()
