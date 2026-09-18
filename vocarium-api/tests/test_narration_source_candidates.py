"""A chapter-to-episode mapping is not a measured fragment boundary."""
from hoerspiele.engine import generate_narration_cues


def test_shared_chapter_follows_unequal_subtitle_evidence_boundary():
    fragments = [
        {"id": f"f{i}", "order": i + 1,
         "text": text}
        for i, text in enumerate([
            "Der Ninja wirft die weiße Rauchbombe zwischen die Büsche. Der Junge duckt sich hinter einen breiten Felsen.",
            "Zwei runde Holzscheiben tragen den Ninja über das Wasser. Der Junge springt mit ausgestreckten Armen über den Teich.",
            "Fünf Männer umringen den Jungen mit ihren blanken Waffen. Ein roter Stab schlägt die scharfe Klinge beiseite.",
            "Der Ninja öffnet das schwere Gitter aus schwarzem Metall. Dahinter sitzt ein großer Mann mit breiten Schultern.",
        ])
    ]
    project = {
        "chapters": [{"id": "chapter", "title": "Kapitel 62", "fragments": fragments}],
        "mapping": [{"chapter_id": "chapter", "episode_id": ep} for ep in ["ep1", "ep2"]],
        "transcript": [{"id": f"{ep}-s{i}", "episode_id": ep,
                        "episode_start_ms": i * 20000 + 1000, "episode_end_ms": i * 20000 + 4000,
                        "text": ("Rauchbombe, Büsche, weißer Rauch, breiter Felsen." if ep == "ep1" else "Holzscheiben über dem Wasser. Teich, Sprung. Fünf Männer und blanke Waffen. Roter Stab, scharfe Klinge. Schweres Gitter aus schwarzem Metall, breite Schultern.")}
                       for ep in ["ep1", "ep2"] for i in range(30)],
        "media_assets": [{"episode_id": ep, "duration_ms": 600000} for ep in ["ep1", "ep2"]],
    }
    cues = generate_narration_cues(project, "audio_drama")
    for ep in ["ep1", "ep2"]:
        represented = {fragment_id for cue in cues if cue["anchor_episode_id"] == ep
                       for fragment_id in cue["source_fragment_ids"]}
        assert represented == ({"f0"} if ep == "ep1" else {"f1", "f2", "f3"})


def test_ambiguous_subtitles_keep_all_fragments_once_in_order():
    from hoerspiele.engine import source_fragment_groups_for_episodes
    fragments = [{"id": str(i), "text": "Der Junge läuft über den grünen Boden."} for i in range(7)]
    project = {"transcript": [{"episode_id": ep, "episode_start_ms": 1000, "episode_end_ms": 2000, "text": "Der Junge läuft über den grünen Boden."} for ep in ["a", "b", "c"]]}
    groups = source_fragment_groups_for_episodes(project, fragments, ["a", "b", "c"])
    assert [item for group in groups for item in group] == fragments
    assert [len(group) for group in groups] == [2, 3, 2]


def test_missing_transcript_does_not_invent_an_episode_boundary():
    from hoerspiele.engine import source_fragment_groups_for_episodes
    fragments = [{"id": str(i), "text": "Holzscheiben und Piranhas im Wasser."} for i in range(4)]
    groups = source_fragment_groups_for_episodes({}, fragments, ["a", "b"])
    assert groups == [fragments[:2], fragments[2:]]
