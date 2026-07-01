import { useState, useEffect } from 'react';
import { getModels } from '../api';
import type { Model } from '../types';

/**
 * Lightweight status chip showing the currently loaded model.
 * No switching — that happens contextually per page.
 */
export default function ModelSelector() {
  const [models, setModels] = useState<Model[]>([]);

  useEffect(() => {
    const check = () => getModels().then(setModels).catch(() => {});
    check();
    const id = setInterval(check, 15000);
    return () => clearInterval(id);
  }, []);

  const current = models.find((m) => m.loaded);

  return (
    <div
      className="glass-subtle"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '8px',
        padding: '6px 12px',
        borderRadius: '999px',
        fontSize: '11px',
        fontFamily: 'var(--font-mono)',
        letterSpacing: 0,
        color: 'var(--color-text-secondary)',
      }}
    >
      <span
        className={current ? 'status-dot status-dot-online' : 'status-dot status-dot-loading'}
      />
      <span style={{ color: 'var(--color-text-dim)' }}>model</span>
      <span style={{ color: current ? 'var(--color-text)' : 'var(--color-text-dim)' }}>
        {current ? current.id : 'idle'}
      </span>
    </div>
  );
}
