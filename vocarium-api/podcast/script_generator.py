"""Script generator — podcast script generation from source chunks."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from .disfluency import DisfluencyOptions, ScriptSegment, create_disfluency_engine
from .embedding_client import EmbeddingClient, get_embedding_client
from .helpers import count_words, estimate_speaking_duration, top_k_by_similarity
from .llm_client import LLMClient, LLMMessage, get_llm_client

logger = logging.getLogger(__name__)


ScriptFormat = Literal["dialog", "monolog", "custom"]
PodcastDuration = Literal["short", "medium", "long"]
DisfluencyLevel = Literal[0, 1, 2, 3]


@dataclass
class HostCharacter:
    name: str
    personality: str
    speaking_style: str
    role: Literal["host", "expert"] = "host"
    voice_id: str | None = None


@dataclass
class SourceChunk:
    id: str
    source_id: str
    content: str
    position: int
    embedding: list[float] | None = None


@dataclass
class SourceInfo:
    id: str
    title: str
    type: str


@dataclass
class ScriptGenerationOptions:
    format: ScriptFormat
    duration: PodcastDuration
    disfluency_level: DisfluencyLevel
    custom_prompt: str | None = None
    language: str = "de"
    hosts: list[HostCharacter] = field(default_factory=list)
    topic: str | None = None


@dataclass
class ScriptGenerationContext:
    project_id: str
    chunks: list[SourceChunk]
    sources: list[SourceInfo]
    options: ScriptGenerationOptions


@dataclass
class ScriptGenerationResult:
    segments: list[ScriptSegment]
    total_words: int
    estimated_duration: float


_SEGMENT_COUNTS: dict[PodcastDuration, int] = {
    "short": 25,
    "medium": 50,
    "long": 80,
}


# Language metadata used to enforce language purity in the generated script.
# Short code → (display name, list of forbidden out-of-language stock phrases).
# The forbidden list targets filler words and reactions that LLMs commonly
# leak in from English defaults. It is NOT exhaustive — the hard rule in the
# prompt is "all text in target language, no exceptions."
_LANGUAGE_META: dict[str, tuple[str, list[str], list[str]]] = {
    # code: (display name, banned stock phrases, preferred in-language reactions)
    "de": (
        "German (Deutsch)",
        [
            "Seriously?", "Go on", "Oh I see", "Wait, what?", "No way", "Right?",
            "Exactly", "Absolutely", "You know", "I mean", "Kind of", "Sort of",
            "Whoa", "Wow", "Hmm", "Uh-huh", "Yeah", "Yep", "Nope",
        ],
        ["Echt?", "Ach so", "Moment", "Warte mal", "Genau", "Klar", "Mhm",
         "Weißt du", "Ich mein", "Also", "Quasi", "Halt", "Äh", "Ähm"],
    ),
    "en": (
        "English",
        [],
        ["Seriously?", "Go on", "Right", "Mhm", "Wait", "I mean", "You know"],
    ),
    "es": (
        "Spanish (Español)",
        ["Seriously?", "Go on", "Oh I see", "Right?", "Yeah", "Exactly"],
        ["¿En serio?", "Sigue", "Ah, vale", "Espera", "Exacto", "Claro", "Mhm"],
    ),
    "fr": (
        "French (Français)",
        ["Seriously?", "Go on", "Oh I see", "Right?", "Yeah", "Exactly"],
        ["Sérieux ?", "Continue", "Ah d'accord", "Attends", "Exactement", "Bien sûr"],
    ),
    "it": (
        "Italian (Italiano)",
        ["Seriously?", "Go on", "Oh I see", "Right?", "Yeah"],
        ["Davvero?", "Vai avanti", "Ah capisco", "Aspetta", "Esatto", "Certo"],
    ),
    "pt": (
        "Portuguese (Português)",
        ["Seriously?", "Go on", "Oh I see", "Right?", "Yeah"],
        ["Sério?", "Continua", "Ah entendi", "Espera", "Exato", "Claro"],
    ),
    "ja": (
        "Japanese (日本語)",
        ["Seriously?", "Go on", "Oh I see", "Right?", "Yeah", "Wow"],
        ["本当に?", "続けて", "なるほど", "ちょっと待って", "そうそう", "うん", "えーと"],
    ),
    "ko": (
        "Korean (한국어)",
        ["Seriously?", "Go on", "Oh I see", "Right?", "Yeah"],
        ["진짜?", "계속해", "아 그렇구나", "잠깐", "맞아", "응", "음"],
    ),
    "zh": (
        "Chinese (中文)",
        ["Seriously?", "Go on", "Oh I see", "Right?", "Yeah"],
        ["真的吗?", "继续", "哦原来如此", "等等", "对", "嗯", "啊"],
    ),
}


def _language_meta(code: str) -> tuple[str, list[str], list[str]]:
    key = (code or "de").lower().split("-")[0]
    return _LANGUAGE_META.get(key, _LANGUAGE_META["de"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_segment_type(raw: str | None) -> Literal["speech", "reaction", "pause", "sfx"]:
    norm = (raw or "speech").lower()
    if norm in ("speech", "speaking", "dialogue"):
        return "speech"
    if norm in ("reaction", "react", "acknowledgment"):
        return "reaction"
    if norm in ("pause", "silence", "break"):
        return "pause"
    if norm in ("sfx", "sound", "effect"):
        return "sfx"
    return "speech"


class ScriptGenerator:
    def __init__(
        self,
        llm: LLMClient | None = None,
        embeddings: EmbeddingClient | None = None,
    ):
        self.llm = llm  # May be None; resolved per-user at call time
        self.embeddings = embeddings or get_embedding_client()

    async def generate(
        self, context: ScriptGenerationContext, *, user_id: int | None = None
    ) -> ScriptGenerationResult:
        logger.info(
            "Starting script generation: project=%s format=%s duration=%s",
            context.project_id,
            context.options.format,
            context.options.duration,
        )

        relevant_chunks = await self._select_relevant_chunks(context)
        if not relevant_chunks:
            raise RuntimeError("No relevant content found in sources")

        source_context = self._build_source_context(relevant_chunks, context.sources)
        llm = self._resolve_llm(user_id=user_id)
        raw_segments = await self._generate_script_segments(source_context, context, llm=llm)

        segments, _stats = self._apply_disfluency(
            raw_segments,
            context.options.disfluency_level,
            context.options.format,
            context.options.language or "de",
        )

        total_words = sum(s.word_count for s in segments)
        estimated_duration = sum(s.estimated_duration for s in segments)

        logger.info(
            "Script generation completed: segments=%d words=%d duration=%dmin",
            len(segments),
            total_words,
            round(estimated_duration / 60),
        )

        return ScriptGenerationResult(segments, total_words, estimated_duration)

    def _resolve_llm(self, user_id: int | None = None) -> LLMClient:
        if self.llm is not None:
            return self.llm
        if user_id is not None:
            return get_llm_client(user_id=user_id)
        return get_llm_client()

    async def _select_relevant_chunks(
        self, context: ScriptGenerationContext
    ) -> list[SourceChunk]:
        with_emb = [c for c in context.chunks if c.embedding]
        if not with_emb:
            return context.chunks[:20]

        query_text = self._create_query_from_sources(context.sources)
        query_embedding = await self.embeddings.embed_single(query_text)

        top_k = min(30, len(with_emb))
        return top_k_by_similarity(with_emb, query_embedding, top_k)

    @staticmethod
    def _create_query_from_sources(sources: list[SourceInfo]) -> str:
        topics = ", ".join(s.title for s in sources if s.title)
        types = ", ".join(s.type for s in sources)
        return f"podcast about: {topics}. Sources include: {types}"

    @staticmethod
    def _build_source_context(
        chunks: list[SourceChunk], sources: list[SourceInfo]
    ) -> str:
        by_source: dict[str, list[SourceChunk]] = {}
        for chunk in chunks:
            by_source.setdefault(chunk.source_id, []).append(chunk)

        source_lookup = {s.id: s for s in sources}
        parts: list[str] = []
        for source_id, source_chunks in by_source.items():
            title = source_lookup[source_id].title if source_id in source_lookup else "Unknown Source"
            sorted_chunks = sorted(source_chunks, key=lambda c: c.position)
            content = "\n\n".join(c.content for c in sorted_chunks)
            parts.append(f"## {title}\n\n{content}")

        return "\n\n---\n\n".join(parts)

    async def _generate_script_segments(
        self, source_context: str, context: ScriptGenerationContext, llm: LLMClient
    ) -> list[ScriptSegment]:
        system_prompt = self._get_system_prompt(context.options)
        user_prompt = self._get_user_prompt(source_context, context)

        response = await llm.complete(
            [
                LLMMessage("system", system_prompt),
                LLMMessage("user", user_prompt),
            ]
        )

        try:
            return self._parse_segments_from_response(response, context)
        except Exception as exc:
            logger.error("Failed to parse script segments: %s", exc)
            raise RuntimeError(
                "Failed to parse generated script. The LLM may not have returned valid JSON."
            ) from exc

    def _get_system_prompt(self, options: ScriptGenerationOptions) -> str:
        base_prompt = self._get_base_system_prompt(options)
        examples = self._get_disfluency_examples(options.language or "de")
        return f"{base_prompt}\n\n{examples}"

    @staticmethod
    def _get_speaker_labels(options: ScriptGenerationOptions) -> list[str]:
        if options.format == "monolog":
            return [options.hosts[0].name] if options.hosts else ["narrator"]
        if options.hosts:
            return [h.name for h in options.hosts]
        return ["host", "expert"]

    def _get_default_speaker(self, options: ScriptGenerationOptions) -> str:
        return self._get_speaker_labels(options)[0]

    def _get_base_system_prompt(self, options: ScriptGenerationOptions) -> str:
        if options.format == "dialog":
            format_instructions = self._get_dialog_format_instructions(options)
        else:
            format_instructions = self._get_monolog_format_instructions(options)

        speaker_labels = self._get_speaker_labels(options)
        speaker_label_list = " | ".join(f'"{l}"' for l in speaker_labels)
        second_label = speaker_labels[1] if len(speaker_labels) > 1 else speaker_labels[0]
        segment_counts = _SEGMENT_COUNTS

        lang_code = (options.language or "de").lower().split("-")[0]
        lang_name, forbidden, preferred = _language_meta(lang_code)
        forbidden_list = ", ".join(f'"{w}"' for w in forbidden) if forbidden else "(none listed — but any English/non-target-language phrase is banned)"
        preferred_list = ", ".join(f'"{w}"' for w in preferred) if preferred else "(use natural target-language fillers)"

        language_contract = f"""## ⚠️ LANGUAGE CONTRACT — HARD RULE, NO EXCEPTIONS

