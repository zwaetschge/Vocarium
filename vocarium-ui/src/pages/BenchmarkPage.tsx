import { useState, useEffect, useMemo } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { getModels, getVoices, runBenchmark, getBenchmarkResults } from '../api';
import type { Model, Voice, BenchmarkResult } from '../types';
import WaveformBars from '../components/WaveformBars';
import { benchmarkVoices } from '../voiceUtils';

function rtfToken(rtf: number) {
  if (rtf < 1) return { color: 'var(--color-success)', raw: '#4ADE80', label: 'faster than realtime' };
  if (rtf < 2) return { color: 'var(--color-warning)', raw: '#F4C152', label: 'near realtime' };
  return { color: 'var(--color-danger)', raw: '#FF5A65', label: 'slower than realtime' };
}

export default function BenchmarkPage() {
  const [models, setModels] = useState<Model[]>([]);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [selectedVoices, setSelectedVoices] = useState<string[]>([]);
  const [testText, setTestText] = useState(
    'The quick brown fox jumps over the lazy dog. This is a benchmark test for voice synthesis quality and speed.'
  );
  const [runsPerCombo, setRunsPerCombo] = useState(1);
  const [running, setRunning] = useState(false);
  const [statusText, setStatusText] = useState('');
  const [error, setError] = useState('');
  const [results, setResults] = useState<BenchmarkResult[]>([]);
  const [historicalResults, setHistoricalResults] = useState<BenchmarkResult[]>([]);

  useEffect(() => {
    getModels()
      .then((m) => {
        const baseModels = m.filter((x) => x.type === 'base');
        setModels(baseModels);
        setSelectedModels(baseModels.map((x) => x.id));
      })
      .catch(() => {});
    getVoices()
      .then((v) => {
        const available = benchmarkVoices(v);
        setVoices(available);
        if (available.length > 0) setSelectedVoices([available[0].id]);
      })
      .catch(() => {});
    getBenchmarkResults()
      .then((r) => setHistoricalResults(r.results || []))
      .catch(() => {});
  }, []);

  const toggleModel = (id: string) => {
    setSelectedModels((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  const toggleVoice = (id: string) => {
    setSelectedVoices((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  const handleRun = async () => {
    if (!selectedModels.length || !selectedVoices.length || !testText.trim()) return;
    setRunning(true);
    setError('');
    setStatusText('Queueing benchmark…');
    setResults([]);
    try {
      const r = await runBenchmark({
        text: testText,
        voice_ids: selectedVoices,
        model_ids: selectedModels,
        runs_per_combo: runsPerCombo,
      });
      setResults(r.results || []);
      const hist = await getBenchmarkResults();
      setHistoricalResults(hist.results || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Benchmark failed');
    } finally {
      setRunning(false);
      setStatusText('');
    }
  };

  const maxRtf = useMemo(() => {
    return Math.max(...(results.length ? results.map((r) => r.rtf) : [1]), 1);
  }, [results]);

  const summary = useMemo(() => {
    if (!results.length) return null;
    const rtfs = results.map((r) => r.rtf);
    const gens = results.map((r) => r.generation_time);
    const avg = rtfs.reduce((a, b) => a + b, 0) / rtfs.length;
    const fastest = Math.min(...rtfs);
    const slowest = Math.max(...rtfs);
    const totalGen = gens.reduce((a, b) => a + b, 0);
    return { avg, fastest, slowest, totalGen, count: results.length };
  }, [results]);

  const totalCombos = selectedModels.length * selectedVoices.length * runsPerCombo;
  const canRun = !running && selectedModels.length > 0 && selectedVoices.length > 0 && testText.trim().length > 0;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '28px', maxWidth: '1200px' }}>
      {/* Header */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        <h1 style={{ fontSize: '32px', fontWeight: 600, fontFamily: 'var(--font-display)', letterSpacing: 0, lineHeight: 1.05, color: 'var(--color-text)' }}>
          Benchmark
        </h1>
        <p style={{ fontSize: '14px', color: 'var(--color-text-secondary)', lineHeight: 1.55, maxWidth: '640px' }}>
          Measure synthesis speed across base models and cloned voices. Runs serialize through the GPU queue and persist for trend tracking.
        </p>
      </div>

      {/* Intro strip */}
      <motion.div
        className="card-subtle"
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.25, 0.46, 0.45, 0.94] }}
        style={{ padding: '14px 18px', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '12px' }}
      >
        <div
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: '8px',
            padding: '5px 11px',
            borderRadius: '999px',
            background: 'rgba(123,97,255,0.08)',
            border: '1px solid rgba(123,97,255,0.22)',
            fontSize: '11px',
            color: 'var(--color-text-secondary)',
            fontFamily: 'var(--font-mono)',
            letterSpacing: 0,
          }}
        >
          <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
            <path d="M2 10V5M5 10V2M8 10V7M11 10V3" />
          </svg>
          <span>RTF · generation seconds per audio second</span>
        </div>
        <span style={{ fontSize: '11px', color: 'var(--color-text-dim)', letterSpacing: 0, textTransform: 'uppercase', fontWeight: 500 }}>
          Lower is faster · &lt;1 beats realtime
        </span>
        <div style={{ marginLeft: 'auto', fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
          {totalCombos} run{totalCombos === 1 ? '' : 's'} queued
        </div>
      </motion.div>

      {/* Config card */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.45, ease: [0.25, 0.46, 0.45, 0.94], delay: 0.05 }}
        className="card"
        style={{ padding: '24px', display: 'flex', flexDirection: 'column', gap: '22px' }}
      >
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span className="label-eyebrow" style={{ fontSize: '10px' }}>Configuration</span>
          <span style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
            {selectedModels.length}m × {selectedVoices.length}v × {runsPerCombo}r
          </span>
        </div>

        {/* Models */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
            <label className="label-eyebrow" style={{ fontSize: '10px' }}>Models</label>
            <span style={{ fontSize: '10px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
              {selectedModels.length} / {models.length}
            </span>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
            {models.length === 0 ? (
              <span style={{ fontSize: '12px', color: 'var(--color-text-dim)', fontStyle: 'italic' }}>
                No models available.
              </span>
            ) : (
              models.map((m) => {
                const selected = selectedModels.includes(m.id);
                return (
                  <motion.button
                    key={m.id}
                    whileTap={{ scale: 0.96 }}
                    whileHover={{ y: -1 }}
                    transition={{ type: 'spring', stiffness: 400, damping: 28 }}
                    onClick={() => toggleModel(m.id)}
                    style={{
                      padding: '7px 13px',
                      fontSize: '12px',
                      fontWeight: 500,
                      borderRadius: '9999px',
                      fontFamily: 'var(--font-mono)',
                      cursor: 'pointer',
                      background: selected ? 'rgba(123,97,255,0.14)' : 'rgba(255,255,255,0.03)',
                      border: selected ? '1px solid rgba(123,97,255,0.4)' : '1px solid rgba(255,255,255,0.08)',
                      color: selected ? 'var(--color-accent)' : 'var(--color-text-secondary)',
                      boxShadow: selected ? '0 2px 10px rgba(123,97,255,0.15)' : 'none',
                      transition: 'background 0.2s, border-color 0.2s, color 0.2s',
                      letterSpacing: 0,
                    }}
                  >
                    {m.id}
                  </motion.button>
                );
              })
            )}
          </div>
        </div>

        {/* Voices */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
            <label className="label-eyebrow" style={{ fontSize: '10px' }}>Voices</label>
            <span style={{ fontSize: '10px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
              {selectedVoices.length} / {voices.length}
            </span>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
            {voices.length === 0 ? (
              <span style={{ fontSize: '12px', color: 'var(--color-text-dim)', fontStyle: 'italic' }}>
                No voices available. Clone or design one first.
              </span>
            ) : (
              voices.map((v) => {
                const selected = selectedVoices.includes(v.id);
                return (
                  <motion.button
                    key={v.id}
                    whileTap={{ scale: 0.96 }}
                    whileHover={{ y: -1 }}
                    transition={{ type: 'spring', stiffness: 400, damping: 28 }}
                    onClick={() => toggleVoice(v.id)}
                    style={{
                      padding: '7px 13px',
                      fontSize: '12px',
                      fontWeight: 500,
                      borderRadius: '9999px',
                      cursor: 'pointer',
                      background: selected ? 'rgba(50,181,255,0.12)' : 'rgba(255,255,255,0.03)',
                      border: selected ? '1px solid rgba(50,181,255,0.38)' : '1px solid rgba(255,255,255,0.08)',
                      color: selected ? '#8DD4FF' : 'var(--color-text-secondary)',
                      boxShadow: selected ? '0 2px 10px rgba(50,181,255,0.12)' : 'none',
                      transition: 'background 0.2s, border-color 0.2s, color 0.2s',
                      letterSpacing: 0,
                    }}
                  >
                    {v.name}
                  </motion.button>
                );
              })
            )}
          </div>
        </div>

        {/* Text + Runs */}
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 3fr) minmax(0, 1fr)', gap: '16px' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <label className="label-eyebrow" style={{ fontSize: '10px' }}>Test text</label>
              <span style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
                {testText.length} chars
              </span>
            </div>
            <textarea
              value={testText}
              onChange={(e) => setTestText(e.target.value)}
              rows={3}
              className="input-field"
              style={{ resize: 'vertical', minHeight: '84px', lineHeight: 1.55 }}
              placeholder="Sentence or paragraph to synthesize during benchmark"
            />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <label className="label-eyebrow" style={{ fontSize: '10px' }}>Runs per combo</label>
            <input
              type="number"
              min={1}
              max={10}
              value={runsPerCombo}
              onChange={(e) => setRunsPerCombo(Math.max(1, Math.min(10, parseInt(e.target.value) || 1)))}
              className="input-field"
              style={{ fontFamily: 'var(--font-mono)', letterSpacing: 0 }}
            />
            <span style={{ fontSize: '11px', color: 'var(--color-text-dim)', lineHeight: 1.5 }}>
              Median of 3+ runs smooths startup jitter.
            </span>
          </div>
        </div>

        {/* Run button */}
        <motion.button
          whileHover={canRun ? { y: -1 } : {}}
          whileTap={canRun ? { scale: 0.99 } : {}}
          transition={{ type: 'spring', stiffness: 300, damping: 22 }}
          onClick={handleRun}
          disabled={!canRun}
          className="btn btn-primary"
          style={{
            width: '100%',
            padding: '15px 28px',
            fontSize: '14.5px',
            fontWeight: 600,
            letterSpacing: 0,
            cursor: canRun ? 'pointer' : 'not-allowed',
            opacity: canRun ? 1 : 0.5,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '12px',
          }}
        >
          {running ? (
            <>
              <WaveformBars active size="sm" bars={5} color="text" />
              <span>{statusText || 'Running benchmark'}</span>
            </>
          ) : (
            <>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M5 3l8 5-8 5V3z" fill="currentColor" />
              </svg>
              <span>Run benchmark</span>
              <span style={{ fontSize: '11px', opacity: 0.75, fontFamily: 'var(--font-mono)', letterSpacing: 0 }}>
                {totalCombos} run{totalCombos === 1 ? '' : 's'}
              </span>
            </>
          )}
        </motion.button>
      </motion.div>

      {/* Error */}
      <AnimatePresence>
        {error && (
          <motion.div
            initial={{ opacity: 0, y: -6, height: 0 }}
            animate={{ opacity: 1, y: 0, height: 'auto' }}
            exit={{ opacity: 0, y: -6, height: 0 }}
            transition={{ duration: 0.25 }}
            style={{
              padding: '13px 16px',
              background: 'rgba(255,90,101,0.08)',
              border: '1px solid rgba(255,90,101,0.28)',
              borderRadius: '12px',
              color: 'var(--color-danger)',
              fontSize: '13px',
              display: 'flex',
              alignItems: 'center',
              gap: '10px',
            }}
          >
            <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
              <circle cx="10" cy="10" r="7.5" />
              <path d="M10 6v4M10 13.5v0.5" />
            </svg>
            <span style={{ flex: 1 }}>{error}</span>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Results */}
      <AnimatePresence>
        {results.length > 0 && summary && (
          <motion.div
            initial={{ opacity: 0, y: 18 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 10 }}
            transition={{ duration: 0.45, ease: [0.25, 0.46, 0.45, 0.94] }}
            style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}
          >
            {/* Summary */}
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
                gap: '12px',
              }}
            >
              <SummaryStat
                label="Average RTF"
                value={`${summary.avg.toFixed(2)}x`}
                accent={rtfToken(summary.avg).color}
                hint={rtfToken(summary.avg).label}
              />
              <SummaryStat
                label="Fastest"
                value={`${summary.fastest.toFixed(2)}x`}
                accent="var(--color-success)"
                hint="best run"
              />
              <SummaryStat
                label="Slowest"
                value={`${summary.slowest.toFixed(2)}x`}
                accent={rtfToken(summary.slowest).color}
                hint="worst run"
              />
              <SummaryStat
                label="Total GPU time"
                value={`${summary.totalGen.toFixed(1)}s`}
                accent="var(--color-accent)"
                hint={`across ${summary.count} run${summary.count === 1 ? '' : 's'}`}
              />
            </div>

            {/* RTF Chart */}
            <div className="card" style={{ padding: '24px', display: 'flex', flexDirection: 'column', gap: '18px' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <span className="label-eyebrow" style={{ fontSize: '10px' }}>RTF comparison</span>
                <div style={{ display: 'flex', gap: '14px', fontSize: '10px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)', letterSpacing: 0 }}>
                  <LegendDot color="var(--color-success)" label="< 1x" />
                  <LegendDot color="var(--color-warning)" label="1-2x" />
                  <LegendDot color="var(--color-danger)" label="> 2x" />
                </div>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                {results.map((r, i) => {
                  const voiceName = voices.find((v) => v.id === r.voice_id)?.name || r.voice_id;
                  const token = rtfToken(r.rtf);
                  return (
                    <motion.div
                      key={`${r.model_id}-${r.voice_id}-${r.run}`}
                      initial={{ opacity: 0, x: -16 }}
                      animate={{ opacity: 1, x: 0 }}
                      transition={{ delay: i * 0.05, duration: 0.35, ease: [0.25, 0.46, 0.45, 0.94] }}
                      style={{ display: 'flex', alignItems: 'center', gap: '12px' }}
                    >
                      <div
                        style={{
                          width: '120px',
                          fontSize: '11px',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                          fontFamily: 'var(--font-mono)',
                          color: 'var(--color-text-secondary)',
                          letterSpacing: 0,
                        }}
                      >
                        {r.model_id}
                      </div>
                      <div
                        style={{
                          width: '96px',
                          fontSize: '11.5px',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                          color: 'var(--color-text)',
                          letterSpacing: 0,
                        }}
                      >
                        {voiceName}
                      </div>
                      <div
                        style={{
                          flex: 1,
                          height: '24px',
                          borderRadius: '7px',
                          overflow: 'hidden',
                          background: 'rgba(255,255,255,0.035)',
                          border: '1px solid rgba(255,255,255,0.05)',
                          position: 'relative',
                        }}
                      >
                        <motion.div
                          initial={{ width: 0 }}
                          animate={{ width: `${Math.min((r.rtf / maxRtf) * 100, 100)}%` }}
                          transition={{ duration: 0.7, delay: 0.15 + i * 0.05, ease: [0.25, 0.46, 0.45, 0.94] }}
                          style={{
                            height: '100%',
                            borderRadius: '6px',
                            background: `linear-gradient(90deg, ${token.raw}55, ${token.raw}22)`,
                            boxShadow: `inset 0 0 0 1px ${token.raw}40`,
                          }}
                        />
                        {/* 1x reference line */}
                        {maxRtf > 1 && (
                          <div
                            style={{
                              position: 'absolute',
                              top: 0,
                              bottom: 0,
                              left: `${(1 / maxRtf) * 100}%`,
                              width: '1px',
                              background: 'rgba(255,255,255,0.22)',
                            }}
                          />
                        )}
                      </div>
                      <div
                        style={{
                          width: '64px',
                          textAlign: 'right',
                          fontSize: '12px',
                          fontWeight: 600,
                          fontVariantNumeric: 'tabular-nums',
                          fontFamily: 'var(--font-mono)',
                          color: token.color,
                          letterSpacing: 0,
                        }}
                      >
                        {r.rtf.toFixed(2)}x
                      </div>
                    </motion.div>
                  );
                })}
              </div>
            </div>

            {/* Results table */}
            <ResultsTable
              title="Run detail"
              rows={results.map((r) => ({
                ...r,
                displayName: voices.find((v) => v.id === r.voice_id)?.name || r.voice_id,
              }))}
              withRun
            />
          </motion.div>
        )}
      </AnimatePresence>

      {/* Historical */}
      {historicalResults.length > 0 && results.length === 0 && (
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.15, duration: 0.4 }}
          style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}
        >
          <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
            <span className="label-eyebrow" style={{ fontSize: '10px' }}>Previous runs</span>
            <span style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
              {Math.min(historicalResults.length, 20)} of {historicalResults.length}
            </span>
          </div>
          <ResultsTable
            title="History"
            rows={historicalResults.slice(0, 20).map((r) => ({
              ...r,
              displayName: r.voice_name || r.voice_id,
            }))}
            compact
          />
        </motion.div>
      )}
    </div>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}>
      <span
        style={{
          width: '7px',
          height: '7px',
          borderRadius: '50%',
          background: color,
          boxShadow: `0 0 6px ${color}80`,
        }}
      />
      {label}
    </span>
  );
}

function SummaryStat({
  label,
  value,
  accent,
  hint,
}: {
  label: string;
  value: string;
  accent: string;
  hint?: string;
}) {
  return (
    <div
      className="card-subtle"
      style={{
        padding: '14px 16px',
        display: 'flex',
        flexDirection: 'column',
        gap: '4px',
        position: 'relative',
        overflow: 'hidden',
      }}
    >
      <span
        style={{
          position: 'absolute',
          left: 0,
          top: 12,
          bottom: 12,
          width: '2px',
          background: accent,
          borderRadius: '0 2px 2px 0',
          boxShadow: `0 0 10px ${accent}88`,
        }}
      />
      <span className="label-eyebrow" style={{ fontSize: '9.5px', paddingLeft: '8px' }}>{label}</span>
      <span
        style={{
          fontSize: '22px',
          fontFamily: 'var(--font-mono)',
          fontWeight: 600,
          color: accent,
          letterSpacing: 0,
          lineHeight: 1.1,
          paddingLeft: '8px',
        }}
      >
        {value}
      </span>
      {hint && (
        <span style={{ fontSize: '10.5px', color: 'var(--color-text-dim)', paddingLeft: '8px', letterSpacing: 0 }}>
          {hint}
        </span>
      )}
    </div>
  );
}

function ResultsTable({
  rows,
  withRun,
  compact,
}: {
  title: string;
  rows: Array<BenchmarkResult & { displayName: string }>;
  withRun?: boolean;
  compact?: boolean;
}) {
  const headers = withRun
    ? ['Model', 'Voice', 'Run', 'Audio', 'Gen time', 'RTF']
    : ['Model', 'Voice', 'Audio', 'Gen time', 'RTF'];

  return (
    <div
      className="card"
      style={{
        overflow: 'hidden',
        padding: 0,
      }}
    >
      <table style={{ width: '100%', fontSize: '13px', borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
            {headers.map((h) => (
              <th
                key={h}
                style={{
                  padding: compact ? '11px 18px' : '13px 20px',
                  textAlign: 'left',
                  fontSize: '10px',
                  fontWeight: 600,
                  textTransform: 'uppercase',
                  letterSpacing: 0,
                  color: 'var(--color-text-dim)',
                  fontFamily: 'var(--font-body)',
                }}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const token = rtfToken(r.rtf);
            return (
              <motion.tr
                key={`${r.model_id}-${r.voice_id}-${r.run}-${i}`}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ delay: i * 0.025 }}
                style={{
                  borderBottom: i === rows.length - 1 ? 'none' : '1px solid rgba(255,255,255,0.04)',
                  transition: 'background 0.15s',
                }}
                onMouseEnter={(e) => {
                  (e.currentTarget as HTMLElement).style.background = 'rgba(123,97,255,0.03)';
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLElement).style.background = 'transparent';
                }}
              >
                <td
                  style={{
                    padding: compact ? '10px 18px' : '12px 20px',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '11px',
                    color: 'var(--color-text-secondary)',
                    letterSpacing: 0,
                  }}
                >
                  {r.model_id}
                </td>
                <td
                  style={{
                    padding: compact ? '10px 18px' : '12px 20px',
                    fontSize: '12.5px',
                    color: 'var(--color-text)',
                    letterSpacing: 0,
                  }}
                >
                  {r.displayName}
                </td>
                {withRun && (
                  <td
                    style={{
                      padding: compact ? '10px 18px' : '12px 20px',
                      fontFamily: 'var(--font-mono)',
                      fontSize: '11px',
                      color: 'var(--color-text-dim)',
                    }}
                  >
                    #{r.run}
                  </td>
                )}
                <td
                  style={{
                    padding: compact ? '10px 18px' : '12px 20px',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '11px',
                    color: 'var(--color-text)',
                  }}
                >
                  {r.audio_duration.toFixed(2)}s
                </td>
                <td
                  style={{
                    padding: compact ? '10px 18px' : '12px 20px',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '11px',
                    color: 'var(--color-text)',
                  }}
                >
                  {r.generation_time.toFixed(2)}s
                </td>
                <td
                  style={{
                    padding: compact ? '10px 18px' : '12px 20px',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '11.5px',
                    fontWeight: 600,
                    color: token.color,
                    letterSpacing: 0,
                  }}
                >
                  {r.rtf.toFixed(3)}x
                </td>
              </motion.tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
