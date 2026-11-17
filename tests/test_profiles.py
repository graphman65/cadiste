from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from cadiste.profiles import PROFILE_NAME, Profile, ProfileError, create_starter
from cadiste.workspace import Workspace, WorkspaceError


def configuration(root: str = ".", **changes: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "name": "Example",
        "root": root,
        "interpreter": None,
        "target": "model:build",
        "parts": "model:PARTS",
        "args": [],
        "assemblies": {"Main": "model:assembly"},
        "simulations": {},
        "trace_env": {},
        **changes,
    }


def test_profile_round_trip_is_declarative_and_root_is_profile_relative(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "model.py").write_text("raise RuntimeError('configuration must never import me')\n")
    path = tmp_path / PROFILE_NAME
    path.write_text(json.dumps(configuration("project", trace_env={"BUILD_CACHE": "off"})))
    profile = Profile.load(path)
    assert profile.root == root
    assert profile.python == sys.executable
    assert profile.trace_env == {"BUILD_CACHE": "off"}
    assert profile.ros_command == ("ros2",)
    saved = tmp_path / "saved.json"
    profile.write(saved)
    assert Profile.load(saved) == profile
    with pytest.raises(ProfileError, match="never overwritten"):
        profile.write(saved)
    assert Profile.load(saved) == profile


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"version": True}, "version"),
        ({"target": "python model.py"}, "module:attribute"),
        ({"parts": "model.py"}, "module:attribute"),
        ({"args": "hello"}, "list of strings"),
        ({"args": ["bad\0argument"]}, "NUL"),
        ({"trace_env": {"BAD=KEY": "value"}}, "trace_env"),
        ({"trace_env": {"OK": 7}}, "trace_env"),
        ({"ros_command": []}, "ros_command"),
        ({"ros_command": "ros2"}, "ros_command"),
        ({"ros_command": None}, "ros_command"),
        ({"ros_command": [""]}, "ros_command"),
        ({"ros_command": ["ros2", 1]}, "ros_command"),
        ({"ros_command": ["ros2", "bad\0argument"]}, "ros_command"),
        ({"simulations": {"Missing": "model:sim"}}, "match assemblies"),
        ({"interpreter": "/does/not/exist/python"}, "executable file"),
        ({"command": "shell command"}, "unknown profile fields"),
    ],
)
def test_invalid_profiles_fail_without_code_execution(tmp_path: Path, changes: dict[str, Any], message: str):
    with pytest.raises(ProfileError, match=message):
        Profile.from_dict(configuration(**changes), base=tmp_path)


def test_profile_size_is_bounded(tmp_path: Path):
    path = tmp_path / PROFILE_NAME
    path.write_text(" " * 65537)
    with pytest.raises(ProfileError, match="64 KiB"):
        Profile.load(path)


def test_profile_preserves_ros_command_arguments_without_shell_parsing(tmp_path: Path):
    command = ["docker", "exec", "-i", "cadiste-ros2", "/ros_entrypoint.sh", "ros2"]
    profile = Profile.from_dict(configuration(ros_command=command), base=tmp_path)
    assert profile.ros_command == tuple(command)
    assert profile.to_dict()["ros_command"] == command
    path = tmp_path / PROFILE_NAME
    profile.write(path)
    assert Profile.load(path).ros_command == tuple(command)


def test_starter_has_real_catalog_geometry_and_assembly_without_overwrites(tmp_path: Path):
    from cadiste.assembly import capture_placements
    from cadiste.export import assembly_scene, export_trace
    from cadiste.provenance import Provenance
    from cadiste.trace import tracing

    directory = tmp_path / "new-project"
    path = create_starter(directory, name="Starter")
    profile = Profile.load(path)
    assert profile.parts == "model:PARTS"
    assert profile.assemblies == {"Starter": "model:assembly"}
    spec = importlib.util.spec_from_file_location("cadiste_profile_test_model", directory / "model.py")
    assert spec is not None and spec.loader is not None
    model = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(model)
    assert list(model.PARTS) == ["Block"]
    assert model.build("Block").volume == pytest.approx(6000)
    with tracing() as trace:
        built = model.build("Block")
    exported, _ = export_trace(
        trace,
        built,
        Provenance(trace).face_origins(built),
        root=directory,
        build_seconds=0,
    )
    assert exported["result"]["attributed"] == len(built.faces())
    with capture_placements(root=directory):
        assembly = model.assembly()
    assert len(assembly.children) == 2
    assert assembly.volume == pytest.approx(12000)
    assert [child.label for child in assembly.children] == ["Left block", "Right block"]
    assert tuple(assembly.children[1].location.position) == pytest.approx((35, 0, 0))
    scene, leaves = assembly_scene(assembly, root=directory)
    assert len({leaf.path for leaf in leaves}) == 2
    assert scene["ownership"]["matched"] == scene["ownership"]["total"] == 2
    assert list(scene["part_of"].values()) == ["Block", "Block"]
    placements = [record["placement"] for record in scene["instances"].values()]
    assert all(placement["method"] == "instance" for placement in placements)
    assert all(placement["site"]["file"] == "model.py" for placement in placements)
    assert any("Location((WIDTH + 5, 0, 0))" in placement["code"] for placement in placements)
    before = {entry.name: entry.read_bytes() for entry in directory.iterdir() if entry.is_file()}
    with pytest.raises(ProfileError, match="choose a new directory"):
        create_starter(directory, name="Do not overwrite")
    assert {entry.name: entry.read_bytes() for entry in directory.iterdir() if entry.is_file()} == before


