import { useState, useRef, useCallback, useEffect } from 'react';

export function useAudio() {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [progress, setProgress] = useState(0);
  const [duration, setDuration] = useState(0);
  const [currentId, setCurrentId] = useState<string | null>(null);

  const cleanup = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.removeAttribute('src');
      audioRef.current = null;
    }
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    }
    setPlaying(false);
    setProgress(0);
    setDuration(0);
  }, []);

  const play = useCallback((blob: Blob, id?: string) => {
    cleanup();

    const url = URL.createObjectURL(blob);
    urlRef.current = url;
    const audio = new Audio(url);
    audioRef.current = audio;
    setCurrentId(id || null);

    audio.addEventListener('loadedmetadata', () => {
      setDuration(audio.duration);
    });

    audio.addEventListener('timeupdate', () => {
      if (audio.duration > 0) {
        setProgress(audio.currentTime / audio.duration);
      }
    });

    audio.addEventListener('ended', () => {
      setPlaying(false);
      setProgress(0);
    });

    audio.play();
    setPlaying(true);
  }, [cleanup]);

  const toggle = useCallback(() => {
    if (!audioRef.current) return;
    if (playing) {
      audioRef.current.pause();
      setPlaying(false);
    } else {
      audioRef.current.play();
      setPlaying(true);
    }
  }, [playing]);

  const stop = useCallback(() => {
    cleanup();
    setCurrentId(null);
  }, [cleanup]);

  const seek = useCallback((pct: number) => {
    if (!audioRef.current || !audioRef.current.duration) return;
    audioRef.current.currentTime = pct * audioRef.current.duration;
    setProgress(pct);
  }, []);

  useEffect(() => {
    return cleanup;
  }, [cleanup]);

  return { playing, progress, duration, currentId, play, toggle, stop, seek };
}
