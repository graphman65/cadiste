"""Optional native Bambu Studio import/save evidence in a disposable settings directory.

Uses Bambu's documented CLI --export-3mf, not an archive-copy stand-in or a launch acknowledgement.
https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import posixpath
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

_CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
_PRODUCTION_PATH = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"
_UNITS = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}


def read_geometry(path: Path) -> dict[str, Any]:
    """Read actual instantiated 3MF object bounds, including nested production-extension objects."""
    with ZipFile(path) as archive:
        models = {name: ET.fromstring(archive.read(name)) for name in archive.namelist() if name.endswith(".model")}
        root_path = "3D/3dmodel.model"
        if "_rels/.rels" in archive.namelist():
            relationships = ET.fromstring(archive.read("_rels/.rels"))
            for relationship in relationships:
                if relationship.attrib.get("Type", "").endswith("/3dmodel"):
                    root_path = relationship.attrib["Target"].lstrip("/")
        names: dict[str, str] = {}
        if "Metadata/model_settings.config" in archive.namelist():
            settings = ET.fromstring(archive.read("Metadata/model_settings.config"))
            for obj in settings.findall("object"):
                for metadata in obj.findall("metadata"):
                    if metadata.attrib.get("key") == "name":
                        names[obj.attrib["id"]] = metadata.attrib["value"]
    resources = {
        name: {obj.attrib["id"]: obj for obj in model.findall(f"{_CORE}resources/{_CORE}object")}
        for name, model in models.items()
    }
    for model in models.values():
        if model.attrib.get("unit", "millimeter") not in _UNITS:
            raise ValueError("unsupported 3MF units")

    def transformed(points: list[tuple[float, ...]], element: ET.Element) -> list[tuple[float, ...]]:
        matrix = [float(value) for value in element.attrib.get("transform", "1 0 0 0 1 0 0 0 1 0 0 0").split()]
        if len(matrix) != 12 or not all(math.isfinite(value) for value in matrix):
            raise ValueError("invalid 3MF transform")
        return [
            tuple(sum(point[k] * matrix[k * 3 + axis] for k in range(3)) + matrix[9 + axis] for axis in range(3))
            for point in points
        ]

    def reference(model_path: str, element: ET.Element) -> str:
        value = element.attrib.get(_PRODUCTION_PATH)
        if not value:
            return model_path
        return (
            value.lstrip("/")
            if value.startswith("/")
            else posixpath.normpath(posixpath.join(posixpath.dirname(model_path), value))
        )

    def geometry(
        model_path: str, object_id: str, chain: frozenset[tuple[str, str]]
    ) -> tuple[list[tuple[float, ...]], int]:
        key = (model_path, object_id)
        if key in chain or len(chain) > 64:
            raise ValueError("cyclic or excessive 3MF component nesting")
        obj = resources[model_path][object_id]
        points = [
            tuple(float(vertex.attrib[axis]) for axis in ("x", "y", "z"))
            for vertex in obj.findall(f"{_CORE}mesh/{_CORE}vertices/{_CORE}vertex")
        ]
        if not all(math.isfinite(value) for point in points for value in point):
            raise ValueError("3MF contains non-finite vertices")
        triangle_count = len(obj.findall(f"{_CORE}mesh/{_CORE}triangles/{_CORE}triangle"))
        for component in obj.findall(f"{_CORE}components/{_CORE}component"):
            child_path = reference(model_path, component)
            if models[child_path].attrib.get("unit", "millimeter") != models[model_path].attrib.get(
                "unit", "millimeter"
            ):
                raise ValueError("cross-model mixed 3MF units are unsupported")
            child_points, child_count = geometry(child_path, component.attrib["objectid"], chain | {key})
            points.extend(transformed(child_points, component))
            triangle_count += child_count
        return points, triangle_count

    root = models[root_path]
    scale = _UNITS[root.attrib.get("unit", "millimeter")]
    objects = []
    for item in root.findall(f"{_CORE}build/{_CORE}item"):
        object_id = item.attrib["objectid"]
        model_path = reference(root_path, item)
        if models[model_path].attrib.get("unit", "millimeter") != root.attrib.get("unit", "millimeter"):
            raise ValueError("cross-model mixed 3MF units are unsupported")
        points, triangle_count = geometry(model_path, object_id, frozenset())
        points = transformed(points, item)
        if not points or not triangle_count:
            raise ValueError("3MF build object contains no mesh geometry")
        low = [min(point[axis] for point in points) * scale for axis in range(3)]
        high = [max(point[axis] for point in points) * scale for axis in range(3)]
        obj = resources[model_path][object_id]
        objects.append(
            {
                "name": names.get(object_id, obj.attrib.get("name")),
                "min": low,
                "max": high,
                "dimensions": [b - a for a, b in zip(low, high, strict=True)],
                "triangles": triangle_count,
            }
        )
    if not objects:
        raise ValueError("3MF has no build objects")
    return {"object_count": len(objects), "objects": objects, "units": "mm"}


def compare_geometry(before: dict[str, Any], after: dict[str, Any], tolerance: float = 0.01) -> dict[str, Any]:
    """Compare import object counts and dimensions, keeping repositioning distinct from rescaling."""
    remaining = list(enumerate(after["objects"]))
    matches = []
    for index, source in enumerate(before["objects"]):
        if not remaining:
            matches.append(
                {"source_index": index, "imported_index": None, "dimensions_match": False, "placement_match": False}
            )
            continue

        def score(
            candidate: tuple[int, dict[str, Any]],
            source: dict[str, Any] = source,
        ) -> tuple[float, float, bool]:
            obj = candidate[1]
            return (
                max(abs(a - b) for a, b in zip(source["dimensions"], obj["dimensions"], strict=True)),
                max(abs(a - b) for a, b in zip(source["min"], obj["min"], strict=True)),
                source["name"] != obj["name"],
            )

        chosen = min(remaining, key=score)
        remaining.remove(chosen)
        dimension_delta, position_delta, renamed = score(chosen)
        matches.append(
            {
                "source_index": index,
                "imported_index": chosen[0],
                "dimensions_match": dimension_delta <= tolerance,
                "placement_match": position_delta <= tolerance,
                "max_dimension_delta_mm": dimension_delta,
                "max_position_delta_mm": position_delta,
                "name_preserved": not renamed,
            }
        )
    counts = before["object_count"] == after["object_count"]
    dimensions = counts and all(item["dimensions_match"] for item in matches)
    placement = counts and all(item["placement_match"] for item in matches)
    return {
        "object_counts_match": counts,
        "dimensions_match": dimensions,
        "placement_match": placement,
        "matches": matches,
        "tolerance_mm": tolerance,
        "import_confirmed": counts and dimensions,
        "status": "passed" if dimensions and placement else "changed",
    }


def verify_import(
    path: Path | str, *, executable: str | None = None, timeout: float = 60, tolerance: float = 0.01
) -> dict[str, Any]:
    """Actually load/save via native Bambu, using fresh HOME/XDG/datadir; never alter user presets.

    A successful process exit alone is not import evidence. Only a parseable native output compared to
    the input establishes counts/dimensions. This is a CLI import, not proof of GUI or slicing behavior.
    """
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0 or timeout > 600:
        raise ValueError("Bambu verification timeout must be greater than 0 and at most 600 seconds")
    if isinstance(tolerance, bool) or not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("Bambu verification tolerance must be finite and nonnegative")
    path = Path(path).resolve()
    before = read_geometry(path)
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    executable = executable or next(
        (found for name in ("bambustudio", "bambu-studio", "BambuStudio") if (found := shutil.which(name))), None
    )
    base: dict[str, Any] = {
        "status": "unavailable",
        "import_confirmed": False,
        "method": "native_cli_load_save_roundtrip",
        "user_settings": "isolated_disposable_home_xdg_datadir",
        "source_sha256": source_hash,
        "expected": before,
        "limitations": ["Native CLI roundtrip does not establish GUI import behavior or slicer printability."],
    }
    if executable is None:
        return {
            **base,
            "reason": "Bambu Studio native runtime was not found on PATH.",
            "action": "Install a native Bambu Studio build supporting --export-3mf, then retry verify_import=true.",
        }
    with tempfile.TemporaryDirectory(prefix="cadiste-bambu-verify-") as temporary:
        directory = Path(temporary)
        home, config, data, cache, runtime = (
            directory / name for name in ("home", "config", "data", "cache", "runtime")
        )
        for folder in (home, config, data, cache, runtime):
            folder.mkdir(mode=0o700)
        source = directory / "input.3mf"
        shutil.copyfile(path, source)
        output = directory / "roundtrip.3mf"
        environment = dict(os.environ)
        environment.update(
            HOME=str(home),
            XDG_CONFIG_HOME=str(config),
            XDG_DATA_HOME=str(data),
            XDG_CACHE_HOME=str(cache),
            XDG_RUNTIME_DIR=str(runtime),
            APPIMAGE_EXTRACT_AND_RUN="1",
        )
        # Do not attach an optional GUI bootstrap to the user's D-Bus session.
        environment.pop("DBUS_SESSION_BUS_ADDRESS", None)
        command = [
            executable,
            "--datadir",
            str(config / "BambuStudio"),
            "--arrange",
            "0",
            "--outputdir",
            str(directory),
            "--export-3mf",
            output.name,
            str(source),
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                log, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # The child may exit between timeout and kill; always collect and close its pipes.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                log, _ = process.communicate()
                return {
                    **base,
                    "reason": "Native Bambu roundtrip timed out; its isolated process group was terminated.",
                    "action": (
                        "Install the native runtime's display/OpenGL dependencies "
                        "or use a supported headless build, then retry."
                    ),
                    "log": log.decode(errors="replace")[-12000:],
                }
        except OSError as error:
            return {
                **base,
                "reason": f"Native Bambu runtime could not start: {error}",
                "action": "Check the executable and install its shared-library/AppImage dependencies, then retry.",
            }
        log_text = log.decode(errors="replace")[-12000:]
        if process.returncode != 0 or not output.is_file():
            return {
                **base,
                "reason": "Native Bambu did not complete a load/save roundtrip.",
                "exit_code": process.returncode,
                "action": (
                    "Inspect the native log; install missing display/OpenGL/shared-library dependencies "
                    "or a Bambu build supporting --export-3mf. No import is confirmed."
                ),
                "log": log_text,
            }
        try:
            after = read_geometry(output)
        except (ValueError, KeyError, BadZipFile, ET.ParseError) as error:
            return {
                **base,
                "status": "failed",
                "reason": f"Native output is not a readable geometry project: {error}",
                "log": log_text,
            }
        comparison = compare_geometry(before, after, tolerance)
        return {
            **base,
            **comparison,
            "imported": after,
            "native_exit_code": process.returncode,
            "native_output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "log": log_text,
        }
