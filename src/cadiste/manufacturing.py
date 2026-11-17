"""Manufacturing evidence from an exact retained generation, never a model rebuild.

Mesh estimates are geometric diagnostics, not slicing, support generation or a printability certificate.
"""

from __future__ import annotations

import json
import math
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from build123d import Compound, Location
from build123d.topology import Shape

# OCP is a compiled extension without type stubs.
# pyright: reportAttributeAccessIssue=false
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.gp import gp_Trsf
from OCP.TopLoc import TopLoc_Location

from .assembly import owning_part
from .measure import MeasureError, load
from .three_mf import mesh_geometry

_GEOMETRY_LOCK = threading.RLock()


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError(f"{label} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _vector(value: Any, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{label} must contain three finite numbers")
    return [_number(item, label) for item in value]


def orient_geometry(
    shape: Shape,
    rotation: list[float] | None = None,
    translation: list[float] | None = None,
    *,
    on_bed: bool = False,
) -> Shape:
    """Copy then apply fixed world XYZ Euler degrees (Rz Ry Rx), mm translation, optionally lift Z=0.

    The retained/source shape, its children and shared OCCT topology are never modified.
    """
    if not isinstance(on_bed, bool):
        raise ValueError("on_bed must be boolean")
    rx, ry, rz = (
        math.radians(value % 360) for value in _vector(rotation if rotation is not None else [0, 0, 0], "rotation")
    )
    translation = _vector(translation if translation is not None else [0, 0, 0], "translation")
    sx, sy, sz = math.sin(rx), math.sin(ry), math.sin(rz)
    cx, cy, cz = math.cos(rx), math.cos(ry), math.cos(rz)
    rows = (
        (cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx),
        (sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx),
        (-sy, cy * sx, cy * cx),
    )
    transform = gp_Trsf()
    transform.SetValues(*(value for row, shift in zip(rows, translation, strict=True) for value in (*row, shift)))
    location = Location(TopLoc_Location(transform))

    # Copy the assembly tree as well as topology: a transformed compound must retain its object boundaries.
    def copy_node(node: Shape) -> Shape:
        copied = Shape.cast(BRepBuilderAPI_Copy(node.wrapped, True, False).Shape())
        copied.label = node.label
        copied.color = node.color
        if node.children:
            original_location = node.location
            copied.children = [copy_node(child) for child in node.children]
            copied.location = original_location
        return copied

    result = copy_node(shape)
    result.location = location * result.location
    if on_bed:
        result.location = Location((0, 0, -result.bounding_box().min.Z)) * result.location
    return result


def _orientation(config: dict[str, Any], name: str | None) -> dict[str, Any]:
    if name is None:
        return {}
    if not isinstance(name, str) or name not in config.get("orientations", {}):
        raise ValueError(f"unknown registered print orientation: {name!r}")
    pose = config["orientations"][name]
    if not isinstance(pose, dict) or pose.keys() - {"rotation", "translation", "on_bed"} or "rotation" not in pose:
        raise ValueError(f"orientation {name!r} requires rotation and optional translation/on_bed")
    result: dict[str, Any] = {"rotation": _vector(pose["rotation"], f"orientation {name!r} rotation")}
    if "translation" in pose:
        result["translation"] = _vector(pose["translation"], f"orientation {name!r} translation")
    if "on_bed" in pose:
        if not isinstance(pose["on_bed"], bool):
            raise ValueError(f"orientation {name!r} on_bed must be boolean")
        result["on_bed"] = pose["on_bed"]
    return result


def _bounds(shape: Shape) -> dict[str, list[float]]:
    box = shape.bounding_box()
    minimum = [box.min.X, box.min.Y, box.min.Z]
    maximum = [box.max.X, box.max.Y, box.max.Z]
    return {"min": minimum, "max": maximum, "dimensions": [b - a for a, b in zip(minimum, maximum, strict=True)]}


