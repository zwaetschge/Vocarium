import { useEffect, useRef } from 'react';
import { subscribeReactive } from '../lib/audioReactive';

/** Halo hinter dem Play-Button — mutiert das DOM direkt per rAF,
 *  die Reader-Seite rendert dadurch nie neu. */
export function AudioReactiveGlow({ active }: { active: boolean }) {
  const haloRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!active) {
      if (haloRef.current) {
        haloRef.current.style.opacity = '0';
        haloRef.current.style.transform = 'translate(-50%, -50%) scale(0.85)';
      }
      return;
    }
    return subscribeReactive(({ amplitude }) => {
      const halo = haloRef.current;
      if (!halo) return;
      const scale = 0.95 + amplitude * 0.55;
      const opacity = 0.25 + amplitude * 0.65;
      halo.style.transform = `translate(-50%, -50%) scale(${scale.toFixed(3)})`;
      halo.style.opacity = opacity.toFixed(3);
    });
  }, [active]);

  return (
    <div
      ref={haloRef}
      aria-hidden
      style={{
        position: 'absolute', left: '50%', top: '50%',
        width: '96px', height: '96px', borderRadius: '50%',
        pointerEvents: 'none',
        background: 'radial-gradient(circle, rgba(123,97,255,0.55), rgba(123,97,255,0) 70%)',
        transform: 'translate(-50%, -50%) scale(0.85)',
        opacity: 0,
        transition: 'opacity 80ms linear',
        filter: 'blur(2px)',
        zIndex: 0,
      }}
    />
  );
}

/** Frequenzstreifen — gespiegelte Balken, ruhig ohne Wiedergabe. */
export function AudioReactiveBars({ active, bars = 24, height = 18 }: { active: boolean; bars?: number; height?: number }) {
  const wrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!active) {
      const w = wrapRef.current;
      if (w) {
        for (const child of Array.from(w.children) as HTMLElement[]) {
          child.style.transform = 'scaleY(0.05)';
          child.style.opacity = '0.18';
        }
      }
      return;
    }
    return subscribeReactive(({ bands }) => {
      const w = wrapRef.current;
      if (!w) return;
      const children = w.children;
      const half = Math.floor(bars / 2);
      for (let i = 0; i < children.length; i++) {
        const idx = i < half ? half - 1 - i : i - half;
        const bandIndex = Math.min(bands.length - 1, Math.floor((idx / half) * bands.length));
        const v = Math.max(0.05, Math.min(1, bands[bandIndex] ?? 0));
        const el = children[i] as HTMLElement;
        el.style.transform = `scaleY(${v.toFixed(3)})`;
        el.style.opacity = (0.35 + v * 0.55).toFixed(3);
      }
    });
  }, [active, bars]);

  return (
    <div
      ref={wrapRef}
      aria-hidden
      style={{
        display: 'flex', alignItems: 'flex-end', justifyContent: 'center',
        gap: '2px', height: `${height}px`, pointerEvents: 'none',
      }}
    >
      {Array.from({ length: bars }).map((_, i) => (
        <div
          key={i}
          style={{
            width: '3px', height: '100%',
            borderRadius: '2px 2px 0 0', transformOrigin: 'bottom',
            background: 'linear-gradient(180deg, var(--color-accent, #7b61ff), rgba(123,97,255,0.55))',
            transform: 'scaleY(0.05)', opacity: 0.18,
            transition: 'transform 60ms linear, opacity 80ms linear',
            boxShadow: '0 0 4px rgba(123,97,255,0.35)',
          }}
        />
      ))}
    </div>
  );
}
