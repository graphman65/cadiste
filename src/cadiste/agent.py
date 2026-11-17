"""What the tools of ``cadiste mcp`` do, run by ``cadiste serve``: the bridge only forwards each call here, with its
session, and hands the answer (text, and images for screenshots) back to the agent.

Answers are text for a model to read (``cadiste.context``): ids in brackets (``[o7]``, ``[b2]``, an instance's
path) are what the other tools take.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import context
from .capture import CaptureError, PrivateRenderer
from .components import CodeModel
from .diagnostics import runtime_info
from .hub import Hub, HubError
from .measure import MeasureError
from .references import SelectionItem, Snapshot, SnapshotError, selection_items

if TYPE_CHECKING:
    from .server import Build, Builds, Measurer, Project

_LINK_WAIT = 60.0
"""Seconds a call waits for the user to link a tab."""
_OPEN_WAIT = 300.0
"""Seconds a tab gets to load and render a build (a large assembly takes a while to draw)."""
_VIEWS = ("iso", "front", "rear", "left", "right", "top", "bottom")


class ToolError(Exception):
    """A tool call that failed; the message tells the agent what to do."""

    def __init__(self, message: str, *, code: str = "tool_error", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def json(self) -> dict[str, Any]:
        return {"error": str(self), "code": self.code, "details": self.details}


@dataclass
class Answer:
    text: str
    images: list[dict[str, str]] = field(default_factory=list)
    """``{"data": <base64 PNG>, "view": <direction>}``."""
    data: dict[str, Any] | None = None
    """Structured code-navigation result, also rendered in ``text``."""

    def json(self) -> dict[str, Any]:
        result: dict[str, Any] = {"text": self.text, "images": self.images}
        if self.data is not None:
            result["data"] = self.data
        return result


def _target(part: str | None, view: str | None) -> dict[str, Any] | None:
    if part:
        return {"kind": "part", "name": part}
    if view == "assembly":
        return {"kind": "assembly"}  # the first one
    if view and view.startswith("assembly:"):
        return {"kind": "assembly", "name": view.removeprefix("assembly:")}
    if view:
        return {"kind": "file", "path": view}
    return None


def _failure(build: Build) -> str:
    """A failed build's error and the end of its log (the traceback)."""
    tail = "\n".join(build.failure.get("log", "").strip().splitlines()[-40:])
    return f"{build.failure.get('error', 'the build failed')}\n{tail}".strip()


def _target_text(target: dict[str, Any] | None) -> str:
    match target:
        case {"kind": "part", "name": name}:
            return f"part {name!r}"
        case {"kind": "assembly", "name": str(name)} if name:
            return f"the assembly {name!r}"
        case {"kind": "assembly"}:
            return "the assembly"
        case {"kind": "file", "path": path}:
            return f"the parts of {path!r}"
        case _:
            return "nothing"


