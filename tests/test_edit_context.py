from __future__ import annotations

from typing import Any

import pytest

from cadiste.code_index import SourceIndex
from cadiste.components import CodeModel
from cadiste.source import code_ref

SOURCE = """from dataclasses import dataclass
@dataclass
class Spec:
    width: float = 10
    cap: float = 7
    @property
    def diameter(self):
        return max(self.width - 2, self.cap)

def consume(value):
    return value

def build(spec: Spec):
    diameter = spec.diameter
    consume(diameter)
"""


def model(source: str = SOURCE, parameters: list[dict[str, Any]] | None = None) -> CodeModel:
    line = len(source.splitlines())
    return CodeModel(
        {
            "sources": {"model.py": source},
            "ops": [
                {
                    "index": 0,
                    "kind": "consume",
                    "site": [
                        {
                            "file": "model.py",
                            "function": "build",
                            "line": line,
                            "code": source.splitlines()[-1].strip(),
                            "invocation": 4,
                        }
                    ],
                    "arguments": [{"name": "value", "source": "diameter", "value": "7"}],
                }
            ],
            "roots": [{"id": "o0", "type": "op", "op": 0}],
            "invocations": {
                "4": {
                    "file": "model.py",
                    "function": "build",
                    "parameters": parameters
                    if parameters is not None
                    else [{"name": "spec", "value": "Spec(width=5, cap=7)", "fields": {"width": "5", "cap": "7"}}],
                }
            },
        },
        "Part",
    )


def test_active_dimension_branch_uses_observed_receiver_without_replacing_defaults():
    packet = model().edit_context("o0", ["diameter"])
    dimension = packet["expressions"][0]
    assert dimension["static"]["value"] == 8
    assert dimension["static"]["branches"][0]["active_arguments"] == [0]
    observed = dimension["observed"][0]
    assert observed["value"] == 7
    assert observed["branches"][0]["active_arguments"] == [1]
    assert observed["drivers"][0]["expression"] == "spec.cap"
    assert observed["drivers"][0]["value"] == 7
    assert observed["drivers"][0]["invocation"] == observed["invocation"] == 4
    assert observed["drivers"][0]["component"] == observed["component"] == packet["component"]["reference"]
    assert code_ref("model.py", "consume") in {entry["reference"] for entry in dimension["consumers"]}
    assert packet["arguments"][0]["value"] == "7"
    assert packet["source_scope"].startswith("captured")


def test_missing_runtime_fields_never_fall_back_to_declared_defaults():
    packet = model(parameters=[{"name": "spec", "fields": {"width": "5"}}]).edit_context("o0", ["diameter"])
    dimension = packet["expressions"][0]
    assert dimension["static"]["value"] == 8
    assert dimension["observed"][0]["status"] == "unresolved"
    assert "value" not in dimension["observed"][0]
    assert dimension["observed"][0]["branches"][0]["status"] == "unresolved"
    assert any("declared defaults are separate" in item["reason"] for item in dimension["observed"][0]["boundaries"])


def test_two_runtime_receivers_keep_distinct_values_even_for_the_same_field_definition():
    source = SOURCE.replace("def build(spec: Spec):", "def build(spec: Spec, other: Spec):").replace(
        "diameter = spec.diameter", "diameter = max(spec.width, other.width)"
    )
    parameters = [{"name": "spec", "fields": {"width": "3"}}, {"name": "other", "fields": {"width": "12"}}]
    packet = model(source, parameters).edit_context("build", ["diameter"])
    observed = packet["expressions"][0]["observed"][0]
    assert observed["value"] == 12
    assert observed["drivers"][0]["expression"] == "other.width"
    assert observed["branches"][0]["active_arguments"] == [1]


def test_edit_context_bounds_each_section_and_retains_source_identity():
    packet = model().edit_context("build", ["diameter", "spec.width", "spec.cap"], limit=1)
    assert packet["definition"]["reference"] == code_ref("model.py", "build")
    assert packet["pages"]["expressions"] == {
        "total": 3,
        "offset": 0,
        "limit": 1,
        "returned": 1,
        "omitted": 2,
        "next_offset": 1,
    }
    assert len(packet["source"]) == 1 and packet["pages"]["source"]["omitted"] > 0
    assert packet["pages"]["observations"]["total"] == 3
    branch = packet["expressions"][0]["observed"][0]["branches"][0]
    assert len(branch["arguments"]) == 1 and branch["arguments_page"]["total"] == 2
    moved = model("\n" * 5 + SOURCE).edit_context("build", ["diameter"])
    assert moved["definition"]["reference"] == packet["definition"]["reference"]
    assert moved["definition"]["line"] == packet["definition"]["line"] + 5


def test_default_expressions_include_captured_arguments_and_observed_fields():
    expressions = {entry["expression"] for entry in model().edit_context("o0")["expressions"]}
    assert {"spec", "spec.width", "spec.cap", "diameter"} <= expressions


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_edit_context_rejects_invalid_limits(limit):
    with pytest.raises(ValueError, match="limit"):
        model().edit_context("build", limit=limit)


