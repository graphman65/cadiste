import { useCallback, useEffect, useRef, useState } from "react";

import { fetchSimulationScene, listenSimulationFeedback } from "./api";
import type { SimulationControlsConfig, SimulationFeedback, SimulationScene, SimulationTarget } from "./types";

export interface LiveSimulation {
  enabled: boolean;
  status: "cad" | "loading" | "waiting" | "live" | "stale" | "disconnected" | "error";
  scene: SimulationScene | null;
  feedback: SimulationFeedback | null;
  error: string | null;
  config: SimulationControlsConfig | null;
}

const CAD: LiveSimulation = { enabled: false, status: "cad", scene: null, feedback: null, error: null, config: null };

/** Own the HTTP/SSE lifetime independently of the collapsible controls panel. */
export function useLiveSimulation(target: SimulationTarget | null) {
  const key = target?.name ?? null;
  const [state, setState] = useState<{ key: string | null; value: LiveSimulation }>({ key: null, value: CAD });
  const generation = useRef(0);
  const cleanup = useRef<(() => void) | null>(null);
  const stop = useCallback(() => {
    generation.current += 1;
    cleanup.current?.();
    cleanup.current = null;
  }, []);
  const disable = useCallback(() => {
    stop();
    setState({ key: null, value: CAD });
  }, [stop]);
  useEffect(() => {
    disable();
    return stop;
  }, [key, disable, stop]);

  const enable = useCallback((config: SimulationControlsConfig) => {
    stop();
    if (key === null || config.target.name !== key) return;
    const current = generation.current;
    const controller = new AbortController();
    let receivedAt: number | null = null;
    let receivedAge: number | null = null;
    let feedbackError = false;
    let active = true;
    const update = (change: Partial<LiveSimulation>) => {
      if (active && current === generation.current) {
        setState((previous) => previous.key === key ? { key, value: { ...previous.value, ...change } } : previous);
      }
    };
    setState({ key, value: { ...CAD, enabled: true, status: "loading", config } });
    const closeFeedback = listenSimulationFeedback(config.target, config.config_id, (feedback) => {
      receivedAt = Date.now();
      receivedAge = feedback.age;
      feedbackError = feedback.status === "error";
      update({ feedback, status: feedback.status, error: feedback.error ?? null });
    }, (error) => {
      if (!feedbackError) update({ status: "disconnected", error });
    });
    fetchSimulationScene(config.target, config.config_id, controller.signal).then((scene) => {
      if (scene.config_id !== config.config_id || scene.model !== config.model) throw new Error("Simulation scene belongs to a different control configuration. Reload controls.");
      update({ scene });
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      closeFeedback();
      active = false;
      if (current === generation.current) setState((previous) => ({ key, value: { ...previous.value, status: "error", error: error instanceof Error ? error.message : String(error) } }));
    });
    const timer = window.setInterval(() => {
      if (receivedAt !== null && Date.now() - receivedAt > 1000 && active && current === generation.current) {
        const age = receivedAge === null ? null : receivedAge + (Date.now() - receivedAt) / 1000;
        setState((previous) => previous.key === key && (previous.value.status === "live" || previous.value.status === "stale")
          ? { key, value: { ...previous.value, status: "stale", feedback: previous.value.feedback === null ? null : { ...previous.value.feedback, age } } } : previous);
      }
    }, 250);
    cleanup.current = () => {
      active = false;
      controller.abort();
      closeFeedback();
      window.clearInterval(timer);
    };
  }, [key, stop]);

  const current = state.key === key ? state.value : CAD;
  const live: LiveSimulation = current.enabled && current.scene === null && !["error", "disconnected"].includes(current.status)
    ? { ...current, status: "loading" } : current;
  return { live, enable, disable };
}
