"""Explicit, bounded mechanical checks on retained exact geometry; no continuous-motion claims."""

from __future__ import annotations

import math
from typing import Any

from OCP.Bnd import Bnd_Box

# OCP is a compiled extension without type stubs.
# pyright: reportAttributeAccessIssue=false
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBndLib import BRepBndLib
from OCP.gp import gp_Ax3, gp_Dir, gp_Pnt, gp_Trsf
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape

from .checks import MAX_SAMPLES, _number, _samples, _shape_info, _vector, _volume, check
from .measure import MeasureError, Shapes, _entity, _same_region, measure

MAX_ASSERTIONS = 128
MAX_CHECKS = 128


def _fields(request: dict[str, Any], allowed: set[str], required: set[str]) -> None:
    if not isinstance(request, dict) or set(request) - allowed or not required <= request.keys():
        raise MeasureError(
            f"request requires {', '.join(sorted(required))}; allowed fields: {', '.join(sorted(allowed))}"
        )


def _paths(value: Any, *, count: int | None = None) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_ASSERTIONS
        or any(not isinstance(path, str) or not path for path in value)
        or (count is not None and len(value) != count)
    ):
        raise MeasureError(
            f"paths must contain {'exactly ' + str(count) if count else '1 to ' + str(MAX_ASSERTIONS)} explicit paths"
        )
    if len(set(value)) != len(value):
        raise MeasureError("paths must not repeat")
    return value


def travel_samples(travel: dict[str, Any], pairs: list[list[str]]) -> list[dict[str, Any]]:
    """Linearly interpolate total world translation and fixed-XYZ angles, jointly, from the recorded pose."""
    _fields(travel, {"steps", "poses"}, {"steps", "poses"})
    steps = travel["steps"]
    if isinstance(steps, bool) or not isinstance(steps, int) or not 2 <= steps <= MAX_SAMPLES:
        raise MeasureError(f"travel steps must be an integer from 2 to {MAX_SAMPLES}")
    full = _samples([{"poses": travel["poses"]}], {path for pair in pairs for path in pair})[0]["poses"]
    return [
        {
            "name": f"travel {index + 1}/{steps}",
            "fraction": index / (steps - 1),
            "poses": [
                {
                    **pose,
                    "translation": [value * index / (steps - 1) for value in pose["translation"]],
                    "rotation": [value * index / (steps - 1) for value in pose["rotation"]],
                }
                for pose in full
            ],
        }
        for index in range(steps)
    ]


def _axial_bounds(
    shape: TopoDS_Shape, origin: tuple[float, float, float], direction: tuple[float, float, float]
) -> tuple[float, float]:
    transform = gp_Trsf()
    transform.SetTransformation(gp_Ax3(gp_Pnt(*origin), gp_Dir(*direction)))
    bounds = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape.Moved(TopLoc_Location(transform)), bounds, False, False)
    if bounds.IsVoid() or bounds.IsOpenZmin() or bounds.IsOpenZmax():
        raise MeasureError("engagement requires finite trimmed cylindrical faces")
    return _number(bounds.CornerMin().Z(), "axial lower bound"), _number(bounds.CornerMax().Z(), "axial upper bound")