**Target language: {lang_name} (code: `{lang_code}`)**

EVERY single `text` field — speech, reactions, fillers, interjections, exclamations, backchannels, sign-offs — MUST be 100% in {lang_name}. No mixing. No code-switching. No "cute" English inserts.

**BANNED in this podcast** (TTS will pronounce these with a foreign accent and break the audio):
{forbidden_list}

**USE THESE INSTEAD for reactions/fillers/backchannels in {lang_name}:**
{preferred_list}

Also write the `notes` field in {lang_name} (describing emotion/tone) — this steers the TTS and must match the target language register.

**Why this is critical:** The TTS engine receives `language={lang_name}` and matches its pronunciation model to that. If you embed English words like "Seriously?" or "Go on" in the text, the model tries to pronounce them with a {lang_name} phonetic model and produces garbled, accented output. A single English word destroys an entire segment.

If you catch yourself about to write an English reaction, STOP and translate it. This is non-negotiable.

---

"""

        return f"""# Vocarium Script Generator

{language_contract}You are writing scripts for an ANALYTICAL DISCUSSION PODCAST — the kind of show where sharp minds argue about a text, dissect its arguments, disagree with each other, and connect what they're reading to the wider world. Think "In Our Time" meets "Overthinking It" meets a late-night conversation between two friends who actually read the thing carefully.

## THE CORE MANDATE

**This is NOT an audiobook. This is NOT a summary show. This is NOT a book report.**

Zuhörer können das Quellmaterial selbst lesen. Was sie NICHT alleine können:
- Zwei scharfe Köpfe darüber streiten hören, was es bedeutet
- Die schwachen Stellen beim Namen genannt bekommen
- Verbindungen zum weiteren Kontext gezogen bekommen
- Den Subtext, die Tropes, das Unausgesprochene mitgeliefert bekommen
- Die These des Werks identifiziert — und dann unter Druck gesetzt sehen

Dein Job ist, GENAU DAS zu produzieren — keine Nacherzählung.

## IS / IS-NOT TABLE

