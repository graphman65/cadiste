"""Declarative, non-executable project configuration shared by the CLI and workspace."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROFILE_NAME = "cadiste.json"
_MAX_PROFILE_BYTES = 65536
_TARGET = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", re.ASCII)
_ENVIRONMENT_KEY = re.compile(r"[A-Za-z_]\w*", re.ASCII)
_VARIANT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*", re.ASCII)
_VARIANT_FIELDS = {
    "label", "target", "args", "parts", "assemblies", "simulations", "trace_env", "check_sets", "manufacturing",
}


class ProfileError(ValueError):
    """Invalid configuration, without importing or executing the project."""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ProfileError(f"{label} must be a nonempty string without NUL characters")
    return value


def _target(value: Any, label: str) -> str:
    value = _text(value, label)
    if not _TARGET.fullmatch(value):
        raise ProfileError(f"{label} must be module:attribute (not a command or file path)")
    return value


def _targets(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ProfileError(f"{label} must be an object mapping names to module:attribute targets")
    return {_text(name, f"{label} name"): _target(target, f"{label}.{name}") for name, target in value.items()}


@dataclass(frozen=True)
class Profile:
    name: str
    root: Path
    target: str
    interpreter: str | None = None
    args: tuple[str, ...] = ()
    parts: str | None = None
    assemblies: dict[str, str] = field(default_factory=dict)
    simulations: dict[str, str] = field(default_factory=dict)
    trace_env: dict[str, str] = field(default_factory=dict)
    ros_command: tuple[str, ...] = ("ros2",)
    variants: dict[str, dict[str, Any]] = field(default_factory=dict)
    default_variant: str | None = None
    check_sets: dict[str, dict[str, Any]] = field(default_factory=dict)
    manufacturing: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any, *, base: Path) -> Profile:
        if not isinstance(data, dict):
            raise ProfileError("profile must be a JSON object")
        allowed = {
            "version",
            "name",
            "root",
            "target",
            "interpreter",
            "args",
            "parts",
            "assemblies",
            "simulations",
            "trace_env",
            "ros_command",
            "variants",
            "default_variant",
            "check_sets",
            "manufacturing",
        }
        unknown = data.keys() - allowed
        if unknown:
            raise ProfileError(f"unknown profile fields: {', '.join(sorted(unknown))}")
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ProfileError("profile version must be 1")
        name = _text(data.get("name"), "name")
        root_value = _text(data.get("root"), "root")
        root = (base / root_value).resolve()
        if not root.is_dir():
            raise ProfileError(f"project root is not an existing directory: {root}")
        target = _target(data.get("target"), "target")
        interpreter = data.get("interpreter")
        if interpreter is not None:
            interpreter_path = Path(_text(interpreter, "interpreter"))
            if not interpreter_path.is_absolute():
                interpreter_path = root / interpreter_path
            interpreter_path = interpreter_path.absolute()
            if not interpreter_path.is_file() or not os.access(interpreter_path, os.X_OK):
                raise ProfileError(f"interpreter is not an executable file: {interpreter_path}")
            interpreter = str(interpreter_path)
        args = data.get("args", [])
        if not isinstance(args, list) or any(not isinstance(arg, str) or "\0" in arg for arg in args):
            raise ProfileError("args must be a list of strings without NUL characters")
        parts = data.get("parts")
        if parts is not None:
            parts = _target(parts, "parts")
        assemblies = _targets(data.get("assemblies", {}), "assemblies")
        simulations = _targets(data.get("simulations", {}), "simulations")
        if unknown_simulations := simulations.keys() - assemblies.keys():
            raise ProfileError(f"simulation names must match assemblies: {', '.join(sorted(unknown_simulations))}")
        trace_env = data.get("trace_env", {})
        if not isinstance(trace_env, dict) or any(
            not isinstance(key, str)
            or not _ENVIRONMENT_KEY.fullmatch(key)
            or not isinstance(value, str)
            or "\0" in value
            for key, value in trace_env.items()
        ):
            raise ProfileError("trace_env must map environment variable names to strings without NUL characters")
        ros_command = data.get("ros_command", ["ros2"])
        if (
            not isinstance(ros_command, (list, tuple))
            or not ros_command
            or any(not isinstance(argument, str) or "\0" in argument for argument in ros_command)
            or not ros_command[0].strip()
        ):
            raise ProfileError("ros_command must be a nonempty argument list with an executable and no NUL characters")
        check_sets = data.get("check_sets", {})
        if not isinstance(check_sets, dict) or any(
            not isinstance(name, str) or not name.strip() or not isinstance(specification, dict)
            for name, specification in check_sets.items()
        ):
            raise ProfileError("check_sets must map nonempty names to mechanical check specifications")
        for name, specification in check_sets.items():
            target_specification = specification.get("target")
            if not isinstance(target_specification, dict) or target_specification.get("kind") not in {
                "part", "assembly", "file",
            }:
                raise ProfileError(f"check_sets.{name}.target must be an explicit part, assembly or file target")
            target_key = "path" if target_specification["kind"] == "file" else "name"
            _text(target_specification.get(target_key), f"check_sets.{name}.target.{target_key}")
            for section in ("checks", "assertions"):
                entries = specification.get(section, [])
                if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
                    raise ProfileError(f"check_sets.{name}.{section} must be a list of explicit requests")
        manufacturing = data.get("manufacturing", {})
        if not isinstance(manufacturing, dict):
            raise ProfileError("manufacturing must be an object")
        variants = data.get("variants", {})
        if not isinstance(variants, dict):
            raise ProfileError("variants must be an object mapping ids to overrides")
        for identifier, overrides in variants.items():
            if not isinstance(identifier, str) or not _VARIANT_ID.fullmatch(identifier):
                raise ProfileError(f"invalid variant id: {identifier!r}")
            if not isinstance(overrides, dict):
                raise ProfileError(f"variant {identifier!r} must be an object")
            if unknown := overrides.keys() - _VARIANT_FIELDS:
                raise ProfileError(f"unknown variant fields: {', '.join(sorted(unknown))}")
            _text(overrides.get("label", identifier), f"variants.{identifier}.label")
            effective = {key: value for key, value in data.items() if key not in {"variants", "default_variant"}}
            effective.update({key: value for key, value in overrides.items() if key != "label"})
            cls.from_dict(effective, base=base)
        default_variant = data.get("default_variant")
        if default_variant is None and variants:
            default_variant = next(iter(variants))
        if default_variant is not None and (
            not isinstance(default_variant, str) or default_variant not in variants
        ):
            raise ProfileError("default_variant must name a declared variant")
        return cls(
            name,
            root,
            target,
            interpreter,
            tuple(args),
            parts,
            assemblies,
            simulations,
            dict(trace_env),
            tuple(ros_command),
            {identifier: dict(overrides) for identifier, overrides in variants.items()},
            default_variant,
            {name: dict(specification) for name, specification in check_sets.items()},
            dict(manufacturing),
        )

    @classmethod
    def load(cls, path: Path | str) -> Profile:
        path = Path(path)
        try:
            with path.open("rb") as stream:
                raw = stream.read(_MAX_PROFILE_BYTES + 1)
            if len(raw) > _MAX_PROFILE_BYTES:
                raise ProfileError("profile exceeds 64 KiB")
            data = json.loads(raw)
        except (OSError, ValueError) as error:
            raise ProfileError(f"cannot read profile {path}: {error}") from error
        return cls.from_dict(data, base=path.parent)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "name": self.name,
            "root": str(self.root),
            "interpreter": self.interpreter,
            "target": self.target,
            "args": list(self.args),
            "parts": self.parts,
            "assemblies": dict(self.assemblies),
            "simulations": dict(self.simulations),
            "trace_env": dict(self.trace_env),
            "ros_command": list(self.ros_command),
            "variants": {identifier: dict(overrides) for identifier, overrides in self.variants.items()},
            "default_variant": self.default_variant,
            "check_sets": {name: dict(specification) for name, specification in self.check_sets.items()},
            "manufacturing": dict(self.manufacturing),
        }

    def effective_variant(self, identifier: str) -> Profile:
        """Validate and resolve overrides without importing any project code."""
        if identifier not in self.variants:
            raise ProfileError(f"unknown variant {identifier!r}")
        data = self.to_dict()
        data.pop("variants")
        data.pop("default_variant")
        data.update({key: value for key, value in self.variants[identifier].items() if key != "label"})
        return self.from_dict(data, base=self.root)

    def write(self, path: Path | str) -> None:
        """Create a profile exclusively: never replace an existing file or symlink."""
        path = Path(path)
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(self.to_dict(), indent=2) + "\n")
        except OSError as error:
            raise ProfileError(
                f"cannot create profile {path} (existing files are never overwritten): {error}"
            ) from error

    @property
    def python(self) -> str:
        return self.interpreter or sys.executable


STARTER_MODEL = '''"""A small, editable build123d project. Dimensions are millimetres."""
from build123d import Box, BuildPart, Compound, Location
from cadiste.assembly import instance

WIDTH = 30
DEPTH = 20
HEIGHT = 10


def block():
    with BuildPart() as part:
        Box(WIDTH, DEPTH, HEIGHT)
    return part.part


PARTS = {"Block": block}


def build(name="Block"):
    return PARTS[name]()


def assembly():
    left = instance(block(), part="Block", label="Left block")
    right = instance(block(), part="Block", label="Right block", location=Location((WIDTH + 5, 0, 0)))
    result = Compound(children=[left, right])
    result.label = "Starter assembly"
    return result
'''


def create_starter(directory: Path, *, name: str, interpreter: str | None = None) -> Path:
    """Create a complete starter in a new directory; an existing directory is never modified."""
    _text(name, "name")
    directory = Path(directory)
    # Validate the interpreter before creating anything.
    if interpreter is not None:
        candidate = Path(interpreter)
        if not candidate.is_absolute():
            raise ProfileError("starter interpreter must be an absolute executable path")
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ProfileError(f"interpreter is not an executable file: {candidate}")
    try:
        directory.mkdir()
    except OSError as error:
        raise ProfileError(f"cannot create project directory {directory}; choose a new directory: {error}") from error
    created: list[Path] = []
    try:
        model = directory / "model.py"
        with model.open("x", encoding="utf-8") as stream:
            created.append(model)
            stream.write(STARTER_MODEL)
        profile = Profile.from_dict(
            {
                "version": 1,
                "name": name,
                "root": str(directory.resolve()),
                "interpreter": interpreter,
                "target": "model:build",
                "parts": "model:PARTS",
                "assemblies": {"Starter": "model:assembly"},
                "simulations": {},
                "trace_env": {},
                "args": [],
                "ros_command": ["ros2"],
            },
            base=directory,
        )
        path = directory / PROFILE_NAME
        profile.write(path)
        created.append(path)
        return path
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            directory.rmdir()
        raise
