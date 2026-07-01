/**
 * Aurora — fixed ambient background stack for the whole app.
 * Layers (z-index):
 *   0  aurora-stage + wavefield/spectrum layers
 *   1  aurora-grain (film grain via SVG data URI)
 *   2  aurora-vignette (edge darkening)
 * App content sits above these via the layout's z-index.
 */
export default function Aurora() {
  return (
    <>
      <div className="aurora-stage" aria-hidden="true">
        <div className="aurora-grid" />
        <div className="aurora-wavefield" />
        <div className="aurora-spectrum" />
        <div className="aurora-scanlines" />
      </div>
      <div className="aurora-grain" aria-hidden="true" />
      <div className="aurora-vignette" aria-hidden="true" />
    </>
  );
}
