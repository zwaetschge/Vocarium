"""Regression coverage for repairs that previously reproduced the same gate failures."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from hoerspiele import engine as e

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_repair_mapping_is_injective_and_preserves_unmatched_cues():
    old = [dict(id=f"old{i}", source_fragment_ids=["f"], anchor_episode_id="ep",
                anchor_segment_id="s", text=f"Text {i}") for i in range(3)]
    raw = [dict(id="raw", source_fragment_ids=["f"], anchor_episode_id="ep", anchor_segment_id="s")]
    repair, locked, _ = e.locked_alignments_for_repair({"cues": old}, raw, {"old1"})
    assert len(raw) == 3
    assert len(locked) == 2
    assert len(repair) == 1
    assert not repair & locked.keys()
    assert {x["narration_text"] for x in locked.values()} == {"Text 0", "Text 2"}


def test_locks_restore_anchor_and_introduction_even_when_text_unchanged():
    lock = dict(cue_id="c1", narration_text="Unchanged", anchor_segment_id="s1", placement="before_anchor")
    intro = dict(research_name="Name", cue_id="c1", episode_id="ep")
    payload = {"_locked_alignments": [lock], "_locked_character_introductions": [intro]}
    result = {"alignments": [{**lock, "anchor_segment_id": "s2"}],
              "character_introductions": [{**intro, "cue_id": "wrong"}]}
    e.enforce_locked_alignments(result, payload, ["ep"])
    assert result["alignments"] == [lock]
    assert result["character_introductions"] == [intro]


def test_global_editorial_failure_unlocks_invalid_cues_and_intro_ids():
    cue = dict(id="cue_intro_12345678901234567890", narrative_purpose="visual_action")
    project = {"cues": [cue], "narration_density": "audio_drama",
               "narration_density_contract": {"editorial_contract_version": e.NARRATION_EDITOR_VERSION},
               "quality_report": {"checks": [{"id": "narration_editorial_quality", "status": "failed",
                                               "detail": "Hörspiel-Beat, Audio-Strategie oder Sprechdauer verletzt den Vertrag"}]}}
    assert e.quality_repair_targets(project)[0] == {cue["id"]}


def test_contexts_require_one_introduction_at_first_episode_for_aliases():
    project = {"mapping": [{"episode_id": "early"}, {"episode_id": "late"}],
               "narration_name_lexicon": [{"canonical": "Muten Roshi", "aliases": ["Herr der Schildkröten"]}]}
    research = {"episode_contexts": [
        {"episode_id": "late", "character_introductions": [{"name": "Herr der Schildkröten (Muten Roshi)", "introduction_required": True}]},
        {"episode_id": "early", "character_introductions": [{"name": "Muten-Roshi", "introduction_required": True}]}]}
    original = deepcopy(research)
    normalized = e.normalize_character_introduction_contexts(project, research)
    required = [(c["episode_id"], x["name"]) for c in normalized["episode_contexts"]
                for x in c["character_introductions"] if x["introduction_required"]]
    assert required == [("early", "Muten Roshi")]
    assert research == original


def test_missing_introductions_make_repair_available_without_existing_intro_cue():
    project = {"cues": [{"id": "ordinary"}], "quality_report": {"checks": [
        {"id": "character_introduction_timing", "status": "failed", "detail": "Upa: 0 formale Zuordnungen statt genau einer"}]}}
    assert e.quality_repair_needs_introductions(project)


def test_coverage_repair_uses_locked_actual_anchor_and_closes_multiple_gaps():
    project = {"narration_rules": {"narration_max_gap_ms": 180000},
               "transcript": [{"id": "moved", "episode_start_ms": 10000}]}
    target = {"ep": {"min_cues": 4, "candidate_anchors": {
        "locked": 300000, "a": 150000, "b": 290000, "c": 430000, "end": 560000}}}
    selected = e.coverage_gap_repair_ids(project, target, {"locked": {"anchor_segment_id": "moved"}})
    assert {"a", "b", "c"} <= selected
    assert "locked" not in selected


def test_locked_source_detail_evidence_survives_repair():
    cue = {"id": "old", "source_fragment_ids": ["f"], "anchor_episode_id": "ep",
           "source_detail_reference_count": 20, "preserved_concrete_detail_count": 2,
           "source_detail_coverage": 0.12}
    raw = [{**cue, "id": "new"}]
    _, locked, _ = e.locked_alignments_for_repair({"cues": [cue]}, raw, set())
    assert locked["new"]["source_detail_reference_count"] == 20
    assert locked["new"]["preserved_concrete_detail_count"] == 2
    assert locked["new"]["source_detail_coverage"] == 0.12


def test_intro_id_in_report_is_not_truncated():
    cue = {"id": "cue_intro_1234567890abcdef1234"}
    project = {"cues": [cue], "quality_report": {"checks": [
        {"id": "other", "status": "failed", "detail": f"Problem at {cue['id']}"}]}}
    assert e.quality_repair_targets(project)[0] == {cue["id"]}


def test_normalization_is_idempotent_and_does_not_merge_unrelated_characters():
    project = {"mapping": [{"episode_id": "ep"}]}
    research = {"episode_contexts": [{"episode_id": "ep", "character_introductions": [
        {"name": name, "introduction_required": True} for name in ["Son-Goku", "Son Goku", "Son Gohan"]]}]}
    normalized = e.normalize_character_introduction_contexts(project, research)
    assert e.normalize_character_introduction_contexts(project, normalized) == normalized
    assert [x["name"] for x in normalized["episode_contexts"][0]["character_introductions"]
            if x["introduction_required"]] == ["Son Goku", "Son Gohan"]


def test_missing_introduction_endpoint_starts_real_repair_scope(monkeypatch):
    project = {"id": "project", "cues": [{"id": "ordinary"}], "mapping": [],
               "quality_report": {"checks": [{"id": "character_introduction_timing", "status": "failed", "detail": "Upa: 0 formale Zuordnungen statt genau einer"}]}}
    monkeypatch.setattr(e, "project_or_404", lambda *args: project)
    monkeypatch.setattr(e, "active_project_run", lambda *args: None)
    monkeypatch.setattr(e, "save_state", lambda: None)
    monkeypatch.setattr(e, "STATE", {"runs": {}})
    calls = []
    class Thread:
        def __init__(self, **kwargs):
            calls.append(kwargs)
        def start(self):
            pass
    monkeypatch.setattr(e.threading, "Thread", Thread)
    user = e.User(username="test", display_name="Test", roles=["editor"], user_id=7)
    scope = e.quality_repair_scope("project", user)
    assert scope["available"] and scope["adds_introductions"]
    run = e.repair_from_quality_report("project", e.QualityRepairRequest(), user)
    assert run["quality_repair"]
    assert calls[0]["args"][-1] is True
    assert calls[0]["target"] is e.rewrite_narration


def test_first_introduction_at_speech_safe_book_boundary_is_not_early(monkeypatch):
    monkeypatch.setattr(e, "source_content_fragments", lambda project: [{"id": "f"}])
    monkeypatch.setattr(e, "merged_speech_intervals", lambda project: {"ep": [(730, 1500)]})
    cue = {"id": "intro", "source_fragment_ids": ["f"], "anchor_episode_id": "ep",
           "anchor_segment_id": "s", "anchor_episode_start_ms": 1000,
           "narrative_purpose": "character_introduction", "alignment_placement": "before_anchor",
           "placement_policy": e.ALIGNED_SCENE_INSERT_POLICY,
           "placement_sample_in_episode": 610 * 48, "timeline_start_sample": 48000,
           "text": "Mai, die Frau mit dunklen Haaren, trat herein."}
    project = {"cues": [cue], "mapping": [{"episode_id": "ep"}],
               "reconciled_transcript": [{"id": "s", "episode_id": "ep", "episode_start_ms": 1000, "episode_end_ms": 1500, "text": "Mai!"}],
               "episode_context_research": {"episode_contexts": [{"episode_id": "ep", "character_introductions": [{"name": "Mai", "introduction_required": True}]}]},
               "character_introductions": [{"research_name": "Mai", "spoken_name": "Mai", "cue_id": "intro", "episode_id": "ep"}]}
    timeline = {"clips": [{"track_id": "trk_narrator", "cue_id": "intro", "timeline_start_sample": 48000,
                           "strategy": e.ALIGNED_SCENE_INSERT_POLICY, "source_start_sample": 0, "source_end_sample": 240000}]}
    passed, detail = e._character_introduction_timing_result(project, timeline)
    assert passed, detail


def test_forced_introduction_candidate_does_not_compete_with_editable_existing_intro():
    cue = {"id": "existing", "source_fragment_ids": ["f"], "anchor_episode_id": "ep", "anchor_segment_id": "s"}
    project = {"id": "p", "mapping": [{"episode_id": "ep"}],
               "chapters": [{"fragments": [{"id": "f", "text": "Mai öffnete die rote Tür."}]}],
               "reconciled_transcript": [{"id": "s", "episode_id": "ep", "episode_start_ms": 1000, "episode_end_ms": 2000, "text": "Mai, komm herein!"}],
               "cues": [cue], "character_introductions": [{"research_name": "Mai", "cue_id": "existing", "episode_id": "ep"}]}
    research = {"episode_contexts": [{"episode_id": "ep", "character_introductions": [
        {"name": "Mai", "introduction_required": True, "visual_description": "Dunkle Haare und grüne Uniform."}]}]}
    assert e.add_dedicated_character_introduction_candidates(project, [cue], research, force=True) == [cue]
    missing = {**project, "character_introductions": []}
    assert len(e.add_dedicated_character_introduction_candidates(missing, [cue], research, force=True)) == 2


def test_researched_introduction_does_not_require_novel_picture_words():
    cue = {"narrative_purpose": "character_introduction", "source_detail_reference_count": 9,
           "preserved_concrete_detail_count": 0, "source_detail_coverage": 0,
           "native_audio_relation": "complements_existing_audio",
           "retained_visual_details": ["schwarzes Haar", "grüne Uniform"]}
    assert e.cue_novelistic_metadata_complete(cue, .22)
    assert not e.cue_novelistic_metadata_complete({**cue, "narrative_purpose": "visual_action"}, .22)
    assert not e.cue_novelistic_metadata_complete({**cue, "retained_visual_details": []}, .22)


def test_coverage_repair_can_reposition_selected_cue_that_blocks_gap_closure():
    project = {"narration_rules": {"narration_max_gap_ms": 150000},
               "transcript": [{"id": f"s{i}", "episode_start_ms": ms}
                              for i, ms in enumerate([0, 190000, 200000, 300000])]}
    targets = {"ep": {"min_cues": 4, "candidate_anchors": {"a": 0, "b": 100000, "c": 200000, "d": 300000}}}
    locked = {cue: {"anchor_segment_id": f"s{i}"} for i, cue in enumerate("abcd")}
    assert e.coverage_gap_repair_ids(project, targets, locked) == {"b"}


def test_coverage_gate_detects_selected_cue_displaced_from_its_source_window():
    project = {"narration_density": "audio_drama", "narration_rules": {"narration_max_gap_ms": 150000},
               "narration_density_contract": {"coverage_targets": {"ep": {
                   "candidate_anchors": {"a": 0, "b": 100000, "c": 200000, "d": 300000}}}},
               "cues": [{"id": cue, "anchor_episode_id": "ep", "anchor_episode_start_ms": ms}
                        for cue, ms in zip("abcd", [0, 190000, 200000, 300000])]}
    assert not e._narration_coverage_result(project)[0]
    project["cues"][1]["anchor_episode_start_ms"] = 100000
    assert e._narration_coverage_result(project)[0]


def test_context_research_carries_cast_across_episode_batches(monkeypatch):
    monkeypatch.setenv('EPISODE_CONTEXT_BATCH', '1')
    monkeypatch.setattr(e, 'mapped_episode_descriptors', lambda project: [{'id': 'early'}, {'id': 'late'}])
    requests = []
    def research(project, episodes, known_characters=None):
        requests.append(deepcopy(known_characters))
        return {'episode_contexts': [{'episode_id': episodes[0]['id'], 'character_introductions': [
            {'name': 'Krillin', 'role': 'Freund', 'visual_description': 'Sechs Stirnpunkte',
             'introduction_required': True},
            {'name': 'Lunch', 'role': 'Erwähnt', 'introduction_required': False}]}]}
    monkeypatch.setattr(e, '_research_episode_context_batch', research)
    project = {'binding': {'title': 'Serie'}, 'narration_name_lexicon': [
        {'canonical': 'Krillin', 'aliases': ['Kuririn']}]}
    e.research_episode_context(project)
    assert requests[0][0]['aliases'] == ['Kuririn']
    assert requests[0][0]['introduced_in_episode'] == ''
    assert requests[1][0]['name'] == 'Krillin'
    assert requests[1][0]['introduced_in_episode'] == 'early'
    assert requests[1][0]['visual_description'] == 'Sechs Stirnpunkte'
    assert requests[1][1]['name'] == 'Lunch'
    assert requests[1][1]['introduced_in_episode'] == ''


def test_explicitly_unverified_character_research_is_not_reused():
    project = {'mapping': [{'episode_id': 'ep'}]}
    character = {'name': 'Dracula', 'introduction_required': True,
                 'visual_description': 'Fledermaus; weitere sichtbare Einzelmerkmale konnten nicht verifiziert werden.'}
    research = {'episode_contexts': [{'episode_id': 'ep', 'synopsis': 'Kampf',
                'sources': [{'url': 'https://example.com/episode'}], 'character_introductions': [character]}]}
    assert not e.episode_context_research_is_current(project, research)
    character['visual_description'] = 'Unsichtbar; weder Körper noch Kleidung sichtbar.'
    assert e.episode_context_research_is_current(project, research)


def test_coverage_does_not_count_a_cues_own_speech_safe_shift_as_missing_beat():
    project = {'narration_density': 'audio_drama', 'cues': [
        {'id': key, 'anchor_episode_id': 'ep', 'anchor_episode_start_ms': ms,
         'placement_sample_in_episode': (ms - 120) * e.SAMPLE_RATE // 1000}
        for key, ms in [('a', 100000), ('b', 300000), ('c', 500000)]],
        'narration_density_contract': {'coverage_targets': {
            'ep': {'candidate_anchors': {'a': 100000, 'b': 300000, 'c': 500000}}}}}
    assert e._narration_coverage_result(project)[0]


def test_grounding_failure_targets_source_less_cue_without_report_ids():
    project = {'mapping': [{'episode_id': 'ep'}],
        'cues': [{'id': 'bad', 'anchor_episode_id': 'ep', 'anchor_segment_id': 's', 'confidence': .9}],
        'quality_report': {'checks': [{'id': 'narration_grounding', 'status': 'failed', 'detail': '1 Cue ohne Beleg'}]}}
    assert e.quality_repair_targets(project)[0] == {'bad'}


def test_obsolete_source_less_intro_candidates_are_omitted_from_repair():
    research = {'episode_contexts': [{'episode_id': 'early', 'character_introductions': [
        {'name': 'Mara', 'introduction_required': True}]}, {'episode_id': 'late', 'character_introductions': [
        {'name': 'Mara', 'introduction_required': True}]}]}
    orphan = {'id': 'orphan', 'anchor_episode_id': 'late', 'source_fragment_ids': [],
              'character_introduction_candidate': True, 'introduction_research_name': 'Mara'}
    first = {**orphan, 'id': 'first', 'anchor_episode_id': 'early'}
    sourced = {**orphan, 'id': 'sourced', 'source_fragment_ids': ['f']}
    project = {'mapping': [{'episode_id': 'early'}, {'episode_id': 'late'}],
               'episode_context_research': research, 'cues': [orphan, first, sourced]}
    omitted = e.obsolete_introduction_candidate_ids(project, research)
    assert omitted == {'orphan'}
    raw = [{**first, 'id': 'raw-first'}, {**sourced, 'id': 'raw-sourced'}]
    repair, locks, _ = e.locked_alignments_for_repair(project, raw, {'orphan'}, omit_cue_ids=omitted)
    assert len(raw) == 2
    assert not repair
    assert len(locks) == 2


def test_coverage_source_beat_is_covered_by_other_cue_at_same_anchor():
    project = {'narration_density': 'audio_drama', 'cues': [
        {'id': 'a', 'anchor_episode_id': 'ep', 'anchor_episode_start_ms': 100000, 'placement_sample_in_episode': 99900*48},
        {'id': 'b', 'anchor_episode_id': 'ep', 'anchor_episode_start_ms': 300000, 'placement_sample_in_episode': 299900*48}],
        'narration_density_contract': {'coverage_targets': {'ep': {
            'candidate_anchors': {'a': 100000, 'b': 300000, 'intro': 100000}}}}}
    assert e._narration_coverage_result(project)[0]
    project['narration_density_contract']['coverage_targets']['ep']['candidate_anchors']['intro'] = 200000
    assert not e._narration_coverage_result(project)[0]
    project['narration_density_contract']['coverage_targets']['ep']['book_edge_episode'] = 'end'
    assert e._narration_coverage_result(project)[0]


def test_coverage_repair_uses_rendered_positions_when_available():
    project = {'transcript': [{'id': 'sa', 'episode_start_ms': 0}, {'id': 'sb', 'episode_start_ms': 149900}],
               'cues': [{'anchor_segment_id': 'sa', 'placement_sample_in_episode': 0},
                        {'anchor_segment_id': 'sb', 'placement_sample_in_episode': 150100*48}]}
    target = {'ep': {'min_cues': 2, 'candidate_anchors': {'a': 0, 'b': 149900, 'filler': 75000}}}
    assert 'filler' in e.coverage_gap_repair_ids(project, target,
        {'a': {'anchor_segment_id': 'sa'}, 'b': {'anchor_segment_id': 'sb'}})


def test_coverage_repair_preserves_locked_book_end_territory(monkeypatch):
    monkeypatch.setattr(e, 'source_content_fragments', lambda project: [{'id': 'first'}, {'id': 'last'}])
    project = {'transcript': [{'id': 'a', 'episode_start_ms': 0}, {'id': 'end', 'episode_start_ms': 1000000}],
               'cues': [{'anchor_segment_id': 'end', 'source_fragment_ids': ['last']}]}
    targets = {'ep': {'min_cues': 2, 'book_edge_episode': 'end',
                     'candidate_anchors': {'a': 0, 'middle': 500000, 'end': 1000000}}}
    assert not e.coverage_gap_repair_ids(project, targets, {'a': {'anchor_segment_id': 'a'}, 'end': {'anchor_segment_id': 'end'}})


def test_valid_spelling_extension_is_not_a_repeated_name_artifact():
    entries = e.normalize_narration_name_lexicon([
        {'canonical': 'Oberst Violet', 'aliases': ['Oberst Violett']},
        {'canonical': 'Tao Baibai', 'aliases': ['Tao Baibai Baibai', 'Tao Pai Pai']},
    ])
    assert entries[0]['canonical'] == 'Oberst Violet'
    assert entries[0]['aliases'] == ['Oberst Violett']
    assert entries[1]['aliases'] == ['Tao Pai Pai']


def test_synthetic_intro_hint_is_not_an_action_coverage_candidate():
    cues = [
        {'id': 'a', 'anchor_episode_id': 'ep', 'anchor_episode_start_ms': 100000},
        {'id': 'intro', 'anchor_episode_id': 'ep', 'anchor_episode_start_ms': 200000,
         'character_introduction_candidate': True},
        {'id': 'b', 'anchor_episode_id': 'ep', 'anchor_episode_start_ms': 300000},
    ]
    targets = e.narration_coverage_targets({}, cues)
    assert targets['ep']['candidate_count'] == 3
    assert set(targets['ep']['coverage_candidate_ids']) == {'a', 'b'}
    assert targets['ep']['candidate_anchor_ms'] == [100000, 300000]
    project = {'narration_density': 'audio_drama', 'cues': [
        {**cues[0], 'placement_sample_in_episode': 100000*48},
        {**cues[2], 'placement_sample_in_episode': 300000*48}],
        'narration_density_contract': {'coverage_targets': targets}}
    assert e._narration_coverage_result(project)[0]


def test_project_list_does_not_revalidate_quality_and_is_owner_scoped(monkeypatch):
    from types import SimpleNamespace
    owned = {'id': 'owned', 'owner_user_id': 2, 'updated_at': '2026-09-10',
             'quality_report': {'evaluator': 'old'}, 'cues': [{'id': 'cue'}]}
    foreign = {'id': 'foreign', 'owner_user_id': 3, 'updated_at': '2026-09-11'}
    monkeypatch.setitem(e.STATE, 'projects', {'owned': owned, 'foreign': foreign})
    def unexpected(*args, **kwargs):
        raise AssertionError('Project overview must not evaluate or persist quality reports')
    monkeypatch.setattr(e, 'ensure_project_quality_state', unexpected)
    monkeypatch.setattr(e, 'save_state', unexpected)
    result = e.list_projects(SimpleNamespace(user_id=2, username='test', is_admin=False))
    assert [item['id'] for item in result] == ['owned']
    assert result[0]['cue_count'] == 1
    assert result[0]['quality_report'] == {'evaluator': 'old'}


def _comment_clip(start_ms, end_ms, cue_id='cue'):
    return {'track_id': 'trk_narrator', 'cue_id': cue_id,
            'timeline_start_sample': start_ms * 48,
            'source_start_sample': 0, 'source_end_sample': (end_ms - start_ms) * 48}


def test_rendered_coverage_rejects_gap_without_any_source_candidate():
    project = {'narration_density': 'audio_drama', 'timeline': {
        'duration_samples': 650000 * 48,
        'clips': [_comment_clip(0, 10000, 'a'), _comment_clip(635000, 650000, 'b')]}}
    ok, detail = e._narration_coverage_result(project)
    assert not ok
    assert '625' in detail


def test_rendered_coverage_counts_comment_ends_overlaps_and_both_book_edges():
    timeline = {'duration_samples': 510001 * 48, 'clips': [
        _comment_clip(160000, 190000, 'a'), _comment_clip(180000, 200000, 'b'),
        _comment_clip(350000, 360000, 'c')]}
    gaps = e.rendered_narration_gap_measurements(timeline, 150000)
    assert gaps['longest_gap_ms'] == 160000
    assert [(g['start_ms'], g['end_ms']) for g in gaps['gaps']] == [(0, 160000), (360000, 510001)]
    assert gaps['gaps'][1]['before_cue_id'] == 'c'


def test_rendered_coverage_uses_new_timeline_instead_of_old_project_timeline():
    project = {'narration_density': 'audio_drama', 'timeline': {
        'duration_samples': 100000 * 48, 'clips': [_comment_clip(0, 100000)]}}
    new_timeline = {'duration_samples': 400000 * 48, 'clips': [
        _comment_clip(0, 20000, 'a'), _comment_clip(380000, 400000, 'b')]}
    assert e._narration_coverage_result(project)[0]
    assert not e._narration_coverage_result(project, new_timeline)[0]


def test_actual_gap_creates_source_backed_candidate_without_old_candidate_hint():
    project = {'narration_density': 'audio_drama', 'chapters': [
        {'id': 'ch', 'title': 'Chapter', 'fragments': [{'id': 'f', 'text': 'Er überquert den steinernen Hof.'}]}],
        'cues': [{'id': 'a', 'anchor_episode_id': 'ep', 'anchor_segment_id': 'a', 'source_fragment_ids': ['f']},
                 {'id': 'b', 'anchor_episode_id': 'ep', 'anchor_segment_id': 'b', 'source_fragment_ids': ['f']}],
        'transcript': [{'id': 'middle', 'episode_id': 'ep', 'episode_start_ms': 120000, 'episode_end_ms': 121000, 'text': 'Komm her!'}],
        'timeline': {'duration_samples': 270000 * 48, 'clips': [
            _comment_clip(0, 10000, 'a'), _comment_clip(260000, 270000, 'b'),
            {'track_id': 'trk_episode', 'episode_id': 'ep', 'timeline_start_sample': 10000 * 48,
             'source_start_sample': 0, 'source_end_sample': 250000 * 48}]}}
    candidates = e.timeline_gap_repair_candidates(project)
    assert len(candidates) == 1
    assert candidates[0]['source_fragment_ids'] == ['f']
    assert candidates[0]['anchor_segment_id'] == 'middle'
    assert candidates[0]['coverage_repair_window'] == {'start_ms': 117000, 'end_ms': 123000}


def test_required_introductions_survive_shared_first_subtitle_hint():
    names = ['Anna', 'Berta', 'Clara', 'Dora']
    project = {'id': 'p', 'mapping': [{'episode_id': 'ep'}], 'chapters': [],
               'reconciled_transcript': [
                   {'id': f's{i}', 'episode_id': 'ep', 'episode_start_ms': 1000+i*1000,
                    'episode_end_ms': 1500+i*1000, 'text': 'Anna Berta Clara Dora' if i == 0 else 'Kommt herein!'}
                   for i in range(4)]}
    research = {'episode_contexts': [{'episode_id': 'ep', 'character_introductions': [
        {'name': name, 'introduction_required': True, 'visual_description': 'Dunkle Haare und grüne Uniform.'}
        for name in names]}]}
    candidates = e.add_dedicated_character_introduction_candidates(project, [], research, force=True)
    assert {c['introduction_research_name'] for c in candidates} == set(names)
    assert len({c['anchor_segment_id'] for c in candidates}) == 4
    assert all(c['source_fragment_ids'] == [] for c in candidates)


def test_gap_source_context_does_not_duplicate_book_boundary_responsibility():
    cue = {'source_fragment_ids': ['first']}
    assert e.scene_alignment_book_edge(cue, 'first', 'last') == 'start'
    cue['coverage_repair_window'] = {'start_ms': 1000, 'end_ms': 2000}
    assert e.scene_alignment_book_edge(cue, 'first', 'last') == 'none'
    assert e.scene_alignment_book_edge({'source_fragment_ids': ['last']}, 'first', 'last') == 'end'


def test_gap_candidates_include_source_passages_between_retained_neighbours():
    fragments = [{'id': name, 'text': name + ' konkrete sichtbare Handlung.'} for name in ['outside', 'left', 'middle', 'right', 'after']]
    project = {'narration_density': 'audio_drama', 'chapters': [{'id': 'ch', 'title': 'Chapter', 'fragments': fragments}],
               'cues': [{'id': 'a', 'anchor_episode_id': 'ep', 'source_fragment_ids': ['left']},
                        {'id': 'b', 'anchor_episode_id': 'ep', 'source_fragment_ids': ['right']}],
               'transcript': [{'id': 'middle-anchor', 'episode_id': 'ep', 'episode_start_ms': 120000, 'episode_end_ms': 121000, 'text': 'Komm her!'}],
               'timeline': {'duration_samples': 270000*48, 'clips': [
                   _comment_clip(0, 10000, 'a'), _comment_clip(260000, 270000, 'b'),
                   {'track_id': 'trk_episode', 'episode_id': 'ep', 'timeline_start_sample': 10000*48, 'source_start_sample': 0, 'source_end_sample': 250000*48}]}}
    candidates = e.timeline_gap_repair_candidates(project)
    assert candidates[0]['source_fragment_ids'] == ['left', 'middle', 'right']
    assert 'middle konkrete sichtbare Handlung.' in candidates[0]['editorial_source_context']
    assert 'outside' not in candidates[0]['editorial_source_context']
    assert 'after' not in candidates[0]['editorial_source_context']


def test_editorial_accepts_subtitle_backed_dub_name_and_rejects_missing_clip(monkeypatch):
    monkeypatch.setattr(e, "source_content_fragments", lambda project: [{"id": "f"}])
    monkeypatch.setattr(e, "merged_speech_intervals", lambda project: {"ep": [(730, 1500)]})
    cue = {"id": "intro", "source_fragment_ids": ["f"], "anchor_episode_id": "ep",
           "anchor_segment_id": "s", "anchor_episode_start_ms": 1000,
           "narrative_purpose": "character_introduction", "alignment_placement": "before_anchor",
           "placement_policy": e.ALIGNED_SCENE_INSERT_POLICY,
           "placement_sample_in_episode": 610 * 48, "timeline_start_sample": 48000,
           "text": "Krillin, die Frau mit dunklen Haaren, trat herein."}
    project = {"cues": [cue], "mapping": [{"episode_id": "ep"}],
               "reconciled_transcript": [{"id": "s", "episode_id": "ep", "episode_start_ms": 1000, "episode_end_ms": 1500, "text": "Krillin!"}],
               "episode_context_research": {"episode_contexts": [{"episode_id": "ep", "character_introductions": [{"name": "Kuririn", "introduction_required": True}]}]},
               "character_introductions": [{"research_name": "Kuririn", "spoken_name": "Krillin", "cue_id": "intro", "episode_id": "ep"}]}
    timeline = {"clips": [{"track_id": "trk_narrator", "cue_id": "intro", "timeline_start_sample": 48000,
                           "strategy": e.ALIGNED_SCENE_INSERT_POLICY, "source_start_sample": 0, "source_end_sample": 240000}]}
    passed, detail = e._character_introduction_timing_result(project, timeline)
    assert passed, detail

    _, editorial_detail = e._editorial_quality_result(project, passed)
    assert "Figuren-Einführungen" not in editorial_detail
    failed, _ = e._character_introduction_timing_result(project, {"clips": []})
    assert not failed
    _, editorial_detail = e._editorial_quality_result(project, failed)
    assert "Figuren-Einführungen" in editorial_detail


# --- Folgenkontext-Recherche: Sackgasse vom 2026-09-11 -----------------------
#
# Der Lauf scheiterte an "Jeder Episodenkontext benötigt mindestens eine
# Quelle", nachdem alle 14 Versuche verbraucht waren. Der Claude-Provider kam
# nie ins Netz: der Agent bot `--tools WebSearch,WebFetch` an, erlaubte sie
# aber nicht, also lehnte `--permission-mode dontAsk` jeden Aufruf ab und das
# Modell antwortete mit leerer Quellenliste. Das Mapping überlebte das, weil
# sein Validator leere Quellen toleriert; der Folgenkontext, der pro Folge eine
# prüfbare URL verlangt, war damit unerfüllbar.


def test_live_web_search_tools_are_allowed_not_just_offered():
    runner = (REPO_ROOT / "vocarium-agent" / "runner.py").read_text()
    command = runner.split("def build_claude_command", 1)[1].split("\ndef ", 1)[0]
    assert '"--tools", "WebSearch,WebFetch" if web_search == "live" else ""' in command
    # Verfügbarkeit ist nicht Freigabe: ohne --allowedTools verbucht die CLI je
    # Aufruf ein permission_denial und die Antwort trägt keine Quelle.
    assert 'command.extend(["--allowedTools", "WebSearch,WebFetch"])' in command


def test_schema_requires_a_source_per_episode():
    """Die CLI muss leere Quellen schon bei der Erzeugung zurückweisen.

    Der Python-Validator verlangte immer eine Quelle je Folge, das an
    `--json-schema` übergebene Schema nicht. Eine Antwort mit `"sources": []`
    bestand deshalb die strukturierte Ausgabe und verbrannte danach einen der
    14 Reparaturversuche. Beide Verträge sagen jetzt dasselbe.
    """
    schema = json.loads((REPO_ROOT / "vocarium-agent" / "episode_context.schema.json").read_text())
    contexts = schema["properties"]["episode_contexts"]
    assert contexts["minItems"] == 1
    assert contexts["items"]["properties"]["sources"]["minItems"] == 1


def _episode_descriptors(count):
    return [{"id": f"e{i}", "season": 1, "episode": i, "title": f"T{i}",
             "summary": "", "originally_available_at": None} for i in range(1, count + 1)]


def test_finished_batches_survive_a_failing_batch(monkeypatch):
    """Ein später Fehlschlag darf die fertigen Pakete nicht verwerfen."""
    episodes = _episode_descriptors(4)
    source = [{"url": "https://example.org/a", "title": "A", "claim": "c"}]
    calls = []

    def research(project, batch, known):
        calls.append([item["id"] for item in batch])
        if len(calls) == 2:
            raise RuntimeError("Agent weg")
        return {"series_title": "Serie", "episode_contexts": [
            {"episode_id": item["id"], "synopsis": "S", "continuity_before": "",
             "major_beats": [], "character_introductions": [], "sources": source}
            for item in batch]}

    monkeypatch.setattr(e, "mapped_episode_descriptors", lambda project: episodes)
    monkeypatch.setattr(e, "_research_episode_context_batch", research)
    monkeypatch.setenv("EPISODE_CONTEXT_BATCH", "2")

    project = {"binding": {"title": "Serie"}, "narration_name_lexicon": []}
    stored = []
    with pytest.raises(RuntimeError):
        e.research_episode_context(project, persist=stored.append)

    # Paket eins liegt gesichert vor, bevor Paket zwei überhaupt versucht wird.
    assert len(stored) == 1
    assert [c["episode_id"] for c in stored[0]["episode_contexts"]] == ["e1", "e2"]

    # Der Wiederholungslauf setzt fort, statt die ersten beiden neu zu suchen.
    project["episode_context_research"] = stored[-1]
    calls.clear()
    merged = e.research_episode_context(project)
    assert calls == [["e3", "e4"]]
    assert [c["episode_id"] for c in merged["episode_contexts"]] == ["e1", "e2", "e3", "e4"]


@pytest.mark.parametrize("broken", [[], [{"url": "keine-url", "title": "A", "claim": "c"}]])
def test_unsourced_entries_are_researched_again(monkeypatch, broken):
    """Genau die Einträge, die der Agent ablehnen würde, dürfen nicht zählen."""
    episodes = _episode_descriptors(1)
    calls = []

    def research(project, batch, known):
        calls.append([item["id"] for item in batch])
        return {"series_title": "Serie", "episode_contexts": [
            {"episode_id": "e1", "synopsis": "S", "continuity_before": "", "major_beats": [],
             "character_introductions": [],
             "sources": [{"url": "https://example.org/a", "title": "A", "claim": "c"}]}]}

    monkeypatch.setattr(e, "mapped_episode_descriptors", lambda project: episodes)
    monkeypatch.setattr(e, "_research_episode_context_batch", research)

    e.research_episode_context({
        "binding": {"title": "Serie"}, "narration_name_lexicon": [],
        "episode_context_research": {"episode_contexts": [
            {"episode_id": "e1", "synopsis": "S", "sources": broken}]},
    })
    assert calls == [["e1"]]


def _turn_budget():
    """Lädt claude_turn_budget aus dem Agenten, ohne dessen Modul zu importieren.

    runner.py läuft im Agenten-Image und bringt Abhängigkeiten mit, die im
    API-Image fehlen; der Helfer selbst hängt nur an os.environ.
    """
    import os

    source = (REPO_ROOT / "vocarium-agent" / "runner.py").read_text("utf-8")
    start = source.index("def claude_turn_budget(")
    end = source.index("def build_claude_command(")
    namespace: dict = {"os": os}
    exec(source[start:end], namespace)
    return namespace["claude_turn_budget"]


def test_live_research_turn_budget_scales_with_the_batch(monkeypatch):
    """Ein Festbudget von 12 Runden brach jede echte Recherche ab.

    Gemessen am 2026-09-12 mit dem echten Folgenkontext-Prompt: eine einzige
    Folge verbrauchte 47 Runden für 18 geprüfte Quellen. Sobald die Werkzeuge
    per --allowedTools wirklich freigegeben sind, endet ein zu kleines Budget
    in `error_max_turns` statt in einem Ergebnis.
    """
    monkeypatch.delenv("CLAUDE_MAX_TURNS", raising=False)
    budget = _turn_budget()

    # Eine Folge braucht messbar mehr als das frühere Festbudget.
    assert int(budget(web_search="live", research_units=1)) > 47
    # Zwei Folgen (EPISODE_CONTEXT_BATCH-Standard) brauchen mehr als eine.
    assert int(budget(web_search="live", research_units=2)) > int(
        budget(web_search="live", research_units=1)
    )
    # Die Obergrenze hält den Lauf unter dem Recherchezeitlimit von 3600 s.
    assert int(budget(web_search="live", research_units=50)) == 240
    # Ohne Websuche bleibt es beim knappen Budget für die reine Ausgabe.
    assert budget(web_search="off", research_units=8) == "6"


def test_turn_budget_stays_overridable(monkeypatch):
    """docker-compose.yml reicht CLAUDE_MAX_TURNS durch; das muss greifen."""
    monkeypatch.setenv("CLAUDE_MAX_TURNS", "33")
    budget = _turn_budget()
    assert budget(web_search="live", research_units=2) == "33"
    assert budget(web_search="off", research_units=1) == "33"


def test_research_units_reach_the_claude_command():
    """Beide Live-Recherchen müssen ihre Paketgröße durchreichen."""
    source = (REPO_ROOT / "vocarium-agent" / "runner.py").read_text("utf-8")
    assert 'research_units=len(payload.get("chapters") or ())' in source
    assert 'research_units=len(payload.get("episodes") or ())' in source
    assert "claude_turn_budget(web_search=web_search, research_units=research_units)" in source


def _failure_detail():
    """Lädt agent_failure_detail samt CLI-Berichtsauswertung isoliert."""
    import json as _json
    import os
    import re as _re
    import subprocess as _subprocess

    source = (REPO_ROOT / "vocarium-agent" / "runner.py").read_text("utf-8")
    start = source.index("CLI_FAILURE_HINTS = {")
    end = source.index("def codex_failure_detail(")
    namespace: dict = {
        "json": _json, "re": _re, "os": os, "subprocess": _subprocess,
        "bounded_string": lambda value, limit: (str(value)[:limit] if value is not None else ""),
    }
    exec(source[start:end], namespace)
    return namespace["agent_failure_detail"]


class _Completed:
    def __init__(self, stdout="", stderr="", returncode=1):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def test_cli_report_names_the_real_cause():
    """Der Abschlussbericht der CLI muss lesbar werden, nicht abgeschnitten.

    Er ist eine einzige lange Zeile ohne "message"-Feld; die alte Auswertung
    kürzte sie nach 500 Zeichen ab und verbarg damit `subtype`, `num_turns`
    und `permission_denials`.
    """
    detail = _failure_detail()

    exhausted = detail(_Completed(stdout=json.dumps({
        "type": "result", "subtype": "error_max_turns", "is_error": True,
        "num_turns": 13, "result": None, "permission_denials": [],
    })), "claude")
    assert "Rundenbudget" in exhausted and "13 Runden" in exhausted

    denied = detail(_Completed(stdout=json.dumps({
        "type": "result", "subtype": "error_during_execution", "num_turns": 4,
        "permission_denials": [{"tool_name": "WebSearch"}, {"tool_name": "WebFetch"}],
    })), "claude")
    assert "WebSearch" in denied and "WebFetch" in denied


def test_existing_failure_paths_are_unchanged():
    """Textfehler und Exit-Codes müssen weiterhin wie bisher gemeldet werden."""
    detail = _failure_detail()
    assert detail(_Completed(stderr='{"error":{"message":"Ungültiger Schlüssel"}}'), "claude") == (
        "Ungültiger Schlüssel"
    )
    assert detail(_Completed(stderr="boom\nletzte Zeile"), "claude") == "letzte Zeile"
    assert detail(_Completed(returncode=7), "Codex") == "Codex-Lauf endete mit Exit-Code 7"