def _mesh_report(
    vertices: list[tuple[float, float, float]], triangles: list[tuple[int, int, int]], angle: float
) -> dict[str, Any]:
    edges: Counter[tuple[int, int]] = Counter()
    directions: Counter[tuple[int, int]] = Counter()
    links: dict[int, list[tuple[int, int]]] = defaultdict(list)
    degenerate = 0
    duplicates = len(triangles) - len({tuple(sorted(triangle)) for triangle in triangles})
    area = overhang_area = bridge_area = 0.0
    downward: list[tuple[int, int, int]] = []
    bottom = min(point[2] for point in vertices)
    for triangle in triangles:
        a, b, c = (vertices[index] for index in triangle)
        u = [b[k] - a[k] for k in range(3)]
        v = [c[k] - a[k] for k in range(3)]
        normal = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        length = math.sqrt(sum(value * value for value in normal))
        if len(set(triangle)) != 3 or length <= 1e-15:
            degenerate += 1
            continue
        area += length / 2
        for x, y, z in (
            (triangle[0], triangle[1], triangle[2]),
            (triangle[1], triangle[2], triangle[0]),
            (triangle[2], triangle[0], triangle[1]),
        ):
            edges[(min(x, y), max(x, y))] += 1
            directions[(x, y)] += 1
            links[x].append((y, z))
        # Bed-contact triangles are not unsupported undersides. No layer height is assumed.
        if max(a[2], b[2], c[2]) <= bottom + 1e-6 and abs(bottom) <= 1e-6:
            continue
        if normal[2] / length < -math.cos(math.radians(angle)):
            overhang_area += length / 2
        if normal[2] / length < -math.cos(math.radians(5)):
            bridge_area += length / 2
            downward.append(triangle)
    nonmanifold_vertices = 0
    for segments in links.values():
        adjacency: dict[int, set[int]] = defaultdict(set)
        degree: Counter[int] = Counter()
        for a, b in segments:
            adjacency[a].add(b)
            adjacency[b].add(a)
            degree[a] += 1
            degree[b] += 1
        unseen = set(adjacency)
        pending = [unseen.pop()]
        while pending:
            for neighbor in adjacency[pending.pop()]:
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    pending.append(neighbor)
        if unseen or any(count != 2 for count in degree.values()):
            nonmanifold_vertices += 1
    boundary = sum(count == 1 for count in edges.values())
    nonmanifold = sum(count > 2 for count in edges.values())
    inconsistent = sum(count == 2 and directions[(a, b)] != directions[(b, a)] for (a, b), count in edges.items())
    # Connected downward patches provide an extent estimate, not a claim that their ends are anchored.
    patches: list[float] = []
    by_vertex: dict[int, list[int]] = defaultdict(list)
    for index, triangle in enumerate(downward):
        for vertex in triangle:
            by_vertex[vertex].append(index)
    remaining = set(range(len(downward)))
    while remaining:
        pending = [remaining.pop()]
        points: set[int] = set()
        while pending:
            for vertex in downward[pending.pop()]:
                points.add(vertex)
                for neighbor in by_vertex[vertex]:
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        pending.append(neighbor)
        patches.append(
            math.hypot(
                *(max(vertices[i][axis] for i in points) - min(vertices[i][axis] for i in points) for axis in (0, 1))
            )
        )
    manifold = bool(triangles) and not any(
        (boundary, nonmanifold, nonmanifold_vertices, inconsistent, degenerate, duplicates)
    )
    return {
        "vertices": len(vertices),
        "triangles": len(triangles),
        "closed_oriented_manifold": manifold,
        "boundary_edges": boundary,
        "nonmanifold_edges": nonmanifold,
        "nonmanifold_vertices": nonmanifold_vertices,
        "inconsistent_winding_edges": inconsistent,
        "degenerate_triangles": degenerate,
        "duplicate_triangles": duplicates,
        "self_intersections": "not_checked",
        "surface_area_mm2": area,
        "overhang": {"threshold_degrees_from_vertical": angle, "area_mm2": overhang_area},
        "bridge_candidates": {
            "area_mm2": bridge_area,
            "patch_count": len(patches),
            "max_xy_patch_extent_mm": max(patches, default=0),
        },
        "evidence": "non_slicer_geometric_estimate",
    }


