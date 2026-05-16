"""Disfluency engine — natural speech patterns (German/English).

Ported from PodForge's disfluencyEngine.ts. Preserves all dictionaries and
signal regexes verbatim so output parity with the TypeScript version holds.
"""

from __future__ import annotations

import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Literal

logger = logging.getLogger(__name__)


SegmentType = Literal["speech", "reaction", "pause", "sfx", "music"]
Language = Literal["german", "english"]
ReactionType = Literal[
    "agreement",
    "surprise",
    "thought",
    "amusement",
    "encouraging",
    "understanding",
]


@dataclass
class ScriptSegment:
    id: str
    script_id: str | None
    speaker: str
    text: str
    type: SegmentType
    voice: str | None = None
    notes: str | None = None
    position: float = 0
    word_count: int = 0
    estimated_duration: float = 0
    regenerated_from: str | None = None
    created_at: str = ""
    updated_at: str = ""
    # Mixing offset relative to the previous segment's end (ms).
    # 0 = use the contextual gap; <0 = overlap (interruption); >0 = forced gap.
    overlap_ms: int = 0
    # Audio-track fields. Used by type="music" and type="sfx" with prompt.
    prompt: str | None = None
    duration_ms: int = 0
    volume_db: float = 0.0


@dataclass
class DisfluencyStatistics:
    fill_word_count: int = 0
    reaction_count: int = 0
    pause_count: int = 0
    sentence_restart_count: int = 0
    interruption_count: int = 0


@dataclass
class DisfluencyOptions:
    level: Literal[0, 1, 2, 3]
    format: Literal["dialog", "monolog", "custom"]
    language: str


# German fill words
FILL_WORDS: dict[Language, dict[str, list[str]]] = {
    "german": {
        "sentenceStart": ["Also", "Ähm", "Ja", "Also ich mein", "Sagen wir mal", "Eigentlich"],
        "midSentence": ["äh", "sozusagen", "quasi", "gewissermaßen", "wie man so sagt", "halt"],
        "hesitation": ["ähm", "hmm", "also", "nee warte", "ähm also", "äh wie sagt man"],
        "transition": [
            "Und zwar",
            "Also der Punkt ist",
            "Was ich sagen will ist",
            "Es geht darum",
            "Genau",
        ],
    },
    "english": {
        "sentenceStart": ["So", "Um", "Like", "I mean", "Basically", "Actually"],
        "midSentence": ["like", "you know", "sort of", "kind of", "I guess"],
        "hesitation": ["um", "uh", "wait", "no wait", "hang on"],
        "transition": ["The point is", "What I mean is", "Basically", "So the thing is"],
    },
}


REACTIONS: dict[Language, dict[ReactionType, list[str]]] = {
    "german": {
        "agreement": ["Mhm", "Ja", "Genau", "Absolut", "Stimmt", "Richtig", "Ja genau", "Ja klar"],
        "surprise": ["Echt jetzt?", "Wirklich?", "Krass", "Wow", "Oh", "Echt?", "Nein oder?"],
        "thought": ["Hmm", "Interessant", "Okay", "Aha", "Ich seh schon"],
        "amusement": ["Haha ja", "Das stimmt", "Ja genau", "Oh ja"],
        "encouraging": ["Und?", "Dann?", "Erzähl weiter", "Ja und?", "Was dann?"],
        "understanding": ["Aha", "Oh okay", "Ah verstehe", "Klar", "Natürlich", "Macht Sinn"],
    },
    "english": {
        "agreement": ["Mhm", "Yeah", "Exactly", "Absolutely", "Right", "True", "Totally"],
        "surprise": ["Really?", "Wow", "No way?", "Seriously?", "Oh"],
        "thought": ["Hmm", "Interesting", "Okay", "I see"],
        "amusement": ["Haha yeah", "That's true", "Oh yeah", "Exactly"],
        "encouraging": ["And then?", "Go on", "Tell me more"],
        "understanding": ["Oh I see", "Ah okay", "Got it", "Makes sense"],
    },
}


TRANSITIONS: dict[Language, list[str]] = {
    "german": [
        "Ja und dazu muss man sagen,",
        "Oh ja, und",
        "Genau, also",
        "Und was ich dazu noch sagen wollte,",
        "Ja stimmt, und",
    ],
    "english": [
        "Yeah and on top of that,",
        "Oh right, and",
        "Exactly, so",
        "And what I wanted to add,",
        "Yeah true, and",
    ],
}


