from __future__ import annotations

import ast
import json
import textwrap

import pytest

from cadiste.code_index import SourceIndex
from cadiste.source import code_ref


def _sources(**files: str) -> dict[str, str]:
    return {file.replace("__", "/") + ".py": textwrap.dedent(source).lstrip() for file, source in files.items()}


def _references(entries: list[dict]) -> set[str]:
    return {entry["reference"] for entry in entries}


@pytest.fixture
def index() -> SourceIndex:
    return SourceIndex(
        {
            "fixture/__init__.py": "from .params import Params as PublicParams\n",
            "fixture/params.py": textwrap.dedent("""\
            from dataclasses import dataclass
            raise AssertionError("navigation must never import a model")
            @dataclass
            class Params:
                axle_d: float = 6
                wall: float = 2
                inner_w: float = 30
                lid_t: float = 1
                @property
                def module_w(self):
                    return self.inner_w + 2 * self.wall
            DEFAULT = Params(axle_d=8)
        """),
            "fixture/spec.py": textwrap.dedent("""\
            from dataclasses import dataclass
            @dataclass
            class Spec:
                clearance: float = 0.2
                steer_axle_tube_wall: float = 3
                nose_len: float = 4
            DEFAULT_SPEC = Spec()
        """),
            "fixture/nose.py": textwrap.dedent("""\
            from dataclasses import dataclass
            from . import PublicParams
            from .spec import Spec
            @dataclass
            class Nose:
                start_x: float
                wall_dx: float
                @classmethod
                def of(cls, p: PublicParams, spec: Spec) -> "Nose":
                    return cls(start_x=p.module_w - spec.nose_len, wall_dx=p.wall)
                def lid_end_x(self, lid_t: float):
                    return self.start_x - self.wall_dx + lid_t
        """),
            "fixture/module.py": textwrap.dedent("""\
            from fixture import PublicParams
            from .params import DEFAULT
            from .spec import Spec, DEFAULT_SPEC
            from .nose import Nose
            class SteeringModule:
                def __init__(self, params: PublicParams = DEFAULT, spec: Spec = DEFAULT_SPEC):
                    p = params
                    nose = Nose.of(p, spec)
                    pin_bore_d = p.axle_d + spec.clearance
                    tube_d = pin_bore_d + 2 * spec.steer_axle_tube_wall
                    roof = nose.lid_end_x(p.lid_t)
                    consume(tube_d, roof)
            def consume(tube_d, roof):
                return tube_d + roof
        """),
            "fixture/lid.py": textwrap.dedent("""\
            from . import PublicParams
            from .spec import Spec
            from .nose import Nose
            class SteeringLid:
                def __init__(self, p: PublicParams, spec: Spec):
                    nose = Nose.of(p, spec)
                    self.stop = nose.lid_end_x(p.lid_t)
        """),
            "fixture/unrelated.py": textwrap.dedent("""\
            from dataclasses import dataclass
            @dataclass
            class Params:
                module_w: float = 99
            class Unrelated:
                def __init__(self, p: Params):
                    tube_d = p.module_w
        """),
        }
    )


def test_relative_absolute_imports_and_package_reexports_resolve(index: SourceIndex):
    assert index.resolve("fixture/module.py", "SteeringModule.__init__", "p.module_w") == [
        code_ref("fixture/params.py", "Params.module_w")
    ]
    assert index.resolve("fixture/module.py", "SteeringModule.__init__", "spec.steer_axle_tube_wall") == [
        code_ref("fixture/spec.py", "Spec.steer_axle_tube_wall")
    ]
    assert index.resolve("fixture/nose.py", "Nose.of", "p.module_w") == [
        code_ref("fixture/params.py", "Params.module_w")
    ]


def test_derived_dimension_follows_local_aliases_and_exact_fields(index: SourceIndex):
    owner = code_ref("fixture/module.py", "SteeringModule.__init__")
    result = index.follow_parameter(owner, "tube_d")
    assert _references(result["origins"]) == {code_ref("fixture/module.py", "SteeringModule.__init__.tube_d")}
    assert {
        code_ref("fixture/module.py", "SteeringModule.__init__.pin_bore_d"),
        code_ref("fixture/params.py", "Params.axle_d"),
        code_ref("fixture/spec.py", "Spec.clearance"),
        code_ref("fixture/spec.py", "Spec.steer_axle_tube_wall"),
    } <= _references(result["dependencies"])
    wall = next(entry for entry in result["dependencies"] if entry["qualified"] == "Spec.steer_axle_tube_wall")
    assert wall["declared_default"] == "3"
    assert wall["evidence"][0]["expression"] == "spec.steer_axle_tube_wall"
    assert wall["evidence"][0]["file"] == "fixture/module.py"
    assert wall["evidence"][0]["line"] > 0
    assert result["origins"][0]["expression"] == "pin_bore_d + 2 * spec.steer_axle_tube_wall"
    assert any(entry["expression"] == "tube_d" for entry in result["consumers"])
    assert result["source_revision"] == index.revision


