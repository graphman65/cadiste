from __future__ import annotations

import os
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from build123d import Box, Compound, Location

from cadiste.bambu_verify import compare_geometry, read_geometry, verify_import
from cadiste.three_mf import export_3mf


def _archive(tmp_path: Path) -> Path:
    first = Box(13.6, 6.3, 0.2).moved(Location((100, 20, 3)))
    first.label = "tiny shim"
    second = Box(2, 3, 4).moved(Location((10, 20, 30)))
    second.label = "other"
    output = tmp_path / "source.3mf"
    export_3mf(Compound(children=[first, second]), output)
    return output


def test_roundtrip_reader_observes_counts_tiny_dimensions_names_and_positions(tmp_path: Path):
    geometry = read_geometry(_archive(tmp_path))
    assert geometry["object_count"] == 2
    assert geometry["objects"][0]["name"] == "tiny shim"
    assert geometry["objects"][0]["dimensions"] == pytest.approx([13.6, 6.3, 0.2])
    assert geometry["objects"][0]["min"] == pytest.approx([93.2, 16.85, 2.9])


def test_native_reader_follows_nested_cross_archive_components_and_composed_transforms(tmp_path: Path):
    core = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    root = ET.fromstring(
        f'''<model xmlns="{core}"
        xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" unit="millimeter">
        <resources><object id="10" name="instance"><components>
        <component objectid="1" p:path="/3D/Objects/object.model" transform="0 1 0 -1 0 0 0 0 1 10 20 30"/>
        </components></object></resources><build>
        <item objectid="10" transform="1 0 0 0 1 0 0 0 1 5 0 0"/></build></model>'''
    )
    child = ET.fromstring(
        f'''<model xmlns="{core}" unit="millimeter"><resources><object id="1">
        <mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
        <vertex x="0" y="3" z="4"/></vertices>
        <triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources></model>'''
    )
    output = tmp_path / "native.3mf"
    with ZipFile(output, "w") as archive:
        archive.writestr("3D/3dmodel.model", ET.tostring(root))
        archive.writestr("3D/Objects/object.model", ET.tostring(child))
    obj = read_geometry(output)["objects"][0]
    assert obj["min"] == pytest.approx([12, 20, 30])
    assert obj["dimensions"] == pytest.approx([3, 2, 4])


def test_comparison_distinguishes_reordering_rescaling_repositioning_and_missing_objects(tmp_path: Path):
    import copy

    source = read_geometry(_archive(tmp_path))
    reordered = {"object_count": 2, "objects": list(reversed(source["objects"]))}
    assert compare_geometry(source, reordered)["status"] == "passed"
    moved = copy.deepcopy(source)
    moved["objects"][0]["min"][0] += 100
    result = compare_geometry(source, moved)
    assert result["import_confirmed"]
    assert not result["placement_match"]
    scaled = copy.deepcopy(source)
    scaled["objects"][0]["dimensions"][2] *= 25.4
    assert not compare_geometry(source, scaled)["import_confirmed"]
    missing = {"object_count": 1, "objects": source["objects"][:1]}
    assert not compare_geometry(source, missing)["object_counts_match"]


def test_missing_native_runtime_is_actionable_not_import_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = _archive(tmp_path)
    monkeypatch.setattr("cadiste.bambu_verify.shutil.which", lambda name: None)
    report = verify_import(source)
    assert report["status"] == "unavailable"
    assert not report["import_confirmed"]
    assert "Install" in report["action"]
    assert report["expected"]["object_count"] == 2


def test_native_launch_is_isolated_and_runtime_failure_does_not_mutate_source_or_user_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = _archive(tmp_path)
    contents = source.read_bytes()
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    preset = user_home / "filament.json"
    preset.write_text('{"preset":"keep"}')
    monkeypatch.setenv("HOME", str(user_home))
    seen = {}

    def unavailable(command, **kwargs):
        seen.update(command=command, **kwargs)
        assert Path(kwargs["env"]["HOME"]) != user_home
        assert Path(kwargs["env"]["HOME"]).is_dir()
        assert kwargs["env"]["XDG_CONFIG_HOME"] != str(user_home)
        assert "--datadir" in command
        assert "--arrange" in command
        assert command[command.index("--arrange") + 1] == "0"
        assert "--slice" not in command
        assert Path(command[-1]).read_bytes() == contents
        raise OSError("missing libwebkit2gtk")

    monkeypatch.setattr("cadiste.bambu_verify.subprocess.Popen", unavailable)
    report = verify_import(source, executable="/native/BambuStudio")
    assert report["status"] == "unavailable"
    assert "libwebkit2gtk" in report["reason"]
    assert not report["import_confirmed"]
    assert not Path(seen["cwd"]).exists()
    assert preset.read_text() == '{"preset":"keep"}'
    assert source.read_bytes() == contents


def test_zero_exit_without_saved_native_project_is_never_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class NoOutput:
        returncode = 0

        def communicate(self, timeout):
            return b"launcher started", None

    monkeypatch.setattr("cadiste.bambu_verify.subprocess.Popen", lambda *args, **kwargs: NoOutput())
    report = verify_import(_archive(tmp_path), executable="/native/BambuStudio")
    assert report["status"] == "unavailable"
    assert not report["import_confirmed"]
    assert "roundtrip" in report["reason"]


def test_timeout_terminates_isolated_process_group_and_reports_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    killed = []

    class Hung:
        pid = 12345
        calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("BambuStudio", timeout)
            return b"display unavailable", None

    monkeypatch.setattr("cadiste.bambu_verify.subprocess.Popen", lambda *args, **kwargs: Hung())
    monkeypatch.setattr("cadiste.bambu_verify.os.killpg", lambda *args: killed.append(args))
    report = verify_import(_archive(tmp_path), executable="/native/BambuStudio", timeout=0.1)
    assert killed[0][0] == 12345
    assert report["status"] == "unavailable"
    assert not report["import_confirmed"]
    assert "timed out" in report["reason"]


@pytest.mark.skipif(
    os.environ.get("CADISTE_TEST_BAMBU") != "1", reason="explicit optional installed-native-runtime integration"
)
def test_actual_installed_native_bambu_import_save_preserves_counts_and_tiny_dimensions(tmp_path: Path):
    report = verify_import(_archive(tmp_path), timeout=120)
    assert report["import_confirmed"], report
    assert report["object_counts_match"]
    assert report["dimensions_match"]
    assert report["native_output_sha256"]
