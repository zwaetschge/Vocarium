from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hoerspiele import engine as e
from hoerspiele.scene_evidence import apply_reviewed_scene_evidence, source_digest


def test_reviewed_anchors_preserve_source_and_alias_order():
    project = {"chapters": [{"fragments": [{"id": "f", "text": "Original novel passage."}]}],
               "reconciled_transcript": [{"id": "right", "episode_id": "ep", "episode_start_ms": 500000}],
               "reviewed_narration_anchors": [{"episode_id": "ep", "source_digest": source_digest("Original novel passage."), "anchor_segment_id": "right"}],
               "reviewed_visual_scene_anchors": [{"id": "picture", "episode_id": "ep", "episode_start_ms": 300000,
                                                   "visual_evidence": "The figure carries a white cloth.", "narration_candidate": True, "evidence_frames": ["300.jpg"]}]}
    cues = [{"id": "earlier", "anchor_episode_id": "other", "text": "Earlier", "source_fragment_ids": []},
            {"id": "novel", "anchor_episode_id": "ep", "text": "Original novel passage.", "source_fragment_ids": ["f"], "anchor_segment_id": "wrong"}]
    original = deepcopy(cues)
    result = apply_reviewed_scene_evidence(project, cues)
    assert [c["id"] for c in result[:2]] == ["earlier", "novel"]
    assert result[0] == cues[0]
    assert result[1]["anchor_segment_id"] == "right"
    assert result[1]["text"] == cues[1]["text"]
    assert cues == original
    assert result[2]["source_fragment_ids"] == []
    assert result[2]["editorial_source_context"] == project["reviewed_visual_scene_anchors"][0]["visual_evidence"]
    assert result[2]["coverage_repair_window"] == {"start_ms": 299000, "end_ms": 301000}
    assert len(apply_reviewed_scene_evidence(project, result)) == 3


def test_final_gate_distinguishes_picture_evidence_from_spoken_audio():
    text = "Die gelben Hosenbeine schweben über den grauen Steinplatten."
    anchor = {"id": "picture", "episode_id": "ep", "episode_start_ms": 700000,
              "episode_end_ms": 700001, "text": text, "visual_scene_anchor": True}
    cue = {"id": "cue", "anchor_segment_id": "picture", "text": text,
           "source_fragment_ids": [], "editorial_source_context": text}
    project = {"reconciled_transcript": [anchor]}
    assert e.native_audio_repetition_violations(project, [cue]) == []
    project["reconciled_transcript"].append({**anchor, "id": "spoken", "visual_scene_anchor": False})
    assert e.native_audio_repetition_violations(project, [cue])


def test_final_gate_short_words_and_spaced_names_do_not_create_false_repetitions():
    project = {"narration_name_lexicon": [{"canonical": "Chao Zu"}],
               "reconciled_transcript": [{"id": "s", "episode_id": "ep", "episode_start_ms": 490000,
                                         "text": "Chaozus Kopf ist härter als Diamant."}]}
    cue = {"id": "c", "anchor_segment_id": "s", "text": "Chaozu hält einen hellblauen Beutel an seinen Kopf.",
           "editorial_source_context": "Chaozu hält einen hellblauen Beutel an seinen Kopf."}
    assert e.native_audio_repetition_violations(project, [cue]) == []
    project["reconciled_transcript"][0]["text"] = "Kommt, setzt den Kampf bitte fort!"
    cue["text"] = cue["editorial_source_context"] = "Sein drittes Auge sitzt mitten auf der angespannten Stirn."
    assert e.native_audio_repetition_violations(project, [cue]) == []


def setup_endpoint(monkeypatch):
    user = SimpleNamespace(user_id=2)
    project = {"reconciled_transcript": [{"id": "s", "episode_id": "ep", "episode_start_ms": 10000,
                                         "start_sample": 960000}],
               "media_assets": [{"episode_id": "ep", "duration_ms": 30000}]}
    def owned(pid, actual_user):
        assert pid == "project" and actual_user is user
        return project
    monkeypatch.setattr(e, "project_or_404", owned)
    monkeypatch.setattr(e, "active_project_run", lambda pid: None)
    monkeypatch.setattr(e, "save_state", lambda: None)
    return user, project


