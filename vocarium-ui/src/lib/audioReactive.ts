/**
 * Audio-reaktive Basis für den Reader (Port aus Canto).
 *
 * Web Audio erlaubt pro HTMLMediaElement genau eine
 * MediaElementAudioSourceNode. Der Reader erzeugt pro Segment ein neues
 * <Audio>, also bekommt jedes Element eine frische Source; AudioContext,
 * Analyser und Destination leben einmal pro Tab.
 *
 * Subscriber erhalten geglättete Amplitude (0..1) und 8 Frequenzbänder
 * (0..1) über eine gemeinsame rAF-Schleife, die nur läuft, solange
 * jemand zuhört.
 */

let ctx: AudioContext | null = null;
let analyser: AnalyserNode | null = null;
let timeBuf: Uint8Array | null = null;
let freqBuf: Uint8Array | null = null;
let lastSource: MediaElementAudioSourceNode | null = null;
let lastSourceEl: HTMLMediaElement | null = null;

const subscribers = new Set<(snap: ReactiveSnapshot) => void>();
let rafHandle: number | null = null;

let smoothAmp = 0;
const smoothBands: number[] = [0, 0, 0, 0, 0, 0, 0, 0];

export type ReactiveSnapshot = {
  amplitude: number;
  bands: readonly number[];
};

function ensureContext(): AudioContext | null {
  if (ctx) return ctx;
  const Ctor = window.AudioContext
    || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) return null;
  try {
    ctx = new Ctor();
    analyser = ctx.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.7;
    analyser.connect(ctx.destination);
    timeBuf = new Uint8Array(analyser.fftSize);
    freqBuf = new Uint8Array(analyser.frequencyBinCount);
    return ctx;
  } catch {
    ctx = null;
    return null;
  }
}

/** Element an den Analyser hängen — pro Element genau einmal, doppelter
 *  Aufruf ist ein No-op (Web Audio würde sonst werfen). */
export function attachAnalyser(audio: HTMLAudioElement): void {
  const c = ensureContext();
  if (!c || !analyser) return;
  if (lastSourceEl === audio) return;
  if (c.state === 'suspended') c.resume().catch(() => undefined);
  try {
    if (lastSource) {
      try { lastSource.disconnect(); } catch { /* egal */ }
      lastSource = null;
    }
    const src = c.createMediaElementSource(audio);
    src.connect(analyser);
    lastSource = src;
    lastSourceEl = audio;
  } catch {
    // Element war schon verdrahtet — Subscriber sehen dann eben Nullen.
  }
}

function tick() {
  rafHandle = null;
  if (subscribers.size === 0) return;

  if (analyser && timeBuf && freqBuf) {
    analyser.getByteTimeDomainData(timeBuf as Uint8Array<ArrayBuffer>);
    analyser.getByteFrequencyData(freqBuf as Uint8Array<ArrayBuffer>);

    let sum = 0;
    for (let i = 0; i < timeBuf.length; i++) {
      const v = (timeBuf[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / timeBuf.length);
    smoothAmp = smoothAmp * 0.75 + Math.min(1, rms * 2.5) * 0.25;

    const bins = freqBuf.length;
    for (let b = 0; b < 8; b++) {
      const start = Math.floor((bins * b) / 8);
      const end = Math.floor((bins * (b + 1)) / 8);
      let s = 0;
      for (let i = start; i < end; i++) s += freqBuf[i];
      const v = s / Math.max(1, end - start) / 255;
      smoothBands[b] = smoothBands[b] * 0.7 + v * 0.3;
    }
  }

  const snap: ReactiveSnapshot = { amplitude: smoothAmp, bands: smoothBands };
  for (const cb of subscribers) {
    try { cb(snap); } catch { /* Subscriber-Fehler ignorieren */ }
  }
  rafHandle = requestAnimationFrame(tick);
}

export function subscribeReactive(cb: (snap: ReactiveSnapshot) => void): () => void {
  subscribers.add(cb);
  if (rafHandle === null) rafHandle = requestAnimationFrame(tick);
  return () => {
    subscribers.delete(cb);
    if (subscribers.size === 0 && rafHandle !== null) {
      cancelAnimationFrame(rafHandle);
      rafHandle = null;
    }
  };
}
