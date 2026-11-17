"""Conservative, non-executing navigation of a captured Python source bundle.

Bindings and evidence belong to the captured revision, never to the live filesystem.
An unresolved expression is useful information: this index does not infer a connection
from a matching attribute name, nor import a model to discover a dynamic value.
"""

from __future__ import annotations

import ast
import itertools
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .source import code_ref, source_revision, split_ref


@dataclass
class _Symbol:
    reference: str
    file: str
    qualified: str
    kind: str
    node: ast.AST
    scope: str
    value: ast.AST | None = None
    annotation: ast.AST | None = None
    conditional: bool = False
    imported: tuple[str, str | None] | None = None
    variants: list[_Symbol] = field(default_factory=list)
    value_scope: str | None = None


class SourceIndex:
    """Parse once; resolve only relationships supported by captured Python syntax."""

    def __init__(self, sources: Mapping[str, str]):
        self.sources = dict(sources)
        self.revision = source_revision(sources)
        self.definitions: dict[str, dict[str, Any]] = {}
        self.errors: list[dict[str, Any]] = []
        self._symbols: dict[str, _Symbol] = {}
        self._bindings: dict[tuple[str, str], list[_Symbol]] = defaultdict(list)
        self._parents: dict[str, str | None] = {}
        self._scopes: dict[int, str] = {}
        self._calls: list[tuple[str, str, ast.Call]] = []
        self._trees: dict[str, ast.Module] = {}
        self._modules: dict[str, list[str]] = defaultdict(list)
        self._file_modules: dict[str, str] = {}
        self._usage_cache: list[dict[str, Any]] | None = None
        self._initializer_cache: dict[str, list[dict[str, Any]]] = {}
        self._dependency_cache: dict[str, list[dict[str, Any]]] = {}
        self._external_scopes: set[tuple[str, str]] = set()
        self._argument_calls: dict[int, list[tuple[str, str, ast.Call, int | str | None]]] = defaultdict(list)
        self._call_cache: dict[int, dict[str, Any]] = {}
        for file, text in self.sources.items():
            path = PurePosixPath(file)
            pieces = list(path.with_suffix("").parts)
            if pieces and pieces[0] == "/":
                pieces.pop(0)
            if pieces and pieces[-1] == "__init__":
                pieces.pop()
            module = ".".join(pieces)
            self._file_modules[file] = module
            for start in range(len(pieces)):
                self._modules[".".join(pieces[start:])].append(file)
            try:
                self._trees[file] = ast.parse(text, filename=file)
            except SyntaxError as error:
                self.errors.append({"file": file, "line": error.lineno, "reason": str(error)})
        for file, tree in self._trees.items():
            scope = code_ref(file, "<module>")
            self._parents[scope] = None
            self._symbols[scope] = _Symbol(scope, file, "<module>", "module", tree, scope)
            self._visit(file, tree, scope)
        for file, owner, call in self._calls:
            arguments: list[tuple[int | str | None, ast.AST]] = [(i, argument) for i, argument in enumerate(call.args)]
            arguments += [(keyword.arg, keyword.value) for keyword in call.keywords]
            for argument_name, argument in arguments:
                for node in ast.walk(argument):
                    self._argument_calls[id(node)].append((file, owner, call, argument_name))
        for reference, symbol in self._symbols.items():
            self.definitions[reference] = self._describe(symbol)

    def _text(self, file: str, node: ast.AST | None) -> str:
        if isinstance(node, ast.Module):
            return self.sources[file]
        return (ast.get_source_segment(self.sources[file], node) or ast.unparse(node)) if node else ""

    def _add(
        self,
        file: str,
        scope: str,
        name: str,
        kind: str,
        node: ast.AST,
        *,
        value: ast.AST | None = None,
        annotation: ast.AST | None = None,
        conditional: bool = False,
        imported: tuple[str, str | None] | None = None,
        value_scope: str | None = None,
    ) -> _Symbol:
        parent = split_ref(scope)[1]
        qualified = name if parent == "<module>" else f"{parent}.{name}"
        reference = code_ref(file, qualified)
        symbol = _Symbol(reference, file, qualified, kind, node, scope, value, annotation, conditional, imported)
        symbol.value_scope = value_scope
        self._bindings[scope, name].append(symbol)
        if reference in self._symbols:
            self._symbols[reference].variants.append(symbol)
        else:
            self._symbols[reference] = symbol
        return symbol

    def _visit(self, file: str, node: ast.AST, scope: str, conditional: bool = False) -> None:
        self._scopes[id(node)] = scope
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            self.errors.append(
                {
                    "file": file,
                    "line": node.lineno,
                    "expression": self._text(file, node),
                    "reason": "anonymous expression scope is not statically expanded",
                }
            )
            return
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            self._external_scopes.update((scope, name) for name in node.names)
            self.errors.append(
                {
                    "file": file,
                    "line": node.lineno,
                    "expression": self._text(file, node),
                    "reason": "global/nonlocal mutations are not propagated across invocations",
                }
            )
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            parent = self._symbols.get(scope)
            if kind == "function" and parent and parent.kind == "class":
                kind = (
                    "property"
                    if any(isinstance(d, ast.Name) and d.id == "property" for d in node.decorator_list)
                    else "method"
                )
            symbol = self._add(file, scope, node.name, kind, node, conditional=conditional)
            self._parents[symbol.reference] = scope
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                positional = [*node.args.posonlyargs, *node.args.args]
                defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
                args = list(zip(positional, defaults, strict=True)) + list(
                    zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
                )
                args += [(arg, None) for arg in (node.args.vararg, node.args.kwarg) if arg]
                for arg, default in args:
                    self._add(
                        file,
                        symbol.reference,
                        arg.arg,
                        "parameter",
                        arg,
                        value=default,
                        annotation=arg.annotation,
                        value_scope=scope,
                    )
                    if default:
                        self._visit(file, default, scope)
                    if arg.annotation:
                        self._visit(file, arg.annotation, scope)
                if node.returns:
                    self._visit(file, node.returns, scope)
            else:
                for base in node.bases:
                    self._visit(file, base, scope)
                for keyword in node.keywords:
                    self._visit(file, keyword.value, scope)
            for decorator in node.decorator_list:
                self._visit(file, decorator, scope)
            for statement in node.body:
                self._visit(file, statement, symbol.reference)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if isinstance(node, ast.Import):
                    name = alias.asname or alias.name.split(".")[0]
                    imported = (alias.name if alias.asname else name, None)
                else:
                    name = alias.asname or alias.name
                    module = node.module or ""
                    if node.level:
                        current = self._file_modules[file].split(".")
                        if PurePosixPath(file).name != "__init__.py":
                            current = current[:-1]
                        keep = len(current) - node.level + 1
                        module = ".".join([*current[: max(0, keep)], *([module] if module else [])])
                    imported = (module, alias.name)
                self._add(file, scope, name, "import", node, conditional=conditional, imported=imported)
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                self._bind_target(
                    file,
                    scope,
                    target,
                    node,
                    node.value,
                    node.annotation if isinstance(node, ast.AnnAssign) else None,
                    conditional,
                )
        if isinstance(node, (ast.For, ast.AsyncFor)):
            self._bind_target(file, scope, node.target, node, None, None, True)
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars:
                    self._bind_target(file, scope, item.optional_vars, node, None, None, conditional)
        if isinstance(node, ast.ExceptHandler) and node.name:
            self._add(file, scope, node.name, "assignment", node, conditional=True)
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            self._add(file, scope, node.name, "assignment", node, conditional=True)
        if isinstance(node, ast.MatchMapping) and node.rest:
            self._add(file, scope, node.rest, "assignment", node, conditional=True)
        if isinstance(node, ast.AugAssign):
            self._bind_target(file, scope, node.target, node, None, None, conditional)
        if isinstance(node, ast.Delete):
            for target in node.targets:
                self._bind_target(file, scope, target, node, None, None, conditional)
        if isinstance(node, ast.Call):
            self._calls.append((file, scope, node))
        for child in ast.iter_child_nodes(node):
            self._visit(
                file, child, scope, conditional or isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.Match))
            )

    def _bind_target(
        self,
        file: str,
        scope: str,
        target: ast.AST,
        node: ast.AST,
        value: ast.AST | None,
        annotation: ast.AST | None,
        conditional: bool,
    ) -> None:
        if isinstance(target, ast.Name):
            owner = self._symbols.get(scope)
            kind = "field" if owner and owner.kind == "class" else "assignment"
            self._add(file, scope, target.id, kind, node, value=value, annotation=annotation, conditional=conditional)
        elif isinstance(target, (ast.Tuple, ast.List)):
            values = (
                value.elts if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts) else None
            )
            for i, element in enumerate(target.elts):
                self._bind_target(file, scope, element, node, values[i] if values else None, None, conditional)
        elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
            owner = self._parents.get(scope)
            if owner and owner in self._symbols and self._symbols[owner].kind == "class":
                self._add(
                    file,
                    owner,
                    target.attr,
                    "field",
                    node,
                    value=value,
                    annotation=annotation,
                    conditional=conditional,
                    value_scope=scope,
                )

    def _lookup(self, scope: str, name: str, line: int | None = None) -> list[_Symbol]:
        original = scope
        while scope:
            if (scope, name) in self._external_scopes:
                return []
            bindings = self._bindings.get((scope, name), [])
            if bindings:
                # Python locals shadow an outer name even before their assignment.
                available = [s for s in bindings if line is None or getattr(s.node, "lineno", 0) <= line]
                if not available:
                    return []
                last_unconditional = next(
                    (i for i in range(len(available) - 1, -1, -1) if not available[i].conditional), -1
                )
                return available[max(0, last_unconditional) :]
            parent = self._parents.get(scope)
            # A method does not close over its class body.
            if parent and parent in self._symbols and self._symbols[parent].kind == "class" and original != parent:
                parent = self._parents.get(parent)
            scope = parent or ""
            line = None
        return []

    def _module_files(self, module: str) -> list[str]:
        exact = [file for file, name in self._file_modules.items() if name == module]
        return exact or list(dict.fromkeys(self._modules.get(module, [])))

    def _import_refs(self, symbol: _Symbol, seen: frozenset[str]) -> set[str]:
        if symbol.reference in seen or not symbol.imported:
            return set()
        module, name = symbol.imported
        files = self._module_files(module)
        result: set[str] = set()
        if name is None:
            return {code_ref(file, "<module>") for file in files}
        for file in files:
            for binding in self._lookup(code_ref(file, "<module>"), name):
                result.update(self._binding_refs(binding, seen | {symbol.reference}))
        # `from package import submodule` can name a module rather than a binding.
        if not result:
            result.update(code_ref(file, "<module>") for file in self._module_files(f"{module}.{name}"))
        return result

    def _binding_refs(self, symbol: _Symbol, seen: frozenset[str]) -> set[str]:
        if symbol.reference in seen:
            return {symbol.reference}
        if symbol.imported:
            return self._import_refs(symbol, seen)
        if symbol.kind == "assignment" and isinstance(symbol.value, (ast.Name, ast.Attribute)):
            resolved = self._resolve(
                symbol.file, symbol.scope, symbol.value, getattr(symbol.node, "lineno", None), seen | {symbol.reference}
            )
            if resolved:
                return resolved
        return {symbol.reference}

    def _resolve(
        self, file: str, scope: str, node: ast.AST, line: int | None = None, seen: frozenset[str] = frozenset()
    ) -> set[str]:
        if isinstance(node, ast.Name):
            return set().union(*(self._binding_refs(s, seen) for s in self._lookup(scope, node.id, line)))
        if isinstance(node, ast.Attribute):
            result: set[str] = set()
            bases = self._resolve(file, scope, node.value, line, seen)
            for base in bases:
                if base.endswith("::<module>"):
                    base_file, _ = split_ref(base)
                    for binding in self._lookup(code_ref(base_file, "<module>"), node.attr):
                        result.update(self._binding_refs(binding, seen))
                    module = self._file_modules[base_file]
                    result.update(code_ref(f, "<module>") for f in self._module_files(f"{module}.{node.attr}"))
            for owner in self._types(file, scope, node.value, line, seen):
                result.update(self._members(owner, node.attr, seen))
            return result
        if isinstance(node, ast.Call):
            return self._resolve(file, scope, node.func, line, seen)
        if isinstance(node, ast.IfExp):
            return self._resolve(file, scope, node.body, line, seen) | self._resolve(
                file, scope, node.orelse, line, seen
            )
        return set()

    def _members(self, owner: str, name: str, seen: frozenset[str]) -> set[str]:
        if owner in seen:
            return set()
        bindings = self._bindings.get((owner, name), [])
        if bindings:
            return {symbol.reference for symbol in bindings}
        symbol = self._symbols.get(owner)
        result: set[str] = set()
        if symbol and isinstance(symbol.node, ast.ClassDef):
            for base in symbol.node.bases:
                for parent in self._resolve(symbol.file, symbol.scope, base, seen=seen | {owner}):
                    result.update(self._members(parent, name, seen | {owner}))
        return result

    def syntax_tree(self, file: str) -> ast.Module | None:
        """Return the already parsed captured tree; consumers must not mutate it."""
        return self._trees.get(file)

    def _annotation_scope(self, symbol: _Symbol) -> str:
        return (self._parents.get(symbol.scope) or symbol.scope) if symbol.kind == "parameter" else symbol.scope

    def _typing_form(self, symbol: _Symbol, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            imports = [binding.imported for binding in self._lookup(self._annotation_scope(symbol), node.id)]
            if len(imports) == 1 and imports[0] and imports[0][0] in {"typing", "typing_extensions"}:
                return imports[0][1]
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            imports = [binding.imported for binding in self._lookup(self._annotation_scope(symbol), node.value.id)]
            if len(imports) == 1 and imports[0] in {("typing", None), ("typing_extensions", None)}:
                return node.attr
        return None

    def _annotation_types(self, symbol: _Symbol, annotation: ast.AST, seen: frozenset[str]) -> set[str]:
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return set()
        if self._typing_form(symbol, annotation) == "Self":
            scope = symbol.scope
            while scope in self._symbols:
                if self._symbols[scope].kind == "class":
                    return {scope}
                if self._symbols[scope].kind == "module":
                    break
                scope = self._symbols[scope].scope
            return set()
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return self._annotation_types(symbol, annotation.left, seen) | self._annotation_types(
                symbol, annotation.right, seen
            )
        if isinstance(annotation, ast.Subscript):
            # Only unions/optional wrappers denote a value of one of their arguments.
            form = self._typing_form(symbol, annotation.value)
            if form in {"Optional", "Union", "Annotated"}:
                args = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
                if form == "Annotated":
                    args = args[:1]
                return set().union(*(self._annotation_types(symbol, arg, seen) for arg in args))
            return self._resolve(symbol.file, self._annotation_scope(symbol), annotation.value, seen=seen)
        return {
            ref
            for ref in self._resolve(symbol.file, self._annotation_scope(symbol), annotation, seen=seen)
            if ref in self._symbols and self._symbols[ref].kind == "class"
        }

    def _types(self, file: str, scope: str, node: ast.AST, line: int | None, seen: frozenset[str]) -> set[str]:
        result: set[str] = set()
        for reference in self._resolve(file, scope, node, line, seen):
            if reference in seen:
                continue
            symbol = self._symbols.get(reference)
            if not symbol:
                continue
            visited = seen | {reference}
            if symbol.kind == "class":
                result.add(reference)
                continue
            if symbol.kind == "parameter" and symbol.qualified.rsplit(".", 1)[-1] in {"self", "cls"}:
                owner = self._parents.get(symbol.scope)
                if owner and owner in self._symbols and self._symbols[owner].kind == "class":
                    result.add(owner)
            for binding in [symbol, *symbol.variants]:
                if binding.annotation:
                    result.update(self._annotation_types(binding, binding.annotation, visited))
                if binding.value:
                    result.update(
                        self._types(
                            binding.file,
                            binding.value_scope or binding.scope,
                            binding.value,
                            getattr(binding.node, "lineno", None),
                            visited,
                        )
                    )
            if isinstance(symbol.node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                isinstance(node, ast.Call) or symbol.kind == "property"
            ):
                if symbol.node.returns:
                    result.update(self._annotation_types(symbol, symbol.node.returns, visited))
                else:
                    for returned in self._returns(symbol.node):
                        result.update(
                            self._types(
                                symbol.file, symbol.reference, returned, getattr(returned, "lineno", None), visited
                            )
                        )
        return result

    @staticmethod
    def _returns(node: ast.AST) -> list[ast.AST]:
        values: list[ast.AST] = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Return) and child.value:
                values.append(child.value)
            elif not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                values.extend(SourceIndex._returns(child))
        return values

    def _describe(self, symbol: _Symbol) -> dict[str, Any]:
        node = symbol.node
        result: dict[str, Any] = {
            "reference": symbol.reference,
            "file": symbol.file,
            "qualified": symbol.qualified,
            "kind": symbol.kind,
            "line": getattr(node, "lineno", 1),
            "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
            "source": self._text(symbol.file, node),
            "source_revision": self.revision,
            "parameters": [],
            "calls": [],
        }
        if symbol.kind == "module":
            result["end_line"] = len(self.sources[symbol.file].splitlines())
        if symbol.annotation:
            result["annotation"] = self._text(symbol.file, symbol.annotation)
        if symbol.value:
            result["declared_default" if symbol.kind in {"parameter", "field"} else "expression"] = self._text(
                symbol.file, symbol.value
            )
        if symbol.kind == "field":
            result["initializers"] = [
                {key: value for key, value in entry.items() if key != "node"} for entry in self._initializers(symbol)
            ]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for (scope, _), bindings in self._bindings.items():
                if scope == symbol.reference:
                    for param in bindings:
                        if param.kind == "parameter":
                            result["parameters"].append(
                                {
                                    "reference": param.reference,
                                    "name": param.qualified.rsplit(".", 1)[-1],
                                    "annotation": self._text(param.file, param.annotation) or None,
                                    "declared_default": self._text(param.file, param.value) if param.value else None,
                                    "file": param.file,
                                    "line": getattr(param.node, "lineno", 1),
                                }
                            )
            if node.returns:
                result["returns"] = self._text(symbol.file, node.returns)
        for file, owner, call in self._calls:
            if owner == symbol.reference:
                result["calls"].append(self._call(file, owner, call))
        if symbol.variants:
            result["bindings"] = [
                {
                    "file": s.file,
                    "line": getattr(s.node, "lineno", 1),
                    "expression": self._text(s.file, s.value),
                    "conditional": s.conditional,
                }
                for s in [symbol, *symbol.variants]
            ]
        return result

    def _call(self, file: str, owner: str, call: ast.Call) -> dict[str, Any]:
        if id(call) in self._call_cache:
            return self._call_cache[id(call)]
        candidates = self._resolve(file, owner, call.func, call.lineno)
        targets = {
            ref
            for ref in candidates
            if ref in self._symbols and self._symbols[ref].kind in {"class", "function", "method"}
        }
        if isinstance(call.func, ast.Name) and call.func.id == "cls":
            targets.update(self._types(file, owner, call.func, call.lineno, frozenset()))
        result = {
            "file": file,
            "line": call.lineno,
            "owner": owner,
            "expression": self._text(file, call),
            "targets": sorted(targets),
            "unresolved": None if targets else "callable has no captured static definition",
            "arguments": [
                {"name": None, "position": i, "expression": self._text(file, arg)} for i, arg in enumerate(call.args)
            ]
            + [{"name": keyword.arg, "expression": self._text(file, keyword.value)} for keyword in call.keywords],
        }
        self._call_cache[id(call)] = result
        return result

    def definition(self, reference: str) -> dict[str, Any]:
        split_ref(reference)
        if reference not in self.definitions:
            raise LookupError(f"definition is not in captured revision {self.revision}: {reference}")
        return dict(self.definitions[reference])

    def at(self, file: str, line: int, qualified: str | None = None) -> str | None:
        if file not in self.sources:
            return None
        if qualified:
            direct = code_ref(file, qualified.replace(".<locals>.", "."))
            if direct in self._symbols:
                return direct
        containing = [
            s
            for s in self._symbols.values()
            if s.file == file
            and s.kind in {"class", "function", "method", "property"}
            and getattr(s.node, "lineno", 1) <= line <= getattr(s.node, "end_lineno", 0)
        ]
        return max(containing, key=lambda s: s.qualified.count(".")).reference if containing else None

    def _owner(self, reference: str) -> tuple[str, str]:
        self.definition(reference)
        symbol = self._symbols[reference]
        scope = reference if symbol.kind in {"function", "method", "property", "class"} else symbol.scope
        return symbol.file, scope

    def resolve(self, file: str, qualified: str, expression: str, line: int | None = None) -> list[str]:
        if file not in self.sources:
            raise LookupError(f"source is not captured: {file}")
        scope = code_ref(file, qualified or "<module>")
        if scope in self._symbols and self._symbols[scope].kind not in {"class", "function", "method", "property"}:
            scope = self._symbols[scope].scope
        try:
            node = ast.parse(expression, mode="eval").body
        except SyntaxError as error:
            raise ValueError(f"expected a Python expression: {expression}") from error
        return sorted(ref for ref in self._resolve(file, scope, node, line) if ref in self._symbols)

    def _expression_refs(self, file: str, scope: str, node: ast.AST, *, query: bool = False) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []

        def visit(expression: ast.AST) -> None:
            if isinstance(expression, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                result.append(
                    {
                        "expression": ast.unparse(expression) if query else self._text(file, expression),
                        "file": file,
                        "line": getattr(expression, "lineno", 1),
                        "owner": scope,
                        "targets": [],
                        "reason": "anonymous expression scope is not statically expanded",
                    }
                )
                return
            if isinstance(expression, (ast.Attribute, ast.Name)) and isinstance(expression.ctx, ast.Load):
                line = None if query else getattr(expression, "lineno", None)
                if isinstance(expression, ast.Name):
                    bindings = self._lookup(scope, expression.id, line)
                    refs = set().union(
                        *(self._import_refs(s, frozenset()) if s.imported else {s.reference} for s in bindings)
                    )
                else:
                    refs = self._resolve(file, scope, expression, line)
                result.append(
                    {
                        "expression": ast.unparse(expression) if query else self._text(file, expression),
                        "file": file,
                        "line": getattr(self._symbols[scope].node, "lineno", 1)
                        if query and scope in self._symbols
                        else getattr(expression, "lineno", 1),
                        "owner": scope,
                        "targets": sorted(refs),
                        "ambiguous_bindings": [
                            {"name": name.id, "references": sorted({binding.reference for binding in candidates})}
                            for name in ast.walk(expression)
                            if isinstance(name, ast.Name)
                            if len(candidates := self._lookup(scope, name.id, line)) > 1
                        ],
                        "consumer_calls": [
                            {**self._call(f, owner, call), "argument": argument}
                            for f, owner, call, argument in self._argument_calls.get(id(expression), [])
                        ],
                    }
                )
                # An attribute denotes one exact dependency; its base's type is not a value dependency.
                # Unresolved attributes remain unresolved, rather than silently becoming a base-name match.
                return
            for child in ast.iter_child_nodes(expression):
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    visit(child)

        visit(node)
        return result

    def _dependencies(self, reference: str) -> list[dict[str, Any]]:
        if reference in self._dependency_cache:
            return self._dependency_cache[reference]
        symbol = self._symbols[reference]
        result: list[dict[str, Any]] = []
        for binding in [symbol, *symbol.variants]:
            if binding.value:
                result.extend(self._expression_refs(binding.file, binding.value_scope or binding.scope, binding.value))
            if isinstance(binding.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for expression in self._returns(binding.node):
                    result.extend(self._expression_refs(binding.file, binding.reference, expression))
        if symbol.kind == "field":
            for initializer in self._initializers(symbol):
                result.extend(self._expression_refs(initializer["file"], initializer["owner"], initializer["node"]))
        self._dependency_cache[reference] = result
        return result

    def _initializers(self, symbol: _Symbol) -> list[dict[str, Any]]:
        if symbol.reference in self._initializer_cache:
            return self._initializer_cache[symbol.reference]
        owner = self._symbols.get(symbol.scope)
        if not owner or not isinstance(owner.node, ast.ClassDef):
            return []
        decorators = []
        for decorator in owner.node.decorator_list:
            function = decorator.func if isinstance(decorator, ast.Call) else decorator
            bindings = self._lookup(owner.scope, function.id) if isinstance(function, ast.Name) else []
            is_dataclass = any(binding.imported == ("dataclasses", "dataclass") for binding in bindings)
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "dataclass"
                and isinstance(function.value, ast.Name)
            ):
                is_dataclass = any(
                    binding.imported == ("dataclasses", None)
                    for binding in self._lookup(owner.scope, function.value.id)
                )
            if is_dataclass:
                decorators.append(decorator)
        if not decorators:
            return []
        keyword_only = any(
            isinstance(d, ast.Call)
            and any(
                kw.arg == "kw_only" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                for kw in d.keywords
            )
            for d in decorators
        )
        fields = [
            statement.target.id
            for statement in owner.node.body
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
        ]
        name = symbol.qualified.rsplit(".", 1)[-1]
        result: list[dict[str, Any]] = []
        for file, scope, call in self._calls:
            targets = self._resolve(file, scope, call.func, call.lineno)
            direct = owner.reference in targets
            cls_call = isinstance(call.func, ast.Name) and call.func.id == "cls"
            if not direct and not (
                cls_call and owner.reference in self._types(file, scope, call.func, call.lineno, frozenset())
            ):
                continue
            argument = next((kw.value for kw in call.keywords if kw.arg == name), None)
            if (
                argument is None
                and not keyword_only
                and not owner.node.bases
                and name in fields
                and fields.index(name) < len(call.args)
            ):
                argument = call.args[fields.index(name)]
            if argument is not None and not isinstance(argument, ast.Starred):
                result.append(
                    {
                        "file": file,
                        "line": call.lineno,
                        "owner": scope,
                        "node": argument,
                        "expression": self._text(file, argument),
                        "kind": "possible dataclass constructor argument, not an observed runtime value",
                    }
                )
        self._initializer_cache[symbol.reference] = result
        return result

    def _usages(self) -> list[dict[str, Any]]:
        if self._usage_cache is None:
            usages: list[dict[str, Any]] = []
            for file, tree in self._trees.items():

                def visit(node: ast.AST, file: str = file) -> None:
                    if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                        return
                    if isinstance(node, (ast.Attribute, ast.Name)) and isinstance(node.ctx, ast.Load):
                        scope = self._scopes.get(id(node), code_ref(file, "<module>"))
                        usages.extend(self._expression_refs(file, scope, node))
                        if isinstance(node, ast.Name) or self._resolve(
                            file, scope, node, getattr(node, "lineno", None)
                        ):
                            return
                    for child in ast.iter_child_nodes(node):
                        visit(child)

                visit(tree)
            self._usage_cache = usages
        return self._usage_cache

    def _closure(self, references: set[str]) -> tuple[list[dict[str, Any]], list[list[str]], list[dict[str, Any]]]:
        dependencies: dict[str, dict[str, Any]] = {}
        cycles: list[list[str]] = []
        unresolved: list[dict[str, Any]] = []
        completed: set[str] = set()

        def expand(reference: str) -> list[str]:
            targets: list[str] = []
            symbol = self._symbols[reference]
            if symbol.variants and any(s.conditional for s in [symbol, *symbol.variants]):
                unresolved.append(
                    {
                        "reference": reference,
                        "reason": "multiple conditional assignments",
                        "bindings": self.definition(reference).get("bindings", []),
                    }
                )
            if symbol.kind == "assignment" and symbol.value is None and getattr(symbol.node, "value", None) is not None:
                unresolved.append(
                    {
                        "reference": reference,
                        "file": symbol.file,
                        "line": getattr(symbol.node, "lineno", 1),
                        "reason": "destructuring a dynamically determined value is not statically resolved",
                    }
                )
            if (
                symbol.kind == "assignment"
                and symbol.value is None
                and isinstance(
                    symbol.node,
                    (
                        ast.For,
                        ast.AsyncFor,
                        ast.With,
                        ast.AsyncWith,
                        ast.ExceptHandler,
                        ast.MatchAs,
                        ast.MatchStar,
                        ast.MatchMapping,
                        ast.AugAssign,
                        ast.Delete,
                    ),
                )
            ):
                unresolved.append(
                    {
                        "reference": reference,
                        "file": symbol.file,
                        "line": getattr(symbol.node, "lineno", 1),
                        "reason": "binding value depends on control flow or the Python runtime",
                    }
                )
            for use in self._dependencies(reference):
                if not use["targets"]:
                    unresolved.append({**use, "reason": "no captured static binding; dynamic or external expression"})
                elif len(use["targets"]) > 1 or use.get("ambiguous_bindings"):
                    unresolved.append({**use, "reason": "multiple statically possible bindings"})
                for target in use["targets"]:
                    if target not in self._symbols:
                        continue
                    dependencies.setdefault(target, {"reference": target, "evidence": []})["evidence"].append(use)
                    targets.append(target)
            return targets

        # Explicit DFS preserves cycle boundaries without recursion limits or path copying.
        for root in sorted(references):
            if root in completed or root not in self._symbols:
                continue
            trail = [root]
            active = {root: 0}
            stack = [iter(expand(root))]
            while stack:
                target = next(stack[-1], None)
                if target is None:
                    stack.pop()
                    finished = trail.pop()
                    active.pop(finished)
                    completed.add(finished)
                elif target in active:
                    cycle = [*trail[active[target] :], target]
                    if cycle not in cycles:
                        cycles.append(cycle)
                elif target not in completed:
                    active[target] = len(trail)
                    trail.append(target)
                    stack.append(iter(expand(target)))
        return list(dependencies.values()), cycles, unresolved

    @staticmethod
    def query_options(limit: int, offset: int, consumers: str | list[str] | None) -> set[str] | None:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if consumers is None:
            return None
        values = [consumers] if isinstance(consumers, str) else consumers
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            raise ValueError("consumers must be a source reference or a list of source references")
        return set(values)

    @staticmethod
    def _compact(record: dict[str, Any]) -> dict[str, Any]:
        """One evidence example, no source bodies; all elisions are individually counted."""
        result: dict[str, Any] = {}
        for key, value in record.items():
            if (key == "source" and "qualified" in record) or key in {
                "calls",
                "parameters",
                "initializers",
                "bindings",
                "consumer_calls",
            }:
                if value:
                    result[key + "_total"] = len(value)
                    result[key + "_omitted"] = len(value)
            elif isinstance(value, list):
                count = 20 if key == "path" else 1
                result[key] = [SourceIndex._compact(item) if isinstance(item, dict) else item for item in value[:count]]
                if len(value) > count:
                    result[key + "_total"] = len(value)
                    result[key + "_omitted"] = len(value) - count
            elif isinstance(value, str) and len(value) > 240:
                result[key] = value[:240]
                result[key + "_characters"] = len(value)
                result[key + "_omitted_characters"] = len(value) - 240
            else:
                result[key] = value
        return result

    @staticmethod
    def page(records, *, limit: int, offset: int, render=None) -> tuple[list[Any], dict[str, Any]]:
        """Count a stable stream without constructing records outside the requested page."""
        selected = []
        total = 0
        for record in records:
            if offset <= total < offset + limit:
                selected.append(render(record) if render else record)
            total += 1
        returned = len(selected)
        return selected, {
            "total": total,
            "offset": offset,
            "limit": limit,
            "returned": returned,
            "omitted": total - returned,
            "next_offset": offset + returned if offset + returned < total else None,
        }

    def _answer_sections(self, sections, section: str, limit: int, offset: int) -> dict[str, Any]:
        if section != "summary" and section not in sections:
            raise ValueError("section must be summary or one of: " + ", ".join(sections))
        answer: dict[str, Any] = {"section": section, "pages": {}, "available_sections": list(sections)}
        for name, records in sections.items():
            # Evidence expansion is opt-in; defaults never serialize entire declarations or call subgraphs.
            if section != name and (section != "summary" or name == "evidence"):
                continue
            values, page = self.page(
                records(), limit=limit, offset=offset, render=None if name == "evidence" else self._compact
            )
            answer[name] = values
            answer["pages"][name] = page
        return answer

    def _consumer_records(self, roots: set[str], consumers: set[str] | None):
        # Direct consumers only: following a derived dimension must not fan out to every use of its bases.
        for use in self._usages():
            common = roots.intersection(use["targets"])
            if not common:
                continue
            if consumers is None or use["owner"] in consumers:
                yield {**use, "reference": use["owner"], "dependencies": sorted(common)}
            for call in use.get("consumer_calls", []):
                for target in call["targets"]:
                    if consumers is None or target in consumers:
                        yield {
                            "reference": target,
                            "file": call["file"],
                            "line": call["line"],
                            "expression": use["expression"],
                            "argument": call["argument"],
                            "dependencies": sorted(common),
                            "evidence": [call],
                            "value_semantics": "static call argument, not an observed runtime value",
                        }

    def follow_parameter(
        self,
        reference: str,
        expression: str,
        *,
        limit: int = 20,
        offset: int = 0,
        consumers: str | list[str] | None = None,
        section: str = "summary",
    ) -> dict[str, Any]:
        consumer_filter = self.query_options(limit, offset, consumers)
        file, scope = self._owner(reference)
        try:
            node = ast.parse(expression, mode="eval").body
        except SyntaxError as error:
            raise ValueError(f"expected a Python expression: {expression}") from error
        uses = self._expression_refs(file, scope, node, query=True)
        refs = self._resolve(file, scope, node)
        if isinstance(node, ast.Name):
            refs = {target for use in uses for target in use["targets"]}
        roots = refs | {target for use in uses for target in use["targets"]}
        symbol = self._symbols[reference]
        if expression.strip() in {symbol.qualified, symbol.qualified.rsplit(".", 1)[-1]}:
            roots = {reference}
        dependencies, cycles, unresolved = (
            self._closure(roots)
            if section in {"summary", "dependencies", "cycles", "unresolved", "evidence"}
            else ([], [], [])
        )
        unresolved.extend(
            {**use, "reason": "multiple statically possible base bindings"}
            for use in uses
            if use.get("ambiguous_bindings")
        )
        if not roots:
            unresolved.append(
                {
                    "expression": expression,
                    "file": file,
                    "owner": scope,
                    "reason": "no captured static binding; dynamic or external expression",
                }
            )
        elif len(refs) > 1:
            unresolved.append(
                {
                    "expression": expression,
                    "targets": sorted(refs),
                    "reason": "multiple statically possible bindings",
                }
            )

        def origins():
            for ref in sorted(roots):
                if ref in self._symbols:
                    yield {
                        **self.definitions[ref],
                        "evidence": [{"file": file, "owner": scope, "expression": expression}],
                    }

        def dependency_records():
            for entry in dependencies:
                yield {**self.definitions[entry["reference"]], "evidence": entry["evidence"]}

        def declarations(ref: str, name: str):
            yield {
                "section": name,
                "reference": ref,
                "declaration": {
                    key: value
                    for key, value in self.definitions[ref].items()
                    if key not in {"source", "calls", "parameters", "initializers", "bindings"}
                },
            }
            for initializer in self.definitions[ref].get("initializers", []):
                yield {"section": name, "reference": ref, "initializer": initializer}
            for binding in self.definitions[ref].get("bindings", []):
                yield {"section": name, "reference": ref, "binding": binding}

        def evidence():
            for origin in origins():
                yield from declarations(origin["reference"], "origins")
                yield {
                    "section": "origins",
                    "reference": origin["reference"],
                    **origin["evidence"][0],
                }
            for entry in dependencies:
                yield from declarations(entry["reference"], "dependencies")
                for use in entry["evidence"]:
                    yield {"section": "dependencies", "reference": entry["reference"], **use}
            for use in self._consumer_records(roots, consumer_filter):
                calls = use.get("evidence") or use.get("consumer_calls") or [None]
                for call in calls:
                    yield {
                        "section": "consumers",
                        **{key: value for key, value in use.items() if key not in {"evidence", "consumer_calls"}},
                        **({"call": call} if call else {}),
                    }
            for boundary in unresolved:
                yield {"section": "unresolved", **boundary}
            for index, cycle in enumerate(cycles):
                for start, end in itertools.pairwise(cycle):
                    yield {"section": "cycles", "cycle": index, "from": start, "to": end}

        sections = {
            "origins": origins,
            "dependencies": dependency_records,
            "consumers": lambda: self._consumer_records(roots, consumer_filter),
            "cycles": lambda: ({"path": cycle} for cycle in cycles),
            "unresolved": lambda: iter(unresolved),
            "evidence": evidence,
        }
        answer = self._answer_sections(sections, section, limit, offset)
        return {
            "reference": reference,
            "expression": expression,
            "source_revision": self.revision,
            "consumer_scope": "direct uses of the queried origins, not all uses of transitive base dimensions",
            "consumers_filter": sorted(consumer_filter) if consumer_filter is not None else None,
            "value_semantics": "Declared defaults and expressions are captured code, not runtime overrides.",
            **answer,
        }

    def dimension_drivers(
        self,
        reference: str,
        expression: str,
        *,
        observed: dict[tuple[str, str], dict[str, Any]] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Resolve bounded arithmetic evidence using this index, never a competing source graph."""
        from .dimensions import DimensionAnalysis

        return DimensionAnalysis(self, observed).analyze(reference, expression, limit)

    def _related_records(self, reference: str, consumers: set[str] | None):
        self.definition(reference)
        symbol = self._symbols[reference]
        prefix = symbol.qualified + "."
        own = {
            ref
            for ref, candidate in self._symbols.items()
            if ref == reference or (candidate.file == symbol.file and candidate.qualified.startswith(prefix))
        }

        def callers():
            for file, owner, node in self._calls:
                if owner in own or (consumers is not None and owner not in consumers):
                    continue
                call = self._call(file, owner, node)
                if own.intersection(call["targets"]):
                    yield {"reference": owner, "evidence": [call]}

        def callees():
            for file, owner, node in self._calls:
                if owner not in own:
                    continue
                call = self._call(file, owner, node)
                for target in call["targets"]:
                    if target not in own and (consumers is None or target in consumers):
                        yield {"reference": target, "evidence": [call]}

        captured_dependencies = None
        shared_graph = None

        def dependency_context():
            nonlocal captured_dependencies
            if captured_dependencies is None:
                direct = {
                    target
                    for use in self._usages()
                    if use["owner"] in own
                    for target in use["targets"]
                    if target not in own
                }
                expanded, cycles, boundaries = self._closure(direct)
                boundaries.extend({"reason": "dependency cycle", "path": cycle} for cycle in cycles)
                captured_dependencies = direct | {entry["reference"] for entry in expanded}, boundaries
            return captured_dependencies

        def shared_context():
            nonlocal shared_graph
            if shared_graph is None:
                own_calls = {
                    target
                    for file, owner, node in self._calls
                    if owner in own
                    for target in self._call(file, owner, node)["targets"]
                    if target not in own
                }
                # Reverse only the compact syntax graph, not a closure/definition graph per sibling.
                reverse: dict[str, set[str]] = defaultdict(set)
                for ref in self._symbols:
                    for use in self._dependencies(ref):
                        for target in use["targets"]:
                            reverse[target].add(ref)
                usage_owners: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for use in self._usages():
                    owner = use["owner"]
                    if owner in own or (consumers is not None and owner not in consumers):
                        continue
                    if owner in self._symbols and self._symbols[owner].kind in {"function", "method", "property"}:
                        for target in use["targets"]:
                            usage_owners[target].append(use)
                shared_graph = own_calls, reverse, usage_owners
            return shared_graph

        def shared(kinds: set[str] | None):
            dependencies, _ = dependency_context()
            if not dependencies:
                return
            own_calls, reverse, usage_owners = shared_context()
            matches: dict[str, dict[str, Any]] = {}
            for dependency in sorted(dependencies):
                if kinds is None:
                    if dependency not in own_calls:
                        continue
                elif dependency not in self._symbols or self._symbols[dependency].kind not in kinds:
                    continue
                pending = [dependency]
                seen: set[str] = set()
                while pending:
                    target = pending.pop()
                    if target in seen:
                        continue
                    seen.add(target)
                    for use in usage_owners.get(target, []):
                        entry = matches.setdefault(use["owner"], {"shared": set(), "evidence": {}})
                        entry["shared"].add(dependency)
                        entry["evidence"][dependency, id(use)] = use
                    pending.extend(sorted(reverse.get(target, ()), reverse=True))
            for owner in sorted(matches):
                entry = matches[owner]
                yield {
                    "reference": owner,
                    "shared": sorted(entry["shared"]),
                    "evidence": [
                        {"dependency": dependency, **use} for (dependency, _), use in entry["evidence"].items()
                    ],
                }

        def unresolved():
            yield from self.errors
            yield from dependency_context()[1]
            for file, owner, node in self._calls:
                if owner in own:
                    call = self._call(file, owner, node)
                    if not call["targets"]:
                        yield call

        sections = {
            "callers": callers,
            "callees": callees,
            "shared_helpers": lambda: shared(None),
            "shared_parameters": lambda: shared({"field", "property", "parameter", "assignment"}),
            "unresolved": unresolved,
        }

        def evidence():
            for name, records in sections.items():
                if name == "evidence":
                    continue
                for record in records():
                    uses = record.get("evidence")
                    if uses:
                        for use in uses:
                            yield {
                                "section": name,
                                "reference": record["reference"],
                                **({"shared": use["dependency"]} if "dependency" in use else {}),
                                "evidence": use,
                            }
                    else:
                        yield {"section": name, **record}

        sections["evidence"] = evidence
        return sections

    def related(
        self,
        reference: str,
        *,
        limit: int = 20,
        offset: int = 0,
        consumers: str | list[str] | None = None,
        section: str = "summary",
    ) -> dict[str, Any]:
        consumer_filter = self.query_options(limit, offset, consumers)
        return {
            "reference": reference,
            "source_revision": self.revision,
            "consumers_filter": sorted(consumer_filter) if consumer_filter is not None else None,
            **self._answer_sections(self._related_records(reference, consumer_filter), section, limit, offset),
        }

    def callers(self, reference: str):
        """Unpaged internal navigation stream; public answers remain explicitly paginated."""
        self.definition(reference)
        symbol = self._symbols[reference]
        prefix = symbol.qualified + "."
        own = {
            ref
            for ref, candidate in self._symbols.items()
            if ref == reference or (candidate.file == symbol.file and candidate.qualified.startswith(prefix))
        }
        for file, owner, node in self._calls:
            if owner not in own:
                call = self._call(file, owner, node)
                if own.intersection(call["targets"]):
                    yield {"reference": owner, "evidence": [call]}
