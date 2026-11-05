import { useEffect, useRef, useState } from "react";
import type { QuaternionTuple, Vector3Tuple, VisibilityState } from "three-cad-viewer";

import type { SelectionItem } from "../types";
import type { Section } from "./Section";
import type { ViewSettings } from "./ViewToolbar";

/** App-owned state that determines visibility and feature focus, not just camera orientation. */
export interface SceneViewState {
  hidden: string[];
  selection: SelectionItem[];
  selected: string | null;
  focusPart: string | null;
  instance: string | null;
  litPart: string | null;
}

export interface SavedViewState {
  scene: SceneViewState;
  camera: { position: Vector3Tuple; quaternion: QuaternionTuple; target: Vector3Tuple; zoom: number };
  settings: ViewSettings;
  states: Record<string, VisibilityState>;
  section: { active: boolean; section: Section | null };
  explode: number | null;
  measuring: boolean;
}

interface SavedView {
  id: string;
  name: string;
  state: SavedViewState;
}

const record = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const tuple = (value: unknown, size: number) => Array.isArray(value) && value.length === size && value.every(finite);

function validState(value: unknown): value is SavedViewState {
  if (!record(value)) return false;
  const { scene, camera, settings, states, section, explode, measuring } = value;
  if (!record(scene) || !Array.isArray(scene.hidden) || !scene.hidden.every((item) => typeof item === "string") ||
      ![scene.selected, scene.focusPart, scene.instance, scene.litPart].every((item) => item === null || typeof item === "string") || !Array.isArray(scene.selection)) return false;
  if (!scene.selection.every((item: unknown) => record(item) && (
    (item.kind === "feature" && typeof item.part === "string" && typeof item.node === "string") ||
    ((item.kind === "entity" || item.kind === "instance") && typeof item.path === "string")
  ))) return false;
  if (!record(camera) || !tuple(camera.position, 3) || !tuple(camera.quaternion, 4) || !tuple(camera.target, 3) || !finite(camera.zoom) || camera.zoom <= 0) return false;
  if (!record(settings) || !["ortho", "transparent", "edges", "axes", "grid", "tools"].every((key) => typeof settings[key] === "boolean")) return false;
  if (!record(states) || !Object.values(states).every((state) => tuple(state, 2) && (state as number[]).every((item) => [0, 1, 2, 3].includes(item)))) return false;
  if (!record(section) || typeof section.active !== "boolean") return false;
  if (section.section !== null) {
    const cut = section.section;
    if (!record(cut) || !record(cut.plane) || typeof cut.plane.label !== "string" || !tuple(cut.plane.point, 3) ||
        !tuple(cut.plane.normal, 3) || !finite(cut.offset) || typeof cut.flipped !== "boolean") return false;
  }
  return (explode === null || (finite(explode) && explode >= 0 && explode <= 1)) && typeof measuring === "boolean";
}

function readViews(key: string): SavedView[] {
  try {
    const stored: unknown = JSON.parse(window.localStorage.getItem(key) ?? "null");
    if (!record(stored) || stored.version !== 1 || !Array.isArray(stored.views)) return [];
    return stored.views.filter((view): view is SavedView => record(view) && typeof view.id === "string" &&
      typeof view.name === "string" && view.name.trim().length > 0 && validState(view.state));
  } catch {
    return [];
  }
}

/** Remounted by scope: views never leak between projects or part/assembly/folder targets. */
export function SavedViews({ scope, capture, restore }: {
  scope: string;
  capture: () => SavedViewState | null;
  restore: (state: SavedViewState) => void;
}) {
  const storageKey = `cadiste.saved-views.v1:${scope}`;
  const [views, setViews] = useState(() => readViews(storageKey));
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    input.current?.focus();
    const away = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setOpen(false);
      trigger.current?.focus();
    };
    document.addEventListener("pointerdown", away);
    root.current?.addEventListener("keydown", escape);
    const element = root.current;
    return () => {
      document.removeEventListener("pointerdown", away);
      element?.removeEventListener("keydown", escape);
    };
  }, [open]);

  const persist = (next: SavedView[]) => {
    try {
      window.localStorage.setItem(storageKey, JSON.stringify({ version: 1, views: next }));
      setViews(next);
      setError(null);
      return true;
    } catch {
      setError("Could not save views. Browser storage is unavailable or full.");
      return false;
    }
  };
  const captureCurrent = () => {
    const state = capture();
    if (state === null) setError("Wait for the model to finish rendering before saving a view.");
    return state;
  };
  const submit = () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    if (views.some((view) => view.id !== editing && view.name.toLocaleLowerCase() === trimmed.toLocaleLowerCase())) {
      setError("A view with that name already exists. Choose another name, or update the existing view.");
      return;
    }
    let next: SavedView[];
    if (editing === null) {
      const state = captureCurrent();
      if (state === null) return;
      next = [...views, { id: crypto.getRandomValues(new Uint32Array(4)).join("-"), name: trimmed, state }];
    } else {
      next = views.map((view) => view.id === editing ? { ...view, name: trimmed } : view);
    }
    if (persist(next)) {
      setName("");
      setEditing(null);
    }
  };

  return (
    <div className="saved-views" ref={root}>
      <button ref={trigger} aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen((value) => !value)} title="Save and restore named views">
        Saved views
      </button>
      {open && (
        <div className="saved-views-menu" role="dialog" aria-label="Saved views">
          <strong>Saved views</strong>
          <p>Camera, visibility, selection, display, explode and section. Kept in this browser for this model.</p>
          <div className="saved-views-list">
            {views.length === 0 && <p>No saved views yet.</p>}
            {views.map((view) => (
              <div className="saved-view-row" key={view.id}>
                <button className="saved-view-restore" title={`Restore ${view.name}`} onClick={() => {
                  restore(structuredClone(view.state));
                  setOpen(false);
                  trigger.current?.focus();
                }}>{view.name}</button>
                <button title={`Replace ${view.name} with the current view`} aria-label={`Update ${view.name}`} onClick={() => {
                  const state = captureCurrent();
                  if (state !== null) persist(views.map((item) => item.id === view.id ? { ...item, state } : item));
                }}>Update</button>
                <button aria-label={`Rename ${view.name}`} onClick={() => {
                  setEditing(view.id);
                  setName(view.name);
                  setError(null);
                  input.current?.focus();
                }}>Rename</button>
                <button aria-label={`Delete ${view.name}`} onClick={() => {
                  if (persist(views.filter((item) => item.id !== view.id)) && editing === view.id) {
                    setEditing(null);
                    setName("");
                  }
                }}>Delete</button>
              </div>
            ))}
          </div>
          <form onSubmit={(event) => { event.preventDefault(); submit(); }}>
            <input ref={input} aria-label="View name" placeholder="e.g. Overall or Drive detail" value={name} onChange={(event) => { setName(event.target.value); setError(null); }} />
            <button className="primary" type="submit" disabled={!name.trim()}>{editing === null ? "Save current view" : "Rename view"}</button>
            {editing !== null && <button type="button" onClick={() => { setEditing(null); setName(""); setError(null); }}>Cancel</button>}
          </form>
          {error !== null && <div className="saved-views-error" role="alert">{error}</div>}
        </div>
      )}
    </div>
  );
}
