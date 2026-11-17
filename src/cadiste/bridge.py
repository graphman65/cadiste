"""``cadiste mcp``: an MCP server on stdio for a coding agent, one per agent session, forwarding every tool to the
running ``cadiste serve`` (``cadiste.agent`` does the work there). It finds the server through the lockfile
``cadiste serve`` writes in its cache directory, looked for from the working directory up, or ``--url``.

The ``cadiste`` skill (how to use the tools) is served with the MCP Skills extension
(``io.modelcontextprotocol/skills``) as ``skill://cadiste/SKILL.md``, and summed up in the server's instructions.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.extension import Extension, MethodBinding, ResourceBinding
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resources import TextResource
from mcp_types import CallToolResult, ImageContent, PaginatedRequestParams, RequestParams, TextContent
from pydantic import Field

from .diagnostics import source_generation
from .references import SelectionItem

SKILL = Path(__file__).resolve().parent / "skills" / "cadiste" / "SKILL.md"
SKILL_URI = "skill://cadiste/SKILL.md"
LOCKFILE = Path(".cache/cadiste/serve.json")
"""Where ``cadiste serve`` says where it listens, under the directory it runs in (its default ``--cache-dir``)."""
DEFAULT_URL = "http://127.0.0.1:8123"
_TIMEOUT = 900
"""Seconds a call may take: waiting for a link, building a part, a tab drawing a large assembly."""

INSTRUCTIONS = """\
cadiste shows a build123d model in the user's browser as a CAD feature tree: operations in order, their arguments,
the call site in the source, and which operation made each face. The user can paste a self-contained Markdown
snapshot beginning '# Cadiste selection v1': read it directly, without a lookup. It captures all selected items,
their geometry, build identities and source provenance at copy time, not live state. You edit the model's Python
source yourself, then call show(part=...) to rebuild it. show/screenshot use a private managed renderer by default,
without linking or touching the user's browser; viewer='linked' explicitly opts into the linked tab. Other linked
controls require user-approved linking. Captures identify the exact target/build; source revisions are separate.
Read skill://cadiste/SKILL.md for the workflow."""

Direction = Literal["iso", "front", "rear", "left", "right", "top", "bottom"]
Content = list[TextContent | ImageContent]
PartName = Annotated[str | None, Field(description="Part name (list_parts); default: the part the linked tab shows")]


def find_url(start: Path) -> str:
    """The URL of the ``cadiste serve`` running for ``start`` or a directory above it; the default port if none."""
    for directory in [start, *start.parents]:
        lockfile = directory / LOCKFILE
        try:
            return str(json.loads(lockfile.read_text())["url"])
        except (OSError, ValueError, KeyError):
            continue
    return DEFAULT_URL


class _UnknownSessionError(Exception):
    """The server does not know the session: it restarted since the session was opened."""


class _ApplicationError(Exception):
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        super().__init__(str(payload["error"]))


