"""Redaktion vor dem Skript: drei parallele Analyse-Agenten und ein Lektorat.

Der Skript-Generator schrieb bisher in einem einzigen Aufruf aus dem rohen
Quellkontext. Das erzeugt glatte, austauschbare Gespräche: der Autor muss in
demselben Atemzug lesen, verstehen, streiten und formulieren. Hier lesen drei
Agenten das Material zuerst getrennt (Belege, Gegenlesart, Dramaturgie) und
liefern ein Dossier, aus dem das Skript dann konkret wird. Danach prüft ein
Lektorat das fertige Skript gegen die Redaktionsregeln und schreibt schwache
Segmente um.

Alle Agenten laufen auf dem konfigurierten Anbieter des Nutzers, standardmäßig
mit `glm-5.3-flash` (PODCAST_ANALYSIS_MODEL); das Skript selbst schreibt das
im Anbieter gewählte Modell.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING, Any

from .llm_client import LLMClient, LLMMessage, extract_json

if TYPE_CHECKING:  # pragma: no cover
    from .disfluency import ScriptSegment
    from .script_generator import ScriptGenerationContext

logger = logging.getLogger("vocarium.podcast.analysis")

ANALYSIS_MODEL = os.getenv("PODCAST_ANALYSIS_MODEL", "glm-5.3-flash")
ANALYSIS_AGENTS = max(0, int(os.getenv("PODCAST_ANALYSIS_AGENTS", "3")))
ANALYSIS_MAX_TOKENS = int(os.getenv("PODCAST_ANALYSIS_MAX_TOKENS", "4096"))
ANALYSIS_TEMPERATURE = float(os.getenv("PODCAST_ANALYSIS_TEMPERATURE", "0.4"))
REVIEW_ENABLED = os.getenv("PODCAST_SCRIPT_REVIEW", "true").lower() in ("1", "true", "yes")
REVIEW_MAX_SHARE = float(os.getenv("PODCAST_REVIEW_MAX_SHARE", "0.4"))
# Der Quellkontext wird je Agent auf diese Länge gekürzt; die Auswahl der
# relevantesten Chunks geschieht davor im Generator.
ANALYSIS_CONTEXT_CHARS = int(os.getenv("PODCAST_ANALYSIS_CONTEXT_CHARS", "60000"))

AGENT_ORDER = ("facts", "critique", "dramaturgy")

AGENT_LABELS = {
    "facts": "Belege",
    "critique": "Gegenlesart",
    "dramaturgy": "Dramaturgie",
}

_COMMON = (
    "Du bist Teil der Redaktion eines analytischen Gesprächspodcasts. Du schreibst KEIN Skript. "
    "Du lieferst ausschließlich ein JSON-Objekt nach dem vorgegebenen Schema, ohne Text davor oder danach, "
    "ohne Codezaun. Alle Werte auf Deutsch. Jede Aussage muss sich auf eine konkrete Stelle im Material stützen; "
    "erfinde nichts, was nicht im Material steht, und kennzeichne Vermutungen als solche."
)

AGENT_PROMPTS: dict[str, tuple[str, str]] = {
    "facts": (
        _COMMON + " Deine Rolle: Rechercheur. Du sammelst, was belegbar ist.",
        """Lies das Material und liefere:
{{
  "thesis": "Was das Material wirklich behauptet oder zeigt (1-2 Sätze, nicht die Handlung)",
  "anchors": [
    {{"quote": "wörtliches Zitat oder sehr enge Paraphrase (max. 40 Wörter)", "where": "Quelle / Abschnitt / Seite, so genau wie möglich", "why_it_matters": "warum diese Stelle die Diskussion trägt"}}
  ],
  "facts": ["8-15 harte Fakten: Namen, Zahlen, Daten, Orte, Definitionen — jeweils ein Satz mit Fundstelle"],
  "terms": [{{"term": "Fachbegriff oder Eigenname", "meaning": "knappe Erklärung aus dem Material"}}],
  "open_questions": ["3-5 Fragen, die das Material aufwirft, aber nicht beantwortet"]
}}
Liefere 6 bis 10 anchors. Zitate müssen im Material stehen.

