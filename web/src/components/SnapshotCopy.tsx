import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Icon } from "./Icon";

export type SnapshotCopyState =
  | { phase: "generating" }
  | { phase: "failed"; error: string }
  | { phase: "ready"; text: string; copying: boolean };

/** Keep a generated snapshot available when clipboard permission or browser support is missing. */
export function SnapshotCopy({ state, onClose, onCopied }: {
  state: SnapshotCopyState;
  onClose: () => void;
  onCopied: () => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const text = useRef<HTMLTextAreaElement>(null);
  const [copying, setCopying] = useState(false);
  const [denied, setDenied] = useState(false);
  const mounted = useRef(true);
  const pending = state.phase === "generating" || state.phase === "ready" && state.copying;
  useLayoutEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useLayoutEffect(() => {
    if (pending) return;
    const previous = document.activeElement;
    close.current?.focus();
    const element = root.current;
    return () => {
      if ((element?.contains(document.activeElement) || document.activeElement === document.body) &&
          previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  }, [pending]);
  const ready = state.phase === "ready" && !state.copying;
  useEffect(() => {
    if (ready) {
      text.current?.focus();
      text.current?.select();
    }
  }, [ready]);

  const copy = () => {
    if (state.phase !== "ready" || state.copying || copying) return;
    setDenied(false);
    // Invoke the API directly in this new button gesture; never regenerate the server snapshot.
    let written: Promise<void>;
    try {
      if (window.isSecureContext && navigator.clipboard?.writeText !== undefined) {
        written = navigator.clipboard.writeText(state.text);
      } else {
        text.current?.focus();
        text.current?.select();
        if (!document.execCommand("copy")) throw new Error("Clipboard copy was refused");
        written = Promise.resolve();
      }
    } catch {
      setDenied(true);
      text.current?.focus();
      text.current?.select();
      return;
    }
    setCopying(true);
    void written.then(() => {
      if (mounted.current) onCopied();
    }, () => {
      if (!mounted.current) return;
      setCopying(false);
      setDenied(true);
      text.current?.focus();
      text.current?.select();
    });
  };

  if (pending) return <div className="notice snapshot-copy-toast" role="status" ref={root}>
    <span>Copying…</span>
    <button className="icon-button" aria-label="Cancel snapshot copy" title="Cancel"
      onClick={onClose} data-action="close-snapshot-copy"><Icon name="close" /></button>
  </div>;

  return <div className="snapshot-copy link-card" role="dialog" aria-label="Copy selection snapshot" aria-describedby="snapshot-copy-status" ref={root}>
    <div className="link-title"><strong>Selection snapshot</strong><span className="spacer" />
      <button ref={close} onClick={onClose} data-action="close-snapshot-copy">Close</button>
    </div>
    <p id="snapshot-copy-status" role={state.phase === "failed" ? "alert" : "status"}>
      {state.phase === "failed" ? `Could not generate snapshot: ${state.error}` :
        denied ? "Clipboard blocked. Copy the text below." : "Copy the snapshot below."}
    </p>
    {state.phase === "ready" && <>
      <label htmlFor="snapshot-copy-text">Read-only snapshot</label>
      <textarea id="snapshot-copy-text" ref={text} readOnly value={state.text} spellCheck={false} />
      <p>Press Ctrl+C (Cmd+C on Mac), or Copy.</p>
      <div className="link-actions"><button disabled={state.copying || copying} onClick={copy} data-action="copy-generated-snapshot">{copying ? "Copying…" : "Copy"}</button></div>
    </>}
  </div>;
}
