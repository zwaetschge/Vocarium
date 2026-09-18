import { useCallback, useEffect, useRef, useState } from 'react';
import { ambienceAudioUrl } from '../api';

const FADE_S = 1.6;

/**
 * Crossfade-Loop nach dem Canto-Muster: zwei <audio>-Elemente, das nächste
 * blendet ein, bevor das aktuelle endet — hörbar nahtlos auch bei Loops,
 * deren Datei-Enden nicht sauber schneiden.
 */
export function useAmbience() {
  const [soundId, setSoundId] = useState<string>(() => localStorage.getItem('ambience-sound') || '');
  const [volume, setVolume] = useState<number>(() => Number(localStorage.getItem('ambience-volume') ?? 0.35));
  const players = useRef<[HTMLAudioElement | null, HTMLAudioElement | null]>([null, null]);
  const activeIdx = useRef(0);
  const timer = useRef(0);
  const volumeRef = useRef(volume);
  volumeRef.current = volume;

  const stop = useCallback(() => {
    window.clearInterval(timer.current);
    for (const p of players.current) { p?.pause(); }
    players.current = [null, null];
  }, []);

  useEffect(() => stop, [stop]);

  useEffect(() => {
    localStorage.setItem('ambience-volume', String(volume));
    const p = players.current[activeIdx.current];
    if (p) p.volume = volume;
  }, [volume]);

  useEffect(() => {
    localStorage.setItem('ambience-sound', soundId);
    stop();
    if (!soundId) return;

    const url = ambienceAudioUrl(soundId);
    const a = new Audio(url);
    const b = new Audio(url);
    a.preload = b.preload = 'auto';
    players.current = [a, b];
    activeIdx.current = 0;
    a.volume = volumeRef.current;
    void a.play().catch(() => undefined);

    // Wächter: kurz vor dem Ende des aktiven Players den anderen einblenden.
    timer.current = window.setInterval(() => {
      const current = players.current[activeIdx.current];
      const next = players.current[1 - activeIdx.current];
      if (!current || !next || !current.duration) return;
      const remaining = current.duration - current.currentTime;
      if (remaining <= FADE_S && next.paused) {
        next.currentTime = 0;
        next.volume = 0;
        void next.play().catch(() => undefined);
        const started = performance.now();
        const fade = () => {
          const t = Math.min(1, (performance.now() - started) / (FADE_S * 1000));
          next.volume = volumeRef.current * t;
          current.volume = volumeRef.current * (1 - t);
          if (t < 1 && players.current[0]) requestAnimationFrame(fade);
          else { current.pause(); activeIdx.current = 1 - activeIdx.current; }
        };
        requestAnimationFrame(fade);
      }
    }, 250);

    return stop;
  }, [soundId, stop]);

  return { soundId, setSoundId, volume, setVolume };
}
