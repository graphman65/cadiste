"""Self-contained, captured selection text. Nothing is stored or resolved by a later lookup."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

from . import context

if TYPE_CHECKING:
    from .components import CodeModel
    from .server import Build, Builds, Measurer, Project


class FeatureItem(TypedDict):
    kind: Literal["feature"]
    part: str
    node: str


class InstanceItem(TypedDict):
    kind: Literal["instance"]
    path: str


class EntityItem(TypedDict):
    kind: Literal["entity"]
    path: str


SelectionItem = FeatureItem | InstanceItem | EntityItem


class SnapshotError(ValueError):
    """A captured build cannot safely be used; the user should reload the viewer."""


def selection_items(value: Any) -> list[SelectionItem]:
    """Validate the canonical selection and deduplicate it without changing its order."""
    if not isinstance(value, list):
        raise SnapshotError("selection must be a list of feature, instance or entity items")
    result: list[SelectionItem] = []
    for item in value:
        if not isinstance(item, dict):
            raise SnapshotError("each selection item must be an object")
        kind = item.get("kind")
        keys = {"kind", "part", "node"} if kind == "feature" else {"kind", "path"}
        if (
            kind not in ("feature", "instance", "entity")
            or set(item) != keys
            or not all(isinstance(item[key], str) and item[key] for key in keys)
        ):
            raise SnapshotError(f"invalid selection item: {item!r}")
        if item not in result:
            result.append(cast(SelectionItem, item))
    return result


class Snapshot:
    def __init__(self, builds: Builds, measurer: Measurer, project: Project, root: Path):
        self.builds = builds
        self.measurer = measurer
        self.project = project
        self.root = root

    def _pinned(self, target: dict[str, Any], shapes: Any) -> Build:
        build = self.builds.latest(target, refresh=False)
        if (
            not isinstance(shapes, str)
            or not shapes
            or build is None
            or build.body is None
            or build.shapes is None
            or build.shapes.name != shapes
            or not build.shapes.is_file()
        ):
            raise SnapshotError(
                f"The displayed build of {target!r} is unavailable or expired. "
                "Reload/rebuild this view before copying the selection; no newer build was substituted."
            )
        return build

    def create(self, tab: str | None, state: dict[str, Any]) -> dict[str, str]:
        target = state.get("target")
        if not isinstance(target, dict):
            raise SnapshotError("nothing is shown to capture")
        if state.get("status") != "ok":
            raise SnapshotError("Wait for a successful build before copying the selection.")
        items = selection_items(state.get("selection"))
        build = self._pinned(target, state.get("shapes"))
        assert build.body is not None and build.shapes is not None
        built = json.loads(build.body)
        part_shapes = state.get("partShapes") or {}
        if not isinstance(part_shapes, dict):
            raise SnapshotError("partShapes must map part names to loaded shapes identities")
        traces: dict[str, tuple[dict[str, Any], Build]] = {}
        trace_errors: dict[str, str] = {}
        relevant = {item["part"] for item in items if item["kind"] == "feature"}
        if target.get("kind") == "part":
            traces[target["name"]] = (built, build)
        else:
            root = built["scene"]["data"]["shapes"]
            member_paths: list[str] = []
            if not items:
                member_paths = [leaf["id"] for leaf in context.leaves(root)]
            for item in items:
                if item["kind"] == "entity":
                    picked = context.pick_path(item["path"])
                    if picked:
                        member_paths.append(picked[0])
                elif item["kind"] == "instance":
                    chain = context.shape_chain(root, item["path"])
                    if chain:
                        member_paths.extend(leaf["id"] for leaf in context.leaves(chain[-1]))
            relevant.update(
                owner
                for path in member_paths
                if (owner := context.owning_part(built, path, set(self.project.names))) is not None
            )
        for part in sorted(relevant):
            if part in traces or part not in part_shapes:
                continue
            try:
                pinned = self._pinned({"kind": "part", "name": part}, part_shapes[part])
            except (SnapshotError, LookupError) as error:
                trace_errors[part] = str(error)
                continue
            assert pinned.body is not None
            traces[part] = (json.loads(pinned.body), pinned)

        digests = [self._identity("target", build)]
        digests.extend(
            self._identity(f"part {part!r}", pinned) for part, (_, pinned) in traces.items() if pinned != build
        )
        digests.extend(f"- part {part!r}: provenance unavailable — {error}" for part, error in trace_errors.items())
        lines = [
            "# Cadiste selection v1",
            "",
            f"Captured at: {datetime.now(UTC).isoformat(timespec='seconds')}",
            f"Project root: {self.root}",
            f"Target: {json.dumps(target, ensure_ascii=False, sort_keys=True)}",
            "Units: millimetres (mm), square mm, cubic mm; angles in degrees.",
            f"Viewer tab: {tab or 'unavailable'}",
            "",
            "This is a captured, not-live selection snapshot. Geometry, feature IDs, arguments and source below "
            "belong to these builds, not necessarily the current files. Source excerpts come from the built trace. "
            "This text is self-contained: no server, lookup or tool call is required to interpret it.",
            "",
            "## Build identities",
            *digests,
            "",
            "## Target context",
            context.trace_summary(built) if target["kind"] == "part" else context.composite_summary(built),
        ]
        if target["kind"] == "part":
            lines += [context.viewer_paths(built)]
        if state.get("hidden"):
            lines += ["Hidden viewer instances: " + ", ".join(state["hidden"])]

        paths = [item["path"] for item in items if item["kind"] == "entity"]
        measured, relationships, errors = self._measure(build, paths)
        lines += [
            "",
            "## Selection",
            f"{len(items)} item(s), in selection order." if items else "Entire current target (empty selection).",
        ]
        labels: list[str] = []
        rendered_owners: set[str] = set()
        code_models: dict[str, CodeModel] = {}
        if not items:
            if target["kind"] == "part":
                lines += [f"Part: {target['name']}"]
                for root in built["roots"]:
                    lines += self._code_lines(built, target["name"], root["id"], code_models)
                lines += ["Captured feature tree:", context.feature_tree(built)]
            else:
                lines += self._members(built)
        for index, item in enumerate(items, 1):
            if item["kind"] == "feature":
                part, node = item["part"], item["node"]
                labels.append(f"{part}: {node}")
                lines += ["", f"### {index}. Feature {node} of part {part!r}"]
                if part not in traces:
                    reason = trace_errors.get(part, "this part's trace was not loaded in the captured viewer state")
                    lines += [f"Feature provenance unavailable: {reason}"]
                    continue
                trace, _ = traces[part]
                lines += [
                    f"Canonical part path: {trace['scene']['part']}",
                    "Owning part geometry/context: " + context.trace_summary(trace),
                    self._describe(trace, node, part, code_models),
                ]
            elif item["kind"] == "instance":
                path = item["path"]
                labels.append(path)
                lines += ["", f"### {index}. Instance {path}"]
                if target["kind"] == "part":
                    lines += ["Instance context unavailable: the target is a traced part, not a composite view."]
                else:
                    lines += self._members(built, path)
                    chain = context.shape_chain(built["scene"]["data"]["shapes"], path)
                    if chain:
                        lines += self._placement_lines(built, chain[-1])
                    bounds = chain[-1].get("bb") if chain else None
                    lines += [
                        f"Viewer bounds (mesh context, not exact B-rep): {json.dumps(bounds)}"
                        if bounds
                        else "Instance geometry unavailable: this build retained no instance bounds.",
                        "Instance placement source unavailable: composite views are built without tracing.",
                    ]
                    if chain:
                        owners = {
                            owner
                            for member in context.leaves(chain[-1])
                            if (owner := context.owning_part(built, member["id"], set(self.project.names))) is not None
                        }
                        for owner in sorted(owners):
                            if owner in rendered_owners:
                                lines += [f"Owning-part context for {owner!r} is captured above (shared owner)."]
                                continue
                            rendered_owners.add(owner)
                            lines += ["", f"#### Owning part {owner!r}: captured trace context"]
                            if owner not in traces:
                                reason = trace_errors.get(owner, "the owning-part trace was not loaded")
                                lines += [f"Owning-part feature/source context unavailable: {reason}."]
                                continue
                            owner_trace, _ = traces[owner]
                            lines += [
                                context.trace_summary(owner_trace),
                                f"Canonical part path: {owner_trace['scene']['part']}",
                                "This pinned owning-part trace is source context, not assembly placement source.",
                                "Captured feature tree:",
                                context.feature_tree(owner_trace),
                                "Captured source entry point(s):",
                            ]
                            lines.extend(
                                self._describe(owner_trace, root["id"], owner, code_models)
                                for root in owner_trace["roots"]
                            )
                            if not owner_trace["roots"]:
                                lines += ["Source entry point unavailable: this part has no traced feature roots."]
            else:
                path = item["path"]
                labels.append(path)
                lines += ["", f"### {index}. Entity", f"Full viewer path: {path}"]
                entity = measured.get(path)
                geometry = (
                    context.entity_text(entity) if entity else f"unavailable ({errors.get(path, 'not measured')})"
                )
                lines += self._entity_context(target, built, build, path, traces, code_models)
                lines += ["Geometry: " + geometry]
        if paths:
            lines += [
                "",
                "## Relationships between selected entities",
                "Viewer paths in measurement order:",
                *[f"- {path}" for path in paths],
            ]
            lines += (
                [context.results_text(relationships)]
                if relationships
                else ["Pair measurements unavailable or not applicable."]
            )
            if errors.get("relationships"):
                lines += ["Relationships unavailable: " + errors["relationships"]]
        label = ", ".join(labels) if labels else "entire current target"
        return {"text": "\n".join(lines), "label": label}

    @staticmethod
    def _identity(label: str, build: Build) -> str:
        assert build.body is not None
        shapes = build.shapes.name if build.shapes else "unavailable"
        return f"- {label}: SHA-256 {hashlib.sha256(build.body).hexdigest()}; shapes {shapes}"

    def _measure(self, build: Build, paths: list[str]) -> tuple[context.Measured, list[dict[str, Any]], dict[str, str]]:
        if not paths:
            return {}, [], {}
        assert build.shapes is not None
        entities: context.Measured = {}
        relationships: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        try:
            result = self.measurer.measure(build.shapes.name, paths)
            entities = {entity["path"]: entity for entity in result["entities"]}
            relationships = result["results"]
        except Exception as error:
            errors["relationships"] = str(error) or type(error).__name__
            for path in paths:
                try:
                    result = self.measurer.measure(build.shapes.name, [path])
                    entities.update({entity["path"]: entity for entity in result["entities"]})
                except Exception as error:
                    errors[path] = str(error) or type(error).__name__
        for path in paths:
            if path not in entities and path not in errors:
                errors[path] = "measurement returned no entity"
        return entities, relationships, errors

    @staticmethod
    def _code_lines(trace: dict[str, Any], part: str, node: str, models: dict[str, CodeModel]) -> list[str]:
        from .components import CodeModel

        try:
            if part not in models:
                models[part] = CodeModel(trace, part)
            selected = models[part].reference(node)
        except (LookupError, ValueError) as error:
            return [f"Code component context unavailable: {str(error).strip(chr(39))}"]
        lines = [
            "Primary code reference: " + str(selected["definition"] or "(unavailable in captured source)"),
            "Construction occurrence: " + str(selected["reference"]),
            "Captured source revision: " + str(selected["source_revision"]),
            "Code component ancestry (outermost first):",
        ]
        for depth, component in enumerate(selected["ancestry"]):
            label = component.get("label", component.get("kind", "component"))
            lines.append(f"{'  ' * depth}- {label} [{component['reference']}]")
        return lines

    def _describe(self, trace: dict[str, Any], node: str, part: str, models: dict[str, CodeModel]) -> str:
        lines = self._code_lines(trace, part, node, models)
        try:
            lines.append(context.describe(trace, node))
        except LookupError as error:
            lines.append(f"Feature detail unavailable: {str(error).strip(chr(39))}")
        return "\n".join(lines)

    def _members(self, built: dict[str, Any], path: str | None = None) -> list[str]:
        root = built["scene"]["data"]["shapes"]
        chain = context.shape_chain(root, path) if path is not None else [root]
        if chain is None:
            return ["Instance context unavailable: this path is not in the captured build."]
        node = chain[-1]
        leaves = context.leaves(node)
        lines = [f"Members ({len(leaves)}):"]
        for leaf in leaves:
            owner = context.owning_part(built, leaf["id"], set(self.project.names))
            ownership = owner or "unavailable (no unambiguous part mapping)"
            lines += [f"- {leaf['name']}; viewer path {leaf['id']}; owning part: {ownership}"]
        return lines

    def _entity_context(
        self,
        target: dict[str, Any],
        built: dict[str, Any],
        build: Build,
        path: str,
        traces: dict[str, tuple[dict[str, Any], Build]],
        code_models: dict[str, CodeModel],
    ) -> list[str]:
        picked = context.pick_path(path)
        if picked is None:
            return ["Provenance unavailable: not a face, edge or vertex path."]
        leaf, topo, number = picked
        if context.shape_chain(built["scene"]["data"]["shapes"], leaf) is None:
            return ["Owning part/provenance unavailable: this viewer leaf is not in the captured build."]
        part = target["name"] if target["kind"] == "part" else context.owning_part(built, leaf, set(self.project.names))
        lines = [f"Owning part: {part or 'unavailable (no unambiguous part mapping)'}"]
        if part is None or part not in traces:
            return [*lines, "Creating feature/source unavailable: no pinned owning-part trace was loaded."]
        trace, trace_build = traces[part]
        canonical = trace["scene"]["part"]
        canonical_path = f"{canonical}/{topo}/{topo}_{number}"
        lines += [f"Canonical part path: {canonical}"]
        if build != trace_build:
            assert build.shapes is not None and trace_build.shapes is not None
            try:
                viewer_chain = context.shape_chain(built["scene"]["data"]["shapes"], leaf) or []
                trace_chain = context.shape_chain(trace["scene"]["data"]["shapes"], canonical) or []
                agrees = self.measurer.corresponds(
                    build.shapes.name,
                    path,
                    trace_build.shapes.name,
                    canonical_path,
                    [node["loc"] for node in viewer_chain if node.get("loc") is not None],
                    [node["loc"] for node in trace_chain if node.get("loc") is not None],
                )
            except Exception as error:
                reason = str(error) or type(error).__name__
                return [
                    *lines,
                    f"Creating feature/source unavailable: geometric correspondence could not be checked ({reason}).",
                ]
            if not agrees:
                return [
                    *lines,
                    "Creating feature/source unavailable: the picked region or its adjacent face indices "
                    "do not geometrically match the pinned part trace; indices cannot safely be mapped.",
                ]
            lines += [
                "Provenance mapping: picked region and adjacent face indices agree geometrically with the pinned "
                "part trace at 0.000001 mm OCC tolerance, after recorded placement normalization."
            ]
        elif leaf != canonical:
            return [
                *lines,
                "Creating feature/source unavailable: this entity belongs to a sketch or auxiliary shape, "
                "not the final part.",
            ]
        if not trace["scene"]["face_ids"]:
            return [*lines, "Creating feature/source unavailable: viewer face numbering differs from trace topology."]
        assert trace_build.shapes is not None
        try:
            # Validate even a face's index on the pinned trace before attributing it.
            self.measurer.measure(trace_build.shapes.name, [canonical_path])
            faces = [number] if topo == "faces" else self.measurer.adjacent(trace_build.shapes.name, canonical_path)
        except Exception as error:
            return [*lines, f"Creating feature/source unavailable: {str(error) or type(error).__name__}."]
        lines += [f"Canonical entity path: {canonical_path}"]
        if faces is None:
            return [*lines, "Adjacent face maker context unavailable: topology adjacency was not retained."]
        lines += [f"Adjacent/result faces: {', '.join(f'f{face}' for face in faces) or 'none'}"]
        makers = []
        for face in faces:
            maker = context.face_maker(trace, face)
            if maker is None:
                lines += [f"Face f{face}: creating feature unavailable (no traced operation)."]
            else:
                lines += [f"Face f{face}: created by o{maker['index']}."]
                if maker["index"] not in makers:
                    makers.append(maker["index"])
        for maker in makers:
            lines += self._code_lines(trace, part, f"o{maker}", code_models)
            lines += [
                "",
                "Creating feature detail (captured source and arguments):",
                "\n".join(context.operation_lines(trace, trace["ops"][maker])),
            ]
        return lines

    @staticmethod
    def _placement_lines(built: dict[str, Any], selected: dict[str, Any]) -> list[str]:
        lines = [
            "Recorded scene-chain placement (root to leaf; compose local transforms in this order):",
            "Translations are mm; quaternion component order is x, y, z, w.",
        ]
        seen: set[str] = set()
        root = built["scene"]["data"]["shapes"]
        for member in context.leaves(selected):
            chain = context.shape_chain(root, member["id"]) or []
            for index, node in enumerate(chain):
                if node["id"] in seen:
                    continue
                seen.add(node["id"])
                translation, quaternion = node.get("loc") or ([0, 0, 0], [0, 0, 0, 1])
                parent = chain[index - 1]["id"] if index else "(scene root)"
                lines += [
                    f"- {node['id']}; parent {parent}; local translation {json.dumps(translation)}; "
                    f"quaternion {json.dumps(quaternion)}"
                ]
        return lines
