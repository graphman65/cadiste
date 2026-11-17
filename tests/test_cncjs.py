from __future__ import annotations

import math
from itertools import pairwise
from pathlib import Path

import pytest
from build123d import Align, Axis, Box, Compound, Cone, Cylinder, Edge, Face, Location, Solid, Sphere, Wire

from cadiste.cncjs import write_gcode

SETTINGS = {
    "tool_diameter": 2,
    "cut_depth": 3,
    "step_down": 1.25,
    "safe_z": 7,
    "feed_rate": 600,
    "plunge_rate": 90,
    "spindle_speed": 12000,
}


def _plate():
    return Box(40, 30, 3, align=(Align.MIN, Align.MIN, Align.MIN))


def _drilled_plate():
    hole = Cylinder(5, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 0)))
    return _plate() - hole


def _export(tmp_path, shape=None, **settings):
    path = tmp_path / "profile.nc"
    report = write_gcode(_plate() if shape is None else shape, path, SETTINGS | settings)
    return path.read_text(), report


def _commands(text):
    return [line for line in text.splitlines() if line and not line.startswith("(")]


def _words(line):
    return {word[0]: float(word[1:]) for word in line.split() if word[0] in "XYZFS"}


def _passes(text):
    """Read the actual G-code without relying on implementation internals."""
    paths = []
    current = None
    xy = None
    for line in _commands(text):
        words = _words(line)
        if line.startswith("G0") and "X" in words:
            xy = (words["X"], words["Y"])
        elif line.startswith("G1") and "Z" in words:
            current = {"depth": words["Z"], "plunge_feed": words["F"], "points": [xy], "feeds": []}
            paths.append(current)
        elif line.startswith("G1") and "X" in words:
            assert current is not None
            xy = (words["X"], words["Y"])
            current["points"].append(xy)
            current["feeds"].append(words["F"])
    return paths


def _assert_outer_path(path):
    points = path["points"]
    assert min(x for x, _ in points) == pytest.approx(-21)
    assert max(x for x, _ in points) == pytest.approx(21)
    assert min(y for _, y in points) == pytest.approx(-16)
    assert max(y for _, y in points) == pytest.approx(16)


def _assert_pocket(pocket, side, depth, minimum, maximum, volume):
    assert pocket["side"] == side
    assert pocket["depth_mm"] == pytest.approx(depth)
    assert pocket["bounds_mm"]["min"] == pytest.approx(minimum)
    assert pocket["bounds_mm"]["max"] == pytest.approx(maximum)
    assert pocket["volume_mm3"] == pytest.approx(volume)


def test_exact_outer_and_hole_compensation_holes_finish_before_release(tmp_path: Path):
    text, report = _export(tmp_path, _drilled_plate(), step_down=3)
    paths = _passes(text)
    assert len(paths) == report["contour_count"] == 2
    hole, outer = paths
    assert all(math.hypot(x, y) == pytest.approx(4, abs=1e-8) for x, y in hole["points"])
    _assert_outer_path(outer)
    assert report["hole_count"] == 1
    assert text.index("HOLE") < text.index("OUTER")
    assert "G40 G49" in text


def test_curved_compensated_paths_are_closed_with_bounded_chord_error(tmp_path: Path):
    shape = Cylinder(10, 3, align=(Align.CENTER, Align.CENTER, Align.MIN))
    text, report = _export(tmp_path, shape, step_down=3)
    points = _passes(text)[0]["points"]
    assert len(points) > 20
    assert points[-1] == points[0]
    for first, second in pairwise(points):
        assert math.hypot(*first) == pytest.approx(11, abs=1e-8)
        midpoint = tuple((a + b) / 2 for a, b in zip(first, second, strict=True))
        assert 0 <= 11 - math.hypot(*midpoint) <= report["chord_error_mm"] + 1e-8
    assert report["chord_error_mm"] <= 0.05


def test_all_contours_use_multiple_bounded_depths_and_explicit_feeds(tmp_path: Path):
    text, report = _export(tmp_path, _drilled_plate())
    paths = _passes(text)
    assert [path["depth"] for path in paths] == [-1.25, -2.5, -3, -1.25, -2.5, -3]
    assert report["pass_count"] == 3
    assert report["depths_mm"] == [-1.25, -2.5, -3]
    for path in paths:
        assert path["points"][0] == path["points"][-1]
        assert path["plunge_feed"] == 90
        assert set(path["feeds"]) == {600}
    assert "M3 S12000" in text
    assert report["settings"] == SETTINGS


