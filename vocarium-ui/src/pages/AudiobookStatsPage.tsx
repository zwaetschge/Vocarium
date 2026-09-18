import { useEffect, useState } from 'react';
import { getAudiobookStats } from '../api';
import type { AbStats } from '../api';

function fmtHours(ms: number): string {
  const h = ms / 3_600_000;
  if (h >= 10) return `${Math.round(h)} h`;
  if (h >= 1) return `${h.toFixed(1)} h`;
  return `${Math.round(ms / 60_000)} min`;
}

export default function AudiobookStatsPage() {
  const [data, setData] = useState<AbStats | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    getAudiobookStats().then(setData).catch((e) => setError(e instanceof Error ? e.message : 'Fehler'));
  }, []);

  if (error) return <p style={{ color: '#f87171' }}>{error}</p>;
  if (!data) return <p style={{ opacity: 0.7 }}>Lade…</p>;

  const s = data.stats;
  const unlocked = data.achievements.filter((a) => a.unlocked).length;
  const cards = [
    { label: 'Hörzeit gesamt', value: fmtHours(s.totalListeningMs) },
    { label: 'Serie', value: s.streak ? `${s.streak} ${s.streak === 1 ? 'Tag' : 'Tage'} 🔥` : '—' },
    { label: 'Bücher', value: String(s.totalBooks) },
    { label: 'Angefangen', value: String(s.booksStarted) },
    { label: 'Beendet', value: String(s.booksCompleted) },
    { label: 'Segmente gehört', value: String(s.segmentsPlayed) },
    { label: 'Segmente vertont', value: String(s.totalAudioSegments) },
    { label: 'Lesezeichen', value: String(s.totalBookmarks) },
  ];
  const maxDay = Math.max(1, ...s.dailyBreakdown.map((d) => d.ms));
  const days = [...s.dailyBreakdown].reverse();

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '26px', maxWidth: '900px' }}>
      <div>
        <div className="section-kicker" style={{ marginBottom: '8px' }}>Vocarium · Statistiken</div>
        <h1 className="display-serif" style={{ fontSize: '38px', margin: 0 }}>Dein Hörjahr</h1>
        <p style={{ margin: '4px 0 0', fontSize: '13px', opacity: 0.65 }}>
          Hörgewohnheiten und Errungenschaften · {unlocked}/{data.achievements.length} freigeschaltet
        </p>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(170px, 1fr))', gap: '14px' }}>
        {cards.map((c) => (
          <div key={c.label} className="card" style={{ padding: '16px 18px' }}>
            <div className="label-eyebrow" style={{ marginBottom: '6px' }}>{c.label}</div>
            <div style={{ fontSize: '24px', fontWeight: 700, fontFamily: 'var(--font-mono)' }}>{c.value}</div>
          </div>
        ))}
      </div>

      {days.length > 0 && (
        <div className="card" style={{ padding: '18px 20px' }}>
          <div className="label-eyebrow" style={{ marginBottom: '14px' }}>Letzte Tage</div>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: '8px', height: '90px' }}>
            {days.map((d) => (
              <div key={d.date} title={`${d.date}: ${fmtHours(d.ms)}`}
                style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '5px', height: '100%', justifyContent: 'flex-end' }}>
                <div style={{
                  width: '100%', maxWidth: '34px', borderRadius: '5px 5px 0 0',
                  height: `${Math.max(4, (d.ms / maxDay) * 70)}px`,
                  background: 'var(--color-accent, #7b61ff)', opacity: 0.85,
                }} />
                <span style={{ fontSize: '9.5px', fontFamily: 'var(--font-mono)', opacity: 0.55 }}>
                  {d.date.slice(8)}.{d.date.slice(5, 7)}.
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div>
        <div className="label-eyebrow" style={{ marginBottom: '12px' }}>Errungenschaften</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(210px, 1fr))', gap: '12px' }}>
          {data.achievements.map((a) => (
            <div key={a.id} className="card-subtle" style={{
              padding: '14px 16px', display: 'flex', gap: '12px', alignItems: 'center',
              opacity: a.unlocked ? 1 : 0.45,
            }}>
              <span style={{ fontSize: '24px', filter: a.unlocked ? 'none' : 'grayscale(1)' }}>
                {a.unlocked ? a.icon : '🔒'}
              </span>
              <div>
                <strong style={{ display: 'block', fontSize: '13.5px' }}>{a.title}</strong>
                <span style={{ fontSize: '11.5px', opacity: 0.65 }}>{a.description}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
