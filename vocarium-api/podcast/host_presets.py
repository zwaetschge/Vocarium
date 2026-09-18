"""Katalog fertiger Podcast-Persönlichkeiten für den Host-Hub.

Ein Host besteht aus zwei Dingen, die der Skript-Generator getrennt liest:
`personality` (wer spricht) und `speaking_style` (wie geklungen wird). Beide
landen wörtlich im Prompt, deshalb sind sie hier deutsch, konkret und knapp —
Adjektivketten ohne Inhalt produzieren austauschbare Sprecher.

Die Stimmen sind Vorschläge. Welche Stimme eine Engine gerade wirklich anbietet,
weiß nur die Engine, also markiert die API jeden Eintrag mit `voice_available`
und ein Preset ohne verfügbare Stimme lässt sich trotzdem übernehmen — dann eben
ohne Stimme.
"""

from __future__ import annotations

from typing import TypedDict


class HostPreset(TypedDict):
    id: str
    name: str
    tagline: str
    category: str
    role: str
    voice: str
    gender: str
    personality: str
    speaking_style: str


CATEGORIES: list[dict[str, str]] = [
    {"id": "talk", "label": "Talk & Moderation", "description": "Führen durch die Folge, halten das Gespräch offen."},
    {"id": "wissen", "label": "Wissenschaft & Wissen", "description": "Erklären, einordnen, Quellen sortieren."},
    {"id": "crime", "label": "True Crime & Recherche", "description": "Fälle, Akten, Verfahren — nüchtern statt reißerisch."},
    {"id": "comedy", "label": "Comedy & Satire", "description": "Pointen, Tempo, kontrollierte Eskalation."},
    {"id": "business", "label": "Wirtschaft & Karriere", "description": "Zahlen, Strategien, Arbeitsalltag."},
    {"id": "kultur", "label": "Kultur & Literatur", "description": "Bücher, Filme, Musik — mit Haltung."},
    {"id": "tech", "label": "Technik & Zukunft", "description": "Von Bastelei bis Grundsatzfrage."},
    {"id": "gesellschaft", "label": "Gesellschaft & Politik", "description": "Streit, Reportage, Einordnung."},
]

CATEGORY_IDS = {c["id"] for c in CATEGORIES}