def test_retract_precedes_first_xy_and_every_rapid_travel_and_finish(tmp_path: Path):
    text, _ = _export(tmp_path, _drilled_plate())
    commands = _commands(text)
    assert commands[:5] == ["G21 G90 G17 G94 G54", "G40 G49", "M5", "G0 Z7", "M3 S12000"]
    z = None
    for command in commands:
        words = _words(command)
        if command.startswith("G0") and ("X" in words or "Y" in words):
            assert z == 7
        if "Z" in words:
            z = words["Z"]
    assert commands[-3:] == ["G0 Z7", "M5", "M2"]
    assert z == 7
    assert not any(code in text for code in ("G28", "G30", "G92", "G10"))
    assert "SIMULATE" in text and "WORKHOLDING" in text


@pytest.mark.parametrize("cut_depth,step_down,expected_depths", [
    (10, 1, [-1, -2, -3, -4, -5, -6, -7, -8, -9, -10]),
    (10.2, 3, [-3, -6, -9, -10.2]),
    (10, 10, [-10]),
])
def test_closed_contours_step_down_in_place_and_clear_only_between_contours(
    tmp_path: Path, cut_depth, step_down, expected_depths
):
    stock = Box(80, 40, 10, align=(Align.MIN, Align.MIN, Align.MIN))
    bore = Cylinder(1.7, 10, align=(Align.CENTER, Align.CENTER, Align.MIN))
    part = stock - bore.moved(Location((25, 20, 0))) - bore.moved(Location((55, 20, 0)))
    text, report = _export(
        tmp_path, part, tool_diameter=3, cut_depth=cut_depth, step_down=step_down,
        safe_z=5, plunge_rate=100,
    )
    assert report["hole_count"] == 2
    xy = z = start = None
    contour_depths = []
    clearance_moves = 0
    plunge_travel = 0
    for command in _commands(text):
        words = _words(command)
        if command.startswith("G0 X"):
            assert z == 5
            xy = start = (words["X"], words["Y"])
            contour_depths.append([])
        elif command.startswith("G1 Z"):
            assert xy == start
            previous_depths = contour_depths[-1]
            assert z == (previous_depths[-1] if previous_depths else 5)
            if previous_depths:
                assert 0 < z - words["Z"] <= step_down + 1e-9
            assert words["F"] == 100
            plunge_travel += z - words["Z"]
            z = words["Z"]
            previous_depths.append(z)
        elif command.startswith("G1 X"):
            assert z == contour_depths[-1][-1]
            xy = (words["X"], words["Y"])
        elif command.startswith("G0 Z"):
            if z is not None:
                assert z == -cut_depth
                assert xy == start
            assert words["Z"] == 5
            z = words["Z"]
            clearance_moves += 1
    assert contour_depths == [expected_depths] * 3  # Two holes, then the outer profile.
    assert clearance_moves == 4  # Initial clearance and one retract per contour.
    assert plunge_travel == pytest.approx(3 * (5 + cut_depth))


def test_xy_bounds_center_and_model_top_map_to_work_zero_without_mutation(tmp_path: Path):
    hole = Cylinder(3, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((10, 8, 0)))
    shape = (_plate() - hole).moved(Location((100, -50, 27)))
    before = shape.location
    text, report = _export(tmp_path, shape, step_down=3)
    hole_path, outer = _passes(text)
    _assert_outer_path(outer)
    assert all(math.hypot(x + 10, y + 7) == pytest.approx(2, abs=1e-8) for x, y in hole_path["points"])
    assert report["model_center_xy_mm"] == pytest.approx([120, -35])
    assert report["model_top_z_mm"] == pytest.approx(30)
    assert report["thickness_mm"] == pytest.approx(3)
    assert _passes(text)[0]["depth"] == -3
    assert shape.location == before
    maximum_z = shape.bounding_box().max.Z
    assert maximum_z == pytest.approx(30)


def test_last_pass_exact_cut_depth_and_warns_about_spoilboard(tmp_path: Path):
    text, report = _export(tmp_path, cut_depth=3.2, step_down=1)
    assert [path["depth"] for path in _passes(text)] == [-1, -2, -3, -3.2]
    assert report["depths_mm"][-1] == -3.2
    assert any("exceeds model thickness" in warning for warning in report["warnings"])
    assert any("No tabs" in warning for warning in report["warnings"])


