"""Record what build123d builders do while a model builds.

Inside ``tracing()``, build123d's builder entry (``_build_scope_context``), ``Builder.__exit__`` and
``Builder._add_to_context`` are wrapped, so every builder and every operation that changes a builder's object is
recorded with:

- the user call stack that made it (library frames dropped), for grouping operations by the helper functions
  that made them and for pointing at the source line;
- the builder's object before and after it, which carries build123d's ``ShapeHistory`` record of what the
  kernel did to each sub-shape (see ``provenance``);
- the builders whose products it consumed (a sketch extruded, a part inserted).

The model's own source is not modified. The tracer relies on build123d internals, hence the 0.13.x pin.
"""

from __future__ import annotations

import ast
import dataclasses
import enum
import inspect
import linecache
import sys
import sysconfig
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from types import CodeType, FrameType
from typing import Any

from build123d import Mode, build_common
from build123d.build_common import Builder, BuildScope, _PublicationService
from build123d.topology import Shape

# The interpreter's own paths (console-script wrappers included), plus every site-packages on sys.path:
# `uv run --with` layers an overlay environment whose sysconfig paths miss the project environment's site-packages.
_LIBRARY_KEYS = ("stdlib", "platstdlib", "purelib", "platlib", "scripts")
_LIBRARY_ROOTS = tuple(
    {Path(sysconfig.get_paths()[key]).resolve() for key in _LIBRARY_KEYS}
    | {Path(entry).resolve() for entry in sys.path if Path(entry).name in ("site-packages", "dist-packages")}
    | {Path(__file__).resolve().parent}
)


@cache
def _is_library(filename: str) -> bool:
    """Frames of the standard library, installed packages and cadiste itself are not model code.

    An editable install (``uv`` installs the project being run that way) keeps the model's frames outside
    site-packages, so they count as model code.
    """
    if filename.startswith("<"):
        return filename.startswith("<frozen")
    path = Path(filename).resolve()
    return any(path.is_relative_to(root) for root in _LIBRARY_ROOTS)


@cache
def _positions(code: CodeType) -> list[tuple[int | None, int | None, int | None, int | None]]:
    return list(code.co_positions())


