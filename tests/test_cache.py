from __future__ import annotations

import contextlib
import json
import os
import py_compile
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from build123d import import_brep

from cadiste.agent import Agent
from cadiste.cache import TraceCache
from cadiste.hub import Hub
from cadiste.server import Builds, Measurer, Project, _Handler


def _builder(outputs: list[bytes | None], shapes: bool = False):
    calls: list[int] = []

    def make(shapes_file: Path) -> bytes | None:
        calls.append(1)
        output = outputs[min(len(calls), len(outputs)) - 1]
        if shapes and output is not None:
            shapes_file.write_bytes(b"shapes of " + output)
        return output

    return make, calls


def test_a_trace_is_built_once_until_the_model_source_changes(tmp_path: Path):
    source = tmp_path / "model"
    source.mkdir()
    module = source / "part.py"
    module.write_text("WIDTH = 1\n")
    cache = TraceCache(tmp_path / "cache", [source], salt="")
    make, calls = _builder([b"first", b"second"])

    assert cache.get_or_make("trace", "Bracket", make)[:2] == (b"first", False)
    assert cache.get_or_make("trace", "Bracket", make)[:2] == (b"first", True)
    assert len(calls) == 1

    module.write_text("WIDTH = 22\n")  # an edit: another size
    assert cache.get_or_make("trace", "Bracket", make)[:2] == (b"second", False)
    # The entry made for the old source is gone, not left to pile up.
    assert len(list((tmp_path / "cache").glob("trace-Bracket-*.json"))) == 1