def engagement(shapes: Shapes, request: dict[str, Any]) -> dict[str, Any]:
    """Cylinder-face engagement: exact trimmed axial extent, coaxiality and explicit radial-fit limits.

    The first face is the shaft/screw and the second the receiving bore. Thread form, preload, retention
    force and bearing life are not inferred from cylindrical engagement.
    """
    _fields(
        request,
        {
            "kind",
            "paths",
            "min_engagement",
            "max_axis_distance",
            "max_angle",
            "min_radial_clearance",
            "max_radial_clearance",
            "tolerance",
        },
        {"kind", "paths", "min_engagement"},
    )
    paths = _paths(request["paths"], count=2)
    minimum = _number(request["min_engagement"], "min_engagement", nonnegative=True)
    tolerance = _number(request.get("tolerance", 1e-6), "tolerance", nonnegative=True)
    max_axis = _number(request.get("max_axis_distance", tolerance), "max_axis_distance", nonnegative=True)
    max_angle = _number(request.get("max_angle", 1e-6), "max_angle", nonnegative=True)
    if max_angle > 1:
        raise MeasureError("engagement max_angle must be at most one degree; nonparallel interfaces are unsupported")
    min_radial = _number(request.get("min_radial_clearance", 0), "min_radial_clearance")
    max_radial = request.get("max_radial_clearance")
    if max_radial is not None:
        max_radial = _number(max_radial, "max_radial_clearance")
        if max_radial < min_radial:
            raise MeasureError("max_radial_clearance must be at least min_radial_clearance")
    first, second = (_entity(shapes, path) for path in paths)
    if first.kind != "cylinder" or second.kind != "cylinder" or first.axis is None or second.axis is None:
        raise MeasureError(
            "engagement requires two explicit trimmed cylindrical face paths; threads/other surfaces are unsupported"
        )
    origin, direction = first.axis.point, first.axis.direction
    delta = [b - a for a, b in zip(origin, second.axis.point, strict=True)]
    along = sum(a * b for a, b in zip(delta, direction, strict=True))
    axis_distance = math.sqrt(sum((value - along * axis) ** 2 for value, axis in zip(delta, direction, strict=True)))
    cosine = abs(sum(a * b for a, b in zip(direction, second.axis.direction, strict=True)))
    angle = math.degrees(math.acos(min(1, cosine)))
    first_bounds = _axial_bounds(first.shape, origin, direction)
    second_bounds = _axial_bounds(second.shape, origin, direction)
    low, high = max(first_bounds[0], second_bounds[0]), min(first_bounds[1], second_bounds[1])
    length = max(0, high - low)
    radii = [next(row["value"] for row in entity.properties if row["label"] == "Radius") for entity in (first, second)]
    radial = radii[1] - radii[0]
    assertions = {
        "axial_engagement": length + tolerance >= minimum,
        "axis_distance": axis_distance <= max_axis + tolerance,
        "axis_angle": angle <= max_angle,
        "radial_fit": radial + tolerance >= min_radial and (max_radial is None or radial <= max_radial + tolerance),
    }
    return {
        "kind": request["kind"],
        "paths": paths,
        "ok": all(assertions.values()),
        "assertions": assertions,
        "engagement": length,
        "axial_bounds": [first_bounds, second_bounds],
        "axis_distance": axis_distance,
        "axis_angle": angle,
        "radii": radii,
        "radial_clearance": radial,
        "witnesses": [
            [value + end * axis for value, axis in zip(origin, direction, strict=True)] for end in (low, max(low, high))
        ],
        "units": {"distance": "mm", "angle": "degrees"},
        "scope": (
            "trimmed cylinder axial projection and requested fit limits, "
            "not thread/press-fit strength or retention proof"
        ),
    }


def run_check(shapes: Shapes, request: dict[str, Any]) -> dict[str, Any]:
    kind = request.get("kind", "geometry")
    if kind in {"screw_engagement", "bearing_engagement"}:
        return engagement(shapes, request)
    if kind in {"insertion", "operating"}:
        allowed = {"kind", "pairs", "travel", "clearance", "ignore", "tolerance", "volume_tolerance", "contact"}
        _fields(request, allowed, {"kind", "pairs", "travel"})
        samples = travel_samples(request["travel"], request["pairs"])
        fractions = [sample.pop("fraction") for sample in samples]
        result = check(
            shapes, **{key: value for key, value in request.items() if key not in {"kind", "travel"}}, samples=samples
        )
        for sample, fraction in zip(result["samples"], fractions, strict=True):
            sample["fraction"] = fraction
        return {
            **result,
            "kind": kind,
            "travel_evidence": "discrete samples only; collisions between poses are not excluded",
        }
    if kind != "geometry":
        raise MeasureError(f"unsupported mechanical check kind {kind!r}")
    return check(shapes, **{key: value for key, value in request.items() if key != "kind"})


def _shape(shapes: Shapes, path: str) -> tuple[str, TopoDS_Shape, str]:
    if path in shapes.leaves:
        return "solid", shapes.solid(path), path
    topo, shape = shapes.entity(path)
    return topo, shape, path.rsplit("/", 2)[0]


def _preserved(before: Shapes, after: Shapes, request: dict[str, Any]) -> dict[str, Any]:
    _fields(request, {"kind", "paths", "frame", "volume_tolerance"}, {"kind", "paths"})
    paths = _paths(request["paths"])
    frame = request.get("frame", "world")
    if frame not in {"local", "world"}:
        raise MeasureError("preserved frame must be world or local")
    volume_tolerance = _number(request.get("volume_tolerance", 1e-9), "volume_tolerance", nonnegative=True)
    results = []
    for path in paths:
        topo, old, leaf = _shape(before, path)
        current_topo, new, _ = _shape(after, path)
        if topo != current_topo:
            raise MeasureError(f"interface topology changed at {path}")
        if frame == "local":
            if leaf not in before.placements or leaf not in after.placements:
                raise MeasureError(f"{leaf} has no recorded placement for local comparison")
            old = old.Moved(before.placements[leaf].Inverted())
            new = new.Moved(after.placements[leaf].Inverted())
        row: dict[str, Any] = {"path": path, "topology": topo, "passed": False}
        if topo == "solid":
            if not _shape_info(old)["valid"] or not _shape_info(new)["valid"]:
                raise MeasureError(f"preserved interface {path} is not valid solid geometry")
            differences = []
            for first, second in ((old, new), (new, old)):
                cut = BRepAlgoAPI_Cut(first, second)
                if not cut.IsDone():
                    raise MeasureError(f"exact interface difference failed at {path}")
                differences.append(0.0 if cut.Shape().IsNull() else _volume(cut.Shape()))
            row.update(difference_volumes=differences, passed=all(value <= volume_tolerance for value in differences))
        else:
            row["passed"] = _same_region(old, new, topo)
        results.append(row)
    return {
        "kind": "preserved",
        "frame": frame,
        "interfaces": results,
        "ok": all(row["passed"] for row in results),
        "basis": (
            "exact bidirectional solid differences or trimmed entity region intersection; "
            "never equal summary metrics"
        ),
    }