def test_property_and_factory_chains_reach_base_dimensions(index: SourceIndex):
    owner = code_ref("fixture/module.py", "SteeringModule.__init__")
    assert index.resolve("fixture/module.py", "SteeringModule.__init__", "nose.lid_end_x") == [
        code_ref("fixture/nose.py", "Nose.lid_end_x")
    ]
    result = index.follow_parameter(owner, "nose.lid_end_x(p.lid_t)")
    assert code_ref("fixture/nose.py", "Nose.lid_end_x") in _references(result["origins"])
    assert code_ref("fixture/params.py", "Params.lid_t") in _references(result["origins"])
    assert {
        code_ref("fixture/nose.py", "Nose.start_x"),
        code_ref("fixture/nose.py", "Nose.wall_dx"),
        code_ref("fixture/params.py", "Params.module_w"),
        code_ref("fixture/params.py", "Params.inner_w"),
        code_ref("fixture/params.py", "Params.wall"),
        code_ref("fixture/spec.py", "Spec.nose_len"),
    } <= _references(result["dependencies"])
    field = index.definition(code_ref("fixture/nose.py", "Nose.start_x"))
    assert field["initializers"][0]["expression"] == "p.module_w - spec.nose_len"


def test_related_siblings_use_shared_code_not_parameter_spelling(index: SourceIndex):
    result = index.related(code_ref("fixture/module.py", "SteeringModule"))
    lid = code_ref("fixture/lid.py", "SteeringLid.__init__")
    assert lid in _references(result["shared_helpers"])
    assert lid in _references(result["shared_parameters"])
    assert all(
        "unrelated.py" not in entry["reference"]
        for key in ("shared_helpers", "shared_parameters")
        for entry in result[key]
    )
    shared = next(entry for entry in result["shared_parameters"] if entry["reference"] == lid)
    evidence = index.related(code_ref("fixture/module.py", "SteeringModule"), section="evidence", limit=100)
    assert any(
        entry.get("shared") == code_ref("fixture/params.py", "Params.module_w") and entry["reference"] == lid
        for entry in evidence["evidence"]
    )
    assert shared["evidence"]


def test_defaults_are_declared_and_do_not_become_runtime_values(index: SourceIndex):
    definition = index.definition(code_ref("fixture/module.py", "SteeringModule.__init__"))
    parameter = next(entry for entry in definition["parameters"] if entry["name"] == "params")
    assert parameter["declared_default"] == "DEFAULT"
    assert "value" not in parameter
    field = index.definition(code_ref("fixture/params.py", "Params.axle_d"))
    assert field["declared_default"] == "6"
    assert field["initializers"][0]["expression"] == "8"
    assert "value" not in field


def test_lexical_shadowing_and_default_instance_inference():
    index = SourceIndex(
        _sources(
            model="""
        class Outer:
            width: float = 3
        class Inner:
            width: float = 9
        p = Outer()
        def outer(p: Inner):
            def nested():
                return p.width
            return nested()
        def defaults(p=Outer()):
            alias = p
            return alias.width
        def local():
            before = p.width
            p = Inner()
            after = p.width
    """
        )
    )
    assert index.resolve("model.py", "outer.nested", "p.width") == [code_ref("model.py", "Inner.width")]
    assert index.resolve("model.py", "defaults", "alias.width") == [code_ref("model.py", "Outer.width")]
    before = index.definition(code_ref("model.py", "local.before"))["line"]
    assert index.resolve("model.py", "local", "p.width", before) == []
    assert index.resolve("model.py", "local", "p.width") == [code_ref("model.py", "Inner.width")]


def test_conditional_type_bindings_report_both_possibilities():
    index = SourceIndex(
        _sources(
            model="""
        class First:
            width: float = 1
        class Second:
            width: float = 2
        def build(flag):
            p = First()
            if flag:
                p = Second()
            return p.width
    """
        )
    )
    result = index.follow_parameter(code_ref("model.py", "build"), "p.width")
    assert _references(result["origins"]) == {code_ref("model.py", "First.width"), code_ref("model.py", "Second.width")}
    assert result["unresolved"]
    assert index.follow_parameter(code_ref("model.py", "build"), "p")["unresolved"]