def test_an_edit_that_keeps_the_size_still_invalidates(tmp_path: Path):
    source = tmp_path / "model"
    source.mkdir()
    module = source / "part.py"
    module.write_text("WIDTH = 1\n")
    cache = TraceCache(tmp_path / "cache", [source], salt="")
    make, _ = _builder([b"first", b"second"])
    cache.get_or_make("trace", "Bracket", make)

    module.write_text("WIDTH = 2\n")
    stat = module.stat()
    os.utime(module, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert cache.get_or_make("trace", "Bracket", make)[:2] == (b"second", False)


def test_fresh_builds_again_and_a_failed_build_is_not_kept(tmp_path: Path):
    source = tmp_path / "model.py"
    source.write_text("")
    cache = TraceCache(tmp_path / "cache", [source], salt="")
    make, calls = _builder([None, b"built", b"rebuilt"])

    assert cache.get_or_make("trace", "Bracket", make) == (None, False, None)
    assert cache.get_or_make("trace", "Bracket", make)[:2] == (b"built", False)
    assert cache.get_or_make("trace", "Bracket", make, fresh=True)[:2] == (b"rebuilt", False)
    assert cache.get_or_make("trace", "Bracket", make)[:2] == (b"rebuilt", True)
    assert len(calls) == 3


def test_entries_are_separate_per_part_and_per_salt(tmp_path: Path):
    source = tmp_path / "model.py"
    source.write_text("")
    make, _ = _builder([b"a", b"b", b"c"])
    cache = TraceCache(tmp_path / "cache", [source], salt="env=1")

    assert cache.get_or_make("trace", "Bracket", make)[0] == b"a"
    assert cache.get_or_make("trace", "Bracket 2", make)[0] == b"b"
    other_environment = TraceCache(tmp_path / "cache", [source], salt="env=2")
    assert other_environment.get_or_make("trace", "Bracket", make)[:2] == (b"c", False)


def test_a_build_keeps_its_shapes_with_it_and_drops_them_with_it(tmp_path: Path):
    source = tmp_path / "model.py"
    source.write_text("")
    cache = TraceCache(tmp_path / "cache", [source], salt="")
    make, _ = _builder([b"first", b"second"], shapes=True)

    _, _, shapes = cache.get_or_make("trace", "Bracket", make)
    assert shapes is not None and shapes.read_bytes() == b"shapes of first"
    assert cache.get_or_make("trace", "Bracket", make)[1:] == (True, shapes)

    source.write_text("WIDTH = 2\n")
    _, _, rebuilt = cache.get_or_make("trace", "Bracket", make)
    assert rebuilt is not None and rebuilt.read_bytes() == b"shapes of second"
    assert not shapes.exists()  # the old build's shapes went with its JSON
    assert sorted(p.name for p in (tmp_path / "cache").iterdir()) == sorted([rebuilt.name, rebuilt.stem + ".json"])


def test_fresh_builds_get_distinct_identities_even_without_source_changes(tmp_path: Path):
    source = tmp_path / "model.py"
    source.write_text("")
    cache = TraceCache(tmp_path / "cache", [source], salt="")
    make, _ = _builder([b"first", b"second"], shapes=True)

    _, _, first = cache.get_or_make("trace", "Bracket", make)
    _, _, second = cache.get_or_make("trace", "Bracket", make, fresh=True)

    assert first is not None and second is not None and first.name != second.name
    assert not first.exists()
    assert second.read_bytes() == b"shapes of second"
    assert cache.latest("trace", "Bracket") == second.with_suffix(".json")
    assert cache.get_or_make("trace", "Bracket", make)[1:] == (True, second)


@contextlib.contextmanager
def _catalog_server(tmp_path: Path, project: Project) -> Iterator[tuple[str, Builds, Agent]]:
    cache = TraceCache(tmp_path / "cache", [tmp_path / "live_catalog"], "")
    builds = Builds(project, cache, always_fresh=False)
    hub = Hub()
    measurer = Measurer(cache.directory)
    agent = Agent(hub, builds, measurer, project, tmp_path)
    handler = type(
        "CatalogHandler",
        (_Handler,),
        {"project": project, "builds": builds, "measurer": measurer, "hub": hub, "agent": agent},
    )
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", builds, agent
        finally:
            server.shutdown()
            thread.join()


def _request(url: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, bytes]:
    request = Request(
        url + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urlopen(request, timeout=120) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


@pytest.fixture
def live_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package = tmp_path / "live_catalog"
    package.mkdir()
    (package / "__init__.py").write_text("")
    for module in ("alpha", "beta", "anchor"):
        (package / f"{module}.py").write_text(
            "from build123d import Box, BuildPart\n"
            "def piece():\n"
            "    with BuildPart() as part:\n"
            "        Box(1, 2, 3)\n"
            "    return part.part\n"
        )
    pythonpath = os.pathsep.join(filter(None, [str(tmp_path), os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("PYTHONPATH", pythonpath)
    catalog = package / "catalog.py"
    catalog.write_text(_catalog_source("Aaa", "alpha"))
    return catalog


def _catalog_source(name: str, module: str, convention: str = "mapping") -> str:
    definitions = f"{{'{name}': piece, 'Anchor': anchor}}"
    match convention:
        case "iterable":
            parts = f"PARTS = list({definitions})\n"
        case "callable":
            parts = f"def PARTS():\n    return {definitions}\n"
        case "callable_iterable":
            parts = f"def PARTS():\n    return list({definitions})\n"
        case _:
            parts = f"PARTS = {definitions}\n"
    return (
        "print('model import output is not JSON')\n"
        f"from .{module:<5} import piece\n"
        "from .anchor import piece as anchor\n" + parts + "def build(name):\n"
        "    entries = PARTS() if callable(PARTS) else PARTS\n"
        "    if name not in entries:\n"
        "        raise ValueError(name)\n"
        "    return piece() if name != 'Anchor' else anchor()\n"
        "def single(width):\n"
        "    from build123d import Box\n"
        "    return Box(float(width), 2, 3)\n"
    )


def test_running_server_refreshes_parts_folders_builds_exports_and_agent_catalog(live_catalog: Path, tmp_path: Path):
    project = Project("live_catalog.catalog:build", (), None, parts_target="live_catalog.catalog:PARTS")
    with _catalog_server(tmp_path, project) as (url, _, agent):
        status, body = _request(url, "/api/project")
        first = json.loads(body)
        assert status == 200 and first["parts"] == ["Aaa", "Anchor"]
        assert first["folders"]["Aaa"] == [{"name": "alpha", "file": False}]

        # Seed a valid timestamp-based pyc, then edit without changing size or even the exact mtime.
        py_compile.compile(str(live_catalog), doraise=True)
        stat = live_catalog.stat()
        live_catalog.write_text(_catalog_source("Bbb", "beta"))
        assert live_catalog.stat().st_size == stat.st_size
        os.utime(live_catalog, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        status, body = _request(url, "/api/project")
        current = json.loads(body)
        assert status == 200 and current["parts"] == ["Bbb", "Anchor"]
        assert current["folders"]["Bbb"] == [{"name": "beta", "file": False}]
        listing = agent.list_parts("").text
        assert "Bbb" in listing and "beta" in listing and "Aaa" not in listing
        status, body = _request(url, "/api/trace?part=Bbb")
        assert status == 200 and json.loads(body)["result"]["volume"] == pytest.approx(6)
        status, body = _request(url, "/api/file?path=beta")
        assert status == 200
        assert [part["name"] for part in json.loads(body)["scene"]["data"]["shapes"]["parts"]] == ["Bbb"]
        for path in ("/api/trace?part=Aaa", "/api/file?path=alpha"):
            assert _request(url, path)[0] == 404
        for query in ("kind=part&name=Bbb", "kind=file&path=beta"):
            status, body = _request(url, f"/api/export?format=brep&{query}")
            assert status == 200
            exported = tmp_path / "refreshed.brep"
            exported.write_bytes(body)
            shape = import_brep(exported)
            assert shape.is_valid
            assert sum(solid.volume for solid in shape.solids()) == pytest.approx(6)
        for query in ("kind=part&name=Aaa", "kind=file&path=alpha", "kind=file&path=beta&part=Aaa"):
            assert _request(url, f"/api/export?format=brep&{query}")[0] == 404

        # MCP catalog tools discover changes themselves, without an intervening browser request.
        live_catalog.write_text(_catalog_source("Ccc", "alpha"))
        listing = agent.list_parts("").text
        assert "Ccc" in listing and "alpha" in listing
        live_catalog.write_text(_catalog_source("Ddd", "beta"))
        navigation = agent.walk_components("")
        assert navigation.data is not None
        assert navigation.data["parts"][0]["part"] == "Ddd"
        assert navigation.data["folders"] == [
            {"path": "anchor", "parts": ["Anchor"]},
            {"path": "beta", "parts": ["Ddd"]},
        ]
        # Build validation also refreshes, without a catalog request.
        live_catalog.write_text(_catalog_source("Eee", "alpha"))
        assert _request(url, "/api/trace?part=Eee")[0] == 200
        assert _request(url, "/api/trace?part=Ddd")[0] == 404
        # A definition can move while its public part name stays the same.
        live_catalog.write_text(_catalog_source("Eee", "beta"))
        status, body = _request(url, "/api/project")
        moved = json.loads(body)
        assert status == 200 and moved["parts"] == ["Eee", "Anchor"]
        assert moved["folders"]["Eee"] == [{"name": "beta", "file": False}]
        assert _request(url, "/api/file?path=beta")[0] == 200
        assert _request(url, "/api/file?path=alpha")[0] == 404


@pytest.mark.parametrize("failure", ["import", "folders"])
def test_discovery_errors_are_explicit_and_never_publish_partial_catalogs(
    live_catalog: Path, tmp_path: Path, failure: str
):
    project = Project("live_catalog.catalog:build", (), None, parts_target="live_catalog.catalog:PARTS")
    with _catalog_server(tmp_path, project) as (url, _, agent):
        assert _request(url, "/api/project")[0] == 200
        broken = (
            "PARTS = {'Partial': None}\nraise RuntimeError('broken import')\n"
            if failure == "import"
            else "class Entry:\n"
            "    @property\n"
            "    def __dict__(self):\n"
            "        raise RuntimeError('broken folders')\n"
            "PARTS = {'Partial': Entry()}\n"
        )
        live_catalog.write_text(broken)
        for path in ("/api/project", "/api/trace?part=Aaa", "/api/export?kind=file&path=alpha"):
            status, body = _request(url, path)
            assert status == 500
            assert f"broken {failure}" in json.loads(body)["error"]
        with pytest.raises(ValueError):
            project.catalog()
        session_status, session_body = _request(url, "/api/agent/sessions", {})
        assert session_status == 200
        session = json.loads(session_body)["id"]
        for tool in ("list_parts", "walk_components"):
            status, body = _request(url, f"/api/agent/{session}/call/{tool}", {})
            assert status == 422 and f"broken {failure}" in json.loads(body)["error"]
        live_catalog.write_text(_catalog_source("Fresh", "beta"))
        listing = agent.list_parts("").text
        assert "Fresh" in listing and "beta" in listing
        assert project.catalog().names == ("Fresh", "Anchor")


@pytest.mark.parametrize("convention", ["mapping", "iterable", "callable", "callable_iterable"])
def test_parts_discovery_preserves_supported_conventions(live_catalog: Path, tmp_path: Path, convention: str):
    live_catalog.write_text(_catalog_source("First", "alpha", convention))
    project = Project("live_catalog.catalog:build", (), None, parts_target="live_catalog.catalog:PARTS")
    with _catalog_server(tmp_path, project) as (url, _, _):
        assert json.loads(_request(url, "/api/project")[1])["parts"] == ["First", "Anchor"]
        live_catalog.write_text(_catalog_source("Added", "beta", convention))
        status, body = _request(url, "/api/project")
        assert status == 200 and json.loads(body)["parts"] == ["Added", "Anchor"]
        if convention not in ("iterable", "callable_iterable"):
            assert json.loads(body)["folders"]["Added"] == [{"name": "beta", "file": False}]
        else:
            assert json.loads(body)["folders"] == {"Added": [], "Anchor": []}


def test_single_model_catalog_and_arguments_stay_fixed(live_catalog: Path, tmp_path: Path):
    project = Project("live_catalog.catalog:single", ("3",), None)
    with _catalog_server(tmp_path, project) as (url, _, agent):
        expected = ["live_catalog.catalog:single 3"]
        for _ in range(2):
            status, body = _request(url, "/api/project")
            assert status == 200
            assert json.loads(body)["parts"] == expected and json.loads(body)["folders"] == {}
        assert expected[0] in agent.list_parts("").text
        status, body = _request(url, "/api/trace?part=live_catalog.catalog%3Asingle%203")
        assert status == 200 and json.loads(body)["result"]["volume"] == pytest.approx(18)
        assert _request(url, "/api/export?kind=part&name=live_catalog.catalog%3Asingle%203&format=brep")[0] == 200
        assert _request(url, "/api/file?path=alpha")[0] == 404
        live_catalog.write_text("raise RuntimeError('not needed for catalog')\n")
        status, body = _request(url, "/api/project")
        assert status == 200 and json.loads(body)["parts"] == expected
