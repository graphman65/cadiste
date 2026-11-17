from __future__ import annotations

import contextlib
import hashlib
import json
import math
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from build123d import Align, Box, Cone, Cylinder, Location

from cadiste.agent import Agent
from cadiste.bambu_verify import read_geometry
from cadiste.cache import TraceCache
from cadiste.hub import Hub
from cadiste.measure import save
from cadiste.mesh import Leaf
from cadiste.profiles import Profile, ProfileError
from cadiste.server import Build, Builds, Measurer, Project, _Handler


@contextlib.contextmanager
def project_server(tmp_path, first_shape=None, name="Fixture"):
    target = {"kind": "assembly", "name": name}
    paths = [f"/{name}/first", f"/{name}/second"]
    cache = TraceCache(tmp_path / "cache", (), "workflow-api")
    cache.directory.mkdir(parents=True, exist_ok=True)
    shapes = cache.directory / "fixture.shapes"
    first = first_shape if first_shape is not None else Box(2, 4, 6).moved(Location((10, 20, 30)))
    second = Box(3, 5, 7).moved(Location((30, 20, 30)))
    save([Leaf(paths[0], [], [], [], first.wrapped), Leaf(paths[1], [], [], [], second.wrapped)], shapes)
    metadata = {
        "build_id": "fixture-build",
        "source_revision": "fixture-revision",
        "source_fingerprint": cache.fingerprint(),
        "scene": {
            "data": {"shapes": {"id": f"/{name}"}},
            "part_of": dict(zip(paths, ["First", "Second"], strict=True)),
        },
    }
    project = Project(
        "fixture:build",
        (),
        ("First", "Second"),
        {name: "fixture:assembly"},
        manufacturing={
            "orientations": {"flat": {"rotation": [90, 0, 0], "on_bed": True}},
            "groups": {"one": ["first"]},
        },
    )
    expected_target = target

    class RetainedBuilds(Builds):
        def get(self, target, fresh=False, **kwargs):
            if target != expected_target:
                raise LookupError("unknown fixture target")
            return Build(json.dumps(metadata).encode(), True, shapes, {})

        def latest(self, target, **kwargs):
            return self.get(target)

        def captured(self, target, build_id):
            if build_id != metadata["build_id"]:
                raise LookupError("unknown fixture generation")
            return self.get(target)

    builds = RetainedBuilds(project, cache, False)
    measurer = Measurer(cache.directory)
    builds.check = measurer.check
    builds.geometry_lock = measurer._lock
    hub = Hub()
    agent = Agent(hub, builds, measurer, project, tmp_path)
    handler = type(
        "WorkflowHandler",
        (_Handler,),
        {
            "project": project,
            "builds": builds,
            "measurer": measurer,
            "hub": hub,
            "agent": agent,
        },
    )
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", target, paths, builds
        finally:
            server.shutdown()
            worker.join()
            agent.close()