class Bridge:
    """One MCP client's project selection and independent endpoint sessions."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self._initial_url = self.url
        self._workspace_url: str | None = None
        self._selected: dict[str, Any] | None = None
        self._initial_project: dict[str, Any] | None = None
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._routing = threading.RLock()
        self._closed = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self.skill_digest = hashlib.sha256(SKILL.read_bytes()).hexdigest()
        self.mcp_generation = source_generation()

    def _request(
        self, url: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = _TIMEOUT,
    ) -> Any:
        request = urllib.request.Request(
            f"{url}{path}",
            None if body is None else json.dumps(body).encode(),
            {"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read())
            except ValueError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                if error.code == 404 and payload["error"] == "no such session":
                    raise _UnknownSessionError from error
                raise _ApplicationError({**payload, "code": payload.get("code", "application_error")}) from error
            raise ToolError(f"cadiste HTTP transport error at {url}: HTTP {error.code}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ToolError(
                f"cadiste transport error: server is not reachable at {url} ({error}). "
                "Start cadiste serve in the project, reopen its workspace project, or set cadiste mcp --url."
            ) from error

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        return self._request(self.url, path, body)

    def _heartbeat(self) -> None:
        while not self._closed.wait(20):
            with self._lock:
                sessions = list(self._sessions.items())
            for url, session in sessions:
                if self._closed.is_set():
                    return
                # Failed heartbeats age out; never fabricate liveness or reopen sessions.
                with contextlib.suppress(ToolError, _ApplicationError, _UnknownSessionError):
                    self._request(url, f"/api/agent/{session['id']}/heartbeat", {}, timeout=5)

    def _open(self, client: str) -> str:
        with self._lock:
            if self.url not in self._sessions:
                self._sessions[self.url] = self._post(
                    "/api/agent/sessions",
                    {"client": client, "skill_digest": self.skill_digest, "mcp_generation": self.mcp_generation},
                )
                if self._heartbeat_thread is None:
                    self._heartbeat_thread = threading.Thread(target=self._heartbeat, daemon=True)
                    self._heartbeat_thread.start()
            return str(self._sessions[self.url]["id"])

    def _identity(self) -> dict[str, Any]:
        session = self._sessions.get(self.url, {})
        return {
            "endpoint": self.url,
            "project_id": self._selected.get("project_id", self._selected.get("id")) if self._selected else None,
            "profile_path": self._selected.get("profile_path") if self._selected else None,
            "project": session.get("project") or self._selected or self._initial_project,
            "session_id": session.get("id"),
        }

    def _projects(self, client: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._workspace_url is None:
            project = self._request(self._initial_url, "/api/project")
            self._initial_project = {
                "root": project.get("project_root", project.get("root")),
                "profile_path": project.get("profile_path"),
                "target": project.get("target"),
            }
            self._workspace_url = (project.get("workspace_url") or self._initial_url).rstrip("/")
        workspace_url = self._workspace_url
        assert workspace_url is not None
        workspace = self._request(workspace_url, "/api/workspace")
        action = arguments.get("action", "list")
        profile, project_id = arguments.get("profile"), arguments.get("project_id")
        replaced_url = None
        if action == "open":
            if not isinstance(profile, str) or not profile:
                raise _ApplicationError(
                    {"error": "projects open requires a workspace-bounded profile path", "code": "invalid_arguments"}
                )
            child = self._request(workspace_url, "/api/workspace/open", {"profile": profile})
        elif action in {"select", "restart"}:
            requested = project_id or (self._selected or {}).get("project_id")
            child = next(
                (
                    item
                    for item in reversed(workspace["children"])
                    if item.get("project_id", item["id"]) == requested or item["id"] == requested
                ),
                None,
            )
            if child is None:
                raise _ApplicationError(
                    {"error": "unknown project_id; use projects list and choose an owned project", "code": "not_found"}
                )
            if action == "restart":
                replaced_url = child.get("url")
                child = self._request(workspace_url, "/api/workspace/restart", {"id": child["id"]})
        elif action == "list":
            child = None
        else:
            raise _ApplicationError(
                {"error": "projects action must be list, open, select or restart", "code": "invalid_arguments"}
            )
        if child is not None:
            if child.get("status") != "running" or not child.get("url"):
                raise _ApplicationError({
                    "error": child.get("error") or "Project is not running; open or restart it from its workspace.",
                    "code": "project_unavailable", "details": child,
                })
            previous_url = replaced_url if action == "restart" else None
            self._selected = child
            self.url = child["url"].rstrip("/")
            if previous_url is not None:
                with self._lock:
                    self._sessions.pop(previous_url, None)
            self._open(client)
            workspace = self._request(workspace_url, "/api/workspace")
        return {"workspace": workspace, "workspace_url": self._workspace_url, "selected": self._identity()}

    def call(self, client: str, tool: str, arguments: dict[str, Any]) -> Content | CallToolResult:
        with self._routing:
            try:
                return self._call(client, tool, arguments)
            except _ApplicationError as error:
                payload = {**error.payload, "identity": self._identity()}
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(payload))],
                    structured_content=payload, is_error=True,
                )

    def _call(self, client: str, tool: str, arguments: dict[str, Any]) -> Content | CallToolResult:
        given = {key: value for key, value in arguments.items() if value is not None}
        if tool == "projects":
            data = self._projects(client, given)
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(data))], structured_content=data)
        if self._selected is not None and self._workspace_url is not None:
            workspace = self._request(self._workspace_url, "/api/workspace")
            project_id = self._selected.get("project_id", self._selected["id"])
            current = next(
                (child for child in reversed(workspace["children"])
                 if child.get("project_id", child["id"]) == project_id),
                None,
            )
            if current is None or current.get("status") != "running" or not current.get("url"):
                raise _ApplicationError({
                    "error": "Selected project is not running. Use projects open or restart to recover it.",
                    "code": "project_unavailable", "details": current,
                })
            self._selected = current
            self.url = current["url"].rstrip("/")
        if tool == "status":
            given["mcp_skill_digest"] = self.skill_digest
            given["mcp_generation"] = self.mcp_generation
        for _ in range(2):
            session = self._open(client)
            if tool != "status" and self._sessions[self.url].get("workspace_only"):
                raise _ApplicationError(
                    {
                        "error": (
                            "No project selected. Use projects(action='list'), then "
                            "projects(action='open', profile=...) or projects(action='select', project_id=...)."
                        ),
                        "code": "workspace_empty",
                        "details": {"workspace_url": self._workspace_url or self.url},
                    }
                )
            try:
                answer = self._post(f"/api/agent/{session}/call/{tool}", given)
                break
            except _UnknownSessionError:
                with self._lock:
                    if self._sessions.get(self.url, {}).get("id") == session:
                        self._sessions.pop(self.url, None)
        else:
            raise ToolError("the cadiste server keeps forgetting this session")
        content: Content = [TextContent(type="text", text=answer["text"])]
        for image in answer.get("images", []):
            content.append(TextContent(type="text", text=f"view: {image.get('view', '?')}"))
            content.append(ImageContent(type="image", data=image["data"], mime_type="image/png"))
        if "data" in answer:
            return CallToolResult(
                content=list(content), structured_content={**answer["data"], "identity": self._identity()}
            )
        content.append(TextContent(type="text", text="Project identity: " + json.dumps(self._identity())))
        return content

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            sessions, self._sessions = list(self._sessions.items()), {}
        for url, session in sessions:
            with contextlib.suppress(ToolError, _UnknownSessionError, _ApplicationError):
                self._request(url, f"/api/agent/{session['id']}/close", {}, timeout=5)


def _client(ctx: Context) -> str:
    """The agent's name as its MCP client gives it ("claude-code")."""
    params = ctx.session.client_params
    return params.client_info.name if params is not None else "agent"