## Material
{context}"""
    ),
    "critique": (
        _COMMON + " Deine Rolle: Kritikerin. Du suchst Reibung, Schwächen und Verbindungen nach außen.",
        """Lies das Material und liefere:
{{
  "weaknesses": [
    {{"claim": "Behauptung oder Stelle im Material", "evidence": "wo genau", "why_weak": "warum das nicht trägt: Logikfehler, fehlender Beleg, Klischee, Bequemlichkeit"}}
  ],
  "counter_readings": [
    {{"surface_reading": "die naheliegende Lesart", "counter": "eine begründete Gegenlesart", "grounded_in": "Stelle im Material"}}
  ],
  "connections": [
    {{"anchor": "Stelle im Material", "outside": "Genre-Tradition, anderes Werk, historischer oder aktueller Kontext, reale Analogie", "insight": "was die Verbindung sichtbar macht"}}
  ],
  "disputes": [
    {{"topic": "worüber sich zwei kluge Leser streiten würden", "position_a": "Position mit Begründung aus dem Material", "position_b": "Gegenposition mit Begründung aus dem Material"}}
  ]
}}
Liefere mindestens 3 weaknesses, 2 counter_readings, 3 connections und 3 disputes. Keine Höflichkeit, keine Pauschalurteile: jede Kritik zeigt auf eine Stelle.

## Material
{context}"""
    ),
    "dramaturgy": (
        _COMMON + " Deine Rolle: Dramaturg. Du planst den Ablauf einer Folge für konkrete Sprecher.",
        """Sprecher dieser Folge:
{hosts}

Format: {format}, Ziel etwa {segments} Segmente. Thema laut Redaktion: {topic}

Lies das Material und liefere:
{{
  "hook": "der erste Satz der Folge: ein konkreter, überraschender Einstieg aus dem Material, kein Willkommen",
  "acts": [
    {{"title": "Akt-Titel", "anchor": "die konkrete Stelle, um die es geht", "beats": ["3-5 Gesprächsschritte: zitieren, reagieren, deuten, streiten, verbinden, urteilen"], "conflict": "wer sieht hier was anders und warum", "share": "Anteil am Skript in Prozent"}}
  ],
  "callbacks": ["2-3 Motive, die später im Gespräch wieder aufgegriffen werden"],
  "closing": "das Ende: Urteil, offene Frage oder Empfehlung mit Vorbehalt — keine Zusammenfassung",
  "host_notes": {{"<Sprechername>": "wie diese Person in DIESER Folge klingt: Haltung zum Material, ein Steckenpferd, eine typische Wendung, ihr Standpunkt in den Streitpunkten"}}
}}
Plane 3 bis 5 Akte. Die Streitpunkte müssen zu den Persönlichkeiten passen; verteile Positionen so, dass nicht immer dieselbe Person recht hat.

