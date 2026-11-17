"""Conservative, geometry-only 2D profile CAM for manual loading into CNCjs.

No stock layout, machine configuration, workholding or machine communication is inferred.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from build123d import Edge, Face, GeomType, Kind, Location, Solid, Wire
from build123d.topology import Shape

# OCP is a compiled extension without type stubs.
# pyright: reportAttributeAccessIssue=false
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepTools import BRepTools_WireExplorer
from OCP.GCPnts import GCPnts_UniformDeflection
from OCP.Standard import Standard_Failure
from OCP.TopExp import TopExp

_TOL = 1e-6
_CHORD_ERROR = 0.05
_OFFSET_ERROR = 0.025  # OCCT spline reverse-offset boundary band, in mm.
_MAX_PASSES = 10_000
_MAX_POINTS = 200_000
_MAX_BLOCKS = 250_000
_SETTINGS = {
    "tool_diameter", "cut_depth", "step_down", "safe_z", "feed_rate", "plunge_rate", "spindle_speed"
}


def _settings(machining: dict[str, Any]) -> dict[str, float | int]:
    if not isinstance(machining, dict):
        raise ValueError("machining must be an object containing explicit cutter, depths and feeds")
    missing = _SETTINGS - machining.keys()
    unknown = machining.keys() - _SETTINGS
    if missing or unknown:
        raise ValueError(
            f"machining requires exactly {', '.join(sorted(_SETTINGS))}; "
            f"missing: {', '.join(sorted(missing))}; unknown: {', '.join(sorted(map(str, unknown)))}"
        )
    result: dict[str, float | int] = {}
    for name in sorted(_SETTINGS):
        value = machining[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite positive number")
        try:
            number = float(value)
        except OverflowError as error:
            raise ValueError(f"{name} must be a finite positive number") from error
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{name} must be a finite positive number")
        if name == "spindle_speed":
            if not number.is_integer():
                raise ValueError("spindle_speed must be an integer RPM")
            result[name] = int(number)
        else:
            # GRBL accepts fixed decimal numbers, not exponent notation.
            if round(number, 9) == 0:
                raise ValueError(f"{name} is below the G-code output resolution (0.000000001)")
            result[name] = number
    return result


def _horizontal_faces(shape: Shape, z: float) -> list[Face]:
    """Find exact planar openings at a stock surface, not projected silhouettes."""
    result = []
    for face in shape.faces():
        if face.geom_type != GeomType.PLANE or abs(abs(face.normal_at().Z) - 1) > _TOL:
            continue
        bounds = face.bounding_box()
        if abs(bounds.min.Z - z) <= _TOL and abs(bounds.max.Z - z) <= _TOL:
            result.append(face)
    return result


def _bounds(shape: Shape) -> dict[str, list[float]]:
    box = shape.bounding_box()
    return {"min": [box.min.X, box.min.Y, box.min.Z], "max": [box.max.X, box.max.Y, box.max.Z]}


def _omitted_recess(cavity: Solid, bottom_z: float, top_z: float) -> dict[str, Any]:
    top = bool(_horizontal_faces(cavity, top_z))
    bottom = bool(_horizontal_faces(cavity, bottom_z))
    bounds = cavity.bounding_box()
    side = "both" if top and bottom else "top" if top else "bottom" if bottom else "internal"
    depth = top_z - bounds.min.Z if top else bounds.max.Z - bottom_z if bottom else bounds.size.Z
    # No cutter enters omitted recesses; even connected or undercut residuals
    # can be reported without pretending they are machinable blind pockets.
    return {
        "side": side,
        "depth_mm": depth,
        "bounds_mm": _bounds(cavity),
        "volume_mm3": cavity.volume,
    }


def _through_candidates(cavity: Solid, top_z: float) -> list[Face]:
    """Use exact horizontal boundary loops, including intermediate bore rims.

    Mouth intersections are not bottlenecks of countersunk cavities. A rim at
    any topology elevation is only a candidate; the full prism is proved below.
    """
    levels: list[tuple[float, list[Edge]]] = []
    for edge in cavity.edges():
        box = edge.bounding_box()
        if box.size.Z > _TOL:
            continue
        z = edge.position_at(0).Z
        for level, edges in levels:
            if abs(level - z) <= _TOL:
                edges.append(edge)
                break
        else:
            levels.append((z, [edge]))
    candidates = []
    for z, edges in levels:
        try:
            wires = Wire.combine(edges)
        except (ValueError, RuntimeError, Standard_Failure):
            # An ambiguous boundary gives no candidate, never permission to cut.
            continue
        for wire in wires:
            if not wire.is_closed:
                continue
            try:
                face = Face(wire).moved(Location((0, 0, top_z - z)))
                if face.is_valid:
                    candidates.append(face)
            except (ValueError, RuntimeError, Standard_Failure):
                continue
    return candidates


def _profile(shape: Shape) -> tuple[Face, float, float, list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(shape, Shape) or not shape.is_valid:
        raise ValueError("CNC export requires a valid exact BRep solid")
    solids = shape.solids()
    if len(solids) != 1:
        raise ValueError("CNC export supports exactly one solid; select one flat part at a time")
    solid = solids[0]
    if any(len(getattr(shape, name)()) != len(getattr(solid, name)()) for name in ("faces", "edges", "vertices")):
        raise ValueError("CNC export does not support loose faces, edges or mixed geometry")
    box = solid.bounding_box()
    top_z = box.max.Z
    thickness = top_z - box.min.Z
    if thickness <= _TOL:
        raise ValueError("CNC export requires a positive constant-thickness solid")
    top_faces = _horizontal_faces(solid, top_z)
    bottom_faces = _horizontal_faces(solid, box.min.Z)
    if len(top_faces) != 1 or len(bottom_faces) != 1:
        raise ValueError("CNC export requires one horizontal planar top and bottom; slopes are unsupported")
    # Curved pocket faces can expand the OCCT bounding box by topology tolerances.
    # Stock height and work zero come from the actual planar caps, not that box.
    top_z = top_faces[0].center().Z
    bottom_z = bottom_faces[0].center().Z
    thickness = top_z - bottom_z
    # Ignore pocket contours when constructing stock, but never project an outer
    # step or taper into a profile: both exact outer envelopes must coincide.
    top_outer = Face(top_faces[0].outer_wire())
    bottom_outer = Face(bottom_faces[0].outer_wire()).moved(Location((0, 0, thickness)))
    area_tolerance = max(_TOL ** 2, top_outer.outer_wire().length * _TOL)
    if (top_outer - bottom_outer).area + (bottom_outer - top_outer).area > area_tolerance:
        raise ValueError("Outer envelopes must match: slopes, taper and outer steps are unsupported")
    stock = Solid.extrude(top_outer, (0, 0, -thickness))
    volume_tolerance = max(_TOL ** 3, top_outer.area * _TOL)
    if (solid - stock).volume > volume_tolerance:
        raise ValueError("Outer slopes or undercuts extend beyond the constant-thickness stock envelope")
    missing = stock - solid
    holes: list[Wire] = []
    skipped: list[dict[str, Any]] = []
    skipped_holes: list[dict[str, Any]] = []
    for cavity in missing.solids():
        top_openings = _horizontal_faces(cavity, top_z)
        bottom_openings = _horizontal_faces(cavity, bottom_z)
        if not top_openings or not bottom_openings:
            skipped.append(_omitted_recess(cavity, bottom_z, top_z))
            continue
        through: list[Solid] = []
        for opening in _through_candidates(cavity, top_z):
            if opening.area <= area_tolerance:
                continue
            prism = Solid.extrude(opening, (0, 0, -thickness))
            if (prism - cavity).volume > volume_tolerance:
                continue
            # Avoid nested/duplicate rims. Their union still has exact BRep
            # boundaries and every constituent full-height prism was proved void.
            through.append(prism)
        if not through:
            skipped_holes.append({
                "reason": "No proven safe constant vertical through profile; cavity has obstructions or varying walls",
                "bounds_mm": _bounds(cavity),
            })
            continue
        proven = through[0].fuse(*through[1:]) if len(through) > 1 else through[0]
        for opening in _horizontal_faces(proven, top_z):
            if opening.inner_wires():
                bounds = _bounds(opening)
                bounds["min"][2] = bottom_z
                skipped_holes.append({
                    "reason": "Proven opening contains retained internal islands; no safe single interior profile",
                    "bounds_mm": bounds,
                })
                continue
            holes.append(opening.outer_wire())
        remainder = cavity - proven
        for recess in remainder.solids():
            if recess.volume > volume_tolerance:
                skipped.append(_omitted_recess(recess, bottom_z, top_z))
    profile = Face(top_outer.outer_wire(), holes)
    skipped.sort(key=lambda pocket: (pocket["side"], *pocket["bounds_mm"]["min"]))
    return profile.moved(Location((0, 0, -top_z))), thickness, top_z, skipped, skipped_holes


def _wire(value: Edge | Wire) -> Wire:
    return value if isinstance(value, Wire) else Wire([value])


def _offset(wire: Wire, radius: float, *, inside: bool) -> Wire:
    """Choose offset sign by exact containment, independent of BRep orientation."""
    original = Face(wire)
    area_tolerance = max(_TOL ** 2, wire.length * _TOL)
    curved = any(edge.geom_type not in (GeomType.LINE, GeomType.CIRCLE) for edge in wire.edges())
    # Admit spline approximations within the same budget as the recovery proof.
    distance_tolerance = _OFFSET_ERROR if curved else _TOL * 10
    candidates: list[tuple[Wire, float]] = []
    for distance in (radius, -radius):
        try:
            candidate = _wire(wire.offset_2d(distance, kind=Kind.ARC))
            face = Face(candidate)
            if not candidate.is_closed or not face.is_valid or face.area <= area_tolerance:
                continue
            if abs(wire.distance_to(candidate) - radius) > distance_tolerance:
                continue
            if inside:
                correct_side = face.area < original.area and (face - original).area <= area_tolerance
            else:
                correct_side = face.area > original.area and (original - face).area <= area_tolerance
            if correct_side:
                candidates.append((candidate, distance))
        except (ValueError, RuntimeError, Standard_Failure):
            # A collapsed or split result is not a usable tool-center contour.
            continue
    if len(candidates) != 1:
        raise ValueError("Cutter offset collapsed, split or is impossible: hole/feature too narrow for cutter")
    candidate, distance = candidates[0]
    try:
        recovered = Face(_wire(candidate.offset_2d(-distance, kind=Kind.ARC)))
    except (ValueError, RuntimeError, Standard_Failure) as error:
        raise ValueError("Cutter cannot reproduce this profile: narrow or complex geometry") from error
    if curved:
        # Bound approximation geometrically, not by a global area allowance
        # that could hide a single gouged corner.
        bands = sorted(
            (Face(_wire(wire.offset_2d(delta, kind=Kind.ARC))) for delta in (-_OFFSET_ERROR, _OFFSET_ERROR)),
            key=lambda face: face.area,
        )
        # Near-coincident spline face booleans can misclassify an entire
        # interior or lose a real corner defect. Always prove containment
        # with unit-height prisms: their volume equals the planar area.
        recovered_prism = Solid.extrude(recovered, (0, 0, 1))
        inner_prism = Solid.extrude(bands[0], (0, 0, 1))
        outer_prism = Solid.extrude(bands[1], (0, 0, 1))
        difference = ((recovered_prism - outer_prism).volume
                      + (inner_prism - recovered_prism).volume)
    else:
        difference = (recovered - original).area + (original - recovered).area
    if difference > area_tolerance:
        raise ValueError(
            "Cutter cannot reproduce this profile: feature too narrow or internal corner sharper than cutter radius"
        )
    return candidate


def _points(wire: Wire) -> list[tuple[float, float]]:
    """Traverse shared CAD vertices; reserve endpoint error inside the chord budget."""
    result: list[tuple[float, float]] = []
    explorer = BRepTools_WireExplorer(wire.wrapped)
    visited = 0
    while explorer.More():
        edge = explorer.Current()
        first_vertex = explorer.CurrentVertex()
        last_vertex = TopExp.LastVertex_s(edge, True)
        start, end = BRep_Tool.Pnt_s(first_vertex), BRep_Tool.Pnt_s(last_vertex)
        curve = BRepAdaptor_Curve(edge)
        first, last = curve.Value(curve.FirstParameter()), curve.Value(curve.LastParameter())
        reverse = last.Distance(start) < first.Distance(start)
        if reverse:
            first, last = last, first
        start_error, end_error = first.Distance(start), last.Distance(end)
        if (start_error > BRep_Tool.Tolerance_s(first_vertex) + _TOL
                or end_error > BRep_Tool.Tolerance_s(last_vertex) + _TOL):
            raise ValueError("Curve endpoints exceed their CAD vertex tolerances")
        # Adjacent curves may end at different points within a shared vertex's
        # tolerance. Weld to that actual vertex, not an arbitrary nearby sample.
        deflection = _CHORD_ERROR / 2 - max(start_error, end_error) - _TOL
        if deflection <= 0:
            raise ValueError("CAD vertex uncertainty exceeds the chordal error budget")
        samples = GCPnts_UniformDeflection(curve, deflection, True)
        if not samples.IsDone() or samples.NbPoints() < 2:
            raise ValueError("OCCT could not bound the curve's chordal deflection")
        if len(result) + samples.NbPoints() > _MAX_POINTS:
            raise ValueError("Profile requires too many points for a safe CNC export")
        points = [
            (point.X(), point.Y()) for point in (samples.Value(i) for i in range(1, samples.NbPoints() + 1))
        ]
        if reverse:
            points.reverse()
        points[0], points[-1] = (start.X(), start.Y()), (end.X(), end.Y())
        if result:
            if math.dist(result[-1], points[0]) > _TOL:
                raise ValueError("Offset contour vertices are not connected")
            points = points[1:]
        result.extend(points)
        visited += 1
        explorer.Next()
    if visited != len(wire.edges()):
        raise ValueError("Cutter wire traversal did not cover every edge")
    if len(result) < 4 or math.dist(result[0], result[-1]) > _TOL:
        raise ValueError("Cutter contour is not a closed profile")
    result[-1] = result[0]
    return result


def _fmt(value: float | int) -> str:
    if not math.isfinite(value):
        raise ValueError("Nonfinite coordinate in CNC profile")
    result = f"{value:.9f}".rstrip("0").rstrip(".")
    return "0" if result == "-0" else result


def _write_gcode(shape: Shape, path: Path, machining: dict[str, Any]) -> dict[str, Any]:
    """Write validated GRBL-compatible millimetre profile G-code and return setup evidence.

    Work XY=0 is the part's bounds center; the single solid's top becomes work Z=0. The caller is
    responsible for publishing the file only after this function succeeds.
    """
    settings = _settings(machining)
    profile, thickness, model_top, skipped_pockets, skipped_holes = _profile(shape)
    center = profile.bounding_box().center()
    depth = float(settings["cut_depth"])
    step = float(settings["step_down"])
    if depth < thickness:
        raise ValueError(f"cut_depth must reach the full solid thickness ({thickness:g} mm)")
    ratio = depth / step
    if not math.isfinite(ratio) or ratio > _MAX_PASSES:
        raise ValueError(f"cut_depth/step_down requires more than {_MAX_PASSES} passes")
    pass_count = math.ceil(ratio)
    radius = float(settings["tool_diameter"]) / 2
    # The outer contour is mandatory. Interior failures are omissions, never
    # enlarged holes or a reason to silently emit an empty export.
    outer = _offset(profile.outer_wire(), radius, inside=False)
    outer_points = _points(outer)

    def checked_polyline(points: list[tuple[float, float]]) -> Wire:
        polyline = Wire.make_polygon([(x, y, 0) for x, y in points[:-1]])
        if not Face(polyline).is_valid:
            raise ValueError("Unsafe toolpath: chordalized contour self-intersects")
        if polyline.distance_to(profile) < radius - _CHORD_ERROR - _TOL:
            raise ValueError("Unsafe toolpath: cutter intersects retained material beyond chord tolerance")
        return polyline

    contours = [outer]
    polylines = [checked_polyline(outer_points)]
    paths = []
    for wire in profile.inner_wires():
        try:
            contour = _offset(wire, radius, inside=True)
            points = _points(contour)
            polyline = checked_polyline(points)
            for other, other_polyline in zip(contours, polylines, strict=True):
                if (contour.distance_to(other) <= 2 * radius + 2 * _CHORD_ERROR
                        or polyline.distance_to(other_polyline) <= 2 * radius + _TOL):
                    raise ValueError(
                        "Unsafe toolpath clearance: cutter sweeps intersect or leave insufficient clearance"
                    )
        except (ValueError, RuntimeError, Standard_Failure) as error:
            bounds = _bounds(wire)
            bounds["min"][2] = model_top - thickness
            bounds["max"][2] = model_top
            box = wire.bounding_box()
            reason = f"{error}; tool diameter {2 * radius:g} mm, opening bounds {box.size.X:g} x {box.size.Y:g} mm"
            skipped_holes.append({"reason": reason, "bounds_mm": bounds})
            continue
        contours.append(contour)
        polylines.append(polyline)
        paths.append(("hole", points))
    # Finish every supported hole before the external profile releases the part.
    paths.append(("outer", outer_points))
    skipped_holes.sort(key=lambda hole: hole["bounds_mm"]["min"])
    partial = bool(skipped_pockets or skipped_holes)
    if pass_count * sum(len(points) + 5 for _, points in paths) > _MAX_BLOCKS:
        raise ValueError("Profile requires too many G-code blocks; increase step_down or simplify the part")
    safe = _fmt(settings["safe_z"])
    plunge = _fmt(settings["plunge_rate"])
    feed = _fmt(settings["feed_rate"])
    lines = [
        "(CADISTE 2D PROFILE - MANUAL CNCJS LOAD ONLY)",
        "(SET G54 WORK ZERO: XY PART BOUNDS CENTER; Z0 STOCK TOP; MM)",
        f"(MODEL XY CENTER: X{_fmt(center.X)} Y{_fmt(center.Y)})",
        "(INSPECT AND SIMULATE; CHECK WORKHOLDING SPINDLE AND MACHINE LIMITS)",
        "(NO TABS: SECURE PART AND HOLE SLUGS BEFORE THROUGH CUTTING)",
        "(CHECK SAFE Z CLEARS STOCK CLAMPS AND ALL RAPID TRAVEL)",
        "(USE A CENTER-CUTTING TOOL; VERTICAL PLUNGES; NO PECK RAMP OR LEAD-IN)",
        "G21 G90 G17 G94 G54",
        "G40 G49",
        "M5",
        f"G0 Z{safe}",
        f"M3 S{settings['spindle_speed']}",
    ]
    if partial:
        lines[1:1] = [
            "(INCOMPLETE PART - PROFILE ONLY MACHINING)",
            f"(POCKETS OMITTED: {len(skipped_pockets)} RECESSES LEFT UNMACHINED)",
            f"(HOLES OMITTED: {len(skipped_holes)} LEFT UNMACHINED)",
        ]
    depths = [-min(index * step, depth) for index in range(1, pass_count + 1)]
    depths[-1] = -depth
    if len(depths) > 1 and depths[-2] == depths[-1]:
        depths.pop()
        pass_count = len(depths)
    for index, (kind, points) in enumerate(paths, 1):
        lines.append(f"(CONTOUR {index} {kind.upper()})")
        # Every pass closes at this same XY, so only a new contour needs rapid positioning.
        lines.append(f"G0 X{_fmt(points[0][0] - center.X)} Y{_fmt(points[0][1] - center.Y)}")
        for pass_index, z in enumerate(depths, 1):
            lines.extend([
                f"(PASS {pass_index})",
                f"G1 Z{_fmt(z)} F{plunge}",
            ])
            lines.extend(f"G1 X{_fmt(x - center.X)} Y{_fmt(y - center.Y)} F{feed}" for x, y in points[1:])
        lines.append(f"G0 Z{safe}")
    lines.extend(["M5", "M2"])
    if any(len(line) > 79 for line in lines):
        raise ValueError("Machining settings or coordinates exceed conservative GRBL line length")
    warnings = [
        "Inspect and simulate the file; verify G54 zero, spindle direction/speed, feeds and machine limits manually.",
        "Safe Z is user supplied: verify clearance above all stock, fixtures and clamps.",
        "No tabs or holding strategy: secure the part and all hole slugs against release.",
        "Vertical plunges require a suitable center-cutting tool; "
        "no peck, ramp, lead-in or spindle dwell is generated.",
        "No stock dimensions are known: check cutter travel and sacrificial spoilboard clearance manually.",
        "Chordal deflection is at most 0.025 mm; spline offset recovery allows 0.025 mm.",
        "Emitted cutter paths are checked against retained material with a total 0.05 mm tolerance.",
    ]
    if partial:
        warnings.insert(
            0,
            f"INCOMPLETE PART - PROFILE ONLY: {len(skipped_pockets)} pockets omitted and "
            f"{len(skipped_holes)} holes omitted, left unmachined; "
            "only the outer profile and proven through holes are exported.",
        )
    if depth > thickness + _TOL:
        warnings.append("Cut depth exceeds model thickness: verify spoilboard and fixture clearance below the stock.")
    report = {
        "settings": settings,
        "contour_count": len(paths),
        "hole_count": len(paths) - 1,
        "pass_count": pass_count,
        "depths_mm": depths,
        "thickness_mm": thickness,
        "model_top_z_mm": model_top,
        "model_center_xy_mm": [center.X, center.Y],
        "partial_export": partial,
        "skipped_pocket_count": len(skipped_pockets),
        "skipped_pockets": skipped_pockets,
        "skipped_hole_count": len(skipped_holes),
        "skipped_holes": skipped_holes,
        "coordinate_convention": "G54 millimetres; XY=0 at part bounds center; stock top Z=0; cuts at negative Z",
        "compensation": "Exact BRep offsets: outer outward and holes inward by tool radius; G40",
        "chord_error_mm": _CHORD_ERROR,
        "offset_recovery_tolerance_mm": _OFFSET_ERROR,
        "point_count": sum(len(points) for _, points in paths),
        "warnings": warnings,
        "limitations": [
            "One horizontal stock envelope; exact BRep offsets with bounded OCCT curve linearization.",
            "Pockets/recesses are omitted, not machined; no outer steps/tapers, nesting, tabs or tool changes.",
            "Unmachinable holes are omitted; unsupported outer profiles reject the export.",
            "No machine, stock, workholding, tool-load or spindle-acceleration validation.",
        ],
    }
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii")
    return report


def write_gcode(shape: Shape, path: Path, machining: dict[str, Any]) -> dict[str, Any]:
    """Generate exact-profile G-code, converting kernel failures to actionable input errors."""
    try:
        return _write_gcode(shape, path, machining)
    except (Standard_Failure, RuntimeError, ArithmeticError) as error:
        raise ValueError(f"Exact profile geometry could not be machined safely: {error}") from error
