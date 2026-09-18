"""Szenenausrichtung wird in Abschnitte geschnitten, statt Folge für Folge zu laufen.

Band 10, Folge 56857 hatte zwölf Pflicht-Einführungen; der Editor verbrauchte
das gesamte Ausgabebudget von 256.000 Token auf Denken und antwortete nie.
Die Abschnitte müssen jede Bedingung erhalten, die der Runner prüft.
"""
from pathlib import Path
from typing import Any

import pytest

from hoerspiele import engine as e

REPO_ROOT = Path(__file__).resolve().parents[2]


def build_project(episode_id="ep1", duration_ms=1_200_000, lexicon=None):
    return {
        "narration_name_lexicon": lexicon or [],
        "reconciled_transcript": [
            {"episode_id": episode_id, "episode_start_ms": start, "episode_end_ms": start + 3_000}
            for start in range(0, duration_ms, 3_000)
        ],
        "media_assets": [{"episode_id": episode_id, "duration_ms": duration_ms}],
    }


def cue(alias, order, anchor_ms, text="Ein Bild.", intro_name=None):
    item = {
        "id": alias,
        "order": order,
        "text": text,
        "source_context": "",
        "proposed_episode_id": "ep1",
        "proposed_anchor_ms": anchor_ms,
    }
    if intro_name:
        item["character_introduction_candidate"] = True
        item["introduction_research_name"] = intro_name
    return item


def introduction(name, required=True):
    return {"name": name, "role": "r", "distinguishing_traits": "t",
            "visual_description": "v", "first_appearance": "f",
            "introduction_required": required}


def targets(book_edge="both"):
    return {"ep1": {"retained_ms": 1_200_000, "candidate_count": 30, "min_cues": 8,
                    "max_cues": 30, "max_gap_ms": 150_000, "book_edge_episode": book_edge}}


def test_long_episode_is_split_and_every_cue_lands_exactly_once():
    payload = {
        "cues": [cue(f"c{i:03d}", i, i * 40_000) for i in range(30)],
        "episode_contexts": [{"episode_id": "ep1", "character_introductions": []}],
    }
    packages = e.scene_alignment_packages(build_project(), payload, targets(), ["ep1"])
    assert len(packages) == 3
    assert [len(p["cue_ids"]) for p in packages] == [12, 12, 6]
    seen = [alias for p in packages for alias in p["cue_ids"]]
    assert len(seen) == len(set(seen)) == 30


def test_windows_partition_the_timeline_without_overlap():
    payload = {
        "cues": [cue(f"c{i:03d}", i, i * 40_000) for i in range(30)],
        "episode_contexts": [{"episode_id": "ep1", "character_introductions": []}],
    }
    packages = e.scene_alignment_packages(build_project(), payload, targets(), ["ep1"])
    assert packages[0]["window"][0] is None and packages[-1]["window"][1] is None
    for left, right in zip(packages, packages[1:]):
        assert left["window"][1] == right["window"][0]
    anchor_of = {c["id"]: c["proposed_anchor_ms"] for c in payload["cues"]}
    for package in packages:
        start, end = package["window"]
        for alias in package["cue_ids"]:
            assert (start is None or anchor_of[alias] >= start)
            assert (end is None or anchor_of[alias] < end)


def test_mandatory_introductions_cap_the_package_size():
    names = ["Krillin", "Bulma", "Yamchu", "Puar", "Oolong", "Tenshinhan"]
    payload = {
        "cues": [cue(f"c{i:03d}", i, i * 40_000, intro_name=name) for i, name in enumerate(names)],
        "episode_contexts": [{"episode_id": "ep1",
                              "character_introductions": [introduction(n) for n in names]}],
    }
    packages = e.scene_alignment_packages(build_project(), payload, targets(), ["ep1"])
    assert len(packages) == 2
    assert [len(p["required_keys"]) for p in packages] == [4, 2]
    assert set().union(*(p["required_keys"] for p in packages)) == {
        "".join(e.normalized_character_evidence(n).split()) for n in names
    }


def test_introduction_without_dedicated_cue_follows_its_earliest_evidence():
    payload = {
        "cues": [
            cue("c000", 0, 0, text="Der Hügel liegt still."),
            cue("c001", 1, 400_000, text="Krillin tritt aus dem Schatten."),
            cue("c002", 2, 800_000, text="Krillin hebt die Hand."),
        ],
        "episode_contexts": [{"episode_id": "ep1",
                              "character_introductions": [introduction("Krillin")]}],
    }
    project = build_project()
    packages = e.scene_alignment_packages(project, payload, targets(), ["ep1"])
    assert len(packages) == 1
    assert packages[0]["required_keys"] == {"krillin"}

    # Bei zwei Abschnitten trägt der frühere Beleg die Pflicht.
    e_max = e.SCENE_ALIGNMENT_MAX_CUES
    try:
        e.SCENE_ALIGNMENT_MAX_CUES = 2
        packages = e.scene_alignment_packages(project, payload, targets(), ["ep1"])
    finally:
        e.SCENE_ALIGNMENT_MAX_CUES = e_max
    assert [p["required_keys"] for p in packages] == [{"krillin"}, set()]