def _frontmatter(text: str) -> dict[str, str]:
    """A ``SKILL.md``'s frontmatter: one ``key: value`` per line, as the skill writes it."""
    _, front, _ = text.split("---", 2)
    fields = (line.partition(":") for line in front.strip().splitlines())
    return {key.strip(): value.strip() for key, _, value in fields}


class _SkillParams(RequestParams):
    uri: str


class Skills(Extension):
    """The MCP Skills extension serving the one ``cadiste`` skill (its ``SKILL.md``, no other file)."""

    identifier = "io.modelcontextprotocol/skills"

    def __init__(self, file: Path, uri: str):
        self.text = file.read_text()
        raw = self.text.encode()
        self.frontmatter = _frontmatter(self.text)
        self.uri = uri
        self.entry = {
            "uri": uri,
            "frontmatter": self.frontmatter,
            "resources": [{"uri": uri, "digest": f"sha256:{hashlib.sha256(raw).hexdigest()}", "size": len(raw)}],
        }

    def resources(self) -> list[ResourceBinding]:
        resource = TextResource(
            uri=self.uri,
            name=self.frontmatter["name"],
            description=self.frontmatter["description"],
            mime_type="text/markdown",
            text=self.text,
        )
        return [ResourceBinding(resource)]

    def methods(self) -> list[MethodBinding]:
        async def listed(ctx: Any, params: PaginatedRequestParams) -> dict[str, Any]:
            return {"skills": [self.entry]}

        async def get(ctx: Any, params: _SkillParams) -> dict[str, Any]:
            if params.uri != self.uri:
                raise ValueError(f"no skill at {params.uri}")
            return self.entry

        return [
            MethodBinding("skills/list", PaginatedRequestParams, listed),
            MethodBinding("skills/get", _SkillParams, get),
        ]


