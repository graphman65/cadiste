"""The viewer's state as text for a coding agent (``cadiste mcp``): a traced part's feature tree, one feature in
detail, what the user selected in a tab, and how a new build differs from the previous one.

Everything works on the JSON ``cadiste export`` and ``cadiste mesh`` write (``web/src/types.ts``), so the server
answers from its cache without building. Node ids are the viewer's: ``o<op>``, ``b<builder>``, ``g<n>``.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from typing import Any

Json = dict[str, Any]

_EXCERPT = 3
"""Lines of source shown above and below a call."""
_CODE = 100
"""Width a call's code is cut to in one-line listings."""
_PICK = re.compile(r"(?P<leaf>.+)/(?P<topo>faces|edges|vertices)/(?P=topo)_(?P<index>\d+)")


def _flat(code: str, width: int = _CODE) -> str:
    flat = " ".join(code.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _where(site: Json) -> str:
    return f"{site['file']}:{site['line']}"


def _builder_title(builder: Json) -> str:
    return (
        f"{builder['kind']} {builder['name']}".rstrip() if builder["name"] else f"{builder['kind']} #{builder['index']}"
    )


def _node_of_op(trace: Json, op: int) -> str:
    """A child builder's hand-over is shown (and selected) on that builder's node."""
    child = trace["ops"][op]["child"]
    return f"o{op}" if child is None else f"b{child}"


def op_line(trace: Json, op: Json) -> str:
    site = op["site"][-1] if op["site"] else None
    where = f"  {_where(site)}  {_flat(site['code'])}" if site else ""
    inputs = op["inputs"]
    consumed = f"  ← {', '.join(_builder_title(trace['builders'][b]) for b in inputs)}" if inputs else ""
    return f"[o{op['index']}] #{op['index']} {op['kind']} {op['mode']}{where}{consumed}"


def _builder_line(trace: Json, builder: Json) -> str:
    where = f"  {_where(builder['site'][-1])}" if builder["site"] else ""
    owner = f"  [{builder['owner']}]" if builder["owner"] and builder["parent"] is None else ""
    published = builder["published_by"]
    into = f"  → #{published} {trace['ops'][published]['mode']}" if published is not None else ""
    return f"[b{builder['index']}] {_builder_title(builder)} ({builder['mode']}){where}{owner}{into}"


def _node_line(trace: Json, node: Json) -> str:
    match node["type"]:
        case "op":
            return op_line(trace, trace["ops"][node["op"]])
        case "builder":
            return _builder_line(trace, trace["builders"][node["builder"]])
        case _:
            return f"[{node['id']}] {node['label']}()  {_where(node['called_at'])}"


def _tree_lines(trace: Json, node: Json) -> list[str]:
    lines = [_node_line(trace, node)]
    children = node.get("children", [])
    for i, child in enumerate(children):
        last = i == len(children) - 1
        sub = _tree_lines(trace, child)
        lines.append(("└─ " if last else "├─ ") + sub[0])
        lines.extend(("   " if last else "│  ") + line for line in sub[1:])
    return lines


def _nodes(roots: Iterable[Json]) -> Iterable[Json]:
    for node in roots:
        yield node
        yield from _nodes(node.get("children", []))


def find_node(trace: Json, node_id: str) -> Json | None:
    return next((node for node in _nodes(trace["roots"]) if node["id"] == node_id), None)


def _vec(values: Iterable[float]) -> str:
    return "(" + ", ".join(f"{v:.3f}" for v in values) + ")"


def _size(result: Json) -> str:
    """Volume and bounding box, for a build exported with them."""
    if "volume" not in result:
        return ""
    low, high = result["bbox"]["min"], result["bbox"]["max"]
    size = " x ".join(f"{b - a:.2f}" for a, b in zip(low, high, strict=True))
    return f"; volume {result['volume']:.2f} mm³, bounding box {size} mm from {_vec(low)} to {_vec(high)}"


def trace_summary(trace: Json) -> str:
    result = trace["result"]
    return (
        f"{result['type']}: {len(trace['ops'])} operations, {result['attributed']}/{result['faces']} faces traced to "
        f"an operation{_size(result)}; built in {trace['build_seconds']:.2f} s"
    )


def viewer_paths(trace: Json) -> str:
    """How to name the part's faces, edges and vertices to the tools that take viewer paths."""
    part = trace["scene"]["part"]
    if not trace["scene"]["face_ids"]:
        return f"The part's viewer path is {part!r}; its mesh does not number faces as the part does."
    return (
        f"Viewer paths: face fN is '{part}/faces/faces_N', edge eN '{part}/edges/edges_N', vertex vN "
        f"'{part}/vertices/vertices_N' (fN is shape.faces()[N])."
    )


def feature_tree(trace: Json) -> str:
    if not trace["roots"]:
        return f"No builder operation ran: this {trace['result']['type']} was made without one, so it has no features."
    return "\n\n".join("\n".join(_tree_lines(trace, root)) for root in trace["roots"])


def _excerpt(trace: Json, site: Json) -> list[str]:
    """The source around a call, numbered, the call's lines marked."""
    source = trace["sources"].get(site["file"])
    if source is None:
        return [f"Source unavailable: {site['file']} was not captured in this build."]
    lines = source.splitlines()
    first = site["line"]
    last = first + max(1, len(site["code"].splitlines())) - 1
    start, end = max(1, first - _EXCERPT), min(len(lines), last + _EXCERPT)
    width = len(str(end))
    return [f"{'>' if first <= n <= last else ' '} {n:>{width}} | {lines[n - 1]}" for n in range(start, end + 1)]


def _faces(numbers: list[int]) -> str:
    return ", ".join(f"f{n}" for n in numbers) if numbers else "none"


def _stack(sites: list[Json]) -> str:
    return " → ".join(f"{site['label']} {_where(site)}" for site in sites)


def describe(trace: Json, node_id: str) -> str:
    """One feature in detail, the way the viewer's Details panel shows it, with the source at its call."""
    node = find_node(trace, node_id)
    if node is None:
        if re.fullmatch(r"o\d+", node_id) and int(node_id[1:]) < len(trace["ops"]):
            # A child builder's hand-over: the tree shows it on the builder's node.
            node = find_node(trace, _node_of_op(trace, int(node_id[1:])))
        if node is None:
            raise LookupError(f"no node {node_id!r} in this part's feature tree (ids: o<op>, b<builder>, g<n>)")
    lines: list[str] = []
    match node["type"]:
        case "op":
            lines += operation_lines(trace, trace["ops"][node["op"]])
        case "builder":
            builder = trace["builders"][node["builder"]]
            lines.append(_builder_line(trace, builder))
            if builder["parent"] is not None:
                lines.append(f"  inside: {_builder_line(trace, trace['builders'][builder['parent']])}")
            if builder["consumed_by"]:
                lines.append("  consumed by:")
                lines += [f"    {op_line(trace, trace['ops'][op])}" for op in builder["consumed_by"]]
            if builder["site"]:
                lines.append(f"  call stack: {_stack(builder['site'])}")
                lines.append("  source:")
                lines += [f"    {line}" for line in _excerpt(trace, builder["site"][-1])]
            else:
                lines.append("  source unavailable: no builder call site was captured.")
            if node["children"]:
                lines.append("  contents:")
                lines += [f"    {line}" for child in node["children"] for line in _tree_lines(trace, child)]
        case _:
            lines.append(f"[{node['id']}] {node['label']}(), defined in {node['defined_in']}")
            lines.append(f"  called at {_where(node['called_at'])}")
            lines += [f"    {line}" for line in _excerpt(trace, node["called_at"])]
            lines.append("  contents:")
            lines += [f"    {line}" for child in node["children"] for line in _tree_lines(trace, child)]
    return "\n".join(lines)


def operation_lines(trace: Json, op: Json) -> list[str]:
    """Full captured operation detail, including hand-overs represented as builders in the tree."""
    builder = trace["builders"][op["builder"]]
    lines = [op_line(trace, op), f"  in {_builder_line(trace, builder)}"]
    if op["arguments"]:
        lines.append("  arguments (parameter = expression written → value):")
        for argument in op["arguments"]:
            written = f"{argument['source']} → " if argument["source"] else "(default) "
            lines.append(f"    {argument['name']} = {written}{argument['value']}")
    else:
        lines.append("  arguments: none captured.")
    if op["inputs"]:
        lines.append("  consumed: " + ", ".join(_builder_line(trace, trace["builders"][b]) for b in op["inputs"]))
    if builder["kind"] == "BuildPart":
        lines.append(f"  faces of the result it made: {_faces(op['faces'])}")
        if op["via_faces"]:
            lines.append(f"  faces made inside what it brought in: {_faces(op['via_faces'])}")
    if op["site"]:
        lines.append(f"  call stack: {_stack(op['site'])}")
        lines.append("  source:")
        lines += [f"    {line}" for line in _excerpt(trace, op["site"][-1])]
    else:
        lines.append("  source unavailable: no operation call site was captured.")
    return lines


def face_maker(trace: Json, face: int) -> Json | None:
    return next((op for op in trace["ops"] if face in op["faces"]), None)


def _quantity(quantity: Json) -> str:
    value = quantity["value"]
    match quantity["kind"]:
        case "length":
            text = f"{value:.3f} mm"
        case "area":
            text = f"{value:.3f} mm²"
        case "angle":
            text = f"{value:.3f}°"
        case _:
            text = _vec(value)
    return f"{quantity['label']} {text}"


def entity_text(entity: Json | None) -> str:
    """A measured entity (``measure.measure``): its geometry and properties; "?" when it was not measured."""
    return f"{entity['kind']}; " + "; ".join(_quantity(q) for q in entity["properties"]) if entity else "?"


def results_text(results: list[Json]) -> str:
    """What relates measured entities: distances, angle, totals."""
    return "between them: " + "; ".join(_quantity(q) for q in results)


Measured = dict[str, Json]
"""Viewer path -> its measured entity (``measure.measure``'s ``entities``)."""


def indent(text: str) -> str:
    return "\n".join(f"  {line}" if line else line for line in text.splitlines())


def shape_chain(root: Json, node_id: str) -> list[Json] | None:
    """The shape tree's nodes from the root down to ``node_id``."""
    if root["id"] == node_id:
        return [root]
    for child in root.get("parts", []):
        chain = shape_chain(child, node_id)
        if chain is not None:
            return [root, *chain]
    return None


def leaves(node: Json) -> list[Json]:
    return [node] if "parts" not in node else [leaf for child in node["parts"] for leaf in leaves(child)]


def owning_part(built: Json, path: str, parts: set[str]) -> str | None:
    """Resolve only captured instance ownership, never an instance's display name."""
    from .assembly import owning_part as resolve

    return resolve(built["scene"], path, parts)


def pick_path(path: str) -> tuple[str, str, int] | None:
    """The viewer leaf, topology list and index of an entity path."""
    match = _PICK.fullmatch(path)
    return (match["leaf"], match["topo"], int(match["index"])) if match else None


def composite_summary(built: Json) -> str:
    members = leaves(built["scene"]["data"]["shapes"])
    return f"{len(members)} instances, built in {built['build_seconds']:.2f} s"


def instance_tree(built: Json, depth: int = 3) -> str:
    """The shape tree of an assembly or a file's parts, groups to ``depth``, with ids for ``select``."""
    lines: list[str] = []

    def walk(node: Json, level: int) -> None:
        children = node.get("parts")
        count = f" ({len(leaves(node))} instances)" if children is not None else ""
        lines.append(f"{'  ' * level}{node['name']}{count}  [{node['id']}]")
        if children is not None and level < depth:
            for child in children:
                walk(child, level + 1)

    walk(built["scene"]["data"]["shapes"], 0)
    return "\n".join(lines)


# ---- comparing two builds of a part ----


def _signature(trace: Json, op: Json) -> tuple[str, ...]:
    """What identifies an operation across edits: its kind and mode, its builder's kind, the function it was called
    in (line numbers move with every edit above them)."""
    site = op["site"][-1] if op["site"] else {"file": "", "function": ""}
    return (op["kind"], op["mode"], trace["builders"][op["builder"]]["kind"], site["file"], site["function"])


def _changes(before: Json, after: Json) -> list[str]:
    old = {a["name"]: a for a in before["arguments"]}
    changed = []
    for argument in after["arguments"]:
        previous = old.get(argument["name"])
        if previous is None:
            changed.append(f"{argument['name']} = {_flat(argument['value'], 60)} (new)")
        elif previous["value"] != argument["value"] or previous["source"] != argument["source"]:
            parts = []
            if previous["source"] != argument["source"]:
                parts.append(
                    f"{_flat(previous['source'] or '(default)', 50)} → {_flat(argument['source'] or '(default)', 50)}"
                )
            if previous["value"] != argument["value"]:
                parts.append(f"value {_flat(previous['value'], 50)} → {_flat(argument['value'], 50)}")
            changed.append(f"{argument['name']}: {'; '.join(parts)}")
    return changed


def _number(before: float, after: float, unit: str) -> str:
    if abs(after - before) <= 1e-9 * max(1.0, abs(before)):
        return f"{after:.2f} {unit} (unchanged)"
    percent = f", {100 * (after - before) / before:+.2f} %" if before else ""
    return f"{before:.2f} → {after:.2f} {unit}{percent}"


def _close(before: Any, after: Any) -> bool:
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return abs(before - after) <= 1e-9 * max(1.0, abs(before), abs(after))
    if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        return len(before) == len(after) and all(_close(a, b) for a, b in zip(before, after, strict=True))
    if isinstance(before, dict) and isinstance(after, dict):
        return before.keys() == after.keys() and all(_close(before[key], after[key]) for key in before)
    return before == after


def _bounds(built: Json) -> Json | None:
    exact = built.get("result", {}).get("bbox")
    if exact is not None:
        return {"min": exact["min"], "max": exact["max"], "basis": "exact B-rep", "unit": "mm"}
    box = built.get("scene", {}).get("data", {}).get("shapes", {}).get("bb")
    if box and all(f"{axis}{end}" in box for axis in "xyz" for end in ("min", "max")):
        return {
            "min": [box[f"{axis}min"] for axis in "xyz"],
            "max": [box[f"{axis}max"] for axis in "xyz"],
            "basis": "viewer mesh bounds",
            "unit": "mm",
        }
    return None


def _multiply_quaternions(a: list[float], b: list[float]) -> list[float]:
    x, y, z, w = a
    u, v, s, t = b
    return [
        w * u + x * t + y * s - z * v,
        w * v - x * s + y * t + z * u,
        w * s + x * v - y * u + z * t,
        w * t - x * u - y * v - z * s,
    ]


def _placement(chain: list[Any]) -> Json:
    translation, rotation = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]
    for point, quaternion in chain:
        # Scene quaternions use (x,y,z,w). Rotate each local offset into its parent's frame.
        vector = _multiply_quaternions(
            _multiply_quaternions(rotation, [*point, 0.0]),
            [-rotation[0], -rotation[1], -rotation[2], rotation[3]],
        )
        translation = [a + b for a, b in zip(translation, vector[:3], strict=True)]
        rotation = _multiply_quaternions(rotation, quaternion)
    # q and -q represent the same orientation.
    if next((v for v in reversed(rotation) if abs(v) > 1e-12), 1.0) < 0:
        rotation = [-value for value in rotation]
    return {"translation": translation, "quaternion": rotation}