def test_evidence_save_is_owner_scoped_and_does_not_start_production(monkeypatch):
    user, project = setup_endpoint(monkeypatch)
    request = e.SceneEvidenceRequest(episode_id="ep", pictures=[{"start_ms": 15000,
        "visual_evidence": "A figure holds a white cloth beside the stone wall.", "evidence_frames": ["frame.jpg"]}])
    result = e.save_scene_evidence("project", request, user)
    assert result["production_started"] is False
    assert project["reconciled_transcript"][0]["id"] == "s"
    picture = project["reviewed_visual_scene_anchors"][0]
    assert picture["start_sample"] == 1200000
    assert picture["visual_scene_anchor"] and picture["narration_candidate"]
    e.save_scene_evidence("project", request, user)
    assert len(project["reconciled_transcript"]) == 2


def test_evidence_rejects_foreign_anchors_and_running_productions(monkeypatch):
    user, project = setup_endpoint(monkeypatch)
    original = deepcopy(project)
    request = e.SceneEvidenceRequest(episode_id="ep", narration_anchors=[{
        "source_digest": "a" * 64, "anchor_segment_id": "another-project"}])
    with pytest.raises(HTTPException) as error:
        e.save_scene_evidence("project", request, user)
    assert error.value.status_code == 400
    assert project == original
    monkeypatch.setattr(e, "active_project_run", lambda pid: {"status": "running"})
    with pytest.raises(HTTPException) as error:
        e.save_scene_evidence("project", e.SceneEvidenceRequest(episode_id="ep"), user)
    assert error.value.status_code == 409
    assert project == original


def test_character_role_name_does_not_inherit_contained_person_aliases():
    project = {"narration_name_lexicon": [{"canonical": "Panputto", "aliases": ["Panputt"]}]}
    assert e.character_name_variants(project, "Panputtos Manager") == ["Panputtos Manager"]
    assert set(e.character_name_variants(project, "Panputto")) == {"Panputto", "Panputt"}
    assert set(e.character_name_variants(project, "Panputto (Panputt)")) == {"Panputto", "Panputt"}


def test_short_overlay_contract_matches_runner_without_weakening_insert_minimum():
    cue = {"text": "Der Herr der Kraniche lächelt; Muten Roshi wird ernst.",
           "narrative_purpose": "visual_action", "beat_type": "reaction",
           "audio_strategy": "prefer_ambience_overlay", "placement_policy": "overlay_speech_free",
           "target_duration_ms": 4050, "measured_duration_ms": 4100}
    assert e.cue_audio_drama_contract_complete(cue)
    assert not e.cue_audio_drama_contract_complete({**cue, "target_duration_ms": 3000})
    assert not e.cue_audio_drama_contract_complete({**cue, "text": "Er lächelt nur."})
    assert not e.cue_audio_drama_contract_complete({**cue, "placement_policy": e.ALIGNED_SCENE_INSERT_POLICY})
    assert not e.cue_audio_drama_contract_complete({**cue, "measured_duration_ms": 23000})


def test_actual_gap_prefers_reviewed_picture_and_keeps_source_separate_and_id_stable():
    picture = {"id": "picture", "episode_id": "ep", "episode_start_ms": 120000,
               "episode_end_ms": 120001, "text": "Ein blaues Auto steht vor einer weißen Wand.",
               "visual_evidence": "Ein blaues Auto steht vor einer weißen Wand.",
               "visual_scene_anchor": True, "narration_candidate": True, "evidence_frames": ["120.jpg"]}
    project = {"narration_density": "audio_drama", "chapters": [], "cues": [],
               "reviewed_visual_scene_anchors": [picture], "reconciled_transcript": [picture,
                  {"id": "later-dialogue", "episode_id": "ep", "episode_start_ms": 129000,
                   "episode_end_ms": 130000, "text": "Wo bleibt er?"}],
               "timeline": {"duration_samples": 270000*48, "clips": [
                   {"track_id": "trk_narrator", "cue_id": "a", "timeline_start_sample": 0, "source_start_sample": 0, "source_end_sample": 10000*48},
                   {"track_id": "trk_narrator", "cue_id": "b", "timeline_start_sample": 260000*48, "source_start_sample": 0, "source_end_sample": 10000*48},
                   {"track_id": "trk_episode", "episode_id": "ep", "timeline_start_sample": 10000*48,
                    "source_start_sample": 0, "source_end_sample": 250000*48}]}}
    candidates = e.timeline_gap_repair_candidates(project)
    assert len(candidates) == 1
    assert candidates[0]["anchor_segment_id"] == "picture"
    assert candidates[0]["source_fragment_ids"] == []
    assert candidates[0]["editorial_source_context"] == picture["visual_evidence"]
    assert candidates[0]["id"] == apply_reviewed_scene_evidence(project, [])[0]["id"]
    assert e.timeline_gap_repair_candidates(project) == candidates
    assert candidates[0]["coverage_repair_window"] == {"start_ms": 119000, "end_ms": 121000}


