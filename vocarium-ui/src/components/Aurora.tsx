/**
 * Aurora — fixed background stack for the whole app.
 * Layers (z-index):
 *   0  aurora-stage + drifting orbs
 *   1  aurora-grain (film grain via SVG data URI)
 *   2  aurora-vignette (edge darkening)
 * App content sits above these via the layout's z-index.
 */
export default function Aurora() {
  return (
    <>
      <div className="aurora-stage" aria-hidden="true">
        <div className="aurora-orb aurora-orb-1" />
        <div className="aurora-orb aurora-orb-2" />
        <div className="aurora-orb aurora-orb-3" />
        <div className="aurora-orb aurora-orb-4" />
      </div>
      <div className="aurora-grain" aria-hidden="true" />
      <div className="aurora-vignette" aria-hidden="true" />
    </>
  );
}
