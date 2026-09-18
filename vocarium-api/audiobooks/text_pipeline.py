"""Document parsing and segmentation for the Audiobooks area.

A faithful port of Canto's text pipeline (rebuild spec §5): paragraph-aware
chunking into 50–800-character sentence groups, format parsers for EPUB, PDF,
DOCX and TXT. Segmentation is persisted at import time and the chunker version
is part of the audio cache path — the spec's hardest-won lesson is that silent
re-chunking invalidates every cached segment.
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from dataclasses import dataclass

# Bump whenever chunker or parser output can change for the same input.
# Die Version wird pro Buch beim Import festgeschrieben (ab_books.chunker_version)
# und ist Teil des Audio-Cache-Pfads — Alt-Bücher behalten Segmentierung UND Cache.
# v2 (2026-08-29): CRLF-Normalisierung, Separator-Zeilen entfernt,
# erweiterte Kapitelerkennung, heading-Flag für Überschriften-Absätze.
CHUNKER_VERSION = "v2"

MIN_SEGMENT_LENGTH = 50
MAX_SEGMENT_LENGTH = 800

# Spec §5.1 — the multi-part entries can never match single words; kept anyway
# so behaviour matches the original.
GERMAN_ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "nr", "str", "bzw", "usw",
    "etc", "ca", "evtl", "ggf", "inkl", "max", "min", "sog", "vgl",
    "z.b", "u.a", "d.h", "o.ä", "z.t",
}

# Kapitelköpfe: "KAPITEL 3", "Kapitel drei:", "TEIL II", "ERSTER TEIL",
# "PROLOG", "EPILOG", "1. Der Anfang" — jeweils als eigene Zeile.
_ORDINAL = r"(?:ERSTER|ZWEITER|DRITTER|VIERTER|FÜNFTER|SECHSTER|SIEBTER|ACHTER|NEUNTER|ZEHNTER|Erster|Zweiter|Dritter|Vierter|Fünfter|Sechster|Siebter|Achter|Neunter|Zehnter)"
CHAPTER_PATTERN = re.compile(
    r"^[ \t]*(?:"
    r"(?:KAPITEL|Kapitel|CHAPTER|Chapter|TEIL|Teil|PART|Part|BUCH|Buch)[ \t]+(?:\d+|[IVXLC]+)[.:]?[ \t]*.*"
    rf"|{_ORDINAL}[ \t]+(?:TEIL|Teil|BUCH|Buch|KAPITEL|Kapitel)"
    r"|(?:PROLOG|Prolog|EPILOG|Epilog|VORWORT|Vorwort|NACHWORT|Nachwort)[ \t]*.{0,60}"
    r"|(?:\d+\.)[ \t]+.{3,80}"
    r")[ \t]*$",
    re.MULTILINE,
)

# Rein dekorative Zeilen (=====, -----, ***, ~~~) — raus aus dem Hörtext.
# MULTILINE ist Pflicht: ohne matcht ^…$ nur den Gesamtstring, nie Zeilen.
SEPARATOR_LINE = re.compile(r"^[ \t]*[=\-_*~#·•─-╿]{3,}[ \t]*$", re.MULTILINE)


def normalize_text(text: str) -> str:
    """CRLF/CR → LF und Trennlinien entfernen. MUSS vor jeder Kapitel- und
    Segmentlogik laufen: Windows-Zeilenenden lassen sonst jedes ``$``-Pattern
    ins Leere laufen (Kapitel wurden nie erkannt)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = SEPARATOR_LINE.sub("", text)
    return text
SENTENCE_END = re.compile(r"[.!?…][\"»']?\s*$")
UPPER_START = re.compile(r"^[A-ZÄÖÜ„\"«]")


@dataclass
class Segment:
    index: int
    text: str
    chapter_index: int
    paragraph_break: bool
    heading: bool = False

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "text": self.text,
            "chapterIndex": self.chapter_index,
            "paragraphBreak": self.paragraph_break,
            "heading": self.heading,
        }


@dataclass
class Chapter:
    title: str
    content: str


@dataclass
class ParsedDocument:
    title: str
    author: str
    chapters: list[Chapter]


# ── Chunker (spec §5.1) ──────────────────────────────────────────────────────