## Material
{context}"""
    ),
}


def _hosts_block(context: "ScriptGenerationContext") -> str:
    lines = []
    for host in context.options.hosts:
        lines.append(f"- {host.name} ({'Experte' if host.role == 'expert' else 'Moderation'}): {host.personality} — Sprechstil: {host.speaking_style}")
    return "\n".join(lines) or "- (keine Sprecher konfiguriert)"


async def _run_agent(name: str, source_context: str, context: "ScriptGenerationContext", llm: LLMClient, segments: int) -> dict[str, Any]:
    system, template = AGENT_PROMPTS[name]
    user = template.format(
        context=source_context[:ANALYSIS_CONTEXT_CHARS],
        hosts=_hosts_block(context),
        format=context.options.format,
        segments=segments,
        topic=context.options.topic or "(kein Thema vorgegeben)",
    )
    response = await llm.complete(
        [LLMMessage("system", system), LLMMessage("user", user)],
        model=ANALYSIS_MODEL,
        temperature=ANALYSIS_TEMPERATURE,
        max_tokens=ANALYSIS_MAX_TOKENS,
    )
    parsed = extract_json(response)
    if not isinstance(parsed, dict):
        raise ValueError(f"Agent {name} lieferte kein JSON-Objekt")
    return parsed


async def run_analysis(source_context: str, context: "ScriptGenerationContext", llm: LLMClient, segments: int) -> dict[str, Any]:
    """Drei Agenten parallel; ein ausgefallener Agent kostet nur seinen Teil des Dossiers."""
    if ANALYSIS_AGENTS <= 0:
        return {}
    names = AGENT_ORDER[:ANALYSIS_AGENTS]
    results = await asyncio.gather(
        *[_run_agent(name, source_context, context, llm, segments) for name in names],
        return_exceptions=True,
    )
    dossier: dict[str, Any] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("Analysis agent %s failed: %s", name, result)
            dossier[name] = None
        else:
            dossier[name] = result
            logger.info("Analysis agent %s delivered %d keys", name, len(result))
    return dossier


def _items(value: Any, limit: int) -> list[Any]:
    if isinstance(value, list):
        return value[:limit]
    return []


def _text(value: Any, limit: int = 400) -> str:
    return " ".join(str(value or "").split())[:limit]


def format_dossier(dossier: dict[str, Any]) -> str:
    """Markdown-Dossier für den Skriptautor. Leere Teile werden ausgelassen."""
    if not dossier or not any(dossier.values()):
        return ""
    out: list[str] = ["## Redaktionsdossier (drei Analysen, verbindlich)"]
    facts = dossier.get("facts") or {}
    if facts:
        out.append("### Belege")
        if facts.get("thesis"):
            out.append(f"**These:** {_text(facts['thesis'])}")
        for i, a in enumerate(_items(facts.get("anchors"), 10), 1):
            if isinstance(a, dict):
                out.append(f"{i}. „{_text(a.get('quote'), 300)}“ ({_text(a.get('where'), 80)}) — {_text(a.get('why_it_matters'), 200)}")
        if facts.get("facts"):
            out.append("**Fakten:** " + " · ".join(_text(f, 160) for f in _items(facts.get("facts"), 15)))
        if facts.get("terms"):
            out.append("**Begriffe:** " + "; ".join(f"{_text(t.get('term'), 40)}: {_text(t.get('meaning'), 120)}" for t in _items(facts.get("terms"), 8) if isinstance(t, dict)))
        if facts.get("open_questions"):
            out.append("**Offene Fragen:** " + " · ".join(_text(q, 160) for q in _items(facts.get("open_questions"), 5)))
    critique = dossier.get("critique") or {}
    if critique:
        out.append("### Gegenlesart")
        for w in _items(critique.get("weaknesses"), 5):
            if isinstance(w, dict):
                out.append(f"- Schwäche: {_text(w.get('claim'), 200)} ({_text(w.get('evidence'), 80)}) — {_text(w.get('why_weak'), 200)}")
        for c in _items(critique.get("counter_readings"), 3):
            if isinstance(c, dict):
                out.append(f"- Gegenlesart: statt „{_text(c.get('surface_reading'), 150)}“ lieber „{_text(c.get('counter'), 200)}“ (bei {_text(c.get('grounded_in'), 80)})")
        for c in _items(critique.get("connections"), 4):
            if isinstance(c, dict):
                out.append(f"- Verbindung: {_text(c.get('anchor'), 120)} → {_text(c.get('outside'), 160)}: {_text(c.get('insight'), 200)}")
        for d in _items(critique.get("disputes"), 4):
            if isinstance(d, dict):
                out.append(f"- Streitpunkt „{_text(d.get('topic'), 120)}“: A) {_text(d.get('position_a'), 200)} B) {_text(d.get('position_b'), 200)}")
    drama = dossier.get("dramaturgy") or {}
    if drama:
        out.append("### Dramaturgie")
        if drama.get("hook"):
            out.append(f"**Hook (erster Satz):** {_text(drama['hook'], 300)}")
        for i, act in enumerate(_items(drama.get("acts"), 5), 1):
            if isinstance(act, dict):
                beats = " → ".join(_text(b, 120) for b in _items(act.get("beats"), 6))
                out.append(f"**Akt {i}: {_text(act.get('title'), 80)}** ({_text(act.get('share'), 10)}) — Anker: {_text(act.get('anchor'), 160)}; Schritte: {beats}; Konflikt: {_text(act.get('conflict'), 200)}")
        if drama.get("callbacks"):
            out.append("**Callbacks:** " + " · ".join(_text(c, 120) for c in _items(drama.get("callbacks"), 3)))
        if drama.get("closing"):
            out.append(f"**Schluss:** {_text(drama['closing'], 300)}")
        notes = drama.get("host_notes")
        if isinstance(notes, dict):
            for name, note in list(notes.items())[:6]:
                out.append(f"**{_text(name, 60)} in dieser Folge:** {_text(note, 300)}")
    return "\n".join(out)


REVIEW_SYSTEM = (
    "Du bist das Lektorat eines analytischen Gesprächspodcasts. Du prüfst ein fertiges Skript gegen die "
    "Redaktionsregeln und schreibst nur die schwachen Segmente neu. Du antwortest ausschließlich mit einem "
    "JSON-Objekt, ohne Text davor oder danach, ohne Codezaun. Sprache: Deutsch."
)

REVIEW_USER = """## Regeln, gegen die du prüfst
1. Keine Nacherzählung ohne Deutung: jede Handlungserwähnung trägt Interpretation, Kritik oder Verbindung.
2. Konkret statt vage: Zitate, Namen, Zahlen, Stellen. Verboten sind Füllurteile wie „spannend“, „interessant“, „faszinierend“, „wirklich gut geschrieben“, „am Ende des Tages“.
3. Die Sprecher bleiben in ihrer Persönlichkeit und ihrem Sprechstil (siehe unten) und widersprechen sich mindestens zweimal echt.
4. Segmentlängen variieren: kurze Einwürfe, mittlere Gedanken, wenige längere Ausführungen (max. 70 Wörter). Keine zwei langen Monologe hintereinander.
5. Reaktionen sind kurz und konkret: ein Tag plus höchstens drei Wörter.
6. Nonverbale Tags nur aus der erlaubten Liste, höchstens zwei je Segment, dort, wo der Laut fällt.
7. Keine englischen Einsprengsel, keine Regieanweisungen im Text, kein Wikipedia-Ton.
8. Erstes oder zweites Segment enthält den Vocarium-Label-Einwurf nach dem Hook, das letzte den Vocarium-Sign-off. Diese Stellen NICHT verändern, außer sie fehlen.