@pytest.mark.parametrize("name", list(SETTINGS))
@pytest.mark.parametrize("value", [True, False, 0, -1, float("nan"), float("inf"), -float("inf"), "2", None, 10**400])
def test_invalid_explicit_settings_rejected_before_output(tmp_path: Path, name, value):
    output = tmp_path / "invalid.nc"
    with pytest.raises(ValueError, match="finite positive"):
        write_gcode(_plate(), output, SETTINGS | {name: value})
    assert not output.exists()


@pytest.mark.parametrize("name", list(SETTINGS))
def test_every_machining_option_is_required(tmp_path: Path, name):
    settings = {key: value for key, value in SETTINGS.items() if key != name}
    with pytest.raises(ValueError, match="requires exactly"):
        write_gcode(_plate(), tmp_path / "invalid.nc", settings)


@pytest.mark.parametrize("settings", [SETTINGS | {"typo": 1}, {}, None, [], SETTINGS | {"spindle_speed": 12000.5}])
def test_unknown_or_noninteger_settings_never_silently_default(tmp_path: Path, settings):
    with pytest.raises(ValueError):
        write_gcode(_plate(), tmp_path / "invalid.nc", settings)


def test_insufficient_depth_and_excessive_pass_count_are_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="full solid thickness"):
        _export(tmp_path, cut_depth=2.9)
    with pytest.raises(ValueError, match="passes"):
        _export(tmp_path, step_down=0.00001)


def test_excessive_combined_path_and_pass_count_is_rejected(tmp_path: Path):
    shape = Cylinder(100, 3, align=(Align.CENTER, Align.CENTER, Align.MIN))
    with pytest.raises(ValueError, match="too many G-code blocks"):
        _export(tmp_path, shape, step_down=0.0003)
    assert not (tmp_path / "profile.nc").exists()


@pytest.mark.parametrize("radius", [0.25, 0.5, 1])
def test_holes_no_larger_than_cutter_are_explicitly_omitted(tmp_path: Path, radius):
    hole = Cylinder(radius, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 0)))
    text, report = _export(tmp_path, _plate() - hole, step_down=3)
    _assert_outer_path(_passes(text)[0])
    assert report["hole_count"] == 0
    assert report["partial_export"] is True
    assert report["skipped_hole_count"] == len(report["skipped_holes"]) == 1
    skipped = report["skipped_holes"][0]
    assert skipped["bounds_mm"]["min"] == pytest.approx([20 - radius, 15 - radius, 0])
    assert skipped["bounds_mm"]["max"] == pytest.approx([20 + radius, 15 + radius, 3])
    assert "2 mm" in skipped["reason"]
    assert "HOLES OMITTED: 1" in text


def test_sharp_internal_hole_corners_are_explicitly_omitted(tmp_path: Path):
    hole = Box(8, 8, 3, align=(Align.MIN, Align.MIN, Align.MIN)).moved(Location((15, 10, 0)))
    text, report = _export(tmp_path, _plate() - hole, step_down=3)
    assert len(_passes(text)) == report["contour_count"] == 1
    assert report["skipped_hole_count"] == 1
    assert report["skipped_holes"][0]["reason"]


def test_narrow_concave_outer_slot_is_rejected(tmp_path: Path):
    face = Face(Wire.make_polygon([
        (0, 0, 0), (20, 0, 0), (20, 20, 0), (10.5, 20, 0),
        (10.5, 5, 0), (9.5, 5, 0), (9.5, 20, 0), (0, 20, 0),
    ]))
    with pytest.raises(ValueError, match=r"narrow|corner|collapsed"):
        _export(tmp_path, Solid.extrude(face, (0, 0, 3)))


@pytest.mark.parametrize("shape", [_plate(), _drilled_plate()])
def test_no_pockets_preserves_complete_export_contract(tmp_path: Path, shape):
    _, report = _export(tmp_path, shape)
    assert report["partial_export"] is False
    assert report["skipped_pocket_count"] == 0
    assert report["skipped_pockets"] == []
    assert report["skipped_hole_count"] == 0
    assert report["skipped_holes"] == []


