import { lazy, Suspense, useEffect, useState } from 'react';

// three.js wiegt gut 600 KB; es darf das erste Rendern nicht blockieren und
// kommt deshalb als eigenes Chunk nach.
const ThreeAurora = lazy(() => import('./ThreeAurora'));

/**
 * Aurora — fixed ambient background stack for the whole app.
 * Layers (z-index):
 *   0  three-aurora (WebGL-FBM-Nebel, audio-reaktiv) über dem CSS-Fallback
 *      der aurora-stage — fällt WebGL aus, bleibt der alte CSS-Verlauf
 *   1  aurora-grain (film grain via SVG data URI)
 *   2  aurora-vignette (edge darkening)
 * App content sits above these via the layout's z-index.
 */
export default function Aurora({ animated = true }: { animated?: boolean }) {
  const [motionAllowed, setMotionAllowed] = useState(false);
  useEffect(() => {
    const media=window.matchMedia('(prefers-reduced-motion: reduce)');
    const update=()=>setMotionAllowed(!media.matches && document.visibilityState === 'visible');
    update(); media.addEventListener('change',update); document.addEventListener('visibilitychange',update);
    return ()=>{media.removeEventListener('change',update);document.removeEventListener('visibilitychange',update);};
  }, []);
  return (
    <>
      <div className="aurora-stage" aria-hidden="true">
        <div className="aurora-grid" />
      </div>
      {animated && motionAllowed && <Suspense fallback={null}>
        <ThreeAurora />
      </Suspense>}
      <div className="aurora-grain" aria-hidden="true" />
      <div className="aurora-vignette" aria-hidden="true" />
    </>
  );
}
