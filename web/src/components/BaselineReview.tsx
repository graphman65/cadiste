export interface BaselineImage {
  image: string;
  build_id?: string;
}


/** Equal-camera images are captured by the caller from explicit retained builds, never latest-source rebuilds. */
export function BaselineReview({ baseline, current, onClose }: {
  baseline: BaselineImage & { id: string; name?: string };
  current: BaselineImage;
  onClose: () => void;
}) {
  return (
    <section className="baseline-review" role="dialog" aria-label="Baseline side-by-side review" aria-modal="false">
      <header className="readout-head">
        <strong>{baseline.name ?? baseline.id} · same-camera comparison</strong>
        <button type="button" onClick={onClose} aria-label="Close baseline review">Close</button>
      </header>
      <div className="baseline-images" style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 8 }}>
        <figure style={{ margin: 0, minWidth: 0 }}>
          <figcaption>Baseline {baseline.build_id ?? ""}</figcaption>
          <img src={baseline.image.startsWith("data:") || baseline.image.startsWith("blob:") ? baseline.image : `data:image/png;base64,${baseline.image}`} alt="Pinned baseline at the comparison camera" style={{ width: "100%", display: "block" }} />
        </figure>
        <figure style={{ margin: 0, minWidth: 0 }}>
          <figcaption>Current {current.build_id ?? ""}</figcaption>
          <img src={current.image.startsWith("data:") || current.image.startsWith("blob:") ? current.image : `data:image/png;base64,${current.image}`} alt="Current build at the same comparison camera" style={{ width: "100%", display: "block" }} />
        </figure>
      </div>
      <p className="muted">Visual evidence only. Equal images, bounds or volume do not prove shape equivalence or mechanical fit.</p>
    </section>
  );
}