def _placement(before: Shapes, after: Shapes, request: dict[str, Any]) -> dict[str, Any]:
    _fields(request, {"kind", "path", "tolerance", "angle_tolerance"}, {"kind", "path"})
    path = request["path"]
    if not isinstance(path, str) or path not in before.placements or path not in after.placements:
        raise MeasureError("placement assertion requires a whole leaf with recorded placement in both builds")
    tolerance = _number(request.get("tolerance", 1e-6), "tolerance", nonnegative=True)
    angle_tolerance = _number(request.get("angle_tolerance", 1e-6), "angle_tolerance", nonnegative=True)
    old, new = before.placements[path].Transformation(), after.placements[path].Transformation()
    displacement = [new.Value(row, 4) - old.Value(row, 4) for row in range(1, 4)]
    cosine = (sum(old.Value(row, col) * new.Value(row, col) for row in range(1, 4) for col in range(1, 4)) - 1) / 2
    angle = math.degrees(math.acos(min(1, max(-1, cosine))))
    return {
        "kind": "placement",
        "path": path,
        "translation_delta": displacement,
        "angle_delta": angle,
        "ok": math.sqrt(sum(value * value for value in displacement)) <= tolerance and angle <= angle_tolerance,
        "before": [[old.Value(row, col) for col in range(1, 5)] for row in range(1, 4)],
        "after": [[new.Value(row, col) for col in range(1, 5)] for row in range(1, 4)],
    }


def _quantity(shapes: Shapes, paths: list[str], label: str) -> dict[str, Any]:
    measured = measure(shapes, paths)
    rows = measured["results"] if len(paths) > 1 else measured["entities"][0]["properties"]
    matches = [row for row in rows if row["label"] == label]
    if len(matches) != 1:
        raise MeasureError(f"measurement label {label!r} is unavailable or ambiguous for these paths")
    return matches[0]


def run_assertion(after: Shapes, request: dict[str, Any], before: Shapes | None = None) -> dict[str, Any]:
    kind = request.get("kind")
    if kind in {"preserved", "placement"}:
        if before is None:
            raise MeasureError(f"{kind} assertion requires an explicit pinned baseline with exact shapes")
        return _preserved(before, after, request) if kind == "preserved" else _placement(before, after, request)
    if kind != "measurement":
        raise MeasureError(f"unsupported interface assertion kind {kind!r}")
    _fields(request, {"kind", "paths", "label", "min", "max", "preserved", "tolerance"}, {"kind", "paths", "label"})
    paths = _paths(request["paths"])
    label = request["label"]
    if not isinstance(label, str) or not label:
        raise MeasureError("measurement label must be a nonempty exact quantity label")
    preserved = request.get("preserved", False)
    if not isinstance(preserved, bool):
        raise MeasureError("measurement preserved must be a boolean")
    if not preserved and not {"min", "max"}.intersection(request):
        raise MeasureError("measurement requires min, max or preserved=true")
    tolerance = _number(request.get("tolerance", 1e-6), "tolerance", nonnegative=True)
    quantity = _quantity(after, paths, label)
    value = quantity["value"]
    minimum = _number(request["min"], "min") if "min" in request else None
    maximum = _number(request["max"], "max") if "max" in request else None
    if minimum is not None and maximum is not None and minimum > maximum:
        raise MeasureError("measurement min must not exceed max")
    passed = True
    if minimum is not None or maximum is not None:
        scalar = _number(value, "bounded measurement value")
        passed = (minimum is None or scalar + tolerance >= minimum) and (
            maximum is None or scalar <= maximum + tolerance
        )
    result = {"kind": kind, "paths": paths, "quantity": quantity, "ok": passed}
    if preserved:
        if before is None:
            raise MeasureError("preserved measurement requires an explicit pinned baseline with exact shapes")
        previous = _quantity(before, paths, label)
        if previous["kind"] != quantity["kind"]:
            raise MeasureError("measurement quantity kind changed across builds")
        old_value = previous["value"]
        if isinstance(value, (list, tuple)):
            old_vector, new_vector = _vector(old_value, "baseline measurement"), _vector(value, "current measurement")
            delta = math.dist(old_vector, new_vector)
        else:
            delta = abs(_number(value, "current measurement") - _number(old_value, "baseline measurement"))
        result.update(before=previous, delta=delta, ok=passed and delta <= tolerance)
    return result