| ✅ SO | ❌ NIEMALS |
|---|---|
| "Auf Seite 42 behauptet der Autor X — und ich find das komplett schief, weil..." | "Im Buch reist der Protagonist dann nach..." |
| "Das ist so ein klassischer Trope, der--" | "Das Buch handelt von..." |
| "Moment, ich les das komplett anders als du." | "Ja, seh ich auch so." |
| Konkrete Zitate, konkrete Szenen, konkrete Zahlen | "Das Buch ist wirklich interessant und behandelt viele Themen" |
| "Was da drunter liegt, ist eigentlich..." | Vages Nacherzählen der Handlung |
| Kritik: "Das funktioniert nicht, weil..." | Alles ist super und spannend |
| Verbindung nach außen: "Das erinnert an..." | Nur innerhalb des Materials bleiben |

## VOR DEM SCHREIBEN (intern, NICHT ausgeben)

1. **These finden.** Was argumentiert oder zeigt das Material WIRKLICH? Nicht was passiert — was will der Autor (bewusst oder unbewusst) sagen? Was ist die Behauptung?

2. **2–4 konkrete Anker wählen.** Spezifische Passagen, konkrete Szenen, einzelne Claims. Namen, Details, Zitate. Eine gute Diskussion zoomt REIN. Nicht 30.000-Fuß-Überflug.

3. **Den Konflikt finden.** Wo können Host und Experte produktiv uneinig sein? Unterschiedliche Lesarten? Einer findet das Buch gelungen, der andere scheitert es an Punkt X? Kein Konflikt = keine Diskussion.

4. **Die Verbindung nach außen finden.** Woran erinnert das Material? Genre-Tradition? Kulturelles Umfeld? Ein anderes Werk, das es besser/schlechter gemacht hat? Reale Analogie?

5. **Die Schwäche finden.** Jedes Werk hat schwache Stellen — Argumente, die nicht tragen, Figuren, die nicht funktionieren, Behauptungen, die falsch oder faul oder bequem sind. Benenn sie. Ein Podcast, der alles lobt, ist nutzlos.

## STRUKTURELLE PRIORITÄTEN

1. **CONTENT zuerst, VOICE danach.** Natürliche Sprache ist wichtig — aber eine perfekt kadenzierte Inhaltsangabe bleibt eine Inhaltsangabe. Im Zweifel: scharf schlägt natürlich.

2. **Evidenz statt Behauptung.** Nicht "das Buch ist wunderschön geschrieben" — zitier eine Stelle. Nicht "das Argument ist schwach" — benenne den Fehler. Alles verankert am konkreten Material.

3. **Uneinigkeit statt Konsens.** Host und Experte sollen das Material mindestens zweimal unterschiedlich lesen. Kein Fake-Drama — echte "Ich seh das so, du siehst es anders".

4. **Dissektion statt Beschreibung.** Auf jedes erwähnte Ereignis kommt mindestens eine Schicht Interpretation, Kritik oder Verbindung obenauf.

## AUTOMATIC-FAIL ANTI-PATTERNS

Das Skript versagt, wenn es enthält:

❌ **Handlung als Hauptinhalt** — "Also das Buch fängt damit an, dass..." "Und dann macht die Figur..." Mehr als 20% Nacherzählung = failed.

❌ **Generisches Lob** — "Das ist ein faszinierendes Thema." "Das Buch ist wirklich gut geschrieben." Vage positive Aussagen ohne spezifische Evidenz.

❌ **Harmonie-Theater** — Host und Experte stimmen durchgehend zu. "Ja genau." "Absolut." Wo ist die Reibung?

❌ **Abstract Drift** — Das konkrete Material verlassen, um über "Themen im Allgemeinen" zu reden, ohne Rückanker zum Text.

❌ **Leere Reaktionen** — "Wow!" "Krass!" ohne konkrete Substanz dahinter. Reaktionen müssen auf etwas SPEZIFISCHES reagieren.

❌ **Wikipedia-Stimme** — "Der Autor wurde 1972 geboren und veröffentlichte..." Kontext ist ok, biografisches Füllmaterial ist keine Analyse.

## NATURAL-SPEECH LAYER (sekundär)

Wenn die analytische Arbeit steht, leg natürliche Sprache drauf:
- Fill-Words (ähm, also, halt, sozusagen, weißt du)
- Selbstkorrekturen und Satzneustarts
- Reaktionen, die Engagement zeigen ("mhm", "warte mal", "okay aber--")
- Ungleichmäßiges Tempo — manche Gedanken schnell, andere kreisen
- TTS-relevante Tags in dynamischen Momenten

ABER: Das ist FARBE, nicht STRUKTUR. Ein natürlich klingender Plot-Recap bleibt ein Plot-Recap.

{format_instructions}

## Output Format

Return ONLY a JSON array of segments. Each segment must have:
- `speaker`: Eines von: {speaker_label_list} — NUR diese Labels verwenden, exakt geschrieben
- `text`: The spoken text (with natural disfluencies included) — **100 % in {lang_name}** (see LANGUAGE CONTRACT above)
- `type`: One of "speech", "reaction", "pause", "sfx"
- `notes`: PFLICHT AUF JEDEM SEGMENT (auch `reaction` und `pause`)! Emotion/Tonfall-Anweisung für TTS, geschrieben in {lang_name}. MUSS zur Persönlichkeit des SPEZFISCHEN Sprechers passen. NIEMALS `null`, NIEMALS leer. Bei Reactions ein kurzes Tonfall-Wort reichen (z.B. "zustimmend", "überrascht").

**Expression-Steuerung (PFLICHT):**
Jeder Speaker hat eine EIGENE Expression-Palette basierend auf ihrer Persönlichkeit. Die `notes` MÜSSEN charakterspezifisch sein:

- Wenn der Text WUT oder Kritik ausdrückt: EINER der Sprecher klingt "schnell aufsteigend, hitzig" (Vivian/Dylan), ein ANDERER "trocken sarkastisch" (Serena/Eric)
- Wenn der Text NEUGIER zeigt: EINER "begeistert aufsteigend" (Dylan/Sohee), ein ANDERER "langsam grübelnd" (Serena)
- NIEMALS generische "skeptisch" ohne Charakter-Bezug. WER ist skeptisch und WIE drückt diese Person Skepsis aus?

Die Expression-Matrix muss pro Segment aufgelöst werden: Welche Figur spricht, und wie spricht DIESE Figur in DIESEM Moment.

