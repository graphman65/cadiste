---
name: cadiste
description: Edit and investigate build123d models code-first with Cadiste. Use for standalone selection snapshots, code/component references, parameter provenance, related components, or model changes identified in the viewer.
---

# Code-first editing of build123d models with Cadiste

cadiste links a build123d model's component construction to its Python code: definitions, construction calls,
builders, operations, parameter origins and consumers. The user points at code through a self-contained Markdown
snapshot copied from the viewer. Read it directly; no lookup tool or running server is needed to understand it.
The primary editing map is the code/component hierarchy, not face numbers. Use geometry and the render as
supporting evidence. Edit the model's Python source with normal file tools; cadiste never writes source.

## Selection snapshots

Copied text begins `# Cadiste selection v1`. It includes the capture timestamp, project root, units, target,
build digests, viewer tab identity when available, all selected items in order, exact available geometry,
relationships, and captured feature/source context. It describes the build the user saw, not live/current files.
Unavailable geometry or provenance is explicitly marked, never guessed. Empty selection captures the whole
target; composite targets enumerate their members and known owning parts.

Use Ctrl/Cmd-click to add/remove features, instances or picked entities from the selection. The C key copies the
current selection; with no selection it captures the pointer's item. The top-bar copy button captures the entire
selection. Read all the items: features can span several parts of a folder.

Snapshots include a primary definition reference, separate construction occurrence references, component
ancestry and a captured source revision. The primary reference points to Python code, not a viewer entity.
Definition references such as `code:src/model/body.py::Housing.__init__` do not use line numbers as identity.
Construction occurrences have separate references: repeated calls to one helper are not one component.
Use a definition reference for shared code and a construction occurrence for the exact selected call. Inspect
the occurrence's call site and arguments before editing: several selected features can have the same owning
definition. Walk farther with the code tools; the pasted snapshot itself needs no lookup.

Pass `revision=` when a query must use the snapshot's captured source. This is a source-revision guard, not an
archive lookup or a geometry/build identity. A mismatch must not be silently replaced by current source.
If no matching capture remains, retain the standalone excerpt as historical evidence and query current code
separately. Without `revision=`, code queries use current source builds. Read current files before editing;
captured line numbers are navigation hints, not proof that the files are unchanged.

Static resolution follows captured Python definitions and imports without executing expressions. Dynamic,
ambiguous or unavailable bindings remain explicit. Declared defaults are not runtime overrides. Constructor
and helper parameters are observed at their first traced event (not necessarily entry); actual operation
arguments belong to the recorded operation. Observed values stay attached to their own invocation and scope:
matching variable names in a helper are not evidence that they are the same parameter.

## Linking

The tools that drive the viewer (`show`, `select`, `highlight`, `set_view`, `set_visibility`, `screenshot`) use the
session's linked tab. A snapshot's tab identity is context only, not an automatic link. Without a link, a tool asks
the user to link a tab: every open tab shows a request with a code (`amber-fox`). If a tool is waiting, tell the
user the code and ask them to click **Link this tab** in the tab to use, then call the tool again. `status` says
whether you are linked.

## The loop

1. **Read the snapshot directly.** It captures:
   - features (`[o7]` operations, `[b2]` builders/sketches, `[g3]` function-call groups), their arguments
     (parameter = expression written → value), call stack and source;
   - faces, edges and vertices with exact available geometry and full creating-feature details, or adjacent
     face makers for edges and vertices;
   - selected instances, their full viewer paths and known owning parts;
   - or the entire target, including composite members, when the selection is empty.
   Files are relative to the snapshot's project root. Use captured owning-part provenance and recorded scene
   transforms for composite selections; do not infer ownership or assembly placement source from a viewer path.
   Reload/rebuild an expired viewer before copying again when current geometry is needed. Never substitute a
   newer build's face numbers.
2. **Walk to the code.** With no snapshot, `walk_components()` lists the project without a build, then
   `walk_components(part=...)` starts a part's construction tree. With a snapshot, use its owning part and
   definition or construction occurrence reference. Use `direction="parents"` or `"children"` and shallow
   `depth` to navigate. Explicit-part queries need no viewer link and must not wait for one.
   - `inspect_component(part=..., component=...)` gives the definition, call sites, captured arguments and
     construction relationships. Follow referenced components rather than stopping at an insertion wrapper.
   - `follow_parameter(part=..., component=..., expression=...)` follows a dimension's origin, derivation and
     consumers. Check observed values separately from declared defaults.
   - `find_related_components(part=..., component=...)` finds code-evidenced callers, callees and shared
     dependencies, including unbuilt sibling definitions. Do not infer mating from names or proximity.
   Use these tools before writing inspection scripts or shell probes. Use `measure` for an exact geometric
   question the source does not settle; a screenshot alone is not a dimension or code relationship.
