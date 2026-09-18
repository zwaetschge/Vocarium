"""Nonverbale OmniVoice-Tags.

OmniVoice kennt eine kleine Menge eckig geklammerter Symbole, die es als
echte Lautäußerung rendert statt sie vorzulesen — ``[laughter]`` wird zu
einem Lacher, ``[sigh]`` zu einem Seufzer. Gemessen am 2026-08-29 gegen
die laufende Instanz: der Text bleibt in der Transkription unverändert,
die Audiodauer wächst um die Länge der Vokalisation.

Kikiri (Kokoro/StyleTTS2) kennt die Symbole nicht und würde sie
buchstabieren, deshalb entfernt :func:`sanitize` sie dort.
"""

from __future__ import annotations

import re

# id → (deutsches Label, kurze Beschreibung fürs UI). Reihenfolge ist die
# Anzeigereihenfolge in der Tag-Palette.
TAG_CATALOG: list[tuple[str, str, str]] = [
    ("laughter", "Lachen", "Kurzer Lacher mitten im Satz"),
    ("sigh", "Seufzen", "Ausatmen, resigniert oder erleichtert"),
    ("confirmation-en", "Bestätigung", "Zustimmendes „mhm“"),
    ("question-en", "Nachfrage", "Fragendes „hm?“"),
    ("question-ah", "Nachfrage „ah?“", "Fragend, offener Vokal"),
    ("question-oh", "Nachfrage „oh?“", "Fragend, gerundet"),
    ("question-ei", "Nachfrage „ei?“", "Fragend, hell"),
    ("question-yi", "Nachfrage „yi?“", "Fragend, sehr hell"),
    ("surprise-ah", "Überraschung „ah!“", "Erstaunt"),
    ("surprise-oh", "Überraschung „oh!“", "Erstaunt, gerundet"),
    ("surprise-wa", "Überraschung „wa!“", "Perplex"),
    ("surprise-yo", "Überraschung „yo!“", "Ausruf"),
    ("dissatisfaction-hnn", "Unmut", "Genervtes „hnn“"),
]

ALLOWED_TAGS: frozenset[str] = frozenset(tag for tag, _, _ in TAG_CATALOG)

# Nur einzelne Wörter in Klammern gelten als Tag-Kandidat. „[Kapitel 4]“ oder
# „[siehe S. 12]“ sind Fließtext und bleiben unangetastet.
_TAG_RE = re.compile(r"\[([A-Za-z][A-Za-z-]{1,23})\]")
_WS_RE = re.compile(r"[ \t]{2,}")


def catalog() -> list[dict[str, str]]:
    """Tag-Katalog als JSON-freundliche Liste für das WebUI."""
    return [{"id": t, "label": label, "hint": hint} for t, label, hint in TAG_CATALOG]


def _drop(text: str, predicate) -> str:
    cleaned = _TAG_RE.sub(lambda m: "" if predicate(m.group(1).lower()) else m.group(0), text)
    return _WS_RE.sub(" ", cleaned).strip()


def strip_tags(text: str) -> str:
    """Alle Tag-Kandidaten entfernen — für Wortzählung und Nicht-OmniVoice."""
    return _drop(text, lambda _tag: True)


def sanitize(text: str, *, engine: str) -> str:
    """Text so aufbereiten, wie ihn die jeweilige Engine sehen darf.

    OmniVoice behält die bekannten Tags und verliert erfundene (die es
    sonst vorlesen würde); jede andere Engine bekommt reinen Text.
    """
    if engine != "omnivoice":
        return strip_tags(text)
    return _drop(text, lambda tag: tag not in ALLOWED_TAGS)


def used_tags(text: str) -> list[str]:
    """Bekannte Tags in Reihenfolge ihres Auftretens, ohne Duplikate."""
    seen: list[str] = []
    for match in _TAG_RE.finditer(text):
        tag = match.group(1).lower()
        if tag in ALLOWED_TAGS and tag not in seen:
            seen.append(tag)
    return seen


def word_count(text: str) -> int:
    """Wörter ohne Tags — ein ``[laughter]`` ist kein gesprochenes Wort."""
    return len(strip_tags(text).split())


# Interjektionen, die das LLM ausschreibt, obwohl OmniVoice dafuer einen
# echten Laut hat. Wird ein Segment (oder ein Satz darin) mit so einem Wort
# eroeffnet, ersetzt das Tag das Wort -- gesprochen klingt "[laughter]" wie
# Lachen, "Haha" dagegen wie ein vorgelesenes Wort.
_INTERJECTION_TAGS: list[tuple[str, str]] = [
    (r"h[ae]h[ae](?:h[ae])*", "laughter"),
    (r"hihi(?:hi)*", "laughter"),
    (r"lol", "laughter"),
    (r"m+h*m+", "confirmation-en"),
    (r"h+m+\??", "question-en"),
    (r"seufz|puh+|pff+|uff+", "sigh"),
    (r"wow|boah|krass", "surprise-wa"),
    (r"oh+", "surprise-oh"),
    (r"ah+|aha", "surprise-ah"),
    (r"hnn+|tss+|tz+|grr+", "dissatisfaction-hnn"),
]
_INTERJECTION_RE = __import__("re").compile(
    r"(?:(?<=^)|(?<=[.!?…]\s))(?P<word>" + "|".join(f"(?:{pat})" for pat, _ in _INTERJECTION_TAGS) + r")(?P<punct>[,!.?…]*)(?=\s|$)",
    __import__("re").IGNORECASE,
)


def enrich_tags(text: str) -> str:
    """Ersetzt ausgeschriebene Laute am Satzanfang durch OmniVoice-Tags."""
    import re

    def swap(match: "re.Match[str]") -> str:
        word = match.group("word")
        for pattern, tag in _INTERJECTION_TAGS:
            if re.fullmatch(pattern, word, re.IGNORECASE):
                return f"[{tag}]"
        return word

    result = _INTERJECTION_RE.sub(swap, text or "")
    return re.sub(r"\s{2,}", " ", result).strip()