@dataclass(frozen=True)
class PreparedExport:
    shape: Shape
    report: dict[str, Any]
    metadata: dict[str, Any]
    stem: str


def _prepare_export(
    builds: Any,
    target: dict[str, Any],
    only: list[str] | None = None,
    group: str | None = None,
    orientation: str | None = None,
    expected_revision: str | None = None,
    *,
    build_id: str | None = None,
    hidden: list[str] | None = None,
    overhang_angle: float = 45,
    analyze: bool = True,
    prepared_build: Any = None,
) -> PreparedExport:
    """Select, orient and report one retained generation; never silently rebuild missing geometry."""
    if not isinstance(analyze, bool):
        raise ValueError("analyze must be boolean")
    angle = _number(overhang_angle, "overhang_angle")
    if not 0 < angle < 90:
        raise ValueError("overhang_angle must be greater than 0 and less than 90 degrees")
    build = (
        prepared_build
        if prepared_build is not None
        else (builds.captured(target, build_id) if build_id is not None else builds.latest(target))
    )
    if build is None or build.body is None or build.shapes is None:
        raise MeasureError("manufacturing requires retained whole geometry; build/show the target explicitly first")
    metadata = json.loads(build.body)
    if expected_revision is not None and expected_revision != metadata.get("source_revision"):
        raise ValueError("source revision mismatch for retained manufacturing geometry")
    shapes = load(build.shapes)
    project, local_target, _ = builds.project.resolve(target)
    config = getattr(project, "manufacturing", {}) or {}
    if not isinstance(config, dict):
        raise ValueError("manufacturing configuration must be an object")
    if config.keys() - {"orientations", "groups", "part_orientations", "bed"}:
        raise ValueError("unknown manufacturing configuration fields")
    for field in ("orientations", "groups"):
        mapping = config.get(field, {})
        if not isinstance(mapping, dict) or any(not isinstance(name, str) or not name for name in mapping):
            raise ValueError(f"manufacturing {field} must be a mapping of non-empty aliases")
    for name in config.get("orientations", {}):
        _orientation(config, name)
    for name, entries in config.get("groups", {}).items():
        if (
            not isinstance(entries, list)
            or not entries
            or any(not isinstance(path, str) or not path for path in entries)
        ):
            raise ValueError(f"export group {name!r} requires explicit non-empty instance paths")
    pose = _orientation(config, orientation)
    per_part = config.get("part_orientations", {})
    if not isinstance(per_part, dict) or any(
        not isinstance(key, str) or not key or not isinstance(alias, str) or not alias
        for key, alias in per_part.items()
    ):
        raise ValueError("part_orientations must map captured owners or exact instance paths to orientation aliases")
    for alias in per_part.values():
        _orientation(config, alias)
    scene = metadata.get("scene", {})
    paths = set(shapes.leaves)
    if local_target.get("kind") == "part":
        final_path = scene.get("part")
        if not isinstance(final_path, str):
            raise MeasureError("retained part geometry has no captured final-part path; explicitly rebuild it")
        paths = {path for path in paths if path == final_path or path.startswith(final_path + "/")}
        if not paths:
            raise MeasureError("the captured final part has no retained whole geometry")
    root = scene.get("data", {}).get("shapes", {}).get("id")
    if root is None:
        root = next(iter(paths), "").split("/", 2)
        root = "/" + root[1] if len(root) > 1 else ""

    def resolve_path(path: Any) -> set[str]:
        if not isinstance(path, str) or not path:
            raise ValueError("manufacturing paths must be non-empty strings")
        canonical = path if path.startswith("/") else f"{root}/{path}"
        members = {leaf for leaf in paths if leaf == canonical or leaf.startswith(canonical + "/")}
        if not members:
            raise ValueError(f"manufacturing path is not in this retained target: {path!r}")
        return members

    selected = set(paths)
    if group is not None:
        groups = config.get("groups", {})
        if not isinstance(group, str) or group not in groups:
            raise ValueError(f"unknown registered export group: {group!r}")
        entries = groups[group]
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"export group {group!r} must contain explicit instance/group paths")
        selected = set().union(*(resolve_path(path) for path in entries))
    owners = {path: owning_part(scene, path) for path in paths}
    if local_target.get("kind") == "part":
        owners = {path: owner or target.get("name") for path, owner in owners.items()}
    if only is not None:
        if local_target.get("kind") != "file":
            raise ValueError("only is a folder part-name allowlist; use a registered group for instances")
        if hidden:
            raise ValueError("only and hidden cannot be combined")
        if not isinstance(only, list) or any(not isinstance(name, str) or not name for name in only):
            raise ValueError("only must be a list of non-empty captured part names")
        if unknown := set(only) - set(owners.values()):
            raise ValueError(f"parts are not captured in this target: {', '.join(sorted(unknown))}")
        selected &= {path for path, owner in owners.items() if owner in only}
    if hidden is not None:
        if not isinstance(hidden, list):
            raise ValueError("hidden must be a list of viewer paths")
        for path in hidden:
            selected -= resolve_path(path)
    if not selected:
        raise ValueError("nothing to export: no manufacturing objects remain")
    objects = []
    by_path: dict[str, Shape] = {}
    reports = []
    bed = config.get("bed")
    if bed is not None:
        if not isinstance(bed, dict) or "size" not in bed or bed.keys() - {"size", "origin"}:
            raise ValueError("bed requires size and optional origin in mm")
        size = _vector(bed["size"], "bed size")
        if any(value <= 0 for value in size):
            raise ValueError("bed size must be positive")
        origin = _vector(bed.get("origin", [0, 0, 0]), "bed origin")
        bed = {"size": size, "origin": origin}
    for path in shapes.leaves:
        if path not in selected:
            continue
        shape = Shape.cast(shapes.solid(path))
        alias = orientation if orientation is not None else per_part.get(path, per_part.get(owners[path]))
        object_pose = pose if orientation is not None else _orientation(config, alias)
        shape = orient_geometry(shape, **object_pose)
        shape.label = scene.get("instances", {}).get(path, {}).get("label") or path.rsplit("/", 1)[-1]
        by_path[path] = shape
        bounds = _bounds(shape)
        mesh = None
        if analyze:
            vertices, triangles = mesh_geometry(shape)
            mesh = _mesh_report(vertices, triangles, angle)
        placement: dict[str, Any] = {
            "on_z0": abs(bounds["min"][2]) <= 1e-6,
            "below_z0": bounds["min"][2] < -1e-6,
            "within_registered_bed": None,
        }
        if bed is not None:
            placement["within_registered_bed"] = all(
                bounds["min"][axis] >= bed["origin"][axis] - 1e-6
                and bounds["max"][axis] <= bed["origin"][axis] + bed["size"][axis] + 1e-6
                for axis in range(3)
            )
        objects.append(shape)
        reports.append(
            {
                "path": path,
                "owner": owners[path],
                "orientation": alias,
                "transform": object_pose,
                "bounds_mm": bounds,
                "solid_count": len(shape.solids()),
                "brep_valid": BRepCheck_Analyzer(shape.wrapped).IsValid() if analyze else None,
                "mesh": mesh,
                "bed_placement": placement,
            }
        )
    stem = str(target.get("name") or target.get("path") or "Parts").replace("/", "_")

    def assembled(node: dict[str, Any]) -> Shape | None:
        if "parts" not in node:
            return by_path.get(node["id"])
        children = [child for part in node["parts"] if (child := assembled(part)) is not None]
        if not children:
            return None
        label = scene.get("labels", {}).get(node.get("id")) or node.get("name") or stem
        return Compound(label=label, children=children)

    tree = scene.get("data", {}).get("shapes", {})
    combined = assembled(tree) if "parts" in tree else None
    if combined is None:
        combined = Compound(label=stem, children=objects)
    warnings = []
    if analyze and any(not item["mesh"]["closed_oriented_manifold"] for item in reports):
        warnings.append("One or more export meshes are not closed oriented manifolds.")
    if any(item["bed_placement"]["below_z0"] for item in reports):
        warnings.append("Geometry extends below Z=0; choose a registered on_bed orientation before printing.")
    if bed is None:
        warnings.append("No bed envelope registered; printer fit is unavailable.")
    elif any(not item["bed_placement"]["within_registered_bed"] for item in reports):
        warnings.append("One or more objects extend outside the registered bed envelope.")
    report = {
        "target": target,
        "build_id": metadata.get("build_id"),
        "source_revision": metadata.get("source_revision"),
        "source_fingerprint": metadata.get("source_fingerprint"),
        "retained_geometry": True,
        "geometry_only": True,
        "filament_presets": "not_supplied",
        "group": group,
        "orientation": orientation,
        "registered": {"orientations": list(config.get("orientations", {})), "groups": list(config.get("groups", {}))},
        "object_count": len(objects),
        "solid_count": sum(item["solid_count"] for item in reports),
        "bounds_mm": _bounds(combined),
        "objects": reports,
        "bed": bed,
        "warnings": warnings,
        "evidence": "non_slicer_geometric_estimate" if analyze else "retained_geometry_only",
        "analysis_performed": analyze,
        "limitations": [
            "Overhang and bridge candidates use tessellated surface normals, "
            "not layers, toolpaths, material or support settings.",
            "Bridge patch extent is an XY bounding-box diagonal; endpoint support "
            "and actual printable bridge span are not established.",
            "Manifold topology does not rule out self-intersections or certify printability; slicing is not performed.",
            "Bed envelope fit does not check keep-out zones, adhesion, collisions between objects or printer motion.",
        ],
    }
    if not analyze:
        report["limitations"].insert(0, "Mesh/manifold/overhang/bridge and BRep validity analysis was not requested.")
    return PreparedExport(combined, report, metadata, stem)


