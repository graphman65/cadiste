---
name: cadiste
description: Change a build123d model from a self-contained Cadiste selection snapshot or what the user points at in the viewer, and check each change in the viewer.
---

# Editing a build123d model with the cadiste viewer

cadiste shows a build123d model the way a parametric CAD timeline would: every feature (operation) in order, the
sketches it used, the arguments it was called with, and which feature made each face. The user points you at
something in the model with a self-contained Markdown snapshot copied from their viewer tab. Read that text
directly; no lookup tool or running server is needed to understand it. You edit the model's Python source with
your own file tools; cadiste never writes source.

## Selection snapshots

Copied text begins `# Cadiste selection v1`. It includes the capture timestamp, project root, units, target,
build digests, viewer tab identity when available, all selected items in order, exact available geometry,
relationships, and captured feature/source context. It describes the build the user saw, not live/current files.
Unavailable geometry or provenance is explicitly marked, never guessed. Empty selection captures the whole
target; composite targets enumerate their members and known owning parts.

Use Ctrl/Cmd-click to add/remove features, instances or picked entities from the selection. The C key copies the
current selection; with no selection it captures the pointer's item. The top-bar copy button captures the entire
selection. Read all the items: features can span several parts of a folder.

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
   Files are relative to the snapshot's project root. Its source is captured from the built trace; current source
   may have changed. For composite entities, provenance requires OCC geometric agreement of the picked region
   and adjacent face indices at 0.000001 mm tolerance after recorded placement normalization. Otherwise the
   reason it is unavailable is stated. Selected instances include pinned owning-part feature/source context,
   and recorded scene transforms; assembly placement source can still be unavailable. Reload/rebuild an expired viewer
   before copying again. Never substitute a newer build's face numbers.
2. **Find the code.** The call site (`features.py:47`) is where the feature is made; its arguments name the
   expressions to change. A value shared by several parts (a parameter dataclass field) changes them all: say so
   before changing it, or change the expression at the call instead. `feature_tree` lists every feature of a part;
   `describe` gives one in detail.
3. **Edit** the source with your own tools.
4. **Check.** `show(part=...)` builds the part again from the edited source, opens it in the linked tab and
   answers with how it differs from the build before (volume, bounding box, faces, operations added, removed or
   called with other arguments) and a screenshot. A build that fails answers with its traceback: fix and show
   again. Pass `screenshots=["front", "top"]` (or `"current"`) for other views; `select` and `highlight` point at
   what changed before a screenshot.
5. **Repeat** until the comparison and the screenshots show what the user asked for. Report the files and lines
   you changed and what the comparison says.

## Tools

| Tool | Use |
|---|---|
| `status` | Link state, what the tab shows |
| `list_parts` | The project's parts, folders, whether it has an assembly |
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

`select` takes an ordered list with these canonical item shapes (duplicates are removed in order):

```json
[
  {"kind": "feature", "part": "Bracket", "node": "o2"},
  {"kind": "instance", "path": "/Assembly/Bracket"},
  {"kind": "entity", "path": "/Assembly/Bracket/faces/faces_3"}
]
```


## Good to know

- Face `fN` is `shape.faces()[N]` of the built part; viewer paths look like `/Group/<part>/faces/faces_12`.
- A feature that brought in a shape made elsewhere (`insert`, a part built without builders) owns all of its faces:
  the code that made them is inside what it inserted, so search the source for it.
- Faces from `split` are attributed by where they lie; check the screenshot.
- A part that `show` reports "from the cache" was not built again: its source did not change since the last build.