HOST_PRESETS: list[HostPreset] = [
    # ── Talk & Moderation ───────────────────────────────────────────────────
    {
        "id": "lena-brandt", "name": "Lena Brandt", "tagline": "Die warme Gastgeberin",
        "category": "talk", "role": "host", "voice": "Podcast-Female", "gender": "female",
        "personality": "Moderiert seit zehn Jahren und hat sich abgewöhnt, klüger klingen zu wollen als ihre Gäste. Holt Zuhörer am Anfang jeder Folge dort ab, wo sie stehen, und sagt offen, wenn sie etwas selbst nicht verstanden hat.",
        "speaking_style": "warm, zugewandt, kurze Sätze, viele Rückfragen, lacht leicht",
    },
    {
        "id": "tobias-krenn", "name": "Tobias Krenn", "tagline": "Der ruhige Anker",
        "category": "talk", "role": "host", "voice": "Podcast-Male", "gender": "male",
        "personality": "Bleibt ruhig, wenn das Gespräch kippt, und bringt es mit einem einzigen Satz zurück zum Thema. Hört mehr zu als er spricht, fasst dafür präzise zusammen.",
        "speaking_style": "ruhig, gleichmäßiges Tempo, sortierte Sätze, betonte Pausen",
    },
    {
        "id": "mira-sadeghi", "name": "Mira Sadeghi", "tagline": "Die neugierige Nachfragerin",
        "category": "talk", "role": "host", "voice": "Vanessa", "gender": "female",
        "personality": "Gibt sich nie mit der ersten Antwort zufrieden und fragt so lange nach, bis ein Beispiel auf dem Tisch liegt. Freundlich, aber hartnäckig.",
        "speaking_style": "neugierig, schnell reagierend, hakt nach, stellt Doppelfragen",
    },
    {
        "id": "falk-osterloh", "name": "Falk Osterloh", "tagline": "Der Late-Night-Zyniker",
        "category": "talk", "role": "host", "voice": "Simon", "gender": "male",
        "personality": "Hat schon alles gehört und sagt das auch. Sein Zynismus ist Methode: Er zwingt sein Gegenüber, das Argument wirklich zu machen, statt es nur zu behaupten.",
        "speaking_style": "trocken, lakonisch, spitze Einwürfe, betont beiläufig",
    },
    {
        "id": "juli-nowak", "name": "Juli Nowak", "tagline": "Das Energiebündel",
        "category": "talk", "role": "host", "voice": "Bluetooth-Lady", "gender": "female",
        "personality": "Redet schnell, denkt schneller und reißt Zuhörer mit. Springt gern zwischen Themen, merkt es selbst und lacht darüber.",
        "speaking_style": "schnell, begeistert, viele Einwürfe, steigende Betonung",
    },

    # ── Wissenschaft & Wissen ───────────────────────────────────────────────
    {
        "id": "hanna-vogt", "name": "Dr. Hanna Vogt", "tagline": "Die Erklärerin",
        "category": "wissen", "role": "expert", "voice": "Vanessa", "gender": "female",
        "personality": "Kann jede Studie in drei Sätzen erklären, ohne sie kaputtzuvereinfachen. Sagt dazu, wie sicher ein Befund ist, und trennt Beleg von Vermutung.",
        "speaking_style": "klar, strukturiert, erklärend, arbeitet mit Vergleichen",
    },
    {
        "id": "ernst-halder", "name": "Prof. Ernst Halder", "tagline": "Der Grundlagenforscher",
        "category": "wissen", "role": "expert", "voice": "David-Nathan", "gender": "male",
        "personality": "Vierzig Jahre Labor, entsprechend wenig beeindruckt von Schlagzeilen. Ordnet jedes neue Ergebnis in eine längere Linie ein und nennt die Gegenargumente selbst.",
        "speaking_style": "bedächtig, präzise, längere Sätze, souveräne Pausen",
    },
    {
        "id": "nils-brehm", "name": "Nils Brehm", "tagline": "Der Faktenchecker",
        "category": "wissen", "role": "expert", "voice": "Justus-Jonas", "gender": "male",
        "personality": "Prüft Behauptungen, während sie fallen, und liefert Zahl, Quelle oder Widerspruch sofort nach. Nüchtern, nie belehrend.",
        "speaking_style": "sachlich, knapp, zitiert Zahlen, korrigiert freundlich",
    },
    {
        "id": "amelie-roth", "name": "Dr. Amelie Roth", "tagline": "Die Medizinerin am Bett",
        "category": "wissen", "role": "expert", "voice": "Ygritte", "gender": "female",
        "personality": "Denkt vom Patienten her, nicht von der Statistik. Erklärt medizinische Zusammenhänge in Alltagsbildern und sagt klar, wo die Evidenz aufhört.",
        "speaking_style": "ruhig, empathisch, anschaulich, sorgfältig formuliert",
    },
    {
        "id": "kaspar-lindt", "name": "Kaspar Lindt", "tagline": "Der Wissenschaftsjournalist",
        "category": "wissen", "role": "host", "voice": "default-2", "gender": "male",
        "personality": "Übersetzt zwischen Labor und Wohnzimmer und fragt genau dort nach, wo Fachleute abkürzen. Merkt sofort, wenn eine Erklärung zu glatt ist.",
        "speaking_style": "neugierig, verständlich, fasst zwischendurch zusammen",
    },

    # ── True Crime & Recherche ──────────────────────────────────────────────
    {
        "id": "sonja-marek", "name": "Sonja Marek", "tagline": "Die Aktenleserin",
        "category": "crime", "role": "host", "voice": "Podcast-Female", "gender": "female",
        "personality": "Erzählt Fälle streng chronologisch und trennt konsequent zwischen belegt, ausgesagt und vermutet. Verweigert sich dem Reißerischen.",
        "speaking_style": "ruhig, chronologisch, sachlich, gezielt gesetzte Pausen",
    },
    {
        "id": "ruben-faist", "name": "Ruben Faist", "tagline": "Der Ermittler a. D.",
        "category": "crime", "role": "expert", "voice": "David-Nathan", "gender": "male",
        "personality": "Dreißig Jahre Kripo. Erklärt, warum Ermittlungen scheitern, ohne Kollegen vorzuführen, und kennt den Unterschied zwischen Verdacht und Beweis aus Erfahrung.",
        "speaking_style": "nüchtern, erfahren, kurze Sätze, gelegentlich Fachbegriffe mit Erklärung",
    },
    {
        "id": "cara-wendt", "name": "Cara Wendt", "tagline": "Die Gerichtsreporterin",
        "category": "crime", "role": "host", "voice": "Ygritte", "gender": "female",
        "personality": "Saß in hunderten Verhandlungen und beschreibt Menschen im Saal so genau wie den Sachverhalt. Achtet penibel auf die Unschuldsvermutung.",
        "speaking_style": "beobachtend, detailgenau, ruhiges Tempo, szenisch",
    },
    {
        "id": "milan-grote", "name": "Milan Grote", "tagline": "Der Profiler",
        "category": "crime", "role": "expert", "voice": "Simon", "gender": "male",
        "personality": "Denkt in Mustern und Wahrscheinlichkeiten, warnt aber selbst davor, aus einem Muster eine Gewissheit zu machen. Widerspricht gern der einfachen Erklärung.",
        "speaking_style": "analytisch, zurückhaltend, formuliert in Hypothesen",
    },
    {
        "id": "ines-pahl", "name": "Ines Pahl", "tagline": "Die Opferanwältin",
        "category": "crime", "role": "expert", "voice": "Vanessa", "gender": "female",
        "personality": "Bringt die Perspektive zurück, die in Fallgeschichten meist fehlt: die der Betroffenen. Wird deutlich, wenn ein Verfahren an ihnen vorbeiläuft.",
        "speaking_style": "bestimmt, empathisch, klare Haltung, wird bei Unrecht schärfer",
    },

    # ── Comedy & Satire ─────────────────────────────────────────────────────
    {
        "id": "bene-krauss", "name": "Bene Krauss", "tagline": "Der Alltagskomiker",
        "category": "comedy", "role": "host", "voice": "Michael-Scott", "gender": "male",
        "personality": "Findet den Witz in Dingen, an denen alle vorbeilaufen: Bahnansagen, Elternabende, Betreffzeilen. Erzählt gern zu lang und weiß es.",
        "speaking_style": "erzählend, selbstironisch, baut auf Pointen hin, lacht mit",
    },
    {
        "id": "pia-lorenz", "name": "Pia Lorenz", "tagline": "Die trockene Pointe",
        "category": "comedy", "role": "host", "voice": "Bluetooth-Lady", "gender": "female",
        "personality": "Sagt den einen Satz, nach dem alle kurz still sind und dann lachen. Braucht keinen Anlauf und nimmt nie eine Pointe zurück.",
        "speaking_style": "trocken, knapp, perfektes Timing, betont beiläufig",
    },
    {
        "id": "ollie-schmitt", "name": "Ollie Schmitt", "tagline": "Der Chaos-Sidekick",
        "category": "comedy", "role": "host", "voice": "Homer-v2", "gender": "male",
        "personality": "Verliert regelmäßig den Faden und findet dabei zufällig den besten Gedanken der Folge. Unterbricht sich selbst öfter als andere.",
        "speaking_style": "sprunghaft, laut, Selbstunterbrechungen, spontane Ausrufe",
    },
    {
        "id": "marek-uhl", "name": "Marek Uhl", "tagline": "Der Kabarettist",
        "category": "comedy", "role": "expert", "voice": "Marc-Uwe-Kling", "gender": "male",
        "personality": "Verpackt scharfe Kritik in Anekdoten und kommt so weiter als jede direkte Anklage. Baut die Pointe geduldig auf.",
        "speaking_style": "erzählend, pointiert, ironisch, wechselt Tempo bewusst",
    },
    {
        "id": "toni-beck", "name": "Toni Beck", "tagline": "Der Improvisierer",
        "category": "comedy", "role": "host", "voice": "Son-Goku-Kind", "gender": "male",
        "personality": "Nimmt jeden Einwurf an und spinnt ihn weiter, bis die Idee zusammenbricht. Kennt keine Scheu vor der schlechten Idee.",
        "speaking_style": "aufgedreht, schnell, spielt Szenen an, überdreht gern",
    },

    # ── Wirtschaft & Karriere ───────────────────────────────────────────────
    {
        "id": "katharina-moeller", "name": "Katharina Möller", "tagline": "Die Analystin",
        "category": "business", "role": "expert", "voice": "Vanessa", "gender": "female",
        "personality": "Liest Bilanzen wie andere Zeitung und erkennt die Geschichte hinter der Kennzahl. Sagt klar, wenn ein Geschäftsmodell nicht trägt.",
        "speaking_style": "präzise, faktenorientiert, nennt Zahlen, argumentiert schrittweise",
    },
    {
        "id": "jonas-feldkamp", "name": "Jonas Feldkamp", "tagline": "Der Gründer",
        "category": "business", "role": "host", "voice": "Podcast-Male", "gender": "male",
        "personality": "Hat zwei Firmen aufgebaut und eine an die Wand gefahren, und redet über beides gleich offen. Ungeduldig mit Theorie ohne Praxis.",
        "speaking_style": "direkt, energisch, konkrete Beispiele, ungeduldig bei Floskeln",
    },
    {
        "id": "rita-sanders", "name": "Rita Sanders", "tagline": "Die Verhandlerin",
        "category": "business", "role": "expert", "voice": "Bluetooth-Lady", "gender": "female",
        "personality": "Denkt in Interessen statt in Positionen und zerlegt jeden Konflikt in die Frage, wer eigentlich was braucht. Ruhig auch im Streit.",
        "speaking_style": "ruhig, strategisch, stellt Gegenfragen, formuliert Optionen",
    },
    {
        "id": "sven-achterberg", "name": "Sven Achterberg", "tagline": "Der Zahlenmensch",
        "category": "business", "role": "expert", "voice": "Simon", "gender": "male",
        "personality": "Traut keiner Aussage ohne Größenordnung und rechnet Behauptungen im Gespräch nach. Trocken, aber nie herablassend.",
        "speaking_style": "nüchtern, rechnet laut, benennt Annahmen, korrigiert Größenordnungen",
    },
    {
        "id": "nadja-ilic", "name": "Nadja Ilić", "tagline": "Die Karriere-Coachin",
        "category": "business", "role": "host", "voice": "Ygritte", "gender": "female",
        "personality": "Hört den Satz hinter dem Satz und stellt die unbequeme Frage freundlich. Interessiert sich mehr für Entscheidungen als für Lebensläufe.",
        "speaking_style": "zugewandt, fragend, spiegelt Gesagtes, ermutigend",
    },

    # ── Kultur & Literatur ──────────────────────────────────────────────────
    {
        "id": "elisabeth-kray", "name": "Elisabeth Kray", "tagline": "Die Literaturkritikerin",
        "category": "kultur", "role": "expert", "voice": "Vanessa", "gender": "female",
        "personality": "Belegt jedes Urteil am Text und zitiert die Stelle, die ihre These trägt. Lobt selten, dann aber ernsthaft.",
        "speaking_style": "eloquent, urteilsfreudig, zitiert wörtlich, präzise Begriffe",
    },
    {
        "id": "anton-rieger", "name": "Anton Rieger", "tagline": "Der Vorleser",
        "category": "kultur", "role": "host", "voice": "Rufus-Beck", "gender": "male",
        "personality": "Kann eine Handlung nacherzählen, ohne die Spannung zu zerstören, und liest Passagen so vor, dass man das Buch danach will.",
        "speaking_style": "erzählend, moduliert, nimmt sich Zeit, wechselt Tonlagen",
    },
    {
        "id": "fides-norrmann", "name": "Fides Norrmann", "tagline": "Die Kulturjournalistin",
        "category": "kultur", "role": "host", "voice": "Podcast-Female", "gender": "female",
        "personality": "Verbindet das Werk mit der Woche, in der es erscheint, und fragt immer, warum ausgerechnet jetzt. Kennt den Betrieb und seine Eitelkeiten.",
        "speaking_style": "wach, einordnend, stellt Kontextfragen, flüssiges Tempo",
    },
    {
        "id": "gregor-weiss", "name": "Gregor Weiß", "tagline": "Der Filmkenner",
        "category": "kultur", "role": "expert", "voice": "David-Nathan", "gender": "male",
        "personality": "Beschreibt Einstellungen so genau, dass man sie sieht, und erklärt Wirkung aus Handwerk statt aus Bauchgefühl.",
        "speaking_style": "bildhaft, detailverliebt, ruhig, schwärmt kontrolliert",
    },

    {
        "id": "der-erzaehler", "name": "Der Erzähler", "tagline": "Die Stimme aus dem Off",
        "category": "kultur", "role": "expert", "voice": "Dragonball-Erzaehler", "gender": "male",
        "personality": "Führt durch eine Geschichte, ohne selbst Teil davon zu sein. Baut Spannung über Rhythmus auf, nicht über Lautstärke.",
        "speaking_style": "dramatisch, getragen, große Pausen, deutliche Betonung",
    },
    # ── Technik & Zukunft ───────────────────────────────────────────────────
    {
        "id": "timo-sprenger", "name": "Timo Sprenger", "tagline": "Der Praktiker",
        "category": "tech", "role": "host", "voice": "Podcast-Male", "gender": "male",
        "personality": "Fragt bei jeder Technologie zuerst, was sie im Alltag ändert, und lässt sich von Ankündigungen nicht beeindrucken.",
        "speaking_style": "bodenständig, konkret, fragt nach Alltagsbeispielen",
    },
    {
        "id": "yara-kern", "name": "Dr. Yara Kern", "tagline": "Die KI-Forscherin",
        "category": "tech", "role": "expert", "voice": "Ygritte", "gender": "female",
        "personality": "Arbeitet an den Modellen, über die andere spekulieren, und unterscheidet sauber zwischen dem, was geht, und dem, was verkauft wird.",
        "speaking_style": "sachlich, differenziert, benennt Unsicherheiten, ruhig",
    },
    {
        "id": "momo-steinbach", "name": "Momo Steinbach", "tagline": "Der Nerd im Maschinenraum",
        "category": "tech", "role": "expert", "voice": "Maurice-Moss", "gender": "male",
        "personality": "Erklärt genau die Ebene, die alle überspringen, und wird lebhaft, sobald es um Details geht. Merkt manchmal spät, dass er zu tief ist.",
        "speaking_style": "detailversessen, schnell, technisch, holt sich selbst zurück",
    },
    {
        "id": "lasse-ohm", "name": "Lasse Ohm", "tagline": "Der Hardware-Bastler",
        "category": "tech", "role": "host", "voice": "Valentin", "gender": "male",
        "personality": "Hat alles, worüber er spricht, selbst auseinandergenommen. Erzählt am liebsten von dem Versuch, der schiefging.",
        "speaking_style": "locker, praktisch, erzählt aus Erfahrung, selbstironisch",
    },
    {
        "id": "ronja-kastl", "name": "Ronja Kastl", "tagline": "Die Datenschützerin",
        "category": "tech", "role": "expert", "voice": "Bluetooth-Lady", "gender": "female",
        "personality": "Fragt bei jedem Feature zuerst, welche Daten es kostet, und kennt die Antwort meist schon. Unbequem, aber nie alarmistisch.",
        "speaking_style": "pointiert, kritisch, klare Beispiele, hartnäckig",
    },

    # ── Gesellschaft & Politik ──────────────────────────────────────────────
    {
        "id": "halima-sarr", "name": "Halima Sarr", "tagline": "Die Reporterin vor Ort",
        "category": "gesellschaft", "role": "host", "voice": "Vanessa", "gender": "female",
        "personality": "War da, wo das Thema stattfindet, und erzählt es über Menschen statt über Begriffe. Bringt Szenen mit, keine Statements.",
        "speaking_style": "szenisch, lebendig, erzählt Beobachtungen, direkte Rede",
    },
    {
        "id": "bernd-kuhlmann", "name": "Bernd Kuhlmann", "tagline": "Der Hauptstadtkorrespondent",
        "category": "gesellschaft", "role": "expert", "voice": "Podcast-Male", "gender": "male",
        "personality": "Kennt Verfahren und Personen und erklärt, warum eine Entscheidung so und nicht anders fällt. Distanziert zu allen Seiten.",
        "speaking_style": "routiniert, einordnend, nennt Abläufe, ruhig",
    },
    {
        "id": "theresa-aigner", "name": "Theresa Aigner", "tagline": "Die Streitbare",
        "category": "gesellschaft", "role": "expert", "voice": "Ygritte", "gender": "female",
        "personality": "Sucht den Widerspruch, weil sich erst darin zeigt, ob ein Argument trägt. Bleibt beim Thema, auch wenn es unangenehm wird.",
        "speaking_style": "scharf, argumentativ, widerspricht direkt, klare Betonung",
    },
    {
        "id": "ilja-wolter", "name": "Ilja Wolter", "tagline": "Der Erklärbär",
        "category": "gesellschaft", "role": "host", "voice": "default-1", "gender": "male",
        "personality": "Nimmt komplizierte Verfahren auseinander, bis jeder Schritt einzeln verständlich ist. Wiederholt lieber einmal zu viel.",
        "speaking_style": "geduldig, gliedernd, arbeitet mit Aufzählungen, ruhiges Tempo",
    },
    {
        "id": "marlene-voss", "name": "Marlene Voss", "tagline": "Die Zuhörerin",
        "category": "gesellschaft", "role": "host", "voice": "Podcast-Female", "gender": "female",
        "personality": "Lässt Menschen ausreden, auch wenn es dauert, und stellt danach die eine Frage, die alles zusammenhält.",
        "speaking_style": "zurückhaltend, aufmerksam, lange Pausen, sehr präzise Fragen",
    },
]

PRESETS_BY_ID: dict[str, HostPreset] = {p["id"]: p for p in HOST_PRESETS}


def _validate() -> None:
    """Fehler im Katalog sollen beim Import auffallen, nicht beim Klick."""
    if len(PRESETS_BY_ID) != len(HOST_PRESETS):
        raise ValueError("host preset ids are not unique")
    for preset in HOST_PRESETS:
        if preset["category"] not in CATEGORY_IDS:
            raise ValueError(f"unknown category {preset['category']!r} in {preset['id']!r}")
        if preset["role"] not in {"host", "expert"}:
            raise ValueError(f"unknown role {preset['role']!r} in {preset['id']!r}")


_validate()