def _split_sentences(paragraph: str) -> list[str]:
    words = paragraph.split()
    sentences: list[str] = []
    current: list[str] = []
    for i, word in enumerate(words):
        current.append(word)
        if not SENTENCE_END.search(word):
            continue
        bare = word.rstrip('.!?…"»\'').casefold()
        if bare in GERMAN_ABBREVIATIONS or len(bare) == 1:
            continue
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is not None and not UPPER_START.match(nxt):
            continue
        sentences.append(" ".join(current))
        current = []
    if current:
        sentences.append(" ".join(current))
    return sentences


def _split_long_segment(text: str) -> list[str]:
    parts: list[str] = []
    rest = text
    while len(rest) > MAX_SEGMENT_LENGTH:
        cut = -1
        for match in re.finditer(r"[,;]", rest[:MAX_SEGMENT_LENGTH]):
            if match.end() >= MIN_SEGMENT_LENGTH:
                cut = match.end()
        if cut < 0:
            cut = rest.rfind(" ", MIN_SEGMENT_LENGTH, MAX_SEGMENT_LENGTH)
        if cut < 0:
            cut = MAX_SEGMENT_LENGTH
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return [p for p in parts if p]


def _merge_and_split(sentences: list[str]) -> list[str]:
    out: list[str] = []
    buffer = ""
    for sentence in sentences:
        candidate = f"{buffer} {sentence}".strip() if buffer else sentence
        if len(candidate) > MAX_SEGMENT_LENGTH:
            if buffer:
                out.append(buffer)
            if len(sentence) > MAX_SEGMENT_LENGTH:
                out.extend(_split_long_segment(sentence))
                buffer = ""
            else:
                buffer = sentence
        elif len(candidate) >= MIN_SEGMENT_LENGTH:
            out.append(candidate)
            buffer = ""
        else:
            buffer = candidate
    if buffer:
        if out and len(buffer) < MIN_SEGMENT_LENGTH:
            out[-1] = f"{out[-1]} {buffer}"
        else:
            out.append(buffer)
    return out