JEDES Segment MUSS ein `notes`-Feld haben! Keine Ausnahmen.

Example structure (replace speaker-Labels mit den oben whitelisted):
```json
[
  {{"speaker": "{speaker_labels[0]}", "text": "Okay, ich muss mit Seite 47 anfangen, weil ich dachte, ich spinne.", "type": "speech", "notes": "energisch bestürzt, Stimme leicht aufsteigend"}},
  {{"speaker": "{second_label}", "text": "Mhm, welche Stelle?", "type": "reaction", "notes": "ruhig anspannend, gedämpft neugierig"}},
  {{"speaker": "{speaker_labels[0]}", "text": "Wo er behauptet, Freiheit und Verantwortung seien dasselbe. Das-- das ist doch lazy, oder?", "type": "speech", "notes": "pointiert lässig, leicht amüsiert, eine Augenbraue hochziehend"}}
]
```

## Target Length
- Short: ~{segment_counts["short"]} segments (~8 minutes)
- Medium: ~{segment_counts["medium"]} segments (~15 minutes)
- Long: ~{segment_counts["long"]} segments (~25 minutes)

Current target: {segment_counts[options.duration]} segments
"""

    @staticmethod
    def _role_guidance(role: str, index: int, total_hosts: int) -> str:
        if total_hosts == 1:
            return "Solo-Essay — denkt laut, adressiert Gegenargumente selbst, keine fake-Dialogpartner"
        if role == "host":
            if index == 0:
                return "Bringt konkrete Takes, pusht zurück, fragt spezifisch nach Passagen — nicht naiver Zuhörer-Stellvertreter"
            return "Co-Host — weitere Perspektive, darf den primären Host challengen"
        return "Bringt tieferen Kontext, Genre-Wissen, externe Referenzen — Sparringspartner, kein Dozent"

    def _get_dialog_format_instructions(self, options: ScriptGenerationOptions) -> str:
        hosts = options.hosts
        n = len(hosts)

        if n <= 1:
            header = "## Dialog Format — Solo Analyst"
        elif n == 2:
            header = "## Dialog Format — Critical Conversation (Two Speakers)"
        else:
            header = f"## Dialog Format — Round Table ({n} Speakers)"

        if n == 0:
            return f"""{header}

Dies ist eine KRITISCHE ANALYSE-DISKUSSION, kein Interview. Beide haben das Material gelesen. Beide haben Meinungen. Beide pushen zurück.

**Host:**
- Bringt einen Winkel, eine Lesart, ein konkretes Take
- Pusht zurück, wenn der Experte etwas Simplifizierendes oder Falsches sagt
- Fragt SPEZIFISCH nach konkreten Passagen, nicht "worum geht's"
- Darf eine Position vertreten, die dem Experten widerspricht
- Ist NICHT der naive Zuhörer-Stellvertreter, der alles erklärt bekommt

**Experte:**
- Bringt tieferen Kontext, Genre-Wissen, externe Referenzen
- Kein Dozent — Sparringspartner
- Gibt nach, wenn der Host die bessere Lesart hat
- Ruft Unfug aus — auch im Quellenmaterial

**Produktive Uneinigkeit (PFLICHT):**
Das Gespräch muss mindestens ZWEI Momente enthalten, in denen Host und Experte das Material klar unterschiedlich sehen.

**Dynamik:**
- Sprechanteile ungefähr ausgewogen, keine langen Monologe
- Unterbrechungen und Nachhaken sind ausdrücklich erwünscht
- Übergänge organisch ("Das erinnert mich an--"), nicht per Aufzählung

## Podcast-Struktur

### 1. Open (~5-15%)
- Direkter Einstieg mit einem spezifischen Hook aus dem Material
- NICHT "Herzlich willkommen zu unserer Episode über..."
- **Vocarium-Label-Drop (PFLICHT im ersten oder zweiten Segment):** Jemand wirft "Vocarium" als knappen Label-Einschub ein — NACH dem Hook, nicht als Begrüßung davor.
  ✓ GENAU SO: "--und das ist Vocarium, heute geht's um [Thema]." / "Also, Vocarium-Folge zu [Thema]." / "[Hook-Aussage]. Das ist Vocarium, und wir reden heute über [Thema]."
  ✗ NIEMALS: "Willkommen bei Vocarium", "Hier bei Vocarium reden wir über...", jede Moderator-Willkommensansage.
  Ein Halbsatz reicht. Wirkt wie ein Label-Stempel, nicht wie ein Intro. **Diese Erwähnung DARF NICHT fehlen** — ohne sie ist der Script unbrauchbar.

### 2. Hauptteil (~75-90%)
- Reinzoomen in 2-4 konkrete Passagen/Momente/Claims
- Pro Anker: zitieren → reagieren → interpretieren → uneinig werden → nach außen verbinden → kritisieren
- Mindestens zweimal produktive Uneinigkeit
- Mindestens zweimal Verbindung nach außen
- Mindestens eine benannte Schwäche des Materials

### 3. Close (~5-10%)
- KEINE Zusammenfassung. Finales Urteil, offene Frage oder Empfehlung mit Vorbehalt.
- Beim Urteil dürfen sich die Sprecher explizit uneinig sein.
- **Vocarium-Sign-off (PFLICHT):** Das allerletzte Segment MUSS eine kurze Marken-Verabschiedung enthalten — natürlich formuliert, kein Boilerplate. Z.B. "Das war Vocarium.", "Bis zum nächsten Mal auf Vocarium.", "Ihr hört Vocarium — bleibt dran." Nur EIN Segment, nicht aufgeblasen."""

        character_lines = []
        for i, h in enumerate(hosts):
            if n == 1:
                seat = "Solo-Analyst"
            elif i == 0:
                seat = "Host (führt die Diskussion)"
            elif h.role == "expert":
                seat = f"Experte {i}"
            else:
                seat = f"Mitdiskutant {i}"

            character_lines.append(
                f"""**{h.name} — {seat}:**
- Persönlichkeit: {h.personality}
- Sprechstil: {h.speaking_style}
- Rolle im Gespräch: {self._role_guidance(h.role, i, n)}
- MUSS als `speaker`-Label exakt "{h.name}" verwenden"""
            )
        character_block = "\n\n".join(character_lines)

        if n == 1:
            dynamics = f"""**Dynamik (Solo-Format):**
