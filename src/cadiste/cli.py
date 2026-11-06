"""Command line: ``cadiste trace | export | mesh | save | serve | mcp``."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from build123d import export_brep, export_step, export_stl
from build123d.topology import Compound, Shape

from .export import assembly_scene, export_trace
from .mesh import Leaf
from .project import load as _load
from .project import part_definitions
from .provenance import OriginPath, Provenance, unlocated_key
from .trace import Trace, tracing
from .tree import feature_tree, op_label, render


def _build(target: str, args: list[str]) -> tuple[Trace, Shape, float]:
    build: Callable[..., Any] = _load(target)
    if not callable(build):
        raise SystemExit(f"{target} is not callable")
    start = time.perf_counter()
    trace: Trace
    with tracing() as trace:
        result = build(*args)
    elapsed = time.perf_counter() - start
    if not isinstance(result, Shape):
        raise SystemExit(f"{target} returned {type(result).__name__}, not a build123d shape")
    return trace, result, elapsed


def _origin_text(path: OriginPath | None) -> str:
    if path is None:
        return "?"
    made_by = path[-1]
    site = made_by.site[-1].location if made_by.site else "?"
    via = f"  via {', '.join(op_label(op) for op in path[:-1])}" if len(path) > 1 else ""
    return f"{op_label(made_by)} {site}{via}"


def _print_faces(shape: Shape, origins: list[OriginPath | None]) -> None:
    for index, (face, path) in enumerate(zip(shape.faces(), origins, strict=True)):
        c = face.center()
        print(
            f"f{index:<4} {face.geom_type.name:10} {face.area:9.2f} mm²  "
            f"({c.X:7.2f}, {c.Y:7.2f}, {c.Z:7.2f})  ← {_origin_text(path)}"
        )


def cmd_trace(args: argparse.Namespace) -> None:
    trace, result, elapsed = _build(args.target, args.args)
    for record in trace.builders:
        if record.parent is None and record.events:
            print("\n".join(render(feature_tree(record), args.width)))
            print()
    if not trace.ops:
        print(f"no builder operation ran: this {type(result).__name__} was made without one, so it has no features\n")

    start = time.perf_counter()
    origins = Provenance(trace).face_origins(result)
    provenance_time = time.perf_counter() - start
    if args.faces:
        _print_faces(result, origins)
        print()
    found = sum(path is not None for path in origins)
    print(
        f"{len(trace.builders)} builders, {len(trace.ops)} operations, build {elapsed:.2f} s; "
        f"{found}/{len(origins)} faces traced to an operation in {provenance_time:.2f} s"
    )


def _write_json(data: object, output: Path | None) -> None:
    text = json.dumps(data, separators=(",", ":"))
    if output is None:
        sys.stdout.write(text)
    else:
        output.write_text(text)


def _write_shapes(leaves: list[Leaf], output: Path | None) -> None:
    if output is not None:
        from .measure import save

        save(leaves, output)


def cmd_export(args: argparse.Namespace) -> None:
    # Whatever the model prints must not end up in the JSON.
    with contextlib.redirect_stdout(sys.stderr):
        trace, result, elapsed = _build(args.target, args.args)
        origins = Provenance(trace).face_origins(result)
        data, leaves = export_trace(trace, result, origins, root=Path.cwd(), build_seconds=elapsed)
        _write_shapes(leaves, args.shapes)
    _write_json(data, args.output)


def _build_untraced(target: str, args: list[str], each: bool, label: str | None) -> tuple[Shape, float]:
    build: Callable[..., Any] = _load(target)
    start = time.perf_counter()
    if each:
        # One shape per argument, labelled with it, grouped like an assembly: a file's parts side by side.
        children = []
        for name in args:
            child = build(name)
            if not isinstance(child, Shape):
                raise SystemExit(f"{target}({name!r}) returned {type(child).__name__}, not a build123d shape")
            child.label = name
            children.append(child)
        result: Any = Compound(label=label or "Parts", children=children)
    else:
        result = build(*args)
    elapsed = time.perf_counter() - start
    if not isinstance(result, Shape):
        raise SystemExit(f"{target} returned {type(result).__name__}, not a build123d shape")
    return result, elapsed


def cmd_mesh(args: argparse.Namespace) -> None:
    with contextlib.redirect_stdout(sys.stderr):
        result, elapsed = _build_untraced(args.target, args.args, args.each, args.label)
        scene, leaves = assembly_scene(result)
        if args.match_parts:
            scene["part_of"] = _instance_parts(leaves, args.match_parts, args.part_build or args.target)
        _write_shapes(leaves, args.shapes)
    _write_json({"scene": scene, "build_seconds": elapsed}, args.output)


def _instance_parts(leaves: list[Leaf], parts_target: str, build_target: str) -> dict[str, str]:
    """Leaf path -> the part it is an instance of. An assembly places moved copies of its parts, which keep their
    topology: a leaf whose first face is a part's, wherever placed, is that part's. Building the parts again costs
    nothing when the model memoises them (pipecrawler's ``build_part``), which is also what makes them match; a
    model that builds its parts anew for the assembly leaves its instances unmatched."""
    build: Callable[..., Any] = _load(build_target)
    owners: dict[Any, str] = {}
    for name in part_definitions(parts_target):
        part = build(name)
        faces = part.faces() if isinstance(part, Shape) else []
        if faces:
            owners.setdefault(unlocated_key(faces[0].wrapped), name)
    matched: dict[str, str] = {}
    for leaf in leaves:
        owner = owners.get(unlocated_key(leaf.faces[0])) if leaf.faces else None
        if owner is not None:
            matched[leaf.path] = owner
    return matched


_WRITERS: dict[str, Callable[[Shape, Path], bool]] = {"step": export_step, "stl": export_stl, "brep": export_brep}
"""What ``cadiste save`` writes: STEP keeps an assembly's tree (names, colours, placements); STL is one mesh."""


def cmd_save(args: argparse.Namespace) -> None:
    with contextlib.redirect_stdout(sys.stderr):
        result, _ = _build_untraced(args.target, args.args, args.each, args.label)
        if args.label and not args.each:
            result.label = args.label
        if not _WRITERS[args.format](result, args.output):
            raise SystemExit(f"could not write {args.output} as {args.format.upper()}")


def _environment(pairs: list[str]) -> dict[str, str]:
    environment = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise SystemExit(f"expected KEY=VALUE, got {pair!r}")
        environment[key] = value
    return environment


def _assemblies(entries: list[str]) -> dict[str, str]:
    """``[NAME=]module:callable`` entries -> name -> target, in order. Unnamed: "Assembly" for a lone one, else the
    callable's name."""
    assemblies: dict[str, str] = {}
    for entry in entries:
        name, separator, target = entry.rpartition("=")
        if not separator:
            name = "Assembly" if len(entries) == 1 else target.rpartition(":")[2]
        if ":" not in target or not name:
            raise SystemExit(f"expected [NAME=]module:callable, got {entry!r}")
        if name in assemblies:
            raise SystemExit(f"two assemblies named {name!r}: name them, NAME=module:callable")
        assemblies[name] = target
    return assemblies


def cmd_serve(args: argparse.Namespace) -> None:
    from .cache import TraceCache
    from .project import source_roots
    from .server import Project, serve

    environment = _environment(args.trace_env)
    assemblies = _assemblies(args.assembly)
    cache = None
    if not args.no_cache:
        targets = [args.target, *([args.parts] if args.parts else []), *assemblies.values()]
        salt = json.dumps({"target": args.target, "args": args.args, "env": environment}, sort_keys=True)
        cache = TraceCache(args.cache_dir.resolve(), source_roots(targets), salt)
    project = Project(args.target, tuple(args.args), None, assemblies, environment, parts_target=args.parts)
    project.catalog(refresh=True)
    # The lockfile goes where `cadiste mcp` looks for it, with or without a cache.
    serve(project, args.host, args.port, cache, lockfile=args.cache_dir.resolve() / "serve.json")


def cmd_mcp(args: argparse.Namespace) -> None:
    try:
        from .bridge import run
    except ImportError as error:
        raise SystemExit(f"cadiste mcp needs the mcp extra (pip install 'cadiste[mcp]'): {error}") from error
    run(args.url)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cadiste", description="Trace build123d models")
    commands = parser.add_subparsers(required=True)

    trace = commands.add_parser(
        "trace", help="build a model under the tracer; print its feature tree and where each face came from"
    )
    trace.add_argument("target", help="module:callable returning the shape, e.g. mypkg.parts:Bracket")
    trace.add_argument("args", nargs="*", help="string arguments passed to the callable")
    trace.add_argument("--faces", action="store_true", help="list every face of the result with its origin")
    trace.add_argument("--width", type=int, default=80, help="source excerpt width")
    trace.set_defaults(run=cmd_trace)

    export = commands.add_parser("export", help="build a model under the tracer and write the viewer's JSON")
    export.add_argument("target", help="module:callable returning the shape")
    export.add_argument("args", nargs="*", help="string arguments passed to the callable")
    export.add_argument("--output", type=Path, help="file to write (default: stdout)")
    export.add_argument("--shapes", type=Path, help="also write the scene's shapes, for `cadiste serve` to measure")
    export.set_defaults(run=cmd_export)

    mesh = commands.add_parser("mesh", help="build a model (not traced) and write the viewer's scene for it")
    mesh.add_argument("target", help="module:callable returning the shape (a part or an assembly)")
    mesh.add_argument("args", nargs="*", help="string arguments passed to the callable")
    mesh.add_argument("--output", type=Path, help="file to write (default: stdout)")
    mesh.add_argument("--shapes", type=Path, help="also write the scene's shapes, for `cadiste serve` to measure")
    mesh.add_argument(
        "--each",
        action="store_true",
        help="call the target once per argument and group the shapes, each labelled with its argument",
    )
    mesh.add_argument("--label", help="name of the group made with --each")
    mesh.add_argument(
        "--match-parts",
        metavar="MODULE:ATTRIBUTE",
        help="name the part each instance of the result is of, among these parts (as serve's --parts)",
    )
    mesh.add_argument(
        "--part-build", metavar="MODULE:CALLABLE", help="what builds a part from its name (default: the target)"
    )
    mesh.set_defaults(run=cmd_mesh)

    save = commands.add_parser("save", help="build a model (not traced) and write it as a CAD file: STEP, STL or BREP")
    save.add_argument("target", help="module:callable returning the shape (a part or an assembly)")
    save.add_argument("args", nargs="*", help="string arguments passed to the callable")
    save.add_argument("--output", type=Path, required=True, help="file to write")
    save.add_argument("--format", choices=list(_WRITERS), default="step", help="default: step")
    save.add_argument(
        "--each",
        action="store_true",
        help="call the target once per argument and group the shapes, each labelled with its argument",
    )
    save.add_argument("--label", help="name of the shape written (of the group made with --each)")
    save.set_defaults(run=cmd_save)

    serve = commands.add_parser("serve", help="serve the viewer; every request traces a fresh build")
    serve.add_argument("target", help="module:callable returning the shape")
    serve.add_argument("args", nargs="*", help="string arguments passed to the callable (without --parts)")
    serve.add_argument(
        "--parts",
        help="module:attribute naming the parts (a mapping, an iterable or a callable returning one); "
        "each part is built by calling the target with its name",
    )
    serve.add_argument(
        "--assembly",
        action="append",
        default=[],
        metavar="[NAME=]MODULE:CALLABLE",
        help="an assembly, viewed (not traced) on its own; repeat for several",
    )
    serve.add_argument(
        "--trace-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="environment for traced builds only, e.g. to turn the model's own part cache off (repeatable)",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8123)
    serve.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache/cadiste"),
        help="where traces are kept between requests and restarts (default: .cache/cadiste)",
    )
    serve.add_argument("--no-cache", action="store_true", help="trace every request anew")
    serve.set_defaults(run=cmd_serve)

    mcp = commands.add_parser(
        "mcp",
        help="an MCP server on stdio for a coding agent: what the user selects in a `cadiste serve` tab, rebuilds, "
        "screenshots",
    )
    mcp.add_argument(
        "--url",
        help="the `cadiste serve` to use (default: the one serving the working directory or a parent, found by the "
        "serve.json it writes in .cache/cadiste; else http://127.0.0.1:8123)",
    )
    mcp.set_defaults(run=cmd_mcp)

    args = parser.parse_args(argv)
    args.run(args)