def post(url, path, payload):
    request = Request(url + path, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def test_malformed_geometry_sample_is_structured_and_does_not_disconnect_session(tmp_path: Path):
    with project_server(tmp_path) as (url, target, paths, _):
        session = post(url, "/api/agent/sessions", {"client": "regression"})["id"]
        with pytest.raises(HTTPError) as failure:
            post(
                url,
                f"/api/agent/{session}/call/check_geometry",
                {
                    "target": target,
                    "pairs": [paths],
                    "samples": [{"path": paths[0], "rotation": [0, 0, 0]}],
                },
            )
        error = json.load(failure.value)
        assert failure.value.code == 422
        assert error["code"] == "geometry_error"
        assert "translation" in error["error"]
        assert error["details"]["tool"] == "check_geometry"
        answer = post(url, f"/api/agent/{session}/call/check_geometry", {"target": target, "pairs": [paths]})
        assert answer["data"]["pairs"][0]["minimum_distance"] == pytest.approx(17.5)
        assert post(url, f"/api/agent/{session}/heartbeat", {})["alive"]


def test_http_baseline_retains_exact_displayed_build_and_release_invalidates_only_pin(tmp_path: Path):
    with project_server(tmp_path) as (url, target, _, builds):
        baseline = post(url, "/api/baselines", {"action": "pin", "targets": [{**target, "build_id": "fixture-build"}]})
        with urlopen(url + f"/api/baseline-scene?baseline={baseline['id']}&kind=assembly&name=Fixture") as response:
            assert json.load(response)["build_id"] == "fixture-build"
        post(url, "/api/baselines", {"action": "release", "id": baseline["id"]})
        with pytest.raises(HTTPError) as failure:
            urlopen(url + f"/api/baseline-scene?baseline={baseline['id']}&kind=assembly&name=Fixture")
        assert failure.value.code == 404
        current = builds.captured(target, "fixture-build")
        assert current.shapes is not None and current.shapes.is_file()


def test_http_export_and_preflight_use_same_selected_oriented_retained_geometry(tmp_path: Path):
    with project_server(tmp_path) as (url, target, _, _):
        options = {"target": target, "group": "one", "orientation": "flat"}
        preflight = post(url, "/api/manufacturing/preflight", options)
        artifact = post(url, "/api/export", {**options, "format": "3mf", "expected_revision": "fixture-revision"})
        geometry = read_geometry(Path(artifact["path"]))
        assert artifact["build_id"] == preflight["build_id"] == "fixture-build"
        assert geometry["object_count"] == preflight["object_count"] == 1
        assert geometry["objects"][0]["dimensions"] == pytest.approx([2, 6, 4])
        assert geometry["objects"][0]["min"][2] == pytest.approx(0)
        assert artifact["preflight"]["objects"][0]["bounds_mm"] == preflight["objects"][0]["bounds_mm"]
        assert not artifact["rebuilt_for_export"]
        with pytest.raises(HTTPError) as failure:
            post(url, "/api/export", {**options, "format": "3mf", "expected_revision": "other-revision"})
        assert json.load(failure.value)["code"] == "invalid_arguments"


def test_cncjs_download_uses_selected_retained_geometry_and_rejects_stale_revision(tmp_path: Path):
    machining = {
        "tool_diameter": 1, "cut_depth": 6, "step_down": 2, "safe_z": 3,
        "feed_rate": 300, "plunge_rate": 100, "spindle_speed": 12000,
    }
    with project_server(tmp_path) as (url, target, paths, builds):
        options = {
            "target": target, "machining": machining, "hidden": [paths[1]],
            "expected_revision": "fixture-revision",
        }
        artifact = post(url, "/api/cncjs", options)
        with urlopen(url + artifact["url"], timeout=30) as response:
            body = response.read()
            assert response.headers.get_content_type() == "application/octet-stream"
            assert ".nc" in response.headers["Content-Disposition"]
        assert hashlib.sha256(body).hexdigest() == artifact["sha256"]
        assert artifact["build_id"] == "fixture-build"
        assert artifact["source_revision"] == "fixture-revision"
        assert not artifact["rebuilt_for_export"] and not artifact["launched"]
        lines = body.decode().splitlines()
        # Center only the selected object, not the union with the hidden object.
        points = [
            {word[0]: float(word[1:]) for word in line.split() if word[0] in "XY"}
            for line in lines if line.startswith(("G0 ", "G1 "))
        ]
        xs = [point["X"] for point in points if "X" in point]
        ys = [point["Y"] for point in points if "Y" in point]
        assert min(xs) == pytest.approx(-1.5)
        assert max(xs) == pytest.approx(1.5)
        assert min(ys) == pytest.approx(-2.5)
        assert max(ys) == pytest.approx(2.5)
        assert artifact["cutting"]["model_center_xy_mm"] == pytest.approx([10, 20])
        assert any("Z-6" in line for line in lines)
        published = set((builds.cache.directory / "artifacts").iterdir())
        for invalid in (
            {**options, "expected_revision": "stale"},
            {**options, "hidden": paths},
            {**options, "machining": {**machining, "tool_diameter": 0}},
        ):
            with pytest.raises(HTTPError) as failure:
                post(url, "/api/cncjs", invalid)
            assert failure.value.code == 400
            assert json.load(failure.value)["code"] == "invalid_arguments"
        assert set((builds.cache.directory / "artifacts").iterdir()) == published


def test_cncjs_pocketed_part_download_preserves_through_hole_and_reports_incomplete_export(tmp_path: Path):
    blank = Box(40, 30, 6, align=(Align.MIN, Align.MIN, Align.MIN))
    pocket = Box(8, 8, 2, align=(Align.MIN, Align.MIN, Align.MIN)).moved(Location((10, 10, 4)))
    hole = Cylinder(3, 6, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((30, 15, 0)))
    part = (blank - pocket - hole).moved(Location((100, 200, 27)))
    machining = {
        "tool_diameter": 2, "cut_depth": 6, "step_down": 3, "safe_z": 5,
        "feed_rate": 300, "plunge_rate": 100, "spindle_speed": 12000,
    }
    with project_server(tmp_path, first_shape=part) as (url, target, paths, _):
        artifact = post(url, "/api/cncjs", {
            "target": target, "hidden": [paths[1]], "machining": machining,
            "expected_revision": "fixture-revision",
        })
        cutting = artifact["cutting"]
        assert cutting["partial_export"] and cutting["skipped_pocket_count"] == 1
        skipped, = cutting["skipped_pockets"]
        assert skipped["side"] == "top"
        assert skipped["depth_mm"] == pytest.approx(2)
        assert skipped["bounds_mm"]["min"] == pytest.approx([110, 210, 31])
        assert skipped["bounds_mm"]["max"] == pytest.approx([118, 218, 33])
        with urlopen(url + artifact["url"], timeout=30) as response:
            body = response.read()
        assert hashlib.sha256(body).hexdigest() == artifact["sha256"]
        assert artifact["build_id"] == "fixture-build" and not artifact["launched"]
        # Every actual tool-center move belongs to the true through hole or outside the stock.
        for line in body.decode().splitlines():
            if not line.startswith(("G0 X", "G1 X")):
                continue
            words = {word[0]: float(word[1:]) for word in line.split() if word[0] in "XY"}
            x, y = words["X"], words["Y"]
            assert (
                math.isclose(math.hypot(x - 10, y), 2, abs_tol=1e-8)
                or x <= -20 or x >= 20 or y <= -15 or y >= 15
            )
        assert cutting["hole_count"] == 1
        assert cutting["depths_mm"] == [-3, -6]


def test_cncjs_skips_small_lead_in_bore_without_cutting_nut_pocket_through(tmp_path: Path):
    blank = Box(40, 30, 10, align=(Align.MIN, Align.MIN, Align.MIN))
    small = Cylinder(1.7, 10, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((10, 15, 0)))
    rear_lead = Cone(1.9, 1.7, 0.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((10, 15, 0)))
    nut_pocket = Cylinder(4.5, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((10, 15, 7)))
    floor_lead = Cone(1.7, 1.9, 0.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((10, 15, 6.8)))
    large = Cylinder(5, 10, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((30, 15, 0)))
    part = blank - small - rear_lead - nut_pocket - floor_lead - large
    machining = {
        "tool_diameter": 7, "cut_depth": 10, "step_down": 1, "safe_z": 5,
        "feed_rate": 600, "plunge_rate": 100, "spindle_speed": 12000,
    }
    with project_server(tmp_path, first_shape=part, name="Plywood neck face é") as (url, target, paths, _):
        artifact = post(url, "/api/cncjs", {"target": target, "hidden": [paths[1]], "machining": machining})
        cutting = artifact["cutting"]
        assert cutting["partial_export"]
        assert cutting["hole_count"] == 1
        assert cutting["skipped_hole_count"] == 1
        skipped, = cutting["skipped_holes"]
        assert skipped["reason"]
        assert skipped["bounds_mm"]["min"][0] < 10 < skipped["bounds_mm"]["max"][0]
        with urlopen(url + artifact["url"], timeout=30) as response:
            body = response.read()
        # The 7 mm cutter must never enter the 3.4 mm bore or 9 mm blind nut pocket.
        for line in body.decode().splitlines():
            if not line.startswith(("G0 X", "G1 X")):
                continue
            words = {word[0]: float(word[1:]) for word in line.split() if word[0] in "XY"}
            x, y = words["X"], words["Y"]
            assert (
                math.isclose(math.hypot(x - 10, y), 1.5, abs_tol=1e-8)
                or x <= -20 or x >= 20 or y <= -15 or y >= 15
            )


@pytest.mark.parametrize("name", ["%2e%2e%2fprivate.nc", "%252e%252e%252fprivate.nc", "bad%5cname.nc"])
def test_encoded_artifact_paths_cannot_escape_artifact_directory(tmp_path: Path, name):
    with project_server(tmp_path) as (url, _, _, builds):
        (builds.cache.directory / "private.nc").write_text("outside published artifact directory")
        with pytest.raises(HTTPError) as failure:
            urlopen(url + "/api/artifacts/" + name, timeout=30)
        assert failure.value.code == 400


def test_profile_variant_replaces_mechanical_and_manufacturing_configuration(tmp_path: Path):
    original = {"fit": {"target": {"kind": "part", "name": "Key"}, "checks": []}}
    profile = Profile.from_dict(
        {
            "version": 1,
            "name": "Fixture",
            "root": ".",
            "target": "fixture:build",
            "check_sets": original,
            "manufacturing": {"groups": {"keys": ["Key"]}},
            "variants": {"small": {"check_sets": {}, "manufacturing": {"groups": {"keys": ["SmallKey"]}}}},
        },
        base=tmp_path,
    )
    variant = profile.effective_variant("small")
    assert profile.check_sets == original
    assert variant.check_sets == {}
    assert variant.manufacturing["groups"]["keys"] == ["SmallKey"]
    assert Profile.from_dict(profile.to_dict(), base=tmp_path) == profile
    invalid = profile.to_dict()
    invalid["check_sets"] = {"bad": {"checks": []}}
    with pytest.raises(ProfileError, match="explicit part"):
        Profile.from_dict(invalid, base=tmp_path)