def test_starter_rejects_bad_interpreter_before_creating_directory(tmp_path: Path):
    directory = tmp_path / "new-project"
    with pytest.raises(ProfileError, match="absolute executable"):
        create_starter(directory, name="Starter", interpreter="python")
    assert not directory.exists()


def test_workspace_browse_is_paginated_and_rejects_symlinks_and_escape(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    (root / "within").mkdir()
    (root / "alias").symlink_to(root / "within", target_is_directory=True)
    (root / "model.py").write_text("raise RuntimeError('not discovery')")
    (root / "profile.json").write_text("{}")
    workspace = Workspace(root)
    try:
        first = workspace.browse(limit=1)
        assert first["total"] == 2
        assert len(first["entries"]) == 1
        second = workspace.browse(offset=first["next_offset"], limit=1)
        assert second["next_offset"] is None
        assert first["entries"][0]["path"] != second["entries"][0]["path"]
        for path in ["../outside", str(outside), "escape", "alias"]:
            with pytest.raises(WorkspaceError):
                workspace.browse(path)
        with pytest.raises(WorkspaceError, match="limit"):
            workspace.browse(limit=101)
    finally:
        workspace.close()


def test_workspace_registers_existing_or_explicit_configuration_and_persists_recents(tmp_path: Path):
    (tmp_path / "existing").mkdir()
    workspace = Workspace(tmp_path)
    result = workspace.register(configuration=configuration("existing"), path="existing-profile.json")
    assert result["profile"]["root"] == str(tmp_path / "existing")
    assert result["profile_path"] == str(tmp_path / "existing-profile.json")
    with pytest.raises(ProfileError, match="never overwritten"):
        workspace.register(configuration=configuration("existing"), path="existing-profile.json")
    workspace.register(profile="existing-profile.json")
    assert len(workspace.snapshot()["projects"]) == 1
    workspace.close()
    restored = Workspace(tmp_path)
    try:
        assert restored.snapshot()["projects"][0]["profile_path"] == result["profile_path"]
    finally:
        restored.close()


def test_workspace_validates_original_profile_root_before_symlink_resolution(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    actual = root / "actual"
    actual.mkdir()
    (root / "alias").symlink_to(actual, target_is_directory=True)
    (root / "linked.json").write_text(json.dumps(configuration("alias")))
    workspace = Workspace(root)
    try:
        with pytest.raises(WorkspaceError, match="symlink"):
            workspace.register(profile="linked.json")
        with pytest.raises(WorkspaceError, match="symlink"):
            workspace.register(configuration=configuration("alias"), path="new.json")
        assert not (root / "new.json").exists()
        with pytest.raises(WorkspaceError, match="traversal"):
            workspace.register(configuration=configuration("actual/../actual"), path="other.json")
    finally:
        workspace.close()


def test_workspace_create_does_not_touch_existing_directories(tmp_path: Path):
    workspace = Workspace(tmp_path)
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "model.py").write_text("USER_CODE = True\n")
    try:
        with pytest.raises(ProfileError, match="choose a new directory"):
            workspace.create("existing", "Example")
        assert (existing / "model.py").read_text() == "USER_CODE = True\n"
        assert not (existing / PROFILE_NAME).exists()
        result = workspace.create("created", "Created")
        assert Profile.load(result["profile_path"]).name == "Created"
    finally:
        workspace.close()


# These tests use an actual independent child and actual ephemeral TCP bind, but avoid
# importing the heavyweight CAD runtime to isolate workspace handshake/lifecycle boundaries.
_READY_CHILD = """
import json, os, signal, socket, subprocess, sys, time
from pathlib import Path
ready, token, mode, port = sys.argv[1:]
if mode == 'error':
    print('ModuleNotFoundError: missing_project_dependency', flush=True)
    sys.exit(7)
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(('127.0.0.1', int(port)))
server.listen()
if mode == 'owned-resource':
    worker = (
        "import signal,sys; "
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(9)); "
        "print('resource-ready', flush=True); sys.stdin.buffer.read(1)"
    )
    resource = subprocess.Popen([sys.executable, '-c', worker], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    resource.stdout.readline()
    def shutdown(*_):
        try:
            resource.stdin.write(b'x')
            resource.stdin.flush()
        except BrokenPipeError:
            sys.exit(41)
        sys.exit(0 if resource.wait(timeout=2) == 0 else 42)
    signal.signal(signal.SIGTERM, shutdown)
if mode != 'timeout':
    temporary = Path(ready + '.tmp')
    temporary.write_text(json.dumps({'token': token if mode != 'wrong-token' else 'wrong', 'pid': os.getpid(),
                                     'url': f'http://127.0.0.1:{server.getsockname()[1]}'}))
    os.replace(temporary, ready)
print('ready child output', flush=True)
while True:
    time.sleep(1)
"""


def launch_factory(monkeypatch: pytest.MonkeyPatch, mode: str = "ready") -> None:
    popen = subprocess.Popen

    def launch(command: list[str], **kwargs: Any):
        # Exercise an actual child process; only replace its expensive model/server entrypoint.
        ready = command[command.index("--ready-file") + 1]
        token = next(
            argument.removeprefix("--ready-token=") for argument in command if argument.startswith("--ready-token=")
        )
        port = command[command.index("--port") + 1]
        return popen([sys.executable, "-c", _READY_CHILD, ready, token, mode, port], **kwargs)

    monkeypatch.setattr("cadiste.workspace.subprocess.Popen", launch)


def test_workspace_launch_uses_owned_process_separate_cwd_free_port_and_reuses_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    launch_factory(monkeypatch)
    workspace = Workspace(tmp_path, launcher_url="http://127.0.0.1:8123")
    registered = workspace.create("project", "Example")
    (tmp_path / "project" / "src").mkdir()
    original_cwd = Path.cwd()
    try:
        child = workspace.open(registered["profile_path"])
        assert child["status"] == "running"
        assert child["url"].startswith("http://127.0.0.1:")
        again = workspace.open(registered["profile_path"])
        assert again["id"] == child["id"]
        assert workspace.status(child["id"])["status"] == "running"
        assert Path.cwd() == original_cwd
        stopped = workspace.stop(child["id"])
        assert stopped["status"] == "stopped"
        assert stopped["returncode"] is not None
        assert stopped["url"] is None
        with pytest.raises(WorkspaceError, match="external processes"):
            workspace.stop("not-owned")
    finally:
        workspace.close()


@pytest.mark.parametrize(
    "mode, message",
    [
        ("error", "missing_project_dependency"),
        ("timeout", "did not become ready"),
        ("wrong-token", "readiness handshake"),
    ],
)
def test_workspace_startup_failures_include_actionable_diagnostics_and_kill_owned_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    message: str,
):
    launch_factory(monkeypatch, mode)
    workspace = Workspace(tmp_path, startup_timeout=0.3 if mode == "timeout" else 5)
    registered = workspace.create("project", "Example")
    try:
        child = workspace.open(registered["profile_path"])
        assert child["status"] == "failed"
        assert message in child["error"]
        assert child["returncode"] is not None
        assert child["url"] is None
    finally:
        workspace.close()