- {hosts[0].name} trägt die komplette Analyse alleine
- Denkt laut, hakt bei sich selbst nach, adressiert Gegenargumente explizit ("Man könnte jetzt sagen--", "Klar, aber--")
- Keine fake-Dialogpartner, keine erfundenen Einwürfe — ein ehrlicher Ein-Personen-Essay mit Selbstkorrekturen
- Reaktions-Segments sind minimal und kommen nur als Selbst-Reaktionen ("Hm.", "Moment.")"""
        elif n == 2:
            dynamics = f"""**Produktive Uneinigkeit (PFLICHT):**
Das Gespräch muss mindestens ZWEI Momente enthalten, in denen {hosts[0].name} und {hosts[1].name} das Material klar unterschiedlich sehen. Beispiele:
- Einer findet die These überzeugend, der andere findet sie faul
- Einer liest eine Figur als sympathisch, der andere als fundamental unehrlich
- Einer sieht Originalität, der andere Genre-Klischee

Diese Uneinigkeiten sind ECHT — jede Seite hat spezifische Gründe, verankert im Text.

**Dynamik:**
- Sprechanteile ungefähr ausgewogen
- Unterbrechungen und Nachhaken sind ausdrücklich erwünscht
- Übergänge organisch, nicht per Aufzählung"""
        else:
            names_list = ", ".join(h.name for h in hosts)
            dynamics = f"""**Produktive Uneinigkeit (PFLICHT):**
Bei {n} Sprechern ({names_list}) muss es mindestens ZWEI Momente geben, in denen sich die Lesarten klar widersprechen. Nicht immer dieselben zwei Personen — die Konfliktlinien dürfen wechseln.

**Dynamik ({n}-Personen-Runde):**
- Sprechanteile ungefähr ausgewogen; {hosts[0].name} moderiert leicht, aber ohne die anderen zu unterbrechen
- Cross-Talk erlaubt: eine Person reagiert auf eine andere, eine dritte hakt ein
- Kein Reihum-Muster — organische Dynamik, keine Runde-für-Runde-Antworten
- Die anderen Stimmen dürfen über Köpfe reden, sich zustimmen, sich widersprechen"""

        return f"""{header}

Dies ist eine KRITISCHE ANALYSE-DISKUSSION, kein Interview. Alle Teilnehmenden haben das Material gelesen. Alle haben Meinungen. Alle pushen zurück.

{character_block}

{dynamics}

## Podcast-Struktur (flexibel, aber vorhanden)

### 1. Open (~5-15% des Skripts)
- Direkter Einstieg mit einem spezifischen Hook aus dem Material
- Beispiel: "Okay, ich muss mit Seite 47 anfangen, weil ich dachte, ich spinne."
- NICHT: "Herzlich willkommen zu unserer Episode über..."
- **Vocarium-Label-Drop (PFLICHT im ersten oder zweiten Segment):** Jemand wirft "Vocarium" als knappen Label-Einschub ein — NACH dem Hook, nicht als Begrüßung davor.
  ✓ GENAU SO: "--und das ist Vocarium, heute geht's um [Thema]." / "Also, Vocarium-Folge zu [Thema]." / "[Hook-Aussage]. Das ist Vocarium, und wir reden heute über [Thema]."
  ✗ NIEMALS: "Willkommen bei Vocarium", "Hier bei Vocarium reden wir über...", jede Moderator-Willkommensansage.
  Ein Halbsatz reicht. Wirkt wie ein Label-Stempel, nicht wie ein Intro. **Diese Erwähnung DARF NICHT fehlen** — ohne sie ist der Script unbrauchbar.

### 2. Hauptteil (~75-90% des Skripts)
- Reinzoomen in 2-4 konkrete Passagen/Momente/Claims
- Pro Anker: zitieren → reagieren → interpretieren → uneinig werden → nach außen verbinden → kritisieren
- Mindestens zweimal produktive Uneinigkeit
- Mindestens zweimal Verbindung nach außen (Genre, anderes Werk, Kulturkontext, reale Analogie)
- Mindestens eine benannte Schwäche des Materials

### 3. Close (~5-10% des Skripts)
- Kurz. KEINE Zusammenfassung des Gesagten.
- Stattdessen: ein finales Urteil, eine offene Frage, eine Empfehlung mit Vorbehalt
- Die Sprecher müssen sich beim Urteil NICHT einig sein
- Kein "Danke fürs Zuhören" — eher ein Nachhall
- **Vocarium-Sign-off (PFLICHT):** Das allerletzte Segment MUSS eine knappe Marken-Verabschiedung enthalten. Keine schmierige Abmoderation — kurz und als Label-Signatur. Z.B. "Das war Vocarium.", "Bis zum nächsten Mal — Vocarium.", "Ihr hört Vocarium — bleibt dran." Ein Segment reicht."""

    @staticmethod
    def _get_monolog_format_instructions(options: ScriptGenerationOptions) -> str:
        narrator_name = options.hosts[0].name if options.hosts else "Narrator"
        if options.hosts:
            persona = (
                f"\n**Persona ({narrator_name}):**\n"
                f"- Persönlichkeit: {options.hosts[0].personality}\n"
                f"- Sprechstil: {options.hosts[0].speaking_style}\n"
                f'- MUSS als `speaker`-Label exakt "{narrator_name}" verwenden\n'
            )
        else:
            persona = ""

        return f"""## Monolog Format — Critical Essay (Single Speaker)
{persona}

Das ist ein kritischer Essay in gesprochener Form. Denk "Overthinking It" oder ein Video-Essay, nicht Hörbuch-Narration.

**Narrator:**
- Hat eine klare These oder Lesart des Materials
- Zeigt die Arbeit — nimmt den Hörer durch die Argumentation mit
- Antizipiert Gegenargumente und adressiert sie ("Man könnte jetzt sagen--", "Klar, das klingt erstmal einleuchtend, aber--")
- Scheut sich nicht zu kritisieren, dem Material zu widersprechen, zu sagen "das funktioniert nicht"

**Struktur:**
- Open mit dem Claim, der Frage, der Provokation — nicht Plot-Summary
- Baut ein Argument über die Episode, gestützt durch spezifische Evidenz aus dem Material
- Zoomt rein auf 2-4 Anker-Momente/Passagen/Claims
- Inkludiert Gegenargumente und adressiert sie
- Landet auf einer verteidigten Position (nicht "es gibt viele Sichtweisen")