class Agent:
    def __init__(self, hub: Hub, builds: Builds, measurer: Measurer, project: Project, root: Path):
        self.hub = hub
        self.builds = builds
        self.measurer = measurer
        self.project = project
        self.root = root
        self.snapshots = Snapshot(builds, measurer, project, root)
        self.local_url: str | None = None
        self.controls: Any = None
        self.runtime: Any = None
        self.export_callback: Callable[..., dict[str, Any]] | None = None
        self.bambu_callback: Callable[..., dict[str, Any]] | None = None
        self.cncjs_callback: Callable[..., dict[str, Any]] | None = None
        self._private: PrivateRenderer | None = None
        self._agent_states: dict[str, dict[str, Any]] = {}
        self.tools: dict[str, Callable[..., Answer]] = {
            "status": self.status,
            "list_parts": self.list_parts,
            "feature_tree": self.feature_tree,
            "describe": self.describe,
            "walk_components": self.walk_components,
            "inspect_component": self.inspect_component,
            "follow_parameter": self.follow_parameter,
            "find_related_components": self.find_related_components,
            "measure": self.measure,
            "show": self.show,
            "select": self.select,
            "highlight": self.highlight,
            "set_view": self.set_view,
            "set_visibility": self.set_visibility,
            "screenshot": self.screenshot,
            "request_outcome": self.request_outcome,
            "saved_views": self.saved_views,
            "set_section": self.set_section,
            "set_explode": self.set_explode,
            "check_geometry": self.check_geometry,
            "verify_targets": self.verify_targets,
            "suggest_affected_targets": self.suggest_affected_targets,
            "describe_assembly": self.describe_assembly,
            "inspect_simulation": self.inspect_simulation,
            "export_target": self.export_target,
            "open_bambu": self.open_bambu,
            "export_cncjs": self.export_cncjs,
            "baseline": self.baseline,
            "compare_builds": self.compare_builds,
            "renderer_state": self.renderer_state,
            "edit_context": self.edit_context,
            "manufacturing_preflight": self.manufacturing_preflight,
        }

    def call(self, session: str, tool: str, arguments: dict[str, Any]) -> Answer:
        function = self.tools.get(tool)
        if function is None:
            raise ToolError(f"no tool {tool!r}", code="not_found", details={"tool": tool})
        try:
            return function(session, **arguments)
        except ToolError:
            raise
        except (TypeError, ValueError) as error:
            raise ToolError(str(error), code="invalid_arguments", details={"tool": tool}) from error
        except MeasureError as error:
            raise ToolError(str(error), code="geometry_error", details={"tool": tool}) from error
        except LookupError as error:
            raise ToolError(str(error).strip("'\""), code="not_found", details={"tool": tool}) from error
        except CaptureError as error:
            raise ToolError(str(error), code="capture_error", details={"tool": tool}) from error
        except (HubError, SnapshotError, OSError, RuntimeError) as error:
            raise ToolError(str(error).strip("'\""), code="runtime_error", details={"tool": tool}) from error

    # ---- helpers ----

    def _tab(self, session: str) -> str:
        return self.hub.linked_tab(session, _LINK_WAIT)

    def _built(self, target: dict[str, Any], build_id: str | None = None) -> tuple[dict[str, Any], Path | None]:
        """Use an exact retained renderer generation when supplied; never replace it with latest."""
        build = (
            self.builds.captured(target, build_id)
            if build_id
            else self.builds.latest(target) or self.builds.get(target, False)
        )
        if build.body is None:
            raise ToolError(_failure(build))
        return json.loads(build.body), build.shapes

    def _open(self, session: str) -> tuple[str, dict[str, Any]]:
        """The linked tab and its state; an error when it shows nothing built yet."""
        tab = self._tab(session)
        state = self.hub.tab_state(tab)
        if not state.get("target"):
            raise ToolError("the linked tab shows nothing: open a part with show(part=...)")
        return tab, state

    def _paths(self, text: str, trace: dict[str, Any]) -> str:
        return f"{text}\n\n{context.viewer_paths(trace)}\nFiles are relative to {self.root}."

    def _private_renderer(self) -> PrivateRenderer:
        if self.local_url is None:
            raise ToolError("private rendering is unavailable until the local Cadiste HTTP server is bound")
        if self._private is None:
            self._private = PrivateRenderer(self.local_url, hub=self.hub)
        return self._private

    @staticmethod
    def _identity(state: dict[str, Any]) -> dict[str, Any]:
        if not state.get("target") or not state.get("build_id"):
            raise ToolError("renderer has no exact build identity; open it with show first")
        return {"target": state["target"], "build_id": state["build_id"]}

    def _viewer_state(self, session: str, viewer: str) -> tuple[str | None, dict[str, Any]]:
        if viewer == "linked":
            return self._open(session)
        if viewer != "agent":
            raise ToolError("viewer must be 'agent' (private) or 'linked' (explicit user tab)")
        state = self._agent_states.get(session)
        if state is None:
            raise ToolError("no private target is open: pass target/part/view, or call show first")
        return None, state

    def _render_call(self, session: str, viewer: str, method: str, params: dict[str, Any]) -> Any:
        tab, state = self._viewer_state(session, viewer)
        guarded = {**params, "expected": self._identity(state)}
        if tab is not None:
            return self.hub.call(tab, method, guarded, _OPEN_WAIT)
        return self._private_renderer().call(session, method, guarded, _OPEN_WAIT)

    def request_outcome(self, session: str, request_id: str) -> Answer:
        record = self.hub.outcome(request_id)
        value = record.get("value")
        if isinstance(value, dict) and isinstance(value.get("images"), list):
            images = value["images"]
            record = {**record, "value": {**value, "images": [{"view": image.get("view")} for image in images]}}
            return Answer(json.dumps(record, indent=2), images=images, data=record)
        return self._code_answer(record)

    def close(self) -> None:
        if self._private is not None:
            self._private.close()
            self._private = None
        self._agent_states.clear()

    # ---- tools ----

    def status(self, session: str, mcp_skill_digest: str | None = None, mcp_generation: str | None = None) -> Answer:
        info = self.hub.session(session)
        lines = [f"session {info.code} ({info.client})"]
        if info.tab is None:
            waiting = " — a link request is showing in every tab" if info.requesting else ""
            lines.append(f"not linked to a viewer tab{waiting}; private show/screenshot need no link")
        else:
            state = self.hub.tab_state(info.tab)
            status = state.get("status", "none")
            lines.append(f"linked to a viewer tab showing {_target_text(state.get('target'))} ({status})")
            if status == "error" and state.get("error"):
                lines.append(f"build error: {state['error']}")
        private = self._agent_states.get(session)
        if private is not None:
            lines.append(f"private agent renderer: {_target_text(private['target'])}, build {private['build_id']}")
        diagnostic = runtime_info(
            self.root, local_url=self.local_url, skill_digest=mcp_skill_digest, mcp_generation=mcp_generation
        )
        lines.extend(diagnostic["warnings"])
        return Answer("\n".join(lines), data={"runtime": diagnostic, "agent": private, "linked": info.info()})

    def list_parts(self, session: str) -> Answer:
        catalog = self.project.catalog(refresh=True)
        lines = [f"{len(catalog.names)} part(s) of {self.project.target}:"]
        for name in catalog.names:
            folder = "/".join(str(step["name"]) for step in catalog.folders.get(name, []))
            lines.append(f"  {name}" + (f"  (in {folder})" if folder else ""))
        for name, assembly in self.project.assemblies.items():
            lines.append(f"assembly {name!r}: {assembly} — show(view='assembly:{name}')")
        folders = sorted({"/".join(str(s["name"]) for s in steps) for steps in catalog.folders.values() if steps})
        if folders:
            lines.append("folders (show(view=<folder>) views a folder's parts together): " + ", ".join(folders))
        return Answer("\n".join(lines))

    def snapshot(self, tab: str | None, state: dict[str, Any]) -> dict[str, str]:
        """Capture all selected items as standalone Markdown; never store an opaque reference."""
        return self.snapshots.create(tab, state)

    def _part(self, session: str, part: str | None) -> str:
        """The part named, or the one the linked tab shows."""
        if part:
            return part
        _, state = self._open(session)
        target = state["target"]
        if target["kind"] != "part":
            raise ToolError(f"the linked tab shows {_target_text(target)}, not a part: name one (part=...)")
        return target["name"]

    def feature_tree(self, session: str, part: str | None = None) -> Answer:
        name = self._part(session, part)
        trace, _ = self._built({"kind": "part", "name": name})
        return Answer(self._paths(f"{name}: {context.trace_summary(trace)}\n\n{context.feature_tree(trace)}", trace))

    def describe(self, session: str, node: str, part: str | None = None) -> Answer:
        name = self._part(session, part)
        trace, _ = self._built({"kind": "part", "name": name})
        return Answer(self._paths(context.describe(trace, node), trace))

    def _code_model(self, part: str, revision: str | None) -> CodeModel:
        catalog = self.project.catalog(refresh=True)
        if part not in catalog.names:
            raise ToolError(f"unknown part {part!r}; choose: {', '.join(catalog.names)}")
        target = {"kind": "part", "name": part}
        if revision is None:
            build = self.builds.get(target, False)
        else:
            build = self.builds.latest(target)
            if build is None:
                raise ToolError(
                    "no captured build for the requested source revision; omit revision to build current code"
                )
        if build.body is None:
            raise ToolError(_failure(build))
        model = CodeModel(json.loads(build.body), part)
        if revision is not None and revision != model.revision:
            raise ToolError(
                f"source revision mismatch: requested {revision!r}, captured {model.revision!r}; "
                "use the captured revision or explicitly rebuild with show(part=...)"
            )
        return model

    @staticmethod
    def _code_answer(data: dict[str, Any]) -> Answer:
        return Answer(json.dumps(data, indent=2, ensure_ascii=False), data=data)

    def walk_components(
        self,
        session: str,
        part: str | None = None,
        component: str | None = None,
        direction: str = "children",
        depth: int = 1,
        revision: str | None = None,
    ) -> Answer:
        if direction not in ("children", "parents"):
            raise ValueError("direction must be 'children' or 'parents'")
        if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= 8:
            raise ValueError("depth must be an integer from 0 to 8")
        if part is None and component is None:
            if revision is not None:
                raise ValueError("a source revision belongs to a captured part; provide part=...")
            catalog = self.project.catalog(refresh=True)
            folders = sorted(
                {
                    "/".join(str(step["name"]) for step in steps[:end])
                    for steps in catalog.folders.values()
                    for end in range(1, len(steps) + 1)
                }
            )
            return self._code_answer(
                {
                    "kind": "project_catalog",
                    "target": self.project.target,
                    "parts": [
                        {
                            "part": name,
                            "folders": catalog.folders.get(name, []),
                            **self.project.part_binding(name),
                        }
                        for name in catalog.names
                    ],
                    "folders": [{"path": path, "parts": catalog.folder_parts(path)} for path in folders],
                    "assemblies": [
                        {"name": name, "entry_target": target} for name, target in self.project.assemblies.items()
                    ],
                    "source_scope": "project catalog, no part was built",
                }
            )
        name = self._part(session, part)
        return self._code_answer(self._code_model(name, revision).walk(component, direction, depth))

    def inspect_component(self, session: str, part: str, component: str, revision: str | None = None) -> Answer:
        return self._code_answer(self._code_model(part, revision).inspect(component))

    def follow_parameter(
        self,
        session: str,
        part: str,
        component: str,
        expression: str,
        revision: str | None = None,
        limit: int = 20,
        offset: int = 0,
        consumers: str | list[str] | None = None,
        section: str = "summary",
    ) -> Answer:
        return self._code_answer(
            self._code_model(part, revision).follow_parameter(
                component, expression, limit=limit, offset=offset, consumers=consumers, section=section
            )
        )

    def find_related_components(
        self,
        session: str,
        part: str,
        component: str,
        revision: str | None = None,
        limit: int = 20,
        offset: int = 0,
        consumers: str | list[str] | None = None,
        section: str = "summary",
    ) -> Answer:
        return self._code_answer(
            self._code_model(part, revision).related(
                component, limit=limit, offset=offset, consumers=consumers, section=section
            )
        )

    def measure(
        self,
        session: str,
        paths: list[str],
        part: str | None = None,
        target: dict[str, Any] | None = None,
        build_id: str | None = None,
    ) -> Answer:
        if target is None:
            target = {"kind": "part", "name": part} if part else None
        if target is None:
            identity = self._identity(self._open(session)[1])
            target, build_id = dict(identity["target"]), build_id or identity["build_id"]
        built, shapes = self._built(target, build_id)
        if shapes is None:
            raise ToolError("this build kept no shapes to measure on: show(..., fresh=True) builds it again")
        from .measure import MeasureError

        try:
            measurement = self.measurer.measure(shapes.name, paths)
        except MeasureError as error:
            raise ToolError(str(error)) from error
        lines = [f"{entity['path']}: {context.entity_text(entity)}" for entity in measurement["entities"]]
        if measurement["results"]:
            lines.append(context.results_text(measurement["results"]))
        return Answer("\n".join(lines), data={**measurement, "target": target, "build_id": built.get("build_id")})

    def show(
        self,
        session: str,
        part: str | None = None,
        view: str | None = None,
        fresh: bool = True,
        select: str | None = None,
        screenshots: list[str] | None = None,
        viewer: str = "agent",
        target: dict[str, Any] | None = None,
        arrangement: dict[str, Any] | None = None,
        baseline: str | None = None,
    ) -> Answer:
        target = target or _target(part, view)
        if target is not None and target.get("kind") == "assembly" and not target.get("name"):
            target = {"kind": "assembly", "name": next(iter(self.project.assemblies), "")}
        if target is None:
            raise ToolError("name what to show: target=..., part=<name>, or view='assembly:<name>' / <folder>")
        if viewer not in ("agent", "linked"):
            raise ToolError("viewer must be 'agent' or 'linked'")
        started = time.perf_counter()
        previous = self.builds.baselines.get(baseline, target) if baseline else self.builds.latest(target)
        build = self.builds.get(target, fresh)
        build_ms = (time.perf_counter() - started) * 1000
        if build.body is None:
            return Answer(_failure(build))
        built = json.loads(build.body)
        identity = {"target": target, "build_id": built["build_id"]}
        source = "from cache" if build.cached else "built"
        summary = context.trace_summary(built) if target["kind"] == "part" else context.composite_summary(built)
        text = f"{_target_text(target)} {source}: {summary}"
        before = json.loads(previous.body) if previous is not None and previous.body is not None else None
        diff = context.structured_diff(before, built)
        label = f"pinned baseline {baseline}" if baseline else "previous build"
        text += f"\n\nCompared with the {label}:\n" + context.indent(context.compare(before, built))
        params = {
            **identity,
            "select": select,
            "views": self._views(screenshots if screenshots is not None else ["iso"]),
            "arrangement": arrangement,
        }
        if viewer == "agent":
            result = self._private_renderer().call(session, "capture", params, _OPEN_WAIT)
        else:
            result = self.hub.call(self._tab(session), "capture", params, _OPEN_WAIT)
        self._assert_capture(result, identity)
        evidence = {key: value for key, value in result.items() if key != "images"}
        evidence.update(
            source_revision=built.get("source_revision"),
            source_fingerprint=built.get("source_fingerprint"),
            views=[image["view"] for image in result.get("images", [])],
            diff=diff,
            baseline=baseline,
            timings={"build_ms": build_ms, **result.get("timings", {})},
        )
        if viewer == "agent":
            self._agent_states[session] = {
                **identity,
                "source_revision": built.get("source_revision"),
                "source_fingerprint": built.get("source_fingerprint"),
            }
        return Answer(text + f"\nBuild: {identity['build_id']} · {viewer} renderer", result.get("images", []), evidence)

    @staticmethod
    def _views(views: list[str]) -> list[str]:
        if len(views) > 16 or any(view not in (*_VIEWS, "current") for view in views):
            raise ToolError(f"capture at most 16 views from {', '.join(_VIEWS)}, current")
        return views

    @staticmethod
    def _assert_capture(result: dict[str, Any], identity: dict[str, Any]) -> None:
        if result.get("target") != identity["target"] or result.get("build_id") != identity["build_id"]:
            raise ToolError("renderer returned a different target/build; capture discarded")

    def select(self, session: str, items: list[SelectionItem]) -> Answer:
        tab, _ = self._open(session)
        self._render_call(session, "linked", "select", {"items": selection_items(items)})
        return Answer(self.snapshot(tab, self.hub.tab_state(tab))["text"])

    def highlight(self, session: str, paths: list[str]) -> Answer:
        self._render_call(session, "linked", "highlight", {"paths": paths})
        return Answer(f"{len(paths)} path(s) lit" if paths else "highlight cleared")

    def set_view(
        self,
        session: str,
        direction: str | None = None,
        fit: bool | str | None = None,
        ortho: bool | None = None,
        transparent: bool | None = None,
        camera: dict[str, Any] | None = None,
        viewer: str = "linked",
    ) -> Answer:
        if direction is not None and direction not in _VIEWS:
            raise ToolError(f"unknown direction {direction!r}")
        params = {"direction": direction, "fit": fit, "ortho": ortho, "transparent": transparent, "camera": camera}
        result = self._render_call(
            session, viewer, "view", {key: value for key, value in params.items() if value is not None}
        )
        return self._code_answer(result)

    def set_visibility(self, session: str, instances: list[str], visible: bool, viewer: str = "linked") -> Answer:
        return self._code_answer(
            self._render_call(session, viewer, "visibility", {"instances": instances, "visible": visible})
        )

    def screenshot(
        self,
        session: str,
        views: list[str] | None = None,
        target: dict[str, Any] | None = None,
        part: str | None = None,
        view: str | None = None,
        build_id: str | None = None,
        viewer: str = "agent",
        arrangement: dict[str, Any] | None = None,
    ) -> Answer:
        captured: dict[str, Any] | None = None
        explicit = target or _target(part, view)
        if explicit is not None and explicit.get("kind") == "assembly" and not explicit.get("name"):
            explicit = {"kind": "assembly", "name": next(iter(self.project.assemblies), "")}
        if explicit is not None:
            build = (
                self.builds.captured(explicit, build_id)
                if build_id is not None
                else self.builds.latest(explicit) or self.builds.get(explicit, False)
            )
            if build is None:
                raise ToolError("requested build is no longer retained; explicitly build current source with show")
            if build.body is None:
                raise ToolError(_failure(build))
            captured = json.loads(build.body)
            if not isinstance(captured, dict):
                raise ToolError("captured build is not an object; rebuild explicitly")
            identity = {"target": explicit, "build_id": captured["build_id"]}
            params = {**identity, "views": self._views(views or ["iso"]), "arrangement": arrangement}
            if viewer == "agent":
                result = self._private_renderer().call(session, "capture", params, _OPEN_WAIT)
            elif viewer == "linked":
                result = self.hub.call(self._tab(session), "capture", params, _OPEN_WAIT)
            else:
                raise ToolError("viewer must be 'agent' or 'linked'")
        else:
            _, state = self._viewer_state(session, viewer)
            identity = self._identity(state)
            if build_id is not None and build_id != identity["build_id"]:
                raise ToolError("build_id differs from the open renderer; pass its explicit target")
            result = self._render_call(
                session, viewer, "screenshot", {"views": self._views(views or ["current"]), "arrangement": arrangement}
            )
        self._assert_capture(result, identity)
        evidence = {key: value for key, value in result.items() if key != "images"}
        evidence["views"] = [image["view"] for image in result["images"]]
        if captured is not None:
            evidence.update(
                source_revision=captured.get("source_revision"), source_fingerprint=captured.get("source_fingerprint")
            )
        if viewer == "agent":
            self._agent_states[session] = {
                **identity,
                "source_revision": evidence.get("source_revision"),
                "source_fingerprint": evidence.get("source_fingerprint"),
            }
        return Answer(
            f"{_target_text(identity['target'])} · build {identity['build_id']} · {viewer} renderer",
            result["images"],
            evidence,
        )

    def saved_views(
        self,
        session: str,
        action: str = "list",
        id: str | None = None,
        name: str | None = None,
        state: dict[str, Any] | None = None,
        viewer: str = "linked",
    ) -> Answer:
        result = self._render_call(
            session, viewer, "saved_views", {"action": action, "id": id, "name": name, "state": state}
        )
        if viewer == "agent" and action == "restore":
            self._agent_states[session] = self._identity(result)
        return self._code_answer(result)

    def set_section(
        self,
        session: str,
        active: bool,
        plane: dict[str, Any] | None = None,
        offset: float = 0,
        flipped: bool = False,
        viewer: str = "linked",
    ) -> Answer:
        return self._code_answer(
            self._render_call(
                session, viewer, "section", {"active": active, "plane": plane, "offset": offset, "flipped": flipped}
            )
        )

    def set_explode(self, session: str, amount: float | None = None, viewer: str = "linked") -> Answer:
        return self._code_answer(self._render_call(session, viewer, "explode", {"amount": amount}))

    def check_geometry(
        self,
        session: str,
        target: dict[str, Any],
        pairs: list[list[str]],
        clearance: float = 0,
        ignore: list[list[str]] | None = None,
        samples: list[dict[str, Any]] | None = None,
        build_id: str | None = None,
        tolerance: float = 1e-6,
        volume_tolerance: float = 1e-9,
        contact: list[dict[str, Any]] | None = None,
    ) -> Answer:
        build = self.builds.captured(target, build_id) if build_id else self.builds.get(target, False)
        if build is None or build.body is None or build.shapes is None:
            raise ToolError("exact shapes for this generation are unavailable; explicitly rebuild with show")
        result = self.measurer.check(
            build.shapes.name,
            pairs,
            clearance=clearance,
            ignore=ignore,
            samples=samples,
            tolerance=tolerance,
            volume_tolerance=volume_tolerance,
            contact=contact,
        )
        return self._code_answer({**result, "target": target, "build_id": json.loads(build.body)["build_id"]})

    def verify_targets(
        self,
        session: str,
        targets: list[dict[str, Any]],
        fresh: bool = False,
        checks: list[dict[str, Any]] | None = None,
        expected_revision: str | None = None,
        baseline: str | None = None,
        check_sets: list[str] | None = None,
        assertions: list[dict[str, Any]] | None = None,
    ) -> Answer:
        from .verification import verify

        return self._code_answer(
            verify(
                self.builds, targets, fresh=fresh, checks=checks, expected_revision=expected_revision,
                baseline=baseline, check_sets=check_sets, assertions=assertions,
            )
        )

    def describe_assembly(
        self, session: str, name: str, path: str | None = None, limit: int = 50, offset: int = 0
    ) -> Answer:
        from .assembly import describe_assembly

        built, _ = self._built({"kind": "assembly", "name": name})
        return self._code_answer(describe_assembly(built, path, limit=limit, offset=offset))

    def inspect_simulation(self, session: str, target: dict[str, Any]) -> Answer:
        from .runtime import inspect_simulation

        if self.controls is None:
            raise ToolError("simulation controls are unavailable in this server")
        result = inspect_simulation(self.builds, self.controls, target)
        if self.runtime is not None:
            result["runtime"] = self.runtime.status(target)
        return self._code_answer(result)

    def export_target(
        self,
        session: str,
        target: dict[str, Any],
        format: str = "step",
        only: list[str] | None = None,
        expected_revision: str | None = None,
        group: str | None = None,
        orientation: str | None = None,
        preflight: bool = True,
    ) -> Answer:
        if self.export_callback is None:
            raise ToolError("exports are unavailable in this server")
        return self._code_answer(
            self.export_callback(
                target, form=format, only=only, expected_revision=expected_revision,
                group=group, orientation=orientation, preflight=preflight,
            )
        )

    def open_bambu(
        self, session: str, target: dict[str, Any], only: list[str] | None = None,
        expected_revision: str | None = None, group: str | None = None,
        orientation: str | None = None, verify_import: bool = False,
    ) -> Answer:
        if self.bambu_callback is None:
            raise ToolError("Bambu Studio launching is unavailable in this server")
        return self._code_answer(
            self.bambu_callback(
                target, only=only, expected_revision=expected_revision,
                group=group, orientation=orientation, verify_import=verify_import,
            )
        )

    def export_cncjs(
        self, session: str, target: dict[str, Any], machining: dict[str, Any],
        only: list[str] | None = None, expected_revision: str | None = None,
        group: str | None = None, orientation: str | None = None,
    ) -> Answer:
        if self.cncjs_callback is None:
            raise ToolError("CNCjs exports are unavailable in this server")
        return self._code_answer(
            self.cncjs_callback(
                target, machining=machining, only=only, expected_revision=expected_revision,
                group=group, orientation=orientation,
            )
        )

    def suggest_affected_targets(
        self, session: str, changed_files: list[str], targets: list[dict[str, Any]] | None = None
    ) -> Answer:
        from .verification import suggest_affected_targets

        return self._code_answer(suggest_affected_targets(self.builds, changed_files, targets=targets))

    def baseline(
        self, session: str, action: str = "list", targets: list[dict[str, Any]] | None = None,
        id: str | None = None, name: str | None = None,
    ) -> Answer:
        if action == "pin":
            return self._code_answer(self.builds.baselines.pin(targets or [], name=name))
        if action == "list":
            return self._code_answer({"baselines": self.builds.baselines.list()})
        if action == "release" and id:
            return self._code_answer(self.builds.baselines.release(id))
        raise ValueError("baseline action must be pin (targets), list, or release (id)")

    def renderer_state(self, session: str, viewer: str = "agent") -> Answer:
        if viewer == "linked":
            tab, _state = self._open(session)
            result = self.hub.call(tab, "inspect", {}, _OPEN_WAIT)
        elif viewer == "agent":
            result = self._private_renderer().call(session, "inspect", {}, _OPEN_WAIT)
        else:
            raise ValueError("viewer must be agent or linked")
        return self._code_answer(result)

    def compare_builds(
        self, session: str, target: dict[str, Any], baseline: str,
        build_id: str | None = None, viewer: str = "agent", views: list[str] | None = None,
        arrangement: dict[str, Any] | None = None,
    ) -> Answer:
        previous = self.builds.baselines.get(baseline, target)
        current = self.builds.captured(target, build_id) if build_id else self.builds.get(target, False)
        if previous.body is None or current.body is None:
            raise ToolError("comparison requires two successful builds", code="build_error")
        before, after = json.loads(previous.body), json.loads(current.body)
        if viewer not in ("agent", "linked"):
            raise ValueError("viewer must be agent or linked")
        tab = self._tab(session) if viewer == "linked" else None

        def render(method: str, params: dict[str, Any]) -> Any:
            if tab is not None:
                return self.hub.call(tab, method, params, _OPEN_WAIT)
            return self._private_renderer().call(session, method, params, _OPEN_WAIT)

        directions = self._views(views if views is not None else ["current"])
        identity = {"target": target, "build_id": after["build_id"]}
        captured = render("capture", {**identity, "views": [], "arrangement": arrangement})
        self._assert_capture(captured, identity)
        state = render("inspect", {})
        original_camera = {**(arrangement or {}), "camera": state.get("camera"), "fit": False}
        old_identity = {"target": target, "build_id": before["build_id"]}
        images = []
        cameras = {}
        try:
            for direction in directions:
                setup = dict(original_camera)
                if direction != "current":
                    setup.pop("camera", None)
                    setup.update(direction=direction, fit="all")
                prepared = render("capture", {**identity, "views": [], "arrangement": setup})
                self._assert_capture(prepared, identity)
                camera = render("inspect", {}).get("camera")
                cameras[direction] = camera
                fixed = {**(arrangement or {}), "camera": camera, "fit": False}
                # Capture current views only: independently fitting each build would hide dimension changes.
                old = render("capture", {
                    **old_identity, "baseline": baseline, "views": ["current"], "arrangement": fixed,
                })
                self._assert_capture(old, old_identity)
                new = render("capture", {**identity, "views": ["current"], "arrangement": fixed})
                self._assert_capture(new, identity)
                for side, result in (("baseline", old), ("current", new)):
                    images.extend({**image, "view": f"{side}/{direction}"} for image in result.get("images", []))
        finally:
            restored = render("capture", {**identity, "views": [], "arrangement": original_camera})
            self._assert_capture(restored, identity)
        if viewer == "agent":
            self._agent_states[session] = identity
        return Answer(
            context.compare(before, after), images,
            {"baseline": baseline, **identity, "diff": context.structured_diff(before, after),
             "camera": state.get("camera"), "view_cameras": cameras, "same_camera": True},
        )

    def edit_context(self, session: str, requests: list[dict[str, Any]], limit: int = 10) -> Answer:
        if not isinstance(requests, list) or not 1 <= len(requests) <= 32:
            raise ValueError("requests must contain 1-32 component queries")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        results = []
        models: dict[tuple[str, str | None], CodeModel] = {}
        for request in requests:
            if not isinstance(request, dict) or not isinstance(request.get("part"), str):
                raise ValueError("each request requires part and component")
            part, component = request["part"], request.get("component")
            if not isinstance(component, str):
                raise ValueError("each request requires a component reference")
            variants = request.get("variants")
            if variants is not None and (
                not isinstance(variants, list) or not variants
                or any(identifier not in self.project.variant_projects for identifier in variants)
            ):
                raise ValueError("variants must name declared profile variants")
            if variants and self.project.variant_projects:
                prefix, separator, local = part.partition("/")
                local = local if separator and prefix in self.project.variant_projects else part
                parts = [f"{identifier}/{local}" for identifier in variants]
            else:
                parts = [part]
            reports = []
            for name in parts:
                key = (name, request.get("revision"))
                if key not in models:
                    models[key] = self._code_model(*key)
                reports.append(models[key].edit_context(component, request.get("expressions"), limit=limit))
            expressions: dict[str, list[dict[str, Any]]] = {}
            for report in reports:
                for expression in report.get("expressions", []):
                    expressions.setdefault(expression["expression"], []).append({
                        "part": report["part"],
                        "static": {key: expression["static"].get(key) for key in ("status", "value")},
                        "observed": [
                            {key: value.get(key) for key in ("status", "value", "invocation")}
                            for value in expression.get("observed", [])
                        ],
                    })
            differences = []
            for expression, values in expressions.items():
                signatures = [
                    {"static": value["static"], "observed": [
                        {key: observation.get(key) for key in ("status", "value")} for observation in value["observed"]
                    ]} for value in values
                ]
                if any(value != signatures[0] for value in signatures[1:]):
                    differences.append({"expression": expression, "variants": values})
            results.append({"request": request, "reports": reports, "variant_differences": differences})
        return self._code_answer({"results": results, "limit": limit})

    def manufacturing_preflight(
        self, session: str, target: dict[str, Any], only: list[str] | None = None,
        group: str | None = None, orientation: str | None = None, overhang_angle: float = 45,
        expected_revision: str | None = None,
    ) -> Answer:
        from .manufacturing import preflight

        return self._code_answer(preflight(
            self.builds, target, only=only, group=group, orientation=orientation,
            overhang_angle=overhang_angle, expected_revision=expected_revision,
        ))