SENTENCE_RESTARTS: dict[Language, list[str]] = {
    "german": [
        "Also-- nee, anders gesagt,",
        "Ich mein-- also,",
        "Ähm-- ja genau,",
        "Nee warte-- also,",
        "Also eigentlich--",
    ],
    "english": [
        "I mean-- well actually,",
        "So-- no wait,",
        "Um-- yeah right,",
        "Like-- actually,",
        "Well basically--",
    ],
}


SIGNALS: dict[Language, dict[str, re.Pattern[str]]] = {
    "german": {
        "surprise": re.compile(
            r"\b(unglaublich|überraschend|schockierend|krass|verrückt|wahnsinn|extrem|enorm|riesig|gewaltig|dramatisch|unerwartet|überraschenderweise|doppelt so|hundert prozent|tausende|millionen|milliarden|zum ersten mal)\b",
            re.IGNORECASE,
        ),
        "uncertainty": re.compile(
            r"\b(vielleicht|eventuell|möglicherweise|könnte|dürfte|ich glaube|ich denke|vermutlich|wahrscheinlich|angeblich|anscheinend)\b",
            re.IGNORECASE,
        ),
        "amusement": re.compile(
            r"\b(witzig|lustig|komisch|ironisch|absurd|skurril|humor|spaßig)\b",
            re.IGNORECASE,
        ),
        "conclusion": re.compile(
            r"\b(also|deshalb|daher|folglich|somit|insgesamt|zusammengefasst|im endeffekt|letztendlich|das heißt|das bedeutet|am ende|schlussendlich)\b",
            re.IGNORECASE,
        ),
        "factBuildup": re.compile(
            r"\b(erstens|zweitens|zum einen|zum anderen|außerdem|darüber hinaus|zusätzlich|noch dazu|und dann|und zwar)\b",
            re.IGNORECASE,
        ),
        "narrative": re.compile(
            r"\b(dann|daraufhin|plötzlich|als|während|danach|anschließend|später)\b",
            re.IGNORECASE,
        ),
    },
    "english": {
        "surprise": re.compile(
            r"\b(incredible|surprising|shocking|crazy|insane|extreme|enormous|massive|dramatic|unexpected|unbelievable|wild|double|hundred percent|thousands|millions|billions|for the first time)\b",
            re.IGNORECASE,
        ),
        "uncertainty": re.compile(
            r"\b(maybe|perhaps|possibly|could|might|i think|i believe|probably|presumably|apparently|allegedly)\b",
            re.IGNORECASE,
        ),
        "amusement": re.compile(
            r"\b(funny|hilarious|ironic|absurd|weird|humor|amusing)\b",
            re.IGNORECASE,
        ),
        "conclusion": re.compile(
            r"\b(so|therefore|hence|thus|overall|in short|in the end|ultimately|that means|that is|basically|essentially)\b",
            re.IGNORECASE,
        ),
        "factBuildup": re.compile(
            r"\b(first|second|on one hand|on the other hand|also|additionally|furthermore|moreover|and then|namely)\b",
            re.IGNORECASE,
        ),
        "narrative": re.compile(
            r"\b(then|suddenly|while|during|afterwards|later|next)\b",
            re.IGNORECASE,
        ),
    },
}


NUMBER_PATTERN = re.compile(
    r"\b\d[\d.,]*\s*(%|prozent|percent|jahre?|years?|mal|times|mio|mrd|million|billion|tausend|thousand|euro|dollar)?\b",
    re.IGNORECASE,
)

EXCLAMATION_INNER = re.compile(r"!\s+[A-ZÄÖÜ]")

# Split on sentence boundaries while keeping the delimiter with the preceding sentence.
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# ReDoS safety cap
MAX_SEGMENT_TEXT_LENGTH = 5000


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def _lowercase_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def _pick(pool: list[str]) -> str:
    return random.choice(pool)


def pick_reaction_type(text: str, lang: Language, is_last: bool) -> ReactionType:
    """Pick a reaction type matching the content/tone of a segment."""
    lower = text.lower()
    trimmed = text.strip()
    ends_question = trimmed.endswith("?")

    signals = SIGNALS[lang]
    has_number = bool(NUMBER_PATTERN.search(text))
    has_exclamation = trimmed.endswith("!") or bool(EXCLAMATION_INNER.search(text))

    pool: list[ReactionType] = []

    if ends_question:
        pool += ["agreement", "thought", "understanding"]
    if signals["surprise"].search(lower) or has_exclamation:
        pool += ["surprise", "surprise"]
    if has_number and not ends_question:
        pool += ["surprise", "thought"]
    if signals["uncertainty"].search(lower):
        pool += ["encouraging", "thought"]
    if signals["amusement"].search(lower):
        pool += ["amusement", "amusement"]
    if signals["conclusion"].search(lower):
        pool += ["understanding", "agreement"]
    if signals["factBuildup"].search(lower):
        pool += ["encouraging", "thought"]
    if signals["narrative"].search(lower):
        pool += ["encouraging", "thought"]
    if is_last:
        pool.append("understanding")

    if not pool:
        pool = ["agreement", "thought", "understanding"]

    return random.choice(pool)


