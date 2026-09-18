import * as React from 'react';
import { projectCoverUrl } from '../../lib/hoerspiele';
import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import { steps } from '../../lib/hoerspiele';

export function CheckIcon({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}

export function CrossIcon({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  );
}

export function PlusIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round">
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

export function TrashIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" />
    </svg>
  );
}

export function ChevronIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="m9 6 6 6-6 6" />
    </svg>
  );
}

export function ProgressBar({ value, tone }: { value: number; tone?: 'done' | 'failed' }) {
  return (
    <div className="hs-progress">
      <div
        className={`hs-progress-bar${tone === 'done' ? ' hs-progress-done' : ''}${tone === 'failed' ? ' hs-progress-failed' : ''}`}
        style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
      />
    </div>
  );
}

/** Die sieben Schritte von der Textquelle bis zum Export. */
/**
 * Schrittleiste. Mit ``onSelect`` sind bereits erreichte Schritte anklickbar —
 * die Leiste ist dann zugleich die Navigation der Detailseite.
 */
export function Stepper({
  stage, reachable, onSelect,
}: { stage: number; reachable?: number; onSelect?: (step: number) => void }) {
  const limit = reachable ?? stage;
  return (
    <ol className="hs-stepper">
      {steps.map((label, index) => {
        const position = index + 1;
        const done = position < stage;
        const current = position === stage;
        const className = `hs-step${done ? ' hs-step-done' : ''}${current ? ' hs-step-current' : ''}`;
        const body = (
          <>
            {done ? <CheckIcon size={12} /> : <span className="hs-step-num">{String(position).padStart(2, '0')}</span>}
            {label}
          </>
        );
        if (!onSelect) return <li key={label} className={className}>{body}</li>;
        return (
          <li key={label}>
            <button
              type="button"
              className={className}
              onClick={() => onSelect(position)}
              disabled={position > limit}
              aria-current={current ? 'step' : undefined}
            >
              {body}
            </button>
          </li>
        );
      })}
    </ol>
  );
}

/**
 * Modaler Dialog. Escape schließt, solange keine Anfrage läuft; der Fokus
 * springt beim Öffnen in den Dialog, damit Tastaturbedienung funktioniert.
 */
export function Modal({
  title,
  onClose,
  busy,
  children,
}: {
  title: string;
  onClose: () => void;
  busy?: boolean;
  children: ReactNode;
}) {
  const surface = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    surface.current?.querySelector<HTMLElement>('input, select, textarea, button')?.focus();
  }, []);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape' && !busy) onClose();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy, onClose]);

  return (
    <div className="hs-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
      <div className="hs-modal card" role="dialog" aria-modal="true" aria-label={title} ref={surface}>
        {children}
      </div>
    </div>
  );
}


/** Serienposter aus Plex mit Monogramm als Ersatz, wenn kein Cover vorliegt. */
export function ProjectCover({ project, className = '' }: { project: { id: string; title: string; binding?: { series_id?: string } | null }; className?: string }) {
  const [failed, setFailed] = React.useState(false);
  const wantsCover = Boolean(project.binding?.series_id) && !failed;
  return (
    <span className={`hs-cover ${className}`.trim()} aria-hidden>
      {wantsCover && (
        <img className="hs-cover-img" src={projectCoverUrl(project.id)} alt="" loading="lazy" decoding="async" onError={() => setFailed(true)} />
      )}
      {!wantsCover && <span className="hs-monogram">{monogramOf(project.title)}</span>}
    </span>
  );
}

function monogramOf(title: string) {
  const words = title.trim().split(/\s+/).filter(Boolean);
  return (words.length >= 2 ? words[0][0] + words[1][0] : title.slice(0, 2)).toUpperCase();
}