def test_top_pocket_omitted_without_losing_independent_through_hole(tmp_path: Path):
    pocket = Cylinder(3, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((8, 8, 2)))
    text, report = _export(tmp_path, _drilled_plate() - pocket, step_down=3)
    hole, outer = _passes(text)
    assert all(math.hypot(x, y) == pytest.approx(4, abs=1e-8) for x, y in hole["points"])
    _assert_outer_path(outer)
    assert report["hole_count"] == 1
    assert report["contour_count"] == 2
    assert report["partial_export"] is True
    assert report["skipped_pocket_count"] == len(report["skipped_pockets"]) == 1
    _assert_pocket(report["skipped_pockets"][0], "top", 1, [5, 5, 2], [11, 11, 3], 9 * math.pi)
    assert "INCOMPLETE PART - PROFILE ONLY" in text
    assert "POCKETS OMITTED: 1" in text
    assert any("pockets omitted" in warning and "INCOMPLETE" in warning for warning in report["warnings"])


def test_bottom_pocket_omitted_without_through_cut(tmp_path: Path):
    pocket = Cylinder(3, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((8, 8, 0)))
    text, report = _export(tmp_path, _plate() - pocket, step_down=3)
    paths = _passes(text)
    assert len(paths) == report["contour_count"] == 1
    assert report["hole_count"] == 0
    _assert_outer_path(paths[0])
    assert report["partial_export"] is True
    assert report["skipped_pocket_count"] == 1
    _assert_pocket(report["skipped_pockets"][0], "bottom", 1, [5, 5, 0], [11, 11, 1], 9 * math.pi)


def test_opposing_disconnected_pockets_with_xy_overlap_are_not_through_holes(tmp_path: Path):
    top = Cylinder(3, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((8, 8, 2)))
    bottom = Cylinder(3, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((9, 8, 0)))
    text, report = _export(tmp_path, _plate() - top - bottom, step_down=3)
    paths = _passes(text)
    assert len(paths) == report["contour_count"] == 1
    assert report["hole_count"] == 0
    _assert_outer_path(paths[0])
    assert report["partial_export"] is True
    assert report["skipped_pocket_count"] == 2
    _assert_pocket(report["skipped_pockets"][0], "bottom", 1, [6, 5, 0], [12, 11, 1], 9 * math.pi)
    _assert_pocket(report["skipped_pockets"][1], "top", 1, [5, 5, 2], [11, 11, 3], 9 * math.pi)


def test_shifted_model_keeps_pocket_evidence_in_world_coordinates(tmp_path: Path):
    pocket = Cylinder(3, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((8, 8, 2)))
    shape = (_plate() - pocket).moved(Location((100, -50, 27)))
    text, report = _export(tmp_path, shape, step_down=3)
    paths = _passes(text)
    assert len(paths) == 1
    _assert_outer_path(paths[0])
    assert paths[0]["depth"] == -3
    assert report["model_top_z_mm"] == pytest.approx(30)
    _assert_pocket(report["skipped_pockets"][0], "top", 1, [105, -45, 29], [111, -39, 30], 9 * math.pi)


@pytest.mark.parametrize("curved", [False, True])
def test_omitted_small_pocket_needs_no_cutter_fit_or_exportable_contour(tmp_path: Path, curved):
    if curved:
        pocket = Solid.extrude(Face(Wire.make_ellipse(0.5, 0.25)), (0, 0, 1)).moved(Location((8, 8, 2)))
        minimum, maximum, volume = [7.5, 7.75, 2], [8.5, 8.25, 3], math.pi / 8
    else:
        pocket = Box(0.5, 0.5, 1, align=(Align.MIN, Align.MIN, Align.MIN)).moved(Location((8, 8, 2)))
        minimum, maximum, volume = [8, 8, 2], [8.5, 8.5, 3], 0.25
    text, report = _export(tmp_path, _plate() - pocket, step_down=3)
    paths = _passes(text)
    assert len(paths) == 1
    _assert_outer_path(paths[0])
    assert report["hole_count"] == 0
    assert report["skipped_pocket_count"] == 1
    _assert_pocket(report["skipped_pockets"][0], "top", 1, minimum, maximum, volume)


@pytest.mark.parametrize("side,z", [("top", 2), ("bottom", 0)])
def test_counterbore_exports_only_proven_through_hole(tmp_path: Path, side, z):
    through = Cylinder(3, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 0)))
    recess = Cylinder(5, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, z)))
    text, report = _export(tmp_path, _plate() - through - recess, step_down=3)
    hole, outer = _passes(text)
    assert all(math.hypot(x, y) == pytest.approx(2, abs=1e-8) for x, y in hole["points"])
    _assert_outer_path(outer)
    assert report["hole_count"] == 1
    assert report["partial_export"] is True
    assert report["skipped_pocket_count"] == 1
    _assert_pocket(report["skipped_pockets"][0], side, 1, [15, 10, z], [25, 20, z + 1], 16 * math.pi)


