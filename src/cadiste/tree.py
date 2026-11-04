"""The feature tree of a traced builder: its operations and child builders, grouped by the model-code function
calls that made them (``hinge_drum()`` > ``hinge_relief()`` > ``CrossPrism``)."""

from __future__ import annotations

from dataclasses import dataclass, field

from build123d import Mode

from .trace import BuilderRecord, CallSite, Op


@dataclass(eq=False)
class OpNode:
    op: Op


@dataclass(eq=False)
class BuilderNode:
    builder: BuilderRecord
    published: Op | None
    """The parent operation this builder's product became, if it handed it over at exit."""
    children: list[Node] = field(default_factory=list)


@dataclass(eq=False)
class Group:
    """One call of a model-code function inside the builder's block."""

    site: CallSite
    """The invocation's frame (function, file), at its first event."""
    called_at: CallSite
    """The call, in the enclosing frame."""
    children: list[Node] = field(default_factory=list)


type Node = OpNode | BuilderNode | Group


def _group_path(site: tuple[CallSite, ...], anchor: int | None) -> list[tuple[CallSite, CallSite]]:
    """The function invocations between the builder's own frame (``anchor``) and the event, outermost first,
    each with the call that made it."""
    start = next((i for i in range(len(site) - 1, -1, -1) if site[i].invocation == anchor), None)
    if start is None:
        return []
    return [(site[j], site[j - 1]) for j in range(start + 1, len(site))]


def feature_tree(builder: BuilderRecord, published: Op | None = None) -> BuilderNode:
    node = BuilderNode(builder, published)
    anchor = builder.site[-1].invocation if builder.site else None
    publishing = {op.child: op for op in builder.ops if op.child is not None}
    for event in builder.events:
        if isinstance(event, Op) and event.child is not None:
            continue  # shown on the child builder's node
        children = node.children
        for site, called_at in _group_path(event.site, anchor):
            last = children[-1] if children else None
            if not (isinstance(last, Group) and last.site.invocation == site.invocation):
                last = Group(site, called_at)
                children.append(last)
            children = last.children
        children.append(OpNode(event) if isinstance(event, Op) else feature_tree(event, publishing.get(event)))
    return node


# ---- text ----

_MODE = {Mode.ADD: "add", Mode.SUBTRACT: "subtract", Mode.INTERSECT: "intersect", Mode.REPLACE: "replace"}


def _code(text: str, width: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def builder_label(record: BuilderRecord) -> str:
    return record.name or f"{record.kind}#{record.index}"


def op_label(op: Op) -> str:
    return f"#{op.index} {op.kind}"


def _describe(node: Node, width: int) -> str:
    match node:
        case OpNode(op=op):
            site = op.site[-1] if op.site else None
            inputs = f"  ← {', '.join(builder_label(b) for b in op.inputs)}" if op.inputs else ""
            where = f"  {site.location}  {_code(site.code, width)}" if site else ""
            return f"{op_label(op)} {_MODE.get(op.mode, op.mode.name.lower())}{where}{inputs}"
        case BuilderNode(builder=record, published=published):
            mode = "private" if record.mode == Mode.PRIVATE else record.mode.name.lower()
            owner = f"  [{type(record.owner).__name__}]" if record.owner is not None and record.parent is None else ""
            where = f"  {record.site[-1].location}" if record.site else ""
            into = f"  → {op_label(published)} {_MODE.get(published.mode, '')}" if published else ""
            return f"{record.title} ({mode}){where}{owner}{into}"
        case Group(site=site, called_at=called_at):
            return f"{site.label}()  {called_at.location}"


def render(node: Node, width: int = 80) -> list[str]:
    lines = [_describe(node, width)]
    children = [] if isinstance(node, OpNode) else node.children
    for i, child in enumerate(children):
        last = i == len(children) - 1
        sub = render(child, width)
        lines.append(("└─ " if last else "├─ ") + sub[0])
        lines.extend(("   " if last else "│  ") + line for line in sub[1:])
    return lines
