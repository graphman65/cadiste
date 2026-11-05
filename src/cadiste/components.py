"""Code-first navigation of a captured build, without importing or executing model source."""

from __future__ import annotations

import ast
import hashlib
import json
from collections import defaultdict
from typing import Any

from .code_index import SourceIndex

Json = dict[str, Any]
_MAX_DEPTH = 8


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:20]


class CodeModel:
    """Definitions, source call occurrences and runtime aliases are deliberately separate identities."""

    def __init__(self, trace: Json, part: str):
        self.trace = trace
        self.part = part
        self.index = SourceIndex(trace.get("sources", {}))
        self.revision = trace.get("source_revision") or self.index.revision
        self.nodes: dict[str, Json] = {}
        self.aliases: dict[str, str] = {}
        self.roots: list[str] = []
        self._counts: dict[tuple[str | None, str], int] = defaultdict(int)
        self._sites: dict[tuple[str, int, str, str], Json] = {}
        self._invocation_nodes: dict[int, str] = {}
        self._ops = {op["index"]: op for op in trace.get("ops", [])}
        self._builders = {builder["index"]: builder for builder in trace.get("builders", [])}
        for root in trace.get("roots", []):
            parent = None
            if root.get("type") == "builder":
                sites = self._builders[root["builder"]].get("site", [])
                # Dispatch/cache functions are not model components. Start at the outer constructor, or the
                # builder-owning function for function-based models; retain the full stack in inspection.
                start = next(
                    (offset for offset, site in enumerate(sites) if site.get("function", "").endswith(".__init__")),
                    max(0, len(sites) - 1),
                )
                for offset in range(start, len(sites)):
                    site = sites[offset]
                    invocation = site.get("invocation")
                    if invocation is not None and invocation in self._invocation_nodes:
                        parent = self._invocation_nodes[invocation]
                        continue
                    called_at = sites[offset - 1] if offset else site
                    definition = self._owner(site)
                    parent = self._add(
                        "invocation",
                        definition,
                        called_at,
                        parent,
                        label=site.get("label"),
                        observed_invocation=site.get("invocation"),
                        record={"site": sites[: offset + 1]},
                    )
                    if invocation is not None:
                        self._invocation_nodes[invocation] = parent
            self._tree(root, parent)
        # Published hand-overs retain their own operation identity and arguments. The visible feature tree
        # represents them with a child builder, but that does not make the two code occurrences identical.
        for op in self._ops.values():
            alias = f"o{op['index']}"
            if alias not in self.aliases:
                sites = op.get("site", [])
                site = sites[-1] if sites else {}
                parent = next(
                    (
                        self._invocation_nodes[frame["invocation"]]
                        for frame in reversed(sites)
                        if frame.get("invocation") in self._invocation_nodes
                    ),
                    self.aliases.get(f"b{op.get('builder')}"),
                )
                reference = self._add(
                    "op",
                    self._owner(site),
                    site,
                    parent,
                    alias=alias,
                    label=op.get("kind"),
                    record=op,
                    observed_invocation=site.get("invocation"),
                )
                # Keep the construction walk faithful to its captured tree; inspect exposes publication edges.
                (self.nodes[parent]["children"] if parent else self.roots).remove(reference)

    def _owner(self, site: Json) -> str | None:
        return self.index.at(site.get("file", ""), site.get("line", 0), site.get("function"))

    def _site(self, site: Json) -> Json:
        file, line = site.get("file", ""), site.get("line", 0)
        key = (file, line, site.get("function", ""), site.get("code", ""))
        if key in self._sites:
            return self._sites[key]
        owner = self._owner(site)
        tree = self.index.syntax_tree(file)
        candidates = (
            []
            if tree is None
            else [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.Call, ast.With, ast.AsyncWith))
                and node.lineno <= line <= (node.end_lineno or node.lineno)
            ]
        )
        try:
            recorded = ast.dump(ast.parse(site.get("code", ""), mode="eval").body, include_attributes=False)
        except SyntaxError:
            recorded = None
        # Calls are narrower than their containing with block; use the closest expression.
        candidates.sort(
            key=lambda node: (
                ast.dump(node, include_attributes=False) != recorded,
                (node.end_lineno or node.lineno) - node.lineno,
                -node.col_offset,
            )
        )
        expression = candidates[0] if candidates else None
        if expression is not None:
            assert tree is not None
            fingerprint = ast.dump(expression, include_attributes=False)
            equals = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.Call, ast.With, ast.AsyncWith))
                and type(node) is type(expression)
                and ast.dump(node, include_attributes=False) == fingerprint
                and self.index.at(file, node.lineno) == owner
            ]
            equals.sort(key=lambda node: (node.lineno, node.col_offset))
            ordinal = next(i for i, node in enumerate(equals) if node is expression)
        else:
            # Old captures can lack the file. Their recorded expression is still evidence, not live source.
            fingerprint = " ".join(site.get("code", "").split())
            ordinal = 0
        arguments = []
        if owner:
            arguments = [
                call
                for call in self.index.definition(owner).get("calls", [])
                if call.get("file") == file and call.get("line") == line
            ]
        result = {
            "reference": f"site:{owner or file}@{_digest([fingerprint, ordinal])}",
            "owner": owner,
            "file": file,
            "line": line,
            "expression": site.get("code", ""),
            "calls": arguments,
            "source_available": file in self.trace.get("sources", {}),
        }
        self._sites[key] = result
        return result

    def _add(
        self,
        kind: str,
        definition: str | None,
        site: Json,
        parent: str | None,
        *,
        alias: str | None = None,
        label: str | None = None,
        record: Json | None = None,
        observed_invocation: int | None = None,
    ) -> str:
        call_site = self._site(site)
        signature = _digest([kind, definition, call_site["reference"]])
        count = self._counts[parent, signature]
        self._counts[parent, signature] += 1
        reference = f"component:{self.part}:{_digest([parent, signature, count])}"
        if kind in ("group", "invocation") and definition and definition.endswith(".__init__"):
            # Traced ``self`` can be a subclass: its dynamic label is not the constructor being executed.
            label = definition.rsplit("::", 1)[-1].removesuffix(".__init__")
        node = {
            "reference": reference,
            "definition": definition,
            "kind": kind,
            "label": label or kind,
            "call_site": call_site,
            "runtime_alias": alias,
            "invocation": count + 1,
            "parents": [parent] if parent else [],
            "children": [],
            "record": record or {},
            "observed_invocation": observed_invocation,
        }
        self.nodes[reference] = node
        if alias:
            self.aliases[alias] = reference
        if parent:
            self.nodes[parent]["children"].append(reference)
        else:
            self.roots.append(reference)
        return reference

    def _tree(self, node: Json, parent: str | None) -> None:
        kind = node["type"]
        if kind == "group":
            site = node.get("called_at", {})
            definition = self.index.at(node.get("defined_in", ""), 0, node.get("function"))
            record = node
            label = node.get("label")
            observed_invocation = node.get("invocation")
        else:
            record = (self._ops if kind == "op" else self._builders)[node[kind if kind == "op" else "builder"]]
            sites = record.get("site", [])
            site = sites[-1] if sites else {}
            definition = self._owner(site)
            label = record.get("name") or record.get("kind")
            observed_invocation = site.get("invocation")
        reference = self._add(
            kind,
            definition,
            site,
            parent,
            alias=node["id"],
            label=label,
            record=record,
            observed_invocation=observed_invocation,
        )
        if kind == "group" and observed_invocation is not None:
            self._invocation_nodes[observed_invocation] = reference
        for child in node.get("children", []):
            self._tree(child, reference)

    def _definition_matches(self, owner: str | None, reference: str) -> bool:
        if owner == reference:
            return True
        definition = self.index.definitions.get(reference, {})
        return definition.get("kind") == "class" and owner == reference + ".__init__"

    def _resolve(self, component: str) -> str:
        if component in self.aliases:
            return self.aliases[component]
        if component in self.nodes or component in self.index.definitions:
            return component
        matches = [
            ref
            for ref, value in self.index.definitions.items()
            if value["qualified"] == component or value["qualified"].rsplit(".", 1)[-1] == component
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise LookupError(f"ambiguous component {component!r}; choose: {', '.join(matches)}")
        choices = list(dict.fromkeys(node["definition"] for node in self.nodes.values() if node["definition"]))
        choices.extend(list(self.aliases)[:10])
        raise LookupError(
            f"unknown component {component!r} in {self.part!r}; choose a code/component reference "
            f"or captured alias: {', '.join(choices[:30])}"
        )

    def _occurrences(self, reference: str) -> list[str]:
        if reference in self.nodes:
            return [reference]
        matches = [ref for ref, node in self.nodes.items() if self._definition_matches(node["definition"], reference)]
        boundaries = [ref for ref in matches if self.nodes[ref]["kind"] in ("group", "invocation")]
        return boundaries or matches

    def _summary(self, reference: str) -> Json:
        if reference in self.nodes:
            node = self.nodes[reference]
            return {
                **{
                    key: node[key]
                    for key in ("reference", "definition", "kind", "label", "call_site", "runtime_alias", "invocation")
                },
                "evidence": "observed construction",
            }
        definition = self.index.definition(reference)
        return {
            "reference": reference,
            "definition": reference,
            "kind": definition["kind"],
            "label": definition["qualified"],
            "file": definition["file"],
            "line": definition["line"],
            "evidence": "static captured code",
        }

    def reference(self, node: str) -> Json:
        reference = self._resolve(node)
        summary = self._summary(reference)
        ancestry = []
        current = reference
        seen = set()
        while current in self.nodes and current not in seen:
            seen.add(current)
            parents = self.nodes[current]["parents"]
            if not parents:
                break
            current = parents[0]
            ancestry.append(self._summary(current))
        return {**summary, "ancestry": list(reversed(ancestry)), "source_revision": self.revision}

    def _constructor(self, reference: str) -> str:
        definition = self.index.definitions.get(reference, {})
        constructor = reference + ".__init__"
        return constructor if definition.get("kind") == "class" and constructor in self.index.definitions else reference

    def _edges(self, reference: str, direction: str) -> list[str]:
        if reference in self.nodes:
            return self.nodes[reference][direction]
        observed = list(
            dict.fromkeys(edge for ref in self._occurrences(reference) for edge in self.nodes[ref][direction])
        )
        definition = self.index.definition(self._constructor(reference))
        if direction == "children":
            static = [
                target
                for call in definition.get("calls", [])
                for target in call.get("targets", [])
                if target in self.index.definitions
            ]
        else:
            static = [
                entry["reference"]
                for entry in self.index.related(reference).get("callers", [])
                if entry.get("reference") in self.index.definitions
            ]
        represented = [self.nodes[ref]["definition"] for ref in observed]
        static = [
            target for target in static if not any(self._definition_matches(owner, target) for owner in represented)
        ]
        return list(dict.fromkeys([*observed, *static]))

    def walk(self, component: str | None = None, direction: str = "children", depth: int = 1) -> Json:
        if direction not in ("children", "parents"):
            raise ValueError("direction must be 'children' or 'parents'")
        if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= _MAX_DEPTH:
            raise ValueError(f"depth must be an integer from 0 to {_MAX_DEPTH}")
        roots = [self._resolve(component)] if component else self.roots
        expanded: set[str] = set()

        def visit(reference: str, remaining: int, path: set[str]) -> Json:
            result = self._summary(reference)
            # Multiple feature occurrences inside one function are not recursion. Only invocation/definition
            # boundaries count as repeated definitions; operation siblings remain visible at shallow depth.
            boundary = reference not in self.nodes or self.nodes[reference]["kind"] in ("group", "invocation")
            identity = result.get("definition") if boundary else reference
            if reference in path or (identity and identity in expanded):
                return {**result, "stopped": "cycle or repeated shared definition"}
            if identity:
                expanded.add(identity)
            edges = self._edges(reference, direction)
            if remaining:
                result[direction] = [visit(edge, remaining - 1, path | {reference}) for edge in edges]
            elif edges:
                result["has_more"] = True
            return result

        return {
            "part": self.part,
            "source_revision": self.revision,
            "direction": direction,
            "depth": depth,
            "nodes": [visit(reference, depth, set()) for reference in roots],
        }

    def _observed(self, node: Json) -> Json | None:
        invocation = node.get("observed_invocation")
        captured = self.trace.get("invocations", {}).get(str(invocation)) if invocation is not None else None
        if not captured:
            return None
        return {**captured, "capture": "observed at first traced event, not guaranteed original entry arguments"}

    def inspect(self, component: str) -> Json:
        reference = self._resolve(component)
        definition = self.nodes[reference]["definition"] if reference in self.nodes else reference
        invocations = []
        for occurrence in self._occurrences(reference):
            node = self.nodes[occurrence]
            record = node["record"]
            inputs = [self.aliases[f"b{index}"] for index in record.get("inputs", []) if f"b{index}" in self.aliases]
            consumers = [
                self.aliases[f"o{index}"] for index in record.get("consumed_by", []) if f"o{index}" in self.aliases
            ]
            published = self.aliases.get(f"o{record.get('published_by')}") if node["kind"] == "builder" else None
            child = self.aliases.get(f"b{record.get('child')}") if node["kind"] == "op" else None
            invocations.append(
                {
                    **self._summary(occurrence),
                    "arguments": record.get("arguments", []),
                    "call_stack": record.get("site", []),
                    "parents": node["parents"],
                    "children": node["children"],
                    "inputs": list(dict.fromkeys(inputs)),
                    "consumers": consumers,
                    "published_by": published,
                    "child_builder": child,
                    "observed_parameters": self._observed(node),
                }
            )
        return {
            "part": self.part,
            "source_revision": self.revision,
            "component": self.reference(reference),
            "definition": self.index.definition(definition) if definition else None,
            "invocations": invocations,
            "source_scope": "captured files only; no live source was read",
        }

    def follow_parameter(self, component: str, expression: str) -> Json:
        reference = self._resolve(component)
        definition = self.nodes[reference]["definition"] if reference in self.nodes else reference
        if not definition:
            raise LookupError("this captured component has no source definition; parameter analysis is unavailable")
        scope = self._constructor(definition)
        result = self.index.follow_parameter(scope, expression)
        roots = self._occurrences(reference)
        pending = list(roots)
        seen: set[str] = set()
        values = []
        observed_invocations: set[int | None] = set()
        observed_values = []
        while pending:
            occurrence = pending.pop()
            if occurrence in seen:
                continue
            seen.add(occurrence)
            node = self.nodes[occurrence]
            same_scope = node["definition"] == scope
            invocation = node.get("observed_invocation")
            observed = self._observed(node) if same_scope and invocation not in observed_invocations else None
            if observed:
                observed_invocations.add(invocation)
                for parameter in observed.get("parameters", []):
                    name = parameter["name"]
                    if expression == name:
                        observed_values.append(
                            {
                                "component": occurrence,
                                "expression": name,
                                "value": parameter.get("value"),
                                "capture": observed["capture"],
                            }
                        )
                    elif expression.startswith(name + ".") and expression[len(name) + 1 :] in parameter.get(
                        "fields", {}
                    ):
                        observed_values.append(
                            {
                                "component": occurrence,
                                "expression": expression,
                                "value": parameter["fields"][expression[len(name) + 1 :]],
                                "capture": observed["capture"],
                            }
                        )
            arguments = node["record"].get("arguments", []) if same_scope else []
            for argument in arguments:
                source = argument.get("source")
                # Only directly evidenced expression/value pairs; declared defaults are never runtime values.
                if source and (source == expression or self._contains_expression(source, expression)):
                    values.append({"component": occurrence, "operation": node["runtime_alias"], **argument})
            pending.extend(node["children"])
            if node["kind"] == "builder":
                published = self.aliases.get(f"o{node['record'].get('published_by')}")
                if published:
                    pending.append(published)
        return {
            **result,
            "part": self.part,
            "source_revision": self.revision,
            "component": self.reference(reference),
            "captured_operation_values": values,
            "observed_parameter_values": observed_values,
        }

    @staticmethod
    def _contains_expression(source: str, expression: str) -> bool:
        try:
            target = ast.dump(ast.parse(expression, mode="eval").body, include_attributes=False)
            return any(
                ast.dump(node, include_attributes=False) == target for node in ast.walk(ast.parse(source, mode="eval"))
            )
        except SyntaxError:
            return False

    def related(self, component: str) -> Json:
        reference = self._resolve(component)
        definition = self.nodes[reference]["definition"] if reference in self.nodes else reference
        if not definition:
            raise LookupError("this captured component has no source definition; related-code analysis is unavailable")
        return {
            **self.index.related(definition),
            "part": self.part,
            "source_revision": self.revision,
            "component": self.reference(reference),
        }
