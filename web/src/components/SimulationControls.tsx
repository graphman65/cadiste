import { useEffect, useId, useMemo, useRef, useState } from "react";

import { fetchSimulationControls, sendSimulationCommand, SimulationCommandFailure } from "../api";
import type { SimulationCommand, SimulationCommandResult, SimulationControlsConfig, SimulationTarget } from "../types";
import type { LiveSimulation } from "../simulation";

function measured(value: number | null | undefined): string {
  return value == null ? "—" : value.toFixed(5);
}

function invalidSetpoint(value: string, command: SimulationCommand): string | null {
  if (value.trim() === "" || !Number.isFinite(Number(value))) return "Enter a finite setpoint.";
  if (Math.abs(Number(value)) > command.velocity_limit) {
    return `Setpoint must be between ${-command.velocity_limit} and ${command.velocity_limit} ${command.units}.`;
  }
  return null;
}

type Outcome = { error?: string; result: SimulationCommandResult };

type RuntimeStatus = {
  status: "not_started" | "launched" | "stopped" | "failed" | "exited";
  owned: boolean;
  pid: number | null;
  exit_code: number | null;
  active: { bundle_id: string; config_id: string; source_fingerprint: string; directory: string } | null;
  current: { config_id: string | null; source_fingerprint: string } | null;
  mismatch: { source: boolean; config: boolean | null } | null;
  prerequisites: { ros2: string | null; status: string; detail: string };
  logs: { path: string | null; text: string; truncated: boolean };
  error: string | null;
  inspection_error: string | null;
};

async function runtimeRequest(target: SimulationTarget, signal?: AbortSignal, action?: "start" | "stop" | "relaunch"): Promise<RuntimeStatus> {
  const response = await fetch(action === undefined
    ? `/api/simulation/runtime?${new URLSearchParams({ ...target })}`
    : "/api/simulation/runtime", action === undefined ? { signal } : {
    method: "POST", signal,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target, action, gui: true }),
  });
  const body = await response.json() as RuntimeStatus & { error?: string };
  if (!response.ok && body.status !== "failed") throw new Error(body.error ?? `Runtime request failed (${response.status})`);
  return body;
}