def test_through_hole_inside_square_pocket_does_not_offset_pocket_corners(tmp_path: Path):
    through = Cylinder(2, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 0)))
    recess = Box(8, 8, 1, align=(Align.MIN, Align.MIN, Align.MIN)).moved(Location((16, 11, 2)))
    text, report = _export(tmp_path, _plate() - through - recess, step_down=3)
    hole, outer = _passes(text)
    assert all(math.hypot(x, y) == pytest.approx(1, abs=1e-8) for x, y in hole["points"])
    _assert_outer_path(outer)
    assert report["hole_count"] == 1
    _assert_pocket(report["skipped_pockets"][0], "top", 1, [16, 11, 2], [24, 19, 3], 64 - 4 * math.pi)


def test_connected_opposing_pockets_do_not_project_through_internal_obstruction(tmp_path: Path):
    top = Cylinder(5, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 2)))
    bottom = Cylinder(5, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 0)))
    narrow_connection = Cylinder(2, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 1)))
    text, report = _export(tmp_path, _plate() - top - bottom - narrow_connection, step_down=3)
    hole, outer = _passes(text)
    assert all(math.hypot(x, y) == pytest.approx(1, abs=1e-8) for x, y in hole["points"])
    _assert_outer_path(outer)
    assert report["hole_count"] == 1
    assert report["skipped_pocket_count"] == 2
    assert report["partial_export"]


def test_closed_internal_void_is_explicitly_reported_without_tool_entry(tmp_path: Path):
    void = Cylinder(3, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 1)))
    text, report = _export(tmp_path, _plate() - void, step_down=3)
    _assert_outer_path(_passes(text)[0])
    assert report["hole_count"] == 0
    assert report["skipped_pocket_count"] == 1
    _assert_pocket(report["skipped_pockets"][0], "internal", 1, [17, 12, 1], [23, 18, 2], 9 * math.pi)


@pytest.mark.parametrize("kind", ["sloped", "stepped", "undercut", "curved"])
def test_unmachinable_blind_pocket_shape_does_not_block_supported_outer_cut(tmp_path: Path, kind):
    if kind == "sloped":
        pocket = Cone(2, 3, 2, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 1)))
    elif kind == "curved":
        pocket = Sphere(2).moved(Location((20, 15, 3)))
    else:
        top_radius, bottom_radius = (3, 2) if kind == "stepped" else (2, 3)
        upper = Cylinder(top_radius, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 2)))
        lower = Cylinder(bottom_radius, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 1)))
        pocket = upper + lower
    text, report = _export(tmp_path, _plate() - pocket, step_down=3)
    outer, = _passes(text)
    _assert_outer_path(outer)
    assert report["hole_count"] == 0
    assert report["partial_export"] and report["skipped_pocket_count"] == 1
    assert report["skipped_pockets"][0]["side"] == "top"
    assert report["skipped_pockets"][0]["depth_mm"] == pytest.approx(2)


def test_through_taper_exports_only_proven_minimum_bore_and_reports_recess(tmp_path: Path):
    hole = Cone(3, 4, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 0)))
    text, report = _export(tmp_path, _plate() - hole, step_down=3)
    bore, outer = _passes(text)
    assert all(math.hypot(x, y) == pytest.approx(2, abs=1e-8) for x, y in bore["points"])
    _assert_outer_path(outer)
    assert report["hole_count"] == 1
    assert report["skipped_pocket_count"] == 1
    assert report["partial_export"]


def test_taper_is_rejected_instead_of_top_silhouette_export(tmp_path: Path):
    with pytest.raises(ValueError, match=r"constant-thickness|slopes"):
        _export(tmp_path, Cone(10, 7, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)))


@pytest.mark.parametrize("shape", [Sphere(5), _plate().rotate(Axis.X, 15)])
def test_nonhorizontal_or_nonplanar_geometry_is_rejected(tmp_path: Path, shape):
    with pytest.raises(ValueError, match=r"horizontal|slopes|Pockets"):
        _export(tmp_path, shape, cut_depth=20)


def test_multiple_parts_and_loose_geometry_are_explicitly_rejected(tmp_path: Path):
    compound = Compound(children=[_plate(), _plate().moved(Location((100, 0, 0)))])
    with pytest.raises(ValueError, match="exactly one solid"):
        _export(tmp_path, compound)
    mixed = Compound(children=[_plate(), Face(Wire.make_polygon([(100, 0, 0), (110, 0, 0), (110, 10, 0)]))])
    with pytest.raises(ValueError, match=r"loose|mixed"):
        _export(tmp_path, mixed)


