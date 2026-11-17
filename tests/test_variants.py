from __future__ import annotations

import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import pytest
from build123d import import_brep

from cadiste.agent import Agent
from cadiste.cache import TraceCache
from cadiste.cli import main
from cadiste.context import leaves, owning_part
from cadiste.hub import Hub
from cadiste.profiles import Profile, ProfileError
from cadiste.project import source_roots
from cadiste.server import Builds, Measurer, Project, _Handler, _ShapesGoneError, export_artifact


def configuration(**changes: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "name": "Variants",
        "root": ".",
        "target": "variant_adult:build",
        "parts": "variant_adult:PARTS",
        "assemblies": {"instrument/Main": "variant_adult:assembly"},
        "simulations": {"instrument/Main": "variant_adult:simulation"},
        "trace_env": {"SIZE": "adult"},
        "variants": {
            "adult": {"label": "Adult"},
            "child": {
                "label": "Child",
                "target": "variant_child:build",
                "parts": "variant_child:PARTS",
                "assemblies": {"instrument/Main": "variant_child:assembly"},
                "simulations": {"instrument/Main": "variant_child:simulation"},
                "trace_env": {},
            },
        },
        **changes,
    }


def test_profile_variant_inheritance_replaces_mappings_and_round_trips(tmp_path: Path):
    profile = Profile.from_dict(configuration(), base=tmp_path)
    assert profile.default_variant == "adult"
    adult, child = profile.effective_variant("adult"), profile.effective_variant("child")
    assert adult.target == "variant_adult:build" and adult.trace_env == {"SIZE": "adult"}
    assert child.target == "variant_child:build" and child.trace_env == {}
    assert child.assemblies == {"instrument/Main": "variant_child:assembly"}
    assert child.root == adult.root == tmp_path
    assert not child.variants and child.default_variant is None
    assert Profile.from_dict(profile.to_dict(), base=tmp_path) == profile


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"variants": []}, "variants must be an object"),
        ({"variants": {"bad/id": {}}}, "invalid variant id"),
        ({"variants": {"child": []}}, "must be an object"),
        ({"variants": {"child": {"root": "."}}}, "unknown variant fields"),
        ({"variants": {"child": {"interpreter": None}}}, "unknown variant fields"),
        ({"variants": {"child": {"variants": {}}}}, "unknown variant fields"),
        ({"variants": {"child": {"label": ""}}}, "label"),
        ({"variants": {"child": {"target": "bad"}}}, "module:attribute"),
        ({"variants": {"child": {"args": "bad"}}}, "list of strings"),
        ({"variants": {"child": {"assemblies": {}}}}, "match assemblies"),
        ({"variants": {"child": {"trace_env": {"bad=key": "1"}}}}, "trace_env"),
        ({"default_variant": "missing"}, "declared variant"),
        ({"variants": {}, "default_variant": "child"}, "declared variant"),
    ],
)
def test_invalid_variant_profiles_are_rejected(tmp_path: Path, changes: dict[str, Any], message: str):
    with pytest.raises(ProfileError, match=message):
        Profile.from_dict(configuration(**changes), base=tmp_path)


@pytest.fixture
def models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(tmp_path), *filter(None, [os.environ.get("PYTHONPATH")])]))
    for identifier, width in (("adult", 8), ("child", 3)):
        (tmp_path / f"variant_{identifier}.py").write_text(
            "from build123d import Box, BuildPart, Compound\n"
            "from cadiste.assembly import instance\n"
            "from cadiste.simulation import Simulation, Link\n"
            f"WIDTH = {width}\n"
            "def build(name):\n"
            "    assert name == 'set/Key'\n"
            "    with BuildPart() as part:\n"
            "        Box(WIDTH, 2, 1)\n"
            "    return part.part\n"
            "PARTS = {'set/Key': build}\n"
            "def assembly():\n"
            "    return Compound(label='Controller', children=[instance(build('set/Key'), part='set/Key', label='Key')])\n"
            "def simulation():\n"
            f"    return Simulation('{identifier}', (Link('body', build('set/Key')),), ())\n"
        )
    yield tmp_path
    for name in ("variant_adult", "variant_child"):
        sys.modules.pop(name, None)


