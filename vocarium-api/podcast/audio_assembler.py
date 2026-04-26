"""Audio assembler — combines TTS segments into a single podcast audio file.

Ported from PodForge's audioAssembler.ts. The TTS producer is injected via
``TTSGenerator`` so this module stays decoupled from the Vocarium voice
resolver / qwen3-tts client wiring in ``main.py``.

The pipeline:
  1. synthesize each speech/reaction segment to an individual MP3
  2. trim leading silence from each segment
  3. stitch the files with context-aware pauses using ffmpeg's concat demuxer
  4. apply EBU R128 loudness normalisation (I=-16, TP=-1.5, LRA=11)
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

from .disfluency import ScriptSegment
from .helpers import generate_id

logger = logging.getLogger(__name__)


AudioFormat = Literal["mp3", "wav"]
AssemblyStage = Literal["synthesizing", "assembling", "complete", "error"]


# --- Dataclasses -------------------------------------------------------------


@dataclass
class AssemblyOptions:
    output_format: AudioFormat = "mp3"
    pause_speech: int = 300
    pause_reaction: int = 50
    pause_speaker_change: int = 250
    reaction_overlap: int = 0


@dataclass
class AssemblyProgress:
    stage: AssemblyStage
    progress: float
    message: str
    segment_id: str | None = None
    segment_position: int | None = None


@dataclass
class AssemblyResult:
    file_path: Path
    duration: float
    file_size: int


@dataclass
class SynthesisResult:
    segment: ScriptSegment
    file_path: Path | None
    duration: float


ProgressCallback = Callable[[AssemblyProgress], None]


# --- TTS protocol ------------------------------------------------------------


class TTSGenerator(Protocol):
    """TTS dependency injected by the caller.

    ``synthesize_to_file`` must write the requested audio to ``output_path``
    and return the duration in seconds. ``default_voice_for_speaker`` is used
    when a segment has no explicit voice and no speaker→voice mapping yet."""

    async def synthesize_to_file(
        self,
        text: str,
        voice: str,
        output_path: Path,
        output_format: AudioFormat,
        *,
        user_id: int | None = None,
        notes: str | None = None,
    ) -> float:
        ...

    async def default_voice_for_speaker(self, speaker: str, *, user_id: int | None = None) -> str:
        ...


# --- Text cleaning ------------------------------------------------------------


_CLEAN_STEPS: list[tuple[re.Pattern[str], str]] = [
    # Standalone laughter → [laughter]
    (re.compile(r"^(Ha){2,}!*\.?$", re.IGNORECASE), "[laughter]"),
    (re.compile(r"^(Hi){2,}!*\.?$", re.IGNORECASE), "[laughter]"),
    (re.compile(r"^(He){2,}!*\.?$", re.IGNORECASE), "[laughter]"),
    # Inline laughter → [laughter] + rest
    (re.compile(r"\b((?:Ha){2,}|(?:Hi){2,}|(?:He){2,})!*\s*,?\s*", re.IGNORECASE), "[laughter] "),
    # German stage directions → CosyVoice tags
    (re.compile(r"\[lacht\]", re.IGNORECASE), "[laughter]"),
    (re.compile(r"\[schmunzelt\]", re.IGNORECASE), "[laughter]"),
    (re.compile(r"\[kichert\]", re.IGNORECASE), "[laughter]"),
    (re.compile(r"\[seufzt\]", re.IGNORECASE), "<sigh>"),
    (re.compile(r"\[atmet\]", re.IGNORECASE), "<breath>"),
    (re.compile(r"\[schnauft\]", re.IGNORECASE), "<breath>"),
    (re.compile(r"\[hustet\]", re.IGNORECASE), "<cough>"),
    (re.compile(r"\[räuspert sich\]", re.IGNORECASE), "<cough>"),
    (re.compile(r"\[schnappt nach luft\]", re.IGNORECASE), "<gasp>"),
    (re.compile(r"\[hmm\]", re.IGNORECASE), "<mn>"),
    (re.compile(r"\[mhm\]", re.IGNORECASE), "<mn>"),
    # English stage directions
    (re.compile(r"\[laughs\]", re.IGNORECASE), "[laughter]"),
    (re.compile(r"\[chuckles\]", re.IGNORECASE), "[laughter]"),
    (re.compile(r"\[giggles\]", re.IGNORECASE), "[laughter]"),
    (re.compile(r"\[sighs\]", re.IGNORECASE), "<sigh>"),
    (re.compile(r"\[gasps\]", re.IGNORECASE), "<gasp>"),
    (re.compile(r"\[coughs\]", re.IGNORECASE), "<cough>"),
    # Strip any remaining bracketed stage directions (keep [laughter])
    (re.compile(r"\[(?!laughter\])[^\]]+\]"), ""),
    # Leading hesitation words
    (re.compile(r"^(Ähm|Äh|Hmm|Mhm),?\s*", re.IGNORECASE), "<mn> "),
    # Mid-sentence hesitation after ellipsis
    (re.compile(r"(\.\.\.\s*)(äh|ähm),?\s*", re.IGNORECASE), r"\1<mn> "),
    (re.compile(r",?\s+(äh|ähm),?\s+", re.IGNORECASE), " <mn> "),
    (re.compile(r",?\s+(hmm|mhm),?\s+", re.IGNORECASE), " <mn> "),
    # Standalone hesitation
    (re.compile(r"^(Mhm|Hmm|Ähm|Äh)!*\.?$", re.IGNORECASE), "<mn>"),
    # Em-dashes → pauses
    (re.compile(r"--"), "..."),
    # Collapse whitespace
    (re.compile(r"\s{2,}"), " "),
]


def clean_text_for_tts(text: str) -> str:
    """Normalise stage directions and hesitations to CosyVoice tags."""
    out = text
    for pattern, replacement in _CLEAN_STEPS:
        out = pattern.sub(replacement, out)
    return out.strip()


# --- SFX map -----------------------------------------------------------------

_SFX_MAP: dict[str, str] = {
    "[lacht]": "laugh.mp3",
    "[laughs]": "laugh.mp3",
    "[schmunzelt]": "chuckle.mp3",
    "[chuckles]": "chuckle.mp3",
    "[kichert]": "giggle.mp3",
    "[giggles]": "giggle.mp3",
}


# --- Helpers -----------------------------------------------------------------


_PAUSE_NOTE_RE = re.compile(r"pause-(\d+(?:\.\d+)?)")
_ENTHUSIASTIC_START = re.compile(
    r"^(Ja|Genau|Absolut|Wow|Krass|Yes|Exactly|Right)", re.IGNORECASE
)


def _extract_pause_duration(segment: ScriptSegment) -> float:
    """Read the pause length (seconds) from a pause segment's notes."""
    if segment.notes:
        m = _PAUSE_NOTE_RE.search(segment.notes)
        if m:
            return float(m.group(1))
    return 0.5