@pytest.mark.parametrize("expressions", ["width", [""], [3]])
def test_edit_context_rejects_invalid_expression_lists(expressions):
    with pytest.raises(ValueError, match="expressions"):
        model().edit_context("build", expressions)


@pytest.mark.parametrize(
    "source,expression",
    [
        ("def build(max):\n    return max(3, 9)\n", "max(3, 9)"),
        ("max = external\ndef build():\n    return max(3, 9)\n", "max(3, 9)"),
        ("def build():\n    result = max(3, 9)\n    max = external\n    return result\n", "result"),
        ("def build():\n    return external(3)\n", "external(3)"),
        ("def build():\n    return min(3, unknown)\n", "min(3, unknown)"),
        ("def build():\n    return max([3, 9])\n", "max([3, 9])"),
    ],
)
def test_unresolved_and_shadowed_calls_are_not_guessed(source, expression):
    index = SourceIndex({"model.py": source})
    result = index.dimension_drivers(code_ref("model.py", "build"), expression)
    assert result["status"] == "unresolved" and "value" not in result
    assert result["boundaries"]


def test_tied_active_branches_remain_explicit():
    index = SourceIndex({"model.py": "LEFT = 4\nRIGHT = 4\ndef build():\n    return max(LEFT, RIGHT)\n"})
    result = index.dimension_drivers(code_ref("model.py", "build"), "max(LEFT, RIGHT)")
    assert result["value"] == 4
    assert result["branches"][0]["tied"] is True
    assert result["branches"][0]["active_arguments"] == [0, 1]
    assert {entry["reference"] for entry in result["drivers"]} == {
        code_ref("model.py", "LEFT"),
        code_ref("model.py", "RIGHT"),
    }


def test_conditional_bindings_and_property_cycles_remain_explicit():
    source = """class Spec:
    @property
    def left(self):
        return self.right
    @property
    def right(self):
        return self.left

def build(spec: Spec):
    if external:
        diameter = 2
    else:
        diameter = 7
    return diameter
"""
    index = SourceIndex({"model.py": source})
    owner = code_ref("model.py", "build")
    conditional = index.dimension_drivers(owner, "diameter")
    assert conditional["status"] == "unresolved"
    assert conditional["boundaries"][0]["references"] == [code_ref("model.py", "build.diameter")]
    cyclic = index.dimension_drivers(owner, "spec.left")
    assert cyclic["status"] == "unresolved"
    assert any(entry["reason"] == "dependency cycle" for entry in cyclic["boundaries"])
    packet = CodeModel({"sources": {"model.py": source}}, "Part").edit_context("build", ["diameter", "spec.left"])
    assert packet["expressions"][0]["unresolved"]
    assert packet["expressions"][1]["cycles"]


def test_observed_parameter_defaults_are_not_static_runtime_fallbacks():
    source = "def build(width=3):\n    diameter = width * 2\n    consume(diameter)\n"
    packet = model(source, [{"name": "width", "value": "9"}]).edit_context("build", ["diameter"])
    assert packet["parameters"][0]["declared_default"] == "3"
    assert packet["expressions"][0]["static"]["value"] == 6
    assert packet["expressions"][0]["observed"][0]["value"] == 18
    missing = model(source, []).edit_context("build", ["diameter"])
    assert missing["expressions"][0]["observed"][0]["status"] == "unresolved"


def test_non_numeric_observed_repr_is_never_executed_or_used_as_a_default():
    source = "def build(width=3):\n    diameter = width * 2\n    consume(diameter)\n"
    parameters = [{"name": "width", "value": "__import__('os').getpid()"}]
    packet = model(source, parameters).edit_context("build", ["diameter"])
    observed = packet["expressions"][0]["observed"][0]
    assert observed["status"] == "unresolved"
    assert "finite numeric literal" in observed["boundaries"][0]["reason"]


def test_repeated_invocations_are_compared_without_merging_runtime_values():
    trace = model().trace
    trace["ops"].append(
        {
            **trace["ops"][0],
            "index": 1,
            "site": [{**trace["ops"][0]["site"][0], "invocation": 5}],
        }
    )
    trace["roots"].append({"id": "o1", "type": "op", "op": 1})
    trace["invocations"]["5"] = {
        "file": "model.py",
        "function": "build",
        "parameters": [{"name": "spec", "fields": {"width": "20", "cap": "7"}}],
    }
    packet = CodeModel(trace, "Part").edit_context("build", ["diameter"])
    observed = packet["expressions"][0]["observed"]
    assert [(item["invocation"], item["value"]) for item in observed] == [(4, 7), (5, 18)]
    assert observed[0]["component"] != observed[1]["component"]
    assert observed[0]["branches"][0]["active_arguments"] == [1]
    assert observed[1]["branches"][0]["active_arguments"] == [0]
    individual = CodeModel(trace, "Part").edit_context("o1", ["diameter"])
    assert [item["invocation"] for item in individual["expressions"][0]["observed"]] == [5]