def composite(child_env: dict[str, str] | None = None) -> Project:
    variants = {
        identifier: Project(
            f"variant_{identifier}:build",
            (),
            ("set/Key",),
            {"instrument/Main": f"variant_{identifier}:assembly"},
            trace_env=child_env if identifier == "child" else None,
            folders={"set/Key": [{"name": "pieces", "file": False}, {"name": "local", "file": True}]},
            simulations={"instrument/Main": f"variant_{identifier}:simulation"},
        )
        for identifier in ("adult", "child")
    }
    return Project(
        "variant_adult:build", (), None,
        variants=variants,
        variant_labels={"adult": "Adult", "child": "Child"},
        default_variant="child",
    )


def test_variant_builds_exports_owners_and_captures_are_separate(models: Path):
    project = composite()
    cache = TraceCache(models / "cache", source_roots(project.source_targets()), json.dumps(project.build_bindings(), sort_keys=True))
    builds = Builds(project, cache, always_fresh=False)
    captured = {}
    assembly_paths = []
    for identifier, volume in (("adult", 16), ("child", 6)):
        target = {"kind": "part", "name": f"{identifier}/set/Key"}
        build = builds.get(target, False)
        assert build.body is not None, build.failure
        trace = json.loads(build.body)
        assert trace["result"]["volume"] == pytest.approx(volume)
        captured[identifier] = trace["build_id"]
        assert builds.get(target, False).cached
        assert builds.captured(target, trace["build_id"]).body == build.body
        for export_target, only in (
            (target, None),
            ({"kind": "assembly", "name": f"{identifier}/instrument/Main"}, None),
            ({"kind": "file", "path": f"{identifier}/pieces/local"}, [f"{identifier}/set/Key"]),
        ):
            artifact = export_artifact(builds, export_target, "brep", only=only, preflight=False)
            file = Path(artifact["path"])
            assert sum(solid.volume for solid in import_brep(file).solids()) == pytest.approx(volume)
        for kind, key, local in (("assembly", "name", "instrument/Main"), ("file", "path", "pieces/local")):
            built = builds.get({"kind": kind, key: f"{identifier}/{local}"}, False)
            assert built.body is not None, built.failure
            data = json.loads(built.body)
            members = leaves(data["scene"]["data"]["shapes"])
            assert len(members) == 1
            path = members[0]["id"]
            assert owning_part(data, path, set(project.names)) == f"{identifier}/set/Key"
            assert data["scene"]["instances"][path]["owner"] == f"{identifier}/set/Key"
            if kind == "assembly":
                assembly_paths.append(path)
            else:
                assert members[0]["name"] == f"{identifier}/set/Key"
        assert builds.control_config({"kind": "assembly", "name": f"{identifier}/instrument/Main"})["model"] == identifier
        assert builds.simulation_scene({"kind": "assembly", "name": f"{identifier}/instrument/Main"})["model"] == identifier
    assert assembly_paths[0] == assembly_paths[1]
    assert captured["adult"] != captured["child"]
    with pytest.raises(_ShapesGoneError):
        builds.captured({"kind": "part", "name": "child/set/Key"}, captured["adult"])
    with pytest.raises(LookupError):
        export_artifact(
            builds, {"kind": "file", "path": "child/pieces/local"}, "brep", only=["adult/set/Key"], preflight=False,
        )


