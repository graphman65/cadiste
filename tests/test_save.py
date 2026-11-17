from __future__ import annotations

from pathlib import Path

import pytest
from build123d import import_brep, import_step, import_stl
from build123d.topology import Shape

from cadiste.cli import _exclude_hidden, main

_MODEL = """
from build123d import Box, Cylinder


def build(name):
    return Box(10, 10, 10) if name == "cube" else Cylinder(5, 20)
"""


@pytest.fixture
def model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    (tmp_path / "cadiste_save_model.py").write_text(_MODEL)
    monkeypatch.syspath_prepend(str(tmp_path))
    return "cadiste_save_model:build"


def _volume(shape: Shape) -> float:
    return sum(solid.volume for solid in shape.solids())


def test_a_folder_saved_as_step_keeps_each_part_named_in_its_group(tmp_path: Path, model: str):
    output = tmp_path / "parts.step"

    main(["save", "--each", "--label", "pair", "--output", str(output), model, "--", "cube", "rod"])

    group = import_step(output)
    assert group.label == "pair"
    children: list[Shape] = list(group.children)
    assert [child.label for child in children] == ["cube", "rod"]
    assert _volume(children[0]) == pytest.approx(1000, rel=1e-6)
    assert _volume(children[1]) == pytest.approx(500 * 3.141592653589793, rel=1e-3)


def test_a_part_saved_as_step_is_named_after_its_label(tmp_path: Path, model: str):
    output = tmp_path / "cube.step"

    main(["save", "--label", "Cube", "--output", str(output), model, "--", "cube"])

    assert import_step(output).label == "Cube"


def test_brep_and_stl_hold_the_same_part(tmp_path: Path, model: str):
    brep, stl = tmp_path / "cube.brep", tmp_path / "cube.stl"

    main(["save", "--format", "brep", "--output", str(brep), model, "--", "cube"])
    main(["save", "--format", "stl", "--output", str(stl), model, "--", "cube"])

    assert _volume(import_brep(brep)) == pytest.approx(1000, rel=1e-6)
    # Binary STL: an 80-byte header, the triangle count, 50 bytes per triangle; a box is 12 triangles.
    assert int.from_bytes(stl.read_bytes()[80:84], "little") == 12


_ASSEMBLY = """
from build123d import Box, Color, Compound, Location


def build():
    first = Box(2, 2, 2)
    first.label = "bolt"
    first.color = Color("red")
    second = Box(2, 2, 2).moved(Location((20, 0, 0)))
    second.label = "bolt"
    second.color = Color("blue")
    arm = Compound(label="arm", children=[first, second])
    arm.location = Location((100, 0, 0), (0, 0, 90))
    spare = Box(2, 2, 2).moved(Location((-30, 0, 0)))
    spare.label = "spare"
    rig = Compound(label="Rig", children=[arm, spare])
    rig.location = Location((0, 50, 0))
    return rig
"""


@pytest.fixture
def assembly_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    (tmp_path / "cadiste_save_assembly.py").write_text(_ASSEMBLY)
    monkeypatch.syspath_prepend(str(tmp_path))
    return "cadiste_save_assembly:build"


def _assembly_paths() -> tuple[str, str, str]:
    """Use the same converter as the viewer, including duplicate sibling naming."""
    from cadiste_save_assembly import build
    from ocp_tessellate.convert import to_ocpgroup

    group, _ = to_ocpgroup(build(), names=["Rig"], colors=[None], alphas=[None])
    arm, spare = group.objects
    first, second = arm.objects
    return f"/Rig/arm/{first.name}", f"/Rig/arm/{second.name}", f"/Rig/{spare.name}"


@pytest.mark.parametrize("form", ["step", "brep", "stl"])
def test_hidden_instances_prune_actual_geometry_and_keep_nested_placements(
    tmp_path: Path, assembly_model: str, form: str,
):
    first, _, spare = _assembly_paths()
    output = tmp_path / f"visible.{form}"
    main([
        "save", "--format", form, "--output", str(output),
        "--hidden", first, "--hidden", spare, assembly_model,
    ])
    shape = {"step": import_step, "brep": import_brep, "stl": import_stl}[form](output)
    bounds = shape.bounding_box()
    assert pytest.approx((99, 69, -1), abs=1e-5) == (bounds.min.X, bounds.min.Y, bounds.min.Z)
    assert pytest.approx((101, 71, 1), abs=1e-5) == (bounds.max.X, bounds.max.Y, bounds.max.Z)
    if form != "stl":
        assert len(shape.solids()) == 1
        assert _volume(shape) == pytest.approx(8)
    else:
        assert int.from_bytes(output.read_bytes()[80:84], "little") == 12
    if form == "step":
        assert shape.label == "Rig"
        arm = next(node for node in shape.descendants if node.label == "arm")
        assert arm.parent.label == "Rig"
        assert len(arm.children) == 1
        assert arm.children[0].label == "bolt"
        color = arm.children[0].color
        assert color is not None
        assert tuple(color) == pytest.approx((0, 0, 1, 1), abs=1e-6)


def test_group_exclusion_and_no_hidden_preserve_geometry(tmp_path: Path, assembly_model: str):
    from cadiste_save_assembly import build

    original = build()
    assert _exclude_hidden(original, None) is original
    assert _exclude_hidden(original, []) is original
    for hidden, count in [(None, 3), ("/Rig/arm", 1)]:
        output = tmp_path / f"{count}.brep"
        args = ["save", "--format", "brep", "--output", str(output)]
        if hidden:
            args += ["--hidden", hidden]
        main([*args, assembly_model])
        shape = import_brep(output)
        assert len(shape.solids()) == count
        assert _volume(shape) == pytest.approx(count * 8)


def test_all_hidden_and_stale_paths_fail_without_output(tmp_path: Path, assembly_model: str):
    first, second, spare = _assembly_paths()
    for hidden in [["/Rig"], [first, second, spare], ["/Rig/absent"]]:
        output = tmp_path / "rejected.step"
        args = ["save", "--output", str(output)]
        for path in hidden:
            args += ["--hidden", path]
        with pytest.raises(SystemExit, match=r"all geometry is hidden|unknown hidden viewer paths"):
            main([*args, assembly_model])
        assert not output.exists()
