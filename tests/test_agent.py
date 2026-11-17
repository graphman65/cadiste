from __future__ import annotations

import pytest

from cadiste.agent import Agent, ToolError


@pytest.mark.parametrize(
    "change",
    [
        {"build_id": "other-build"},
        {"target": {"kind": "part", "name": "Other"}},
        {"target": {"kind": "assembly", "name": "Part"}},
    ],
)
def test_capture_identity_rejects_other_geometry_even_with_identical_source_revision(change):
    expected = {"target": {"kind": "part", "name": "Part"}, "build_id": "build-one"}
    result = {**expected, "source_revision": "same-source", "images": [], **change}
    with pytest.raises(ToolError, match="different target/build"):
        Agent._assert_capture(result, expected)


def test_source_revision_is_not_a_substitute_for_renderer_build_identity():
    state = {"target": {"kind": "part", "name": "Part"}, "source_revision": "captured-source", "status": "ok"}
    with pytest.raises(ToolError, match="no exact build identity"):
        Agent._identity(state)
    state["build_id"] = "geometry-generation"
    assert Agent._identity(state) == {"target": state["target"], "build_id": "geometry-generation"}


def test_capture_requires_identity_in_response_not_merely_images():
    expected = {"target": {"kind": "file", "path": "parts"}, "build_id": "one"}
    with pytest.raises(ToolError, match="different target/build"):
        Agent._assert_capture({"images": [{"view": "front", "data": "not-used"}]}, expected)