def test_picture_grounding_requires_persisted_matching_source_not_a_cue_flag():
    marker = {"id": "picture", "episode_id": "ep", "episode_start_ms": 120000,
              "visual_evidence": "Ein blaues Auto steht vor einer weißen Wand.",
              "evidence_frames": ["120.jpg"], "visual_scene_anchor": True, "narration_candidate": True}
    project = {"mapping": [{"episode_id": "ep"}], "reviewed_visual_scene_anchors": [marker],
               "reconciled_transcript": [marker]}
    cue = apply_reviewed_scene_evidence(project, [])[0]
    project["cues"] = [cue]
    assert e.ungrounded_narration_cue_ids(project) == set()
    for changed in [{"editorial_source_context": "Invented source"}, {"anchor_episode_id": "other"},
                    {"anchor_episode_start_ms": 130000}, {"visual_evidence_frames": []}, {"confidence": 0.1}]:
        project["cues"] = [{**cue, **changed}]
        assert e.ungrounded_narration_cue_ids(project) == {cue["id"]}
    project["cues"] = [cue]
    project["reviewed_visual_scene_anchors"] = []
    assert e.ungrounded_narration_cue_ids(project) == {cue["id"]}


def test_opening_utterance_start_and_inner_anchor_share_safe_boundary():
    speech = [(318484, 324000)]
    assert e.speech_safe_insert_ms(318484, speech) == 318364
    assert e.speech_safe_insert_ms(320320, speech) == 318364
    assert e.speech_safe_insert_ms(318364, speech) == 318364
    assert e.speech_safe_insert_ms(324000, speech) == 324000


def test_picture_source_survives_nearby_subtitle_timing_anchor():
    marker = {"id": "picture", "episode_id": "ep", "episode_start_ms": 122000,
              "visual_evidence": "Die graue Kampffläche liegt vor dem strohfarbenen Dach.",
              "evidence_frames": ["122.jpg"], "visual_scene_anchor": True, "narration_candidate": True}
    subtitle = {"id": "speech", "episode_id": "ep", "episode_start_ms": 122789}
    project = {"mapping": [{"episode_id": "ep"}], "reviewed_visual_scene_anchors": [marker],
               "reconciled_transcript": [marker, subtitle]}
    cue = apply_reviewed_scene_evidence(project, [])[0]
    cue.update(anchor_segment_id="speech", anchor_episode_start_ms=122789)
    project["cues"] = [cue]
    assert e.ungrounded_narration_cue_ids(project) == set()
    for changed in [{"episode_start_ms": 124000}, {"episode_id": "other"}]:
        project["reconciled_transcript"] = [marker, {**subtitle, **changed}]
        project["cues"] = [{**cue, "anchor_episode_start_ms": changed.get("episode_start_ms", 122789)}]
        assert e.ungrounded_narration_cue_ids(project) == {cue["id"]}
    project["reconciled_transcript"] = [subtitle]
    project["cues"] = [cue]
    assert e.ungrounded_narration_cue_ids(project) == {cue["id"]}


