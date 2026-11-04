---
name: cadiste
description: Change a build123d model from what the user points at in the cadiste viewer, and check each change in the viewer. Use when the user's message holds a cadiste reference (cadiste:ref/<id>) or points at a part, a feature, a face, an edge or an assembly instance in cadiste ("make this thinner", "move this hole", "why is this face here").
---

# Editing a build123d model with the cadiste viewer

cadiste shows a build123d model the way a parametric CAD timeline would: every feature (operation) in order, the
sketches it used, the arguments it was called with, and which feature made each face. The user points you at
something in the model with a reference copied from their viewer tab; the `cadiste` MCP tools resolve it and drive
that tab. You edit the model's Python source yourself, with your own file tools; cadiste never writes source.

## References

The user copies a reference in the viewer (the C key copies what the pointer is on: a feature row, a face, an edge,
a vertex, an instance; the link button in the top bar copies the selection) and pastes it in their message:
`cadiste:ref/1a2b3c4d (Steering Module: #7 CrossPrism)`. Call `get_reference` with it. A reference keeps what was
pointed at as the user saw it then; when the part was built again since, the answer says so, and `feature_tree` or
`describe` give the current ids.

## Linking

The tools that drive the viewer (`show`, `select`, `highlight`, `set_view`, `set_visibility`, `screenshot`) use the
session's tab: the one the first reference came from. Without one, a tool asks the user to link a tab: every open
cadiste tab shows a request with a code (`amber-fox`). If a tool answers that it is waiting for a link, tell the
user the code and ask them to click **Link this tab** in the tab to use, then call the tool again. `status` says
whether you are linked.

## The loop

1. **Resolve the reference.** `get_reference` answers with:
   - a feature from the tree (`[o7]` an operation, `[b2]` a builder or sketch, `[g3]` a function call grouping
     operations), with its arguments (parameter = expression written → value), the call stack and the source
     around the call;
   - faces, edges and vertices with their exact geometry (diameter, length, centre...) and the feature that made
     each face, or the faces an edge bounds and the features that made them;
   - in the assembly, the instance, the group path it sits in and the part it is an instance of;
   - or the whole part, when nothing in particular was pointed at.
   Files are relative to the project root given at the end of the answer.
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
| `get_reference(reference)` | What a reference the user pasted points at, resolved to source |
| `feature_tree(part?)` | Every feature of a part with its id, call site and code |
| `describe(node, part?)` | One feature in detail: arguments, call stack, source, faces it made |
| `measure(paths, part?)` | Exact geometry of viewer paths (faces, edges, vertices) and distances or angles between them |
| `show(part= or view=, fresh?, select?, screenshots?)` | Build (again) and open in the tab; comparison and screenshots |
| `select(node?, instance?, picks?)` | Select a feature or instance, or pick faces/edges/vertices, in the tab |
| `highlight(paths)` | Light faces, edges or instances in the tab without changing the selection (`[]` clears) |
| `set_view(direction?, fit?, ortho?, transparent?)` | Camera and display of the tab |
| `set_visibility(instances, visible)` | Show or hide assembly instances |
| `screenshot(views?)` | Images of the tab (`iso`, `front`, `rear`, `left`, `right`, `top`, `bottom`, `current`) |

Without `part`, `feature_tree`, `describe` and `measure` work on the part the tab shows. Ids and face numbers are
the ones of the build the tab shows: after editing, `show` the part before relying on them again.

## Good to know

- Face `fN` is `shape.faces()[N]` of the built part; viewer paths look like `/Group/<part>/faces/faces_12`.
- A feature that brought in a shape made elsewhere (`insert`, a part built without builders) owns all of its faces:
  the code that made them is inside what it inserted, so search the source for it.
- Faces from `split` are attributed by where they lie; check the screenshot.
- A part that `show` reports "from the cache" was not built again: its source did not change since the last build.
