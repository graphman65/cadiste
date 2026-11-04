import type { PointerEvent } from "react";

interface Props {
  /** "x" resizes a width (dragged left/right), "y" a height. */
  axis: "x" | "y";
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
  /** The handle sits on the panel's leading edge (a right-hand panel): dragging toward the start grows it. */
  reverse?: boolean;
}

/** A drag handle: the size follows the pointer from where the drag started, within [min, max]. */
export function Splitter({ axis, value, min, max, onChange, reverse = false }: Props) {
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const handle = event.currentTarget;
    const start = axis === "x" ? event.clientX : event.clientY;
    const startValue = value;
    // Capture: the drag keeps going over the 3D view, which would otherwise orbit.
    handle.setPointerCapture(event.pointerId);
    const move = (moved: globalThis.PointerEvent) => {
      const delta = (axis === "x" ? moved.clientX : moved.clientY) - start;
      onChange(Math.min(max, Math.max(min, startValue + (reverse ? -delta : delta))));
    };
    const end = () => {
      handle.removeEventListener("pointermove", move);
      handle.classList.remove("dragging");
    };
    handle.classList.add("dragging");
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", end, { once: true });
    handle.addEventListener("pointercancel", end, { once: true });
  };
  return (
    <div
      className={`splitter splitter-${axis}${reverse ? " reverse" : ""}`}
      role="separator"
      aria-orientation={axis === "x" ? "vertical" : "horizontal"}
      onPointerDown={onPointerDown}
    />
  );
}
