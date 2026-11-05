from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest

from cadiste.agent import Agent, ToolError
from cadiste.components import CodeModel
from cadiste.hub import Hub
from cadiste.server import Build, Builds, Measurer, Project
from cadiste.source import code_ref, source_revision

SOURCE = """from dataclasses import dataclass
@dataclass
class Spec:
    wall: float = 2.0

def construct(spec: Spec):
    with BuildPart() as body:
        pocket(spec)
        pocket(spec)

def pocket(spec: Spec):
    with BuildSketch() as sketch:
        Circle(spec.wall)
    extrude(amount=spec.wall * 2)
"""


def _trace(offset: int = 0) -> dict[str, Any]:
    source = "\n" * offset + SOURCE

    def site(line: int, function: str, invocation: int) -> dict[str, Any]:
        return {
            "file": "model.py",
            "line": line + offset,
            "function": function,
            "label": function,
            "code": SOURCE.splitlines()[line - 1].strip(),
            "invocation": invocation,
        }

    outer = site(7, "construct", 10)
    builders = [
        {
            "index": 0,
            "kind": "BuildPart",
            "name": "body",
            "parent": None,
            "site": [outer],
            "consumed_by": [],
            "published_by": None,
        }
    ]
    ops = []
    groups = []
    for index, call_line in enumerate((8, 9)):
        called_at = site(call_line, "construct", 10)
        inner = site(12, "pocket", 20 + index)
        builders.append(
            {
                "index": index + 1,
                "kind": "BuildSketch",
                "name": "sketch",
                "parent": 0,
                "site": [called_at, inner],
                "consumed_by": [index + 2],
                "published_by": index + 2,
            }
        )
        ops.append(
            {
                "index": index,
                "kind": "Circle",
                "site": [called_at, site(13, "pocket", 20 + index)],
                "builder": index + 1,
                "inputs": [],
                "child": None,
                "arguments": [
                    {"name": "radius", "source": "spec.wall", "value": "3.5"},
                    {"name": "mode", "source": None, "value": "Mode.ADD"},
                ],
            }
        )
        groups.append(
            {
                "id": f"g{index + 1}",
                "type": "group",
                "label": "pocket",
                "function": "pocket",
                "defined_in": "model.py",
                "called_at": called_at,
                "invocation": 20 + index,
                "children": [
                    {
                        "id": f"b{index + 1}",
                        "type": "builder",
                        "builder": index + 1,
                        "children": [{"id": f"o{index}", "type": "op", "op": index}],
                    }
                ],
            }
        )
    for index in range(2):
        ops.append(
            {
                "index": index + 2,
                "kind": "extrude",
                "site": [site(8 + index, "construct", 10), site(14, "pocket", 20 + index)],
                "builder": 0,
                "inputs": [index + 1],
                "child": index + 1,
                "arguments": [{"name": "amount", "source": "spec.wall * 2", "value": "7.0"}],
            }
        )
    sources = {"model.py": source}
    return {
        "sources": sources,
        "source_revision": source_revision(sources),
        "builders": builders,
        "ops": ops,
        "roots": [{"id": "b0", "type": "builder", "builder": 0, "children": groups}],
        "invocations": {
            "20": {
                "file": "model.py",
                "function": "pocket",
                "label": "pocket",
                "parameters": [{"name": "spec", "value": "Spec(wall=3.5)", "type": "Spec", "fields": {"wall": "3.5"}}],
            }
        },
    }


def test_repeated_calls_keep_definition_site_and_runtime_context_separate():
    model = CodeModel(_trace(), "Part")
    first, second = model.reference("g1"), model.reference("g2")
    assert first["definition"] == second["definition"] == code_ref("model.py", "pocket")
    assert first["reference"] != second["reference"]
    assert first["call_site"]["reference"] != second["call_site"]["reference"]
    assert first["ancestry"][-1]["runtime_alias"] == "b0"
    invocations = model.inspect("pocket")["invocations"]
    assert [item["runtime_alias"] for item in invocations] == ["g1", "g2"]
    assert invocations[0]["call_site"]["calls"][0]["arguments"]
    assert "def pocket" in model.inspect("g1")["definition"]["source"]