def test_workspace_allows_server_to_close_its_owned_resources_before_group_escalation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    launch_factory(monkeypatch, "owned-resource")
    workspace = Workspace(tmp_path)
    registered = workspace.create("project", "Resources")
    try:
        child = workspace.open(registered["profile_path"])
        assert child["status"] == "running"
        stopped = workspace.stop(child["id"])
        assert stopped["returncode"] == 0
    finally:
        workspace.close()


def test_workspace_restart_retains_project_identity_and_only_replaces_owned_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    launch_factory(monkeypatch)
    workspace = Workspace(tmp_path)
    first = workspace.create("first", "First")
    other = workspace.create("other", "Other")
    try:
        original = workspace.open(first["profile_path"])
        unrelated = workspace.open(other["profile_path"])
        replacement = workspace.restart(original["id"])
        assert replacement["status"] == "running"
        assert replacement["project_id"] == original["project_id"] == first["project_id"]
        assert replacement["id"] != original["id"]
        assert replacement["pid"] != original["pid"]
        assert replacement["url"] == original["url"]
        assert workspace.status(original["id"])["returncode"] is not None
        assert workspace.status(unrelated["id"])["status"] == "running"
        with pytest.raises(WorkspaceError, match="stale child identity"):
            workspace.restart(original["id"])
        with pytest.raises(WorkspaceError, match="external processes"):
            workspace.restart("not-owned")
        assert workspace.status(replacement["id"])["status"] == "running"
        registered = {item["profile_path"]: item for item in workspace.snapshot()["projects"]}
        assert registered[first["profile_path"]]["project_id"] == first["project_id"]
    finally:
        workspace.close()