def test_unsafe_clearance_omits_hole_without_losing_outer_profile(tmp_path: Path):
    # Nominal remaining wall 0.05 mm: smaller than the conservative chord margin.
    hole = Cylinder(5, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((5.05, 15, 0)))
    text, report = _export(tmp_path, _plate() - hole, step_down=3)
    _assert_outer_path(_passes(text)[0])
    assert report["hole_count"] == 0
    assert report["skipped_hole_count"] == 1
    assert report["partial_export"]


@pytest.mark.parametrize("spline", [False, True])
def test_curved_outer_offsets_have_bounded_emitted_chords(tmp_path: Path, spline):
    if spline:
        # Headstock-like spline/line joins include small sharp concavities.
        edges = [
            Edge.make_line((-29, 0), (29, 0)),
            Edge.make_line((29, 0), (29, 40)),
            Edge.make_bezier((29, 40), (29, 58), (42, 61), (41, 80)),
            Edge.make_bezier((41, 80), (41, 95), (-22, 96), (-31, 100)),
            Edge.make_bezier((-31, 100), (-42, 104), (-35, 68), (-29, 40)),
            Edge.make_line((-29, 40), (-29, 0)),
        ]
        face = Face(Wire(edges))
    else:
        face = Face(Wire.make_ellipse(20, 10))
    shape = Solid.extrude(face, (0, 0, 3))
    text, report = _export(tmp_path, shape, tool_diameter=7, step_down=3)
    face = face.moved(Location(-face.bounding_box().center()))
    points = _passes(text)[0]["points"]
    assert len(points) > 20
    assert points[0] == points[-1]
    assert not report["partial_export"]
    assert report["offset_recovery_tolerance_mm"] <= 0.025
    for first, second in pairwise(points):
        # Independently inspect the actual emitted chord, not exporter samples.
        for fraction in (0, 0.25, 0.5, 0.75, 1):
            point = tuple(a + fraction * (b - a) for a, b in zip(first, second, strict=True))
            distance = face.outer_wire().distance_to((*point, 0))
            assert distance >= 3.5 - report["chord_error_mm"] - 1e-6


@pytest.mark.parametrize("left_control_x,machinable", [(-35, False), (-29, True)])
def test_short_headstock_join_preserves_declared_cutter_clearance(
    tmp_path: Path, left_control_x, machinable
):
    face = Face(Wire([
        Edge.make_line((-29, 0), (29, 0)),
        Edge.make_line((29, 0), (29, 40)),
        Edge.make_bezier((29, 40), (29, 58), (42, 45), (41, 64)),
        Edge.make_bezier((41, 64), (41, 79), (-22, 80), (-31, 84)),
        Edge.make_bezier((-31, 84), (-42, 88), (left_control_x, 52), (-29, 40)),
        Edge.make_line((-29, 40), (-29, 0)),
    ]))
    if not machinable:
        with pytest.raises(ValueError):
            _export(tmp_path, Solid.extrude(face, (0, 0, 3)), tool_diameter=7, step_down=3)
        return
    text, report = _export(tmp_path, Solid.extrude(face, (0, 0, 3)), tool_diameter=7, step_down=3)
    face = face.moved(Location(-face.bounding_box().center()))
    outer, = _passes(text)
    assert outer["points"][0] == outer["points"][-1]
    path = Wire.make_polygon([(x, y, 0) for x, y in outer["points"][:-1]])
    assert Face(path).is_valid
    assert path.distance_to(face) >= 3.5 - report["chord_error_mm"] - 1e-6
    assert not report["partial_export"]