def _rand_between(a: int, b: int) -> int:
    return round(a + random.random() * (b - a))


def _calculate_contextual_pause(
    prev: ScriptSegment, curr: ScriptSegment
) -> int:
    """Choose a natural gap (ms) between two segments based on conversation
    dynamics."""
    if curr.type == "reaction":
        short = len(curr.text) < 15
        return _rand_between(0, 30) if short else _rand_between(20, 60)

    if prev.type == "reaction":
        return _rand_between(30, 80)

    if prev.speaker == curr.speaker:
        tail = prev.text
        if tail.endswith("...") or tail.endswith("--"):
            return _rand_between(200, 400)
        return _rand_between(80, 180)

    prev_is_question = prev.text.strip().endswith("?")
    curr_is_short = len(curr.text.split()) < 8

    if prev_is_question and curr_is_short:
        return _rand_between(100, 200)
    if prev_is_question:
        return _rand_between(250, 500)

    if _ENTHUSIASTIC_START.match(curr.text):
        return _rand_between(80, 180)

    prev_notes = (prev.notes or "").lower()
    curr_notes = (curr.notes or "").lower()
    if any(k in curr_notes for k in ("übergang", "transition", "neues thema")):
        return _rand_between(500, 800)

    return _rand_between(180, 350)


# --- Assembler ---------------------------------------------------------------