def _instances(built: Json) -> dict[str, Json]:
    root = built.get("scene", {}).get("data", {}).get("shapes")
    instances: dict[str, Json] = {}
    if root is None:
        return instances
    owners = built.get("scene", {}).get("part_of", {})

    def walk(node: Json, chain: list[Any]) -> None:
        locations = [*chain, node["loc"]] if node.get("loc") is not None else chain
        if "parts" in node:
            for child in node["parts"]:
                walk(child, locations)
        else:
            path = node["id"]
            instances[path] = {
                "path": path,
                "name": node.get("name"),
                "part": owners.get(path),
                "transform": _placement(locations),
                "locations": locations,
            }

    walk(root, [])
    return instances


def structured_diff(before: Json | None, after: Json) -> Json:
    """Observed geometry/placement changes, separate from source revision and build identity.

    This is a summary of captured bounds, metrics, operations and scene placements, not a proof of geometric
    equality: equal volumes and bounds do not imply equal shapes.
    """
    new_bounds = _bounds(after)
    if before is None:
        return {"available": False, "reason": "no earlier build", "bounds": {"before": None, "after": new_bounds}}
    old_bounds = _bounds(before)
    bounds: Json = {"before": old_bounds, "after": new_bounds, "changed": not _close(old_bounds, new_bounds)}
    if old_bounds is not None and new_bounds is not None:
        low = [b - a for a, b in zip(old_bounds["min"], new_bounds["min"], strict=True)]
        high = [b - a for a, b in zip(old_bounds["max"], new_bounds["max"], strict=True)]
        bounds.update(delta_min=low, delta_max=high, translation=low if _close(low, high) else None)
    old_instances, new_instances = _instances(before), _instances(after)
    added = [new_instances[path] for path in sorted(new_instances.keys() - old_instances.keys())]
    removed = [old_instances[path] for path in sorted(old_instances.keys() - new_instances.keys())]
    changed = []
    for path in sorted(old_instances.keys() & new_instances.keys()):
        old, new = old_instances[path], new_instances[path]
        transform_changed = not _close(old["transform"], new["transform"])
        owner_changed = old["part"] != new["part"]
        if transform_changed or owner_changed:
            changed.append(
                {
                    "path": path,
                    "before": old,
                    "after": new,
                    "transform_changed": transform_changed,
                    "part_changed": owner_changed,
                    "translation_delta": [
                        b - a
                        for a, b in zip(
                            old["transform"]["translation"],
                            new["transform"]["translation"],
                            strict=True,
                        )
                    ],
                }
            )
    origin = None
    if "result" in before and "result" in after and len(old_instances) == len(new_instances) == 1:
        old_origin = next(iter(old_instances.values()))["transform"]["translation"]
        new_origin = next(iter(new_instances.values()))["transform"]["translation"]
        origin = {
            "before": old_origin,
            "after": new_origin,
            "changed": not _close(old_origin, new_origin),
            "delta": [b - a for a, b in zip(old_origin, new_origin, strict=True)],
            "basis": "recorded scene placement",
        }
    metrics = {
        key: {"before": before.get("result", {}).get(key), "after": after.get("result", {}).get(key)}
        for key in ("volume", "faces", "attributed")
        if key in before.get("result", {}) or key in after.get("result", {})
    }
    operations = []
    a = [_signature(before, op) for op in before.get("ops", [])]
    b = [_signature(after, op) for op in after.get("ops", [])]
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2), strict=True):
                arguments = _changes(before["ops"][i], after["ops"][j])
                if arguments:
                    operations.append({"kind": "changed", "before": i, "after": j, "arguments": arguments})
        else:
            operations.extend({"kind": "removed", "before": i} for i in range(i1, i2))
            operations.extend({"kind": "added", "after": j} for j in range(j1, j2))
    return {
        "available": True,
        "changed": bool(
            bounds["changed"]
            or added
            or removed
            or changed
            or operations
            or any(not _close(value["before"], value["after"]) for value in metrics.values())
        ),
        "scope": "captured metrics, bounds, operation arguments and scene placement; not geometric equality",
        "bounds": bounds,
        "origin": origin,
        "metrics": metrics,
        "operations": operations,
        "instances": {"added": added, "removed": removed, "changed": changed},
    }


