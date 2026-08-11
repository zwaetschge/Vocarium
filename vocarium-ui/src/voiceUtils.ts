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
  return voice.source || 'Voice';
}
