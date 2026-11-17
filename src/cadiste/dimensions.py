"""Bounded numeric evidence over SourceIndex; never execute captured Python."""

from __future__ import annotations

import ast
import math
import operator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .code_index import SourceIndex

_UNKNOWN = object()
_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def _number(value: Any) -> bool:
    return (type(value) is int and value.bit_length() <= 4096) or (type(value) is float and math.isfinite(value))


def observed_number(value: Any) -> Any:
    """Read a captured numeric repr, not arbitrary literals, objects or expressions."""
    if _number(value):
        return value
    if not isinstance(value, str) or len(value) > 1500:
        return _UNKNOWN
    try:
        node = ast.parse(value, mode="eval").body
    except (SyntaxError, ValueError, RecursionError):
        return _UNKNOWN
    sign = 1
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        sign = -1 if isinstance(node.op, ast.USub) else 1
        node = node.operand
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and _number(node.value):
        return sign * node.value
    return _UNKNOWN


@dataclass
class _Value:
    value: Any = _UNKNOWN
    drivers: list[dict[str, Any]] = field(default_factory=list)


class DimensionAnalysis:
    def __init__(self, index: SourceIndex, observed: dict[tuple[str, str], dict[str, Any]] | None = None):
        self.index = index
        self.observed = observed
        self.branches: list[dict[str, Any]] = []
        self.boundaries: list[dict[str, Any]] = []
        self.steps = 0

    def boundary(self, scope: str, node: ast.AST, reason: str, **extra: Any) -> _Value:
        self.boundaries.append({"owner": scope, "expression": ast.unparse(node), "reason": reason, **extra})
        return _Value()

    def analyze(self, reference: str, expression: str, limit: int) -> dict[str, Any]:
        self.index.query_options(limit, 0, None)
        file, scope = self.index._owner(reference)
        try:
            node = ast.parse(expression, mode="eval").body
        except (SyntaxError, ValueError, RecursionError) as error:
            raise ValueError(f"expected a Python expression: {expression}") from error
        value = self.visit(file, scope, node, (), {}, 0, query=True)
        result: dict[str, Any] = {
            "status": "resolved" if value.value is not _UNKNOWN else "unresolved",
            "basis": (
                "captured declarations and scoped observations"
                if self.observed is not None
                else "captured declarations only"
            ),
            "pages": {},
        }
        if value.value is not _UNKNOWN:
            result["value"] = value.value
        for name, records in (("drivers", value.drivers), ("branches", self.branches), ("boundaries", self.boundaries)):
            unique = []
            for record in records:
                if record not in unique:
                    unique.append(record)
            result[name], result["pages"][name] = self.index.page(
                unique, limit=limit, offset=0, render=lambda record: self.compact(record, limit)
            )
        return result

    def compact(self, record: dict[str, Any], limit: int) -> dict[str, Any]:
        result = {}
        for key, value in record.items():
            if isinstance(value, list):
                result[key], metadata = self.index.page(
                    value,
                    limit=limit,
                    offset=0,
                    render=lambda item: self.compact(item, limit) if isinstance(item, dict) else item,
                )
                if metadata["omitted"]:
                    result[key + "_page"] = metadata
            elif isinstance(value, dict):
                result[key] = self.compact(value, limit)
            elif isinstance(value, str) and len(value) > 240:
                result[key] = value[:240]
                result[key + "_omitted_characters"] = len(value) - 240
            else:
                result[key] = value
        return result

    def builtin(self, scope: str, name: str) -> bool:
        original = scope
        while scope:
            if (scope, name) in self.index._bindings or (scope, name) in self.index._external_scopes:
                return False
            parent = self.index._parents.get(scope)
            if parent and self.index._symbols[parent].kind == "class" and original != parent:
                parent = self.index._parents.get(parent)
            scope = parent or ""
        return True

    def visit(self, file, scope, node, trail, substitutions, depth, *, query=False) -> _Value:
        self.steps += 1
        if depth >= 64 or self.steps > 1000:
            return self.boundary(scope, node, "numeric analysis budget exceeded")
        text = ast.unparse(node)
        line = None if query else getattr(node, "lineno", None)
        if isinstance(node, ast.Name) and node.id in substitutions:
            f, s, replacement = substitutions[node.id]
            return self.visit(f, s, replacement, trail, {}, depth + 1, query=True)
        if isinstance(node, ast.Attribute):
            # A property is evaluated against this exact receiver, never a same-typed sibling instance.
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in substitutions:
                f, s, replacement = substitutions[root.id]
                suffix = text[len(root.id) :]
                replacement = ast.parse(f"({ast.unparse(replacement)}){suffix}", mode="eval").body
                return self.visit(f, s, replacement, trail, {}, depth + 1, query=True)
        if self.observed is not None and (scope, text) in self.observed:
            evidence = self.observed[scope, text]
            number = observed_number(evidence.get("value"))
            if number is _UNKNOWN:
                return self.boundary(scope, node, "captured value is not a finite numeric literal", evidence=evidence)
            return _Value(
                number,
                [{**evidence, "owner": scope, "expression": text, "value": number, "evidence": "observed"}],
            )
        if isinstance(node, ast.Constant) and _number(node.value):
            return _Value(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            result = self.visit(file, scope, node.operand, trail, substitutions, depth + 1, query=query)
            if result.value is not _UNKNOWN:
                result.value = -result.value if isinstance(node.op, ast.USub) else result.value
            return result
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left = self.visit(file, scope, node.left, trail, substitutions, depth + 1, query=query)
            right = self.visit(file, scope, node.right, trail, substitutions, depth + 1, query=query)
            drivers = left.drivers + right.drivers
            if left.value is _UNKNOWN or right.value is _UNKNOWN:
                return _Value(drivers=drivers)
            if isinstance(node.op, ast.Pow) and (abs(right.value) > 64 or abs(left.value) > 1e100):
                return self.boundary(scope, node, "power exceeds numeric analysis bounds")
            try:
                number = _BINARY[type(node.op)](left.value, right.value)
            except (ArithmeticError, ValueError, TypeError):
                return self.boundary(scope, node, "arithmetic is undefined for these captured values")
            if not _number(number):
                return self.boundary(scope, node, "result is not a bounded finite real number")
            return _Value(number, drivers)
        if isinstance(node, ast.Call):
            if not (
                isinstance(node.func, ast.Name)
                and node.func.id in {"min", "max"}
                and self.builtin(scope, node.func.id)
                and not node.keywords
                and len(node.args) >= 2
                and not any(isinstance(arg, ast.Starred) for arg in node.args)
            ):
                return self.boundary(scope, node, "call is unsupported or min/max is shadowed; no code was executed")
            if len(node.args) > 1000:
                return self.boundary(scope, node, "min/max argument count exceeds numeric analysis bounds")
            values = [self.visit(file, scope, arg, trail, substitutions, depth + 1, query=query) for arg in node.args]
            branch = {"owner": scope, "expression": text, "operation": node.func.id, "status": "unresolved"}
            self.branches.append(branch)
            if any(value.value is _UNKNOWN for value in values):
                return _Value(drivers=[driver for value in values for driver in value.drivers])
            chosen = (min if node.func.id == "min" else max)(value.value for value in values)
            active = [i for i, value in enumerate(values) if value.value == chosen]
            branch.update(status="resolved", value=chosen, active_arguments=active, tied=len(active) > 1)
            branch["arguments"] = [
                {"position": i, "expression": ast.unparse(arg), "value": value.value, "active": i in active}
                for i, (arg, value) in enumerate(zip(node.args, values, strict=True))
            ]
            return _Value(chosen, [driver for i in active for driver in values[i].drivers])
        if isinstance(node, (ast.Name, ast.Attribute)):
            if isinstance(node, ast.Name):
                bindings = self.index._lookup(scope, node.id, line)
            else:
                root = node.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                if not isinstance(root, ast.Name):
                    return self.boundary(scope, node, "receiver depends on unsupported dynamic syntax")
                receivers = self.index._lookup(scope, root.id, line)
                if len(receivers) != 1 or receivers[0].conditional:
                    return self.boundary(scope, node, "receiver has no unique unconditional captured binding")
                refs = self.index._resolve(file, scope, node, line)
                bindings = [self.index._symbols[ref] for ref in sorted(refs) if ref in self.index._symbols]
                bindings = [binding for symbol in bindings for binding in [symbol, *symbol.variants]]
            if len(bindings) != 1:
                return self.boundary(
                    scope, node, "no unique captured binding", references=sorted({s.reference for s in bindings})
                )
            symbol = bindings[0]
            if symbol.conditional:
                return self.boundary(
                    scope, node, "binding depends on conditional control flow", reference=symbol.reference
                )
            if symbol.imported:
                refs = self.index._import_refs(symbol, frozenset())
                if len(refs) != 1:
                    return self.boundary(scope, node, "import has no unique captured binding", references=sorted(refs))
                symbol = self.index._symbols[next(iter(refs))]
            if symbol.reference in trail:
                return self.boundary(scope, node, "dependency cycle", path=[*trail, symbol.reference])
            if symbol.conditional:
                return self.boundary(
                    scope, node, "binding depends on conditional control flow", reference=symbol.reference
                )
            if self.observed is not None and symbol.kind in {"parameter", "field"}:
                # A field default is not evidence that this particular runtime receiver kept that default.
                return self.boundary(
                    scope,
                    node,
                    "runtime parameter or field has no scoped observation; declared defaults are separate",
                    reference=symbol.reference,
                )
            next_trail = (*trail, symbol.reference)
            if symbol.kind == "property":
                if not isinstance(symbol.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return self.boundary(
                        scope, node, "property has no captured function body", reference=symbol.reference
                    )
                returns = self.index._returns(symbol.node)
                direct = [
                    stmt.value for stmt in symbol.node.body if isinstance(stmt, ast.Return) and stmt.value is not None
                ]
                if len(returns) != 1 or direct != returns or symbol.variants:
                    return self.boundary(
                        scope, node, "property returns depend on conditional control flow", reference=symbol.reference
                    )
                mapping = {}
                if isinstance(node, ast.Attribute):
                    mapping["self"] = (file, scope, node.value)
                return self.visit(symbol.file, symbol.reference, returns[0], next_trail, mapping, depth + 1)
            if symbol.value is None or symbol.kind not in {"parameter", "field", "assignment"}:
                return self.boundary(
                    scope, node, "binding has no supported numeric declaration", reference=symbol.reference
                )
            result = self.visit(
                symbol.file,
                symbol.value_scope or symbol.scope,
                symbol.value,
                next_trail,
                substitutions,
                depth + 1,
            )
            if not result.drivers and result.value is not _UNKNOWN:
                result.drivers.append(
                    {
                        "reference": symbol.reference,
                        "owner": scope,
                        "expression": text,
                        "value": result.value,
                        "evidence": (
                            "declared default" if symbol.kind in {"parameter", "field"} else "captured declaration"
                        ),
                    }
                )
            return result
        return self.boundary(scope, node, "unsupported numeric syntax; no value was guessed")
