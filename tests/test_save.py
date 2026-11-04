from __future__ import annotations

from pathlib import Path

import pytest
from build123d import import_brep, import_step
from build123d.topology import Shape

from cadiste.cli import main

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