def prepare_export(
    builds: Any,
    target: dict[str, Any],
    only: list[str] | None = None,
    group: str | None = None,
    orientation: str | None = None,
    expected_revision: str | None = None,
    *,
    build_id: str | None = None,
    hidden: list[str] | None = None,
    overhang_angle: float = 45,
    analyze: bool = True,
    prepared_build: Any = None,
) -> PreparedExport:
    """Prepare retained geometry under the shared measurement lock (fallback for headless callers)."""
    with getattr(builds, "geometry_lock", _GEOMETRY_LOCK):
        return _prepare_export(
            builds,
            target,
            only,
            group,
            orientation,
            expected_revision,
            build_id=build_id,
            hidden=hidden,
            overhang_angle=overhang_angle,
            analyze=analyze,
            prepared_build=prepared_build,
        )


def preflight(
    builds: Any,
    target: dict[str, Any],
    only: list[str] | None = None,
    group: str | None = None,
    orientation: str | None = None,
    overhang_angle: float = 45,
    expected_revision: str | None = None,
    *,
    build_id: str | None = None,
    hidden: list[str] | None = None,
) -> dict[str, Any]:
    return prepare_export(
        builds,
        target,
        only,
        group,
        orientation,
        expected_revision,
        build_id=build_id,
        hidden=hidden,
        overhang_angle=overhang_angle,
    ).report