class AudioAssembler:
    def __init__(
        self,
        tts: TTSGenerator,
        output_dir: str | os.PathLike[str] | None = None,
        sfx_dir: str | os.PathLike[str] | None = None,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        user_id: int | None = None,
    ):
        self.tts = tts
        self.output_dir = Path(
            output_dir or os.environ.get("PODCAST_AUDIO_PATH", "/app/data/podcast_audio")
        )
        self.sfx_dir = Path(sfx_dir) if sfx_dir else Path(
            os.environ.get("PODCAST_SFX_PATH", "/app/data/podcast_sfx")
        )
        self.ffmpeg = ffmpeg_path
        self.ffprobe = ffprobe_path
        self.user_id = user_id
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def assemble_from_segments(
        self,
        segments: list[ScriptSegment],
        project_id: str,
        options: AssemblyOptions | None = None,
        on_progress: ProgressCallback | None = None,
        force: bool = False,
        user_id: int | None = None,
    ) -> AssemblyResult:
        options = options or AssemblyOptions()

        logger.info(
            "Starting audio assembly (project=%s, segments=%d, format=%s, force=%s)",
            project_id,
            len(segments),
            options.output_format,
            force,
        )

        project_dir = self.output_dir / project_id
        if force and project_dir.exists():
            shutil.rmtree(project_dir, ignore_errors=True)
            logger.info("Cleared cached audio files for regeneration: %s", project_dir)

        self._progress(
            on_progress, "synthesizing", 0, "Starting audio synthesis..."
        )

        synthesized = await self._synthesize_segments(
            segments, project_id, options, on_progress, user_id=user_id
        )

        self._progress(
            on_progress, "assembling", 80, "Combining audio segments..."
        )

        output_file = await self._combine_segments(
            synthesized, project_id, options, on_progress
        )

        duration = await self.get_audio_duration(output_file)
        file_size = output_file.stat().st_size

        self._progress(
            on_progress, "complete", 100, "Audio assembly complete"
        )

        logger.info(
            "Audio assembly complete (file=%s, duration=%.2fs, size=%d)",
            output_file, duration, file_size,
        )

        return AssemblyResult(
            file_path=output_file, duration=duration, file_size=file_size
        )

    # --- Synthesis -----------------------------------------------------------

    async def _synthesize_segments(
        self,
        segments: list[ScriptSegment],
        project_id: str,
        options: AssemblyOptions,
        on_progress: ProgressCallback | None,
        user_id: int | None = None,
    ) -> list[SynthesisResult]:
        resolved_user_id = user_id or self.user_id
        logger.info(
            "AudioAssembler._synthesize_segments user_id=%s segments=%d",
            resolved_user_id, len(segments),
        )

        # speaker → voice map (pre-build from all segments)
        speaker_voice: dict[str, str] = {}
        for seg in segments:
            if seg.voice and seg.speaker and seg.speaker not in speaker_voice:
                speaker_voice[seg.speaker] = seg.voice

        # Resolve voices for speakers that have no explicit voice yet
        for seg in segments:
            if seg.speaker and seg.speaker not in speaker_voice:
                try:
                    speaker_voice[seg.speaker] = await self.tts.default_voice_for_speaker(
                        seg.speaker, user_id=resolved_user_id
                    )
                except Exception:
                    pass  # will fail later during synthesis

        project_dir = self.output_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)

        total = len(segments)
        results: list[SynthesisResult | None] = [None] * total
        completed = 0

        sem = asyncio.Semaphore(3)  # 3 concurrent workers max — 2 TTS servers with blocking inference

        async def _synthesize_one(idx: int, segment: ScriptSegment) -> None:
            nonlocal completed
            
            self._progress(
                on_progress,
                "synthesizing",
                (completed / total) * 80 if total else 0,
                f"Synthesizing segment {idx + 1}/{total}",
                segment_id=segment.id,
                segment_position=idx,
            )

            # Pauses — instant
            if segment.type == "pause":
                results[idx] = SynthesisResult(
                    segment=segment,
                    file_path=None,
                    duration=_extract_pause_duration(segment),
                )
                completed += 1
                return

            # SFX
            if segment.type == "sfx":
                sfx_file = self._resolve_sfx(segment.text)
                if sfx_file and sfx_file.exists():
                    results[idx] = SynthesisResult(
                        segment=segment,
                        file_path=sfx_file,
                        duration=await self.get_audio_duration(sfx_file),
                    )
                    completed += 1
                    return
                # fall through → treat as speech

            voice = (
                segment.voice
                or speaker_voice.get(segment.speaker)
            )
            if not voice:
                logger.error("No voice for speaker %s segment %s", segment.speaker, segment.id)
                results[idx] = SynthesisResult(segment=segment, file_path=None, duration=0.0)
                completed += 1
                return

            output_path = project_dir / f"{segment.id}.{options.output_format}"

            if output_path.exists():
                duration = await self.get_audio_duration(output_path)
                results[idx] = SynthesisResult(
                    segment=segment, file_path=output_path, duration=duration
                )
                completed += 1
                return

            cleaned = clean_text_for_tts(segment.text)
            if cleaned != segment.text:
                logger.info(
                    "TTS text cleaned (orig=%r, cleaned=%r)",
                    segment.text[:80],
                    cleaned[:80],
                )
            if not cleaned:
                logger.debug(
                    "Skipping segment with empty cleaned text (id=%s)", segment.id
                )
                results[idx] = SynthesisResult(segment=segment, file_path=None, duration=0.0)
                completed += 1
                return

            try:
                duration = await self._retry_synthesize(
                    text=cleaned,
                    voice=voice,
                    output_path=output_path,
                    output_format=options.output_format,
                    segment_id=segment.id,
                    user_id=resolved_user_id,
                    notes=segment.notes,
                )
                results[idx] = SynthesisResult(
                    segment=segment, file_path=output_path, duration=duration
                )
            except Exception as exc:
                logger.error("Segment %s synthesis failed: %s", segment.id, exc)
                results[idx] = SynthesisResult(segment=segment, file_path=None, duration=0.0)
                if on_progress:
                    self._progress(
                        on_progress, "synthesizing", (completed / total) * 80,
                        f"Failed segment {idx + 1}/{total}: {exc!s}",
                        segment_id=segment.id, segment_position=idx,
                    )
            completed += 1

        # Run all workers concurrently, limited by semaphore
        workers = [asyncio.create_task(_synthesize_one(i, seg)) for i, seg in enumerate(segments)]
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

        return [r for r in results if r is not None]


    async def _retry_synthesize(
        self,
        *,
        text: str,
        voice: str,
        output_path: Path,
        output_format: AudioFormat,
        segment_id: str,
        user_id: int | None = None,
        notes: str | None = None,
        max_attempts: int = 3,
        initial_delay: float = 1.0,
        backoff_factor: float = 2.0,
    ) -> float:
        last_err: Exception | None = None
        delay = initial_delay
        for attempt in range(1, max_attempts + 1):
            try:
                return await self.tts.synthesize_to_file(
                    text=text,
                    voice=voice,
                    output_path=output_path,
                    output_format=output_format,
                    user_id=user_id,
                    notes=notes,
                )
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "TTS synthesis failed (segment=%s, attempt=%d/%d): %s",
                    segment_id, attempt, max_attempts, exc,
                )
                if output_path.exists():
                    try:
                        output_path.unlink()
                    except OSError:
                        pass
                if attempt < max_attempts:
                    await asyncio.sleep(delay)
                    delay *= backoff_factor
        assert last_err is not None
        raise last_err

    def _resolve_sfx(self, text: str) -> Path | None:
        for tag, filename in _SFX_MAP.items():
            if tag in text:
                return self.sfx_dir / filename
        return None

    # --- Combination ---------------------------------------------------------

    async def _combine_segments(
        self,
        synthesized: list[SynthesisResult],
        project_id: str,
        options: AssemblyOptions,
        on_progress: ProgressCallback | None,
    ) -> Path:
        output_file = self.output_dir / f"{project_id}.{options.output_format}"

        temp_dir = self.output_dir / "temp" / generate_id()
        temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            silence_cache: dict[int, Path] = {}
            trimmed_cache: dict[Path, Path] = {}

            async def get_silence(duration_ms: int) -> Path:
                rounded = max(round(duration_ms / 50) * 50, 50)
                if rounded in silence_cache:
                    return silence_cache[rounded]
                silence_path = temp_dir / f"silence_{rounded}ms.mp3"
                await self._run_ffmpeg([
                    "-f", "lavfi",
                    "-i", "anullsrc=r=24000:cl=mono",
                    "-t", f"{rounded / 1000:.3f}",
                    "-c:a", "libmp3lame",
                    "-b:a", "192k",
                    "-y", str(silence_path),
                ])
                silence_cache[rounded] = silence_path
                return silence_path

            async def get_trimmed(src: Path) -> Path:
                if src in trimmed_cache:
                    return trimmed_cache[src]
                trimmed_path = temp_dir / f"trimmed_{src.name}"
                try:
                    await self._run_ffmpeg([
                        "-i", str(src),
                        "-af",
                        "silenceremove=start_periods=1:start_duration=0.02:start_threshold=-45dB",
                        "-c:a", "libmp3lame",
                        "-b:a", "192k",
                        "-y", str(trimmed_path),
                    ])
                    if trimmed_path.exists() and trimmed_path.stat().st_size > 500:
                        trimmed_cache[src] = trimmed_path
                        return trimmed_path
                except Exception as exc:
                    logger.debug("trim failed for %s: %s", src, exc)
                trimmed_cache[src] = src
                return src

            concat_lines: list[str] = []

            for i, item in enumerate(synthesized):
                seg = item.segment

                if i > 0 and seg.type != "pause":
                    prev = synthesized[i - 1].segment
                    pause_ms = _calculate_contextual_pause(prev, seg)
                    if pause_ms > 0:
                        silence_path = await get_silence(pause_ms)
                        concat_lines.append(
                            f"file '{_escape_concat_path(silence_path)}'"
                        )

                if seg.type == "pause":
                    pause_ms = int(_extract_pause_duration(seg) * 1000)
                    silence_path = await get_silence(pause_ms)
                    concat_lines.append(
                        f"file '{_escape_concat_path(silence_path)}'"
                    )
                    continue

                if item.file_path and item.file_path.exists():
                    trimmed = await get_trimmed(item.file_path)
                    concat_lines.append(
                        f"file '{_escape_concat_path(trimmed)}'"
                    )

            concat_list_path = temp_dir / "concat.txt"
            concat_list_path.write_text("\n".join(concat_lines), encoding="utf-8")

            self._progress(
                on_progress, "assembling", 85, "Running ffmpeg..."
            )

            await self._run_ffmpeg([
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list_path),
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-c:a", "libmp3lame",
                "-b:a", "192k",
                "-y", str(output_file),
            ])

            return output_file
        finally:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception as exc:
                logger.warning("Failed to clean temp dir %s: %s", temp_dir, exc)

    # --- Housekeeping --------------------------------------------------------

    async def clean_stale_jobs(self, max_age_hours: float = 24) -> int:
        """Remove temp directories older than ``max_age_hours``. Returns the
        number of directories cleaned up."""
        temp_root = self.output_dir / "temp"
        if not temp_root.exists():
            return 0

        import time
        cutoff = time.time() - max_age_hours * 3600
        cleaned = 0

        for entry in temp_root.iterdir():
            if not entry.is_dir():
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    cleaned += 1
                    logger.info("Swept stale temp dir: %s", entry)
            except Exception as exc:
                logger.warning("Failed to sweep %s: %s", entry, exc)

        return cleaned

    # --- ffmpeg plumbing -----------------------------------------------------

    async def _run_ffmpeg(self, args: list[str]) -> None:
        logger.debug("Running ffmpeg: %s", " ".join([self.ffmpeg, *args]))
        proc = await asyncio.create_subprocess_exec(
            self.ffmpeg, *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            tail = (stderr or b"").decode("utf-8", "replace")[-500:]
            logger.error("ffmpeg failed (code=%d): %s", proc.returncode, tail)
            raise RuntimeError(f"ffmpeg failed (code={proc.returncode}): {tail}")

    async def get_audio_duration(self, file_path: str | os.PathLike[str]) -> float:
        path = Path(file_path)
        try:
            proc = await asyncio.create_subprocess_exec(
                self.ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0 and stdout:
                text = stdout.decode("utf-8", "replace").strip()
                try:
                    return float(text)
                except ValueError:
                    pass
            logger.warning(
                "ffprobe failed for %s: %s", path, (stderr or b"").decode("utf-8", "replace"),
            )
        except Exception as exc:
            logger.warning("ffprobe error for %s: %s", path, exc)

        # Fallback: rough MP3 estimate at 192 kbps
        try:
            size = path.stat().st_size
            return (size / 192000) * 8
        except OSError:
            return 0.0

    @staticmethod
    async def check_ffmpeg(ffmpeg_path: str = "ffmpeg") -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_path, "-version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False

    # --- Progress helper -----------------------------------------------------

    @staticmethod
    def _progress(
        cb: ProgressCallback | None,
        stage: AssemblyStage,
        progress: float,
        message: str,
        *,
        segment_id: str | None = None,
        segment_position: int | None = None,
    ) -> None:
        if cb is None:
            return
        try:
            cb(AssemblyProgress(
                stage=stage,
                progress=progress,
                message=message,
                segment_id=segment_id,
                segment_position=segment_position,
            ))
        except Exception as exc:
            logger.debug("progress callback raised: %s", exc)


def _escape_concat_path(path: Path) -> str:
    """ffmpeg concat demuxer requires single-quoted paths with embedded ``'``
    escaped as ``'\\''``."""
    return str(path).replace("'", "'\\''")