def test_book_edges_stay_at_the_outer_packages():
    payload = {
        "cues": [cue(f"c{i:03d}", i, i * 40_000) for i in range(30)],
        "episode_contexts": [{"episode_id": "ep1", "character_introductions": []}],
    }
    packages = e.scene_alignment_packages(build_project(), payload, targets("both"), ["ep1"])
    assert [p["coverage_overrides"]["book_edge_episode"] for p in packages] == ["start", "none", "end"]

    packages = e.scene_alignment_packages(build_project(), payload, targets("end"), ["ep1"])
    assert [p["coverage_overrides"]["book_edge_episode"] for p in packages] == ["none", "none", "end"]


def test_minimum_cue_count_scales_with_the_slice():
    payload = {
        "cues": [cue(f"c{i:03d}", i, i * 40_000) for i in range(30)],
        "episode_contexts": [{"episode_id": "ep1", "character_introductions": []}],
    }
    project = build_project()
    packages = e.scene_alignment_packages(project, payload, targets(), ["ep1"])
    whole = e.narration_coverage_targets(project, [
        {"id": f"c{i:03d}", "anchor_episode_id": "ep1", "anchor_episode_start_ms": i * 40_000}
        for i in range(30)
    ])["ep1"]
    minima = [p["coverage_overrides"]["min_cues"] for p in packages]
    assert sum(minima) <= whole["min_cues"] + len(packages)
    for package, minimum in zip(packages, minima):
        assert minimum <= package["coverage_overrides"]["candidate_count"]


def test_dedicated_introduction_cues_do_not_count_as_coverage_candidates():
    """Einfuehrungs-Cues fehlen in candidate_anchor_ms, nicht in der Zaehlung.

    Die Abdeckungsluecke darf sie nicht als Beleg werten; auswaehlbar sind sie
    trotzdem, und der Runner zaehlt sie bei candidate_counts mit.  Beides zu
    vermischen ergab die Forderung "1 bis 1" bei zwei Cues.
    """
    payload = {
        "cues": [cue("c000", 0, 0), cue("c001", 1, 40_000, intro_name="Krillin")],
        "episode_contexts": [{"episode_id": "ep1",
                              "character_introductions": [introduction("Krillin")]}],
    }
    packages = e.scene_alignment_packages(build_project(), payload, targets(), ["ep1"])
    override = packages[0]["coverage_overrides"]
    assert override["candidate_count"] == 2
    assert override["max_cues"] == 2
    assert override["candidate_anchor_ms"] == [0]


def load_density_validator() -> dict[str, Any]:
    """runner.py laeuft im Agenten-Image; im API-Image fehlen seine Importe.

    Wie in test_quality_repair.py wird die Quelle als Text gelesen und nur der
    gebrauchte Abschnitt in einem eigenen Namensraum ausgefuehrt.
    """
    source = (REPO_ROOT / "vocarium-agent" / "runner.py").read_text("utf-8")
    start = source.index("def episode_coverage_gap_violations(")
    end = source.index("def validated_episode_context_request(")
    namespace: dict[str, Any] = {"Any": Any}
    exec(source[start:end], namespace)
    return namespace


def test_explicit_package_minimum_is_not_raised_back_to_four():
    """Ein Abschnitt mit vier Cues darf nicht "alle vier platzieren" heissen.

    Der Runner zaehlt Einfuehrungs-Cues bei candidate_counts mit.  Mit dem
    frueheren max(4, ...) forderte ein Vier-Cue-Abschnitt jeden davon, und die
    Absenkung nach erschoepften Versuchen (min_cues = 1) kam nie an.
    """
    namespace = load_density_validator()
    result = namespace["validate_alignment_density"](
        "audio_drama",
        {"c0", "c1", "c2", "c3"},
        ["c0"],
        {"ep1": 4},
        {"ep1": 1},
        coverage_targets={"ep1": {"candidate_count": 4, "min_cues": 1, "max_cues": 4}},
    )
    assert result == set()


def test_package_without_coverage_candidates_demands_nothing():
    namespace = load_density_validator()
    result = namespace["validate_alignment_density"](
        "audio_drama",
        {"c0"},
        [],
        {"ep1": 0},
        {"ep1": 0},
        coverage_targets={"ep1": {"candidate_count": 0, "min_cues": 0}},
    )
    assert result == set()