@pytest.mark.parametrize("resized,tool_diameter", [(False, 7), (True, 2)])
def test_trimmed_horn_offsets_follow_shared_vertices_without_reversing_toolpaths(
    tmp_path: Path, resized: bool, tool_diameter: float
):
    # Offset trimming gives these connected curves slightly different endpoint
    # coordinates inside their shared OCCT vertex tolerances.
    def body_point(x, y):
        return (x * 226 / 276, 150 + (y - 150) * 270 / 320) if resized else (x, y)

    edges = [Edge.make_bezier(*(body_point(*point) for point in points)) for points in [
        ((0, -170), (12, -170), (24, -150), (48, -148)),
        ((48, -148), (105, -148), (138, -124), (138, -82)),
        ((138, -82), (138, -44), (95, -10), (91, 25)),
        ((91, 25), (87, 62), (116, 85), (119, 103)),
        ((119, 103), (123, 126), (111, 133), (105, 113)),
        ((105, 113), (94, 78), (65, 72), (46, 91)),
    ]]
    edges.append(Edge.make_bezier(body_point(46, 91), body_point(30, 107),
                                  (29, body_point(0, 129)[1]), (29, 150)))
    edges.extend([
        Edge.make_line((29, 150), (29, 180)),
        Edge.make_line((29, 180), (-29, 180)),
        Edge.make_line((-29, 180), (-29, 150)),
        Edge.make_bezier((-29, 150), (-29, body_point(0, 120)[1]),
                         body_point(-55, 101), body_point(-72, 113)),
    ])
    edges.extend(Edge.make_bezier(*(body_point(*point) for point in points)) for points in [
        ((-72, 113), (-86, 122), (-83, 148), (-94, 145)),
        ((-94, 145), (-108, 141), (-105, 108), (-111, 92)),
        ((-111, 92), (-119, 70), (-87, 56), (-91, 25)),
        ((-91, 25), (-95, -10), (-138, -44), (-138, -82)),
        ((-138, -82), (-138, -124), (-112, -148), (-83, -148)),
        ((-83, -148), (-61, -148), (-63, -130), (-44, -133)),
        ((-44, -133), (-25, -136), (-12, -170), (0, -170)),
    ])
    face = Face(Wire(edges))
    # Solid cleanup merges tangent Bézier spans into the B-splines produced by
    # the plywood model, whose near-coincident planar recovery booleans fail.
    shape = Solid.extrude(face, (0, 0, 3)).clean()
    text, report = _export(tmp_path, shape, tool_diameter=tool_diameter, step_down=3)
    face = face.moved(Location(-face.bounding_box().center()))
    outer, = _passes(text)
    assert outer["points"][0] == outer["points"][-1]
    assert outer["depth"] == -3
    path = Wire.make_polygon([(x, y, 0) for x, y in outer["points"][:-1]])
    assert Face(path).is_valid
    assert path.distance_to(face) >= tool_diameter / 2 - report["chord_error_mm"] - 1e-6
    assert not report["partial_export"]


def _lead_in_plate(nut_pocket: bool):
    stock = Box(70, 40, 10, align=(Align.MIN, Align.MIN, Align.MIN))
    bore = Cylinder(1.7, 10, align=(Align.CENTER, Align.CENTER, Align.MIN))
    rear = Cone(1.9, 1.7, 0.2, align=(Align.CENTER, Align.CENTER, Align.MIN))
    floor = 7 if nut_pocket else 10
    front = Cone(1.7, 1.9, 0.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((0, 0, floor - 0.2)))
    cavity = bore + rear + front
    if nut_pocket:
        cavity += Cylinder(4.5, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((0, 0, 7)))
    larger = Cylinder(5, 10, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((50, 20, 0)))
    return stock - cavity.moved(Location((20, 20, 0))) - larger


@pytest.mark.parametrize("nut_pocket", [False, True])
@pytest.mark.parametrize("tool", [3, 7])
def test_opposing_lead_ins_keep_nominal_bore_separate_from_nut_recesses(tmp_path: Path, nut_pocket, tool):
    text, report = _export(tmp_path, _lead_in_plate(nut_pocket), tool_diameter=tool, cut_depth=10, step_down=10)
    paths = _passes(text)
    assert report["partial_export"]
    assert report["skipped_pocket_count"] == 2
    assert report["skipped_hole_count"] == (1 if tool == 7 else 0)
    assert report["hole_count"] == (1 if tool == 7 else 2)
    assert len(paths) == report["contour_count"] == report["hole_count"] + 1
    holes, outer = paths[:-1], paths[-1]
    assert min(x for x, _ in outer["points"]) == pytest.approx(-35 - tool / 2)
    assert max(x for x, _ in outer["points"]) == pytest.approx(35 + tool / 2)
    for path in holes:
        center = -15 if max(x for x, _ in path["points"]) < 0 else 15
        radius = (1.7 if center == -15 else 5) - tool / 2
        assert all(math.hypot(x - center, y) == pytest.approx(radius, abs=1e-8) for x, y in path["points"])
    if tool == 7:
        skipped, = report["skipped_holes"]
        assert skipped["bounds_mm"]["min"] == pytest.approx([18.3, 18.3, 0])
        assert skipped["bounds_mm"]["max"] == pytest.approx([21.7, 21.7, 10])
        assert "7 mm" in skipped["reason"] and "3.4" in skipped["reason"]
    assert f"HOLES OMITTED: {report['skipped_hole_count']}" in text
    assert "POCKETS OMITTED: 2" in text
    assert any("INCOMPLETE PART - PROFILE ONLY" in warning for warning in report["warnings"])


