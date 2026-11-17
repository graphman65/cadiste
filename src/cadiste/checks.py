"""Exact checks of explicit solid pairs at the recorded pose and independent rigid samples.

Callers must serialize this module with the same Measurer lock used for all other OCP work.
No motion between samples is checked, and minimum distance is never a penetration depth.
"""

from __future__ import annotations

import math
from typing import Any

# OCP is a compiled extension without type stubs.
# pyright: reportAttributeAccessIssue=false
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp
from OCP.gp import gp_Trsf
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape

from .measure import MeasureError, Shapes

MAX_PAIRS = 128
MAX_SAMPLES = 128
MAX_EVALUATIONS = 4096


def _number(value: Any, label: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MeasureError(f"{label} must be a finite number")
    try:
        result = float(value)
    except OverflowError as error:
        raise MeasureError(f"{label} must be a finite number") from error
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise MeasureError(f"{label} must be finite" + (" and nonnegative" if nonnegative else ""))
    return result


def _pairs(value: Any, label: str, *, empty: bool) -> list[tuple[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_PAIRS or (not value and not empty):
        raise MeasureError(f"{label} must contain {'0' if empty else '1'} to {MAX_PAIRS} explicit pairs")
    result: list[tuple[str, str]] = []
    seen: set[frozenset[str]] = set()
    for pair in value:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise MeasureError(f"each {label} entry must contain two whole leaf paths")
        if any(not isinstance(path, str) or not path for path in pair) or pair[0] == pair[1]:
            raise MeasureError(f"each {label} entry must name two different whole leaf paths")
        key = frozenset(pair)
        if key in seen:
            raise MeasureError(f"{label} contains a repeated unordered pair")
        seen.add(key)
        result.append((pair[0], pair[1]))
    return result


def _vector(value: Any, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise MeasureError(f"{label} must contain three finite numbers")
    return [_number(item, label) for item in value]


def _samples(value: Any, paths: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_SAMPLES:
        raise MeasureError(f"samples must contain at most {MAX_SAMPLES} independent poses")
    result = []
    total = 0
    for sample in value:
        if not isinstance(sample, dict):
            raise MeasureError("each sample must be an explicit pose or coordinated poses object")
        coordinated = "poses" in sample
        if coordinated:
            if sample.keys() - {"name", "poses"}:
                raise MeasureError("coordinated sample accepts only name and poses")
            entries = sample["poses"]
            if not isinstance(entries, list) or not entries or len(entries) > MAX_PAIRS * 2:
                raise MeasureError(f"coordinated poses must contain 1 to {MAX_PAIRS * 2} entries")
        else:
            entries = [sample]
        if "name" in sample and (
            not isinstance(sample["name"], str) or not sample["name"] or len(sample["name"]) > 200
        ):
            raise MeasureError("sample name must contain 1 to 200 characters")
        poses = []
        seen = set()
        for entry in entries:
            required = {"path", "translation", "rotation"}
            optional = {"origin"} if coordinated else {"origin", "name"}
            if not isinstance(entry, dict) or not required <= entry.keys() or entry.keys() - required - optional:
                raise MeasureError("each sample requires path, translation and rotation, with optional origin")
            if not isinstance(entry["path"], str) or entry["path"] not in paths:
                raise MeasureError("each sample path must occur in an explicit pair")
            if entry["path"] in seen:
                raise MeasureError("a coordinated sample cannot move the same path twice")
            seen.add(entry["path"])
            pose = {
                "path": entry["path"],
                "translation": _vector(entry["translation"], "sample translation (mm)"),
                "rotation": _vector(entry["rotation"], "sample rotation (degrees)"),
            }
            if "origin" in entry:
                pose["origin"] = _vector(entry["origin"], "sample origin (world mm)")
            poses.append(pose)
        total += len(poses)
        if total > MAX_EVALUATIONS:
            raise MeasureError(f"samples exceed {MAX_EVALUATIONS} rigid pose evaluations")
        result.append({**sample, "poses": poses})
    return result


def _contacts(
    value: Any, keys: set[frozenset[str]], ignored: set[frozenset[str]]
) -> dict[frozenset[str], dict[str, float]]:
    if not isinstance(value, list) or len(value) > MAX_PAIRS:
        raise MeasureError(f"contact must contain at most {MAX_PAIRS} explicit pair configurations")
    result = {}
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) - {"paths", "max_distance", "max_overlap_volume"}
            or "paths" not in item
        ):
            raise MeasureError("contact requires paths and optional max_distance/max_overlap_volume")
        pair = _pairs([item["paths"]], "contact paths", empty=False)[0]
        key = frozenset(pair)
        if key not in keys or key in ignored or key in result:
            raise MeasureError("contact must name a unique requested, nonignored pair")
        result[key] = {
            "max_distance": _number(item.get("max_distance", 0), "contact max_distance", nonnegative=True),
            "max_overlap_volume": _number(
                item.get("max_overlap_volume", 0), "contact max_overlap_volume", nonnegative=True
            ),
        }
    return result


def _volume(shape: TopoDS_Shape) -> float:
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    return _number(properties.Mass(), "computed volume", nonnegative=True)


def _shape_info(shape: TopoDS_Shape) -> dict[str, Any]:
    if shape.IsNull():
        return {"valid": False, "status": "null", "solid_count": 0, "volume": None}
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    count = 0
    while explorer.More():
        count += 1
        explorer.Next()
    valid = BRepCheck_Analyzer(shape).IsValid()
    return {
        "valid": valid and count > 0,
        "status": "valid" if valid and count else "invalid" if not valid else "not_solid",
        "solid_count": count,
        "volume": _volume(shape) if valid and count else None,
    }


def _moved(shape: TopoDS_Shape, location: TopLoc_Location | None, sample: dict[str, Any]) -> TopoDS_Shape:
    # Rz * Ry * Rx: fixed world XYZ rotations about an explicit pivot or the recorded leaf origin.
    rx, ry, rz = (math.radians(value % 360) for value in sample["rotation"])
    sx, sy, sz = math.sin(rx), math.sin(ry), math.sin(rz)
    cx, cy, cz = math.cos(rx), math.cos(ry), math.cos(rz)
    rotation = (
        (cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx),
        (sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx),
        (-sy, cy * sx, cy * cx),
    )
    origin = sample.get("origin")
    if origin is None:
        if location is None:
            raise MeasureError(f"{sample['path']} has no recorded placement; specify origin or rebuild with fresh=True")
        recorded = location.Transformation()
        origin = [recorded.Value(row, 4) for row in range(1, 4)]
    values = []
    for axis, row in enumerate(rotation):
        translation = origin[axis] + sample["translation"][axis] - sum(a * b for a, b in zip(row, origin, strict=True))
        values.extend((*row, _number(translation, "sample world transform")))
    transform = gp_Trsf()
    transform.SetValues(*values)
    return shape.Moved(TopLoc_Location(transform))


def _pair(
    paths: tuple[str, str],
    solids: dict[str, TopoDS_Shape],
    validity: dict[str, dict[str, Any]],
    clearance: float,
    tolerance: float,
    volume_tolerance: float,
) -> dict[str, Any]:
    first, second = paths
    result: dict[str, Any] = {
        "paths": list(paths),
        "status": "unavailable",
        "available": False,
        "validity": {path: validity[path] for path in paths},
        "overlap_volume": None,
        "minimum_distance": None,
        "witnesses": None,
        "assertions": {"no_interference": False, "clearance": False},
        "passed": False,
    }
    if not all(validity[path]["valid"] for path in paths):
        result["status"] = "invalid_shape"
        return result
    try:
        common = BRepAlgoAPI_Common(solids[first], solids[second])
        if not common.IsDone():
            raise MeasureError("exact solid intersection failed")
        overlap = _volume(common.Shape())
        extrema = BRepExtrema_DistShapeShape(solids[first], solids[second])
        if not extrema.IsDone() or extrema.NbSolution() < 1:
            raise MeasureError("exact minimum distance has no solution")
        distance = _number(extrema.Value(), "computed distance", nonnegative=True)
        points = (extrema.PointOnShape1(1), extrema.PointOnShape2(1))
        witnesses = [
            [_number(value, "computed witness") for value in (point.X(), point.Y(), point.Z())] for point in points
        ]
        no_interference = overlap <= volume_tolerance
        enough_clearance = no_interference and distance + tolerance >= clearance
        result.update(
            {
                "available": True,
                "status": "overlap" if not no_interference else "touching" if distance <= tolerance else "separated",
                "overlap_volume": overlap,
                "minimum_distance": distance,
                "witnesses": witnesses,
                "assertions": {"no_interference": no_interference, "clearance": enough_clearance},
                "passed": no_interference and enough_clearance,
            }
        )
    except Exception as error:
        result["error"] = str(error)
    return result


def check(
    shapes: Shapes,
    pairs: list[list[str]],
    *,
    clearance: float = 0.0,
    ignore: list[list[str]] | None = None,
    samples: list[dict[str, Any]] | None = None,
    tolerance: float = 1e-6,
    volume_tolerance: float = 1e-9,
    contact: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check explicit whole-leaf pairs at baseline and independent, optionally coordinated rigid poses.

    Clearance/tolerance are mm, overlap/volume_tolerance are mm^3. Touching passes a zero-clearance
    assertion if overlap is within volume_tolerance; a zero distance never reports penetration depth.
    Ignored pairs are explicit exclusions from this request, not a scene-wide collision filter.
    Sample origin optionally sets the rotation pivot in world mm; if omitted, the recorded leaf origin is used.
    Rotation uses fixed world XYZ axes, followed by world translation, independently of other samples.
    """
    requested = _pairs(pairs, "pairs", empty=False)
    ignored = _pairs([] if ignore is None else ignore, "ignore", empty=True)
    keys = {frozenset(pair) for pair in requested}
    ignored_keys = {frozenset(pair) for pair in ignored}
    if not ignored_keys <= keys:
        raise MeasureError("ignore must name only explicitly requested pairs")
    clearance = _number(clearance, "clearance", nonnegative=True)
    tolerance = _number(tolerance, "tolerance", nonnegative=True)
    volume_tolerance = _number(volume_tolerance, "volume_tolerance", nonnegative=True)
    contacts = _contacts([] if contact is None else contact, keys, ignored_keys)
    poses = _samples([] if samples is None else samples, {path for pair in requested for path in pair})
    active = [pair for pair in requested if frozenset(pair) not in ignored_keys]
    if len(active) * (len(poses) + 1) > MAX_EVALUATIONS:
        raise MeasureError(f"request exceeds {MAX_EVALUATIONS} pair/pose evaluations; reduce pairs or samples")
    if not shapes.whole_available:
        raise MeasureError("this older shape cache has no whole solids; rebuild with fresh=True before checking")
    for pair in requested:
        for path in pair:
            if path not in shapes.leaves:
                raise MeasureError(f"{path} is not a whole leaf instance in this build")
    paths = dict.fromkeys(path for pair in active for path in pair)
    solids = {path: shapes.solid(path) for path in paths}
    validity = {path: _shape_info(shape) for path, shape in solids.items()}

    def evaluate(
        pair: tuple[str, str], placed: dict[str, TopoDS_Shape], statuses: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        row = _pair(pair, placed, statuses, clearance, tolerance, volume_tolerance)
        allowed = contacts.get(frozenset(pair))
        if allowed is not None:
            row["intentional_contact"] = allowed
            row["assertions"]["intentional_contact"] = bool(
                row["available"]
                and row["minimum_distance"] <= allowed["max_distance"] + tolerance
                and row["overlap_volume"] <= allowed["max_overlap_volume"] + volume_tolerance
            )
            row["passed"] = row["assertions"]["intentional_contact"]
        return row

    baseline = [evaluate(pair, solids, validity) for pair in active]
    sampled = []
    for index, sample in enumerate(poses):
        placed = dict(solids)
        statuses = dict(validity)
        resolved = []
        for pose in sample["poses"]:
            path = pose["path"]
            if "origin" not in pose:
                location = shapes.placements.get(path)
                if location is None:
                    raise MeasureError(f"{path} has no recorded placement; specify origin or rebuild with fresh=True")
                transform = location.Transformation()
                pose = {**pose, "origin": [transform.Value(row, 4) for row in range(1, 4)]}
            resolved.append(pose)
            if path in placed:
                placed[path] = _moved(solids[path], shapes.placements.get(path), pose)
                statuses[path] = _shape_info(placed[path])
        moved = {pose["path"] for pose in resolved}
        results = [
            evaluate(pair, placed, statuses) if moved.intersection(pair) else baseline[at]
            for at, pair in enumerate(active)
        ]
        legacy = resolved[0] if "path" in sample else {}
        sampled.append(
            {
                **sample,
                **legacy,
                "index": index,
                "poses": resolved,
                "pairs": results,
                "passed": all(row["passed"] for row in results),
            }
        )
    return {
        "scope": {
            "pairs": "explicit_only",
            "motion": "independent_samples_not_continuous",
            "samples_accumulate": False,
            "rotation": "fixed_world_XYZ_about_explicit_or_recorded_origin",
            "default_origin": "recorded_leaf_world_origin",
            "translation": "world_axes_after_rotation",
            "penetration_depth": "not_computed; zero minimum distance does not measure penetration",
        },
        "units": {"distance": "mm", "translation": "mm", "origin": "world mm", "rotation": "degrees", "volume": "mm^3"},
        "clearance": clearance,
        "tolerance": tolerance,
        "volume_tolerance": volume_tolerance,
        "ignored": [list(pair) for pair in ignored],
        "validity": validity,
        "pairs": baseline,
        "samples": sampled,
        "failing_poses": [sample for sample in sampled if not sample["passed"]],
        "ok": all(row["passed"] for row in baseline) and all(row["passed"] for row in sampled),
    }