## Sprecher
{hosts}

## Dossier (Kurzfassung)
{dossier}

## Skript (Index, Sprecher, Typ, Text)
{script}

Liefere:
{{
  "score": 0-10,
  "findings": ["knappe Befunde, max. 8"],
  "rewrites": [
    {{"index": <int>, "text": "neuer Text in derselben Sprecherrolle, gleicher Typ, gleiche Position im Gespräch"}}
  ]
}}
Schreibe höchstens {max_rewrites} Segmente neu und nur, wenn es das Gespräch messbar besser macht. Behalte Sprecherzuordnung, Typ und Reihenfolge; keine neuen Segmente, keine Löschungen."""


async def review_script(
    segments: list["ScriptSegment"],
    dossier: dict[str, Any],
    context: "ScriptGenerationContext",
    llm: LLMClient,
) -> tuple[list["ScriptSegment"], dict[str, Any] | None]:
    """Lektorat: schwache Segmente werden ersetzt, der Rest bleibt unangetastet."""
    if not REVIEW_ENABLED or not segments:
        return segments, None
    from .helpers import count_words, estimate_speaking_duration  # local import: keeps module import cheap
    from .tags import enrich_tags, sanitize as sanitize_tags

    max_rewrites = max(1, int(len(segments) * REVIEW_MAX_SHARE))
    script_lines = "\n".join(f"{i}\t{s.speaker}\t{s.type}\t{s.text}" for i, s in enumerate(segments))
    short_dossier = format_dossier(dossier)[:6000]
    user = REVIEW_USER.format(hosts=_hosts_block(context), dossier=short_dossier or "(kein Dossier)", script=script_lines, max_rewrites=max_rewrites)
    try:
        response = await llm.complete(
            [LLMMessage("system", REVIEW_SYSTEM), LLMMessage("user", user)],
            model=ANALYSIS_MODEL,
            temperature=0.3,
            max_tokens=ANALYSIS_MAX_TOKENS,
        )
        verdict = extract_json(response)
    except Exception as exc:
        logger.warning("Script review skipped: %s", exc)
        return segments, None
    if not isinstance(verdict, dict):
        return segments, None
    applied = 0
    for item in _items(verdict.get("rewrites"), max_rewrites):
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        text = enrich_tags(sanitize_tags(str(item.get("text") or "").strip(), engine="omnivoice"))
        if not (0 <= index < len(segments)) or not text or text == segments[index].text:
            continue
        seg = segments[index]
        words = count_words(text)
        seg.text = text
        seg.word_count = words
        seg.estimated_duration = estimate_speaking_duration(words)
        applied += 1
    summary = {"score": verdict.get("score"), "findings": _items(verdict.get("findings"), 8), "rewrites": applied}
    logger.info("Script review: score=%s rewrites=%d", summary["score"], applied)
    return segments, summary