def test_repeated_execution_of_one_call_site_is_not_collapsed():
    trace = _trace()
    trace["roots"][0]["children"][1]["called_at"] = copy.deepcopy(trace["roots"][0]["children"][0]["called_at"])
    model = CodeModel(trace, "Part")
    first, second = model.reference("g1"), model.reference("g2")
    assert first["call_site"]["reference"] == second["call_site"]["reference"]
    assert first["reference"] != second["reference"]
    assert [first["invocation"], second["invocation"]] == [1, 2]


def test_source_identities_survive_line_only_movement():
    before, after = CodeModel(_trace(), "Part"), CodeModel(_trace(19), "Part")
    for alias in ("b0", "g1", "g2", "b1", "o0", "o2"):
        assert before.reference(alias)["reference"] == after.reference(alias)["reference"]
        assert before.reference(alias)["definition"] == after.reference(alias)["definition"]
    assert before.revision != after.revision
    assert before.reference("g1")["call_site"]["line"] + 19 == after.reference("g1")["call_site"]["line"]


def test_nested_builders_shallow_children_parents_and_repeated_definition_boundary():
    model = CodeModel(_trace(), "Part")
    children = model.walk("b0")["nodes"][0]["children"]
    assert [node["runtime_alias"] for node in children] == ["g1", "g2"]
    assert "children" not in children[0]
    assert children[0]["has_more"]
    parent = model.walk("o0", direction="parents")["nodes"][0]["parents"]
    assert [node["runtime_alias"] for node in parent] == ["b1"]
    assert "parents" not in parent[0]
    assert model.walk("b0", depth=4)["nodes"][0]["children"][1]["stopped"]
    assert "children" not in model.walk("b0", depth=0)["nodes"][0]
    assert model.reference("o2")["reference"] != model.reference("b1")["reference"]
    assert model.inspect("o2")["invocations"][0]["child_builder"] == model.reference("b1")["reference"]
    consumed = model.inspect("b1")["invocations"][0]
    assert consumed["consumers"] == [model.reference("o2")["reference"]]


def test_parameter_results_keep_defaults_operation_values_and_observed_fields_separate():
    model = CodeModel(_trace(), "Part")
    followed = model.follow_parameter("g1", "spec.wall")
    assert followed["origins"]
    values = {item["name"]: item for item in followed["captured_operation_values"]}
    assert values["radius"] == {
        "component": model.reference("o0")["reference"],
        "operation": "o0",
        "name": "radius",
        "source": "spec.wall",
        "value": "3.5",
    }
    assert values["amount"]["value"] == "7.0" and values["amount"]["source"] == "spec.wall * 2"
    assert followed["observed_parameter_values"][0]["value"] == "3.5"
    assert "first traced event" in followed["observed_parameter_values"][0]["capture"]
    assert all(value["name"] != "mode" for value in followed["captured_operation_values"])
    assert model.inspect("g1")["invocations"][0]["observed_parameters"]["parameters"][0]["fields"]["wall"] == "3.5"


def test_parameter_observations_keep_helper_bindings_and_invocations_separate():
    trace = _trace()
    trace["sources"]["model.py"] = SOURCE.replace("pocket(spec)", "pocket()").replace(
        "def pocket(spec: Spec):", "def pocket(spec: Spec = Spec(wall=3.5)):"
    )
    trace["source_revision"] = source_revision(trace["sources"])
    trace["invocations"]["10"] = {
        "file": "model.py",
        "function": "construct",
        "label": "construct",
        "parameters": [{"name": "spec", "fields": {"wall": "9.0"}}],
    }
    model = CodeModel(trace, "Part")
    outer = model.follow_parameter(code_ref("model.py", "construct"), "spec.wall")
    inner = model.follow_parameter("g1", "spec.wall")
    assert [value["value"] for value in outer["observed_parameter_values"]] == ["9.0"]
    assert outer["captured_operation_values"] == []
    assert [value["value"] for value in inner["observed_parameter_values"]] == ["3.5"]
    assert {value["operation"] for value in inner["captured_operation_values"]} == {"o0", "o2"}