def _is_heading(paragraph: str) -> bool:
    """Überschriften-Absatz: kurz, ohne Satzende — oder ein Kapitelkopf."""
    flat = re.sub(r"\s+", " ", paragraph).strip()
    if not flat or len(flat) > 90:
        return False
    if CHAPTER_PATTERN.match(flat):
        return True
    if re.search(r"[.!?…;,]$", flat):
        return False
    # Kurze Zeile ohne Satzzeichen, überwiegend Großbuchstaben oder Titelform
    letters = [c for c in flat if c.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    return len(flat) <= 70 and (upper_ratio > 0.7 or len(flat.split()) <= 6)


def chunk_text(text: str, chapter_index: int) -> list[Segment]:
    segments: list[Segment] = []
    text = normalize_text(text)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    prev_flat = ""
    for pi, paragraph in enumerate(paragraphs):
        flat = re.sub(r"\s+", " ", paragraph).strip()
        # Ohne Buchstaben/Ziffern (übrige Deko) oder exakte Wiederholung des
        # Vorgänger-Absatzes (EPUB-Titelseiten doppeln den Titel) → weglassen.
        if not re.search(r"[\wÄÖÜäöü]", flat) or flat == prev_flat:
            continue
        prev_flat = flat
        heading = _is_heading(paragraph)
        if heading:
            segments.append(Segment(
                index=len(segments), text=flat, chapter_index=chapter_index,
                paragraph_break=(pi > 0), heading=True,
            ))
            continue
        for si, chunk in enumerate(_merge_and_split(_split_sentences(flat))):
            segments.append(Segment(
                index=len(segments),
                text=chunk,
                chapter_index=chapter_index,
                paragraph_break=(si == 0 and pi > 0),
            ))
    return segments


def split_chapters_txt(text: str) -> list[Chapter]:
    text = normalize_text(text)
    matches = list(CHAPTER_PATTERN.finditer(text))
    if len(matches) >= 2:
        chapters = []
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[match.end():end].strip()
            title = match.group(0).strip()
            # Nackter Kapitelkopf ("KAPITEL 3") + Titel auf der Folgezeile →
            # zusammenziehen: "KAPITEL 3 — Die Nacht, in der …"
            if re.fullmatch(r"(?:KAPITEL|Kapitel|CHAPTER|Chapter|TEIL|Teil|BUCH|Buch)\s+(?:\d+|[IVXLC]+)[.:]?", title):
                first_lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
                if first_lines and len(first_lines[0]) <= 80 and not re.search(r"[.!?…]$", first_lines[0]):
                    title = f"{title} — {first_lines[0]}"
            if body:
                chapters.append(Chapter(title=title[:120], content=body))
        if len(chapters) >= 2:
            return chapters
    return [Chapter(title="Kapitel 1", content=text)]


# ── Parsers ──────────────────────────────────────────────────────────────────

def _strip_html(markup: str) -> str:
    markup = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.DOTALL | re.IGNORECASE)
    # Block ends become paragraph breaks — load-bearing for the chunker.
    markup = re.sub(r"</(p|div|h[1-6])>", "\n\n", markup, flags=re.IGNORECASE)
    markup = re.sub(r"<br\s*/?>", "\n", markup, flags=re.IGNORECASE)
    markup = re.sub(r"<[^>]+>", " ", markup)
    text = html.unescape(markup)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_epub(data: bytes) -> ParsedDocument:
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    if len(names) > 5000:
        raise ValueError("EPUB has too many entries")
    if sum(i.file_size for i in zf.infolist()) > 500 * 1024 * 1024:
        raise ValueError("EPUB decompresses beyond 500 MB")

    title, author = "", ""
    spine_files: list[str] = []
    opf_names = [n for n in names if n.endswith(".opf")]
    if opf_names:
        opf = zf.read(opf_names[0]).decode("utf-8", "replace")
        m = re.search(r"<dc:title[^>]*>([^<]+)</dc:title>", opf)
        title = html.unescape(m.group(1).strip()) if m else ""
        m = re.search(r"<dc:creator[^>]*>([^<]+)</dc:creator>", opf)
        author = html.unescape(m.group(1).strip()) if m else ""
        base = opf_names[0].rsplit("/", 1)[0] + "/" if "/" in opf_names[0] else ""
        manifest = dict(re.findall(r'<item[^>]*?id="([^"]+)"[^>]*?href="([^"]+)"', opf))
        manifest.update({i: h for h, i in re.findall(r'<item[^>]*?href="([^"]+)"[^>]*?id="([^"]+)"', opf)})
        for idref in re.findall(r'<itemref[^>]*?idref="([^"]+)"', opf):
            href = manifest.get(idref)
            if href:
                candidate = base + href
                if candidate in names:
                    spine_files.append(candidate)
    if not spine_files:
        spine_files = sorted(n for n in names if re.search(r"\.(xhtml|html|htm)$", n, re.IGNORECASE))

    chapters: list[Chapter] = []
    for name in spine_files:
        try:
            markup = zf.read(name).decode("utf-8", "replace")
        except KeyError:
            continue
        heading = re.search(r"<h[1-3][^>]*>(.*?)</h[1-3]>", markup, re.DOTALL | re.IGNORECASE)
        text = _strip_html(markup)
        if len(text) <= 20:
            continue
        chapter_title = _strip_html(heading.group(1))[:120] if heading else f"Kapitel {len(chapters) + 1}"
        chapters.append(Chapter(title=chapter_title or f"Kapitel {len(chapters) + 1}", content=text))
    if not chapters:
        raise ValueError("EPUB contains no readable chapters")
    return ParsedDocument(title=title, author=author, chapters=chapters)


