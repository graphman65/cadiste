"""The viewer's state as text for a coding agent (``cadiste mcp``): a traced part's feature tree, one feature in
detail, what the user selected in a tab, and how a new build differs from the previous one.

Everything works on the JSON ``cadiste export`` and ``cadiste mesh`` write (``web/src/types.ts``), so the server
answers from its cache without building. Node ids are the viewer's: ``o<op>``, ``b<builder>``, ``g<n>``.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable
from typing import Any

Json = dict[str, Any]

_EXCERPT = 3
"""Lines of source shown above and below a call."""
_CODE = 100
"""Width a call's code is cut to in one-line listings."""
_PICK = re.compile(r"(?P<leaf>.+)/(?P<topo>faces|edges|vertices)/(?P=topo)_(?P<index>\d+)")
_TOPO = {"faces": "face", "edges": "edge", "vertices": "vertex"}
_LETTER = {"faces": "f", "edges": "e", "vertices": "v"}


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
        return []
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
            lines += _describe_op(trace, trace["ops"][node["op"]])
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


def _describe_op(trace: Json, op: Json) -> list[str]:
    builder = trace["builders"][op["builder"]]
    lines = [op_line(trace, op), f"  in {_builder_line(trace, builder)}"]
    if op["arguments"]:
        lines.append("  arguments (parameter = expression written → value):")
        for argument in op["arguments"]:
            written = f"{_flat(argument['source'], 80)} → " if argument["source"] else "(default) "
            lines.append(f"    {argument['name']} = {written}{_flat(argument['value'], 120)}")
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
    return lines


def _op_of_face(trace: Json, face: int) -> Json | None:
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


def _scene_owner(trace: Json, leaf: str) -> str:
    """What a leaf of a part's scene is: the part, a sketch, a sketch element."""
    scene = trace["scene"]
    if leaf == scene["part"]:
        return "the part"
    for builder, sketch in scene["sketches"].items():
        if sketch["path"] == leaf:
            return f"sketch {_builder_line(trace, trace['builders'][int(builder)])}"
    for op, element in scene["elements"].items():
        if element == leaf:
            return f"sketch element {op_line(trace, trace['ops'][int(op)])}"
    return leaf


Measured = dict[str, Json]
"""Viewer path -> its measured entity (``measure.measure``'s ``entities``)."""
Adjacent = Callable[[str], list[int] | None]
"""The faces an edge or vertex bounds (``measure.adjacent_faces``)."""


def _pick_lines(trace: Json, path: str, measured: Measured, adjacent: Adjacent) -> list[str]:
    match = _PICK.fullmatch(path)
    if match is None:
        return [f"{path}: not a face, an edge or a vertex"]
    leaf, topo, index = match["leaf"], match["topo"], int(match["index"])
    geometry = entity_text(measured.get(path))
    lines = [f"{_TOPO[topo]} {_LETTER[topo]}{index} of {_scene_owner(trace, leaf)}: {geometry}", f"  path {path}"]
    if leaf != trace["scene"]["part"] or not trace["scene"]["face_ids"]:
        return lines
    if topo == "faces":
        op = _op_of_face(trace, index)
        lines.append(f"  made by {op_line(trace, op)}" if op else "  made by no traced operation")
    else:
        faces = adjacent(path)
        if faces is not None:
            lines.append(f"  bounds {len(faces)} face(s):")
            for face in faces:
                op = _op_of_face(trace, face)
                lines.append(f"    f{face} ← {op_line(trace, op) if op else 'no traced operation'}")
    return lines


def part_selection(
    trace: Json, selected: str | None, picks: list[str], measured: Measured, results: list[Json], adjacent: Adjacent
) -> str:
    """What is selected in a part: the feature tree's node, and the faces, edges and vertices picked to measure."""
    sections = []
    if selected is not None:
        sections.append("Selected feature:\n" + indent(describe(trace, selected)))
    if picks:
        lines = [line for path in picks for line in _pick_lines(trace, path, measured, adjacent)]
        if results:
            lines.append(results_text(results))
        sections.append(f"Faces, edges and vertices ({len(picks)}):\n" + indent("\n".join(lines)))
    if not sections:
        return "No feature, face, edge or vertex is selected: the whole part."
    return "\n\n".join(sections)


def indent(text: str) -> str:
    return "\n".join(f"  {line}" if line else line for line in text.splitlines())