def test_missing_captured_source_never_reads_the_live_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    trace = _trace()
    trace["sources"] = {}
    trace.pop("source_revision")
    (tmp_path / "model.py").write_text(SOURCE)
    monkeypatch.chdir(tmp_path)
    model = CodeModel(trace, "Part")
    inspected = model.inspect("o0")
    assert inspected["definition"] is None
    assert not inspected["component"]["call_site"]["source_available"]
    assert inspected["invocations"][0]["arguments"][0]["value"] == "3.5"
    with pytest.raises(LookupError, match="no source definition"):
        model.follow_parameter("o0", "spec.wall")


@pytest.mark.parametrize("depth", [-1, 9, True, 1.5])
def test_invalid_depth_is_explicit(depth: Any):
    with pytest.raises(ValueError, match="depth"):
        CodeModel(_trace(), "Part").walk(depth=depth)


def test_unknown_and_ambiguous_component_errors_offer_choices():
    trace = _trace()
    trace["sources"]["other.py"] = "def pocket():\n    pass\n"
    model = CodeModel(trace, "Part")
    with pytest.raises(LookupError, match=r"ambiguous.*code:model\.py::pocket.*code:other\.py::pocket"):
        model.inspect("pocket")
    with pytest.raises(LookupError, match=r"unknown component.*choose"):
        model.walk("absent")
    with pytest.raises(ValueError, match="direction"):
        model.walk(direction="sideways")


class _Builds:
    def __init__(self, trace: dict[str, Any]):
        self.build = Build(json.dumps(trace).encode(), False, None, {})
        self.current = self.build
        self.requests: list[dict[str, Any]] = []

    def latest(self, target: dict[str, Any]) -> Build:
        self.requests.append(target)
        return self.build

    def get(self, target: dict[str, Any], fresh: bool) -> Build:
        self.requests.append(target)
        return self.current


def _agent(tmp_path: Path) -> tuple[Agent, _Builds, dict[str, Any]]:
    trace = _trace()
    builds = _Builds(trace)
    project = Project("model:build", (), ("Part", "Sibling"), assemblies={"All": "model:assembly"})
    return Agent(Hub(), cast(Builds, builds), Measurer(tmp_path), project, tmp_path), builds, trace


def test_catalog_builds_nothing_and_explicit_part_navigation_is_headless(tmp_path: Path):
    agent, builds, trace = _agent(tmp_path)
    catalog = agent.call("unlinked", "walk_components", {}).json()["data"]
    assert [item["part"] for item in catalog["parts"]] == ["Part", "Sibling"]
    assert catalog["assemblies"] == [{"name": "All", "entry_target": "model:assembly"}]
    assert builds.requests == []
    for tool, arguments in (
        ("walk_components", {"component": "g1"}),
        ("inspect_component", {"component": "g1"}),
        ("follow_parameter", {"component": "g1", "expression": "spec.wall"}),
        ("find_related_components", {"component": "g1"}),
    ):
        answer = agent.call("unlinked", tool, {"part": "Part", "revision": trace["source_revision"], **arguments})
        assert answer.data is not None and answer.data["source_revision"] == trace["source_revision"]
    assert all(target == {"kind": "part", "name": "Part"} for target in builds.requests)
    with pytest.raises(ToolError, match="source revision mismatch"):
        agent.call("unlinked", "inspect_component", {"part": "Part", "component": "g1", "revision": "stale"})
    with pytest.raises(ToolError, match=r"unknown part.*Part"):
        agent.call("unlinked", "walk_components", {"part": "Absent"})


def test_mcp_discovery_and_structured_results_use_the_same_headless_navigation(tmp_path: Path):
    pytest.importorskip("mcp")
    from mcp_types import CallToolResult, TextContent

    from cadiste.bridge import Bridge, server

    agent, _, _ = _agent(tmp_path)

    class LocalBridge(Bridge):
        def _post(self, path: str, body: dict[str, Any]) -> Any:
            if path == "/api/agent/sessions":
                return {"id": "unlinked", "code": "local"}
            return agent.call("unlinked", path.rsplit("/", 1)[-1], body).json()

    bridge = LocalBridge("http://unused")
    discovered = asyncio.run(server(bridge).list_tools())
    tools = {tool.name: tool for tool in discovered}
    assert {"walk_components", "inspect_component", "follow_parameter", "find_related_components"} <= tools.keys()
    assert tools["walk_components"].input_schema["properties"]["depth"]["maximum"] == 8
    result = bridge.call("test", "inspect_component", {"part": "Part", "component": "g1"})
    assert isinstance(result, CallToolResult)
    assert result.structured_content["definition"]["reference"] == code_ref("model.py", "pocket")
    assert isinstance(result.content[0], TextContent)
    assert "def pocket" in result.content[0].text
    old = bridge.call("test", "list_parts", {})
    assert isinstance(old, list) and isinstance(old[0], TextContent) and "Part" in old[0].text


