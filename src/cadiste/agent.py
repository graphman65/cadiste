"""What the tools of ``cadiste mcp`` do, run by ``cadiste serve``: the bridge only forwards each call here, with its
session, and hands the answer (text, and images for screenshots) back to the agent.

Answers are text for a model to read (``cadiste.context``): ids in brackets (``[o7]``, ``[b2]``, an instance's
path) are what the other tools take.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import context
from .components import CodeModel
from .hub import Hub, HubError
from .references import SelectionItem, Snapshot, SnapshotError, selection_items

if TYPE_CHECKING:
    from .server import Build, Builds, Measurer, Project

_LINK_WAIT = 60.0
"""Seconds a call waits for the user to link a tab."""
_OPEN_WAIT = 300.0
"""Seconds a tab gets to load and render a build (a large assembly takes a while to draw)."""
_TAB_WAIT = 30.0
_VIEWS = ("iso", "front", "rear", "left", "right", "top", "bottom")


class ToolError(Exception):
    """A tool call that failed; the message tells the agent what to do."""


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
        }

    def call(self, session: str, tool: str, arguments: dict[str, Any]) -> Answer:
        function = self.tools.get(tool)
        if function is None:
            raise ToolError(f"no tool {tool!r}")
        try:
            return function(session, **arguments)
        except TypeError as error:
            raise ToolError(f"{tool}: {error}") from error
        except (HubError, LookupError, ValueError, SnapshotError) as error:
            raise ToolError(str(error).strip("'\"")) from error

    # ---- helpers ----

    def _tab(self, session: str) -> str:
        return self.hub.linked_tab(session, _LINK_WAIT)

    def _built(self, target: dict[str, Any]) -> tuple[dict[str, Any], Path | None]:
        """The target's build as the viewer shows it, parsed: the last one made, even if the source changed since
        (``show`` builds anew), so ids and face numbers are the ones the user sees; built when there is none yet. A
        failed build raises with its log."""
        build = self.builds.latest(target) or self.builds.get(target, False)
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

    # ---- tools ----

    def status(self, session: str) -> Answer:
        info = self.hub.session(session)
        lines = [f"session {info.code} ({info.client})"]
        if info.tab is None:
            waiting = " — a link request is showing in every tab" if info.requesting else ""
            lines.append(f"not linked to a viewer tab{waiting}; a tool that needs one asks the user to link it")
        else:
            state = self.hub.tab_state(info.tab)
            status = state.get("status", "none")
            lines.append(f"linked to a viewer tab showing {_target_text(state.get('target'))} ({status})")
            if status == "error" and state.get("error"):
                lines.append(f"build error: {state['error']}")
        return Answer("\n".join(lines))

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
                            "entry_target": self.project.target,
                            "arguments": [name] if catalog.parts is not None else list(self.project.args),
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
    ) -> Answer:
        return self._code_answer(self._code_model(part, revision).follow_parameter(component, expression))

    def find_related_components(
        self,
        session: str,
        part: str,
        component: str,
        revision: str | None = None,
    ) -> Answer:
        return self._code_answer(self._code_model(part, revision).related(component))

    def measure(self, session: str, paths: list[str], part: str | None = None) -> Answer:
        if part:
            target: dict[str, Any] = {"kind": "part", "name": part}
        else:
            target = self._open(session)[1]["target"]
        _, shapes = self._built(target)
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
        return Answer("\n".join(lines))

    def show(
        self,
        session: str,
        part: str | None = None,
        view: str | None = None,
        fresh: bool = True,
        select: str | None = None,
        screenshots: list[str] | None = None,
    ) -> Answer:
        target = _target(part, view)
        if target is None:
            raise ToolError("name what to show: part=<name>, or view='assembly[:<name>]' or view=<folder>")
        kind = target["kind"]
        previous = self.builds.latest(target) if kind == "part" and fresh else None
        build = self.builds.get(target, fresh)
        tab = self._tab_if_any(session)
        if tab is not None:
            self.hub.call(tab, "open", {"target": target, "select": select}, _OPEN_WAIT)
        if build.body is None:
            return Answer(_failure(build))
        built = json.loads(build.body)
        source = "from the cache, the source unchanged since" if build.cached else "built"
        if kind == "part":
            text = f"{_target_text(target)} {source}: {context.trace_summary(built)}"
            if fresh:
                before = json.loads(previous.body) if previous is not None and previous.body is not None else None
                text += "\n\nCompared with the previous build:\n" + context.indent(context.compare(before, built))
        else:
            text = f"{_target_text(target)} {source}: {context.composite_summary(built)}"
        if tab is None:
            text += "\n\n(no viewer tab linked: link one to see it and get screenshots)"
            return Answer(text)
        images = self._screenshots(tab, screenshots if screenshots is not None else ["iso"])
        return Answer(text, images)

    def _tab_if_any(self, session: str) -> str | None:
        try:
            return self._tab(session)
        except HubError:
            return None

    def _screenshots(self, tab: str, views: list[str]) -> list[dict[str, str]]:
        """One image per view, in order: "current" is the camera as the tab has it."""
        unknown = [view for view in views if view not in _VIEWS and view != "current"]
        if unknown:
            raise ToolError(f"unknown view(s) {unknown}: use {', '.join(_VIEWS)} or 'current'")
        images: list[dict[str, str]] = []
        for view in views:
            # The tab takes the current camera as an empty list of views.
            result = self.hub.call(tab, "screenshot", {"views": [] if view == "current" else [view]}, _TAB_WAIT)
            images += result.get("images", [])
        return images

    def select(self, session: str, items: list[SelectionItem]) -> Answer:
        tab, _ = self._open(session)
        self.hub.call(tab, "select", {"items": selection_items(items)}, _TAB_WAIT)
        return Answer(self.snapshot(tab, self.hub.tab_state(tab))["text"])

    def highlight(self, session: str, paths: list[str]) -> Answer:
        tab, _ = self._open(session)
        self.hub.call(tab, "highlight", {"paths": paths}, _TAB_WAIT)
        return Answer(f"{len(paths)} path(s) lit" if paths else "highlight cleared")

    def set_view(
        self,
        session: str,
        direction: str | None = None,
        fit: bool | None = None,
        ortho: bool | None = None,
        transparent: bool | None = None,
    ) -> Answer:
        if direction is not None and direction not in _VIEWS:
            raise ToolError(f"unknown direction {direction!r}: use {', '.join(_VIEWS)}")
        tab, _ = self._open(session)
        given = {"fit": fit, "ortho": ortho, "transparent": transparent}
        params = {"direction": direction, **given}
        self.hub.call(tab, "view", {key: value for key, value in params.items() if value is not None}, _TAB_WAIT)
        return Answer("view set")

    def set_visibility(self, session: str, instances: list[str], visible: bool) -> Answer:
        tab, state = self._open(session)
        if state["target"]["kind"] == "part":
            raise ToolError("instances are shown and hidden in the assembly or a folder's view, not in a part")
        self.hub.call(tab, "visibility", {"instances": instances, "visible": visible}, _TAB_WAIT)
        return Answer(f"{len(instances)} instance(s) {'shown' if visible else 'hidden'}")

    def screenshot(self, session: str, views: list[str] | None = None) -> Answer:
        tab, state = self._open(session)
        images = self._screenshots(tab, views or ["current"])
        return Answer(f"{_target_text(state['target'])}: {len(images)} view(s)", images)