def _expression_source(frame: FrameType) -> str:
    """Source of the expression the frame is executing (the whole call, over several lines if need be)."""
    try:
        start, end, col, end_col = _positions(frame.f_code)[frame.f_lasti // 2]
    except IndexError:
        return ""
    lines = linecache.getlines(frame.f_code.co_filename)
    if None in (start, end, col, end_col) or not lines:
        return ""
    assert start is not None and end is not None and col is not None and end_col is not None
    # Column offsets are UTF-8 byte offsets.
    chunk = [line.encode() for line in lines[start - 1 : end]]
    if not chunk:
        return ""
    if len(chunk) == 1:
        return chunk[0][col:end_col].decode(errors="replace")
    chunk[0], chunk[-1] = chunk[0][col:], chunk[-1][:end_col]
    return b"".join(chunk).decode(errors="replace")


@dataclass(frozen=True)
class CallSite:
    """One model-code frame of a call stack, as it was when the recorded event happened."""

    invocation: int
    """Identifies the function call the frame belongs to: equal for every event of one call, unique per call."""
    file: str
    line: int
    function: str
    """Qualified name of the frame's function."""
    label: str
    """What to call the invocation: the class name for a shape's ``__init__``, else the function name."""
    code: str
    """Source of the expression being executed at ``line``."""

    @property
    def location(self) -> str:
        return f"{Path(self.file).name}:{self.line}"


@dataclass(eq=False)
class BuilderRecord:
    """A builder entered during the trace."""

    index: int
    kind: str
    """``BuildPart``, ``BuildSketch`` or ``BuildLine``."""
    mode: Mode
    parent: BuilderRecord | None
    """The innermost builder active when this one was entered, even one in another function's scope."""
    site: tuple[CallSite, ...]
    """Model-code stack at ``with`` time, outermost first."""
    owner: Shape | None
    """The shape object whose ``__init__`` entered this builder (``SteeringModule`` for its ``body``)."""
    name: str = ""
    """The ``with ... as`` name, once the builder has exited."""
    events: list[Op | BuilderRecord] = field(default_factory=list)
    """In order: this builder's operations and the builders entered while it was the innermost active one."""
    product: Shape | None = None
    """What the builder handed out at exit (placed by any active ``Locations``)."""
    builder: Builder | None = field(default=None, repr=False)
    scope: BuildScope | None = field(default=None, repr=False)
    """The builder's build123d scope: the placements its product gets at exit (see ``placed``)."""

    def placed(self, shape: Shape, records: dict[int, BuilderRecord]) -> Shape | None:
        """``shape``, made in this builder's local coordinates (a sketch element), where the builder's product
        ends up: placed like the product at exit, then like each enclosing sketch or line builder's product.
        ``records`` maps ``id(builder)`` to its record. None when a scope along the way was not traced."""
        record: BuilderRecord | None = self
        while record is not None and record.kind != "BuildPart":
            if record.scope is None:
                return None
            shape = _PublicationService.place(shape, record.scope) or shape
            parent = record.builder.builder_parent if record.builder is not None else None
            record = records.get(id(parent)) if parent is not None else None
        return shape

    @property
    def ops(self) -> list[Op]:
        return [event for event in self.events if isinstance(event, Op) and event.builder is self]

    @property
    def title(self) -> str:
        return f"{self.kind} {self.name}".rstrip()


@dataclass(frozen=True)
class Argument:
    """One argument an operation was called with."""

    name: str
    """Parameter name; a ``**kwargs`` entry appears under its own keyword."""
    value: str
    """Short description of the value passed (shapes are summarised, floats rounded)."""
    source: str
    """The expression written in the call, or ``""`` when the call's source does not show it."""


@dataclass(eq=False)
class Op:
    """One change to a builder's object: a builder object (``Box``), an operation (``chamfer``), or a child
    builder handing its product over at exit."""

    index: int
    builder: BuilderRecord
    kind: str
    mode: Mode
    site: tuple[CallSite, ...]
    """Model-code stack at call time, outermost first: ``site[-1]`` is the call."""
    before: Shape | None
    after: Shape | None
    """The builder's object after the operation; its ``_history`` says what the operation did."""
    inputs: tuple[BuilderRecord, ...] = ()
    """Builders whose product (or owning shape object) was passed to the operation."""
    child: BuilderRecord | None = None
    """For a child builder publishing its product at exit: that builder."""
    obj: Shape | None = field(default=None, repr=False)
    """The builder object instance (``Box(...)``), for object operations."""
    arguments: tuple[Argument, ...] = ()


@dataclass
class Trace:
    builders: list[BuilderRecord] = field(default_factory=list)
    ops: list[Op] = field(default_factory=list)


@dataclass(eq=False)
class _Call:
    """The arguments an operation received, with its signature when it is known."""

    signature: inspect.Signature | None
    args: tuple[Any, ...]
    kwargs: dict[str, Any]

    def describe(self, kind: str, code: str) -> tuple[Argument, ...]:
        """Each argument by parameter name, with its expression in ``code`` (the call's source)."""
        sources = _call_sources(self.signature, kind, code)
        return tuple(
            Argument(name, _describe(value), sources.get(name, ""))
            for name, value in _bind(self.signature, self.args, self.kwargs)
        )


def _bound(signature: inspect.Signature, args, kwargs) -> list[tuple[str, Any]] | None:
    """Bind to ``signature``, ``**kwargs`` entries flattened under their own keywords."""
    try:
        bound = signature.bind_partial(*args, **kwargs)
    except TypeError:
        return None
    pairs: list[tuple[str, Any]] = []
    for name, value in bound.arguments.items():
        if signature.parameters[name].kind == inspect.Parameter.VAR_KEYWORD:
            pairs.extend(value.items())
        else:
            pairs.append((name, value))
    return pairs


def _bind(signature: inspect.Signature | None, args: tuple[Any, ...], kwargs: dict[str, Any]) -> list[tuple[str, Any]]:
    pairs = _bound(signature, args, kwargs) if signature is not None else None
    return pairs if pairs is not None else [*((f"arg{i}", v) for i, v in enumerate(args)), *kwargs.items()]


def _callee(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _call_sources(signature: inspect.Signature | None, kind: str, code: str) -> dict[str, str]:
    """Parameter name -> argument expression, when ``code`` is a call to ``kind``."""
    if signature is None or not code:
        return {}
    try:
        call = ast.parse(code, mode="eval").body
    except SyntaxError:
        return {}
    if not isinstance(call, ast.Call) or _callee(call.func) != kind:
        return {}
    # A *starred argument hides which parameters the positional ones land on.
    positional = [] if any(isinstance(arg, ast.Starred) for arg in call.args) else call.args
    keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
    pairs = _bound(signature, positional, keywords)
    if pairs is None:
        return {}

    def text(node: ast.expr) -> str:
        return " ".join((ast.get_source_segment(code, node) or "").split())

    return {name: ", ".join(text(n) for n in node) if isinstance(node, tuple) else text(node) for name, node in pairs}


def _describe(value: Any, depth: int = 0) -> str:
    """A short, readable rendering of an argument value."""
    if value is None or isinstance(value, (bool, int, str)):
        text = repr(value)
    elif isinstance(value, float):
        text = f"{value:.6g}"
    elif isinstance(value, enum.Enum):
        text = f"{type(value).__name__}.{value.name}"
    elif isinstance(value, Shape):
        try:
            faces = len(value.faces())
        except Exception:  # an empty shape has no topology to count
            faces = 0
        text = f"{type(value).__name__} ({faces} faces)" if faces else type(value).__name__
    elif isinstance(value, (list, tuple)) and value and all(isinstance(item, Shape) for item in value):
        kinds = sorted({type(item).__name__ for item in value})
        text = f"[{len(value)} × {'/'.join(kinds)}]"  # noqa: RUF001 - the multiplication sign is meant
    elif isinstance(value, (list, tuple)) and depth < 3:
        items = [_describe(item, depth + 1) for item in value[:8]] + (["…"] if len(value) > 8 else [])
        inner = ", ".join(items) + ("," if isinstance(value, tuple) and len(value) == 1 else "")
        text = f"({inner})" if isinstance(value, tuple) else f"[{inner}]"
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        text = f"{type(value).__name__}(…)"
    else:
        text = repr(value)
    return text if len(text) <= 160 else text[:159] + "…"


# Not Builder.__enter__: it compares its caller's frame with the active builder's to decide parenthood, so a
# wrapper frame would orphan every nested builder. It activates its scope through this module global.
_ORIGINAL_SCOPE_CONTEXT = build_common._build_scope_context
_ORIGINAL_EXIT = Builder.__exit__
_ORIGINAL_ADD = Builder._add_to_context


class _Recorder:
    def __init__(self, trace: Trace):
        self.trace = trace
        self.active: list[BuilderRecord] = []
        self.records: dict[int, BuilderRecord] = {}
        # Frames stay referenced for the whole trace so that id(frame) identifies one invocation.
        self.frames: dict[int, tuple[FrameType, int]] = {}

    # ---- stacks ----

    def _invocation(self, frame: FrameType) -> int:
        known = self.frames.get(id(frame))
        if known is None:
            known = (frame, len(self.frames))
            self.frames[id(frame)] = known
        return known[1]

    def _site(self, frame: FrameType) -> CallSite:
        code = frame.f_code
        label = code.co_qualname
        owner = frame.f_locals.get("self") if code.co_name == "__init__" else None
        if isinstance(owner, Shape):
            label = type(owner).__name__
        return CallSite(
            invocation=self._invocation(frame),
            file=code.co_filename,
            line=frame.f_lineno,
            function=code.co_qualname,
            label=label,
            code=_expression_source(frame),
        )

    def _user_frames(self, frame: FrameType | None) -> list[FrameType]:
        """Model-code frames from ``frame`` outwards, innermost first."""
        frames = []
        while frame is not None:
            if not _is_library(frame.f_code.co_filename):
                frames.append(frame)
            frame = frame.f_back
        return frames

    def _stack(self, frames: list[FrameType]) -> tuple[CallSite, ...]:
        return tuple(self._site(frame) for frame in reversed(frames))

    # ---- records ----

    def _record_for(self, builder: Builder) -> BuilderRecord:
        record = self.records.get(id(builder))
        if record is None:  # entered before the trace started
            record = BuilderRecord(len(self.trace.builders), type(builder).__name__, builder.mode, None, (), None)
            record.builder = builder
            self.records[id(builder)] = record
            self.trace.builders.append(record)
        return record

    def enter(self, builder: Builder, scope: BuildScope) -> None:
        frames = self._user_frames(sys._getframe(1))
        owner = next(
            (s for f in frames if f.f_code.co_name == "__init__" and isinstance(s := f.f_locals.get("self"), Shape)),
            None,
        )
        parent = self.active[-1] if self.active else None
        record = BuilderRecord(
            index=len(self.trace.builders),
            kind=type(builder).__name__,
            mode=builder.mode,
            parent=parent,
            site=self._stack(frames),
            owner=owner,
        )
        record.builder = builder
        record.scope = scope
        self.records[id(builder)] = record
        self.trace.builders.append(record)
        if parent is not None:
            parent.events.append(record)
        self.active.append(record)

    def exit(self, builder: Builder) -> None:
        record = self._record_for(builder)
        if record in self.active:  # identity: records compare by identity
            self.active.remove(record)
        record.product = builder._placed_obj if builder._placed_obj is not None else builder._obj
        frame = builder._python_frame
        if frame is not None:
            record.name = next((name for name, value in frame.f_locals.items() if value is builder), "")

    def add(self, builder: Builder, mode: Mode, before: Shape | None, frame: FrameType) -> None:
        library: list[FrameType] = []
        current: FrameType | None = frame
        while current is not None and _is_library(current.f_code.co_filename):
            library.append(current)
            current = current.f_back
        user = self._user_frames(current)

        kind, obj, child, call = self._classify(library)
        site = self._stack(user)
        record = self._record_for(builder)
        values = [obj, *call.args, *call.kwargs.values()] if call is not None else [obj]
        inputs = tuple(
            dict.fromkeys(linked for value in values for linked in self._linked(value) if linked is not record)
        )
        op = Op(
            index=len(self.trace.ops),
            builder=record,
            kind=kind,
            mode=mode,
            site=site,
            before=before,
            after=builder._obj,
            inputs=inputs,
            child=child,
            obj=obj,
            arguments=call.describe(kind, site[-1].code if site else "") if call is not None else (),
        )
        self.trace.ops.append(op)
        record.events.append(op)

    def _classify(self, library: list[FrameType]) -> tuple[str, Shape | None, BuilderRecord | None, _Call | None]:
        """What called ``_add_to_context``: ``library`` is the run of library frames below the model code,
        innermost first."""
        for index, frame in enumerate(library):
            name, owner = frame.f_code.co_name, frame.f_locals.get("self")
            if name == "_publish_to_context" and isinstance(owner, Shape):
                # BaseObjectMeta.__call__ -> _publish_to_context: an object like Box(...) or a custom one.
                frame_call = next((f for f in library[index + 1 :] if f.f_code.co_name == "__call__"), None)
                call = _Call(None, (owner,), {})
                if frame_call is not None:
                    try:
                        # Not inspect.signature(cls): that is BaseObjectMeta.__call__(*args, **kwargs).
                        init = inspect.signature(type(owner).__init__)
                        signature = init.replace(parameters=list(init.parameters.values())[1:])
                    except (TypeError, ValueError):
                        signature = None
                    call = _Call(
                        signature,
                        tuple(frame_call.f_locals.get("args", ())),
                        dict(frame_call.f_locals.get("kwargs", {})),
                    )
                return type(owner).__name__, owner, None, call
            if name == "__exit__" and isinstance(owner, Builder):
                child = self._record_for(owner)
                return child.kind, None, child, None
        if not library:
            return "_add_to_context", None, None, None
        entry = library[-1]  # the library function the model code called
        info = inspect.getargvalues(entry)
        parameters = [inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in info.args]
        args = [info.locals[name] for name in info.args]
        kwargs: dict[str, Any] = {}
        if info.varargs:
            parameters.append(inspect.Parameter(info.varargs, inspect.Parameter.VAR_POSITIONAL))
            args += list(info.locals[info.varargs])
        if info.keywords:
            parameters.append(inspect.Parameter(info.keywords, inspect.Parameter.VAR_KEYWORD))
            kwargs = dict(info.locals[info.keywords])
        return entry.f_code.co_name, None, None, _Call(inspect.Signature(parameters), tuple(args), kwargs)

    def _linked(self, value: Any, depth: int = 0) -> Iterator[BuilderRecord]:
        if isinstance(value, Builder):
            if id(value) in self.records:
                yield self.records[id(value)]
            return
        if isinstance(value, Shape):
            for record in self.records.values():
                # A shape object's own builder counts, not the ones nested in it (its sketches).
                owned = value is record.owner and (record.parent is None or record.parent.owner is not value)
                if value is record.product or owned:
                    yield record
            return
        if depth < 2 and isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                yield from self._linked(item, depth + 1)


_RECORDER: _Recorder | None = None


def _scope_context(scope: BuildScope):
    if _RECORDER is not None and isinstance(scope.builder, Builder) and scope.owner is scope.builder:
        _RECORDER.enter(scope.builder, scope)
    return _ORIGINAL_SCOPE_CONTEXT(scope)


def _exit(self: Builder, exception_type, exception_value, traceback):
    try:
        return _ORIGINAL_EXIT(self, exception_type, exception_value, traceback)
    finally:
        if _RECORDER is not None:
            _RECORDER.exit(self)


def _add_to_context(self: Builder, *objects, faces_to_pending: bool = True, clean: bool = True, mode: Mode = Mode.ADD):
    before = self._obj
    result = _ORIGINAL_ADD(self, *objects, faces_to_pending=faces_to_pending, clean=clean, mode=mode)
    if _RECORDER is not None and mode != Mode.PRIVATE and objects:
        _RECORDER.add(self, mode, before, sys._getframe(1))
    return result


@contextmanager
def tracing() -> Iterator[Trace]:
    """Record every builder and builder operation run inside the block (one trace at a time)."""
    global _RECORDER
    if _RECORDER is not None:
        raise RuntimeError("a trace is already running")
    trace = Trace()
    _RECORDER = _Recorder(trace)
    build_common._build_scope_context = _scope_context
    Builder.__exit__ = _exit  # type: ignore[method-assign]
    Builder._add_to_context = _add_to_context  # type: ignore[method-assign]
    try:
        yield trace
    finally:
        build_common._build_scope_context = _ORIGINAL_SCOPE_CONTEXT
        Builder.__exit__ = _ORIGINAL_EXIT  # type: ignore[method-assign]
        Builder._add_to_context = _ORIGINAL_ADD  # type: ignore[method-assign]
        _RECORDER = None