def test_default_navigation_uses_current_build_but_revision_queries_remain_captured(tmp_path: Path):
    agent, builds, old = _agent(tmp_path)
    current = _trace(3)
    builds.current = Build(json.dumps(current).encode(), False, None, {})
    arguments = {"part": "Part", "component": "g1"}
    answer = agent.call("unlinked", "inspect_component", arguments)
    assert answer.data is not None and answer.data["source_revision"] == current["source_revision"]
    captured = agent.call("unlinked", "inspect_component", {**arguments, "revision": old["source_revision"]})
    assert captured.data is not None and captured.data["source_revision"] == old["source_revision"]
    with pytest.raises(ToolError, match="source revision mismatch"):
        agent.call("unlinked", "inspect_component", {**arguments, "revision": current["source_revision"]})


def test_unbuilt_scalar_helpers_and_sibling_callers_are_navigable():
    trace = _trace()
    trace["sources"]["helpers.py"] = """def scalar():
    return 3

def first():
    return scalar()

def unbuilt_sibling():
    return scalar()

def recursive(n):
    return recursive(n - 1)
"""
    model = CodeModel(trace, "Part")
    inspected = model.inspect("code:helpers.py::scalar")
    assert inspected["invocations"] == []
    assert inspected["definition"]["source"].startswith("def scalar")
    walked = model.walk("code:helpers.py::first")["nodes"][0]["children"]
    assert walked[0]["reference"] == "code:helpers.py::scalar"
    assert walked[0]["evidence"] == "static captured code"
    related = model.related("code:helpers.py::scalar")
    assert {item["reference"] for item in related["callers"]} == {
        "code:helpers.py::first",
        "code:helpers.py::unbuilt_sibling",
    }
    recursive = model.walk("code:helpers.py::recursive", depth=8)["nodes"][0]
    assert recursive["children"][0]["stopped"] == "cycle or repeated shared definition"


def test_class_parameter_queries_use_constructor_scope():
    trace = _trace()
    trace["sources"]["model.py"] += """
class Driver:
    def __init__(self, spec: Spec):
        tube_d = spec.wall * 2
"""
    trace["source_revision"] = source_revision(trace["sources"])
    followed = CodeModel(trace, "Part").follow_parameter("Driver", "tube_d")
    assert {item["qualified"] for item in followed["origins"]} == {"Driver.__init__.tube_d"}
    assert "Spec.wall" in {item["qualified"] for item in followed["dependencies"]}


def test_default_roots_skip_dispatchers_and_preserve_constructor_definition_roles():
    source = """def dispatch():
    Outer()

class Outer:
    def __init__(self):
        Inner()

class Inner:
    def __init__(self):
        with BuildPart():
            pass
"""
    stack = [
        {
            "file": "model.py",
            "line": line,
            "function": function,
            "label": "DynamicSubclass",
            "invocation": invocation,
            "code": source.splitlines()[line - 1].strip(),
        }
        for line, function, invocation in ((2, "dispatch", 1), (6, "Outer.__init__", 2), (10, "Inner.__init__", 3))
    ]
    trace = {
        "sources": {"model.py": source},
        "ops": [],
        "builders": [{"index": 0, "kind": "BuildPart", "name": "body", "site": stack}],
        "roots": [{"id": "b0", "type": "builder", "builder": 0, "children": []}],
    }
    model = CodeModel(trace, "Part")
    root = model.walk()["nodes"][0]
    assert root["label"] == "Outer" and root["definition"] == "code:model.py::Outer.__init__"
    assert root["children"][0]["label"] == "Inner"
    assert model.inspect("b0")["invocations"][0]["call_stack"][0]["function"] == "dispatch"
    assert model.inspect(root["reference"])["invocations"][0]["call_stack"][0]["function"] == "dispatch"
