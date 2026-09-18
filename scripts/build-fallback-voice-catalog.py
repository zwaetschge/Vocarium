#!/usr/bin/env python3
"""Turn a raw speaker sweep into the curated fallback voice catalog.

`de_DE-mls-medium` ships 236 speakers, most of them unremarkable and some of
them plainly bad. Shipping all of them would make the fallback picker useless,
so this script ranks them on three signals measured from real renders
(analyze-piper-mls-speakers.py) and keeps a gender-balanced, pitch-spread
selection:

* **voiced fraction** — the strongest quality signal. A speaker that phonates
  cleanly scores high; breathy, buzzy or clipped ones drop.
* **duration** — the probe sentence is fixed, so duration is tempo. Speakers far
  from the median either race or drag.
* **RMS** — after peak normalisation this reads as dynamic range; very quiet or
  very hot speakers sound compressed.

Pitch spread matters as much as raw score: twelve excellent speakers that all
sit at 190 Hz are one voice, not twelve.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

# Curated German first names. Order is the assignment order within a gender,
# lowest pitch first, so "Konrad" ends up the deepest male and "Lilly" the
# brightest female — the picker then reads top-to-bottom as dark-to-bright.
MALE_NAMES = [
    "Konrad", "Bernhard", "Ludwig", "Hartmut", "Gregor", "Matthias", "Roland",
    "Stefan", "Andreas", "Tobias", "Jonas", "Fabian", "Nico", "Lennard",
]
FEMALE_NAMES = [
    "Helena", "Roswitha", "Katharina", "Ingrid", "Barbara", "Sabine", "Antje",
    "Miriam", "Franziska", "Johanna", "Nele", "Carla", "Josie", "Lilly",
]

FEMALE_MIN_F0 = 168.0
MALE_MAX_F0 = 152.0


def score(row: dict, median_duration: float) -> float:
    voiced = row["voiced"]
    tempo_penalty = abs(row["duration"] - median_duration) / median_duration
    level_penalty = abs(row["rms"] - 0.145) / 0.145
    return voiced - 0.35 * tempo_penalty - 0.20 * level_penalty


def pick(rows: list[dict], names: list[str], median_duration: float) -> list[tuple[dict, str]]:
    """Take the best speakers while keeping the pitch range covered.

    Splitting into as many pitch buckets as there are names, then taking the
    best of each, beats a flat top-N: a flat ranking clusters, and a bank of
    near-identical voices is a bank of one.
    """
    if not rows:
        return []
    ranked = sorted(rows, key=lambda r: score(r, median_duration), reverse=True)
    lo = min(r["f0"] for r in rows)
    hi = max(r["f0"] for r in rows)
    span = max(1e-6, hi - lo)
    buckets: dict[int, list[dict]] = {}
    for row in ranked:
        index = min(len(names) - 1, int((row["f0"] - lo) / span * len(names)))
        buckets.setdefault(index, []).append(row)

    chosen: list[dict] = []
    used: set[int] = set()
    for index in range(len(names)):
        for candidate in buckets.get(index, []):
            if candidate["speaker_id"] not in used:
                chosen.append(candidate)
                used.add(candidate["speaker_id"])
                break
    # Buckets can be empty at the extremes; backfill from the global ranking.
    for candidate in ranked:
        if len(chosen) >= len(names):
            break
        if candidate["speaker_id"] not in used:
            chosen.append(candidate)
            used.add(candidate["speaker_id"])
    chosen.sort(key=lambda r: r["f0"])
    return list(zip(chosen, names))


def mls_voices(sweep: list[dict]) -> list[dict]:
    rows = [r for r in sweep if "error" not in r and r["f0"] > 0]
    median_duration = statistics.median(r["duration"] for r in rows)
    female = [r for r in rows if r["f0"] >= FEMALE_MIN_F0]
    male = [r for r in rows if r["f0"] <= MALE_MAX_F0]

    voices: list[dict] = []
    for gender, picks in (
        ("female", pick(female, FEMALE_NAMES, median_duration)),
        ("male", pick(male, MALE_NAMES, median_duration)),
    ):
        for row, name in picks:
            voices.append(
                {
                    "id": f"fallback-de-{name.lower()}",
                    "name": name,
                    "model": "de_DE-mls-medium",
                    "speaker_id": row["speaker_id"],
                    "gender": gender,
                    "description": f"Fallback-Stimme, {row['f0']:.0f} Hz Grundton",
                }
            )
    return voices


NAMED_VOICES = [
    {
        "id": "fallback-de-thorsten",
        "name": "Thorsten",
        "model": "de_DE-thorsten-medium",
        "speaker_id": 0,
        "gender": "male",
        "description": "Studioqualität, neutrale Erzählstimme",
    },
    {
        "id": "fallback-de-eva",
        "name": "Eva",
        "model": "de_DE-eva_k-x_low",
        "speaker_id": 0,
        "gender": "female",
        "description": "Ruhige Vorlesestimme",
    },
    {
        "id": "fallback-de-kerstin",
        "name": "Kerstin",
        "model": "de_DE-kerstin-low",
        "speaker_id": 0,
        "gender": "female",
        "description": "Freundlich, mittlere Lage",
    },
    {
        "id": "fallback-de-ramona",
        "name": "Ramona",
        "model": "de_DE-ramona-low",
        "speaker_id": 0,
        "gender": "female",
        "description": "Warm, leicht dunkel",
    },
    {
        "id": "fallback-de-karlsson",
        "name": "Karlsson",
        "model": "de_DE-karlsson-low",
        "speaker_id": 0,
        "gender": "male",
        "description": "Sachlich, nachrichtennah",
    },
    {
        "id": "fallback-de-pavoque",
        "name": "Pavoque",
        "model": "de_DE-pavoque-low",
        "speaker_id": 0,
        "gender": "male",
        "description": "Kräftig, erzählend",
    },
]

EMOTIONS = [
    ("neutral", "Neutral", "Sachlicher Grundton"),
    ("amused", "Erheitert", "Aufgeräumt, mit Lächeln in der Stimme"),
    ("angry", "Wütend", "Scharf, hoher Druck"),
    ("surprised", "Überrascht", "Hohe Spannung, steigende Melodie"),
    ("disgusted", "Angewidert", "Zurückweisend, gepresst"),
    ("sleepy", "Müde", "Langsam, tiefer Tonfall"),
    ("drunk", "Beschwipst", "Unpräzise Artikulation"),
    ("whisper", "Flüsternd", "Geflüstert, sehr leise"),
]


def emotional_voices(config: dict) -> list[dict]:
    mapping = config.get("speaker_id_map", {})
    voices = []
    for key, label, description in EMOTIONS:
        if key not in mapping:
            continue
        voices.append(
            {
                "id": f"fallback-de-thorsten-{key}",
                "name": f"Thorsten · {label}",
                "model": "de_DE-thorsten_emotional-medium",
                "speaker_id": int(mapping[key]),
                "gender": "male",
                "description": description,
            }
        )
    return voices


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sweep", default=".piper-tmp/mls-speakers.json")
    parser.add_argument("--models", default="kikiri-tts/piper-models")
    parser.add_argument("--out", default="kikiri-tts/voices.json")
    args = parser.parse_args()

    sweep = json.loads(Path(args.sweep).read_text(encoding="utf-8"))
    emotional_config = json.loads(
        (Path(args.models) / "de_DE-thorsten_emotional-medium.onnx.json").read_text(encoding="utf-8")
    )

    voices = NAMED_VOICES + emotional_voices(emotional_config) + mls_voices(sweep)
    payload = {
        "note": (
            "Fallback-Stimmbank fuer kikiri-tts. Wird nur benutzt, wenn OmniVoice "
            "nicht laufen kann. Erzeugt von scripts/build-fallback-voice-catalog.py."
        ),
        "voices": voices,
    }
    Path(args.out).write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(voices)} voices -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