def test_coverage_repair_retains_existing_picture_introduction_carrier():
    project = {"id": "p", "mapping": [{"episode_id": "ep"}], "chapters": [],
               "cues": [{"id": "picture-cue", "anchor_episode_id": "ep"}],
               "character_introductions": [{"research_name": "Panputtos Manager", "cue_id": "picture-cue"}],
               "reconciled_transcript": [{"id": "s", "episode_id": "ep", "episode_start_ms": 1000,
                                          "episode_end_ms": 2000, "text": "Panputtos Manager!"}]}
    research = {"episode_contexts": [{"episode_id": "ep", "character_introductions": [
        {"name": "Panputtos Manager", "introduction_required": True,
         "visual_description": "Weißer Hut und dunkle Sonnenbrille."}]}]}
    assert e.add_dedicated_character_introduction_candidates(project, [], research)
    assert not e.add_dedicated_character_introduction_candidates(project, [], research, preserve_existing=True)
    assert e.add_dedicated_character_introduction_candidates(project, [], research, force=True, preserve_existing=True)


def test_additive_repair_preserves_valid_neighbours_but_releases_ungrounded_text(monkeypatch):
    user = SimpleNamespace(user_id=2)
    base = {"anchor_episode_id": "ep", "anchor_segment_id": "s", "confidence": .95}
    project = {"cues": [{**base, "id": "good", "source_fragment_ids": ["f"]},
                        {**base, "id": "bad", "source_fragment_ids": []}],
               "mapping": [{"episode_id": "ep"}], "quality_report": {"checks": [
                   {"id": "narration_coverage", "status": "failed", "cue_ids": ["good", "bad"],
                    "actual": {"gaps": [{"before_cue_id": "good", "after_cue_id": "bad"}]}},
                   {"id": "narration_grounding", "status": "failed", "cue_ids": ["bad"]}]}}
    launched = []
    monkeypatch.setattr(e, "project_or_404", lambda pid, actual_user: project)
    monkeypatch.setattr(e, "active_project_run", lambda pid: None)
    monkeypatch.setattr(e, "save_state", lambda: None)
    monkeypatch.setattr(e, "STATE", {"runs": {}})
    monkeypatch.setattr(e.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: launched.append(kw["args"])))
    e.repair_from_quality_report("p", e.QualityRepairRequest(preserve_valid_cues=True), user)
    assert launched[0][3] == {"bad"}
    assert launched[0][5] is True  # Coverage is still mandatory.


def _saved_narration_anchor(monkeypatch):
    user, project = setup_endpoint(monkeypatch)
    project["reconciled_transcript"][0].update(
        episode_end_ms=12000, text="The same reviewed subtitle.")
    passage = "The original novel passage."
    e.save_scene_evidence("project", e.SceneEvidenceRequest(
        episode_id="ep", narration_anchors=[{
            "source_digest": source_digest(passage), "anchor_segment_id": "s"}]), user)
    cue = {"id": "novel", "anchor_episode_id": "ep", "editorial_source_context": passage,
           "text": passage, "anchor_segment_id": "old-proposal"}
    return project, cue


def test_reviewed_narration_anchor_survives_reconciliation_id_replacement(monkeypatch):
    project, cue = _saved_narration_anchor(monkeypatch)
    project["reconciled_transcript"][0]["id"] = "new-reconciled-id"
    before = deepcopy(project)
    result = apply_reviewed_scene_evidence(project, [cue])
    assert result[0]["anchor_segment_id"] == "new-reconciled-id"
    assert result[0]["anchor_episode_start_ms"] == 10000
    assert result[0]["text"] == cue["text"]
    assert project == before  # Resolving sources does not persist or rewrite the review.


@pytest.mark.parametrize("change", ["start", "end", "text", "episode", "duplicate", "same_id_new_text"])
def test_reviewed_narration_anchor_rebinding_requires_unique_unchanged_evidence(monkeypatch, change):
    project, cue = _saved_narration_anchor(monkeypatch)
    row = project["reconciled_transcript"][0]
    row["id"] = "new-id" if change != "same_id_new_text" else "s"
    if change == "start":
        row["episode_start_ms"] += 1
    elif change == "end":
        row["episode_end_ms"] += 1
    elif change in {"text", "same_id_new_text"}:
        row["text"] = "A different subtitle."
    elif change == "episode":
        row["episode_id"] = "other-episode"
    elif change == "duplicate":
        project["reconciled_transcript"].append({**row, "id": "duplicate-id"})
    with pytest.raises(ValueError, match="Reviewed narration anchor"):
        apply_reviewed_scene_evidence(project, [cue])
