import { useSyncExternalStore } from 'react';
import { announcePlayback, clearPlayback } from '../lib/offline';

export type PlayerTrack = { key: string; title: string; subtitle: string; href: string; url?: string; onStop?: () => void; next?: () => void; prev?: () => void };
type Snapshot = { track: PlayerTrack | null; playing: boolean; time: number; duration: number; speed: number; volume: number; error: string };
let state: Snapshot = { track: null, playing: false, time: 0, duration: 0, speed: 1, volume: 1, error: '' };
let audio: HTMLAudioElement | null = null;
let detach = () => {};
const listeners = new Set<() => void>();
const positions = new Map<string, number>();
const emit = (patch: Partial<Snapshot>) => { state = { ...state, ...patch }; listeners.forEach(l => l()); };
export const usePlayer = () => useSyncExternalStore(cb => { listeners.add(cb); return () => { listeners.delete(cb); }; }, () => state);
function announce() {
  const track = state.track;
  if (track) announcePlayback(state.playing, { title:track.title, subtitle:track.subtitle, onPlay:()=>void resume(), onPause:pause, onSeek:delta=>seek(state.time+delta), onNext:track.next, onPrev:track.prev });
}
export async function resume() {
  if (!audio) return;
  try { emit({error:''}); await audio.play(); }
  catch { emit({playing:false,error:'Wiedergabe fehlgeschlagen. Prüfe die Verbindung und versuche es erneut.'}); }
}
export function pause() { audio?.pause(); }
export function seek(seconds: number) { if (audio && Number.isFinite(seconds)) audio.currentTime = Math.max(0, Math.min(seconds, Number.isFinite(audio.duration) ? audio.duration : seconds)); }
export function setSpeed(speed: number) { if (audio) audio.playbackRate = speed; emit({speed}); }
export function setVolume(volume: number) { if (audio) audio.volume = volume; emit({volume}); }
export function releaseAudio(element: HTMLAudioElement | null) {
  if (!element || element !== audio) return;
  detach(); audio.pause(); audio = null; emit({track:null,playing:false}); clearPlayback();
}
export function adoptAudio(element: HTMLAudioElement, track: PlayerTrack) {
  const oldTrack = state.track;
  const previous = audio;
  if (previous) { if (oldTrack) positions.set(oldTrack.key, previous.currentTime); detach(); previous.pause(); }
  audio = null;
  if (oldTrack && oldTrack.key !== track.key) oldTrack.onStop?.();
  audio = element;
  emit({ track, time:element.currentTime, duration:0, playing:false, error:'' });
  element.playbackRate = state.speed; element.volume = state.volume;
  const update = () => {
    if (element !== audio) return;
    emit({playing:!element.paused && !element.ended,time:element.currentTime,duration:Number.isFinite(element.duration) ? element.duration : 0,speed:element.playbackRate,volume:element.volume});
  };
  const transport = () => { update(); announce(); };
  const error = () => emit({playing:false,error:'Audio konnte nicht geladen werden. Erneut abspielen oder Verbindung prüfen.'});
  const events = ['timeupdate','durationchange','loadedmetadata','ratechange','volumechange'];
  events.forEach(name=>element.addEventListener(name,update));
  ['play','pause','ended'].forEach(name=>element.addEventListener(name,transport));
  element.addEventListener('error',error);
  detach = () => { events.forEach(name=>element.removeEventListener(name,update)); ['play','pause','ended'].forEach(name=>element.removeEventListener(name,transport)); element.removeEventListener('error',error); };
  announce();
}
export function playFile(track: PlayerTrack, start?: number) {
  if (!track.url) return;
  if (state.track?.key === track.key && audio) { if (start !== undefined) seek(start); void resume(); return; }
  const element = new Audio(track.url);
  const position = start ?? positions.get(track.key) ?? 0;
  element.addEventListener('loadedmetadata',()=>{ if (position > 0 && position < element.duration) element.currentTime = position; },{once:true});
  adoptAudio(element,track); void resume();
}