def test_episode_without_own_target_still_demands_four():
    namespace = load_density_validator()
    with pytest.raises(namespace["CoverageCountError"]) as excinfo:
        namespace["validate_alignment_density"](
            "audio_drama",
            {f"c{i}" for i in range(6)},
            ["c0", "c1", "c2"],
            {"ep1": 6},
            {"ep1": 3},
        )
    assert excinfo.value.episode_id == "ep1"
    assert "4 bis 6" in str(excinfo.value)


def test_no_hard_minimum_of_four_survives_in_the_runner():
    """Jede feste Vier ueberschreibt die Forderung, die der Aufrufer ausrechnet."""
    source = (REPO_ROOT / "vocarium-agent" / "runner.py").read_text("utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert 'max(4, int(target.get("min_cues")' not in code
    assert '.get("min_cues") or 4' not in code


def test_section_ceiling_counts_introduction_cues():
    """Die Obergrenze je Abschnitt muss Einfuehrungs-Cues mitzaehlen.

    candidate_anchor_ms laesst sie bewusst weg, weil sie von der Abdeckung
    befreit sind.  Wurde daraus auch max_cues gerechnet, forderte ein Abschnitt
    mit einem normalen und drei Einfuehrungs-Cues "1 bis 1"; der Runner zaehlt
    beim Auswaehlen alle vier und brach den Lauf ab (Band 10, Folge 56857).
    """
    cues = [cue("c000", 0, 0)] + [
        cue(f"c{i:03d}", i, i * 40_000, intro_name=f"Figur {i}") for i in range(1, 4)
    ]
    payload = {
        "cues": cues,
        "episode_contexts": [
            {
                "episode_id": "ep1",
                "character_introductions": [introduction(f"Figur {i}") for i in range(1, 4)],
            }
        ],
    }
    packages = e.scene_alignment_packages(build_project(), payload, targets(), ["ep1"])
    assert len(packages) == 1
    override = packages[0]["coverage_overrides"]
    assert override["max_cues"] == 4
    assert override["candidate_count"] == 4
    assert override["candidate_anchor_ms"] == [0]


def test_every_section_may_place_all_of_its_own_cues():
    """Die Summe der Abschnittsgrenzen bleibt die alte Folgengrenze."""
    payload = {
        "cues": [cue(f"c{i:03d}", i, i * 40_000) for i in range(30)],
        "episode_contexts": [{"episode_id": "ep1", "character_introductions": []}],
    }
    packages = e.scene_alignment_packages(build_project(), payload, targets(), ["ep1"])
    assert sum(p["coverage_overrides"]["max_cues"] for p in packages) == 30
    for package in packages:
        override = package["coverage_overrides"]
        assert override["max_cues"] == len(package["cue_ids"])
        assert override["min_cues"] <= override["max_cues"]


def test_repair_orders_picture_around_locked_actual_anchor_without_mutating_source(monkeypatch):
    project = {"binding": {"title": "Series"}, "mapping": [{"episode_id": "ep"}],
               "chapters": [], "narration_density": "audio_drama",
               "reconciled_transcript": [
                   {"id": "rough", "episode_id": "ep", "episode_start_ms": 1065000, "episode_end_ms": 1066000, "text": "Earlier dialogue"},
                   {"id": "picture", "episode_id": "ep", "episode_start_ms": 1155000, "episode_end_ms": 1155001, "text": "Picture", "visual_scene_anchor": True},
                   {"id": "accepted", "episode_id": "ep", "episode_start_ms": 1263000, "episode_end_ms": 1264000, "text": "Later dialogue"}],
               "media_assets": [{"episode_id": "ep", "duration_ms": 1400000}]}
    cues = [{"id": "old", "text": "A source passage", "source_fragment_ids": [],
             "anchor_episode_id": "ep", "anchor_segment_id": "rough", "anchor_episode_start_ms": 1065000},
            {"id": "new", "text": "A reviewed picture", "source_fragment_ids": [],
             "anchor_episode_id": "ep", "anchor_segment_id": "picture", "anchor_episode_start_ms": 1155000}]
    class Captured(Exception):
        pass
    def capture(project, payload, targets, episode_ids):
        old, new = payload["cues"]
        assert old["id"] == "c000" and new["id"] == "c001"
        assert old["proposed_anchor_ms"] == 1263000
        assert new["order"] < old["order"]
        raise Captured
    monkeypatch.setenv("CODEX_AGENT_BASE_URL", "http://unused")
    monkeypatch.setattr(e, "agent_execution_settings", lambda profile: {})
    monkeypatch.setattr(e, "scene_alignment_packages", capture)
    with pytest.raises(Captured):
        e.research_scene_alignment(project, cues, {"episode_contexts": []}, "audio_drama",
                                  repair={"locked": {"old": {"anchor_segment_id": "accepted"}}, "cue_ids": {"new"}})
    assert cues[0]["anchor_episode_start_ms"] == 1065000