def test_variant_catalog_agent_bindings_and_source_invalidation(models: Path):
    project = composite()
    assert project.names == ["adult/set/Key", "child/set/Key"]
    assert project.folders["child/set/Key"] == [
        {"name": "child", "file": False},
        {"name": "pieces", "file": False},
        {"name": "local", "file": True},
    ]
    roots = source_roots(project.source_targets())
    assert set(roots) == {models / "variant_adult.py", models / "variant_child.py"}
    cache = TraceCache(models / "cache", roots, json.dumps(project.build_bindings(), sort_keys=True))
    builds = Builds(project, cache, always_fresh=False)
    assert builds.control_config({"kind": "assembly"})["model"] == "child"
    agent = Agent(Hub(), builds, Measurer(cache.directory), project, models)
    try:
        catalog = agent.walk_components("").data
        assert catalog is not None
        child = next(part for part in catalog["parts"] if part["part"] == "child/set/Key")
        assert child["entry_target"] == "variant_child:build"
        assert child["arguments"] == ["set/Key"]
        navigation = agent.walk_components("", part="child/set/Key").data
        assert navigation is not None and navigation["part"] == "child/set/Key"
        assert "variant_child.py" in json.dumps(navigation)
        assert "variant_adult.py" not in json.dumps(navigation)
    finally:
        agent.close()
    before = cache.fingerprint()
    with (models / "variant_child.py").open("a") as file:
        file.write("\nCHILD_EDIT = 1\n")
    assert cache.fingerprint() != before
    changed = composite({"BUILD_CACHE": "off"})
    assert changed.build_bindings() != project.build_bindings()


def test_cli_variant_sets_default_without_removing_variants(models: Path, monkeypatch: pytest.MonkeyPatch):
    import cadiste.server as server

    path = models / "cadiste.json"
    path.write_text(json.dumps(configuration()))
    received = []
    monkeypatch.setattr(server, "serve", lambda project, *args, **kwargs: received.append(project))
    main(["serve", "--profile", str(path), "--variant", "child", "--no-cache"])
    project = received[0]
    assert project.default_variant == "child"
    assert project.names == ["adult/set/Key", "child/set/Key"]
    assert project.variants == ({"id": "adult", "label": "Adult"}, {"id": "child", "label": "Child"})
    assert next(iter(project.assemblies)) == "child/instrument/Main"
    with pytest.raises(SystemExit, match="unknown variant"):
        main(["serve", "--profile", str(path), "--variant", "missing"])
    with pytest.raises(SystemExit, match="requires a profile"):
        main(["serve", "variant_adult:build", "--variant", "child"])


def test_project_api_exposes_declared_variants_and_namespaced_folders(models: Path):
    project = composite()
    builds = Builds(project, TraceCache(models / "cache", [], ""), always_fresh=False)
    handler = type("VariantHandler", (_Handler,), {"project": project, "builds": builds})
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/project", timeout=10) as response:
                data = json.load(response)
            assert data["variants"] == [{"id": "adult", "label": "Adult"}, {"id": "child", "label": "Child"}]
            assert data["default_variant"] == "child"
            assert data["parts"] == ["adult/set/Key", "child/set/Key"]
            assert data["assemblies"] == ["child/instrument/Main", "adult/instrument/Main"]
            assert data["folders"]["child/set/Key"][0] == {"name": "child", "file": False}
        finally:
            server.shutdown()
            thread.join()


def test_cli_preserves_variant_across_interpreter_reexec(models: Path, monkeypatch: pytest.MonkeyPatch):
    interpreter = models / "other-python"
    interpreter.symlink_to(sys.executable)
    path = models / "cadiste.json"
    path.write_text(json.dumps(configuration(interpreter=str(interpreter))))
    commands = []

    def reexec(executable, command, environment):
        commands.append((executable, command))
        raise RuntimeError("reexec intercepted")

    monkeypatch.setattr(os, "execve", reexec)
    with pytest.raises(RuntimeError, match="reexec intercepted"):
        main(["serve", "--profile", str(path), "--variant", "child"])
    executable, command = commands[0]
    assert executable == str(interpreter)
    assert command[command.index("--variant") + 1] == "child"
