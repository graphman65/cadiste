from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from build123d import Box, BuildPart

from cadiste import tracing
from cadiste.source import capture_sources


@dataclass(frozen=True)
class Dimensions:
    width: float = 2


def add_box(dimensions: Dimensions) -> None:
    Box(dimensions.width, 3, 4)


def test_repeated_component_calls_keep_their_observed_parameter_overrides_separate():
    with tracing() as trace:
        with BuildPart():
            add_box(Dimensions(width=7))
            add_box(Dimensions(width=11))

    first, second = (op.site[-1] for op in trace.ops)
    assert first.invocation != second.invocation
    first_args = {parameter["name"]: parameter for parameter in first.parameters}
    second_args = {parameter["name"]: parameter for parameter in second.parameters}
    assert first_args["dimensions"]["fields"]["width"] == "7"
    assert second_args["dimensions"]["fields"]["width"] == "11"
    assert first_args["dimensions"]["type"] == f"{__name__}.Dimensions"


def test_captured_package_keeps_parameter_and_unentered_sibling_definitions(tmp_path: Path):
    package = tmp_path / "model"
    package.mkdir()
    (package / "__init__.py").write_text("")
    builder = package / "body.py"
    builder.write_text("from .dimensions import WIDTH\ndef body():\n    return WIDTH\n")
    dimensions = package / "dimensions.py"
    dimensions.write_text("WIDTH = 7\n")
    sibling = package / "lid.py"
    sibling.write_text("from .dimensions import WIDTH\ndef lid():\n    return WIDTH\n")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "secrets.py").write_text("TOKEN = 'not model source'\n")

    captured = capture_sources([str(builder)], tmp_path)
    dimensions.write_text("WIDTH = 99\n")
    sibling.unlink()

    assert captured["model/dimensions.py"] == "WIDTH = 7\n"
    assert "def lid():" in captured["model/lid.py"]
    assert "unrelated/secrets.py" not in captured