def test_property_cycles_and_dynamic_values_are_explicit():
    index = SourceIndex(
        _sources(
            model="""
        class Params:
            @property
            def a(self):
                return self.b + 1
            @property
            def b(self):
                return self.a + 1
        def build(p: Params):
            dynamic = external_factory().width
            return p.a
    """
        )
    )
    result = index.follow_parameter(code_ref("model.py", "build"), "p.a")
    assert result["cycles"]
    cycle = result["cycles"][0]["path"]
    assert set(cycle) == {code_ref("model.py", "Params.a"), code_ref("model.py", "Params.b")}
    dynamic = index.follow_parameter(code_ref("model.py", "build"), "dynamic")
    assert dynamic["unresolved"]
    assert dynamic["unresolved"][0]["expression"] == "external_factory().width"


def test_import_module_aliases_and_inherited_fields():
    index = SourceIndex(
        {
            "pkg/__init__.py": "",
            "pkg/base.py": "class Base:\n    diameter: float = 5\n",
            "pkg/model.py": (
                "import pkg.base as b\nclass Child(b.Base):\n    pass\ndef build(p: Child):\n    return p.diameter\n"
            ),
        }
    )
    assert index.resolve("pkg/model.py", "build", "p.diameter") == [code_ref("pkg/base.py", "Base.diameter")]


def test_identity_survives_line_changes_and_errors_stay_in_captured_boundary():
    sources = {"model.py": "def build(x=2):\n    return x\n"}
    before = SourceIndex(sources)
    after = SourceIndex({"model.py": "\n\n" + sources["model.py"]})
    reference = code_ref("model.py", "build")
    assert before.definition(reference)["reference"] == after.definition(reference)["reference"]
    assert before.revision != after.revision
    assert before.at("model.py", 0, "build") == reference
    with pytest.raises(LookupError):
        before.definition(code_ref("missing.py", "build"))
    with pytest.raises(ValueError):
        before.follow_parameter(reference, "x; import os")
    assert before.follow_parameter(reference, "not_captured.attribute")["unresolved"]


def test_source_bundle_is_parsed_once_and_queries_do_not_read_live_files(monkeypatch):
    original = ast.parse
    parsed_files: list[str] = []

    def capture(source, filename="<unknown>", mode="exec", **kwargs):
        if mode == "exec":
            parsed_files.append(filename)
        return original(source, filename=filename, mode=mode, **kwargs)

    monkeypatch.setattr(ast, "parse", capture)
    index = SourceIndex(
        {"does-not-exist.py": "class Params:\n    width: float = 5\ndef build(p: Params):\n    return p.width\n"}
    )
    owner = code_ref("does-not-exist.py", "build")
    index.follow_parameter(owner, "p.width")
    index.related(owner)
    index.resolve("does-not-exist.py", "build", "p.width")
    assert parsed_files == ["does-not-exist.py"]


def test_property_identity_queries_and_unbuilt_method_callers(index: SourceIndex):
    result = index.follow_parameter(code_ref("fixture/params.py", "Params.module_w"), "module_w")
    assert {
        code_ref("fixture/params.py", "Params.inner_w"),
        code_ref("fixture/params.py", "Params.wall"),
    } <= _references(result["dependencies"])
    related = index.related(code_ref("fixture/nose.py", "Nose.lid_end_x"))
    assert {
        code_ref("fixture/module.py", "SteeringModule.__init__"),
        code_ref("fixture/lid.py", "SteeringLid.__init__"),
    } <= _references(related["callers"])


def test_annotation_and_default_names_use_definition_not_parameter_scope():
    index = SourceIndex(
        _sources(
            model="""
        class Params:
            width: float = 3
        instance = Params()
        def build(Params: Params, instance=instance):
            return Params.width + instance.width
    """
        )
    )
    assert index.resolve("model.py", "build", "Params.width") == [code_ref("model.py", "Params.width")]
    assert index.resolve("model.py", "build", "instance.width") == [code_ref("model.py", "Params.width")]


def test_tuple_aliases_and_dataclass_factory_import_aliases():
    index = SourceIndex(
        _sources(
            model="""
        from dataclasses import dataclass as record
        from typing import Self
        @record
        class Params:
            width: float = 3
        @record
        class Derived:
            width: float
            @classmethod
            def of(cls, p: Params) -> Self:
                return cls(width=p.width * 2)
        def build(p=Params()):
            shape, width = Derived.of(p), p.width
            return shape.width + width
    """
        )
    )
    assert index.resolve("model.py", "build", "shape.width") == [code_ref("model.py", "Derived.width")]
    result = index.follow_parameter(code_ref("model.py", "build"), "shape.width")
    assert code_ref("model.py", "Params.width") in _references(result["dependencies"])