3. **Edit the source.** Read the current definition and existing model conventions. Trace shared dimensions
   before changing them; choose a local call expression when the request is local. Preserve the user's explicit
   constraints and intentional asymmetry. Make the change in Python, not in viewer state.
4. **Verify the code and geometry.** Check changed expressions, affected consumers and the requested invariants
   with existing project checks or code-tool evidence. `show(part=...)` then rebuilds the edited part, opens it
   in the linked tab and returns a comparison and screenshot. Rebuild affected shared consumers as needed.
   Fix a failed build from its traceback and show again. Use `screenshots=["front", "top"]` (or `"current"`)
   for useful views; `select` and `highlight` locate changes without replacing the code check.
5. **Report evidence.** State the changed source symbols, preserved constraints, exercised checks and observed
   comparison. A successful render alone does not prove fit or thickness. Investigation-only requests do not
   require changing or rendering the model.

## Design constraints

- For tube wall thickness, distinguish radius from diameter and the bore from the outside. With fixed inner
  diameter, wall thickness is `(outer_diameter - inner_diameter) / 2`; increase the outside only.
- For a lid seat or mating wall, inspect both components and shared helpers/parameters before changing one.
  Verify the intended shared dimensions without mirroring unrelated holes, reliefs or other asymmetry.
- For a junction fillet, identify the junction in source and reuse the model's existing edge selectors and
  datums. Face/edge indices are build-specific evidence, not durable selectors for the edited model.
- Preserve stated clearances and units. Do not change a shared default, bore or mating dimension just because
  increasing it would make the selected feature stronger.


## Tools

| Tool | Use |
|---|---|
| `status` | Link state, what the tab shows |
| `list_parts` | The project's parts, folders, whether it has an assembly |
| `walk_components(part?, component?, direction?, depth?, revision?)` | List the project or walk code-linked construction parents/children; no viewer required |
| `inspect_component(part, component, revision?)` | Definition, call sites, observed arguments, source and construction relationships |
| `follow_parameter(part, component, expression, revision?)` | Parameter origin/derivation chain and consumers, with explicit unresolved bindings |
| `find_related_components(part, component, revision?)` | Code-evidenced callers/callees and shared helper/parameter dependencies |
| `feature_tree(part?)` | Every feature of a part with its id, call site and code |
| `describe(node, part?)` | One feature in detail: arguments, call stack, source, faces it made |
| `measure(paths, part?)` | Exact geometry of viewer paths (faces, edges, vertices) and distances or angles between them |
| `show(part= or view=, fresh?, select?, screenshots?)` | Build (again) and open in the tab; comparison and screenshots |
| `select(items)` | Replace selection with any number of feature/instance/entity items (`[]` clears) and return a snapshot |
| `highlight(paths)` | Light faces, edges or instances in the tab without changing the selection (`[]` clears) |
| `set_view(direction?, fit?, ortho?, transparent?)` | Camera and display of the tab |
| `set_visibility(instances, visible)` | Show or hide assembly instances |
| `screenshot(views?)` | Images of the tab (`iso`, `front`, `rear`, `left`, `right`, `top`, `bottom`, `current`) |

Without `part`, `feature_tree`, `describe` and `measure` work on the part the tab shows. Ids and face numbers are
the ones of the build the tab shows: after editing, `show` the part before relying on them again.
The four code tools do not need a linked viewer. Their explicit-part calls may build a trace to collect current
construction context, but do not open or move the view. They return structured data alongside readable text.

`select` takes an ordered list with these canonical item shapes (duplicates are removed in order):

```json
[
  {"kind": "feature", "part": "Bracket", "node": "o2"},
  {"kind": "instance", "path": "/Assembly/Bracket"},
  {"kind": "entity", "path": "/Assembly/Bracket/faces/faces_3"}
]
```

## Skill and tool availability

The MCP process captures this skill when it starts. After a Cadiste update, reload/restart the Cadiste MCP
connection if the published skill or tool list is stale. The four code tools must appear in `tools/list`;
do not replace missing tools with one-off inspection scripts or revive the old opaque-reference workflow.

## Good to know

- Face `fN` is `shape.faces()[N]` of the built part; viewer paths look like `/Group/<part>/faces/faces_12`.
- For an `insert` or a shape built elsewhere, inspect the construction occurrence and follow its `child_builder`
  or `inputs` references when available. Inspect the referenced Python definition for code absent from the
  executed feature tree; do not assume the wrapper is the source of every geometric detail.
- Faces from `split` are attributed by where they lie; check the screenshot.
- A part that `show` reports "from the cache" was not built again: its source did not change since the last build.