def server(bridge: Bridge) -> MCPServer:
    mcp = MCPServer(
        "cadiste",
        title="cadiste",
        description="The cadiste viewer of build123d models: what the user selected, rebuilds, screenshots",
        instructions=INSTRUCTIONS,
        extensions=[Skills(SKILL, SKILL_URI)],
    )

    def call(ctx: Context, tool: str, **arguments: Any) -> Any:
        return bridge.call(_client(ctx), tool, arguments)

    @mcp.tool()
    def status(ctx: Context) -> Content:
        """Whether this session is linked to a cadiste viewer tab, and what that tab shows."""
        return call(ctx, "status")

    @mcp.tool()
    def projects(
        ctx: Context,
        action: Literal["list", "open", "select", "restart"] = "list",
        profile: str | None = None,
        project_id: str | None = None,
    ) -> CallToolResult:
        """List/open workspace projects, select one for this MCP session, or restart an owned server."""
        return call(ctx, "projects", action=action, profile=profile, project_id=project_id)

    @mcp.tool()
    def baseline(
        ctx: Context,
        action: Literal["pin", "list", "release"] = "list",
        targets: list[dict[str, Any]] | None = None,
        id: str | None = None,
        name: str | None = None,
    ) -> CallToolResult:
        """Pin exact retained target builds under a durable baseline ID, list pins, or release only that baseline."""
        return call(ctx, "baseline", action=action, targets=targets, id=id, name=name)

    @mcp.tool()
    def compare_builds(
        ctx: Context,
        target: dict[str, Any],
        baseline: str,
        build_id: str | None = None,
        viewer: Literal["agent", "linked"] = "agent",
        views: list[str] | None = None,
        arrangement: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Compare a baseline and exact current build with structured diffs and same-camera images."""
        return call(ctx, "compare_builds", target=target, baseline=baseline, build_id=build_id,
                    viewer=viewer, views=views, arrangement=arrangement)

    @mcp.tool()
    def renderer_state(ctx: Context, viewer: Literal["agent", "linked"] = "agent") -> CallToolResult:
        """Read renderer target/build, phase, camera, visibility layers, timings and errors without starting motion."""
        return call(ctx, "renderer_state", viewer=viewer)

    @mcp.tool()
    def edit_context(ctx: Context, requests: list[dict[str, Any]], limit: int = 10) -> CallToolResult:
        """Inspect bounded parameters, direct consumers, dimension drivers and variants without eval."""
        return call(ctx, "edit_context", requests=requests, limit=limit)

    @mcp.tool()
    def manufacturing_preflight(
        ctx: Context,
        target: dict[str, Any],
        only: list[str] | None = None,
        group: str | None = None,
        orientation: str | None = None,
        overhang_angle: float = 45,
        expected_revision: str | None = None,
    ) -> CallToolResult:
        """Inspect exact retained manufacturing geometry, mesh validity, bed placement and sampled overhang evidence."""
        return call(ctx, "manufacturing_preflight", target=target, only=only, group=group,
                    orientation=orientation, overhang_angle=overhang_angle, expected_revision=expected_revision)

    @mcp.tool()
    def list_parts(ctx: Context) -> Content:
        """The project's parts (by name), the folders they are listed in, and whether it has an assembly."""
        return call(ctx, "list_parts")

    @mcp.tool()
    def feature_tree(ctx: Context, part: PartName = None) -> Content:
        """Every feature (operation, builder, function-call group) of a part, with its id, call site and code."""
        return call(ctx, "feature_tree", part=part)

    @mcp.tool()
    def walk_components(
        ctx: Context,
        part: Annotated[str | None, Field(description="Explicit part; no viewer needed")] = None,
        component: Annotated[
            str | None, Field(description="Code/component reference or captured o/b/g alias; default: part roots")
        ] = None,
        direction: Literal["children", "parents"] = "children",
        depth: Annotated[int, Field(description="Maximum edge depth, 0-8; shallow by default", ge=0, le=8)] = 1,
        revision: Annotated[str | None, Field(description="Require this captured source_revision")] = None,
    ) -> CallToolResult:
        """Walk code-linked construction calls, builders and operations, with source identities distinct from
        runtime aliases. With no part/component, list project parts, folders and assembly entry targets without
        building. Explicit parts work headless; repeated definitions and cycles stop expansion."""
        return call(
            ctx, "walk_components", part=part, component=component, direction=direction, depth=depth, revision=revision
        )

    @mcp.tool()
    def inspect_component(
        ctx: Context,
        part: str,
        component: str,
        revision: str | None = None,
    ) -> CallToolResult:
        """Inspect a captured code definition and its construction occurrences: original call expressions,
        observed operation arguments, code ancestry, parents/children, inputs and consumers. Accepts canonical
        code/component refs and current o/b/g aliases. Does not require a viewer or read live model source."""
        return call(ctx, "inspect_component", part=part, component=component, revision=revision)

    @mcp.tool()
    def follow_parameter(
        ctx: Context,
        part: str,
        component: str,
        expression: str,
        revision: str | None = None,
        limit: int = 20,
        offset: int = 0,
        consumers: str | list[str] | None = None,
        section: str = "summary",
    ) -> CallToolResult:
        """Follow a parameter expression through captured declarations, aliases, derived dimensions and
        consumers, reporting static evidence and unresolved boundaries. Actual captured operation values remain
        separate from declared defaults; no model code is evaluated. Explicit part works headless."""
        return call(
            ctx,
            "follow_parameter",
            part=part,
            component=component,
            expression=expression,
            revision=revision,
            limit=limit,
            offset=offset,
            consumers=consumers,
            section=section,
        )

    @mcp.tool()
    def find_related_components(
        ctx: Context,
        part: str,
        component: str,
        revision: str | None = None,
        limit: int = 20,
        offset: int = 0,
        consumers: str | list[str] | None = None,
        section: str = "summary",
    ) -> CallToolResult:
        """Find code-evidenced callers, callees, shared helpers and shared parameter dependencies, including
        unbuilt siblings in captured source. No proximity/name guesses or inferred mating relationships.
        Explicit part works headless."""
        return call(
            ctx,
            "find_related_components",
            part=part,
            component=component,
            revision=revision,
            limit=limit,
            offset=offset,
            consumers=consumers,
            section=section,
        )

    @mcp.tool()
    def describe(
        ctx: Context,
        node: Annotated[str, Field(description="Feature id from the tree: o<op>, b<builder> or g<group>")],
        part: PartName = None,
    ) -> Content:
        """One feature in detail: arguments (expression written and value), what it consumed, the faces it made,
        its call stack and the source around its call."""
        return call(ctx, "describe", node=node, part=part)

    @mcp.tool()
    def measure(
        ctx: Context,
        paths: Annotated[list[str], Field(description="Viewer paths of faces, edges or vertices (.../faces/faces_3)")],
        part: PartName = None,
        target: dict[str, Any] | None = None,
        build_id: str | None = None,
    ) -> Content:
        """Exact geometry of faces, edges and vertices (on the B-rep, not the mesh), and for two of them the
        distances and the angle between them."""
        return call(ctx, "measure", paths=paths, part=part, target=target, build_id=build_id)

    @mcp.tool()
    def show(
        ctx: Context,
        part: Annotated[str | None, Field(description="Part to build and show")] = None,
        view: Annotated[
            str | None,
            Field(
                description="Instead of a part: 'assembly' (the first) or 'assembly:<name>', "
                "or a folder of parts (list_parts)"
            ),
        ] = None,
        fresh: Annotated[bool, Field(description="Build again even if the source did not change")] = True,
        select: Annotated[str | None, Field(description="Feature id (or instance id) to select once shown")] = None,
        screenshots: Annotated[
            list[Direction | Literal["current"]] | None,
            Field(description="Views to capture (default ['iso']; [] for none)"),
        ] = None,
        viewer: Literal["agent", "linked"] = "agent",
        target: dict[str, Any] | None = None,
        arrangement: dict[str, Any] | None = None,
        baseline: str | None = None,
    ) -> Content:
        """Build and capture an exact generation in private Chromium (default; no link and no user-tab changes).
        Opt into viewer='linked' to open the user's linked tab. arrangement may set selection, hidden instances,
        camera, fit ('all'/'visible'/'selected'), section, explode and display. Returns build evidence and images."""
        return call(
            ctx,
            "show",
            part=part,
            view=view,
            fresh=fresh,
            select=select,
            screenshots=screenshots,
            viewer=viewer,
            target=target,
            arrangement=arrangement,
            baseline=baseline,
        )

    @mcp.tool()
    def select(
        ctx: Context,
        items: Annotated[
            list[SelectionItem],
            Field(
                description="Ordered selection: {kind:'feature',part,node}, {kind:'instance',path}, or "
                "{kind:'entity',path}. Multiple mixed items are allowed; [] clears the selection."
            ),
        ],
    ) -> Content:
        """Replace the linked tab's selection and return its self-contained captured Markdown snapshot."""
        return call(ctx, "select", items=items)

    @mcp.tool()
    def highlight(
        ctx: Context,
        paths: Annotated[list[str], Field(description="Viewer paths: faces, edges, vertices, or whole instances")],
    ) -> Content:
        """Light up these in the linked tab, on top of the user's selection, until the next highlight ([] clears)."""
        return call(ctx, "highlight", paths=paths)

    @mcp.tool()
    def set_view(
        ctx: Context,
        direction: Direction | None = None,
        fit: Annotated[
            bool | Literal["all", "visible", "selected"] | None,
            Field(description="Fit all, visible or selected geometry"),
        ] = None,
        ortho: Annotated[bool | None, Field(description="Orthographic (true) or perspective (false)")] = None,
        transparent: Annotated[bool | None, Field(description="Draw everything see-through")] = None,
        camera: dict[str, Any] | None = None,
        viewer: Literal["agent", "linked"] = "linked",
    ) -> Content:
        """Set a renderer's camera/display; linked controls are explicit, private controls use the last agent target."""
        return call(
            ctx,
            "set_view",
            direction=direction,
            fit=fit,
            ortho=ortho,
            transparent=transparent,
            camera=camera,
            viewer=viewer,
        )

    @mcp.tool()
    def set_visibility(
        ctx: Context,
        instances: Annotated[list[str], Field(description="Instance or group ids of the assembly")],
        visible: bool,
        viewer: Literal["agent", "linked"] = "linked",
    ) -> Content:
        """Show or hide instances in the assembly (or a folder's parts) shown in the linked tab."""
        return call(ctx, "set_visibility", instances=instances, visible=visible, viewer=viewer)

    @mcp.tool()
    def screenshot(
        ctx: Context,
        views: Annotated[
            list[Direction | Literal["current"]] | None,
            Field(description="Directions to capture; default the current camera"),
        ] = None,
        target: dict[str, Any] | None = None,
        part: str | None = None,
        view: str | None = None,
        build_id: str | None = None,
        viewer: Literal["agent", "linked"] = "agent",
        arrangement: dict[str, Any] | None = None,
    ) -> Content:
        """Capture the explicit target/build, or last private target, without linking or touching a user tab.
        viewer='linked' opts into the linked tab. Multi-view capture is atomic; races discard images."""
        return call(
            ctx,
            "screenshot",
            views=views,
            target=target,
            part=part,
            view=view,
            build_id=build_id,
            viewer=viewer,
            arrangement=arrangement,
        )

    @mcp.tool()
    def request_outcome(ctx: Context, request_id: str) -> CallToolResult:
        """Recover a renderer transaction after timeout without blindly resending or rebuilding."""
        return call(ctx, "request_outcome", request_id=request_id)

    @mcp.tool()
    def saved_views(
        ctx: Context,
        action: Literal["list", "create", "update", "delete", "restore"] = "list",
        id: str | None = None,
        name: str | None = None,
        state: dict[str, Any] | None = None,
        viewer: Literal["agent", "linked"] = "linked",
    ) -> CallToolResult:
        """CRUD/restore project-wide saved camera, section, explode, selection and visibility state."""
        return call(ctx, "saved_views", action=action, id=id, name=name, state=state, viewer=viewer)

    @mcp.tool()
    def set_section(
        ctx: Context,
        active: bool,
        plane: dict[str, Any] | None = None,
        offset: float = 0,
        flipped: bool = False,
        viewer: Literal["agent", "linked"] = "linked",
    ) -> CallToolResult:
        """Set a section plane {label,point:[x,y,z],normal:[x,y,z]} in mm; normal is normalized."""
        return call(ctx, "set_section", active=active, plane=plane, offset=offset, flipped=flipped, viewer=viewer)

    @mcp.tool()
    def set_explode(
        ctx: Context, amount: float | None = None, viewer: Literal["agent", "linked"] = "linked"
    ) -> CallToolResult:
        """Set explicit explode amount 0-1; null restores assembled display, not physical geometry."""
        return call(ctx, "set_explode", amount=amount, viewer=viewer)

    @mcp.tool()
    def check_geometry(
        ctx: Context,
        target: dict[str, Any],
        pairs: list[list[str]],
        clearance: float = 0,
        ignore: list[list[str]] | None = None,
        samples: list[dict[str, Any]] | None = None,
        build_id: str | None = None,
        tolerance: float = 1e-6,
        volume_tolerance: float = 1e-9,
        contact: list[dict[str, Any]] | None = None,
    ) -> CallToolResult:
        """Exact explicit whole-instance overlap/separation checks and independent sampled rigid poses.
        Pairs use full leaf paths; samples {path,translation:[mm],rotation:[degrees]} are not continuous proof."""
        return call(
            ctx,
            "check_geometry",
            target=target,
            pairs=pairs,
            clearance=clearance,
            ignore=ignore,
            samples=samples,
            build_id=build_id,
            tolerance=tolerance,
            volume_tolerance=volume_tolerance,
            contact=contact,
        )

    @mcp.tool()
    def verify_targets(
        ctx: Context,
        targets: list[dict[str, Any]],
        fresh: bool = False,
        checks: list[dict[str, Any]] | None = None,
        expected_revision: str | None = None,
        baseline: str | None = None,
        check_sets: list[str] | None = None,
        assertions: list[dict[str, Any]] | None = None,
    ) -> CallToolResult:
        """Headless multi-target build verification with structured geometry/placement diffs and explicit checks."""
        return call(
            ctx, "verify_targets", targets=targets, fresh=fresh, checks=checks, expected_revision=expected_revision,
            baseline=baseline, check_sets=check_sets, assertions=assertions,
        )

    @mcp.tool()
    def describe_assembly(
        ctx: Context, name: str, path: str | None = None, limit: int = 50, offset: int = 0
    ) -> CallToolResult:
        """Inspect paginated assembly ownership, construction, instance transforms and captured placement source."""
        return call(ctx, "describe_assembly", name=name, path=path, limit=limit, offset=offset)

    @mcp.tool()
    def inspect_simulation(ctx: Context, target: dict[str, Any]) -> CallToolResult:
        """Read simulation topology, configuration, prerequisites and owned runtime status; never starts motion."""
        return call(ctx, "inspect_simulation", target=target)

    @mcp.tool()
    def export_target(
        ctx: Context,
        target: dict[str, Any],
        format: Literal["step", "3mf", "stl", "brep"] = "step",
        only: list[str] | None = None,
        expected_revision: str | None = None,
        group: str | None = None,
        orientation: str | None = None,
        preflight: bool = True,
    ) -> CallToolResult:
        """Explicitly export a target; return saved artifact URL/path with source revision and build evidence."""
        return call(ctx, "export_target", target=target, format=format, only=only, expected_revision=expected_revision,
                    group=group, orientation=orientation, preflight=preflight)

    @mcp.tool()
    def open_bambu(
        ctx: Context, target: dict[str, Any], only: list[str] | None = None, expected_revision: str | None = None,
        group: str | None = None, orientation: str | None = None, verify_import: bool = False,
    ) -> CallToolResult:
        """Export geometry-only 3MF and launch Bambu Studio on the server; returns artifact/revision evidence.
        Launch acknowledgement does not mean slicing, upload or printing occurred."""
        return call(ctx, "open_bambu", target=target, only=only, expected_revision=expected_revision,
                    group=group, orientation=orientation, verify_import=verify_import)

    @mcp.tool()
    def export_cncjs(
        ctx: Context, target: dict[str, Any], machining: dict[str, Any],
        only: list[str] | None = None, expected_revision: str | None = None,
        group: str | None = None, orientation: str | None = None,
    ) -> CallToolResult:
        """Export compensated 2D router profiles as .nc for manual CNCjs loading; never send or run G-code.

        machining requires tool_diameter, cut_depth, step_down, safe_z (mm), feed_rate, plunge_rate
        (mm/min), and spindle_speed (integer RPM). Work XY0 is the part's bounds center; work Z0 is stock top.
        Flat stock outlines and proven through holes export; pockets, lead-ins and unmachinable interior
        holes are omitted with separate counts, bounds and reasons in an incomplete/profile-only report.
        Verify workholding, clearance, spindle and simulation manually; no tabs are generated.
        """
        return call(ctx, "export_cncjs", target=target, machining=machining, only=only,
                    expected_revision=expected_revision, group=group, orientation=orientation)

    @mcp.tool()
    def suggest_affected_targets(
        ctx: Context, changed_files: list[str], targets: list[dict[str, Any]] | None = None
    ) -> CallToolResult:
        """Suggest targets from retained source evidence, explicitly conservative/incomplete; builds nothing."""
        return call(ctx, "suggest_affected_targets", changed_files=changed_files, targets=targets)

    return mcp


def run(url: str | None) -> None:
    bridge = Bridge(url or find_url(Path.cwd()))
    try:
        server(bridge).run()
    finally:
        bridge.close()
