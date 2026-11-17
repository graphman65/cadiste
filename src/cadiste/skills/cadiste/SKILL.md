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

## Private captures and explicit linking

`show` and `screenshot` default to `viewer="agent"`: managed headless Chromium, with an isolated context per
agent, no link request and no use of the user's browser session. Install `cadiste[screenshots]` and run
`python -m playwright install chromium`. Missing dependencies fail explicitly; never fall back to the user's tab.
`show(part=...)` or `show(view="assembly:NAME")` establishes the private target. `screenshot` can name a target
and retained `build_id`, or use the last private target. Captures report exact target/build identity.

Only `viewer="linked"` and explicitly linked controls operate the user's tab. Without a link, report the
request code and ask the user to click **Link this tab**. A copied snapshot's tab identity never creates a link.
Camera/visibility/section/explode/saved-view tools accept `viewer="agent"` to arrange the private view.
After a renderer timeout, query `request_outcome` before resending or rebuilding.

When the connection addresses a workspace, use `projects(action="list")`, then explicitly open a bounded
profile or select its `project_id`. Selection belongs to this MCP connection, never all agents/tabs. Check the
returned endpoint/root/profile/variant identity before editing. Do not replace routing with manual HTTP sessions.
`projects(action="restart")` manages only the selected workspace-owned child; reconnect stale MCP tool/skill code
in the coding client. `status` works without a selected project; inactive registrations are not live clients.
`renderer_state(viewer="agent")` reads lifecycle/camera/section/visibility/timings/errors without starting a build.
Suspicious-empty captures carry diagnostics: inspect the evidence rather than silently rebuilding or claiming
that an empty picture verified the model.

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
   Both parameter/related tools default to bounded summaries. Use returned page totals/next offsets and
   `section` to expand only relevant evidence; `consumers` filters exact source references.
   `edit_context(requests=[{part,component,expressions,variants?}], limit=10)` batches bounded source,
   scoped observations, direct consumers and active numeric `min`/`max` drivers. Defaults remain separate from
   observations; unsupported/dynamic code is explicitly unresolved. Compare numeric per-variant evidence, not
   source-reference strings or similarly named parameters.
   For assembly placement use `describe_assembly(name=..., path=...)`, not a part-inspection call with an
   assembly name. Explicit `cadiste.assembly.instance` ownership handles independently rebuilt equal shapes;
   unknown ownership/source stays unknown.
   Use these tools before writing inspection scripts or shell probes. Use `measure` for an exact geometric
   question the source does not settle; a screenshot alone is not a dimension or code relationship.
3. **Edit the source.** Read the current definition and existing model conventions. Trace shared dimensions
   before changing them; choose a local call expression when the request is local. Preserve the user's explicit
   constraints and intentional asymmetry. Make the change in Python, not in viewer state.
   Pin explicit affected targets with `baseline(action="pin", targets=[...], name="Before edit")` before edits.
   Pins survive rolling-generation eviction and warmup; an optional `build_id` on a target pins the exact
   displayed generation. Keep the baseline ID for comparison and preserved-interface assertions.
