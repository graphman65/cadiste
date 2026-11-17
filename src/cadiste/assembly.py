"""Evidence-backed assembly ownership and placement navigation, independent of a viewer."""

from __future__ import annotations

import ast
import copy
import inspect
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import FrameType
from typing import Any, Literal

from build123d import Location
from build123d.topology import Shape

from .source import capture_sources, code_ref, source_revision

_METADATA = "_cadiste_instance"
_PLACEMENTS = {"move", "moved", "locate", "located", "rotate", "translate"}
_ROOT: ContextVar[Path | None] = ContextVar("cadiste_assembly_root", default=None)


def _value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Location):
        return {"position": list(value.position), "orientation": list(value.orientation)}
    if isinstance(value, (list, tuple)) and len(value) <= 20:
        return [_value(item) for item in value]
    return {"type": type(value).__name__, "value": "unavailable (not a captured scalar or location)"}


def _site(frame: FrameType, method: str, values: Mapping[str, Any], root: Path) -> dict[str, Any]:
    path = Path(frame.f_code.co_filename).resolve()
    file = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    qualified = frame.f_code.co_qualname.replace(".<locals>.", ".")
    result: dict[str, Any] = {
        "method": method,
        "definition": code_ref(file, qualified),
        "site": {"file": file, "line": frame.f_lineno},
        "arguments": [{"name": name, "value": _value(value)} for name, value in values.items()],
        "reference": None,
        "code": None,
    }
    try:
        source = path.read_text()
        tree = ast.parse(source)
    except (OSError, SyntaxError, UnicodeError):
        result["unavailable"] = "placement source could not be captured"
        return result
    result["source"] = source
    result["source_revision"] = source_revision({file: source})
    position = inspect.getframeinfo(frame).positions
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and node.lineno <= frame.f_lineno <= (node.end_lineno or node.lineno)
    ]
    if position and position.col_offset is not None:
        precise = [
            node
            for node in calls
            if node.lineno <= (position.lineno or frame.f_lineno)
            and (node.lineno != position.lineno or node.col_offset <= position.col_offset)
            and (
                (node.end_lineno or node.lineno) != position.end_lineno
                or (node.end_col_offset or 0) >= (position.end_col_offset or 0)
            )
        ]
        calls = precise if precise else calls if len(calls) == 1 else []
    # Only a uniquely located call is evidence. Never guess between calls on the same line.
    calls.sort(
        key=lambda node: ((node.end_lineno or node.lineno) - node.lineno, (node.end_col_offset or 0) - node.col_offset)
    )
    if not calls or (len(calls) > 1 and not (position and position.col_offset is not None)):
        result["unavailable"] = "placement call expression could not be uniquely located"
        return result
    call = calls[0]
    scope: ast.AST = tree
    scopes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno <= call.lineno <= (node.end_lineno or node.lineno)
    ]
    if scopes:
        scope = min(scopes, key=lambda node: (node.end_lineno or node.lineno) - node.lineno)
    ordered = sorted(
        (node for node in ast.walk(scope) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    result["reference"] = f"{code_ref(file, qualified)}::placement:{ordered.index(call)}"
    result["code"] = ast.get_source_segment(source, call)
    result["site"].update(line=call.lineno, end_line=call.end_lineno, column=call.col_offset)
    expressions = [
        {"name": None, "position": index, "expression": ast.get_source_segment(source, arg)}
        for index, arg in enumerate(call.args)
    ]
    expressions += [{"name": arg.arg, "expression": ast.get_source_segment(source, arg.value)} for arg in call.keywords]
    result["call_arguments"] = expressions
    return result


def instance(shape: Shape, *, part: str, label: str | None = None, location: Location | None = None) -> Shape:
    """Copy a shape as an explicitly owned assembly instance; fresh equal shapes need no topology cache.

    ``part`` is a catalog identity, not an inferred label. ``location`` is an absolute local placement.
    Each call returns a separate object, so repeated placements retain distinct viewer identities.
    """
    if not isinstance(shape, Shape):
        raise TypeError("instance requires a build123d Shape")
    if not isinstance(part, str) or not part:
        raise ValueError("instance part must be a nonempty catalog name")
    result = copy.copy(shape)
    result.parent = None
    if label is not None:
        result.label = label
    if location is not None:
        result.location = location
    frame = inspect.currentframe()
    try:
        caller = frame.f_back if frame else None
        placement = (
            _site(
                caller,
                "instance",
                {"part": part, "label": label, "location": location},
                _ROOT.get() or Path.cwd().resolve(),
            )
            if caller
            else None
        )
    finally:
        del frame
    setattr(
        result,
        _METADATA,
        {
            "owner": part,
            "ownership": "explicit",
            "placement": placement,
            "placements": [placement] if placement else [],
        },
    )
    return result


@contextmanager
def capture_placements(*, root: Path | None = None) -> Iterator[None]:
    """Capture ordinary build123d placement returns without patching model/library methods.

    Only calls directly from project source are retained. Unsupported/dynamic placements remain unavailable.
    The previously installed profiler is restored and receives its original events.
    """
    root = (root or Path.cwd()).resolve()
    token = _ROOT.set(root)
    previous = sys.getprofile()

    def profile(
        frame: FrameType, event: Literal["call", "return", "c_call", "c_return", "c_exception"], arg: Any
    ) -> None:
        if previous is not None:
            previous(frame, event, arg)
        if event != "return" or frame.f_code.co_name not in _PLACEMENTS | {"__init__"}:
            return
        if not frame.f_globals.get("__name__", "").startswith("build123d."):
            return
        caller = frame.f_back
        if caller is None:
            return
        caller_path = Path(caller.f_code.co_filename).resolve()
        if (
            not caller_path.is_relative_to(root)
            or {"site-packages", "dist-packages", ".venv"}.intersection(caller_path.parts)
            or caller.f_globals.get("__name__", "").startswith("cadiste.")
        ):
            return
        shape = arg if isinstance(arg, Shape) else frame.f_locals.get("self")
        if not isinstance(shape, Shape):
            return
        construction = frame.f_code.co_name == "__init__"
        if construction and not shape.children:
            return
        metadata = dict(getattr(shape, _METADATA, {}))
        parameters = {
            name: value
            for name, value in frame.f_locals.items()
            if name in frame.f_code.co_varnames[: frame.f_code.co_argcount] and name != "self"
        }
        evidence = _site(caller, type(shape).__name__ if construction else frame.f_code.co_name, parameters, root)
        if construction:
            metadata["construction"] = evidence
        else:
            metadata["placements"] = [*metadata.get("placements", []), evidence]
            metadata["placement"] = evidence
        setattr(shape, _METADATA, metadata)

    sys.setprofile(profile)
    try:
        yield
    finally:
        sys.setprofile(previous)
        _ROOT.reset(token)


def _leaves(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [node] if "parts" not in node else [leaf for child in node["parts"] for leaf in _leaves(child)]


def _coverage(scene: dict[str, Any], *, attempted: bool) -> None:
    leaves = _leaves(scene["data"]["shapes"])
    owners = scene.get("part_of", {})
    unmatched = [leaf["id"] for leaf in leaves if leaf["id"] not in owners]
    total, matched = len(leaves), len(leaves) - len(unmatched)
    status = "complete" if matched == total else "partial" if matched else "empty" if attempted else "undefined"
    explanation = {
        "complete": "Every instance has evidence-backed ownership.",
        "partial": (
            "Some instances have no unique ownership evidence; names and geometric resemblance are not ownership."
        ),
        "empty": (
            "No instances matched. Freshly rebuilt shapes do not share topology; "
            "annotate them with cadiste.assembly.instance(..., part=...)."
        ),
        "undefined": (
            "No part catalog matching was requested and no explicit ownership was captured. "
            "Use instance(..., part=...) or configure parts."
        ),
    }[status]
    scene["ownership"] = {
        "status": status,
        "matched": matched,
        "total": total,
        "unmatched": unmatched,
        "explanation": explanation,
    }


def annotate_scene(scene: dict[str, Any], assembly: Shape, *, root: Path | None = None) -> None:
    """Bind source-object metadata by the converter's structural child order, never by labels or resemblance."""
    records: dict[str, Any] = {}
    constructions: dict[str, Any] = {}
    sources: dict[str, str] = {}
    labels: dict[str, str] = {}

    def bind(shape: Shape, node: dict[str, Any], inherited: dict[str, Any] | None = None) -> None:
        labels[node["id"]] = shape.label or type(shape).__name__
        metadata = {**(inherited or {}), **getattr(shape, _METADATA, {})}
        for placement in [metadata.get("construction"), metadata.get("placement"), *metadata.get("placements", [])]:
            if placement and placement.get("source") is not None:
                sources[placement["site"]["file"]] = placement["source"]
        own_construction = getattr(shape, _METADATA, {}).get("construction")
        if own_construction:
            constructions[node["id"]] = {key: value for key, value in own_construction.items() if key != "source"}
        children = list(shape.children)
        nodes = node.get("parts", [])
        if children:
            if len(children) == len(nodes):
                for child, child_node in zip(children, nodes, strict=True):
                    bind(child, child_node, metadata)
            # Converter-added helpers make positional correspondence unsafe: leave them explicitly unmatched.
            return
        for leaf in _leaves(node):
            clean = copy.deepcopy(metadata)
            for placement in [clean.get("construction"), clean.get("placement"), *clean.get("placements", [])]:
                if placement:
                    placement.pop("source", None)
            records[leaf["id"]] = {
                "owner": None, "ownership": "unavailable", "placement": None, **clean,
                "label": shape.label or type(shape).__name__,
            }

    node = scene["data"]["shapes"]
    if not assembly.children and len(node.get("parts", [])) == 1:
        node = node["parts"][0]
    bind(assembly, node)
    scene["instances"] = records
    scene["labels"] = labels
    scene["construction"] = constructions
    scene["part_of"] = {path: record["owner"] for path, record in records.items() if record.get("owner")}
    root = (root or Path.cwd()).resolve()
    # Include imported project packages and parameter modules, even without placement annotations.
    sources = {**capture_sources((str(root / file) for file in sources), root), **sources}
    scene["sources"] = sources
    scene["source_revision"] = source_revision(sources)
    _coverage(scene, attempted=False)


def match_parts(scene: dict[str, Any], leaves: list[Any], parts: Mapping[str, Shape]) -> dict[str, str]:
    """Extend explicit ownership with unique shared-topology evidence; ambiguity never picks the first name."""
    from .provenance import unlocated_key

    owners: dict[Any, set[str]] = {}
    for name, part in parts.items():
        faces = part.faces() if isinstance(part, Shape) else []
        if faces:
            key = (len(faces), frozenset(unlocated_key(face.wrapped) for face in faces))
            owners.setdefault(key, set()).add(name)
    matched = dict(scene.get("part_of", {}))
    records = scene.setdefault("instances", {})
    for leaf in leaves:
        if leaf.path in matched:
            continue
        key = (len(leaf.faces), frozenset(unlocated_key(face) for face in leaf.faces)) if leaf.faces else None
        candidates = owners.get(key, set())
        if len(candidates) == 1:
            owner = next(iter(candidates))
            matched[leaf.path] = owner
            records.setdefault(leaf.path, {}).update(owner=owner, ownership="shared_topology")
    scene["part_of"] = matched
    _coverage(scene, attempted=True)
    return matched


def owning_part(scene: dict[str, Any], path: str, parts: set[str] | None = None) -> str | None:
    """Resolve only captured explicit/shared-topology ownership, never an ancestor's name."""
    owner = scene.get("part_of", {}).get(path)
    if owner is None:
        owner = scene.get("instances", {}).get(path, {}).get("owner")
    return owner if isinstance(owner, str) and (parts is None or owner in parts) else None


def describe_assembly(
    built: dict[str, Any], path: str | None = None, *, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """Bounded, headless construction/placement evidence from one retained assembly build."""
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("limit must be 1..200 and offset must be nonnegative")
    scene = built["scene"]
    root = scene["data"]["shapes"]
    nodes: dict[str, dict[str, Any]] = {}
    chains: dict[str, list[dict[str, Any]]] = {}

    def visit(node: dict[str, Any], above: list[dict[str, Any]]) -> None:
        nodes[node["id"]] = node
        chains[node["id"]] = [*above, {"path": node["id"], "local_transform": node.get("loc")}]
        for child in node.get("parts", []):
            visit(child, chains[node["id"]])

    visit(root, [])
    if path is not None and path not in nodes:
        raise LookupError(f"assembly instance is not in this captured build: {path}")
    members = _leaves(nodes[path] if path else root)
    page = members[offset : offset + limit]
    records = []
    files: set[str] = set()
    construction_paths = {ancestor["path"] for member in page for ancestor in chains[member["id"]]}
    if path is not None:
        construction_paths.update(ancestor["path"] for ancestor in chains[path])
    constructions = {
        node_path: scene.get("construction", {})[node_path]
        for node_path in sorted(construction_paths)
        if node_path in scene.get("construction", {})
    }
    for construction in constructions.values():
        if construction.get("site"):
            files.add(construction["site"]["file"])
    for member in page:
        metadata = scene.get("instances", {}).get(member["id"], {})
        placement = metadata.get("placement")
        placements = metadata.get("placements", [])
        for evidence in [placement, *placements]:
            if evidence and evidence.get("site"):
                files.add(evidence["site"]["file"])
        records.append(
            {
                "path": member["id"],
                "name": member["name"],
                "owner": owning_part(scene, member["id"]),
                "ownership": metadata.get("ownership", "unavailable"),
                "placement": placement,
                "placements": placements,
                "placement_unavailable": None
                if placement
                else "No explicit helper or safely traceable ordinary placement call was captured.",
                "local_transform": member.get("loc"),
                "transform_chain": chains[member["id"]],
            }
        )
    return {
        "root": root["id"],
        "selected": path,
        "build_id": built.get("build_id"),
        "source_revision": scene.get("source_revision"),
        "ownership": scene.get("ownership"),
        "construction": constructions,
        "instances": records,
        "total": len(members),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if offset + limit < len(members) else None,
        "sources": {file: scene.get("sources", {})[file] for file in sorted(files) if file in scene.get("sources", {})},
        "scope": "Captured assembly construction and placements; owner definitions are separate part traces.",
    }