def test_invalid_edited_profile_does_not_interrupt_owned_server_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    launch_factory(monkeypatch)
    workspace = Workspace(tmp_path)
    registered = workspace.create("project", "Example")
    try:
        child = workspace.open(registered["profile_path"])
        Path(registered["profile_path"]).write_text("{}")
        with pytest.raises(ValueError):
            workspace.restart(child["id"])
        assert workspace.status(child["id"])["status"] == "running"
    finally:
        workspace.close()


def test_workspace_reload_restores_parallel_projects_on_same_ports_and_respects_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    launch_factory(monkeypatch)
    launcher = "http://127.0.0.1:8123"
    workspace = Workspace(tmp_path, launcher_url=launcher)
    workspace.restore()
    try:
        first = workspace.open(workspace.create("first", "First")["profile_path"])
        second = workspace.open(workspace.create("second", "Second")["profile_path"])
        stopped = workspace.open(workspace.create("stopped", "Stopped")["profile_path"])
        workspace.stop(stopped["id"])
    finally:
        workspace.close()
    restored = Workspace(tmp_path, launcher_url=launcher)
    try:
        restored.restore()
        children = {child["project_id"]: child for child in restored.snapshot()["children"]}
        assert set(children) == {first["project_id"], second["project_id"]}
        for original in (first, second):
            replacement = children[original["project_id"]]
            assert replacement["status"] == "running"
            assert replacement["url"] == original["url"]
            assert replacement["pid"] != original["pid"]
    finally:
        restored.close()


def test_workspace_restore_reports_port_conflict_without_moving_project_or_stopping_listener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    launch_factory(monkeypatch)
    launcher = "http://127.0.0.1:8123"
    workspace = Workspace(tmp_path, launcher_url=launcher)
    workspace.restore()
    try:
        original = workspace.open(workspace.create("project", "Example")["profile_path"])
    finally:
        workspace.close()
    port = urlsplit(original["url"]).port
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen()
        restored = Workspace(tmp_path, launcher_url=launcher)
        try:
            restored.restore()
            child = restored.snapshot()["children"][0]
            assert child["status"] == "failed"
            assert "Address already in use" in child["error"]
            assert child["url"] is None
            assert listener.getsockname()[1] == port
        finally:
            restored.close()


def test_workspace_stop_of_old_generation_does_not_disable_replacement_restoration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    launch_factory(monkeypatch)
    launcher = "http://127.0.0.1:8123"
    workspace = Workspace(tmp_path, launcher_url=launcher)
    workspace.restore()
    try:
        original = workspace.open(workspace.create("project", "Example")["profile_path"])
        replacement = workspace.restart(original["id"])
        workspace.stop(original["id"])
    finally:
        workspace.close()
    restored = Workspace(tmp_path, launcher_url=launcher)
    try:
        restored.restore()
        child = restored.snapshot()["children"][0]
        assert child["status"] == "running"
        assert child["url"] == replacement["url"]
    finally:
        restored.close()


def test_project_server_accepts_readiness_tokens_starting_with_dash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from urllib.request import urlopen

    monkeypatch.setattr("cadiste.workspace.secrets.token_urlsafe", lambda _: "-readiness-token")
    workspace = Workspace(tmp_path)
    try:
        profile = workspace.create("project", "Dash token", interpreter=sys.executable)
        child = workspace.open(profile["profile_path"])
        assert child["status"] == "running", child
        with urlopen(child["url"] + "/api/project", timeout=10) as response:
            assert json.load(response)["project_root"] == str(tmp_path / "project")
    finally:
        workspace.close()