def test_anonymous_shadowing_and_unknown_destructuring_are_not_guessed():
    index = SourceIndex(
        _sources(
            model="""
        class Params:
            width: float = 3
        p = Params()
        def build():
            widths = [p.width for p in external()]
            first, second = external()
            return widths
    """
        )
    )
    result = index.follow_parameter(code_ref("model.py", "build"), "widths")
    assert not result["dependencies"]
    assert result["unresolved"]
    assert index.follow_parameter(code_ref("model.py", "build"), "first")["unresolved"]


def test_consumers_identify_actual_called_code_and_argument_position(index: SourceIndex):
    result = index.follow_parameter(code_ref("fixture/module.py", "SteeringModule.__init__"), "tube_d")
    consumer = next(
        entry for entry in result["consumers"] if entry["reference"] == code_ref("fixture/module.py", "consume")
    )
    assert consumer["argument"] == 0
    assert consumer["expression"] == "tube_d"
    assert consumer["evidence"][0]["expression"] == "consume(tube_d, roof)"


def test_partly_dynamic_base_is_reported_as_uncertain():
    index = SourceIndex(
        _sources(
            model="""
        class Known:
            width: float = 3
        def build(flag):
            p = Known()
            if flag:
                p = external_factory()
            return p.width
    """
        )
    )
    result = index.follow_parameter(code_ref("model.py", "build"), "p.width")
    assert code_ref("model.py", "Known.width") in _references(result["origins"])
    assert result["unresolved"]


def test_method_objects_are_not_treated_as_called_return_values():
    index = SourceIndex(
        _sources(
            model="""
        class Result:
            width: float = 3
        class Factory:
            def make(self) -> Result:
                return Result()
        def build(p: Factory):
            return p.make().width
    """
        )
    )
    assert index.resolve("model.py", "build", "p.make().width") == [code_ref("model.py", "Result.width")]
    assert index.resolve("model.py", "build", "p.make.width") == []


def test_control_flow_binders_do_not_leak_outer_same_named_parameters():
    index = SourceIndex(
        _sources(
            model="""
        class Params:
            width: float = 3
        p = Params()
        def build():
            for p in external():
                use(p.width)
        def context():
            with external() as p:
                use(p.width)
    """
        )
    )
    assert index.resolve("model.py", "build", "p.width") == []
    assert index.resolve("model.py", "context", "p.width") == []


def _all_pages(query, section: str):
    records = []
    offset = 0
    while True:
        result = query(section=section, limit=100, offset=offset)
        page = result["pages"][section]
        assert page["returned"] == len(result[section])
        assert page["omitted"] == page["total"] - page["returned"]
        records.extend(result[section])
        if page["next_offset"] is None:
            assert len(records) == page["total"]
            return records
        assert page["next_offset"] > offset
        offset = page["next_offset"]


def test_large_parameter_fanout_is_bounded_and_complete_evidence_is_pageable():
    source = "BASE = 3\ndef consume(value):\n    return value\ndef build():\n"
    source += "".join("    consume(BASE)\n" for _ in range(2332))
    index = SourceIndex({"fanout.py": source})
    owner = code_ref("fanout.py", "build")
    consumer = code_ref("fanout.py", "consume")
    def query(**kwargs):
        return index.follow_parameter(owner, "BASE", **kwargs)
    result = query()
    assert result["origins"][0]["expression"] == "3"
    assert "source" not in result["origins"][0]
    assert len(json.dumps(result)) < 40000
    assert result["pages"]["consumers"] == {
        "total": 4664,
        "offset": 0,
        "limit": 20,
        "returned": 20,
        "omitted": 4644,
        "next_offset": 20,
    }
    def filtered(**kwargs):
        return query(consumers=[consumer], **kwargs)
    consumers = _all_pages(filtered, "consumers")
    assert len(consumers) == 2332
    assert {entry["reference"] for entry in consumers} == {consumer}
    assert len({entry["line"] for entry in consumers}) == 2332
    assert query(consumers=[])["pages"]["consumers"]["total"] == 0
    evidence = _all_pages(filtered, "evidence")
    calls = [entry for entry in evidence if entry["section"] == "consumers"]
    assert len(calls) == 2332
    assert all(entry["call"]["arguments"][0]["expression"] == "BASE" for entry in calls)
    assert all(entry["source_revision"] == index.revision for entry in [result])


