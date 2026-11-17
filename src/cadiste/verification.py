"""Headless, explicit-target verification using the existing build cache and captured evidence.

A successful build is not proof of fit. Requested checks run on that exact build's retained B-rep archive;
source revision guards and incomplete dependency suggestions are reported separately from geometry identity.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .context import compare, structured_diff

if TYPE_CHECKING:
    from .server import Build, Builds

Json = dict[str, Any]

_SHAPES_LOCK = threading.Lock()


def _key(target: Json) -> str:
    return json.dumps(target, sort_keys=True, separators=(",", ":"))


def _target(target: Json, builds: Builds) -> Json:
    if not isinstance(target, dict):
        raise ValueError("each verification target must be an explicit target object")
    match target:
        case {"kind": "part", "name": str(name)} if name:
            return {"kind": "part", "name": name}
        case {"kind": "assembly"}:
            name = target.get("name") or next(iter(builds.project.assemblies), "")
            if not isinstance(name, str) or not name:
                raise ValueError("an assembly target requires a configured assembly name")
            return {"kind": "assembly", "name": name}
        case {"kind": "file", "path": str(path)} if path:
            return {"kind": "file", "path": path}
    raise ValueError(f"invalid verification target: {target!r}")


def _data(build: Build | None) -> Json | None:
    if build is None or build.body is None:
        return None
    value = json.loads(build.body)
    if not isinstance(value, dict):
        raise ValueError("build output must be a JSON object")
    return value


def _project_targets(builds: Builds) -> list[Json]:
    project = builds.project
    result = [{"kind": "part", "name": name} for name in project.names]
    result.extend({"kind": "assembly", "name": name} for name in project.assemblies)
    folders = {
        "/".join(str(step["name"]) for step in steps[:index])
        for steps in project.folders.values()
        for index in range(1, len(steps) + 1)
    }
    result.extend({"kind": "file", "path": path} for path in sorted(folders))
    return result


def _suggest(changed_files: Iterable[str], captures: list[tuple[Json, Json | None]]) -> Json:
    changed = {Path(file).as_posix() for file in changed_files}
    suggestions = []
    for target, captured in captures:
        sources = captured.get("sources", {}) if captured is not None else {}
        evidence = sorted(changed.intersection(Path(file).as_posix() for file in sources))
        suggestions.append(
            {
                "target": target,
                "basis": "captured source dependency" if evidence else "conservative candidate",
                "files": evidence,
                "reason": (
                    "Changed files occur in this target's captured source set; capture may include unexecuted code."
                    if evidence
                    else "No matching captured dependency proves this target unaffected; "
                    "dynamic and unbuilt consumers remain possible."
                ),
            }
        )
    return {
        "complete": False,
        "scope": "suggestions only, not a complete dependency scheduler; no targets are automatically built",
        "changed_files": sorted(changed),
        "targets": suggestions,
        "limitations": [
            "Captured Python sources are a conservative dependency set, not an exhaustive read-set.",
            "Dynamic imports, external inputs, uncaptured and newly added consumers may be missing.",
        ],
    }


def suggest_affected_targets(
    builds: Builds,
    changed_files: Iterable[str],
    *,
    targets: list[Json] | None = None,
) -> Json:
    """Suggest from retained captures only; never execute builds or claim complete dependency analysis."""
    captures: list[tuple[Json, Json | None]] = []
    failures = []
    seen = set()
    for supplied in _project_targets(builds) if targets is None else targets:
        try:
            target = _target(supplied, builds)
            key = _key(target)
            if key in seen:
                continue
            seen.add(key)
            captures.append((target, _data(builds.latest(target))))
        except Exception as error:
            captures.append((supplied, None))
            failures.append({"target": supplied, "error": str(error)})
    return {**_suggest(changed_files, captures), "capture_errors": failures}


def _requested(
    builds: Builds, specifications: list[Json], targets: list[Json], label: str
) -> list[list[Json]]:
    if len(specifications) > 128:
        raise ValueError(f"at most 128 requested {label} are supported")
    requested: list[list[Json]] = [[] for _ in targets]
    for specification in specifications:
        if not isinstance(specification, dict):
            raise ValueError(f"each requested {label} must be an object")
        selected = _target(specification["target"], builds) if "target" in specification else None
        indices = [index for index, target in enumerate(targets) if selected is None or selected == target]
        if not indices:
            raise ValueError(f"requested {label} target is not in verification targets: {selected!r}")
        arguments = {key: value for key, value in specification.items() if key != "target"}
        for index in indices:
            requested[index].append(arguments)
    return requested


def _named_sets(builds: Builds, names: list[str], targets: list[Json]) -> tuple[list[Json], list[Json], list[Json]]:
    checks, assertions, selected_sets = [], [], []
    if len(names) > 32 or any(not isinstance(name, str) or not name or len(name) > 200 for name in names):
        raise ValueError("check_sets must contain at most 32 nonempty names of at most 200 characters")
    root = builds.project
    variants = getattr(root, "variant_projects", {})
    seen = set()
    for name in names:
        candidates = []
        prefix, separator, _ = name.partition("/")
        if variants and separator and prefix in variants:
            project, local, variant = root.resolve({"kind": "part", "name": name})
            candidates.append((project, local["name"], variant))
        elif variants:
            for target in targets:
                project, _, variant = root.resolve(target)
                if not any(item[2] == variant for item in candidates):
                    candidates.append((project, name, variant))
        else:
            project, local, variant = root.resolve({"kind": "part", "name": name})
            candidates.append((project, local["name"], variant))
        for project, local_name, variant in candidates:
            qualified = f"{variant}/{local_name}" if variant else local_name
            if qualified in seen:
                continue
            seen.add(qualified)
            specification = getattr(project, "check_sets", {}).get(local_name)
            if not isinstance(specification, dict):
                raise LookupError(f"unknown mechanical check set {qualified!r}")
            if set(specification) - {"target", "checks", "assertions"} or "target" not in specification:
                raise ValueError(f"check set {qualified!r} requires target and checks/assertions only")
            supplied = specification["target"]
            if not isinstance(supplied, dict):
                raise ValueError(f"check set {qualified!r} target must be an explicit target object")
            target = dict(supplied)
            key = "path" if target.get("kind") == "file" else "name"
            value = target.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"check set {qualified!r} requires an explicitly named local target")
            if variant:
                target[key] = f"{variant}/{value}"
            target = _target(target, builds)
            if target not in targets:
                raise ValueError(f"check set {qualified!r} target is not in verification targets: {target!r}")
            set_checks, set_assertions = specification.get("checks", []), specification.get("assertions", [])
            if (
                not isinstance(set_checks, list)
                or not isinstance(set_assertions, list)
                or not (set_checks or set_assertions)
            ):
                raise ValueError(f"check set {qualified!r} requires nonempty checks or assertions lists")
            for entries, destination in ((set_checks, checks), (set_assertions, assertions)):
                for entry in entries:
                    if not isinstance(entry, dict) or {"target", "_check_set"}.intersection(entry):
                        raise ValueError(f"check set {qualified!r} entries must be objects without target overrides")
                    destination.append({**entry, "target": target, "_check_set": qualified})
            selected_sets.append({"name": qualified, "target": target})
    return checks, assertions, selected_sets


def _envelope(specification: Json) -> Json:
    return {
        "request": {key: value for key, value in specification.items() if key != "_check_set"},
        **({"check_set": specification["_check_set"]} if "_check_set" in specification else {}),
    }


def _run_checks(builds: Builds, shapes_file: Path | None, specifications: list[Json]) -> list[Json]:
    if not specifications:
        return []
    if shapes_file is None or not shapes_file.is_file():
        return [
            {
                **_envelope(specification),
                "ok": False,
                "error": "requested checks require this build's retained shapes file",
            }
            for specification in specifications
        ]
    runner = getattr(builds, "check", None)
    loaded = None
    results = []
    for specification in specifications:
        arguments = _envelope(specification)["request"]
        try:
            if callable(runner) and arguments.get("kind", "geometry") == "geometry":
                result = runner(shapes_file.name, **{key: value for key, value in arguments.items() if key != "kind"})
            else:
                from .measure import load
                from .mechanical import run_check

                with getattr(builds, "geometry_lock", _SHAPES_LOCK):
                    if loaded is None:
                        loaded = load(shapes_file)
                    result = run_check(loaded, arguments)
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                raise ValueError("geometry check returned no explicit boolean ok outcome")
            results.append({**_envelope(specification), "ok": result["ok"], "result": result})
        except Exception as error:
            results.append({**_envelope(specification), "ok": False, "error": str(error)})
    return results


def _run_assertions(
    builds: Builds, shapes_file: Path | None, baseline_file: Path | None, specifications: list[Json]
) -> list[Json]:
    if not specifications:
        return []
    from .measure import load
    from .mechanical import run_assertion

    with getattr(builds, "geometry_lock", _SHAPES_LOCK):
        try:
            if shapes_file is None or not shapes_file.is_file():
                raise ValueError("interface assertions require this build's retained exact shapes file")
            after = load(shapes_file)
            before = load(baseline_file) if baseline_file is not None and baseline_file.is_file() else None
        except Exception as error:
            return [{**_envelope(specification), "ok": False, "error": str(error)} for specification in specifications]
        results = []
        for specification in specifications:
            try:
                result = run_assertion(after, _envelope(specification)["request"], before)
                results.append({**_envelope(specification), "ok": result["ok"], "result": result})
            except Exception as error:
                results.append({**_envelope(specification), "ok": False, "error": str(error)})
        return results


def verify(
    builds: Builds,
    targets: list[Json],
    *,
    fresh: bool = False,
    checks: list[Json] | None = None,
    expected_revision: str | None = None,
    baseline: str | None = None,
    check_sets: list[str] | None = None,
    assertions: list[Json] | None = None,
) -> Json:
    """Build each explicit target once and run requested checks on its exact shapes.

    ``expected_revision`` guards each returned capture, never silently substitutes a different source revision.
    Checks without ``target`` apply to every requested target. A targeted check must name one of those targets.
    """
    if not isinstance(targets, list) or not 1 <= len(targets) <= 64:
        raise ValueError("verify requires 1 to 64 explicit targets")
    if checks is not None and not isinstance(checks, list):
        raise ValueError("checks must be a list of explicit check requests")
    if check_sets is not None and not isinstance(check_sets, list):
        raise ValueError("check_sets must be a list of configured mechanical check-set names")
    if assertions is not None and not isinstance(assertions, list):
        raise ValueError("assertions must be a list of explicit interface assertions")
    if baseline is not None and (not isinstance(baseline, str) or not baseline):
        raise ValueError("baseline must be a nonempty pinned baseline id")
    if expected_revision is not None and not isinstance(expected_revision, str):
        raise ValueError("expected_revision must be a captured source revision string")
    normalized = []
    invalid = []
    for supplied in targets:
        try:
            target = _target(supplied, builds)
        except (TypeError, ValueError) as error:
            invalid.append({"target": supplied, "ok": False, "error": str(error), "checks": []})
            continue
        if target not in normalized:
            normalized.append(target)
    named_checks, named_assertions, selected_sets = _named_sets(builds, check_sets or [], normalized)
    requested = _requested(builds, [*(checks or []), *named_checks], normalized, "check")
    asserted = _requested(builds, [*(assertions or []), *named_assertions], normalized, "assertion")
    evaluations = 0
    for specifications in requested:
        for specification in specifications:
            pairs = specification.get("pairs", [])
            samples = specification.get("samples", [])
            travel = specification.get("travel", {})
            count = travel.get("steps", 0) if isinstance(travel, dict) else 0
            count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
            evaluations += max(1, len(pairs) if isinstance(pairs, list) else 1) * (
                1 + max(count, len(samples) if isinstance(samples, list) else 0)
            )
    for specifications in asserted:
        for specification in specifications:
            paths = specification.get("paths", [])
            evaluations += 2 * max(1, len(paths) if isinstance(paths, list) else 1)
    if evaluations > 4096:
        raise ValueError("verification exceeds 4096 exact pair/pose/interface evaluations; reduce targets or requests")
    results = []
    changed_files: set[str] = set()
    captures: dict[str, tuple[Json, Json | None]] = {}
    for target, specifications, interface_requests in zip(normalized, requested, asserted, strict=True):
        outcome: Json = {
            "target": target, "ok": False, "checks": [], "assertions": [],
            "check_sets": [item for item in selected_sets if item["target"] == target],
            "comparison_basis": "pinned_baseline" if baseline is not None else "latest_retained",
        }
        results.append(outcome)
        before, previous = None, None
        try:
            if baseline is None:
                previous = builds.latest(target)
            else:
                from .baselines import Baselines

                store = getattr(builds, "baselines", None) or Baselines(builds)
                previous = store.get(baseline, target)
            before = _data(previous)
            if baseline is not None:
                outcome["baseline"] = {
                    "id": baseline, "available": True, "target": target,
                    "build_id": before.get("build_id") if before is not None else None,
                    "source_revision": before.get("source_revision") if before is not None else None,
                    "shapes_available": previous is not None
                    and previous.shapes is not None
                    and previous.shapes.is_file(),
                }
        except Exception as error:
            outcome["comparison_error"] = str(error)
            if baseline is not None:
                outcome["baseline"] = {"id": baseline, "target": target, "available": False, "error": str(error)}
        try:
            build = builds.get(target, fresh=bool(fresh or builds.always_fresh))
            outcome["cached"] = build.cached
            after = _data(build)
            if after is None:
                outcome.update(build.failure or {"error": "build returned no output"})
                captures[_key(target)] = (target, before)
                continue
            outcome.update(
                build_id=after.get("build_id"),
                source_revision=after.get("source_revision"),
                source_fingerprint=after.get("source_fingerprint"),
                shapes=build.shapes.name if build.shapes is not None else None,
                diff=structured_diff(before, after),
                comparison=compare(before, after),
            )
            captures[_key(target)] = (target, after)
            if before is not None:
                old_sources, new_sources = before.get("sources", {}), after.get("sources", {})
                changed_files.update(
                    file
                    for file in old_sources.keys() | new_sources.keys()
                    if old_sources.get(file) != new_sources.get(file)
                )
            if expected_revision is not None and after.get("source_revision") != expected_revision:
                actual = after.get("source_revision")
                outcome["error"] = f"source revision mismatch: expected {expected_revision!r}, got {actual!r}"
                outcome["checks"] = [
                    {"request": specification, "ok": False, "error": "not run: source revision guard failed"}
                    for specification in specifications
                ]
                outcome["assertions"] = [
                    {**_envelope(specification), "ok": False, "error": "not run: source revision guard failed"}
                    for specification in interface_requests
                ]
                continue
            outcome["checks"] = _run_checks(builds, build.shapes, specifications)
            outcome["assertions"] = _run_assertions(
                builds, build.shapes,
                previous.shapes if baseline is not None and previous is not None else None,
                interface_requests,
            )
            outcome["ok"] = (
                (baseline is None or outcome.get("baseline", {}).get("available", False))
                and all(result["ok"] for result in [*outcome["checks"], *outcome["assertions"]])
            )
            if not outcome["ok"]:
                outcome["error"] = "pinned baseline unavailable or one or more requested checks/assertions failed"
        except Exception as error:
            outcome["error"] = str(error) or type(error).__name__
            captures.setdefault(_key(target), (target, before))
    # Suggestions never build missing consumers. Include all known targets conservatively, even without evidence.
    capture_errors = []
    try:
        for target in _project_targets(builds):
            key = _key(target)
            if key in captures:
                continue
            try:
                captures[key] = (target, _data(builds.latest(target)))
            except Exception as error:
                captures[key] = (target, None)
                capture_errors.append({"target": target, "error": str(error)})
    except Exception as error:
        capture_errors.append({"error": str(error)})
    suggestions = {**_suggest(changed_files, list(captures.values())), "capture_errors": capture_errors}
    return {
        "ok": bool(results) and not invalid and all(result["ok"] for result in results),
        "results": [*results, *invalid],
        "affected_targets": suggestions,
    }