def test_connected_offset_cavities_without_proven_vertical_rim_are_omitted(tmp_path: Path):
    top = Cylinder(5, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((17, 15, 2)))
    bottom = Cylinder(5, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((23, 15, 0)))
    # A U-shaped connection goes around, not through, the overlapping mouths.
    left = Box(1, 7, 2, align=(Align.MIN, Align.MIN, Align.MIN)).moved(Location((16.5, 14.5, 0.5)))
    right = Box(1, 7, 2, align=(Align.MIN, Align.MIN, Align.MIN)).moved(Location((22.5, 14.5, 0.5)))
    bridge = Box(7, 1, 1, align=(Align.MIN, Align.MIN, Align.MIN)).moved(Location((16.5, 20.5, 1)))
    text, report = _export(tmp_path, _plate() - top - bottom - left - right - bridge, step_down=3)
    _assert_outer_path(_passes(text)[0])
    assert report["hole_count"] == 0
    assert report["partial_export"]
    skipped, = report["skipped_holes"]
    assert report["skipped_hole_count"] == 1
    assert skipped["reason"]
    assert skipped["bounds_mm"]["min"] == pytest.approx([12, 10, 0])
    assert skipped["bounds_mm"]["max"] == pytest.approx([28, 21.5, 3])


def test_proven_bore_does_not_hide_connected_both_sided_residual(tmp_path: Path):
    top = Cylinder(5, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 2)))
    bottom = Cylinder(5, 1, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 0)))
    bore = Cylinder(2, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((20, 15, 0)))
    left = Box(1, 2, 1.6, align=(Align.MIN, Align.MIN, Align.MIN)).moved(Location((16, 17, 0)))
    right = Box(1, 2, 1.6, align=(Align.MIN, Align.MIN, Align.MIN)).moved(Location((23, 17, 1.4)))
    bridge = Box(8, 1, 0.2, align=(Align.MIN, Align.MIN, Align.MIN)).moved(Location((16, 18, 1.4)))
    text, report = _export(tmp_path, _plate() - top - bottom - bore - left - right - bridge, step_down=3)
    hole, outer = _passes(text)
    assert all(math.hypot(x, y) == pytest.approx(1, abs=1e-8) for x, y in hole["points"])
    _assert_outer_path(outer)
    assert report["hole_count"] == 1
    assert report["skipped_pocket_count"] == 1
    residual, = report["skipped_pockets"]
    assert residual["side"] == "both"
    assert residual["depth_mm"] == pytest.approx(3)
    assert residual["bounds_mm"]["min"] == pytest.approx([15, 10, 0])
    assert residual["bounds_mm"]["max"] == pytest.approx([25, 20, 3])
    assert residual["volume_mm3"] > 0
    assert report["partial_export"]


def test_translated_box_exports_centered_cutter_compensation(tmp_path: Path):
    shape = Box(2, 4, 6).moved(Location((10, 20, 30)))
    text, report = _export(tmp_path, shape, tool_diameter=1, cut_depth=6, step_down=2, safe_z=3)
    points = _passes(text)[0]["points"]
    assert min(x for x, _ in points) == pytest.approx(-1.5)
    assert max(x for x, _ in points) == pytest.approx(1.5)
    assert min(y for _, y in points) == pytest.approx(-2.5)
    assert max(y for _, y in points) == pytest.approx(2.5)
    assert report["thickness_mm"] == pytest.approx(6)


def test_kernel_failure_returns_actionable_error_without_publishing_file(tmp_path: Path, monkeypatch):
    shape = _plate()

    def unavailable_geometry(*args, **kwargs):
        raise RuntimeError("kernel Boolean/extrusion failed")

    monkeypatch.setattr(Solid, "extrude", unavailable_geometry)
    with pytest.raises(ValueError, match=r"Exact profile geometry.*kernel"):
        _export(tmp_path, shape)
    assert not (tmp_path / "profile.nc").exists()