/** Explicit commands and separately labelled, measured ROS feedback. Closing the panel stops neither. */
export function SimulationControls({ target, live, onEnableLive, onDisableLive }: {
  target: SimulationTarget;
  live: LiveSimulation;
  onEnableLive: (config: SimulationControlsConfig) => void;
  onDisableLive: () => void;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [revision, setRevision] = useState(0);
  const [config, setConfig] = useState<SimulationControlsConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [pending, setPending] = useState(0);
  const [stopPending, setStopPending] = useState(false);
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  const [runtimePending, setRuntimePending] = useState(false);
  const generation = useRef(0);
  const mounted = useRef(true);
  const pendingCount = useRef(0);
  const stopQueued = useRef(false);
  // A Stop clicked during Send must be dispatched after that Send, even if the panel closes.
  const queue = useRef<Promise<void>>(Promise.resolve());
  const transmissionByMotor = useMemo(
    () => new Map(config?.transmissions.map((transmission) => [transmission.name, transmission]) ?? []),
    [config],
  );

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      generation.current += 1;
    };
  }, []);

  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    setConfig(null);
    setValues({});
    setOutcome(null);
    fetchSimulationControls({ kind: "assembly", name: target.name }, controller.signal)
      .then((loaded) => {
        if (generation.current !== current) return;
        setConfig(loaded);
        setValues(Object.fromEntries(loaded.commands.map((command) => [command.joint, "0"])));
      })
      .catch((error: unknown) => {
        if (generation.current !== current || controller.signal.aborted) return;
        setLoadError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        if (generation.current === current) setLoading(false);
      });
    return () => {
      generation.current += 1;
      controller.abort();
    };
  }, [revision, target.name]);

  useEffect(() => {
    setRuntime(null);
    setRuntimeError(null);
    if (!open) return;
    const controller = new AbortController();
    let active = true;
    let fetching = false;
    const refresh = async () => {
      if (fetching) return;
      fetching = true;
      try {
        const status = await runtimeRequest({ kind: "assembly", name: target.name }, controller.signal);
        if (active) { setRuntime(status); setRuntimeError(null); }
      } catch (error: unknown) {
        if (active && !controller.signal.aborted) setRuntimeError(error instanceof Error ? error.message : String(error));
      } finally { fetching = false; }
    };
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 5000);
    return () => { active = false; controller.abort(); clearInterval(timer); };
  }, [open, revision, target.name]);

  const runtimeAction = async (action: "start" | "stop" | "relaunch") => {
    if (runtimePending) return;
    const current = generation.current;
    setRuntimePending(true);
    setRuntimeError(null);
    try {
      const status = await runtimeRequest({ kind: "assembly", name: target.name }, undefined, action);
      if (mounted.current && generation.current === current) setRuntime(status);
    } catch (error: unknown) {
      if (mounted.current && generation.current === current) setRuntimeError(error instanceof Error ? error.message : String(error));
    } finally {
      if (mounted.current) setRuntimePending(false);
    }
  };

  const publish = (command: { values: Record<string, number> } | { stop_all: true }) => {
    if (config === null || stopQueued.current) return;
    const stoppingAll = "stop_all" in command;
    if (!stoppingAll && Object.values(command.values).some((value) => value !== 0) && pendingCount.current > 0) return;
    const current = generation.current;
    const snapshot = config;
    if (stoppingAll) {
      stopQueued.current = true;
      setStopPending(true);
      setValues(Object.fromEntries(snapshot.commands.map((joint) => [joint.joint, "0"])));
    }
    pendingCount.current += 1;
    setPending(pendingCount.current);
    setOutcome(null);
    queue.current = queue.current.then(async () => {
      try {
        const result = await sendSimulationCommand(snapshot.target, snapshot.config_id, command);
        if (mounted.current && generation.current === current) setOutcome({ result });
      } catch (error: unknown) {
        if (mounted.current && generation.current === current) {
          setOutcome({
            error: error instanceof Error ? error.message : String(error),
            result: error instanceof SimulationCommandFailure ? error.result : { published: {}, failed: {} },
          });
        }
      } finally {
        pendingCount.current -= 1;
        if (stoppingAll) stopQueued.current = false;
        if (mounted.current) {
          setPending(pendingCount.current);
          if (stoppingAll) setStopPending(false);
        }
      }
    });
  };

  const close = () => {
    setOpen(false);
  };

  const availability = loading ? "loading" : loadError !== null ? "error" : "cad";
  const displayStatus = live.enabled ? live.status : availability;
  const staleBundle = runtime?.mismatch?.source === true || runtime?.mismatch?.config === true;

  return (
    <div className="simulation-controls">
      <div className="simulation-mode-controls" data-view-mode={live.enabled ? "simulation" : "cad"}>
        <button
          className={live.enabled ? "simulation-live-toggle on" : "simulation-live-toggle"}
          aria-pressed={live.enabled}
          disabled={!live.enabled && config === null}
          onClick={() => live.enabled ? onDisableLive() : config !== null && onEnableLive(config)}
          aria-describedby={`${id}-availability`}
          title={!live.enabled && config === null
            ? loadError ?? "Loading the simulation adapter, including its CAD geometry. Live simulation becomes available when this finishes."
            : "Measured ROS TF and joint states drive the adapter scene. CAD picking, snapshots and saved views are unavailable in this mode."}
        >Live simulation</button>
        <span id={`${id}-availability`} className={`simulation-live-status state-${displayStatus}`} role="status" data-feedback-status={displayStatus}>
          {live.enabled ? live.status : loading ? "Loading simulation controls…" : loadError !== null ? "Controls unavailable" : "CAD view"}
        </span>
        {live.enabled && <button onClick={() => publish({ stop_all: true })} disabled={config === null || config.commands.length === 0 || stopPending} className="simulation-stop-all">Stop all</button>}
      </div>
      <button
        className="simulation-controls-trigger"
        aria-expanded={open}
        aria-controls={`${id}-panel`}
        onClick={() => open ? close() : setOpen(true)}
      >
        ROS 2 Controls
      </button>
      {!open && !live.enabled && loadError !== null && (
        <div className="simulation-controls-panel simulation-controls-unavailable" role="alert">
          <p className="simulation-controls-error">{loadError}</p>
          <button onClick={() => setRevision((value) => value + 1)}>Retry controls</button>
        </div>
      )}
      {open && (
        <section className="simulation-controls-panel" id={`${id}-panel`} aria-labelledby={`${id}-title`}>
          <div className="simulation-controls-header">
            <h2 id={`${id}-title`}>ROS 2 Controls</h2>
            <button aria-label="Close ROS 2 Controls" onClick={close}>Close</button>
          </div>
          <p className="simulation-controls-warning">
            Commands are latched by the simulator. Closing this panel or changing the view does not stop motion.
            Use explicit Stop or Stop all.
          </p>
          <div className="simulation-controls-actions">
            <button
              className="simulation-stop-all"
              disabled={config === null || config.commands.length === 0 || stopPending}
              onClick={() => publish({ stop_all: true })}
            >
              {stopPending ? "Stop all queued / publishing…" : "Stop all"}
            </button>
            <button disabled={loading || pending > 0} onClick={() => { onDisableLive(); setRevision((value) => value + 1); }}>
              Reload controls
            </button>
          </div>
          <div className="simulation-controls-body">
            <div className="muted">Assembly: {target.name}</div>
            <section className="simulation-runtime" aria-label="Simulation runtime lifecycle">
              <h3>Gazebo launcher</h3>
              <p role="status">{runtimePending ? "Applying explicit runtime action…" : runtime === null ? "Loading launcher status…" : `Launcher: ${runtime.status}${runtime.pid === null ? "" : ` · PID ${runtime.pid}`}`}</p>
              <p className="muted">A launched process is not measured live feedback. External processes and containers are never stopped here.</p>
              {runtime !== null && <>
                <p>{runtime.prerequisites.detail}</p>
                <div className="simulation-controls-actions">
                  <button disabled={runtimePending || (runtime.owned && runtime.exit_code === null) || runtime.prerequisites.ros2 === null} onClick={() => void runtimeAction("start")}>Launch Gazebo</button>
                  <button disabled={runtimePending || !runtime.owned || runtime.exit_code !== null} onClick={() => void runtimeAction("stop")}>Stop owned launcher</button>
                  <button disabled={runtimePending || runtime.active === null || runtime.prerequisites.ros2 === null} onClick={() => void runtimeAction("relaunch")}>Rebuild bundle and relaunch</button>
                </div>
                {staleBundle && <p className="simulation-controls-error" role="alert">Stale simulation bundle: CAD source or controls changed. The running bundle was not updated. Explicitly rebuild and relaunch, then reload controls.</p>}
                {runtime.active !== null && <details>
                  <summary>Active bundle and source identity</summary>
                  <p>Bundle: <code>{runtime.active.bundle_id}</code><br />Directory: <code>{runtime.active.directory}</code></p>
                  <p>Bundle config: <code>{runtime.active.config_id}</code><br />Current config: <code>{runtime.current?.config_id ?? "unavailable"}</code></p>
                  <p>Bundle source fingerprint: <code>{runtime.active.source_fingerprint}</code><br />Current source fingerprint: <code>{runtime.current?.source_fingerprint ?? "unavailable"}</code></p>
                  <p className="muted">Source fingerprints track conservative project changes; they are not geometry build IDs or captured source revisions.</p>
                </details>}
                {runtime.error !== null && <p className="simulation-controls-error" role="alert">{runtime.error}</p>}
                {runtime.inspection_error !== null && <p className="simulation-controls-error" role="alert">Current source inspection failed: {runtime.inspection_error}</p>}
                <details>
                  <summary>Retained launcher logs{runtime.logs.truncated ? " (tail)" : ""}</summary>
                  {runtime.logs.path !== null && <p><code>{runtime.logs.path}</code></p>}
                  <pre>{runtime.logs.text || "No launcher output recorded."}</pre>
                </details>
              </>}
              {runtimeError !== null && <p className="simulation-controls-error" role="alert">{runtimeError}</p>}
            </section>
            {loading && <p role="status"><span className="spinner" /> Loading controls…</p>}
            {loadError !== null && <div className="simulation-controls-error" role="alert">{loadError}</div>}
            {live.enabled && (
              <section className="simulation-feedback" aria-label="Measured simulation state">
                <h3>Measured ROS state</h3>
                <p>World frame: <code>{live.config?.feedback.world_frame}</code> · ROS time: <output data-feedback-stamp={live.feedback?.stamp ?? undefined}>{live.feedback?.stamp == null ? "—" : `${live.feedback.stamp.toFixed(3)} s`}</output>
                  {" · "}Data age: {live.feedback?.age == null ? "—" : `${live.feedback.age.toFixed(2)} s`}</p>
                <p className="simulation-world-pose" data-model-position={live.feedback?.model_pose?.position.join(",")} data-model-quaternion={live.feedback?.model_pose?.quaternion.join(",")}>
                  Model world position (m): {live.feedback?.model_pose?.position.map((value) => measured(value)).join(", ") ?? "—"}<br />
                  World quaternion (xyzw): {live.feedback?.model_pose?.quaternion.map((value) => measured(value)).join(", ") ?? "—"}
                </p>
                {live.status !== "live" && <p className="muted">Not live. Values, if present, are the last measured samples, not current motion.</p>}
                {live.error && <p className="simulation-controls-error" role="alert">{live.error}</p>}
                {(live.feedback?.missing_frames.length ?? 0) > 0 && <p className="muted">Missing / stale frames: {live.feedback?.missing_frames.join(", ")}</p>}
                <details className="simulation-joint-measurements">
                  <summary>Physical joint measurements ({live.config?.feedback.joints.length ?? 0})</summary>
                  <div className="simulation-measurements-scroll">
                    <table className="simulation-measured-joints">
                      <thead><tr><th>Joint</th><th>Position q</th><th>Velocity v</th><th>Effort</th></tr></thead>
                      <tbody>{(live.config?.feedback.joints ?? []).map((joint) => {
                        const sample = live.feedback?.joints[joint];
                        const units = live.config?.commands.find((command) => command.joint === joint)?.units;
                        return <tr key={joint} data-measured-joint={joint}>
                          <th scope="row">{joint}<small>{units === "rad/s" ? "q: rad · v: rad/s" : units === "m/s" ? "q: m · v: m/s" : "ROS SI units"}</small></th>
                          <td><output data-quantity="position" data-value={sample?.position ?? undefined}>{measured(sample?.position)}</output></td>
                          <td><output data-quantity="velocity" data-value={sample?.velocity ?? undefined}>{measured(sample?.velocity)}</output></td>
                          <td><output data-quantity="effort" data-value={sample?.effort ?? undefined}>{measured(sample?.effort)}</output></td>
                        </tr>;
                      })}</tbody>
                    </table>
                  </div>
                </details>
              </section>
            )}
            {config !== null && (
              <>
                <p className="simulation-controls-model">Model: <strong>{config.model}</strong></p>
                <details>
                  <summary>Declared joint topology and environment</summary>
                  <p>Model-relative zero-pose positions are metres; rotations use xyzw quaternions. Axes are normalized in each joint frame. These are CAD declarations, not measurements.</p>
                  <p>Robot links: {config.topology.links.map((link) => link.name).join(", ")}</p>
                  <ul>{config.topology.joints.map((joint) => <li key={joint.name}>
                    <strong>{joint.name}</strong>: {joint.parent} → {joint.child} · {joint.kind} · {joint.actuated ? "actuated" : "passive"}
                    <br />Axis: {joint.axis.join(", ")} · Position limits: {joint.kind === "revolute" ? `${joint.lower} … ${joint.upper} rad` : joint.kind === "continuous" ? "continuous" : "fixed"}
                    <br />Zero pose: {joint.pose.position.join(", ")} m; quaternion: {joint.pose.quaternion.join(", ")}
                    <br />Output velocity: ±{joint.velocity} rad/s · effort: {joint.effort} N·m
                  </li>)}</ul>
                  <p>Static environment (not robot links): {config.topology.environment.map((link) => link.name).join(", ") || "none declared"}</p>
                </details>
                <p className="muted">Velocity setpoints below are commands, not measured state. Editing does not send a command.</p>
                {config.commands.length === 0 && <p>No velocity commands are configured for this simulation.</p>}
                {config.commands.map((command, index) => {
                  const value = values[command.joint] ?? "0";
                  const invalid = invalidSetpoint(value, command);
                  const inputId = `${id}-joint-${index}`;
                  const transmission = transmissionByMotor.get(command.joint);
                  const motorSample = live.feedback?.joints[command.joint];
                  const outputSample = transmission === undefined ? undefined : live.feedback?.joints[transmission.joint];
                  return (
                    <fieldset className="simulation-joint" key={command.joint}>
                      <legend>{command.joint}</legend>
                      <code className="simulation-topic">{command.topic}</code>
                      <div className="simulation-joint-limits">
                        {transmission === undefined ? "Joint" : "Motor input"} velocity limit: ±{command.velocity_limit} {command.units} · Effort limit: {command.effort_limit} N·m
                      </div>
                      {transmission !== undefined && (
                        <div className="simulation-joint-limits">
                          Ideal transmission → <code>{transmission.joint}</code><br />
                          Output speed = {transmission.ratio} × motor speed; lossless torque ratio = {1 / transmission.ratio}.
                        </div>
                      )}
                      {live.enabled && transmission !== undefined && (
                        <table className="simulation-measured-joints" aria-label={`${command.joint} transmission state`}>
                          <thead><tr><th>State</th><th>q (rad)</th><th>v (rad/s)</th><th>Torque (N·m)</th></tr></thead>
                          <tbody>
                            <tr data-derived-motor={command.joint}>
                              <th scope="row">Ideal motor<small>Derived from output, not an encoder</small></th>
                              <td><output data-quantity="position" data-value={motorSample?.position ?? undefined}>{measured(motorSample?.position)}</output></td>
                              <td><output data-quantity="velocity" data-value={motorSample?.velocity ?? undefined}>{measured(motorSample?.velocity)}</output></td>
                              <td><output data-quantity="effort" data-value={motorSample?.effort ?? undefined}>{measured(motorSample?.effort)}</output></td>
                            </tr>
                            <tr data-transmission-output={transmission.joint}>
                              <th scope="row">Measured output<small>{transmission.joint}</small></th>
                              <td><output data-quantity="position" data-value={outputSample?.position ?? undefined}>{measured(outputSample?.position)}</output></td>
                              <td><output data-quantity="velocity" data-value={outputSample?.velocity ?? undefined}>{measured(outputSample?.velocity)}</output></td>
                              <td><output data-quantity="effort" data-value={outputSample?.effort ?? undefined}>{measured(outputSample?.effort)}</output></td>
                            </tr>
                          </tbody>
                        </table>
                      )}
                      <label htmlFor={inputId}>Setpoint ({command.units})</label>
                      <input
                        id={inputId}
                        type="number"
                        step="any"
                        min={-command.velocity_limit}
                        max={command.velocity_limit}
                        value={value}
                        aria-label={`${command.joint} setpoint (${command.units})`}
                        aria-invalid={invalid !== null}
                        aria-describedby={invalid !== null ? `${inputId}-error` : undefined}
                        onChange={(event) => setValues((current) => ({ ...current, [command.joint]: event.target.value }))}
                      />
                      <input
                        type="range"
                        aria-label={`${command.joint} setpoint slider (${command.units})`}
                        min={-command.velocity_limit}
                        max={command.velocity_limit}
                        step="any"
                        value={invalid === null ? Number(value) : 0}
                        onChange={(event) => setValues((current) => ({ ...current, [command.joint]: event.target.value }))}
                      />
                      {invalid !== null && <div className="simulation-controls-error" id={`${inputId}-error`}>{invalid}</div>}
                      <div className="simulation-joint-actions">
                        <button
                          className="primary"
                          aria-label={`Send ${command.joint} setpoint`}
                          disabled={invalid !== null || pending > 0 || staleBundle}
                          onClick={() => {
                            if (invalidSetpoint(value, command) === null) publish({ values: { [command.joint]: Number(value) } });
                          }}
                        >Send</button>
                        <button
                          aria-label={`Stop ${command.joint}`}
                          disabled={stopPending}
                          onClick={() => {
                            setValues((current) => ({ ...current, [command.joint]: "0" }));
                            publish({ values: { [command.joint]: 0 } });
                          }}
                        >Stop</button>
                      </div>
                    </fieldset>
                  );
                })}
              </>
            )}
          </div>
          <div className="simulation-controls-status" role="status" aria-live="polite">
            {pending > 0 && <p>{pending} command request{pending === 1 ? "" : "s"} pending. {stopPending && "Stop all will follow earlier sends."}</p>}
            {outcome !== null && (
              <>
                {outcome.error && <p className="simulation-controls-error">{outcome.error}</p>}
                {Object.entries(outcome.result.published).map(([joint, value]) => (
                  <p key={joint}>Published {joint}: {value} {config?.commands.find((command) => command.joint === joint)?.units}</p>
                ))}
                {Object.entries(outcome.result.failed).map(([joint, error]) => (
                  <p className="simulation-controls-error" key={joint}>{joint}: {error}</p>
                ))}
              </>
            )}
            <p className="muted">A publish acknowledgement is not motor feedback. Stop all is an explicit zero setpoint, not an emergency stop.</p>
          </div>
        </section>
      )}
    </div>
  );
}