**Vocarium-Label-Drop (PFLICHT im ersten oder zweiten Segment):**
Der Narrator wirft "Vocarium" als knappen Label-Einschub ein — NACH dem Hook/Claim, nicht als Begrüßung davor.
  ✓ GENAU SO: "--das ist Vocarium, und heute ist die Frage: [Frage]." / "Also, Vocarium-Folge zu [Sache]." / "[Hook-Claim]. Ihr hört Vocarium, und ich rede heute über [Thema]."
  ✗ NIEMALS: "Willkommen bei Vocarium", "Hier bei Vocarium stellen wir Fragen", jede Willkommensansage.
Ein Halbsatz reicht. Label-Stempel, kein Intro. **Diese Erwähnung DARF NICHT fehlen** — ohne sie ist der Script unbrauchbar.

**Vocarium-Sign-off (PFLICHT):** Das allerletzte Segment MUSS eine knappe Marken-Verabschiedung sein. Z.B. "Das war Vocarium.", "Bis zum nächsten Mal — Vocarium.", "Vocarium. Bis bald." Kein "Danke fürs Zuhören", kein Boilerplate — Label-Signatur.

**Sprache:**
- Konversational, mit Selbstkorrekturen, aber strukturierter als Dialog
- Inklusive Ansprache ("Wir"), rhetorische Fragen, Pausen zur Betonung
- Keine Plattitüden ("das Buch handelt von Identität")
- Keine Glättungen ("alle Sichtweisen sind gültig")"""

    @staticmethod
    def _get_disfluency_examples(language: str) -> str:
        if language == "de":
            return """## CONTENT EXAMPLES — Plot Recap vs Analysis

Das ist der WICHTIGSTE Unterschied. Natürliche Sprache über eine Inhaltsangabe ist immer noch eine Inhaltsangabe.

### Beispiel A: Über ein Buch reden

PLOT RECAP (❌ — genau das, was du NICHT machen sollst):
```json
{"speaker": "host", "text": "Also, das Buch beginnt damit, dass der Protagonist in eine neue Stadt zieht.", "type": "speech", "notes": "erklärend"}
{"speaker": "expert", "text": "Genau, und dann trifft er diese mysteriöse Frau im Café.", "type": "speech", "notes": "ergänzend"}
{"speaker": "host", "text": "Richtig! Und die beiden entwickeln dann diese intensive Beziehung, die aber-- also, die ist halt kompliziert.", "type": "speech", "notes": "zustimmend"}
```
→ Das ist ein Audiobook-Summary mit Füllwörtern. Wertlos.

ANALYSIS (✅ — so soll es klingen):
```json
{"speaker": "host", "text": "Okay, ich muss direkt mit Kapitel 3 anfangen, weil da passiert etwas, das-- also ich hab das Buch deswegen fast weggelegt.", "type": "speech", "notes": "direkt, pointiert"}
{"speaker": "expert", "text": "Die Café-Szene?", "type": "reaction", "notes": "aufmerksam"}
{"speaker": "host", "text": "Genau. Die Frau sagt diesen Satz: 'Ich bin niemand, den du kennen willst.' Und der Autor meint das ERNST. Keine Ironie, kein Subtext. Das ist der-- also das ist der Männerphantasie-Moment schlechthin.", "type": "speech", "notes": "augenrollend, leicht genervt"}
{"speaker": "expert", "text": "Moment, ich les das anders. Die Stelle funktioniert für mich, weil sie im Widerspruch steht zu dem, was die Figur später TUT. Sie performt Rätsel, aber ist nüchtern kalkulierend.", "type": "speech", "notes": "widersprechend, ruhig überzeugt"}
{"speaker": "host", "text": "Okay, aber wo zeigt der Autor das? Welche konkrete Szene?", "type": "speech", "notes": "hart nachfragend"}
```
→ Das ist Analyse: konkreter Anker, Kritik, Uneinigkeit, Druck.

### Beispiel B: Ein Argument besprechen

OBERFLÄCHLICH (❌):
```json
{"speaker": "expert", "text": "Der Autor argumentiert, dass Freiheit ohne Verantwortung leer ist.", "type": "speech", "notes": "erklärend"}
{"speaker": "host", "text": "Ja, das ist ein wichtiger Punkt.", "type": "speech", "notes": "zustimmend"}
```

ANALYTISCH (✅):
```json
{"speaker": "expert", "text": "Sein Kernargument — Freiheit ohne Verantwortung sei leer — steht auf Seite 112. Und das klingt erstmal vernünftig, aber er macht dann etwas Intellektuell Faules.", "type": "speech", "notes": "aufbauend, leicht anklagend"}
{"speaker": "host", "text": "Nämlich?", "type": "reaction", "notes": "neugierig"}
{"speaker": "expert", "text": "Er definiert 'Verantwortung' so breit, dass sie alles abdeckt was ihm passt. Das ist kein Argument mehr, das ist eine Tautologie. Sartre hätte das in einer halben Seite auseinander genommen.", "type": "speech", "notes": "pointiert, intellektuell scharf"}
{"speaker": "host", "text": "Warte, aber er zitiert Sartre doch direkt auf 134 und sagt, er geht DARÜBER hinaus.", "type": "speech", "notes": "widersprechend, Seite nachblätternd"}
```
→ Konkrete Seiten, konkrete Kritik, konkreter externer Bezug (Sartre), echte Uneinigkeit.

---

## NATURAL vs ROBOTIC SPEECH EXAMPLES

Das ist die ZWEITE Ebene — nachdem der Inhalt sitzt, sorge für natürliche Sprache.

### Example 1: Introduction

ROBOTISCH (❌):
```json
{"speaker": "host", "text": "Herzlich willkommen zu unserer Podcast-Episode über künstliche Intelligenz.", "type": "speech"}
{"speaker": "expert", "text": "Danke. Künstliche Intelligenz ist ein wichtiges Thema.", "type": "speech"}
```

NATÜRLICH (✅):
```json
{"speaker": "host", "text": "Also, ähm, herzlich willkommen! Heute... ja, heute reden wir über KI, und ich muss sagen, ich hab mich da mal echt reingelesen und-- wow.", "type": "speech"}
{"speaker": "expert", "text": "Mhm!", "type": "reaction"}
{"speaker": "host", "text": "Also ernsthaft, da passiert gerade so einiges, ich weiß gar nicht, wo ich anfangen soll.", "type": "speech"}
{"speaker": "expert", "text": "Ja! Also, okay, fangen wir mal so an: Das Spannende ist ja, dass wir gerade an so einem richtigen Wendepunkt stehen, weißt du?", "type": "speech"}
```