def _shape_chain(root: Json, node_id: str) -> list[Json] | None:
    """The shape tree's nodes from the root down to ``node_id``."""
    if root["id"] == node_id:
        return [root]
    for child in root.get("parts", []):
        chain = _shape_chain(child, node_id)
        if chain is not None:
            return [root, *chain]
    return None


def _leaves(node: Json) -> list[Json]:
    return [node] if "parts" not in node else [leaf for child in node["parts"] for leaf in _leaves(child)]


def _instance_lines(built: Json, node_id: str, parts: set[str]) -> list[str]:
    root = built["scene"]["data"]["shapes"]
    chain = _shape_chain(root, node_id)
    if chain is None:
        return [f"{node_id}: not in this build"]
    node = chain[-1]
    names = " / ".join(item["name"] for item in chain)
    kind = "instance" if "parts" not in node else f"group of {len(_leaves(node))} instances"
    lines = [f"{kind} {names}", f"  id {node_id}"]
    # The server matched the assembly's instances to the parts; a file's parts are named after theirs.
    part = built["scene"].get("part_of", {}).get(node_id)
    known = [part] if part else [item["name"] for item in chain if item["name"] in parts]
    if known:
        lines.append(f"  an instance of part {known[-1]!r}: show(part={known[-1]!r}) opens its feature tree")
    return lines


def composite_selection(
    built: Json,
    instance: str | None,
    picks: list[str],
    hidden: list[str],
    measured: Measured,
    results: list[Json],
    parts: set[str],
) -> str:
    """What is selected in the assembly, or in a file's parts viewed together (untraced: instances only)."""
    sections = []
    if instance is not None:
        sections.append("Selected:\n" + indent("\n".join(_instance_lines(built, instance, parts))))
    if picks:
        lines = []
        for path in picks:
            match = _PICK.fullmatch(path)
            if match is None:
                lines.append(f"{path}: not a face, an edge or a vertex")
                continue
            topo, index = match["topo"], match["index"]
            lines.append(f"{_TOPO[topo]} {_LETTER[topo]}{index}: {entity_text(measured.get(path))}")
            lines += [f"  {line}" for line in _instance_lines(built, match["leaf"], parts)]
        if results:
            lines.append(results_text(results))
        sections.append(f"Faces, edges and vertices ({len(picks)}):\n" + indent("\n".join(lines)))
    if hidden:
        sections.append(f"Hidden instances: {len(hidden)}")
    if not sections:
        return "No instance, face, edge or vertex is selected: all of it."
    return "\n\n".join(sections)


def composite_summary(built: Json) -> str:
    leaves = _leaves(built["scene"]["data"]["shapes"])
    return f"{len(leaves)} instances, built in {built['build_seconds']:.2f} s"


def _picks_label(picks: list[str]) -> list[str]:
    labels = []
    for path in picks:
        match = _PICK.fullmatch(path)
        labels.append(f"{_TOPO[match['topo']]} {_LETTER[match['topo']]}{match['index']}" if match else path)
    return labels


def part_label(trace: Json, selected: str | None, picks: list[str]) -> str:
    """A short name for what is selected in a part: "#7 CrossPrism, face f12"."""
    items = []
    node = find_node(trace, selected) if selected is not None else None
    match node:
        case {"type": "op", "op": op}:
            items.append(f"#{op} {trace['ops'][op]['kind']}")
        case {"type": "builder", "builder": builder}:
            items.append(_builder_title(trace["builders"][builder]))
        case {"label": label}:
            items.append(f"{label}()")
    return ", ".join([*items, *_picks_label(picks)]) or "the whole part"


def composite_label(built: Json, instance: str | None, picks: list[str]) -> str:
    """A short name for what is selected in an assembly or a file's parts: the instance, the picks."""
    chain = _shape_chain(built["scene"]["data"]["shapes"], instance) if instance is not None else None
    items = [chain[-1]["name"]] if chain else []
    return ", ".join([*items, *_picks_label(picks)]) or "all of it"


def instance_tree(built: Json, depth: int = 3) -> str:
    """The shape tree of an assembly or a file's parts, groups to ``depth``, with ids for ``select``."""
    lines: list[str] = []

    def walk(node: Json, level: int) -> None:
        children = node.get("parts")
        count = f" ({len(_leaves(node))} instances)" if children is not None else ""
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


def compare(before: Json | None, after: Json) -> str:
    """How a part's new build differs from the previous one: size, faces, and the operations added, removed or
    called differently."""
    if before is None:
        return "No earlier build of this part to compare with."
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
