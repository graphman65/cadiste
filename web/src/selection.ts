import type { SelectionItem } from "./types";

export type SelectionMode = "replace" | "add" | "toggle";
export interface SelectionModifiers { ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }

export function selectionMode(event: SelectionModifiers): SelectionMode {
  return event.ctrlKey || event.metaKey ? "toggle" : event.shiftKey ? "add" : "replace";
}

export function selectionKey(item: SelectionItem): string {
  return item.kind === "feature" ? JSON.stringify([item.kind, item.part, item.node]) : JSON.stringify([item.kind, item.path]);
}

export function updateSelection(current: SelectionItem[], items: SelectionItem[], mode: SelectionMode): SelectionItem[] {
  const unique = new Map(items.map((item) => [selectionKey(item), item]));
  if (mode === "replace") return [...unique.values()];
  const next = new Map(current.map((item) => [selectionKey(item), item]));
  for (const [key, item] of unique) {
    if (mode === "toggle" && next.has(key)) next.delete(key);
    else next.set(key, item);
  }
  return [...next.values()];
}