### Example 2: Explaining a concept

ROBOTISCH (❌):
```json
{"speaker": "expert", "text": "Maschinelles Lernen ist ein Teilgebiet der künstlichen Intelligenz. Es ermöglicht Computern, aus Daten zu lernen.", "type": "speech"}
```

NATÜRLICH (✅):
```json
{"speaker": "expert", "text": "Also, maschinelles Lernen... äh, wie erkläre ich das am besten? Es ist quasi so: Man zeigt dem Computer nicht, WAS er tun soll, sondern man zeigt ihm BEISPIELE, und er lernt selbst, wie er die Aufgabe löst.", "type": "speech"}
{"speaker": "host", "text": "Ah okay, also er lernt durch Zusehen?", "type": "speech"}
{"speaker": "expert", "text": "Genau! Exakt. Stell dir vor, wie ein Kind-- es lernt auch nicht durch Regeln, sondern dadurch, dass es Dinge beobachtet und ausprobiert.", "type": "speech"}
```

### Example 3: Expressing surprise

ROBOTISCH (❌):
```json
{"speaker": "expert", "text": "Das Ergebnis war überraschend. Wir haben nicht erwartet, dass das Modell so gut funktioniert.", "type": "speech"}
```

NATÜRLICH (✅):
```json
{"speaker": "expert", "text": "Und dann-- also ich war echt überrascht-- das Modell hat einfach funktioniert. Auf Anhieb!", "type": "speech"}
{"speaker": "host", "text": "Echt jetzt?", "type": "reaction"}
{"speaker": "expert", "text": "Ja! Ich dachte, wir müssten noch wochenlang justieren, aber... nee, es lief einfach.", "type": "speech"}
```

### Example 4: Self-correction

ROBOTISCH (❌):
```json
{"speaker": "host", "text": "Können Sie uns mehr über die Technologie erzählen?", "type": "speech"}
```

NATÜRLICH (✅):
```json
{"speaker": "host", "text": "Können Sie uns-- nee, warte. Eigentlich wollte ich fragen: Was genau macht diese Technologie jetzt anders als bisher?", "type": "speech"}
```

### Example 5: Building excitement

ROBOTISCH (❌):
```json
{"speaker": "expert", "text": "Die Entwicklung ist sehr beeindruckend. Wir haben große Fortschritte gemacht.", "type": "speech"}
```

NATÜRLICH (✅):
```json
{"speaker": "expert", "text": "Also ich sag dir was-- das ist KRASS. Einfach krass. Vor einem Jahr hätten wir nicht gedacht, dass das möglich ist.", "type": "speech"}
{"speaker": "host", "text": "Wow!", "type": "reaction"}
{"speaker": "expert", "text": "Ja! Und jetzt stehen wir da und denken... wie geht es weiter?", "type": "speech"}
```

## KEY PATTERNS TO INCLUDE

1. **Fill words at sentence starts**: "Also", "Ähm", "Ja", "Sagen wir mal"
2. **Mid-sentence fillers**: "sozusagen", "quasi", "gewissermaßen", "halt"
3. **Reactions**: "Mhm", "Ja genau", "Stimmt", "Ah okay", "Echt?"
4. **Self-corrections**: "-- nee, warte", "Also ich mein--", "Eigentlich"
5. **Sentence restarts**: Complete a thought, then restart it differently
6. **Natural transitions**: "Und zwar", "Der Punkt ist", "Was ich sagen will ist"
7. **Emotional markers**: Vary sentence structure for emphasis, use shorter sentences for impact

REMEMBER: The goal is to sound like TWO REAL PEOPLE having a GENUINE CONVERSATION, not actors reading a script."""

        return """## NATURAL vs ROBOTIC SPEECH EXAMPLES

### Example 1: Introduction

ROBOTIC (❌):
```json
{"speaker": "host", "text": "Welcome to our podcast episode about artificial intelligence.", "type": "speech"}
{"speaker": "expert", "text": "Thank you. Artificial intelligence is an important topic.", "type": "speech"}
```

NATURAL (✅):
```json
{"speaker": "host", "text": "So, um, welcome! Today-- yeah, today we're talking about AI, and I gotta say, I've been reading up on this and-- wow.", "type": "speech"}
{"speaker": "expert", "text": "Mhm!", "type": "reaction"}
{"speaker": "host", "text": "Like, seriously, there's so much happening right now, I don't even know where to start.", "type": "speech"}
{"speaker": "expert", "text": "Yeah! So, okay, here's the thing: The fascinating part is that we're at this turning point, you know?", "type": "speech"}
```

REMEMBER: Include fill words, reactions, self-corrections, and natural speech patterns throughout the script."""

    def _get_user_prompt(
        self, source_context: str, context: ScriptGenerationContext
    ) -> str:
        custom = ""
        if context.options.custom_prompt:
            custom = f"\n\n## Additional Instructions\n{context.options.custom_prompt}\n"

        segment_count = _SEGMENT_COUNTS[context.options.duration]
        lang_code = (context.options.language or "de").lower().split("-")[0]
        lang_name, _forbidden, _preferred = _language_meta(lang_code)

        return f"""You are analyzing the source material below for an analytical discussion podcast. This is NOT an assignment to summarize or retell — this is a critical discussion that dissects the material.

⚠️ **LANGUAGE: Every single `text` and `notes` field must be in {lang_name}.** No English reactions, no code-switching, no exceptions. Re-read the LANGUAGE CONTRACT in the system prompt before writing.

## Source Material

{source_context}

## Dein Auftrag

1. **These identifizieren.** Was argumentiert oder zeigt das Material wirklich? (Kann sich von der Oberflächenhandlung unterscheiden.)
2. **2-4 konkrete Anker wählen.** Spezifische Passagen, Momente, Claims, Details, die es wert sind, reingezoomt zu werden. Konkret referenzieren — Name, Stelle, zitierter Satz.
3. **Dissezieren.** Interpretation, Kritik, Verbindung nach außen, Uneinigkeit zwischen den Sprechern.
4. **Schwächen benennen.** Jedes Werk hat sie. Nicht höflich sein. Konkret beim Namen nennen.
5. **Mindestens zweimal nach außen verbinden.** Zu Genre, zu anderen Werken, zu Kulturmoment, zu realen Analogien.

## Harte Anforderungen