4. **Verify narrowly.** Use `verify_targets` for explicit affected parts/assemblies and structured position,
   bounds and instance diffs. Suggested consumers are conservative, not a complete dependency proof.
   Use `check_geometry` for explicit whole-instance pairs, requested clearance and intentional-contact exclusions.
   Pose samples use mm/degrees and optional world `origin`; coordinated samples use `poses:[...]` applied
   together from the recorded pose. Named profile `check_sets` support insertion/operating travel and explicit
   trimmed-cylinder screw/bearing engagement. Neither discrete samples nor cylindrical overlap prove continuous
   motion, thread engagement, preload or strength.
   `show` rebuilds/captures privately and returns comparison evidence. A failed build returns its traceback.
   Use useful `screenshots=["front","top"]`, and `arrangement` for selected/visible fit, hidden paths, section,
   camera and explode. Never treat a picture, unchanged volume or successful build as proof of fit.
   Run the narrow existing project check that covers the edit, not repeated full suites.
   Pass `baseline=` and named `check_sets`/explicit `assertions` to `verify_targets`; exact `preserved`,
   `placement` and preserved `measurement` assertions require pinned geometry. Equal volume/bounds are not
   interface preservation. `compare_builds(target,baseline,...)` returns same-camera before/after evidence.
   Release a pin explicitly after review; never infer that warmup's latest generation is the original baseline.
   Use `manufacturing_preflight` for retained print orientations/groups, exact dimensions, mesh closure and bed
   placement. Overhang/bridge reports are geometric estimates, not support percentages or printability proof.
   Exports use that retained B-rep, omit construction sketches and reject source changes. `open_bambu` preserves
   filament presets; optional `verify_import=True` checks an isolated native CLI load/save round trip. Distinguish
   exported/launcher-started/isolated-import-confirmed from the user's interactive import, slicing or printing.
   For router-cut flat parts, `export_cncjs` requires explicit cutter, cut depth, step-down, clearance,
   feeds and spindle RPM. It exports compensated `.nc` for manual loading only; work XY0 is the
   selected part's XY bounds center and work Z0 is stock top. The model is not moved.
   Inspect/simulate and check workholding manually; no tabs or machine execution.
   Unsupported interior machining is omitted rather than blocking supported profiles: inspect
   `partial_export`, `skipped_pockets` and `skipped_holes` in the cutting report. Recess sides may be
   top/bottom/both/internal; skipped holes include bounds and a reason. UI and G-code mark partial exports.
   Lead-ins/countersinks are not through-bore diameters; a cutter too large for a bore must never enlarge it.
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
| `status` | Private/linked target and frontend/backend/MCP generation diagnostics |
| `projects(action?, profile?, project_id?)` | List/open/select/restart workspace-owned projects; selection is per MCP connection |
| `baseline(action?, targets?, id?, name?)` | Pin/list/release immutable target-set JSON and exact shapes |
| `compare_builds(target, baseline, build_id?, viewer?, views?, arrangement?)` | Structured diff and same-camera before/after images |
| `renderer_state(viewer?)` | Read-only renderer lifecycle, display, layered visibility, timings, errors and empty-capture diagnostics |
| `edit_context(requests, limit?)` | Batched bounded source/argument/value/consumer reports, numeric drivers and variant differences |
| `manufacturing_preflight(target, only?, group?, orientation?, overhang_angle?, expected_revision?)` | Retained print-oriented geometry and explicitly non-slicer manufacturing evidence |
| `list_parts` | The project's parts, folders, whether it has an assembly |
| `walk_components(part?, component?, direction?, depth?, revision?)` | List the project or walk code-linked construction parents/children; no viewer required |
| `inspect_component(part, component, revision?)` | Definition, call sites, observed arguments, source and construction relationships |
| `follow_parameter(part, component, expression, revision?, limit?, offset?, consumers?, section?)` | Bounded origins/values/consumer evidence and explicit expansion |
| `find_related_components(part, component, revision?, limit?, offset?, consumers?, section?)` | Bounded code-evidenced callers/callees/shared dependencies |
| `feature_tree(part?)` | Every feature of a part with its id, call site and code |
| `describe(node, part?)` | One feature in detail: arguments, call stack, source, faces it made |
| `measure(paths, part?)` | Exact geometry of viewer paths (faces, edges, vertices) and distances or angles between them |
| `show(part= or view= or target=, fresh?, screenshots?, viewer?, arrangement?, baseline?)` | Build/capture exact generation; optionally compare with a pin |
| `select(items)` | Replace selection with any number of feature/instance/entity items (`[]` clears) and return a snapshot |
| `highlight(paths)` | Light faces, edges or instances in the tab without changing the selection (`[]` clears) |
| `set_view(direction?, fit?, camera?, ortho?, transparent?, viewer?)` | Fit all/visible/selected geometry and set camera/display |
| `set_visibility(instances, visible, viewer?)` | Hide/show instances in the chosen renderer |
| `screenshot(target?, build_id?, views?, viewer?, arrangement?)` | Atomic multi-view capture; private by default |
| `request_outcome(request_id)` | Recover completion after a renderer timeout |
| `saved_views(action?, id?, name?, state?, viewer?)` | List/create/update/delete/restore complete arrangements |
| `set_section(active, plane?, offset?, flipped?, viewer?)` | Section by explicit plane, mm offset and side |
| `set_explode(amount?, viewer?)` | Visual explode amount 0–1; does not change physical checks |
| `verify_targets(targets, fresh?, checks?, expected_revision?, baseline?, check_sets?, assertions?)` | Explicit headless builds/checks and exact preserved-interface assertions |
| `suggest_affected_targets(changed_files, targets?)` | Conservative captured-dependency suggestions without building |
| `check_geometry(target, pairs, clearance?, ignore?, samples?, contact?, build_id?)` | Exact overlap/separation, coordinated poses, bounded contact, witnesses and failing samples |
| `describe_assembly(name, path?, limit?, offset?)` | Ownership coverage and captured placement/construction source |
| `inspect_simulation(target)` | Read-only topology/transmission/runtime evidence; never sends motion |
| `export_target(target, format?, only?, expected_revision?, group?, orientation?, preflight?)` | Retained exact-generation artifact, manufacturing report and source evidence |
| `open_bambu(target, only?, expected_revision?, group?, orientation?, verify_import?)` | Geometry-only handoff and optional isolated native import evidence; never a GUI/print acknowledgement |
| `export_cncjs(target, machining, only?, expected_revision?, group?, orientation?)` | Compensated flat-part router G-code with explicit settings and build identity; manual CNCjs loading only |

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

The runtime panel and `status` distinguish refreshing the page, rebuilding frontend assets, restarting the
backend and reconnecting MCP. Do not confuse source revisions with geometry generations.
`cadiste serve --workspace-root <directory>` opens the web create/load workspace; `--profile cadiste.json`
loads declarative configuration in its project environment. Opening a project executes trusted local Python.
Workspace-created projects are real editable starter models, not a substitute for the user's requested design.

## Good to know

- Face `fN` is `shape.faces()[N]` of the built part; viewer paths look like `/Group/<part>/faces/faces_12`.
- For an `insert` or a shape built elsewhere, inspect the construction occurrence and follow its `child_builder`
  or `inputs` references when available. Inspect the referenced Python definition for code absent from the
  executed feature tree; do not assume the wrapper is the source of every geometric detail.
- Faces from `split` are attributed by where they lie; check the screenshot.
- A part that `show` reports "from the cache" was not built again: its source did not change since the last build.