def test_derived_parameter_uses_direct_consumers_not_every_base_dimension_use():
    index = SourceIndex(
        {
            "model.py": "BASE = 3\nDERIVED = BASE * 2\ndef build():\n    return DERIVED\n"
            + "".join(f"def sibling_{i}():\n    return BASE\n" for i in range(100))
        }
    )
    result = index.follow_parameter(code_ref("model.py", "build"), "DERIVED")
    assert _references(result["dependencies"]) == {code_ref("model.py", "BASE")}
    assert _references(result["consumers"]) == {code_ref("model.py", "build")}
    assert result["pages"]["consumers"]["total"] == 1


def test_related_large_sibling_fanout_is_bounded_filtered_and_pageable(monkeypatch):
    source = "BASE = 3\ndef chosen():\n    return BASE\n"
    source += "".join(f"def sibling_{i:04d}():\n    return BASE\n" for i in range(1922))
    index = SourceIndex({"fanout.py": source})
    owner = code_ref("fanout.py", "chosen")
    original = index._closure
    closure_calls = []

    def capture(references):
        closure_calls.append(references)
        return original(references)

    monkeypatch.setattr(index, "_closure", capture)
    result = index.related(owner)
    assert len(closure_calls) == 1
    assert result["pages"]["shared_parameters"]["total"] == 1922
    assert result["pages"]["shared_parameters"]["omitted"] == 1902
    assert len(json.dumps(result)) < 40000
    selected = code_ref("fanout.py", "sibling_1921")
    filtered = index.related(owner, consumers=selected)
    assert _references(filtered["shared_parameters"]) == {selected}
    assert filtered["pages"]["shared_parameters"]["total"] == 1
    all_matches = _all_pages(lambda **kwargs: index.related(owner, **kwargs), "shared_parameters")
    assert len(all_matches) == len(_references(all_matches)) == 1922
    evidence = _all_pages(lambda **kwargs: index.related(owner, consumers=[selected], **kwargs), "evidence")
    shared = [entry for entry in evidence if entry["section"] == "shared_parameters"]
    assert len(shared) == 1 and shared[0]["shared"] == code_ref("fanout.py", "BASE")


def test_unresolved_boundaries_and_long_evidence_are_not_silently_lost():
    expression = " + ".join("external()" for _ in range(40))
    index = SourceIndex({"model.py": f"WIDTH = {expression}\ndef build():\n    return WIDTH\n"})
    owner = code_ref("model.py", "build")
    result = index.follow_parameter(owner, "WIDTH", limit=2)
    assert result["origins"][0]["expression_omitted_characters"] > 0
    assert result["pages"]["unresolved"]["total"] == 40
    assert result["pages"]["unresolved"]["omitted"] == 38
    boundaries = _all_pages(lambda **kwargs: index.follow_parameter(owner, "WIDTH", **kwargs), "unresolved")
    assert len(boundaries) == 40
    evidence = _all_pages(lambda **kwargs: index.follow_parameter(owner, "WIDTH", **kwargs), "evidence")
    declaration = next(entry["declaration"] for entry in evidence if "declaration" in entry)
    assert declaration["expression"] == expression
    assert sum(entry["section"] == "unresolved" for entry in evidence) == 40


@pytest.mark.parametrize(
    "options",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"offset": -1},
        {"offset": 1.5},
        {"consumers": [3]},
        {"section": "everything"},
    ],
)
def test_bounded_queries_reject_invalid_options(options):
    index = SourceIndex({"model.py": "def build(width=3):\n    return width\n"})
    reference = code_ref("model.py", "build")
    with pytest.raises(ValueError):
        index.follow_parameter(reference, "width", **options)
    with pytest.raises(ValueError):
        index.related(reference, **options)


def test_selected_consumer_and_caller_pages_do_not_expand_dependency_subgraphs(monkeypatch):
    index = SourceIndex(
        {
            "model.py": "BASE = 3\nDERIVED = BASE * 2\ndef helper():\n    return DERIVED\n"
            "def build():\n    return helper()\n"
        }
    )

    def forbidden(_references):
        raise AssertionError("this section does not need a transitive dependency closure")

    monkeypatch.setattr(index, "_closure", forbidden)
    helper = code_ref("model.py", "helper")
    consumers = index.follow_parameter(helper, "DERIVED", section="consumers")
    assert consumers["pages"]["consumers"]["total"] == 1
    callers = index.related(helper, section="callers")
    assert _references(callers["callers"]) == {code_ref("model.py", "build")}