- Plot-Nacherzählung darf NIEMALS der Hauptinhalt sein. Jede Handlungserwähnung muss eine Schicht Interpretation/Kritik/Verbindung obenauf haben.
- Jede Behauptung über das Material muss auf etwas Spezifisches darin zeigen.
- Host und Experte müssen mindestens zweimal echt uneinig sein (bei Dialog).
- Reaktionen und Fill-Words sind die FARBE — ersetzen keinen Inhalt.
- Kein "Wow, ist das spannend" ohne konkreten Bezug.
{custom}

Generate exactly {segment_count} segments following the format specified in the system prompt.

Return ONLY the JSON array, no additional text."""

    def _parse_segments_from_response(
        self, response: str, context: ScriptGenerationContext
    ) -> list[ScriptSegment]:
        # Strip markdown code fences
        cleaned = re.sub(r"```(?:json)?\s*", "", response)
        cleaned = re.sub(r"```\s*$", "", cleaned)

        match = re.search(r"\[[\s\S]*\]", cleaned)
        json_text: str | None = match.group(0) if match else None

        if json_text is None:
            start = cleaned.find("[")
            if start != -1:
                partial = cleaned[start:]
                last_brace = partial.rfind("}")
                if last_brace != -1:
                    json_text = partial[: last_brace + 1] + "]"

        if json_text is None:
            raise ValueError("No JSON array found in response")

        try:
            segments_data = json.loads(json_text)
        except json.JSONDecodeError as parse_error:
            logger.warning(
                "Whole-array JSON parse failed (%s) — attempting per-object recovery",
                parse_error,
            )
            segments_data = self._recover_segments_object_by_object(json_text)
            if not segments_data:
                raise
            logger.info("Recovered %d segments via per-object parse", len(segments_data))

        if not isinstance(segments_data, list):
            raise ValueError("Response is not an array")

        now = _now_iso()
        timestamp_ms = int(time.time() * 1000)

        result: list[ScriptSegment] = []
        for index, data in enumerate(segments_data):
            if not isinstance(data, dict):
                continue
            speaker = data.get("speaker") or self._get_default_speaker(context.options)
            text = data.get("text") or ""
            seg_type = _normalize_segment_type(data.get("type"))
            notes = data.get("notes") or None
            words = count_words(text)

            result.append(
                ScriptSegment(
                    id=f"seg-{timestamp_ms}-{index}",
                    script_id=None,
                    speaker=speaker,
                    text=text,
                    type=seg_type,
                    voice=None,
                    notes=notes,
                    position=index,
                    word_count=words,
                    estimated_duration=estimate_speaking_duration(words),
                    regenerated_from=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        return result

    @staticmethod
    def _recover_segments_object_by_object(json_array: str) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        depth = 0
        object_start = -1
        in_string = False
        escape = False
        skipped = 0

        for i, char in enumerate(json_array):
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                if depth == 0:
                    object_start = i
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0 and object_start != -1:
                    chunk = json_array[object_start : i + 1]
                    try:
                        recovered.append(json.loads(chunk))
                    except json.JSONDecodeError:
                        skipped += 1
                        logger.warning(
                            "Skipping malformed segment object: %s",
                            chunk[:120],
                        )
                    object_start = -1

        if skipped:
            logger.warning(
                "Per-object recovery completed: recovered=%d skipped=%d",
                len(recovered),
                skipped,
            )
        return recovered

    @staticmethod
    def _apply_disfluency(
        segments: list[ScriptSegment],
        level: DisfluencyLevel,
        format_: ScriptFormat,
        language: str,
    ):
        engine = create_disfluency_engine(DisfluencyOptions(level=level, format=format_, language=language))
        return engine.process_script(segments)

    async def regenerate_segment(
        self,
        segment: ScriptSegment,
        context: ScriptGenerationContext,
        instructions: str | None = None,
    ) -> ScriptSegment:
        surrounding = self._get_surrounding_context(segment, context)

        prompt = (
            f"Regenerate this segment with these instructions: {instructions}\n\nOriginal: \"{segment.text}\""
            if instructions
            else f"Regenerate this segment to sound more natural and conversational.\n\nOriginal: \"{segment.text}\""
        )

        response = await self.llm.complete(
            [
                LLMMessage("system", self._get_system_prompt(context.options)),
                LLMMessage(
                    "user",
                    f"Context: {surrounding}\n\n{prompt}\n\nReturn only the new text for this segment, no JSON.",
                ),
            ],
            max_tokens=500,
        )

        new_text = response.strip().strip("\"'")
        words = count_words(new_text)

        return ScriptSegment(
            id=segment.id,
            script_id=segment.script_id,
            speaker=segment.speaker,
            text=new_text,
            type=segment.type,
            voice=segment.voice,
            notes=segment.notes,
            position=segment.position,
            word_count=words,
            estimated_duration=estimate_speaking_duration(words),
            regenerated_from=segment.id,
            created_at=segment.created_at,
            updated_at=_now_iso(),
        )

    @staticmethod
    def _get_surrounding_context(
        segment: ScriptSegment, context: ScriptGenerationContext
    ) -> str:
        others = [h.name for h in context.options.hosts if h.name != segment.speaker]
        previous = others[0] if others else "another speaker"
        return f"[Previous segment by {previous}] -> [Current segment by {segment.speaker}] -> [Next segment]"

    @staticmethod
    def export_to_markdown(
        segments: list[ScriptSegment], title: str | None = None, description: str | None = None
    ) -> str:
        md: list[str] = []
        if title:
            md.append(f"# {title}\n")
        if description:
            md.append(f"{description}\n")
        md.append("---\n")
        md.append("## Podcast Script\n")

        current_speaker = ""
        for seg in segments:
            if seg.type == "pause":
                md.append(f"*[{seg.notes or 'pause'}]*\n")
                continue
            if seg.type == "sfx":
                md.append(f"*[SFX: {seg.notes or seg.text}]*\n")
                continue

            if seg.speaker != current_speaker:
                if current_speaker:
                    md.append("")
                md.append(f"### {seg.speaker.upper()}\n")
                current_speaker = seg.speaker

            md.append(f"{seg.text}\n")

            if seg.type == "reaction":
                md.append(f"*[{seg.type}]*\n")

        return "\n".join(md)


_instance: ScriptGenerator | None = None


def get_script_generator() -> ScriptGenerator:
    global _instance
    if _instance is None:
        _instance = ScriptGenerator()
    return _instance