def _spatial_lines(diff: Json) -> list[str]:
    lines = []
    bounds = diff["bounds"]
    old, new = bounds["before"], bounds["after"]
    if old is not None and new is not None:
        lines.append(
            f"absolute bounds ({new['basis']}): {_vec(old['min'])} to {_vec(old['max'])} → "
            f"{_vec(new['min'])} to {_vec(new['max'])} mm"
        )
        translation = bounds.get("translation")
        if translation is not None and not _close(translation, [0, 0, 0]):
            lines.append(f"bounds translation: {_vec(translation)} mm (size unchanged)")
    origin = diff.get("origin")
    if origin is not None and origin["changed"]:
        lines.append(f"part origin: {_vec(origin['before'])} → {_vec(origin['after'])} mm")
    for instance in diff["instances"]["added"]:
        lines.append(f"+ instance {instance['path']}")
    for instance in diff["instances"]["removed"]:
        lines.append(f"- instance {instance['path']}")
    for instance in diff["instances"]["changed"]:
        lines.append(
            f"~ instance {instance['path']}: translation delta {_vec(instance['translation_delta'])} mm; "
            f"transform {instance['before']['transform']} → {instance['after']['transform']}"
        )
        if instance["part_changed"]:
            lines.append(f"  owning part: {instance['before']['part']} → {instance['after']['part']}")
    return lines