def parse_pdf(data: bytes) -> ParsedDocument:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    text = normalize_text(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError("PDF contains no extractable text")

    matches = list(CHAPTER_PATTERN.finditer(text))
    if len(matches) >= 2:
        chapters = split_chapters_txt(text)
    elif len(text) > 10_000:
        # Spec §5.4: mechanical 5000-char blocks, cut back to the last blank line.
        chapters = []
        i = 0
        while i < len(text):
            end = min(i + 5000, len(text))
            if end < len(text):
                back = text.rfind("\n\n", i + 2500, end)
                if back > 0:
                    end = back
            chapters.append(Chapter(title=f"Abschnitt {len(chapters) + 1}", content=text[i:end].strip()))
            i = end
    else:
        chapters = [Chapter(title="Dokument", content=text)]
    return ParsedDocument(title="", author="", chapters=[c for c in chapters if c.content])


def parse_docx(data: bytes) -> ParsedDocument:
    zf = zipfile.ZipFile(io.BytesIO(data))
    xml = zf.read("word/document.xml").decode("utf-8", "replace")
    paragraphs = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.DOTALL):
        runs = re.findall(r"<w:t[^>]*>([^<]*)</w:t>", para)
        line = html.unescape("".join(runs)).strip()
        if line:
            heading = bool(re.search(r'<w:pStyle[^>]*w:val="Heading[1-3]"', para))
            paragraphs.append(("#" if heading else "") + line)
    if not paragraphs:
        raise ValueError("DOCX contains no text")

    chapters: list[Chapter] = []
    current_title = "Dokument"
    current: list[str] = []
    for line in paragraphs:
        if line.startswith("#"):
            if current:
                chapters.append(Chapter(title=current_title[:120], content="\n\n".join(current)))
                current = []
            current_title = line[1:]
        else:
            current.append(line)
    if current:
        chapters.append(Chapter(title=current_title[:120], content="\n\n".join(current)))
    if len(chapters) < 2:
        text = "\n\n".join(p.lstrip("#") for p in paragraphs)
        chapters = split_chapters_txt(text)
    return ParsedDocument(title="", author="", chapters=chapters)


def extract_epub_cover(data: bytes) -> tuple[bytes, str] | None:
    """Best-effort cover image from an EPUB (spec: max 10 MB, png/jpg).

    Priority: OPF ``meta name="cover"`` item → manifest item with
    ``properties="cover-image"`` → any image whose name contains "cover".
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = zf.namelist()
        opf_names = [n for n in names if n.endswith(".opf")]
        candidates: list[str] = []
        if opf_names:
            opf = zf.read(opf_names[0]).decode("utf-8", "replace")
            base = opf_names[0].rsplit("/", 1)[0] + "/" if "/" in opf_names[0] else ""
            m = re.search(r'<meta[^>]*name="cover"[^>]*content="([^"]+)"', opf)
            if m:
                cid = re.escape(m.group(1))
                item = re.search(rf'<item[^>]*id="{cid}"[^>]*href="([^"]+)"', opf) or \
                    re.search(rf'<item[^>]*href="([^"]+)"[^>]*id="{cid}"', opf)
                if item:
                    candidates.append(base + item.group(1))
            for href in re.findall(r'<item[^>]*properties="[^"]*cover-image[^"]*"[^>]*href="([^"]+)"', opf):
                candidates.append(base + href)
            for href in re.findall(r'<item[^>]*href="([^"]+)"[^>]*properties="[^"]*cover-image[^"]*"', opf):
                candidates.append(base + href)
        candidates += [n for n in names if "cover" in n.lower() and re.search(r"\.(jpe?g|png)$", n, re.IGNORECASE)]
        for name in candidates:
            name = html.unescape(name)
            if name not in names:
                continue
            blob = zf.read(name)
            if not blob or len(blob) > 10 * 1024 * 1024:
                continue
            ext = "png" if name.lower().endswith(".png") else "jpg"
            return blob, ext
    except Exception:
        pass
    return None


PARSERS = {
    "epub": parse_epub,
    "pdf": parse_pdf,
    "docx": parse_docx,
    "txt": lambda data: ParsedDocument(
        title="", author="",
        chapters=split_chapters_txt(data.decode("utf-8", "replace")),
    ),
}

MAX_UPLOAD_BYTES = {"pdf": 200, "epub": 500, "docx": 200, "txt": 50}


def parse_document(data: bytes, fmt: str) -> ParsedDocument:
    if fmt not in PARSERS:
        raise ValueError(f"Unsupported format {fmt!r}")
    limit = MAX_UPLOAD_BYTES[fmt] * 1024 * 1024
    if len(data) > limit:
        raise ValueError(f"{fmt.upper()} exceeds {MAX_UPLOAD_BYTES[fmt]} MB")
    return PARSERS[fmt](data)