class DisfluencyEngine:
    """Adds natural speech patterns to a script based on the configured level."""

    def __init__(self, options: DisfluencyOptions):
        self.options = options

    def process_script(
        self, segments: list[ScriptSegment]
    ) -> tuple[list[ScriptSegment], DisfluencyStatistics]:
        stats = DisfluencyStatistics()

        if self.options.level == 0:
            return segments, stats

        lang: Language = "german" if self.options.language.startswith("de") else "english"
        processed = self._cap_segment_lengths(list(segments))

        if self.options.level >= 1:
            processed = self._add_reactions(processed, lang, stats)

        if self.options.level >= 2:
            processed = self._add_pauses(processed, stats)
            processed = self._add_fill_words_to_starts(processed, lang, stats)

        if self.options.level >= 3:
            processed = self._add_sentence_restarts(processed, lang, stats)
            processed = self._add_interruptions(processed, lang, stats)
            processed = self._balance_disfluency(processed, lang, stats)

        return processed, stats

    def _cap_segment_lengths(self, segments: list[ScriptSegment]) -> list[ScriptSegment]:
        capped: list[ScriptSegment] = []
        for seg in segments:
            if seg.text and len(seg.text) > MAX_SEGMENT_TEXT_LENGTH:
                logger.warning(
                    "Segment exceeds ReDoS safety threshold, truncating",
                    extra={
                        "segmentId": seg.id,
                        "length": len(seg.text),
                        "limit": MAX_SEGMENT_TEXT_LENGTH,
                    },
                )
                capped.append(replace(seg, text=seg.text[:MAX_SEGMENT_TEXT_LENGTH]))
            else:
                capped.append(seg)
        return capped

    def _add_reactions(
        self,
        segments: list[ScriptSegment],
        lang: Language,
        stats: DisfluencyStatistics,
    ) -> list[ScriptSegment]:
        result: list[ScriptSegment] = []
        reactions = REACTIONS[lang]
        speakers = list({s.speaker for s in segments})
        last_reaction_position = -10

        for i, segment in enumerate(segments):
            result.append(segment)

            should_add = (
                segment.type == "speech"
                and (i - last_reaction_position) > 2
                and self._should_add_reaction_to_segment(segment)
            )

            if should_add and len(speakers) > 1:
                other = next((s for s in speakers if s != segment.speaker), None)
                if other is None:
                    continue

                reaction_type = pick_reaction_type(
                    segment.text, lang, i == len(segments) - 1
                )
                reaction_text = _pick(reactions[reaction_type])

                now = _now_iso()
                result.append(
                    ScriptSegment(
                        id=f"reaction-{i}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}",
                        script_id=segment.script_id,
                        speaker=other,
                        text=reaction_text,
                        type="reaction",
                        voice=None,
                        notes=None,
                        position=segment.position + 0.5,
                        word_count=len(reaction_text.split()),
                        estimated_duration=0.8,
                        regenerated_from=None,
                        created_at=now,
                        updated_at=now,
                    )
                )

                stats.reaction_count += 1
                last_reaction_position = i

        return self._recalculate_positions(result)

    @staticmethod
    def _should_add_reaction_to_segment(segment: ScriptSegment) -> bool:
        sentences = [s for s in re.split(r"[.!?]+", segment.text) if s.strip()]
        return len(sentences) >= 2 or len(segment.text) > 100

    def _add_pauses(
        self, segments: list[ScriptSegment], stats: DisfluencyStatistics
    ) -> list[ScriptSegment]:
        result: list[ScriptSegment] = []
        for idx, segment in enumerate(segments):
            result.append(segment)

            if segment.type == "speech" and segment.text.strip().endswith("?"):
                next_seg = segments[idx + 1] if idx + 1 < len(segments) else None
                if next_seg is None or next_seg.type != "reaction":
                    result.append(self._create_pause_segment(segment, 0.8))
                    stats.pause_count += 1

        return self._recalculate_positions(result)

    def _add_fill_words_to_starts(
        self,
        segments: list[ScriptSegment],
        lang: Language,
        stats: DisfluencyStatistics,
    ) -> list[ScriptSegment]:
        fill_words = FILL_WORDS[lang]
        processed_count = 0
        target_ratio = 0.3
        total = len(segments) or 1

        for segment in segments:
            if (
                segment.type != "speech"
                or processed_count / total > target_ratio
                or self._already_has_fill_words(segment.text, lang)
            ):
                continue

            if random.random() < 0.4:
                fill = _pick(fill_words["sentenceStart"])
                segment.text = f"{fill}, {_lowercase_first(segment.text)}"
                segment.word_count += 1
                stats.fill_word_count += 1
                processed_count += 1

        return segments

    def _add_sentence_restarts(
        self,
        segments: list[ScriptSegment],
        lang: Language,
        stats: DisfluencyStatistics,
    ) -> list[ScriptSegment]:
        restarts = SENTENCE_RESTARTS[lang]

        for segment in segments:
            if (
                segment.type != "speech"
                or segment.word_count < 15
                or random.random() > 0.2
            ):
                continue

            sentences = SENTENCE_SPLIT.split(segment.text)
            if len(sentences) < 3:
                continue

            idx = len(sentences) // 3
            restart = _pick(restarts)
            sentences[idx] = f"{restart}, {_lowercase_first(sentences[idx])}"
            segment.text = " ".join(sentences)
            stats.sentence_restart_count += 1

        return segments

    def _add_interruptions(
        self,
        segments: list[ScriptSegment],
        lang: Language,
        stats: DisfluencyStatistics,
    ) -> list[ScriptSegment]:
        if self.options.format != "dialog":
            return segments

        transitions = TRANSITIONS[lang]
        speakers = list({s.speaker for s in segments})
        if len(speakers) < 2:
            return segments

        for i in range(1, len(segments)):
            prev = segments[i - 1]
            current = segments[i]

            if (
                prev.type == "speech"
                and current.type == "speech"
                and prev.speaker != current.speaker
                and len(current.text) > 40
                and random.random() < 0.12
            ):
                transition = _pick(transitions)
                current.text = f"{transition} {_lowercase_first(current.text)}"
                current.word_count += len(transition.split())
                stats.interruption_count += 1

        return segments

    def _balance_disfluency(
        self,
        segments: list[ScriptSegment],
        lang: Language,
        stats: DisfluencyStatistics,
    ) -> list[ScriptSegment]:
        fill_words = FILL_WORDS[lang]

        for segment in segments:
            if segment.type != "speech":
                continue

            sentences = SENTENCE_SPLIT.split(segment.text)
            modified: list[str] = []
            words_added = 0

            for sentence in sentences:
                if len(sentence) > 80 and random.random() < 0.3:
                    words = sentence.split(" ")
                    insert_pos = len(words) // 2
                    fill = _pick(fill_words["midSentence"])
                    words.insert(insert_pos, fill)
                    words_added += 1
                    modified.append(" ".join(words))
                else:
                    modified.append(sentence)

            segment.text = " ".join(modified)
            stats.fill_word_count += words_added

        return segments

    @staticmethod
    def _create_pause_segment(after: ScriptSegment, duration: float) -> ScriptSegment:
        now = _now_iso()
        return ScriptSegment(
            id=f"pause-{after.id}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}",
            script_id=after.script_id,
            speaker=after.speaker,
            text=f"[pause {duration}s]",
            type="pause",
            voice=None,
            notes=f"pause-{duration}s",
            position=after.position + 0.3,
            word_count=0,
            estimated_duration=duration,
            regenerated_from=None,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _recalculate_positions(segments: list[ScriptSegment]) -> list[ScriptSegment]:
        for idx, seg in enumerate(segments):
            seg.position = idx
        return segments

    @staticmethod
    def _already_has_fill_words(text: str, lang: Language) -> bool:
        words = text.lower().strip().split()
        if not words:
            return False
        first = words[0]
        fill_set = {w.lower() for w in FILL_WORDS[lang]["sentenceStart"]}
        fill_set.update(w.lower() for w in FILL_WORDS[lang]["hesitation"])
        return first in fill_set

    def analyze_disfluency(
        self, segments: list[ScriptSegment]
    ) -> DisfluencyStatistics:
        stats = DisfluencyStatistics()
        fill_patterns = ["äh", "ähm", "um", "uh", "like", "halt", "quasi", "sozusagen"]

        for segment in segments:
            if segment.type == "reaction":
                stats.reaction_count += 1
            elif segment.type == "pause":
                stats.pause_count += 1
            elif segment.type == "speech":
                text = segment.text.lower()
                if "--" in text or "—" in text:
                    stats.sentence_restart_count += 1
                for pattern in fill_patterns:
                    matches = re.findall(rf"\b{pattern}\b", text, re.IGNORECASE)
                    stats.fill_word_count += len(matches)

        return stats


def create_disfluency_engine(options: DisfluencyOptions) -> DisfluencyEngine:
    return DisfluencyEngine(options)