def compare(before: Json | None, after: Json) -> str:
    """Captured bounds, placement and operation changes, retaining the readable operation comparison."""
    if before is None:
        return "No earlier build of this part to compare with."
    diff = structured_diff(before, after)
    if "result" not in before or "result" not in after:
        return "\n".join(_spatial_lines(diff)) or "No captured instance placement or bounds changes."
    lines = []
    old, new = before["result"], after["result"]
    if "volume" in old and "volume" in new:
        lines.append(f"volume: {_number(old['volume'], new['volume'], 'mm³')}")
        size = [
            f"{axis} {_number(b0[1] - b0[0], b1[1] - b1[0], 'mm')}"
            for axis, b0, b1 in zip(
                "XYZ",
                zip(old["bbox"]["min"], old["bbox"]["max"], strict=True),
                zip(new["bbox"]["min"], new["bbox"]["max"], strict=True),
                strict=True,
            )
        ]
        lines.append("bounding box: " + "; ".join(size))
    lines.extend(_spatial_lines(diff))
    lines.append(f"faces: {old['faces']} → {new['faces']}, traced {old['attributed']} → {new['attributed']}")
    a = [_signature(before, op) for op in before["ops"]]
    b = [_signature(after, op) for op in after["ops"]]
    operations = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2), strict=True):
                changed = _changes(before["ops"][i], after["ops"][j])
                if changed:
                    operations.append(f"~ {op_line(after, after['ops'][j])}")
                    operations += [f"    {change}" for change in changed]
            continue
        operations += [f"- {op_line(before, before['ops'][i])}" for i in range(i1, i2)]
        operations += [f"+ {op_line(after, after['ops'][j])}" for j in range(j1, j2)]
    if operations:
        lines.append(f"operations: {len(before['ops'])} → {len(after['ops'])}")
        lines += [f"  {line}" for line in operations]
    else:
        lines.append(f"operations: the same {len(after['ops'])}, called with the same arguments")
    return "\n".join(lines)
