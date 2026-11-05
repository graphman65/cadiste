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
source yourself, then call show(part=...) to rebuild it: the answer compares it with the build before and includes
a screenshot. Live viewer tools use a session-linked tab; the first live tool requests a user-approved link when
needed. A snapshot's viewer tab identity is context, not an automatic link. Read skill://cadiste/SKILL.md for the
workflow."""

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


class Bridge:
    """The agent's session on the server, opened on the first call."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self._session: str | None = None
        self._code = ""
        self._lock = threading.Lock()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            f"{self.url}{path}", json.dumps(body).encode(), {"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read()).get("error", str(error))
            except ValueError:
                message = str(error)
            if error.code == 404 and message == "no such session":
                raise _UnknownSessionError from error
            raise ToolError(message) from error
        except (urllib.error.URLError, OSError) as error:
            raise ToolError(
                f"cadiste serve is not reachable at {self.url} ({error}): ask the user to start it in the model's "
                "project (cadiste serve ...), or give `cadiste mcp --url`"
            ) from error

    def _open(self, client: str) -> str:
        with self._lock:
            if self._session is None:
                opened = self._post("/api/agent/sessions", {"client": client})
                self._session, self._code = str(opened["id"]), str(opened["code"])
            return self._session

    def call(self, client: str, tool: str, arguments: dict[str, Any]) -> Content | CallToolResult:
        given = {key: value for key, value in arguments.items() if value is not None}
        for _ in range(2):
            session = self._open(client)
            try:
                answer = self._post(f"/api/agent/{session}/call/{tool}", given)
                break
            except _UnknownSessionError:
                with self._lock:
                    if self._session == session:
                        self._session = None
        else:
            raise ToolError("the cadiste server keeps forgetting this session")
        content: Content = [TextContent(type="text", text=answer["text"])]
        for image in answer.get("images", []):
            content.append(TextContent(type="text", text=f"view: {image.get('view', '?')}"))
            content.append(ImageContent(type="image", data=image["data"], mime_type="image/png"))
        if "data" in answer:
            return CallToolResult(content=list(content), structured_content=answer["data"])
        return content

    def close(self) -> None:
        if self._session is not None:
            with contextlib.suppress(ToolError, _UnknownSessionError):
                self._post(f"/api/agent/{self._session}/close", {})


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
    ) -> CallToolResult:
        """Follow a parameter expression through captured declarations, aliases, derived dimensions and
        consumers, reporting static evidence and unresolved boundaries. Actual captured operation values remain
        separate from declared defaults; no model code is evaluated. Explicit part works headless."""
        return call(ctx, "follow_parameter", part=part, component=component, expression=expression, revision=revision)

    @mcp.tool()
    def find_related_components(
        ctx: Context,
        part: str,
        component: str,
        revision: str | None = None,
    ) -> CallToolResult:
        """Find code-evidenced callers, callees, shared helpers and shared parameter dependencies, including
        unbuilt siblings in captured source. No proximity/name guesses or inferred mating relationships.
        Explicit part works headless."""
        return call(ctx, "find_related_components", part=part, component=component, revision=revision)

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
    ) -> Content:
        """Exact geometry of faces, edges and vertices (on the B-rep, not the mesh), and for two of them the
        distances and the angle between them."""
        return call(ctx, "measure", paths=paths, part=part)

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
    ) -> Content:
        """Build a part (or the assembly, or a folder's parts) from the current source, open it in the linked tab,
        and answer with how it differs from the previous build (volume, bounding box, faces, operations) and
        screenshots. A failed build answers with its traceback. Call it after every edit."""
        return call(ctx, "show", part=part, view=view, fresh=fresh, select=select, screenshots=screenshots)

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
        fit: Annotated[bool | None, Field(description="Reset the camera to fit the whole model")] = None,
        ortho: Annotated[bool | None, Field(description="Orthographic (true) or perspective (false)")] = None,
        transparent: Annotated[bool | None, Field(description="Draw everything see-through")] = None,
    ) -> Content:
        """Turn the linked tab's camera to a preset direction, and set how the model is drawn."""
        return call(ctx, "set_view", direction=direction, fit=fit, ortho=ortho, transparent=transparent)

    @mcp.tool()
    def set_visibility(
        ctx: Context,
        instances: Annotated[list[str], Field(description="Instance or group ids of the assembly")],
        visible: bool,
    ) -> Content:
        """Show or hide instances in the assembly (or a folder's parts) shown in the linked tab."""
        return call(ctx, "set_visibility", instances=instances, visible=visible)

    @mcp.tool()
    def screenshot(
        ctx: Context,
        views: Annotated[
            list[Direction | Literal["current"]] | None,
            Field(description="Directions to capture; default the current camera"),
        ] = None,
    ) -> Content:
        """Images of the linked tab's 3D view, the camera put back as it was."""
        return call(ctx, "screenshot", views=views)

    return mcp


def run(url: str | None) -> None:
    bridge = Bridge(url or find_url(Path.cwd()))
    try:
        server(bridge).run()
    finally:
        bridge.close()