def test_workspace_discovers_named_profiles_recursively_without_executing_models(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    for directory, name in [("first", "First project"), ("collection/second", "Second project")]:
        project = root / directory
        project.mkdir(parents=True)
        Profile.from_dict(configuration(name=name), base=project).write(project / PROFILE_NAME)
        (project / "model.py").write_text("raise RuntimeError('discovery must not import this model')\n")
    invalid = root / "invalid"
    invalid.mkdir()
    (invalid / PROFILE_NAME).write_text("{}")
    for directory in [
        ".hidden/project", "node_modules/project", "__pycache__/project",
        "cadiste/tests/browser", "other/test/project", "other/fixtures/project", "other/__fixtures__/project",
    ]:
        project = root / directory
        project.mkdir(parents=True)
        Profile.from_dict(configuration(name="Ignored"), base=project).write(project / PROFILE_NAME)
    outside = tmp_path / "outside"
    outside.mkdir()
    Profile.from_dict(configuration(name="Outside"), base=outside).write(outside / PROFILE_NAME)
    (root / "escape").symlink_to(outside, target_is_directory=True)
    linked = root / "linked"
    linked.mkdir()
    (linked / PROFILE_NAME).symlink_to(outside / PROFILE_NAME)
    workspace = Workspace(root)
    try:
        projects = workspace.snapshot()["projects"]
        assert {item["profile"]["name"] for item in projects if "profile" in item} == {
            "First project", "Second project",
        }
        assert next(item for item in projects if item["profile_path"] == str(invalid / PROFILE_NAME))["error"]
        assert all(item["child"] is None for item in projects)
    finally:
        workspace.close()


def test_workspace_discovery_caches_names_and_files_for_thirty_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    clock = [100.0]
    monkeypatch.setattr("cadiste.workspace.time.monotonic", lambda: clock[0])
    first = tmp_path / "first"
    first.mkdir()
    profile = first / PROFILE_NAME
    Profile.from_dict(configuration(name="Original"), base=first).write(profile)
    workspace = Workspace(tmp_path)
    try:
        assert [item["profile"]["name"] for item in workspace.snapshot()["projects"]] == ["Original"]
        profile.write_text(json.dumps(configuration(name="Renamed")))
        second = tmp_path / "second"
        second.mkdir()
        Profile.from_dict(configuration(name="Added"), base=second).write(second / PROFILE_NAME)
        clock[0] = 129.9
        assert [item["profile"]["name"] for item in workspace.snapshot()["projects"]] == ["Original"]
        clock[0] = 130.0
        assert [item["profile"]["name"] for item in workspace.snapshot()["projects"]] == ["Added", "Renamed"]
        profile.unlink()
        clock[0] = 159.9
        assert [item["profile"]["name"] for item in workspace.snapshot()["projects"]] == ["Added"]
    finally:
        workspace.close()


def test_workspace_discovery_cache_keeps_project_session_status_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    launch_factory(monkeypatch)
    workspace = Workspace(tmp_path)
    try:
        profile = workspace.create("project", "Example")
        assert workspace.snapshot()["projects"][0]["child"] is None
        child = workspace.open(profile["profile_path"])
        running = workspace.snapshot()["projects"][0]["child"]
        assert running["id"] == child["id"]
        assert running["status"] == "running"
        workspace.stop(child["id"])
        assert workspace.snapshot()["projects"][0]["child"]["status"] == "stopped"
    finally:
        workspace.close()


def test_workspace_omits_missing_recent_profiles_after_loading_history(tmp_path: Path):
    workspace = Workspace(tmp_path)
    try:
        removed = workspace.register(configuration=configuration(name="Removed"), path="cadiste-child.json")
        kept = workspace.register(configuration=configuration(name="Kept"), path="custom-profile.json")
        Path(removed["profile_path"]).unlink()
        assert [item["profile_path"] for item in workspace.snapshot()["projects"]] == [kept["profile_path"]]
    finally:
        workspace.close()
    restored = Workspace(tmp_path)
    try:
        assert [item["profile_path"] for item in restored.snapshot()["projects"]] == [kept["profile_path"]]
    finally:
        restored.close()


def test_workspace_keeps_live_session_but_hides_stopped_session_with_missing_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    launch_factory(monkeypatch)
    workspace = Workspace(tmp_path)
    try:
        profile = workspace.create("project", "Live project")
        child = workspace.open(profile["profile_path"])
        Path(profile["profile_path"]).unlink()
        snapshot = workspace.snapshot()
        assert snapshot["children"][0]["status"] == "running"
        assert snapshot["projects"][0]["child"]["id"] == child["id"]
        workspace.stop(child["id"])
        assert workspace.snapshot()["projects"] == []
    finally:
        workspace.close()
