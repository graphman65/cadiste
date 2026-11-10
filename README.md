# cadiste

A viewer for [build123d](https://github.com/gumyr/build123d) models, in progress. It builds a model under a
tracer and recovers what a parametric CAD timeline shows: the features (operations) in order, grouped by the
functions that made them, the sketches they consumed, the arguments they were called with, and which feature made
each face of the result. The web viewer shows that as a feature tree next to a 3D view rendered like
[OCP CAD Viewer](https://github.com/bernhard-42/vscode-ocp-cad-viewer) (same tessellation, same renderer), for a
part or a whole assembly.

The model's source is not changed. While the build runs, the tracer wraps build123d's builder entry
(`build_common._build_scope_context`), `Builder.__exit__` and `Builder._add_to_context`, and follows the
`ShapeHistory` record build123d 0.13 keeps on every operation. That couples cadiste to build123d internals, hence
the `build123d>=0.13.0,<0.14` pin.

## Setup

```sh
mise install        # python 3.14, uv, node 26
mise run sync       # uv sync, npm ci in web/
mise run web-build  # build the frontend into src/cadiste/static
mise run check      # ruff, pyright, pytest, tsc
```

## View a model

`cadiste serve module:callable --parts module:attribute` serves the viewer on <http://127.0.0.1:8123>. `--parts`
names the parts (a mapping's keys, an iterable, or a callable returning either); the viewer lists them and builds
one by calling the target with its name. Without `--parts`, the target is one model, built with the arguments that
follow it. `--assembly [NAME=]module:callable` adds an assembly, listed above the parts; repeat it for several (an
unnamed one is "Assembly" alone, else named after its callable). `--trace-env KEY=VALUE` sets environment variables
for traced builds only (repeatable).

Browser reload and **Rebuild** refresh both the part list and its folder hierarchy from the current `--parts`
source (`GET /api/project`); no server restart is needed after adding/removing parts or moving their definitions.
MCP `list_parts` and project-level `walk_components` discover the current catalog independently of the browser.
Build/export validation uses that same shared catalog, refreshed before requests, so removed parts and empty
former folders are rejected. Discovery runs in a fresh interpreter in the server's environment, not the
trace-only `--trace-env`; model import prints cannot contaminate its JSON. Names and folders are published
together under a lock. Discovery/import failures are reported explicitly, leave the last complete snapshot
untouched, and block serving it as current until discovery succeeds again.

Even an unchanged catalog costs one fresh import/discovery per refresh or build/export validation (including
lookups of captured builds); the existing snapshot is retained when its contents match. Discovery does not build
geometry unless the model's catalog itself does so, and does not invalidate geometry cache entries. Without
`--parts`, the single-model name and arguments stay fixed and catalog requests import nothing. Discovery and
geometry subprocesses ignore existing bytecode caches, including source edits of the same size and timestamp.

```sh
cd ../pipecrawler
uv run --with-editable ../cadiste cadiste serve pipecrawler.catalog:build_part --parts pipecrawler.catalog:PARTS \
  --assembly "Pipe crawler=pipecrawler.assembly:build_scene" --trace-env PIPECRAWLER_CACHE_DIR=
```

The project's part cache is turned off for traces (a part loaded from a BREP file was never built, so there is
nothing to trace) and kept for the assembly, which is only viewed: 2 s with a warm cache instead of 20 s.

cadiste keeps every trace (and each assembly's scene), with the shapes it measures on, in `.cache/cadiste`
(`--cache-dir`; `--no-cache` to trace every request). An entry stands while nothing it could depend on changes:
every file under the model's top-level packages (path, size, modification time, so an edit, a checkout or a data
file all count), cadiste's own Python, the CAD library versions, the target and the trace environment. Any change
rebuilds a part the next time it is opened (the 30-tooth gear: 9.6 s to build, then cached geometry is reused);
Rebuild always builds again. One entry is kept per part; catalog discovery above is separate from this cache.

- **3D view**: three-cad-viewer (OCP CAD Viewer's renderer) on meshes from `ocp_tessellate`, in the app's colours,
  under the app's own toolbar: preset views, reset, orthographic/perspective, see-through, black edges, axes, XY
  grid (kept across reloads). The last button brings back three-cad-viewer's own toolbar and panels for clipping,
  measuring, explode and zebra; its measurement panels are drawn as the app's cards. The selection drives the view:
  - an operation lights the faces it made (the faces an inserted part brought in, for `insert`); a sketch it
    consumed shows in place, the part ghosted around it;
  - a sketch turns face-on (the preset view closest to its plane), the part drawn as edges only around it;
  - a sketch element (`Polygon`, `Circle`, a `BuildLine`'s `Line`) shows on its sketch;
  - a function group or a nested part builder lights the faces of every operation in it.

  Clicking a face, edge or vertex selects only that exact entity and shows its creating feature in Details when
  available, without revealing the feature's construction sketches; select the operation in the feature tree to
  inspect those. Ctrl/Cmd-click toggles the entity and Shift-click adds it to the selection. Clicking a sketch or
  an element focuses its feature. Hovering shows a card by the pointer: the operation, sketch or assembly instance under it,
  then the face, edge or vertex with three-cad-viewer's mesh measurements (area, length, radius, centre, ends).
  Radius readouts also show the corresponding diameter, with the same approximation marker and millimetre units.
- **Saved views** (in the view toolbar): arrange an overall or detailed view, enter a name, then **Save current
  view**. Views are project-wide: the same list is available from every part, assembly and folder. Click a saved
  name to open its saved target and restore camera orientation, pan and zoom, hidden parts/instances, feature or
  entity selection, display toggles, explode position, and section plane/offset/side. The list labels each view's
  target. **Update** replaces a view with the current target and arrangement; **Rename** and **Delete** manage the
  list. Existing per-target views migrate automatically; duplicate names gain a target-name suffix. Views survive
  reloads in this browser's local storage, separately for each project, and are not shared between browsers.
  Visibility and selections use model paths, so changed topology may require updating a view.
- **Measure** (the ruler in the toolbar): shows exact measurements for the selected faces, edges and vertices.
  Click replaces, Ctrl/Cmd-click toggles, and Shift-click adds a pick. The panel's Clear drops entity picks;
  Escape clears the entire selection. Measurements use the B-rep the mesh was made from (millimetres, degrees):
  - one: a face's area, a cylinder's or sphere's diameter and radius, a cone's half-angle, a torus' major/minor
    diameters and radii, a plane's normal, a cylinder's axis; an edge's length, a circle's or arc's diameter, radius,
    centre and arc angle, an ellipse's major/minor diameters and radii, a line's direction; a vertex's position;
  - two: the minimum distance between them and its Δx, Δy, Δz; the distance between their centres or axes (a
    circle's, a sphere's centre; a cylinder's, cone's or torus' axis; a straight edge's line) and between parallel
    planes when it is not the minimum; the angle between their lines and planes;
  - several edges or faces: their total length or area.

  Distances are drawn over the view, from one measured point to the other, with their value. Picks work across
  parts in the assembly and a file's view, each named in the card.
- **Explode** (beside the ruler): draws the parts apart, each away from the scene's centre; measuring puts them back,
  as does a new build. three-cad-viewer's own explode button (in its toolbar) and slider drive the same state.
- **Section** (beside explode): cuts the scene along a plane, the cut faces capped. The card at the bottom left takes
  the plane from the next click on a flat face (its normal from the face's mesh; the side the face looks out to goes)
  or from XY, YZ or XZ through the scene's centre; its slider and millimetre field move the cut along the normal,
  across the scene's extent, and flip cuts away the other side. Hover and picks go through the cut. The cut stays
  across new builds until the card is closed; it is three-cad-viewer's clipping, so its own clip tab shows it too.
- **Assembly**: not traced; it takes the sidebar below the part list, and the view the rest of the window (no
  right-hand panel). Two tabs, the one chosen kept across reloads:
  - **Parts**: the parts it has instances of, in the project's order, each with its count (×36): the eye hides or
    shows all of its instances, clicking lights them all, **Open** (or a double-click) opens the part. With
    `--parts`, `cadiste mesh --match-parts` names the part of each instance: one whose first face is that part's
    (the same topology, placed elsewhere), so a model that memoises its parts (pipecrawler's `build_part`) has all
    186 matched; one that builds them anew for the assembly has none, and only the Instances tab.
  - **Instances**: the instances, grouped as built: hovering a row highlights it in the view, clicking lights it (a
    group: all of its instances), the eye hides or shows it; clicking an instance in the view selects its row. The
    filter narrows the rows to matching instances.

  Parts owning selected faces, edges, vertices or instances are marked in both the Parts tab and the part list
  above, including when only one of a part's repeated instances is selected. Ctrl/Cmd-click additions and removals
  update both lists; clearing the selection clears their marks. The last marked part is scrolled into view, and
  the part list opens its folders. The head counts the hidden instances, with **Show all**. Each assembly is opened
  from its entry atop the part list (`#assembly=NAME`; `#assembly` opens the first).
- **Sidebar**: the part list has it to itself until a part is open, then shares it with the part's feature tree.
  Its width and the split are dragged and kept across reloads. Parts are in folders named after the package they
  are defined in (pipecrawler: `chassis`, `gears`, `omni_wheel`, ..., `electronics`), relative to the package all
  parts share: a part's class or function, or for a build lambda (`lambda p: SteeringModule(p)`) the first project
  class it calls. A package whose parts come from several files gets a level per file, with a file icon
  (`chassis` / `drive_module`, `drive_lid`); one with a single file stays flat (`gears`). A chevron and an open or
  closed folder icon show each folder's state; folders stay folded across reloads, and the filter opens the ones
  holding a match. Every folder has an **Open** button: all its parts, a file's (`drive_module`) or a package's
  across its files (`chassis`: drive modules, lids and the steering module), built (not traced; the model's own
  part cache serves), grouped, and viewed together in each part's own frame. The sidebar lists them (and only
  the sidebar), each with its feature tree (their traces are fetched alongside, cached once made), its eye and its
  own **Open**; hovering a part's row highlights it in the view. Selecting a
  feature of one part lights its faces among the others, with its Details on the right; clicking a face in the
  view selects the feature that made it, in whichever part. Selected entities and features also mark their owning
  parts in the part list above, opening the folders and scrolling to the last marked part. This marking does not
  depend on a part's trace being available. Feature lookup needs the folder's build of a part to number its faces
  like the part's trace (checked by face count; pipecrawler's cached and fresh builds list every face in the same
  order). The folder's view has no sketches to show. Parts that occupy the same place (pipecrawler's front and rear
  drive modules) overlap; hide some with their eye.
- **Feature tree**: builders (`Part body`, `Sketch relief`), the function calls that grouped their operations
  (`hinge_drum()`), and the operations with their mode (+ add, − subtract, ∩ intersect, ↻ replace, · private); the
  call site is in the tooltip. Arrow keys move and fold; double-click folds.
- **Timeline**: the outermost builder's features left to right; the one holding the selection is lit.
- **Details**: an operation's arguments (parameter, expression written in the call, value), the builders it
  consumed, the faces of the result it made, its call stack, and the source at the call. A builder shows who
  consumed it; a group what it contains. What an operation consumed (or what consumed a builder) is lit in the tree.
  The arguments (a builder's or group's contents) always show; the icons atop the panel show the rest: uses / used
  by, faces, call stack, source. The right-hand panel (a part's or a folder's details) is resized from its left
  edge or hidden to a show button; the width, the panel's state and the shown sections are kept across reloads.
- **View bar** (atop the 3D view): what is open, its kind (icon and word: part, assembly, file, package) and name
  after the folders it is in (`chassis / steering_module / Steering Module`), and its build: ops and faces traced
  for a part, parts or instances for the others, the build time, and whether it came from the cache.
- **Rebuild** traces the part again, past the cache. Each trace runs `cadiste export` in a fresh interpreter, so
  source edits show up, and a model's in-process memoisation (pipecrawler's `functools.cache` on `build_part`)
  cannot hand back a part that was never built under the tracer. The view bar says when a trace came from the cache.
- **Export** (top bar) downloads what is open as STEP, STL or BREP: a part, a folder's parts (a STEP keeps them
  as one group of named parts, each in its own frame) or an assembly (a STEP keeps its tree: group and instance
  names, placements, shapes shared between instances; pipecrawler's weighs 20 MB). A folder's export follows the
  eyes: only the parts shown are written (the menu says how many of how many). An assembly's is always whole. It
  builds the target again with `cadiste save`, not traced and in the server's environment, so the model's own part
  cache may serve; nothing is cached. A failed build shows its traceback in the menu.
  **Open in Bambu Studio** exports the same target as STEP and launches the local app on the machine
  running `cadiste serve`, not on a remote browser's machine. The server searches its `PATH` for `bambustudio`,
  `bambu-studio` or `BambuStudio`; a missing app or failed launch is reported in the menu. Each handoff gets a
  separate file under `<cache-dir>/bambu-studio/`, retained for Bambu's later reloads (with `--no-cache`, only
  until the server stops). Remove old handoff files when no longer needed. This opens the model; it does not
  slice or print it. The action uses `POST /api/bambu-studio` with JSON `{"target": {...}, "parts": [...]}`;
  omit `parts` for all parts, or pass the shown folder parts.

`mise run web-dev` runs Vite with hot reload on <http://localhost:5173>, proxying `/api` to `cadiste serve`.

`cadiste export [--output FILE] [--shapes FILE] module:callable -- [ARG ...]` writes the viewer's JSON for one
traced model, scene included; `cadiste mesh [--output FILE] [--shapes FILE] module:callable [ARG ...]` the scene
alone for an untraced one (an assembly); with `--match-parts module:attribute [--part-build module:callable]`
(default: the target builds the parts) it also maps each instance to its part (`scene.part_of`). Their shape is
`web/src/types.ts`. `--shapes` also writes the scene's
faces, edges and vertices, placed and numbered as the viewer has them, for `cadiste serve` to measure on
(`POST /api/measure` with the file the build's response names in `X-Cadiste-Shapes`, and viewer paths).

`cadiste save --output FILE [--format step|stl|brep] [--label NAME] [--each] module:callable [ARG ...]` builds a
model without the tracer and writes it as a CAD file (`GET /api/export?format=&kind=part&name=` | `kind=file&path=`,
with `part=` repeated to keep only those of the folder's parts | `kind=assembly&name=` serves it); `--label` names the
shape (the STEP product), `--each` builds one shape per argument and groups them under it, as a folder's view does.

## Gazebo with ROS 2

Cadiste exports **articulated SDF models for ROS 2 Jazzy / Gazebo Harmonic** (not Gazebo Classic).
A simulation factory returns `cadiste.simulation.Simulation`: explicit links, joints, collision shapes and optional
static environment links. CAD shapes and `Location` translations stay in millimetres; the exporter converts to
metres, kilograms and kg·m², including each link's centre of mass and full inertia tensor. A supplied mass
overrides density-based estimation. Joint limits and spring references are radians relative to the assembled
CAD pose; axes are in each joint's frame. Geometry alone does not establish mates, motors or material properties,
so ordinary assembly factories are not silently converted into a rigid or guessed robot.

The pipe-crawler project supplies `pipecrawler.simulation:build_simulation`: four articulated chassis modules,
three shared drive axles, two steering hubs, 48 passive rollers and three sprung hinges, with a separate hollow
test pipe. Five motor-output-shaft inputs drive the physical joints through signed ideal ratios; idlers,
pinions and exposed shafts animate from measured axle positions. Its `SimulationParams` exposes estimated
masses, friction, spring dynamics and output-space effort-bounded velocity PI gains. These remain tunable
estimates, **not calibrated hardware dynamics or validated locomotion**; see that project's simulation section.

From the pipe-crawler checkout, with Cadiste installed in its environment:

```sh
uv pip install --python .venv/bin/python -e ../cadiste
.venv/bin/cadiste gazebo --output out/gazebo pipecrawler.simulation:build_simulation
```

This builds without tracing or ROS dependencies. The relocatable directory contains `models/` (SDF, model
configuration and visual/collision meshes), `world.sdf`, `bridge.yaml`, `control.json` and `sim.launch.py`.
Mesh URIs are bundle-local `model://` references; the launch file preserves existing `GZ_SIM_RESOURCE_PATH`
entries. Copy or unzip the complete directory onto a ROS machine; the runtime does not need Python CAD
packages. Models with ideal transmissions or driven gear visuals additionally need the native system below.

```sh
# On a ROS 2 Jazzy installation (Ubuntu 24.04):
sudo apt install ros-jazzy-ros-gz
source /opt/ros/jazzy/setup.bash
# For geared models, first install the IdealTransmission runtime described below.
ros2 launch "$PWD/out/gazebo/sim.launch.py"
# Headless:
ros2 launch "$PWD/out/gazebo/sim.launch.py" gui:=false
```

`cadiste gazebo --output DIR [--launch] module:callable [ARG ...]` forwards string arguments to the simulation
factory; `--launch` exports and starts native `ros2 launch`, with output retained in `DIR/gazebo.log`.
Source the ROS setup **before starting Cadiste** when using that option or the viewer's local handoff.

Register a simulation adapter under its assembly's name to expose both **Gazebo + ROS 2 bundle** (ZIP download)
and **Open in Gazebo (ROS 2)** in the Export menu:

```sh
.venv/bin/cadiste serve pipecrawler.catalog:build_part --parts pipecrawler.catalog:PARTS \
  --assembly 'Pipe crawler=pipecrawler.assembly:build_scene' \
  --simulation 'Pipe crawler=pipecrawler.simulation:build_simulation'
```

`--simulation [NAME=]module:callable` is repeatable and must match a configured assembly. Simulation exports
always include the adapter's whole robot/environment, independently of viewer visibility; folders and individual
parts do not define a robot. `GET /api/gazebo?kind=assembly&name=...` downloads the bundle.
`POST /api/gazebo` with JSON `{"target":{"kind":"assembly","name":"Pipe crawler"}}` exports and starts ROS on the
**server machine**, not the browser's. Handoff bundles are retained under `<cache-dir>/gazebo/model-*/`;
the response and menu identify `gazebo.log`. Success means the launcher started, not that Gazebo finished
loading. Missing ROS packages are reported before a build. Remove old handoff directories after closing Gazebo;
with `--no-cache` they last only until the Cadiste server stops.

The launch file bridges `/clock`, `/tf`, `/<model>/joint_states` (`sensor_msgs/msg/JointState`) and each public
command (`std_msgs/msg/Float64`, rad/s). Direct actuators use `/<model>/joints/<joint>/cmd_vel`; ideal motor inputs
use `/<model>/motors/<motor>/cmd_vel`. Passive joints have no command topics.
`control.json` lists the commands and limits. For example:

```sh
ros2 topic pub --rate 10 /pipe_crawler/motors/front_axle_0_motor/cmd_vel \
  std_msgs/msg/Float64 '{data: 2.0}'
ros2 topic echo /pipe_crawler/joint_states sensor_msgs/msg/JointState
# Commands remain latched: send zero explicitly to stop.
ros2 topic pub --once /pipe_crawler/motors/front_axle_0_motor/cmd_vel \
  std_msgs/msg/Float64 '{data: 0.0}'
```

### Ideal motor transmissions

`Simulation.transmissions` accepts `Transmission(name, joint, ratio)` entries: one named motor input for an
actuated physical output joint. The signed ratio is **output coordinate / motor coordinate**. Output joint
limits and PID gains stay in output units; motor speed limits divide by `abs(ratio)` and motor torque limits
multiply by it. The native system relays `ratio * motor_velocity` to the existing torque-limited physical
controller. It does not replace force-controlled dynamics with enforced joint motion.

`Simulation.visuals` accepts `DrivenVisual(name, parent, joint, shape, pose, axis, multiplier)` entries.
The parent is a physical robot link, `pose` is the model-absolute CAD zero spin frame, and `shape` is local to
that frame. A unit axis in the spin frame rotates by `multiplier * measured_joint_position`. These are ordinary
colored visuals in Gazebo, not extra collision bodies or massive servo joints. Cadiste derives their world
poses from measured parent TF and joint state using the same bindings.

Joint-state ROS messages remain physical measurements. The browser separately labels ideal motor q/v/effort
derived from output samples (`q_motor = q_output / ratio`, `v_motor = v_output / ratio`,
`torque_motor = ratio * torque_output`); missing samples remain unknown. No command integration or independent
motor encoder is implied. Ideal gearing is lossless and massless: no tooth contact, backlash, separate rotor
inertia or electrical motor model is added.

Cadiste's ROS Docker image compiles and installs `libcadiste-ideal-transmission-system.so`. Rebuild the image
and recreate older containers when upgrading. For a native Jazzy/Harmonic installation, from this checkout:

```sh
sudo apt install ros-jazzy-ros-gz build-essential cmake
source /opt/ros/jazzy/setup.bash
cmake -S gazebo/ideal_transmission -B .cache/ideal-transmission-build \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PWD/.cache/ideal-transmission-runtime"
cmake --build .cache/ideal-transmission-build --parallel 2
cmake --install .cache/ideal-transmission-build
export GZ_SIM_SYSTEM_PLUGIN_PATH="$PWD/.cache/ideal-transmission-runtime/lib${GZ_SIM_SYSTEM_PLUGIN_PATH:+:$GZ_SIM_SYSTEM_PLUGIN_PATH}"
```

Launch preserves existing plugin paths and searches installed `AMENT_PREFIX_PATH` prefixes' `lib` directories
too. A geared bundle fails clearly if the system is absent. Projects without transmissions or driven visuals
do not need this native system. Re-export old bundles after changing the transmission configuration.

### Browser ROS 2 controls

An assembly registered with `--simulation` has **ROS 2 Controls** and **Live simulation** buttons over the viewer.
Selecting it discovers the adapter's metadata; the panel generates one control card per motor input or direct
actuated joint: command name, topic, units, velocity/effort limits, a numeric setpoint and a slider. It uses the
same metadata as the exported `control.json`; no crawler-specific joint names or topics are built into the UI.
Passive physical joints have no command cards.

**Live simulation** is disabled while its adapter configuration loads, including CAD construction. The badge
shows **Loading simulation controls…** during that wait; the button enables automatically when ready. If loading
fails, the error and **Retry controls** appear below the toggle without opening the panel, rather than silently
leaving the button disabled. Large adapters can take several seconds to load.

- Edit a setpoint, then click **Send**. Editing alone never publishes. Values must be finite and inside the
  configured signed velocity limit, enforced in both browser and server.
- **Stop** sends zero to one joint; **Stop all** sends zero to every configured joint, including joints whose
  cards are scrolled out of view. Stop all remains available while Send is pending and is queued after older
  sends, so an in-flight command cannot overwrite the requested stop.
- **Reload controls** re-runs the adapter after a configuration edit. It does not export meshes, rebuild the
  running simulator, or change the simulator's existing command. Relaunch Gazebo when its model configuration
  changes. A failed refresh or changed metadata invalidates older browser configurations.

Commands are **latched**: closing the panel, changing assemblies or disconnecting the browser does not stop
motion. Use Stop explicitly. Setpoints and publish acknowledgements are not measured state; a successful
publish does not prove motor response. Stop all is not an emergency stop.

**Live simulation** subscribes to the configured joint-state, TF and clock topics through a separate ROS Python
process. The panel shows measured position, velocity and optional effort for every configured joint (including
passive joints), plus the model's world position, quaternion, simulation timestamp and data age. Expand
**Joint measurements** for the scrollable table. Missing values remain unknown, not zero.

The 3D view uses a separate scene built from the simulation adapter, with explicit link-to-scene mappings.
Its **Robot (<model>)** and **Environment** groups are siblings: only robot link nodes are driven by measured
world transforms, including base translation and rotation; static environment geometry stays fixed outside
the robot group. This is not animation inferred from command setpoints. CAD provenance, entity picking,
measurements, snapshots and saved views are unavailable in this mode. Turn Live simulation off to restore the
normal CAD scene. Live updates continue with the controls panel closed; changing targets or disabling Live
simulation closes the subscriber, not the simulator or its latched commands. Waiting, stale and error states
are explicit; stale geometry retains the last measured pose rather than pretending to be current.

Clock rewinds discard the previous epoch's samples. Gazebo Harmonic can stop publishing TF after a world reset
even while clock and joint states continue. If feedback stays **waiting** with missing frames after a reset,
restart Gazebo (or its Docker container); restarting Cadiste alone cannot restore an absent upstream TF stream.

By default the server runs `ros2 topic pub --once` for commands and `ros2 run cadiste_ros feedback` for measured
feedback, without importing ROS Python modules into Cadiste. Source ROS before starting `cadiste serve`, use the
same ROS domain/network as the simulator, and keep its ROS bridge running. On a native ROS installation, add
the helper package from the Cadiste checkout to the sourced environment before starting the server:

```sh
export AMENT_PREFIX_PATH="/path/to/cadiste/ros/cadiste_ros:${AMENT_PREFIX_PATH}"
```

The Docker image below includes this helper. Alternatively, `--ros-command 'COMMAND ...'` explicitly selects
the ROS command prefix, such as `docker exec -i cadiste-ros2 /ros_entrypoint.sh ros2`. Keep `-i` for feedback:
forwarded stdin controls the remote subscriber's lifetime. Arguments are parsed once at server startup and executed
without a shell; the browser cannot change this runtime selection. Missing executables, subscriber discovery
timeout and per-joint failures are shown in the panel. Stop all attempts every joint even when one publisher
fails; its result lists successes and failures separately. Only the currently supported
`std_msgs/msg/Float64` velocity-command schema is exposed. Panel text, topics and status messages can be selected
and copied without enabling selection on the CAD canvas.

`cadiste controls [--output FILE] module:simulation_factory [ARG ...]` discovers the metadata without mesh
export/tessellation (the factory still builds its CAD objects). The HTTP interface is
`GET /api/simulation/controls?kind=assembly&name=...`; its response includes a target-bound `config_id`.
`POST /api/simulation/command` accepts JSON `{"target": {...}, "config_id": "...", "values": {"joint": 1.0}}`
or `{"target": {...}, "config_id": "...", "stop_all": true}`. Topics, message types and executables cannot be
supplied by the browser: routing comes only from the server's registered adapter snapshot.

The approved metadata includes world/model frames, each link's rest pose and all joint names. The viewer gets
its adapter scene from `GET /api/simulation/scene?kind=assembly&name=...&config_id=...` and measured snapshots
over the `feedback` SSE event at `/api/simulation/feedback` with the same query. Both reject stale configuration
IDs. `cadiste simulation-scene [--output FILE] module:simulation_factory [ARG ...]` builds the mapped scene
without starting ROS.

#### Docker runtime without host ROS

Build the ROS 2 Jazzy / Gazebo Harmonic image from the Cadiste checkout:

```sh
docker build -t cadiste-ros2:jazzy -f docker/ros2/Dockerfile .
```

From the model checkout, export the current simulation and start the headless simulator and ROS bridge:

```sh
.venv/bin/cadiste gazebo --output out/gazebo pipecrawler.simulation:build_simulation
docker run -d --name cadiste-ros2 --restart unless-stopped -e ROS_DOMAIN_ID=71 \
  -v "$PWD/out/gazebo:/bundle:ro" cadiste-ros2:jazzy \
  ros2 launch /bundle/sim.launch.py gui:=false
.venv/bin/cadiste serve pipecrawler.catalog:build_part --parts pipecrawler.catalog:PARTS \
  --assembly 'Pipe crawler=pipecrawler.assembly:build_scene' \
  --simulation 'Pipe crawler=pipecrawler.simulation:build_simulation' \
  --ros-command 'docker exec -i cadiste-ros2 /ros_entrypoint.sh ros2'
```

The prefix sends commands and reads measured feedback inside that existing container, using its ROS environment
and domain. It works with any project's adapter; replace the factory and viewer targets above. The Cadiste
server needs Docker access. Rebuild the image and recreate an older container to install an updated feedback helper.
Use `docker logs cadiste-ros2` for simulator errors, `docker stop cadiste-ros2` to stop it, and
`docker start cadiste-ros2` to start it again. Re-export and restart the container after model changes.
The container is independent of Cadiste's lifetime and does not stop motors merely because a browser closes.
This mode runs Gazebo headlessly; **Open in Gazebo (ROS 2)** remains a separate native desktop launch action.

ROS launch/bridge conventions follow the upstream [Gazebo launch guide](https://gazebosim.org/docs/harmonic/ros2_launch_gazebo/)
and [ros_gz_bridge topic mappings](https://github.com/gazebosim/ros_gz/tree/jazzy/ros_gz_bridge).

## Edit with a coding agent

`cadiste mcp` is an MCP server on stdio that lets a coding agent (Claude Code, omp, Cursor, ...) inspect models
and drive a viewer tab; the agent edits the model's source with its own tools. One `cadiste
mcp` runs per agent session and forwards every call to the `cadiste serve` of the directory it runs in (found by
the `serve.json` the server writes in its `--cache-dir`, from the working directory up; `--url` otherwise). It
needs the `mcp` extra:

```sh
cd ../pipecrawler
claude mcp add cadiste -- uv run --project ../cadiste --extra mcp cadiste mcp
```

- **Code-first navigation.** `walk_components()` lists the project without opening the viewer. Give `part=`
  to walk its construction hierarchy, then follow the returned component references through parents and children.
  `inspect_component(part=..., component=...)` shows the Python definition, construction calls, captured
  arguments, and dependencies. Edits are made to that Python source; rendering checks the result.
- **Parameters and related components.** `follow_parameter(part=..., component=..., expression=...)` follows
  a dimension through local aliases, definitions, derived properties, and parameter fields, and reports its
  consumers. `find_related_components(part=..., component=...)` finds code-evidenced callers, callees, and
  shared dependencies, including unbuilt sibling definitions. It does not guess mating relationships from names
  or geometric proximity. Unresolved/dynamic bindings are explicit.
- **Code identities and revisions.** Definitions have source-based identities such as
  `code:src/pipecrawler/parts/chassis/steering_module.py::NoseChamfer.lid_end_x`; construction occurrences have
  separate references so repeated calls do not collapse. Line numbers are locations, not identities. These
  tools return structured data alongside readable text. Supply `revision=` from a snapshot to require that
  captured source revision; a mismatch fails rather than silently substituting another version. Default code
  queries use current source builds independently of what the viewer currently displays.
- **Captured source context.** Traces include project-package Python, including parameter modules and sibling
  components that did not execute under the geometry tracer. Static navigation never executes expressions.
  Declared defaults are distinct from observed runtime values. Invocation parameters are observed at their
  first traced event, not necessarily function entry; actual operation arguments are captured at the operation.
  Observed values remain attached to individual invocations; matching names in helper scopes are not
  treated as the same parameter.
- **Standalone references.** The copy button writes a self-contained Markdown selection snapshot, headed
  `# Cadiste selection v1`, that can be pasted directly into a chat. Reading it needs no MCP call, reference
  lookup, running server, or cache entry. It captures the project root, units, target and build identities,
  selected features and instances, primary code references and component ancestry, available feature arguments,
  call stacks and captured source, with exact geometry as supporting evidence.
  Folder picks include their owning part and available part provenance. Missing or unverifiable context is
  reported explicitly rather than guessed. A snapshot describes the captured build, not necessarily current source.
- **Multiple selection.** Click replaces the selection; Ctrl/Cmd-click toggles an item, and Shift-click adds
  items (a range in a feature tree). Features, assembly instances, and faces, edges and vertices can be selected
  together, including across parts in a folder. The toolbar shows the selection count and a clear action;
  Escape clears it. The copy button and **C** copy the entire selection. With nothing selected, **C** can copy
  what the pointer is on; the copy button captures the whole target and lists its members.
- **Linking.** Copying a snapshot does not require or create an agent connection. The first live tool call
  that needs a viewer shows a request in every open tab, with the agent's name and a code (`swift-orca`)
  the agent also reports; **Link this tab** picks the tab, **Dismiss** fails the call. The link is kept by tab id,
  which survives a reload (a duplicated browser tab takes a new one); the top bar shows the agents linked to the
  tab, each with an unlink button. A call waits up to 60 s for the link. `show(view='assembly')` opens the first
  assembly, `view='assembly:NAME'` another.
- **Feedback.** `show(part=...)` builds the part again from the current source, opens it in the tab and answers with
  how it differs from the build before (volume, bounding box, faces, operations added, removed or called with other
  arguments: operations are matched by kind, mode and calling function, as line numbers move with edits) and
  screenshots of the tab's 3D view (`iso` by default, any preset direction or `current`). A failed build answers
  with its traceback.
- **Driving the tab.** `select(items=[...])` replaces the multi-selection (`[]` clears it) and answers with its
  captured context. Each item is a feature (`{"kind":"feature","part":"...","node":"o7"}`), an instance
  (`{"kind":"instance","path":"..."}`), or a face/edge/vertex (`{"kind":"entity","path":"..."}`).
  Also `highlight`, `set_view`, `set_visibility`, `screenshot`, `status`, `list_parts`, `feature_tree`, `describe`,
  and `measure`.

The workflow is the `cadiste` skill (`src/cadiste/skills/cadiste/SKILL.md`), served as `skill://cadiste/SKILL.md`
with the MCP Skills extension and summed up in the server's instructions; a harness that loads skills from disk can
use the file as is. Answers are text, with ids the other tools take: `[o7]` an operation, `[b2]` a builder, `[g3]`
a function-call group, and viewer paths (`/Group/<part>/faces/faces_12`, face `f12`).

For example, follow the sleeve diameter from `SteeringModule.__init__` with `expression="tube_d"`, or inspect
`NoseChamfer.lid_end_x` and find its consumers in both `SteeringModule` and `SteeringLid`. These are code-navigation
tool calls, not temporary inspection scripts or viewer-only operations.

## Trace a model

`cadiste trace module:callable [ARG ...]` imports the callable, calls it with the string arguments inside the
tracer, and prints the feature tree of every outermost builder, then a summary. `--faces` adds every face of the
returned shape with the operation that made it (face `fN` is `shape.faces()[N]`).

Run it in the model project's environment, with cadiste layered on top:

```sh
cd ../pipecrawler
PIPECRAWLER_CACHE_DIR="" uv run --with-editable ../cadiste cadiste trace pipecrawler.catalog:build_part "Steering Module" --faces
```

Disable the project's part cache if it has one: a part loaded from a BREP file was not built, so there is nothing
to trace and its faces match no operation.

```text
BuildPart body (add)  steering_module.py:72  [SteeringModule]
├─ #0 SpanBox add  steering_module.py:73  SpanBox((0, body_end), _symmetric(half_w), _symmetric(top))
├─ hinge_drum()  steering_module.py:74
│  ├─ #1 AxisCylinder add  features.py:64  AxisCylinder("Y", 2 * p.hinge_r, _symmetric(p.drum_half_w), at=at)
│  ├─ #2 AxisCylinder subtract  features.py:66  AxisCylinder("Y", cheek_d, (-half_w - 1, -p.drum_half_w), at=at, mode=Mode.SUBT…
│  ├─ #3 AxisCylinder subtract  features.py:67  AxisCylinder("Y", cheek_d, (p.drum_half_w, half_w + 1), at=at, mode=Mode.SUBTRA…
│  ├─ hinge_relief()  features.py:68
│  │  ├─ BuildSketch relief (private)  features.py:42
│  │  │  ├─ #4 Polygon add  features.py:44  Polygon((x0, 0), (edge_x, sz * edge_z), (x0, sz * edge_z), align=None)
│  │  │  ├─ #5 Polygon add  features.py:44  Polygon((x0, 0), (edge_x, sz * edge_z), (x0, sz * edge_z), align=None)
│  │  │  └─ #6 Circle subtract  features.py:46  Circle(p.hinge_r + p.joint_gap, mode=Mode.SUBTRACT)
│  │  └─ #7 CrossPrism subtract  features.py:47  CrossPrism(sketch_of(relief), "Y", _symmetric(p.module_w / 2 + spec.relief_marg…  ← relief
…
f36   PLANE         548.78 mm²  (  84.84,    0.00,   17.50)  ← #0 SpanBox steering_module.py:73
f37   PLANE         803.53 mm²  (  22.81,    0.00,   13.17)  ← #7 CrossPrism features.py:47
…
5 builders, 21 operations, build 0.13 s; 43/43 faces traced to an operation in 0.01 s
```

From Python:

```python
from cadiste import Provenance, feature_tree, render, tracing

with tracing() as trace:
    part = build_my_part()
origins = Provenance(trace).face_origins(part)  # per face: (outer op, ..., op that made it), or None
for record in trace.builders:
    if record.parent is None:
        print("\n".join(render(feature_tree(record))))
```

## How it works

| Module | Role |
|---|---|
| `trace.py` | The hooks. Every builder becomes a `BuilderRecord` (kind, mode, `with` site, `as` name, owning shape object, product); every change to a builder's object becomes an `Op` (kind, mode, call stack, arguments, object before/after, the builders whose products it consumed). |
| `tree.py` | A builder's operations and child builders, grouped by the model-code function calls between the builder's `with` and the event; text rendering. |
| `provenance.py` | Face → operation, through the `ShapeHistory` records. |
| `export.py` | A trace as the viewer's JSON, with the source of the model files it points into, the result's volume and bounding box, and the scene: the part, every sketch, every sketch element in place. |
| `mesh.py` | Shapes tessellated by `ocp_tessellate` into three-cad-viewer's format, with each object's viewer path and each leaf's faces, edges and vertices placed and numbered as the viewer has them. |
| `measure.py` | Those faces, edges and vertices written next to a build's JSON and read back; exact measurements of the ones the viewer picked, and the faces an edge or vertex bounds. |
| `server.py` | `cadiste serve`: the built frontend, the part list, one `cadiste export` subprocess per trace, one `cadiste mesh` per assembly view, through the cache; one `cadiste save` per CAD export; measurements on the last few builds' shapes, kept loaded; the hub's endpoints. |
| `hub.py` | Agent sessions and viewer tabs: link requests, each tab's event stream (Server-Sent Events) and state, requests to a tab and their replies. |
| `agent.py` | What `cadiste mcp`'s tools do, run in the server: cached builds, linked-tab state, and self-contained selection snapshots. |
| `references.py` | Self-contained Markdown selection snapshots, ordered multi-selection validation, build identity checks, and captured geometry/provenance; no lookup store. |
| `context.py` | The builds' JSON as text for an agent: feature tree, one feature in detail, the selection resolved to source, the comparison of two builds. |
| `bridge.py` | `cadiste mcp`: the MCP server on stdio forwarding each tool to the server, and the skill. |
| `cache.py` | Traces and scenes on disk, by part, for one fingerprint of the model's source. |
| `project.py` | The project as the server sees it before building: part names, their folders, the source directories. |
| `source.py` | Captured project Python, line-independent definition references, and source-revision digests. |
| `code_index.py` | Static Python definition, import, alias, parameter, call and dependency analysis over captured source. |
| `components.py` | Source-linked construction hierarchy and component/parameter/relationship navigation used by MCP and snapshots. |
| `web/` | The React frontend (Vite, TypeScript, three-cad-viewer). |

- **Builder entry is hooked through `_build_scope_context`, not `Builder.__enter__`.** `__enter__` makes the
  active builder a parent only when the caller's frame matches, so any wrapper frame would orphan nested builders
  (a `BuildLine` would stop feeding its `BuildSketch`).
- **Operation kind.** A builder object (`Box(...)`, or a custom `BasePartObject` such as pipecrawler's `SpanBox`)
  is named after its class; an operation function (`chamfer`, `split`, `insert`, `revolve`) after the function; a
  child builder handing its product over at exit after the builder.
- **Call stacks** keep model-code frames only: everything under the standard library or a site-packages directory
  is library code. An editable install keeps the model outside site-packages; a model installed non-editable is
  invisible to the tracer.
- **Groups.** Every call of a model function inside a builder's block is one group (`hinge_drum()` called at
  `steering_module.py:74`). A shape's `__init__` is named after its class (`DriveModule()`).
- **Inputs.** An operation consumed a builder when one of its arguments is that builder, its product (what
  `builder.sketch` / `builder.part` returns after exit), or the shape object whose `__init__` entered it
  (`insert(DriveModule(...))` consumed `DriveModule`'s `body`, not the sketches nested in it).
- **Arguments.** A builder object's are bound to its class's `__init__`; an operation function's are read from its
  frame's parameters, so the ones not passed show the value the operation settled on (`chamfer`'s `length2` shows
  its `length`). The call's source is parsed to pair each parameter with the expression written for it; a
  `*starred` argument hides which positional expression is which.
- **Scene.** A sketch is shown as its product (placed on its plane). An element is the object the operation made in
  the sketch's local coordinates, placed the way build123d places the sketch's product at exit (the scope captured
  at `with` time), then the enclosing sketch's for a `BuildLine`. The viewer numbers a part's faces
  `<path>/faces/faces_<i>` in `shape.faces()` order; highlighting and picking by face are on when the part's mesh
  has exactly its faces (every pipecrawler part).
- **Provenance.** For each operation of a builder, in order, each face of the result:
  1. descends (untouched or rebuilt) from a face that was there before: it keeps that face's origin, so a top
     face trimmed by a later hole still belongs to the box;
  2. else descends from a shape the operation brought in: it is the operation's, followed into the builder that
     made that shape when it is another traced builder's product of the same kind (an inserted part), so the
     origin is a path from the outer operation to the innermost one;
  3. else, generated (a chamfer face from an edge): it is the operation's;
  4. else the record says nothing about it (`split` keeps no history in 0.13): a face with an interior point on a
     face from before the operation is a piece of it and keeps its origin; any other face is the operation's.
- **Measurements** are taken on the B-rep, not on the mesh three-cad-viewer's own tools and the hover card measure:
  a 4 mm hole measures 4.000, not the chord of its polygon. A build writes, next to its JSON, every leaf of the
  viewer's tree with its faces, edges and vertices in the viewer's numbering (`faces_<i>` is the `i`-th face
  ocp_tessellate meshed, its edges are those bounding a face), each placed by the locations the viewer composes
  from the root: an assembly's instances share a mesh but are measured where each one is. A leaf whose mesh does
  not hold exactly its shape's faces (or edges, or vertices) cannot have those measured. Minimum distances come
  from `BRepExtrema_DistShapeShape`; centres, axes, planes and directions from the faces' surfaces and the edges'
  curves.

## Results on pipecrawler

All 37 catalog parts trace and render without error. On the 30 built with builders, every face is attributed to an
operation; the slowest provenance pass takes 0.06 s (a drive module half: 42 operations, 61 faces). Part scenes
weigh 3 KiB to 1.7 MB (the torsion spring); the assembly's 186 instances share 37 meshes. A part's shapes to measure
on weigh about 11 KiB (the steering module), the assembly's 2.4 MB; a measurement takes a few milliseconds, and the
first on a build also loads its shapes: well under 0.1 s for a part, 1.5 s for the assembly.

## Limits

- A part made without any builder (pipecrawler's shafts, pins, battery pack and motor can: one `AxisCylinder`
  object returned as is) has no feature tree, and its faces trace to nothing.
- A shape made outside builders and then inserted is one feature: a py_gearworks gear blank is one `insert`
  that owns every flank.
- Sketch elements are linked to the operation that consumed the sketch, not yet to the 3D faces an extrusion
  generated from each of them.
- A sketch consumed through the builder's pending faces (a non-private `BuildSketch` followed by `revolve()` or
  `extrude()` without a profile) is not linked to that operation: selecting the operation does not show its sketch.
- Face highlighting uses three-cad-viewer's highlight controller (`rendered.nestedGroup.highlight`), typed in its
  `.d.ts` but internal-level.
- `_add_to_context` calls with no objects, and `Mode.PRIVATE` ones, are not recorded (they change nothing).
- Measuring has no maximum distance (`BRepExtrema` gives the minimum only), and a circle or an arc gives no
  direction to measure an angle from. A distance to a straight edge's line or a cylinder's axis is to the infinite
  line, so its foot may lie past the edge's ends.
