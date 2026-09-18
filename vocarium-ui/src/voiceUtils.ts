import type { Voice } from './types';

export const DEFAULT_VOICE: Voice = {
  id: 'default',
  name: 'Default base voice',
  language: 'Auto',
  source: 'clone',
  created_at: '',
  has_audio: false,
};

export function withDefaultVoice(voices: Voice[]): Voice[] {
  const withoutDefault = voices.filter((voice) => voice.id !== DEFAULT_VOICE.id);
  return [DEFAULT_VOICE, ...withoutDefault];
}

export function isKikiriVoice(voice: Pick<Voice, 'source'>): boolean {
  return voice.source === 'kikiri';
}

export function benchmarkVoices(voices: Voice[]): Voice[] {
  return withDefaultVoice(
    voices.filter((voice) => voice.source === 'clone' || voice.source === 'design'),
  );
}

export function voiceSourceLabel(voice: Pick<Voice, 'id' | 'source'>): string {
  if (voice.id === DEFAULT_VOICE.id) return 'Base';
  if (voice.source === 'clone') return 'Cloned';
  if (voice.source === 'design') return 'Designed';
  if (voice.source === 'custom') return 'Custom';
  if (voice.source === 'kikiri') return 'Kikiri (Fallback)';
  if (voice.source === 'vibevoice') return 'Klon (VibeVoice)';
  if (voice.source === 'omnivoice') return 'Klon (OmniVoice)';
  return voice.source || 'Voice';
}

/**
 * Voices that can actually render speech today: OmniVoice clones and the
 * Kikiri CPU voices. Qwen is retired, so its rows are filtered out.
 */
export function engineVoices(voices: Voice[]): Voice[] {
  return voices.filter((v) => v.source === 'omnivoice' || v.source === 'kikiri');
}

export function isFallbackVoice(voice: Pick<Voice, 'source' | 'group'>): boolean {
  return voice.source === 'kikiri' && voice.group === 'fallback';
}

export interface VoiceGroup {
  label: string;
  hint?: string;
  voices: Voice[];
}

/**
 * Split the engine voices into the groups the pickers render as `<optgroup>`.
 *
 * Two tiers, because there are two engines: OmniVoice on the GPU is what
 * everything should normally use, and Kikiri on the CPU is the fallback for
 * when OmniVoice cannot run. Kikiri's own halves — the two Kokoro fine-tunes
 * and the Piper preset bank — are one tier, not two: which vocoder produced a
 * fallback voice is a detail of the fallback, not a choice on the same level
 * as "GPU or not". The fine-tunes simply sort first inside it.
 */
export function groupEngineVoices(voices: Voice[]): VoiceGroup[] {
  const list = engineVoices(voices);
  const kikiri = list.filter((v) => v.source === 'kikiri');
  const groups: VoiceGroup[] = [
    {
      label: 'OmniVoice — Klonstimmen',
      hint: 'Hauptengine (GPU)',
      voices: list.filter((v) => v.source === 'omnivoice'),
    },
    {
      label: 'Kikiri — Fallback',
      hint: 'CPU, nur wenn OmniVoice ausfällt',
      voices: [
        ...kikiri.filter((v) => !isFallbackVoice(v)),
        ...kikiri.filter(isFallbackVoice),
      ],
    },
  ];
  return groups.filter((g) => g.voices.length > 0);
}

const GENDER_LABEL: Record<string, string> = { male: 'männlich', female: 'weiblich', m: 'männlich', f: 'weiblich' };

/** Kurze Herkunftszeile: Engine, Geschlecht, und ob es eine Fallback-Stimme ist. */
export function describeVoice(voice: Voice): string {
  const bits: string[] = [];
  if (voice.id === DEFAULT_VOICE.id) bits.push('Standard');
  else if (voice.source === 'omnivoice') bits.push('OmniVoice · GPU');
  else if (voice.source === 'kikiri') bits.push(isFallbackVoice(voice) ? 'Kikiri · Fallback' : 'Kikiri · Finetune');
  else bits.push(voiceSourceLabel(voice));
  const gender = voice.gender ? GENDER_LABEL[voice.gender.toLowerCase()] || voice.gender : '';
  if (gender) bits.push(gender);
  if (voice.backend) bits.push(voice.backend);
  return bits.join(' · ');
}
