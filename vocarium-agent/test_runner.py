import json
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runner
from runner import (
    AlignmentValidationError,
    _oversized_coverage_gap,
    coverage_gap_speech_ratio,
    build_claude_command,
    build_codex_command,
    character_reference_matches,
    exact_character_name_match,
    clean_scene_alignment_seed,
    dialogue_bridge_from_source,
    codex_failure_detail,
    evidence_contains_name,
    execution_metadata,
    execute_with_fallbacks,
    filter_grounded_name_aliases,
    missing_source_detail_tokens,
    measured_source_detail_coverage,
    native_audio_repetition_findings,
    native_context_for_alignment,
    normalized_evidence_text,
    reconcile_grounded_character_introductions,
    repair_researched_character_introductions,
    requires_visual_source_coverage,
    research_scene_alignment,
    save_zai_credentials,
    salvage_optional_audio_drama_result,
    scene_alignment_model_payload,
    update_login_session_output,
    uncovered_causal_visual_endpoint,
    uncovered_concrete_source_passages,
    validate_episode_context_result,
    validate_alignment_density,
    validation_failure_summary,
    visual_detail_match_count,
    validated_alignment_request,
    validated_episode_context_request,
    validated_execution_settings,
    validated_execution_chain,
)


class CodexFailureDetailTest(unittest.TestCase):
    def test_uses_structured_error_message_when_stderr_is_only_whitespace(self):
        result = subprocess.CompletedProcess(
            args=["codex"],
            returncode=1,
            stdout='ERROR: {"error":{"message":"Schema wird nicht unterstützt"}}',
            stderr="\n",
        )

        self.assertEqual(codex_failure_detail(result), "Schema wird nicht unterstützt")

    def test_falls_back_to_exit_code_for_empty_output(self):
        result = subprocess.CompletedProcess(args=["codex"], returncode=7, stdout="", stderr="")

        self.assertEqual(codex_failure_detail(result), "Codex-Lauf endete mit Exit-Code 7")


class CodexExecutionSettingsTest(unittest.TestCase):
    def test_alignment_schema_requires_every_declared_property_for_codex_strict_json(self):
        schema = json.loads(Path(__file__).with_name("alignment.schema.json").read_text())
        alignment = schema["properties"]["alignments"]["items"]

        self.assertEqual(set(alignment["properties"]), set(alignment["required"]))

    def test_validates_managed_settings(self):
        self.assertEqual(
            validated_execution_settings({
                "model": "gpt-5.6",
                "reasoning_effort": "high",
                "timeout_seconds": 900,
            }),
            {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "high", "timeout_seconds": 900},
        )

    def test_rejects_unsafe_model_and_timeout(self):
        with self.assertRaisesRegex(ValueError, "nicht unterstützt"):
            validated_execution_settings({"model": "gpt-5.6; touch /tmp/nope"})
        with self.assertRaisesRegex(ValueError, "nicht unterstützt"):
            validated_execution_settings({"model": "gpt-5.4"})
        with self.assertRaisesRegex(ValueError, "zwischen 60 und 7200"):
            validated_execution_settings({"timeout_seconds": 30})
        with self.assertRaisesRegex(ValueError, "zwischen 60 und 7200"):
            validated_execution_settings({"timeout_seconds": 7201})
        # The hardest chunk of a book needs more than the old 30-minute cap.
        self.assertEqual(
            validated_execution_settings({"timeout_seconds": 3000})["timeout_seconds"],
            3000,
        )

    def test_builds_fixed_read_only_command_with_explicit_quality_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            command = build_codex_command(
                schema=root / "schema.json",
                output=root / "result.json",
                root=root,
                prompt_argument="-",
                execution={"model": "gpt-5.6-sol", "reasoning_effort": "xhigh", "timeout_seconds": 900},
                web_search="disabled",
            )

        self.assertIn("read-only", command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn('web_search="disabled"', command)
        self.assertIn("gpt-5.6-sol", command)
        self.assertIn('model_reasoning_effort="xhigh"', command)
        self.assertIn("--output-schema", command)
        self.assertEqual(command[-1], "-")

    def test_validates_claude_and_zai_profiles(self):
        self.assertEqual(
            validated_execution_settings({
                "provider": "zai",
                "model": "glm-5.2",
                "reasoning_effort": "xhigh",
                "timeout_seconds": 600,
            }),
            {"provider": "zai", "model": "glm-5.2", "reasoning_effort": "xhigh", "timeout_seconds": 600},
        )

    def test_validates_ordered_unique_provider_chain(self):
        chain = validated_execution_chain({
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "timeout_seconds": 900,
            "fallbacks": [
                {"provider": "claude", "model": "sonnet", "reasoning_effort": "high", "timeout_seconds": 600},
                {"provider": "zai", "model": "glm-5.1", "reasoning_effort": "high", "timeout_seconds": 600},
            ],
        })

        self.assertEqual([item["provider"] for item in chain], ["codex", "claude", "zai"])
        with self.assertRaisesRegex(ValueError, "nur einmal"):
            validated_execution_chain({
                "provider": "codex", "model": "gpt-5.6-sol",
                "fallbacks": [{"provider": "codex", "model": "gpt-5.6-terra"}],
            })

    def test_falls_back_and_records_only_safe_attempt_metadata(self):
        chain = [
            {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh", "timeout_seconds": 600},
            {"provider": "claude", "model": "sonnet", "reasoning_effort": "high", "timeout_seconds": 600},
        ]
        calls = []

        def run(payload, execution):
            calls.append(execution["provider"])
            if execution["provider"] == "codex":
                raise RuntimeError("429 secret-token-must-not-leak")
            return {"ok": True}

        result, metadata = execute_with_fallbacks({}, chain, run, "scene-alignment", "disabled")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, ["codex", "claude"])
        self.assertTrue(metadata["fallback_used"])
        self.assertEqual(metadata["attempts"][0]["reason"], "limit")
        self.assertNotIn("secret-token", str(metadata))

    def test_repairs_invalid_output_before_switching_provider(self):
        chain = [
            {"provider": "zai", "model": "glm-5.2", "reasoning_effort": "xhigh", "timeout_seconds": 600},
            {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh", "timeout_seconds": 600},
        ]
        payloads = []

        def run(payload, execution):
            payloads.append((execution["provider"], payload.get("_validation_retry")))
            if len(payloads) == 1:
                raise ValueError("Buchrand Ende fehlt")
            return {"ok": True}

        _, metadata = execute_with_fallbacks({}, chain, run, "scene-alignment", "disabled")

        self.assertEqual([provider for provider, _ in payloads], ["zai", "zai"])
        self.assertEqual(payloads[1][1], "zai: Buchrand Ende fehlt")
        self.assertFalse(metadata["fallback_used"])
        self.assertTrue(metadata["attempts"][0]["repair_retry"])

    def test_accumulates_two_validator_failures_for_second_repair(self):
        chain = [
            {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh", "timeout_seconds": 600},
        ]
        payloads = []

        def run(payload, _execution):
            payloads.append(payload.get("_validation_retry"))
            if len(payloads) == 1:
                raise ValueError("Cue c015 bewahrt nur 29%")
            if len(payloads) == 2:
                raise ValueError("Cue c017 bewahrt nur 17%")
            return {"ok": True}

        result, metadata = execute_with_fallbacks({}, chain, run, "scene-alignment", "disabled")

        self.assertEqual(result, {"ok": True})
        self.assertIn("c015", payloads[1])
        self.assertIn("c015", payloads[2])
        self.assertIn("c017", payloads[2])
        self.assertEqual(len(metadata["attempts"]), 3)

    def test_allows_sixth_targeted_primary_attempt_before_fallback(self):
        chain = [
            {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh", "timeout_seconds": 600},
            {"provider": "zai", "model": "glm-5.2", "reasoning_effort": "xhigh", "timeout_seconds": 600},
        ]
        providers = []

        def run(_payload, execution):
            providers.append(execution["provider"])
            if len(providers) < 6:
                raise ValueError(f"Cue c00{len(providers)} ist ungültig")
            return {"ok": True}

        result, metadata = execute_with_fallbacks(
            {},
            chain,
            run,
            "scene-alignment",
            "disabled",
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(providers, ["codex"] * 6)
        self.assertFalse(metadata["fallback_used"])

    def test_locks_valid_cues_during_targeted_repair(self):
        chain = [
            {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh", "timeout_seconds": 600},
        ]
        payloads = []
        partial = {
            "alignments": [
                {"cue_id": "c001", "narration_text": "Bestandene Passage."},
                {"cue_id": "c002", "narration_text": "Fehlerhafte Passage."},
            ],
            "character_introductions": [
                {"research_name": "Mara", "cue_id": "c001"},
                {"research_name": "Mai", "cue_id": "c002"},
            ],
        }

        def run(payload, _execution):
            payloads.append(payload)
            if len(payloads) == 1:
                raise AlignmentValidationError(
                    "Cue c002 paraphrasiert bereits hörbares Serienaudio",
                    partial,
                    {"c002"},
                )
            return {"ok": True}

        result, _ = execute_with_fallbacks(
            {"cues": []},
            chain,
            run,
            "scene-alignment",
            "disabled",
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            [item["cue_id"] for item in payloads[1]["_locked_alignments"]],
            ["c001"],
        )
        self.assertEqual(
            [item["cue_id"] for item in payloads[1]["_locked_character_introductions"]],
            ["c001"],  # The rejected cue's mapping must remain removable too.
        )
        self.assertEqual(payloads[1]["_repair_cue_ids"], ["c002"])

    def test_repair_feedback_keeps_latest_error_when_history_exceeds_budget(self):
        calls = []
        def run(payload, _execution):
            calls.append(payload)
            attempt = len(calls)
            if attempt <= 4:
                raise ValueError(f"CURRENT-{attempt}: " + "x" * 600)
            return {"ok": True}
        execute_with_fallbacks(
            {"cues": []},
            [{"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "high", "timeout_seconds": 600}],
            run, "scene-alignment", "disabled",
        )

        self.assertTrue(calls[-1]["_validation_retry"].startswith("codex: CURRENT-4:"))
        self.assertLessEqual(len(calls[-1]["_validation_retry"]), 1500)

    def test_coverage_repair_unlocks_selected_misplaced_beats(self):
        partial = {"alignments": [
            {"cue_id": "c090", "anchor_segment_id": "s090"},
            {"cue_id": "c092", "anchor_segment_id": "s999"},
        ], "character_introductions": []}
        calls = []

        def run(payload, _execution):
            calls.append(payload)
            if len(calls) == 1:
                raise AlignmentValidationError(
                    "Folge 63023: 125–1105 s (979 s ohne Kommentar, nimm davon c092, c093)",
                    partial, set(), {"c092", "c093"},
                )
            locked = {item["cue_id"] for item in payload["_locked_alignments"]}
            self.assertEqual(locked, {"c090"})
            self.assertEqual(payload["_repair_cue_ids"], ["c092", "c093"])
            return {"ok": True}

        execute_with_fallbacks(
            {"cues": []},
            [{"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "high", "timeout_seconds": 600}],
            run, "scene-alignment", "disabled",
        )

    def test_coverage_repair_preserves_explicit_engine_locks(self):
        calls = []
        partial = {"alignments": [{"cue_id": "c092"}], "character_introductions": []}
        def run(payload, _execution):
            calls.append(payload)
            if len(calls) == 1:
                raise AlignmentValidationError("coverage gap", partial, set(), {"c092", "c093"})
            self.assertEqual(payload["_locked_alignments"], partial["alignments"])
            return {"ok": True}
        execute_with_fallbacks(
            {"cues": [], "_engine_locked_cue_ids": ["c092"]},
            [{"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "high", "timeout_seconds": 600}],
            run, "scene-alignment", "disabled",
        )

    def test_salvages_only_exhausted_optional_audio_drama_beat(self):
        payload = {
            "narration_density": "audio_drama",
            "cues": [
                {"id": f"c00{index}", "proposed_episode_id": "ep1", "book_edge": "none"}
                for index in range(5)
            ],
            "segments": [
                {"id": f"s00{index}", "episode_id": "ep1"}
                for index in range(5)
            ],
        }
        partial = {
            "alignments": [
                {
                    "cue_id": f"c00{index}",
                    "anchor_segment_id": f"s00{index}",
                    "purpose": "visual_action",
                }
                for index in range(5)
            ],
            "character_introductions": [],
            "name_aliases": [],
            "notes": [],
        }
        error = AlignmentValidationError(
            "Cue c004 bewahrt nur 25% der Romanmerkmale",
            partial,
            {"c004"},
        )
        validated_payloads = []

        def validate_seed(candidate_payload, _execution):
            validated_payloads.append(candidate_payload)
            return candidate_payload["_seed_result"]

        result = salvage_optional_audio_drama_result(
            payload,
            error,
            validate_seed,
            {"provider": "codex"},
        )

        self.assertEqual(
            [item["cue_id"] for item in result["alignments"]],
            ["c000", "c001", "c002", "c003"],
        )
        self.assertIn("c004", result["notes"][-1])
        self.assertEqual(len(validated_payloads), 1)
        self.assertTrue(validated_payloads[0]["_validation_only_seed"])

    def test_never_salvages_book_edge_or_formal_introduction(self):
        payload = {
            "narration_density": "audio_drama",
            "cues": [{"id": "c001", "proposed_episode_id": "ep1", "book_edge": "end"}],
            "segments": [{"id": "s001", "episode_id": "ep1"}],
        }
        partial = {
            "alignments": [{
                "cue_id": "c001",
                "anchor_segment_id": "s001",
                "purpose": "book_boundary",
            }],
            "character_introductions": [],
        }
        error = AlignmentValidationError("Buchrand fehlt", partial, {"c001"})

        self.assertIsNone(
            salvage_optional_audio_drama_result(
                payload,
                error,
                lambda *_: self.fail("must not validate a dropped book edge"),
                {"provider": "codex"},
            )
        )

    def test_rebuilds_shared_formal_introduction_from_researched_visual_traits(self):
        payload = {
            "narration_density": "audio_drama",
            "preferred_names": ["Yamchu", "Pool"],
            "cues": [
                {"id": f"c00{index}", "proposed_episode_id": "ep1", "book_edge": "none"}
                for index in range(4)
            ],
            "segments": [
                {"id": f"s00{index}", "episode_id": "ep1"}
                for index in range(4)
            ],
            "episode_contexts": [{
                "episode_id": "ep1",
                "character_introductions": [
                    {
                        "name": "Yamcha",
                        "visual_description": (
                            "Großer junger Mann mit langem schwarzen Haar; trägt ein "
                            "kampfsporttaugliches Gewand und führt ein Schwert."
                        ),
                        "introduction_required": True,
                    },
                    {
                        "name": "Puar (Pool)",
                        "visual_description": (
                            "Kleine, schwebende, blau gefärbte, katzenartige Gestalt "
                            "mit Flügeln."
                        ),
                        "introduction_required": True,
                    },
                ],
            }],
        }
        partial = {
            "alignments": [
                {
                    "cue_id": f"c00{index}",
                    "anchor_segment_id": f"s00{index}",
                    "purpose": "scene_transition" if index == 0 else "visual_action",
                    "narration_text": "Die Reisenden sehen zwei Gestalten.",
                }
                for index in range(4)
            ],
            "character_introductions": [
                {
                    "research_name": "Yamcha", "spoken_name": "Yamcha",
                    "cue_id": "c000", "episode_id": "ep1",
                },
                {
                    "research_name": "Puar (Pool)", "spoken_name": "Puar",
                    "cue_id": "c000", "episode_id": "ep1",
                },
            ],
            "name_aliases": [],
            "notes": [],
        }
        error = AlignmentValidationError(
            "Verpflichtende Figuren-Einführung hat zu wenige sichtbare Merkmale: c000",
            partial,
            {"c000"},
        )
        validated_payloads = []

        def validate_seed(candidate_payload, _execution):
            validated_payloads.append(candidate_payload)
            return candidate_payload["_seed_result"]

        result = repair_researched_character_introductions(
            payload,
            error,
            validate_seed,
            {"provider": "codex"},
        )

        repaired = result["alignments"][0]
        self.assertEqual(repaired["purpose"], "character_introduction")
        self.assertIn("Yamchu", repaired["narration_text"])
        self.assertIn("Pool", repaired["narration_text"])
        self.assertIn("langem schwarzen Haar", repaired["narration_text"])
        self.assertIn("katzenartige Gestalt", repaired["narration_text"])
        self.assertLessEqual(repaired["target_duration_ms"], 18_000)
        self.assertEqual(
            [item["spoken_name"] for item in result["character_introductions"]],
            ["Yamchu", "Pool"],
        )
        self.assertTrue(validated_payloads[0]["_validation_only_seed"])

        calls = []

        def run_with_timebox(candidate_payload, _execution):
            calls.append(candidate_payload)
            if "_seed_result" in candidate_payload:
                return candidate_payload["_seed_result"]
            raise AlignmentValidationError(
                "Verpflichtende Figuren-Einführung hat zu wenige sichtbare Merkmale: c000",
                partial,
                {"c000"},
            )

        timed_result, metadata = execute_with_fallbacks(
            payload,
            [{
                "provider": "codex",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "xhigh",
                "timeout_seconds": 600,
            }],
            run_with_timebox,
            "scene-alignment",
            "disabled",
        )

        self.assertEqual(len(calls), 3)
        self.assertIn("Pool", timed_result["alignments"][0]["narration_text"])
        self.assertEqual(
            metadata["attempts"][-1]["reason"],
            "character_introduction_repaired_after_timebox",
        )

    def test_fallback_chain_finishes_by_omitting_one_exhausted_optional_beat(self):
        chain = [{
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "timeout_seconds": 600,
        }]
        payload = {
            "narration_density": "audio_drama",
            "cues": [
                {"id": f"c00{index}", "proposed_episode_id": "ep1", "book_edge": "none"}
                for index in range(5)
            ],
            "segments": [
                {"id": f"s00{index}", "episode_id": "ep1"}
                for index in range(5)
            ],
        }
        partial = {
            "alignments": [
                {
                    "cue_id": f"c00{index}",
                    "anchor_segment_id": f"s00{index}",
                    "purpose": "visual_action",
                }
                for index in range(5)
            ],
            "character_introductions": [],
            "name_aliases": [],
            "notes": [],
        }
        calls = []

        def run(candidate_payload, _execution):
            calls.append(candidate_payload)
            if "_seed_result" in candidate_payload:
                return candidate_payload["_seed_result"]
            raise AlignmentValidationError(
                "Cue c004 bewahrt nur 25% der Romanmerkmale",
                partial,
                {"c004"},
            )

        result, metadata = execute_with_fallbacks(
            payload,
            chain,
            run,
            "scene-alignment",
            "disabled",
        )

        self.assertEqual(len(calls), 3)
        self.assertEqual(len(result["alignments"]), 4)
        self.assertEqual(
            metadata["attempts"][-1]["reason"],
            "optional_cue_omitted_after_timebox",
        )

    def test_salvages_last_validated_mix_when_final_provider_is_unavailable(self):
        chain = [
            {
                "provider": "codex", "model": "gpt-5.6-sol",
                "reasoning_effort": "xhigh", "timeout_seconds": 600,
            },
            {
                "provider": "zai", "model": "glm-5.2",
                "reasoning_effort": "xhigh", "timeout_seconds": 600,
            },
        ]
        payload = {
            "narration_density": "audio_drama",
            "cues": [
                {"id": f"c00{index}", "proposed_episode_id": "ep1", "book_edge": "none"}
                for index in range(5)
            ],
            "segments": [
                {"id": f"s00{index}", "episode_id": "ep1"}
                for index in range(5)
            ],
        }
        partial = {
            "alignments": [
                {
                    "cue_id": f"c00{index}",
                    "anchor_segment_id": f"s00{index}",
                    "purpose": "visual_action",
                }
                for index in range(5)
            ],
            "character_introductions": [],
            "name_aliases": [],
            "notes": [],
        }

        codex_calls = 0

        def run(candidate_payload, execution):
            nonlocal codex_calls
            if "_seed_result" in candidate_payload:
                return candidate_payload["_seed_result"]
            if execution["provider"] == "codex":
                codex_calls += 1
                if codex_calls > 1:
                    raise RuntimeError("Codex technisch nicht mehr erreichbar")
                raise AlignmentValidationError(
                    "Cue c004 bewahrt nur 25% der Romanmerkmale",
                    partial,
                    {"c004"},
                )
            raise RuntimeError("GLM lieferte keine verwertbare Antwort")

        result, metadata = execute_with_fallbacks(
            payload,
            chain,
            run,
            "scene-alignment",
            "disabled",
        )

        self.assertEqual(len(result["alignments"]), 4)
        self.assertEqual(metadata["attempts"][-1]["reason"], "optional_cue_omitted")
        self.assertTrue(metadata["fallback_used"])

    def test_sends_only_invalid_cues_to_targeted_repair_model(self):
        payload = {
            "cues": [
                {"id": "c001", "text": "Bestandene Passage"},
                {"id": "c002", "text": "Fehlerhafte Passage"},
                {"id": "c003", "text": "Noch eine bestandene Passage"},
                {"id": "c004", "text": "Nicht ausgewählter Rohkandidat"},
            ],
            "segments": [{"id": "s001"}],
            "_validation_retry": "Cue c002 ist ungültig",
            "_locked_alignments": [
                {"cue_id": "c001", "narration_text": "Bestanden."},
                {"cue_id": "c003", "narration_text": "Auch bestanden."},
            ],
            "_repair_cue_ids": ["c002"],
        }

        model_payload = scene_alignment_model_payload(payload)

        self.assertEqual(
            [item["id"] for item in model_payload["cues"]],
            ["c002"],
        )
        self.assertEqual(
            model_payload["repair_scope"]["cue_ids"],
            ["c002"],
        )
        self.assertIn(
            "optionalen Reparatur-Cue vollständig weglassen",
            model_payload["repair_scope"]["instruction"],
        )
        self.assertNotIn("_validation_retry", model_payload)
        self.assertNotIn("_locked_alignments", model_payload)
        self.assertEqual(model_payload["segments"], [{"id": "s001"}])

    def test_validates_checkpoint_seed_before_calling_model(self):
        payload = {
            "series": {"title": "Beispielserie", "year": 1986},
            "preferred_names": [],
            "known_character_names": ["Bulma", "Son Goku"],
            "narration_density": "detailed",
            "episode_contexts": [{
                "episode_id": "ep1",
                "synopsis": "Eine sichtbare Handlung.",
                "continuity_before": "",
                "major_beats": [],
                "character_introductions": [],
            }],
            "cues": [{
                "id": "c001",
                "order": 0,
                "text": "Bulma hebt die Hand.",
                "source_context": "Bulma hebt schweigend die linke Hand.",
                "proposed_episode_id": "ep1",
                "proposed_anchor_ms": 1_000,
                "nearby_subtitles": "Goku!",
                "book_edge": "none",
            }],
            "segments": [{
                "id": "s001",
                "episode_id": "ep1",
                "episode_order": 0,
                "start_ms": 1_000,
                "text": "Goku!",
            }],
            "seed_result": {
                "alignments": [{
                    "cue_id": "c001",
                    "anchor_segment_id": "s001",
                    "placement": "before_anchor",
                    "purpose": "visual_action",
                    "narration_text": "Schweigend hebt Bulma die linke Hand.",
                    "information_gain": "Ergänzt die sichtbare Geste.",
                    "confidence": 0.95,
                    "source_detail_coverage": 0.8,
                    "native_audio_relation": "complements_existing_audio",
                    "retained_visual_details": ["linke Hand wird gehoben"],
                    "reasoning": "Die Geste ist nicht hörbar.",
                }],
                "character_introductions": [],
                "name_aliases": [],
                "notes": [],
            },
        }
        request = validated_alignment_request(payload)
        execution = {
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "timeout_seconds": 600,
        }

        with (
            patch("runner.provider_auth_status", side_effect=AssertionError("no auth")),
            patch("runner.run_structured_agent", side_effect=AssertionError("no model")),
        ):
            result = research_scene_alignment(request, execution)

        self.assertEqual(result["alignments"][0]["cue_id"], "c001")
        self.assertEqual(request["known_character_names"], ["Bulma", "Son Goku"])

        request["_validation_only_seed"] = True
        with (
            patch(
                "runner.clean_scene_alignment_seed",
                side_effect=AssertionError("validation-only salvage must not rewrite text"),
            ),
            patch("runner.provider_auth_status", side_effect=AssertionError("no auth")),
            patch("runner.run_structured_agent", side_effect=AssertionError("no model")),
        ):
            validation_only_result = research_scene_alignment(request, execution)

        self.assertEqual(
            validation_only_result["alignments"][0]["narration_text"],
            "Schweigend hebt Bulma die linke Hand.",
        )

    def test_known_character_names_do_not_trigger_name_only_repetition(self):
        findings = native_audio_repetition_findings(
            "Bulma und Goku gehen weiter. Bulma hebt dabei schweigend die linke Hand.",
            "Bulma hebt schweigend die linke Hand.",
            [{
                "id": "chosen",
                "episode_id": "ep1",
                "start_ms": 100_000,
                "text": "Bulma und Goku haben sich auf den Weg gemacht.",
            }],
            "chosen",
            ["Bulma", "Goku"],
        )

        self.assertEqual(findings, [])

    def test_seed_cleanup_removes_only_audible_sentence_and_keeps_visual_action(self):
        payload = {
            "preferred_names": [],
            "known_character_names": ["Bulma"],
            "episode_contexts": [{
                "episode_id": "ep1",
                "character_introductions": [],
            }],
            "cues": [{
                "id": "c001",
                "order": 0,
                "source_context": (
                    "Bulma sagte, sie wolle einen süßen Freund. "
                    "Dabei trommelte sie mit den Fingern auf den Boden."
                ),
            }],
            "segments": [{
                "id": "s001",
                "episode_id": "ep1",
                "start_ms": 100_000,
                "text": "Ich will einen süßen Freund.",
            }],
        }
        seed = {
            "alignments": [{
                "cue_id": "c001",
                "anchor_segment_id": "s001",
                "narration_text": (
                    "Bulma wollte einen süßen Freund. "
                    "Dabei trommelte sie mit den Fingern auf den Boden."
                ),
                "purpose": "visual_action",
                "placement": "before_anchor",
                "introduced_characters": [],
            }],
            "character_introductions": [],
        }

        result = clean_scene_alignment_seed(payload, seed)

        self.assertNotIn("süßen Freund", result["alignments"][0]["narration_text"])
        self.assertIn("trommelte", result["alignments"][0]["narration_text"])

    def test_seed_cleanup_reduces_fully_audible_exposition_to_speaker_bridge(self):
        payload = {
            "preferred_names": [],
            "known_character_names": ["Goku"],
            "episode_contexts": [{
                "episode_id": "ep1",
                "character_introductions": [],
            }],
            "cues": [{
                "id": "c001",
                "order": 0,
                "source_context": (
                    "Goku erzählte ihm von den sieben Dragon Balls. "
                    "Wer sie sammelte, durfte sich einen Wunsch erfüllen lassen."
                ),
            }],
            "segments": [{
                "id": "s001",
                "episode_id": "ep1",
                "start_ms": 100_000,
                "text": (
                    "Wenn man die sieben Dragon Balls zusammenhat, "
                    "darf man sich einen Wunsch erfüllen lassen."
                ),
            }],
        }
        seed = {
            "alignments": [{
                "cue_id": "c001",
                "anchor_segment_id": "s001",
                "narration_text": (
                    "Goku erzählte ihm von den sieben Dragon Balls. "
                    "Wer sie sammelte, durfte sich einen Wunsch erfüllen lassen."
                ),
                "purpose": "visual_action",
                "placement": "before_anchor",
                "introduced_characters": [],
            }],
            "character_introductions": [],
        }

        result = clean_scene_alignment_seed(payload, seed)
        alignment = result["alignments"][0]

        self.assertEqual(
            dialogue_bridge_from_source(payload["cues"][0]["source_context"]),
            "Goku erzählte ihm.",
        )
        self.assertEqual(alignment["narration_text"], "Goku erzählte ihm.")
        self.assertEqual(alignment["purpose"], "continuity_bridge")

    def test_seed_cleanup_adds_grounded_required_character_introduction(self):
        payload = {
            "preferred_names": [],
            "known_character_names": ["Oolong"],
            "episode_contexts": [{
                "episode_id": "ep1",
                "character_introductions": [{
                    "name": "Oolong",
                    "visual_description": "Kleines rosafarbenes anthropomorphes Schwein.",
                    "introduction_required": True,
                }],
            }],
            "cues": [{
                "id": "c001",
                "order": 0,
                "source_context": "Oolong trat aus dem Haus.",
            }],
            "segments": [{
                "id": "s001",
                "episode_id": "ep1",
                "start_ms": 100_000,
                "text": "Wer ist da?",
            }],
        }
        seed = {
            "alignments": [{
                "cue_id": "c001",
                "anchor_segment_id": "s001",
                "narration_text": "Oolong trat aus dem Haus.",
                "purpose": "visual_action",
                "placement": "after_anchor",
                "introduced_characters": [],
            }],
            "character_introductions": [],
        }

        result = clean_scene_alignment_seed(payload, seed)

        self.assertEqual(
            result["character_introductions"],
            [{
                "research_name": "Oolong",
                "spoken_name": "Oolong",
                "cue_id": "c001",
                "episode_id": "ep1",
            }],
        )
        self.assertEqual(result["alignments"][0]["purpose"], "character_introduction")
        self.assertEqual(result["alignments"][0]["placement"], "before_anchor")
        self.assertIn("rosafarbenes", result["alignments"][0]["narration_text"])

    def test_seed_cleanup_restores_missing_visual_source_sentences_without_dialogue(self):
        payload = {
            "preferred_names": [],
            "known_character_names": [],
            "episode_contexts": [{
                "episode_id": "ep1",
                "character_introductions": [],
            }],
            "cues": [{
                "id": "c001",
                "order": 0,
                "source_context": (
                    "Sie hob eine rote Schale mit beiden Händen vom niedrigen Tisch. "
                    "Dann stellte sie die Schale in ein hohes Regal. "
                    "»Stell sie weg«, sagte jemand."
                ),
            }],
            "segments": [{
                "id": "s001",
                "episode_id": "ep1",
                "start_ms": 100_000,
                "text": "Stell sie weg.",
            }],
        }
        seed = {
            "alignments": [{
                "cue_id": "c001",
                "anchor_segment_id": "s001",
                "narration_text": "Sie blieb einen Augenblick stehen.",
                "purpose": "visual_action",
                "placement": "before_anchor",
                "introduced_characters": [],
            }],
            "character_introductions": [],
        }

        result = clean_scene_alignment_seed(payload, seed)
        narration = result["alignments"][0]["narration_text"]

        self.assertIn("rote Schale", narration)
        self.assertIn("hohes Regal", narration)
        self.assertNotIn("Stell sie weg", narration)

    def test_seed_cleanup_uses_researched_name_variant_and_preferred_spoken_name(self):
        payload = {
            "preferred_names": ["Herr der Schildkröten"],
            "known_character_names": ["Meister Muten-Roshi (Schildkröten-Eremit)"],
            "episode_contexts": [{
                "episode_id": "ep1",
                "character_introductions": [{
                    "name": "Meister Muten-Roshi (Schildkröten-Eremit)",
                    "visual_description": "Glatzköpfiger alter Mann mit langem weißem Bart und Sonnenbrille.",
                    "introduction_required": True,
                }],
            }],
            "cues": [{
                "id": "c001",
                "order": 0,
                "source_context": "Der Herr der Schildkröten hob seinen Stock.",
            }],
            "segments": [{
                "id": "s001",
                "episode_id": "ep1",
                "start_ms": 100_000,
                "text": "Ich bin der Herr der Schildkröten.",
            }],
        }
        seed = {
            "alignments": [{
                "cue_id": "c001",
                "anchor_segment_id": "s001",
                "narration_text": "Der Herr der Schildkröten hob seinen Stock.",
                "purpose": "visual_action",
                "placement": "after_anchor",
                "introduced_characters": [],
            }],
            "character_introductions": [],
        }

        result = clean_scene_alignment_seed(payload, seed)

        self.assertTrue(
            character_reference_matches(
                "Der Schildkröten-Eremit trat an den Strand.",
                "Meister Muten-Roshi (Schildkröten-Eremit)",
            )
        )
        self.assertEqual(
            result["character_introductions"][0]["spoken_name"],
            "Herr der Schildkröten",
        )
        self.assertIn(
            "Herr der Schildkröten zeigte sich als",
            result["alignments"][0]["narration_text"],
        )

    def test_seed_cleanup_does_not_assign_another_character_from_shared_cue(self):
        payload = {
            "preferred_names": ["Pool", "Yamchu"],
            "known_character_names": ["Yamcha", "Puar (Pool)"],
            "episode_contexts": [{
                "episode_id": "ep1",
                "character_introductions": [
                    {
                        "name": "Yamcha",
                        "visual_description": "Großer junger Mann mit langem schwarzem Haar.",
                        "introduction_required": True,
                    },
                    {
                        "name": "Puar (Pool)",
                        "visual_description": "Kleine blaue katzenartige Gestalt mit Flügeln.",
                        "introduction_required": True,
                    },
                ],
            }],
            "cues": [{
                "id": "c001",
                "order": 0,
                "source_context": "Yamcha fuhr vor, Puar saß hinter ihm.",
            }],
            "segments": [{
                "id": "s001",
                "episode_id": "ep1",
                "start_ms": 100_000,
                "text": "Yamchu und Pool sind da.",
            }],
        }
        seed = {
            "alignments": [{
                "cue_id": "c001",
                "anchor_segment_id": "s001",
                "narration_text": "Yamchu stand am Lenker, Pool saß hinter ihm.",
                "purpose": "visual_action",
                "placement": "after_anchor",
                "introduced_characters": [],
            }],
            "character_introductions": [{
                "research_name": "Yamcha",
                "spoken_name": "Pool",
                "cue_id": "c001",
                "episode_id": "ep1",
            }],
        }

        result = clean_scene_alignment_seed(payload, seed)
        mappings = {
            item["research_name"]: item["spoken_name"]
            for item in result["character_introductions"]
        }

        self.assertEqual(mappings["Yamcha"], "Yamchu")
        self.assertEqual(mappings["Puar (Pool)"], "Pool")
        self.assertIn("Yamchu zeigte sich als", result["alignments"][0]["narration_text"])
        self.assertIn("Pool zeigte sich als", result["alignments"][0]["narration_text"])

    def test_short_character_name_does_not_match_subtitle_ocr_word(self):
        self.assertFalse(exact_character_name_match("Sag maI,", "Mai"))
        self.assertTrue(exact_character_name_match("Bist du das, Mai?", "Mai"))

    def test_carries_targeted_repairs_into_provider_fallback(self):
        chain = [
            {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh", "timeout_seconds": 600},
            {"provider": "zai", "model": "glm-5.2", "reasoning_effort": "xhigh", "timeout_seconds": 600},
        ]
        partial = {
            "alignments": [
                {"cue_id": "c001", "narration_text": "Bestandene Passage."},
                {"cue_id": "c002", "narration_text": "Fehlerhafte Passage."},
            ],
            "character_introductions": [
                {"research_name": "Mara", "cue_id": "c001"},
            ],
        }
        calls = []

        def run(payload, execution):
            calls.append((execution["provider"], payload))
            if execution["provider"] == "codex":
                raise AlignmentValidationError(
                    "Cue c002 paraphrasiert bereits hörbares Serienaudio",
                    partial,
                    {"c002"},
                )
            return {"ok": True}

        result, metadata = execute_with_fallbacks(
            {"cues": []},
            chain,
            run,
            "scene-alignment",
            "disabled",
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            [provider for provider, _ in calls],
            ["codex"] * runner.MAX_INVALID_OUTPUT_ATTEMPTS + ["zai"],
        )
        self.assertEqual(
            [item["cue_id"] for item in calls[-1][1]["_locked_alignments"]],
            ["c001"],
        )
        self.assertTrue(metadata["fallback_used"])

    def test_reports_prior_validation_failure_when_last_fallback_is_unavailable(self):
        chain = [
            {"provider": "zai", "model": "glm-5.2", "reasoning_effort": "xhigh", "timeout_seconds": 600},
            {"provider": "claude", "model": "sonnet", "reasoning_effort": "high", "timeout_seconds": 600},
        ]

        def run(_payload, execution):
            if execution["provider"] == "zai":
                raise ValueError("Cue c017 fehlt")
            raise PermissionError("Claude Code Login erforderlich")

        with self.assertRaisesRegex(ValueError, "zai: Cue c017 fehlt"):
            execute_with_fallbacks({}, chain, run, "scene-alignment", "disabled")

    def test_builds_schema_constrained_claude_command_without_local_tools(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            schema = Path(temp_dir) / "schema.json"
            schema.write_text('{"type":"object"}', "utf-8")
            command = build_claude_command(
                schema=schema,
                execution={"provider": "claude", "model": "sonnet", "reasoning_effort": "xhigh", "timeout_seconds": 900},
                web_search="disabled",
            )

        self.assertIn("--json-schema", command)
        self.assertIn("--safe-mode", command)
        self.assertIn("--no-session-persistence", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--effort") + 1], "xhigh")
        schema_argument = json.loads(command[command.index("--json-schema") + 1])
        self.assertNotIn("$schema", schema_argument)
        self.assertEqual(schema_argument["type"], "object")

    def test_metadata_makes_requested_and_fixed_settings_explicit(self):
        metadata = execution_metadata(
            {"model": "automatic", "reasoning_effort": "high", "timeout_seconds": 1200},
            "mapping-research",
            "live",
        )

        self.assertEqual(metadata["requested_model"], "automatic")
        self.assertEqual(metadata["profile"], "mapping-research")
        self.assertEqual(metadata["web_search"], "live")
        self.assertEqual(metadata["sandbox"], "read-only")


class ProviderAuthTest(unittest.TestCase):
    def test_saves_zai_key_outside_application_state_and_only_returns_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            secret_dir = Path(temp_dir)
            secret_file = secret_dir / "zai.json"
            with patch.object(runner, "PROVIDER_SECRETS_DIR", secret_dir), patch.object(runner, "ZAI_CONFIG_FILE", secret_file):
                response = save_zai_credentials("very-secret-key", "https://api.z.ai/api/anthropic/")
                token, url = runner.zai_credentials()

            self.assertEqual(response, {"configured": True, "base_url": "https://api.z.ai/api/anthropic"})
            self.assertNotIn("very-secret-key", str(response))
            self.assertEqual(token, "very-secret-key")
            self.assertEqual(url, "https://api.z.ai/api/anthropic")
            self.assertEqual(secret_file.stat().st_mode & 0o777, 0o600)

    def test_rejects_non_https_provider_url(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            runner.validated_provider_base_url("http://localhost:8090")

    def test_extracts_login_url_and_device_code_without_exposing_cli_output(self):
        session_id = "test-login-session"
        runner.LOGIN_SESSIONS[session_id] = {
            "id": session_id,
            "provider": "codex",
            "status": "starting",
            "output": "",
            "created_at": 0,
        }
        try:
            update_login_session_output(
                session_id,
                "\x1b[32mOpen https://auth.example.test/device and enter ABCDE-FGHIJ\x1b[0m",
            )
            session = runner.LOGIN_SESSIONS[session_id]
            self.assertEqual(session["status"], "awaiting_code")
            self.assertEqual(session["login_url"], "https://auth.example.test/device")
            self.assertEqual(session["verification_code"], "ABCDE-FGHIJ")
        finally:
            runner.LOGIN_SESSIONS.pop(session_id, None)

    def test_cli_welcome_banner_does_not_mark_login_complete(self):
        session_id = "test-welcome-session"
        runner.LOGIN_SESSIONS[session_id] = {
            "id": session_id,
            "provider": "codex",
            "status": "starting",
            "output": "",
            "created_at": 0,
        }
        try:
            update_login_session_output(session_id, "Welcome to Codex. Open https://auth.openai.com/codex/device")
            self.assertEqual(runner.LOGIN_SESSIONS[session_id]["status"], "awaiting_code")
        finally:
            runner.LOGIN_SESSIONS.pop(session_id, None)


class AlignmentRequestTest(unittest.TestCase):
    context = [{
        "episode_id": "ep8",
        "synopsis": "Der Meister erreicht den brennenden Berg.",
        "continuity_before": "Die Gruppe sucht Hilfe.",
        "major_beats": ["Ankunft am Berg"],
        "character_introductions": [],
    }]

    def test_preserves_valid_book_edge_marker(self):
        request = validated_alignment_request({
            "series": {"title": "Beispielserie", "year": 1986},
            "preferred_names": ["Herr der Schildkröten"],
            "episode_contexts": self.context,
            "cues": [{
                "id": "cue-end",
                "order": 1,
                "text": "Der Herr der Schildkröten erreicht den brennenden Berg.",
                "source_context": "Am Ende trifft der Meister am Berg ein.",
                "proposed_episode_id": "ep8",
                "book_edge": "end",
            }],
            "segments": [{
                "id": "segment-end",
                "episode_id": "ep8",
                "episode_order": 7,
                "start_ms": 592_000,
                "text": "Das ist also der Bratpfannenberg?",
            }],
        })

        self.assertEqual(request["cues"][0]["book_edge"], "end")

    def test_book_edges_override_stale_anchor_order(self):
        request = validated_alignment_request({
            "episode_contexts": self.context,
            "cues": [
                {"id": "action", "order": 0, "text": "Handlung"},
                {"id": "end", "order": 1, "text": "Schluss", "book_edge": "end"},
                {"id": "last_action", "order": 2, "text": "Letzte Handlung"},
                {"id": "start", "order": 3, "text": "Anfang", "book_edge": "start"},
            ],
            "segments": [{"id": "s1", "episode_id": "ep8", "text": "Dialog", "start_ms": 1000}],
        })
        ordered = sorted(request["cues"], key=lambda cue: cue["order"])
        self.assertEqual([cue["id"] for cue in ordered], ["start", "action", "last_action", "end"])

    def test_preserves_local_native_audio_context_for_editorial_complement(self):
        request = validated_alignment_request({
            "narration_density": "detailed",
            "episode_contexts": self.context,
            "cues": [{
                "id": "cue-opening",
                "order": 0,
                "text": "Goku trägt einen Baumstamm.",
                "source_context": "Goku trägt einen Baumstamm mit bloßen Händen.",
                "proposed_episode_id": "ep8",
                "proposed_anchor_ms": 163_000,
                "nearby_subtitles": "125000 ms: Da lebte tief in den Bergen ein Junge.",
            }],
            "segments": [{
                "id": "segment",
                "episode_id": "ep8",
                "episode_order": 0,
                "start_ms": 163_000,
                "text": "Hallo.",
            }],
        })

        self.assertEqual(request["cues"][0]["proposed_anchor_ms"], 163_000)
        self.assertIn("tief in den Bergen", request["cues"][0]["nearby_subtitles"])

    def test_preserves_detailed_narration_density(self):
        request = validated_alignment_request({
            "narration_density": "detailed",
            "episode_contexts": self.context,
            "cues": [{
                "id": "cue",
                "order": 0,
                "text": "Eine belegte Passage.",
                "source_context": "Eine belegte Passage.",
                "proposed_episode_id": "ep8",
            }],
            "segments": [{
                "id": "segment",
                "episode_id": "ep8",
                "episode_order": 0,
                "start_ms": 1_000,
                "text": "Eine passende Szene.",
            }],
        })

        self.assertEqual(request["narration_density"], "detailed")

    def test_preserves_audio_drama_density_and_segment_end(self):
        request = validated_alignment_request({
            "narration_density": "audio_drama",
            "episode_contexts": self.context,
            "cues": [{
                "id": "cue",
                "order": 0,
                "text": "Eine belegte Passage.",
                "source_context": "Eine belegte Passage.",
                "proposed_episode_id": "ep8",
            }],
            "segments": [{
                "id": "segment",
                "episode_id": "ep8",
                "episode_order": 0,
                "start_ms": 1_000,
                "end_ms": 2_250,
                "text": "[Musik] Eine passende Szene.",
            }],
        })

        self.assertEqual(request["narration_density"], "audio_drama")
        self.assertEqual(request["segments"][0]["end_ms"], 2_250)

    def test_rejects_unknown_narration_density(self):
        with self.assertRaisesRegex(ValueError, "Erzähldichte"):
            validated_alignment_request({
                "narration_density": "unbegrenzt",
                "episode_contexts": self.context,
                "cues": [{
                    "id": "cue",
                    "order": 0,
                    "text": "Eine belegte Passage.",
                    "source_context": "Eine belegte Passage.",
                    "proposed_episode_id": "ep8",
                }],
                "segments": [{
                    "id": "segment",
                    "episode_id": "ep8",
                    "episode_order": 0,
                    "start_ms": 1_000,
                    "text": "Eine passende Szene.",
                }],
            })


class AlignmentDensityContractTest(unittest.TestCase):
    context = AlignmentRequestTest.context

    def test_detailed_requires_every_raw_cue(self):
        with self.assertRaisesRegex(ValueError, "jeden belegten Roh-Cue"):
            validate_alignment_density(
                "detailed",
                {"cue-1", "cue-2"},
                ["cue-1"],
                {"ep1": 2},
                {"ep1": 1},
            )

    def test_detailed_accepts_exact_per_episode_counts(self):
        validate_alignment_density(
            "detailed",
            {"cue-1", "cue-2", "cue-3"},
            ["cue-1", "cue-2", "cue-3"],
            {"ep1": 2, "ep2": 1},
            {"ep1": 2, "ep2": 1},
        )

    def test_detailed_allows_grounded_rebalancing_between_episodes(self):
        validate_alignment_density(
            "detailed",
            {"cue-1", "cue-2", "cue-3"},
            ["cue-1", "cue-2", "cue-3"],
            {"ep1": 2, "ep2": 1},
            {"ep1": 1, "ep2": 2},
        )

    def test_audio_drama_selects_only_useful_episode_beats(self):
        validate_alignment_density(
            "audio_drama",
            {f"cue-{index}" for index in range(8)},
            [f"cue-{index}" for index in range(6)],
            {"ep1": 8},
            {"ep1": 6},
        )

        with self.assertRaisesRegex(ValueError, "4 bis 8"):
            validate_alignment_density(
                "audio_drama",
                {f"cue-{index}" for index in range(8)},
                ["cue-1", "cue-2", "cue-3"],
                {"ep1": 8},
                {"ep1": 3},
            )

    def test_overlay_longer_than_its_pause_becomes_an_interruption(self):
        """Rejecting it deadlocked the editor; the render downgrades anyway."""
        payload = {
            "series": {"title": "Beispielserie"},
            "preferred_names": [],
            "narration_density": "audio_drama",
            "episode_contexts": [{
                "episode_id": "ep1", "synopsis": "Eine Szene.",
                "character_introductions": [],
            }],
            "cues": [{
                "id": "c001", "order": 0, "text": "Szene",
                "source_context": "Die rote Tuer mit goldenem Griff schwang langsam zum dunklen Hof auf.",
                "proposed_episode_id": "ep1", "proposed_anchor_ms": 1_000,
                "nearby_subtitles": "Wer ist da?", "book_edge": "none",
                "nearby_speech_gaps": [{"start_ms": 2_000, "end_ms": 5_000, "duration_ms": 3_000}],
            }],
            "segments": [{
                "id": "s001", "episode_id": "ep1", "episode_order": 0,
                "start_ms": 1_000, "end_ms": 2_000, "text": "Wer ist da?",
            }],
        }
        long_text = (
            "Die rote Tuer mit goldenem Griff schwang langsam zum dunklen Hof auf, "
            "waehrend der Staub in der Luft stand und niemand sich ruehrte."
        )
        answer = {
            "alignments": [{
                "cue_id": "c001", "narration_text": long_text,
                "purpose": "visual_action", "introduced_characters": [],
                "information_gain": "Zeigt das Bild.", "source_detail_coverage": 1.0,
                "native_audio_relation": "no_relevant_existing_narration",
                "retained_visual_details": ["rote Tuer", "goldener Griff"],
                "beat_type": "action_sync", "audio_strategy": "prefer_ambience_overlay",
                "target_duration_ms": 9_360, "anchor_segment_id": "s001",
                "placement": "before_anchor", "confidence": 0.95, "reasoning": "Am Anker.",
            }],
            "character_introductions": [], "name_aliases": [], "notes": [],
        }
        execution = {"provider": "codex", "model": "m", "reasoning_effort": "xhigh", "timeout_seconds": 900}

        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=answer),
        ):
            result = research_scene_alignment(payload, execution)

        beat = result["alignments"][0]
        self.assertEqual(beat["audio_strategy"], "pause_at_scene_boundary")
        self.assertTrue(beat["audio_strategy_downgraded"])

    def test_overlay_may_be_short_but_an_interruption_may_not(self):
        """Measured: only 20 of 122 anchors have a pause fitting a full comment."""
        base = {
            "purpose": "visual_action",
            "introduced_characters": [],
            "information_gain": "Zeigt die sichtbare Bewegung.",
            "native_audio_relation": "no_relevant_existing_narration",
            "retained_visual_details": ["rote Tuer"],
            "beat_type": "action_sync",
            "anchor_segment_id": "s001",
            "placement": "before_anchor",
            "confidence": 0.95,
            "reasoning": "Am Anker.",
        }
        short_text = "Goku zerrt die rote Tuer mit beiden Haenden auf."
        words = len(short_text.split())

        overlay_minimum = min(runner.AUDIO_DRAMA_MIN_WORDS["visual_action"], 8)
        self.assertLessEqual(overlay_minimum, words)
        # The same text is too thin to justify stopping the episode.
        self.assertGreater(runner.AUDIO_DRAMA_MIN_WORDS["visual_action"], words)

    def test_claude_without_login_is_not_reported_as_authenticated(self):
        """Exit code 0 with loggedIn=false hid a dead fallback for eight runs."""
        logged_out = SimpleNamespace(returncode=0, stdout='{"loggedIn": false}', stderr="")
        logged_in = SimpleNamespace(returncode=0, stdout='{"loggedIn": true}', stderr="")

        with patch.object(runner.subprocess, "run", return_value=logged_out):
            self.assertFalse(runner.provider_auth_status("claude")["authenticated"])
        with patch.object(runner.subprocess, "run", return_value=logged_in):
            self.assertTrue(runner.provider_auth_status("claude")["authenticated"])

    def test_audio_drama_counts_picture_words_not_filler_ratio(self):
        """Measured case: 9 of 11 "details" were filler like "ausserdem"."""
        source = (
            "Bulma hielt ihn zurück. Sie hatte keine Lust, dass er den halben "
            "Fluss aufwühlte, und ausserdem fiel ihr bereits eine bessere Loesung ein."
        )
        kept_the_action = "Bulma hielt den Jungen am Arm zurueck, bevor er ins Wasser stieg."

        stems = runner.concrete_source_detail_stems(source)
        hints = runner.concrete_detail_hint_tokens(source)
        preserved, available = runner.preserved_concrete_detail_count(
            source, kept_the_action, ""
        )
        vague = "Sie hatte eine bessere Idee und ausserdem war ihr das lieber."
        vague_preserved, _ = runner.preserved_concrete_detail_count(source, vague, "")

        # Filler like "ausserdem" or "besser" must never count as a picture.
        self.assertNotIn("ausserd", hints)
        self.assertNotIn("besser", hints)
        self.assertLess(len(hints), len(stems))
        # Keeping the visible action is what must count.
        self.assertGreaterEqual(preserved, 2)
        self.assertEqual(vague_preserved, 0)

    def test_source_nouns_and_sz_spelling_count_as_picture_words(self):
        """"weiss" written with sz never matched, hiding the whole sentence."""
        source = (
            "Am Haken flatterte ihre weisse Unterhose knapp unter der Oberflaeche."
        ).replace("weisse", "wei\u00dfe")

        hints = runner.concrete_detail_hint_tokens(source)

        self.assertIn("weiss", hints)
        self.assertIn("haken", hints)
        self.assertIn("unterhos", hints)

    def test_repair_payload_shows_the_already_placed_beats(self):
        """A blind repair reuses anchors and crosses the running order."""
        payload = {
            "cues": [
                {"id": "c001", "text": "a"},
                {"id": "c002", "text": "b"},
                {"id": "c003", "text": "c"},
            ],
            "segments": [
                {"id": "s001", "start_ms": 1_000, "text": "x"},
                {"id": "s002", "start_ms": 5_000, "text": "y"},
                {"id": "s003", "start_ms": 9_000, "text": "z"},
            ],
            "_locked_alignments": [
                {"cue_id": "c003", "anchor_segment_id": "s003", "placement": "before_anchor"},
                {"cue_id": "c001", "anchor_segment_id": "s001", "placement": "after_anchor"},
            ],
            "_repair_cue_ids": ["c002"],
        }

        model_payload = runner.scene_alignment_model_payload(payload)
        scope = model_payload["repair_scope"]

        self.assertEqual(scope["cue_ids"], ["c002"])
        self.assertEqual(
            [beat["anchor_segment_id"] for beat in scope["placed_beats"]],
            ["s001", "s003"],
        )
        self.assertEqual([beat["start_ms"] for beat in scope["placed_beats"]], [1_000, 9_000])
        self.assertIn("Reihenfolge", scope["placement_rule"])

    def test_rich_narration_passes_without_the_exact_hint_words(self):
        """76% preserved source detail is proof enough, hint list or not."""
        source = (
            "Der alte Mann zog den schweren Karren durch den Schlamm, "
            "während der Regen auf das Dach des Wagens trommelte."
        )
        rich = (
            "Der alte Mann zog den schweren Karren mühsam durch den Schlamm, "
            "der Regen trommelte auf das Dach des Wagens."
        )
        coverage = runner.measured_source_detail_coverage(source, rich, "", "visual_action")
        preserved, available = runner.preserved_concrete_detail_count(source, rich, "")

        self.assertGreaterEqual(coverage, 0.5)
        # The generous route must carry it even if the hint list barely matches.
        self.assertTrue(
            (coverage >= 0.22 and preserved >= min(2, max(0, available - 1)))
            or coverage >= max(0.5, 0.22 * 2)
        )

    def test_audio_drama_enforces_coverage_floor_from_retained_runtime(self):
        targets = {
            "ep1": {
                "min_cues": 12,
                "max_cues": 20,
                "max_gap_ms": 150_000,
                "candidate_anchor_ms": [index * 60_000 for index in range(20)],
            }
        }

        with self.assertRaisesRegex(ValueError, "12 bis 20"):
            validate_alignment_density(
                "audio_drama",
                {f"cue-{index}" for index in range(20)},
                [f"cue-{index}" for index in range(6)],
                {"ep1": 20},
                {"ep1": 6},
                targets,
                {"ep1": [index * 60_000 for index in range(6)]},
                {"ep1": [index * 60_000 for index in range(20)]},
                {"ep1": [index * 60_000 for index in range(6, 20)]},
            )

    def test_audio_drama_rejects_long_stretch_without_any_comment(self):
        targets = {
            "ep1": {
                "min_cues": 4,
                "max_cues": 20,
                "max_gap_ms": 150_000,
                "candidate_anchor_ms": [index * 60_000 for index in range(20)],
            }
        }
        selected = [0, 60_000, 120_000, 180_000, 900_000, 960_000]
        unselected = [
            (index * 60_000, f"cue-{index}")
            for index in range(20)
            if index * 60_000 not in selected
        ]

        with self.assertRaises(runner.CoverageGapError) as caught:
            validate_alignment_density(
                "audio_drama",
                {f"cue-{index}" for index in range(20)},
                [f"cue-{index}" for index in range(6)],
                {"ep1": 20},
                {"ep1": 6},
                targets,
                {"ep1": selected},
                {"ep1": [index * 60_000 for index in range(20)]},
                {"ep1": unselected},
            )

        self.assertIn("ohne Kommentar", str(caught.exception))
        # The repair must be told exactly which beats would close the gap.
        self.assertTrue(caught.exception.required_cue_ids)
        self.assertIn("cue-7", caught.exception.required_cue_ids)

    def test_exhausted_coverage_gap_is_delivered_with_a_note(self):
        """A dialogue-only stretch must not cost the whole book."""
        partial = {"alignments": [{"cue_id": "c001"}], "character_introductions": [], "notes": []}
        error = runner.AlignmentValidationError(
            "Folge 6560 laesst den Hoerer zu lange ohne Kommentar: 847-1343 s",
            partial,
            set(),
            {"c042"},
        )
        calls = []

        def failing_runner(payload, execution):
            calls.append(execution["provider"])
            raise error

        result, metadata = runner.execute_with_fallbacks(
            {"narration_density": "audio_drama"},
            [{
                "provider": "codex",
                "model": "m",
                "reasoning_effort": "xhigh",
                "timeout_seconds": 60,
            }],
            failing_runner,
            "scene-alignment",
            "disabled",
        )

        self.assertEqual(result["alignments"], partial["alignments"])
        self.assertTrue(any("ohne Kommentar" in note for note in result["notes"]))
        self.assertEqual(metadata["attempts"][-1]["reason"], "coverage_gap_reported")

    def test_a_multi_minute_gap_is_never_quietly_delivered(self):
        """The 799 s hole in 6564 was waved through by the escape hatch."""
        self.assertTrue(runner._oversized_coverage_gap(
            "Folge 6564: 540-1340 s (799 s ohne Kommentar, nimm davon c176)"))
        self.assertFalse(runner._oversized_coverage_gap(
            "Folge 6558: 100-260 s (160 s ohne Kommentar, nimm davon c012)"))
        # Measured blockers sat just above the old ceiling: 302, 306, 367 s.
        self.assertFalse(runner._oversized_coverage_gap(
            "Folge 6572: 163-530 s (367 s ohne Kommentar, nimm davon c020)"))
        self.assertTrue(runner._oversized_coverage_gap(
            "Folge X: 0-500 s (500 s ohne Kommentar, nimm davon c020)"))

        partial = {"alignments": [{"cue_id": "c001"}], "character_introductions": [], "notes": []}
        huge = runner.AlignmentValidationError(
            "Folge 6564 laesst den Hoerer zu lange ohne Kommentar: 540-1340 s (799 s ohne Kommentar)",
            partial, set(), {"c176"},
        )

        def failing_runner(payload, execution):
            raise huge

        with self.assertRaises(Exception):
            runner.execute_with_fallbacks(
                {"narration_density": "audio_drama"},
                [{"provider": "codex", "model": "m", "reasoning_effort": "xhigh", "timeout_seconds": 60}],
                failing_runner, "scene-alignment", "disabled",
            )

    def test_coverage_gap_result_survives_a_later_unrelated_failure(self):
        """A chronology slip on the next attempt must not discard the outcome."""
        partial = {"alignments": [{"cue_id": "c001"}], "character_introductions": [], "notes": []}
        gap = runner.AlignmentValidationError(
            "Folge 6560 laesst den Hoerer zu lange ohne Kommentar: 877-1343 s",
            partial,
            set(),
            {"c042"},
        )
        crossed = runner.AlignmentValidationError(
            "chronologisch ausgerichtet: c090@s1187",
            {"alignments": [], "character_introductions": [], "notes": []},
            {"c090"},
        )
        errors = [gap, crossed, crossed, crossed]

        def failing_runner(payload, execution):
            raise errors.pop(0) if errors else crossed

        result, metadata = runner.execute_with_fallbacks(
            {"narration_density": "audio_drama"},
            [{
                "provider": "codex",
                "model": "m",
                "reasoning_effort": "xhigh",
                "timeout_seconds": 60,
            }],
            failing_runner,
            "scene-alignment",
            "disabled",
        )

        self.assertEqual(result["alignments"], partial["alignments"])
        self.assertTrue(any("ohne Kommentar" in note for note in result["notes"]))

    def test_lead_in_before_the_book_start_is_not_a_gap(self):
        """Book 3 stalled 20 attempts on 739 s that belong to the previous book."""
        candidates = [111_000, 252_000, 293_000, 492_000, 851_000, 900_000]
        selected = [851_000, 900_000]
        unselected = [(ms, f"c{i:03d}") for i, ms in enumerate(candidates) if ms not in selected]

        # On a start-edge episode the lead-in must not count against the editor.
        self.assertEqual(
            runner.episode_coverage_gap_violations(
                selected, candidates, unselected, 150_000, None, "start"
            ),
            [],
        )
        # Nor does the stretch right after the pinned book-edge beat, which can
        # sit in the previous book's recap.
        pinned = [111_000, *selected]
        pinned_unselected = [
            (ms, f"c{i:03d}") for i, ms in enumerate(candidates) if ms not in pinned
        ]
        self.assertEqual(
            runner.episode_coverage_gap_violations(
                pinned, candidates, pinned_unselected, 150_000, None, "start"
            ),
            [],
        )
        # In the middle of a book the same hole is still a real defect.
        self.assertTrue(
            runner.episode_coverage_gap_violations(
                selected, candidates, unselected, 150_000, None, "none"
            )
        )

    def test_selecting_a_filler_and_anchoring_it_away_still_fails(self):
        """The real 6564 defect: 15 comments crammed early, 13 minutes silent."""
        targets = {
            "ep1": {
                "min_cues": 4,
                "max_cues": 20,
                "max_gap_ms": 150_000,
                "candidate_anchor_ms": [index * 60_000 for index in range(20)],
            }
        }
        # Every candidate is selected, but all anchors sit in the first minutes.
        crammed = [index * 10_000 for index in range(20)]
        homes = [(index * 60_000, f"cue-{index}") for index in range(20)]

        with self.assertRaises(runner.CoverageGapError) as caught:
            validate_alignment_density(
                "audio_drama",
                {f"cue-{index}" for index in range(20)},
                [f"cue-{index}" for index in range(20)],
                {"ep1": 20},
                {"ep1": 20},
                targets,
                {"ep1": crammed},
                {"ep1": [index * 60_000 for index in range(20)]},
                {"ep1": []},
                {"ep1": homes},
            )

        self.assertIn("ohne Kommentar", str(caught.exception))
        self.assertTrue(caught.exception.required_cue_ids)

    def test_audio_drama_accepts_gap_without_remaining_candidate(self):
        targets = {
            "ep1": {
                "min_cues": 4,
                "max_cues": 6,
                "max_gap_ms": 150_000,
                "candidate_anchor_ms": [0, 60_000, 120_000, 600_000],
            }
        }

        validate_alignment_density(
            "audio_drama",
            {f"cue-{index}" for index in range(4)},
            [f"cue-{index}" for index in range(4)],
            {"ep1": 4},
            {"ep1": 4},
            targets,
            {"ep1": [0, 60_000, 120_000, 600_000]},
            {"ep1": [0, 60_000, 120_000, 600_000]},
            {"ep1": []},
        )

    def test_preserves_start_and_end_book_edge_marker(self):
        request = validated_alignment_request({
            "series": {"title": "Beispielserie", "year": 1986},
            "episode_contexts": self.context,
            "cues": [{
                "id": "cue-only",
                "order": 0,
                "text": "Anfang und Ende.",
                "source_context": "Eine sehr kurze Geschichte.",
                "proposed_episode_id": "ep8",
                "book_edge": "start_and_end",
            }],
            "segments": [{
                "id": "segment-only",
                "episode_id": "ep8",
                "episode_order": 0,
                "start_ms": 1_000,
                "text": "Anfang und Ende.",
            }],
        })

        self.assertEqual(request["cues"][0]["book_edge"], "start_and_end")

    def test_rejects_unknown_book_edge_marker(self):
        with self.assertRaisesRegex(ValueError, "Buchrand"):
            validated_alignment_request({
                "episode_contexts": self.context,
                "cues": [{
                    "id": "cue-end",
                    "order": 1,
                    "text": "Das Ende.",
                    "source_context": "Das Ende.",
                    "proposed_episode_id": "ep8",
                    "book_edge": "outside",
                }],
                "segments": [{
                    "id": "segment-end",
                    "episode_id": "ep8",
                    "episode_order": 7,
                    "start_ms": 592_000,
                    "text": "Das Ende.",
                }],
            })

    def test_requires_context_for_exactly_the_subtitle_episodes(self):
        with self.assertRaisesRegex(ValueError, "Episodenkontexte"):
            validated_alignment_request({
                "episode_contexts": [{**self.context[0], "episode_id": "ep9"}],
                "cues": [{
                    "id": "cue",
                    "order": 0,
                    "text": "Eine Figur kommt an.",
                    "source_context": "Eine Figur kommt an.",
                    "proposed_episode_id": "ep8",
                    "book_edge": "none",
                }],
                "segments": [{
                    "id": "segment",
                    "episode_id": "ep8",
                    "episode_order": 0,
                    "start_ms": 1_000,
                    "text": "Da ist jemand.",
                }],
            })


class EpisodeContextRequestTest(unittest.TestCase):
    def test_sanitizes_episode_research_payload(self):
        request = validated_episode_context_request({
            "series": {"title": "Beispielserie", "year": 1986},
            "known_characters": [{"name": "Krillin", "aliases": ["Kuririn"], "introduced_in_episode": "prior", "role": "Freund", "visual_description": "Sechs Stirnpunkte", "ignored": "extra"}],
            "episodes": [{
                "id": "ep1",
                "season": 1,
                "episode": 1,
                "title": "Der Anfang",
                "summary": "Eine Begegnung.",
            }],
        })

        self.assertEqual(request["series"]["title"], "Beispielserie")
        self.assertEqual(request["episodes"][0]["id"], "ep1")

        self.assertEqual(request["known_characters"][0]["name"], "Krillin")
        self.assertEqual(request["known_characters"][0]["aliases"], ["Kuririn"])
        self.assertEqual(request["known_characters"][0]["introduced_in_episode"], "prior")
        self.assertNotIn("ignored", request["known_characters"][0])

    def test_rejects_episode_without_title(self):
        with self.assertRaisesRegex(ValueError, "Titel"):
            validated_episode_context_request({
                "episodes": [{"id": "ep1", "season": 1, "episode": 1, "title": ""}],
            })


class EpisodeContextResultTest(unittest.TestCase):
    def test_rejects_duplicate_character_first_appearances(self):
        payload = {
            "series": {"title": "Beispielserie", "year": 1986},
            "episodes": [
                {"id": "ep1", "season": 1, "episode": 1, "title": "Anfang"},
                {"id": "ep2", "season": 1, "episode": 2, "title": "Weiter"},
            ],
        }
        answer = {
            "episode_contexts": [
                {
                    "episode_id": "ep1",
                    "character_introductions": [{"name": "Mara", "role": "Kartografin"}],
                    "sources": [{"url": "https://example.test/ep1"}],
                },
                {
                    "episode_id": "ep2",
                    "character_introductions": [{"name": "Mara", "role": "Kartografin"}],
                    "sources": [{"url": "https://example.test/ep2"}],
                },
            ],
        }

        with self.assertRaisesRegex(ValueError, "nur in ihrer ersten Folge"):
            validate_episode_context_result(payload, answer)

    def test_requires_visual_description_for_a_required_introduction(self):
        payload = {
            "series": {"title": "Beispielserie", "year": 1986},
            "episodes": [{"id": "ep1", "season": 1, "episode": 1, "title": "Anfang"}],
        }
        answer = {
            "episode_contexts": [{
                "episode_id": "ep1",
                "character_introductions": [{
                    "name": "Mara",
                    "role": "Kartografin",
                    "introduction_required": True,
                }],
                "sources": [{"url": "https://example.test/ep1"}],
            }],
        }

        with self.assertRaisesRegex(ValueError, "visuelle Beschreibung"):
            validate_episode_context_result(payload, answer)


class NovelisticEditorialContractTest(unittest.TestCase):
    source = (
        "Son Goku trug einen Baumstamm über der Schulter, der breiter war als sein Körper. "
        "Sein brauner Schwanz schwang hinter ihm. Vor der Hütte zertrümmerte er den Stamm "
        "mit einem einzigen Tritt zu Brennholz."
    )

    def test_rejectable_summary_loses_concrete_action_details(self):
        coverage = measured_source_detail_coverage(
            self.source,
            "Hier lebt Son Goku, ein Bergjunge mit erstaunlicher Kraft.",
            "",
        )

        self.assertLess(coverage, 0.3)

    def test_novelistic_passage_preserves_action_and_body_details(self):
        coverage = measured_source_detail_coverage(
            self.source,
            (
                "Son Goku schleppte einen Baumstamm über der Schulter, breiter als sein Körper. "
                "Sein brauner Schwanz schwang hinter ihm. Vor der Hütte zertrümmerte er den Stamm "
                "mit einem einzigen Tritt zu Brennholz."
            ),
            "",
        )

        self.assertGreaterEqual(coverage, 0.3)

    def test_missing_detail_feedback_names_concrete_source_stems(self):
        missing = missing_source_detail_tokens(
            self.source,
            "Son Goku stand vor seiner Hütte.",
            "",
        )

        self.assertTrue({"baumstamm", "schwanz", "brennholz"} & set(missing))

    def test_fully_audible_exposition_does_not_need_repeating(self):
        coverage = measured_source_detail_coverage(
            "Sieben Dragon Balls rufen Shenlong, der genau einen Wunsch erfüllt.",
            "",
            "Sieben Dragon Balls rufen Shenlong, der genau einen Wunsch erfüllt.",
        )

        self.assertEqual(coverage, 1.0)

    def test_visual_coverage_ignores_audible_exposition_and_srt_ocr(self):
        source = (
            "Bulma setzte sich und erklärte es ihm. Es gab sieben Dragon Balls. "
            "Shenlong erfüllte genau einen Wunsch. Goku betrachtete die drei Kugeln."
        )
        subtitles = (
            "Dragon BaIIs gibt es mit einem bis sieben Sternen. "
            "ShenIong erfüIIt einem jeden Wunsch."
        )
        narration = "Bulma setzte sich. Gokus Blick blieb auf den drei Kugeln liegen."

        coverage = measured_source_detail_coverage(
            source,
            narration,
            subtitles,
            "visual_action",
        )

        self.assertGreaterEqual(coverage, 0.3)

    def test_visual_coverage_does_not_count_dialogue_bridge_as_image_detail(self):
        coverage = measured_source_detail_coverage(
            (
                "Bulma erklärte, dass sieben Kugeln einen Drachengott riefen. "
                "Er erfüllte genau einen Wunsch."
            ),
            "",
            "",
            "visual_action",
        )

        self.assertEqual(coverage, 1.0)

    def test_book_boundary_still_measures_nonvisual_source_details(self):
        coverage = measured_source_detail_coverage(
            "Am Anfang des Kapitels begann die Geschichte in einer abgeschiedenen Stadt.",
            "In einer Stadt begann ein neues Kapitel.",
            "",
            "book_boundary",
        )

        self.assertLess(coverage, 1.0)

    def test_book_boundary_cannot_drift_from_its_nearest_source_anchor(self):
        payload = {
            "series": {"title": "Beispielserie", "year": 1986},
            "preferred_names": [],
            "known_character_names": [],
            "narration_density": "detailed",
            "episode_contexts": [{
                "episode_id": "ep1",
                "synopsis": "Die Geschichte beginnt im Wald.",
                "character_introductions": [],
            }],
            "cues": [{
                "id": "c001",
                "order": 0,
                "text": "Die Geschichte beginnt im Wald.",
                "source_context": "Im abgeschiedenen Wald begann die ungewöhnliche Geschichte.",
                "proposed_episode_id": "ep1",
                "proposed_anchor_ms": 100_000,
                "nearby_subtitles": "Tief in den Bergen.",
                "book_edge": "start",
            }],
            "segments": [
                {"id": "s001", "episode_id": "ep1", "episode_order": 0, "start_ms": 100_000, "text": "Tief in den Bergen."},
                {"id": "s002", "episode_id": "ep1", "episode_order": 0, "start_ms": 150_000, "text": "Später im Wald."},
            ],
        }
        answer = {
            "alignments": [{
                "cue_id": "c001",
                "narration_text": "Im abgeschiedenen Wald begann die ungewöhnliche Geschichte.",
                "purpose": "book_boundary",
                "introduced_characters": [],
                "information_gain": "Erhält den Buchanfang.",
                "source_detail_coverage": 1.0,
                "native_audio_relation": "complements_existing_audio",
                "retained_visual_details": ["abgeschiedener Wald"],
                "anchor_segment_id": "s002",
                "placement": "before_anchor",
                "confidence": 0.98,
                "reasoning": "Zu spät verschoben.",
            }],
            "character_introductions": [],
            "name_aliases": [],
            "notes": [],
        }
        execution = {
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "timeout_seconds": 900,
        }

        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=answer),
            self.assertRaisesRegex(AlignmentValidationError, "Quellenanker") as raised,
        ):
            research_scene_alignment(payload, execution)

        self.assertEqual(raised.exception.invalid_cue_ids, {"c001"})

    def test_audio_drama_opening_can_be_the_formal_character_introduction(self):
        payload = {
            "series": {"title": "Beispielserie", "year": 1986},
            "preferred_names": ["Son Goku"],
            "known_character_names": ["Son Goku"],
            "narration_density": "audio_drama",
            "episode_contexts": [{
                "episode_id": "ep1",
                "synopsis": "Son Goku lebt in den Bergen.",
                "character_introductions": [{
                    "name": "Son Goku",
                    "visual_description": (
                        "Wildes schwarzes Stachelhaar, orangefarbener Gi und brauner Affenschwanz."
                    ),
                    "introduction_required": True,
                }],
            }],
            "cues": [{
                "id": "c001",
                "order": 0,
                "text": "Son Goku trägt den Stamm zur Hütte.",
                "source_context": (
                    "Tief im Gebirge rauschte ein Fluss durch den Wald.\n"
                    "Son Goku hatte wildes schwarzes Stachelhaar und trug einen orangefarbenen Gi. "
                    "Sein brauner Affenschwanz schwang hinter ihm, während er einen Baumstamm trug, "
                    "der breiter als sein Körper war.\n"
                    "Vor seiner Hütte zerschmetterte er den Stamm mit einem einzigen Tritt zu Brennholz."
                ),
                "proposed_episode_id": "ep1",
                "proposed_anchor_ms": 173_464,
                "nearby_subtitles": "Hallo.",
                "book_edge": "start",
            }],
            "segments": [{
                "id": "s001",
                "episode_id": "ep1",
                "episode_order": 0,
                "start_ms": 173_464,
                "end_ms": 174_089,
                "text": "Hallo.",
            }],
        }
        narration = (
            "Son Goku, mit wildem schwarzem Stachelhaar, orangefarbenem Gi und braunem "
            "Affenschwanz, trug am Fluss einen Baumstamm, breiter als sein Körper, zur Hütte. "
            "Dort zerschmetterte er ihn mit einem Tritt zu Brennholz."
        )
        answer = {
            "alignments": [{
                "cue_id": "c001",
                "narration_text": narration,
                "purpose": "character_introduction",
                "introduced_characters": ["Son Goku"],
                "information_gain": "Führt die sichtbare Hauptfigur ohne doppelte Exposition ein.",
                "source_detail_coverage": 1.0,
                "native_audio_relation": "complements_existing_audio",
                "retained_visual_details": [
                    "wildes schwarzes Stachelhaar",
                    "orangefarbener Gi",
                    "brauner Affenschwanz",
                    "überbreiter Baumstamm",
                ],
                "beat_type": "scene_setup",
                "audio_strategy": "pause_at_scene_boundary",
                "target_duration_ms": len(narration.split()) * 390,
                "anchor_segment_id": "s001",
                "placement": "before_anchor",
                "confidence": 0.98,
                "reasoning": "Ein kombinierter Eröffnungsbeat unmittelbar vor der Begrüßung.",
            }],
            "character_introductions": [{
                "research_name": "Son Goku",
                "spoken_name": "Son Goku",
                "cue_id": "c001",
                "episode_id": "ep1",
            }],
            "name_aliases": [],
            "notes": [],
        }
        execution = {
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "timeout_seconds": 900,
        }

        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=answer),
        ):
            result = research_scene_alignment(payload, execution)

        self.assertEqual(result["alignments"][0]["purpose"], "character_introduction")
        self.assertEqual(result["character_introductions"][0]["cue_id"], "c001")

    def test_combined_opening_cannot_drop_its_final_visible_action(self):
        source = (
            "Im Wald rauschte ein schmaler Fluss.\n"
            "Son Goku trug mit braunem Schwanz einen körperbreiten Baumstamm.\n"
            "Vor der Hütte nahm er Anlauf und zertrümmerte den Stamm mit einem Tritt zu Brennholz."
        )
        narration = (
            "Bei Flussrauschen trug Son Goku mit braunem Schwanz einen körperbreiten Baumstamm."
        )

        self.assertEqual(
            uncovered_concrete_source_passages(source, narration, "Hallo."),
            [3],
        )

    def test_audio_drama_visual_action_keeps_final_causal_result(self):
        source = (
            "Bulma prüfte den Boden und zeigte auf weiche Blätter.\n"
            "Sie wählte Kapsel Nummer eins, drückte den Knopf und warf sie auf den Platz.\n"
            "Ein Knall, eine Rauchwolke – dann stand dort ein rundes weißes Haus mit Tür und Fenstern."
        )

        self.assertEqual(
            uncovered_causal_visual_endpoint(
                source,
                "Bulma prüfte die weichen Blätter, wählte Kapsel eins und drückte den Knopf.",
                "Sie wirft die Kapsel. Hepp! [Explosion] Komm rein.",
            ),
            [3],
        )
        self.assertEqual(
            uncovered_causal_visual_endpoint(
                source,
                (
                    "Bulma prüfte die weichen Blätter und wählte Kapsel eins. Nach der "
                    "Rauchwolke stand dort ein rundes weißes Haus mit Tür und Fenstern."
                ),
                "Sie wirft die Kapsel. Hepp! [Explosion] Komm rein.",
            ),
            [],
        )

    def test_dialogue_at_source_end_is_not_a_visual_endpoint(self):
        source = (
            "Ein bewaffneter Bärenräuber trat mit einem langen Schwert auf den Weg.\n"
            "Goku blieb vor der Schildkröte stehen.\n"
            "»Such dir eine andere«, sagte er."
        )

        self.assertEqual(
            uncovered_causal_visual_endpoint(
                source,
                "Der bewaffnete Bärenräuber trat mit langem Schwert auf den Weg.",
                "Such dir eine andere.",
            ),
            [],
        )

    def test_visual_actions_and_book_edges_keep_hard_source_coverage(self):
        self.assertTrue(requires_visual_source_coverage("visual_action"))
        self.assertTrue(requires_visual_source_coverage("character_introduction"))
        self.assertTrue(requires_visual_source_coverage("book_boundary"))

    def test_dialogue_and_motivation_bridges_do_not_fake_visual_coverage(self):
        self.assertFalse(requires_visual_source_coverage("internal_motivation"))
        self.assertFalse(requires_visual_source_coverage("continuity_bridge"))
        self.assertFalse(requires_visual_source_coverage("offscreen_context"))

    def test_safe_validation_summary_contains_only_rule_and_cue_ids(self):
        summary = validation_failure_summary(
            ValueError(
                "Cue c021 paraphrasiert bereits hörbares Serienaudio "
                "„Die Hauptsache ist, dass mein Wunsch erfüllt wird.“"
            )
        )

        self.assertEqual(
            summary,
            {"rule": "native_audio_repetition", "cue_ids": ["c021"]},
        )

    def test_coverage_uses_native_audio_around_selected_anchor(self):
        segments = [
            {"id": "early", "episode_id": "ep1", "start_ms": 100_000, "text": "Ein belangloser früher Satz."},
            {"id": "chosen", "episode_id": "ep1", "start_ms": 800_000, "text": "Es gibt sieben Dragon Balls."},
            {"id": "near", "episode_id": "ep1", "start_ms": 805_000, "text": "Shenlong erfüllt einen Wunsch."},
            {"id": "other", "episode_id": "ep2", "start_ms": 805_000, "text": "Fremde Folge."},
        ]

        context = native_context_for_alignment(segments, "chosen")

        self.assertIn("sieben Dragon Balls", context)
        self.assertIn("Shenlong", context)
        self.assertNotIn("früher Satz", context)
        self.assertNotIn("Fremde Folge", context)

    def test_rejects_paraphrased_audible_wish(self):
        findings = native_audio_repetition_findings(
            (
                "Bulma räusperte sich. Eine Zeit lang hatte sie über einen lebenslangen "
                "Vorrat an Erdbeeren nachgedacht. Inzwischen hatte sie ein besseres Ziel: "
                "einen unglaublich süßen Freund."
            ),
            (
                "Eine Zeit lang hatte Bulma mit dem Gedanken an einen lebenslangen Vorrat "
                "Erdbeeren gespielt. Inzwischen galt ihre Entscheidung einem unglaublich "
                "süßen Freund."
            ),
            [{
                "id": "chosen",
                "episode_id": "ep1",
                "start_ms": 100_000,
                "text": "Erdbeeren wären toll, aber lieber hätte ich einen süßen Freund.",
            }],
            "chosen",
            ["Bulma"],
        )

        self.assertTrue(findings)
        self.assertIn("freund", findings[0]["matched_tokens"])

    def test_rejects_fuzzy_paraphrase_of_audible_taste(self):
        findings = native_audio_repetition_findings(
            "Die Schildkröte versicherte Goku, sie schmecke furchtbar.",
            "Während ihre Antwort über den furchtbaren Geschmack nachklang, ging Goku weiter.",
            [{
                "id": "chosen",
                "episode_id": "ep1",
                "start_ms": 100_000,
                "text": "Man sagt sogar, ich würde ganz furchtbar schmecken!",
            }],
            "chosen",
            ["Goku"],
        )

        self.assertTrue(findings)
        self.assertIn("furchtbar", findings[0]["matched_tokens"])
        self.assertIn("schmeck", findings[0]["matched_tokens"])

    def test_allows_visual_detail_that_is_not_spoken(self):
        findings = native_audio_repetition_findings(
            (
                "Unter der Oberfläche glitt ein gewaltiger Fisch heran. Sein breites Maul "
                "öffnete sich, während Gokus brauner Schwanz wie ein Köder zappelte."
            ),
            (
                "Unter Wasser öffnete ein gewaltiger Fisch sein breites Maul. Vor ihm "
                "zappelte Gokus brauner Schwanz wie ein Köder."
            ),
            [{
                "id": "chosen",
                "episode_id": "ep1",
                "start_ms": 100_000,
                "text": "Da bewegt sich etwas im Wasser!",
            }],
            "chosen",
            ["Goku"],
        )

        self.assertEqual(findings, [])

    def test_does_not_combine_unrelated_tokens_across_narration_sentences(self):
        findings = native_audio_repetition_findings(
            (
                "Der Vater bat Goku, seine Tochter zu retten. "
                "Neben ihm lag ein beschädigtes Rettungsboot."
            ),
            (
                "Seine Bitte ließ den Vater erschöpft wirken. "
                "Goku betrachtete das beschädigte Rettungsboot."
            ),
            [{
                "id": "chosen",
                "episode_id": "ep1",
                "start_ms": 100_000,
                "text": "Ich bitte dich, rette meine Tochter.",
            }],
            "chosen",
            ["Goku"],
        )

        self.assertEqual(findings, [])

    def test_audio_drama_accepts_short_visual_goku_beat_without_repeating_narrator(self):
        payload = {
            "series": {"title": "Dragon Ball", "year": 1986},
            "preferred_names": ["Son Goku"],
            "known_character_names": ["Son Goku"],
            "narration_density": "audio_drama",
            "episode_contexts": [{
                "episode_id": "ep1",
                "synopsis": "Son Goku arbeitet vor seiner Berghütte.",
                "character_introductions": [],
            }],
            "cues": [{
                "id": "c001",
                "order": 0,
                "text": "Son Goku trägt den Baumstamm.",
                "source_context": (
                    "Son Goku trug einen Baumstamm mit beiden Händen. Der Stamm war breiter "
                    "als sein Körper. Unter schwarzem Stachelhaar trug er ein orangefarbenes Gi; "
                    "sein brauner Affenschwanz schwang hinter ihm."
                ),
                "proposed_episode_id": "ep1",
                "proposed_anchor_ms": 100_000,
                "nearby_subtitles": (
                    "Früh morgens ging er Holz für seinen Ofen sammeln. "
                    "Er spaltet das Holz mit seinen Fäusten."
                ),
                "book_edge": "none",
            }],
            "segments": [{
                "id": "s001",
                "episode_id": "ep1",
                "episode_order": 0,
                "start_ms": 100_000,
                "end_ms": 104_000,
                "text": "Er spaltet das Holz mit seinen Fäusten.",
            }],
        }
        answer = {
            "alignments": [{
                "cue_id": "c001",
                "narration_text": (
                    "Unter schwarzem Stachelhaar trug Son Goku ein orangefarbenes Gi; sein "
                    "brauner Affenschwanz schwang hinter ihm. Mit beiden Händen balancierte er "
                    "den Stamm, der breiter war als sein Körper."
                ),
                "purpose": "visual_action",
                "introduced_characters": [],
                "information_gain": "Ergänzt Aussehen, Größenverhältnis und sichtbare Tragbewegung.",
                "source_detail_coverage": 0.8,
                "native_audio_relation": "complements_existing_audio",
                "retained_visual_details": [
                    "schwarzes Stachelhaar und orangefarbenes Gi",
                    "brauner Affenschwanz",
                    "Stamm breiter als sein Körper",
                ],
                "beat_type": "action_sync",
                "audio_strategy": "pause_at_scene_boundary",
                "target_duration_ms": 12_000,
                "anchor_segment_id": "s001",
                "placement": "before_anchor",
                "confidence": 0.96,
                "reasoning": "Unmittelbar vor dem hörbaren Holzspalten.",
            }],
            "character_introductions": [],
            "name_aliases": [],
            "notes": [],
        }
        execution = {
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "timeout_seconds": 900,
        }

        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=answer),
        ):
            result = research_scene_alignment(payload, execution)

        self.assertEqual(result["alignments"][0]["beat_type"], "action_sync")
        self.assertEqual(result["alignments"][0]["target_duration_ms"], 12_600)

        wrong_estimate = json.loads(json.dumps(answer))
        wrong_estimate["alignments"][0]["target_duration_ms"] = 1_000
        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=wrong_estimate),
        ):
            corrected = research_scene_alignment(payload, execution)
        self.assertEqual(corrected["alignments"][0]["narration_text"], answer["alignments"][0]["narration_text"])
        self.assertEqual(corrected["alignments"][0]["target_duration_ms"],
                         len(answer["alignments"][0]["narration_text"].split()) * runner.NARRATION_MS_PER_WORD)

    def test_audio_drama_rejects_comment_that_exceeds_spoken_duration_budget(self):
        payload = {
            "series": {"title": "Beispielserie"},
            "preferred_names": [],
            "narration_density": "audio_drama",
            "episode_contexts": [{
                "episode_id": "ep1",
                "synopsis": "Eine sichtbare Handlung.",
                "character_introductions": [],
            }],
            "cues": [{
                "id": "c001",
                "order": 0,
                "text": "Eine Handlung.",
                "source_context": (
                    "Eine rote Hand hob langsam eine breite hölzerne Kiste vom Tisch und trug "
                    "sie mit zitternden Fingern durch die lange, hell erleuchtete Halle."
                ),
                "proposed_episode_id": "ep1",
                "proposed_anchor_ms": 1_000,
                "nearby_subtitles": "Was ist das?",
                "book_edge": "none",
            }],
            "segments": [{
                "id": "s001", "episode_id": "ep1", "episode_order": 0,
                "start_ms": 1_000, "end_ms": 2_000, "text": "Was ist das?",
            }],
        }
        answer = {
            "alignments": [{
                "cue_id": "c001",
                "narration_text": " ".join([
                    "Die", "rote", "Hand", "hob", "langsam", "die", "breite", "hölzerne",
                    "Kiste", "vom", "Tisch", "und", "trug", "sie", "mit", "zitternden",
                    "Fingern", "durch", "die", "lange", "hell", "erleuchtete", "Halle",
                    "während", "ringsum", "jede", "kleine", "Bewegung", "ausführlich",
                    "sichtbar", "blieb", "und", "der", "Moment", "unnötig", "lange", "dauerte",
                    "obwohl", "niemand", "sonst", "im", "Raum", "eine", "einzige", "Regung",
                    "zeigte", "und", "das", "schwere", "Holz", "immer", "wieder", "gegen",
                    "die", "staubige", "Wand", "der", "Halle", "stieß.",
                ]),
                "purpose": "visual_action",
                "introduced_characters": [],
                "information_gain": "Beschreibt die sichtbare Bewegung.",
                "source_detail_coverage": 0.9,
                "native_audio_relation": "complements_existing_audio",
                "retained_visual_details": ["rote Hand", "hölzerne Kiste", "zitternde Finger"],
                "beat_type": "action_sync",
                "audio_strategy": "prefer_ambience_overlay",
                "target_duration_ms": 14_000,
                "anchor_segment_id": "s001",
                "placement": "before_anchor",
                "confidence": 0.95,
                "reasoning": "Vor der Frage.",
            }],
            "character_introductions": [],
            "name_aliases": [],
            "notes": [],
        }
        execution = {
            "provider": "codex", "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh", "timeout_seconds": 900,
        }

        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=answer),
            self.assertRaisesRegex(ValueError, "höchstens 44 Wörter"),
        ):
            research_scene_alignment(payload, execution)

    def test_audio_drama_rejects_an_unmapped_second_character_introduction(self):
        payload = {
            "series": {"title": "Beispielserie"},
            "preferred_names": [],
            "narration_density": "audio_drama",
            "episode_contexts": [{
                "episode_id": "ep1",
                "synopsis": "Eine Frau betritt den Hof.",
                "character_introductions": [],
            }],
            "cues": [{
                "id": "c001", "order": 0, "text": "Mara betritt den Hof.",
                "source_context": "Mara trug einen roten Mantel und einen breiten schwarzen Hut.",
                "proposed_episode_id": "ep1", "proposed_anchor_ms": 1_000,
                "nearby_subtitles": "Wer ist da?", "book_edge": "none",
            }],
            "segments": [{
                "id": "s001", "episode_id": "ep1", "episode_order": 0,
                "start_ms": 1_000, "end_ms": 2_000, "text": "Wer ist da?",
            }],
        }
        answer = {
            "alignments": [{
                "cue_id": "c001",
                "narration_text": "Mara trug einen roten Mantel und einen breiten schwarzen Hut.",
                "purpose": "character_introduction",
                "introduced_characters": ["Mara"],
                "information_gain": "Beschreibt die sichtbare Figur.",
                "source_detail_coverage": 1.0,
                "native_audio_relation": "complements_existing_audio",
                "retained_visual_details": ["roter Mantel", "breiter schwarzer Hut"],
                "beat_type": "scene_setup",
                "audio_strategy": "pause_at_scene_boundary",
                "target_duration_ms": 4_000,
                "anchor_segment_id": "s001", "placement": "before_anchor",
                "confidence": 0.95, "reasoning": "Vor der Frage.",
            }],
            "character_introductions": [],
            "name_aliases": [],
            "notes": [],
        }
        execution = {
            "provider": "codex", "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh", "timeout_seconds": 900,
        }

        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=answer),
            self.assertRaisesRegex(ValueError, "Figureneinführung genau einmal"),
        ):
            research_scene_alignment(payload, execution)

    def test_audio_drama_restores_unambiguous_required_introduction_metadata(self):
        payload = {
            "series": {"title": "Beispielserie"},
            "preferred_names": ["Prinz Pilaw"],
            "known_character_names": ["Prinz Pilaf"],
            "narration_density": "audio_drama",
            "episode_contexts": [{
                "episode_id": "ep1",
                "synopsis": "Der Herrscher erscheint.",
                "character_introductions": [{
                    "name": "Prinz Pilaf",
                    "visual_description": "Kleine blaue Gestalt mit spitzen Ohren und roter Mütze.",
                    "introduction_required": True,
                }],
            }],
            "cues": [{
                "id": "c001", "order": 0, "text": "Der Herrscher erscheint.",
                "source_context": (
                    "Prinz Pilaf war eine kleine blaue Gestalt mit spitzen Ohren und roter Mütze."
                ),
                "proposed_episode_id": "ep1", "proposed_anchor_ms": 1_000,
                "nearby_subtitles": "Verbeugt euch!", "book_edge": "none",
            }],
            "segments": [{
                "id": "s001", "episode_id": "ep1", "episode_order": 0,
                "start_ms": 1_000, "end_ms": 2_000, "text": "Verbeugt euch!",
            }],
        }
        answer = {
            "alignments": [{
                "cue_id": "c001",
                "narration_text": (
                    "Prinz Pilaw, eine kleine blaue Gestalt mit spitzen Ohren und roter Mütze, "
                    "trat mit erhobenem Kinn vor die versammelte Menge."
                ),
                "purpose": "character_introduction",
                "introduced_characters": ["Prinz Pilaw"],
                "information_gain": "Zeigt den Herrscher erstmals.",
                "source_detail_coverage": 1.0,
                "native_audio_relation": "complements_existing_audio",
                "retained_visual_details": ["kleine blaue Gestalt", "spitze Ohren", "rote Mütze"],
                "beat_type": "scene_setup",
                "audio_strategy": "pause_at_scene_boundary",
                "target_duration_ms": 8_580,
                "anchor_segment_id": "s001", "placement": "before_anchor",
                "confidence": 0.95, "reasoning": "Unmittelbar vor seinem ersten Satz.",
            }],
            "character_introductions": [],
            "name_aliases": [],
            "notes": [],
        }
        execution = {
            "provider": "codex", "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh", "timeout_seconds": 900,
        }

        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=answer),
        ):
            result = research_scene_alignment(payload, execution)

        self.assertEqual(result["character_introductions"], [{
            "research_name": "Prinz Pilaf",
            "spoken_name": "Prinz Pilaw",
            "cue_id": "c001",
            "episode_id": "ep1",
        }])
        self.assertIn("wiederhergestellt", result["notes"][-1])

    def test_required_character_uses_dedicated_introduction_candidate(self):
        payload = {
            "series": {"title": "Beispielserie"},
            "preferred_names": [],
            "narration_density": "audio_drama",
            "episode_contexts": [{
                "episode_id": "ep1", "synopsis": "Mai erscheint.",
                "character_introductions": [{
                    "name": "Mai",
                    "visual_description": "Langes dunkles Haar und militärische Dienstkluft.",
                    "introduction_required": True,
                }],
            }],
            "cues": [
                {
                    "id": "c001", "order": 0, "text": "Ein Haus erscheint.",
                    "source_context": "Ein rundes weißes Haus mit blank poliertem Dach erschien nach einem lauten Knall mitten auf der leeren staubigen Ebene.",
                    "proposed_episode_id": "ep1", "proposed_anchor_ms": 1_000,
                    "nearby_subtitles": "Was ist das?", "book_edge": "none",
                },
                {
                    "id": "c002", "order": 1, "text": "Mai erscheint.",
                    "source_context": "Mai war eine junge Frau mit langem dunklem Haar und militärischer Dienstkluft, die mit schnellen Schritten aus dem Schatten trat.",
                    "proposed_episode_id": "ep1", "proposed_anchor_ms": 10_000,
                    "nearby_subtitles": "Bist du das, Mai?", "book_edge": "none",
                    "character_introduction_candidate": True,
                    "introduction_research_name": "Mai",
                },
                {
                    "id": "c003", "order": 2, "text": "Eine Tür öffnet sich.",
                    "source_context": "Eine rote Tür mit goldenem Griff öffnete sich langsam und gab den Blick auf eine enge steinerne Treppe frei.",
                    "proposed_episode_id": "ep1", "proposed_anchor_ms": 20_000,
                    "nearby_subtitles": "Komm herein.", "book_edge": "none",
                },
                {
                    "id": "c004", "order": 3, "text": "Ein Licht geht an.",
                    "source_context": "An der hohen Decke flammte eine gelbe Lampe auf und tauchte den ganzen Raum in warmes flackerndes Licht.",
                    "proposed_episode_id": "ep1", "proposed_anchor_ms": 30_000,
                    "nearby_subtitles": "Es ist hell.", "book_edge": "none",
                },
            ],
            "segments": [
                {"id": "s001", "episode_id": "ep1", "episode_order": 0, "start_ms": 1_000, "text": "Was ist das?"},
                {"id": "s002", "episode_id": "ep1", "episode_order": 0, "start_ms": 10_000, "text": "Bist du das, Mai?"},
                {"id": "s003", "episode_id": "ep1", "episode_order": 0, "start_ms": 20_000, "text": "Komm herein."},
                {"id": "s004", "episode_id": "ep1", "episode_order": 0, "start_ms": 30_000, "text": "Es ist hell."},
            ],
        }
        common = {
            "purpose": "visual_action", "introduced_characters": [],
            "information_gain": "Neue sichtbare Handlung.", "source_detail_coverage": 1.0,
            "native_audio_relation": "complements_existing_audio",
            "retained_visual_details": ["sichtbares Detail"], "beat_type": "action_sync",
            "audio_strategy": "pause_at_scene_boundary", "target_duration_ms": 3_000,
            "placement": "before_anchor", "confidence": 0.95, "reasoning": "An der Szene.",
        }
        answer = {
            "alignments": [
                {**common, "cue_id": "c001", "anchor_segment_id": "s001", "narration_text": "Ein rundes weißes Haus mit blank poliertem Dach erschien nach einem lauten Knall mitten auf der leeren staubigen Ebene.", "target_duration_ms": 7_400},
                {**common, "cue_id": "c002", "anchor_segment_id": "s002", "narration_text": "Mai, eine junge Frau mit langem dunklem Haar und militärischer Dienstkluft, trat mit schnellen Schritten aus dem Schatten.", "purpose": "character_introduction", "introduced_characters": ["Mai"], "retained_visual_details": ["langes dunkles Haar", "militärische Dienstkluft"], "target_duration_ms": 7_400},
                {**common, "cue_id": "c003", "anchor_segment_id": "s003", "narration_text": "Eine rote Tür mit goldenem Griff öffnete sich langsam und gab den Blick auf eine enge steinerne Treppe frei.", "target_duration_ms": 7_400},
                {**common, "cue_id": "c004", "anchor_segment_id": "s004", "narration_text": "An der hohen Decke flammte eine gelbe Lampe auf und tauchte den ganzen Raum in warmes flackerndes Licht.", "target_duration_ms": 7_000},
            ],
            "character_introductions": [{"research_name": "Mai", "spoken_name": "Mai", "cue_id": "c002", "episode_id": "ep1"}],
            "name_aliases": [], "notes": [],
        }
        execution = {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh", "timeout_seconds": 900}

        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=answer),
        ):
            result = research_scene_alignment(payload, execution)
        self.assertEqual(result["character_introductions"][0]["cue_id"], "c002")

        sparse_payload = json.loads(json.dumps(payload))
        times = {"s001": 100000, "s002": 200000, "s003": 400000, "s004": 500000}
        for segment in sparse_payload["segments"]:
            segment["start_ms"] = times[segment["id"]]
            segment["end_ms"] = segment["start_ms"] + 500
        for cue in sparse_payload["cues"]:
            cue["proposed_anchor_ms"] = {"c001": 100000, "c002": 250000, "c003": 400000, "c004": 500000}[cue["id"]]
        sparse_payload["coverage_targets"] = {"ep1": {"min_cues": 4, "max_cues": 4, "max_gap_ms": 150000}}
        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=json.loads(json.dumps(answer))),
        ):
            # A guessed introduction hint is not a missing novel beat. Its
            # required first appearance is still validated independently.
            research_scene_alignment(sparse_payload, execution)

        wrong = json.loads(json.dumps(answer))
        wrong["character_introductions"][0]["cue_id"] = "c001"
        wrong_payload = json.loads(json.dumps(payload))
        wrong_payload["narration_density"] = "detailed"
        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=wrong),
            self.assertRaisesRegex(AlignmentValidationError, "dedizierter Einführungscue") as caught,
        ):
            research_scene_alignment(wrong_payload, execution)
        self.assertIn("c002", caught.exception.required_cue_ids)
        self.assertIn("c001", caught.exception.invalid_cue_ids)

        retry_scopes = []
        retry_intro_locks = []
        def repair_runner(request, execution):
            retry_scopes.append(set(request.get("_repair_cue_ids", [])))
            retry_intro_locks.append({item["cue_id"] for item in request.get("_locked_character_introductions", [])})
            return research_scene_alignment(request, execution)
        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", side_effect=[wrong, answer]),
        ):
            repaired, _ = runner.execute_with_fallbacks(
                {**wrong_payload, "_repair_cue_ids": ["c001", "c002"]},
                [execution], repair_runner, "scene-alignment", "disabled",
            )
        self.assertIn("c002", retry_scopes[1])
        self.assertNotIn("c001", retry_intro_locks[1])
        self.assertEqual(repaired["character_introductions"][0]["cue_id"], "c002")

        missing_payload = json.loads(json.dumps(payload))
        missing_payload["narration_density"] = "detailed"
        missing_payload["episode_contexts"][0]["character_introductions"][0]["name"] = "Pool"
        missing_payload["_engine_locked_cue_ids"] = ["c001"]
        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=answer),
            self.assertRaises(AlignmentValidationError) as missing,
        ):
            research_scene_alignment(missing_payload, execution)
        self.assertNotIn("c001", missing.exception.invalid_cue_ids)
        self.assertIn("c002", missing.exception.invalid_cue_ids)

        later_payload = json.loads(json.dumps(payload))
        later_payload["episode_contexts"][0]["character_introductions"][0]["introduction_required"] = False
        duplicate = json.loads(json.dumps(answer))
        duplicate["character_introductions"].append(dict(duplicate["character_introductions"][0]))
        for bad_payload, bad_answer, message in (
            (later_payload, answer, "nicht erforderlich"),
            (payload, duplicate, "genau einmal"),
        ):
            with self.subTest(message):
                with (
                    patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
                    patch.object(runner, "run_structured_agent", return_value=bad_answer),
                    self.assertRaisesRegex(AlignmentValidationError, message) as caught,
                ):
                    research_scene_alignment(bad_payload, execution)
                self.assertIn("c002", caught.exception.invalid_cue_ids)

    def test_introduction_metadata_recovery_refuses_ambiguous_cues(self):
        payload = {
            "preferred_names": ["Mara"],
            "episode_contexts": [{
                "episode_id": "ep1",
                "character_introductions": [{
                    "name": "Mara",
                    "visual_description": "Roter Mantel und breiter schwarzer Hut.",
                    "introduction_required": True,
                }],
            }],
            "segments": [
                {"id": "s001", "episode_id": "ep1"},
                {"id": "s002", "episode_id": "ep1"},
            ],
        }
        alignments = [
            {
                "cue_id": cue_id,
                "narration_text": "Mara trug einen roten Mantel und einen breiten schwarzen Hut.",
                "purpose": "character_introduction",
                "introduced_characters": ["Mara"],
                "anchor_segment_id": segment_id,
                "placement": "before_anchor",
            }
            for cue_id, segment_id in (("c001", "s001"), ("c002", "s002"))
        ]
        answer = {"alignments": alignments, "character_introductions": [], "notes": []}

        restored = reconcile_grounded_character_introductions(answer, payload, alignments)

        self.assertEqual(restored, 0)
        self.assertEqual(answer["character_introductions"], [])

    def test_recycled_details_are_allowed_once_the_scenes_are_far_apart(self):
        """A long fight against one creature reuses "Zunge" by necessity."""
        payload = {
            "series": {"title": "Beispielserie"},
            "preferred_names": [],
            "narration_density": "audio_drama",
            "episode_contexts": [{
                "episode_id": "ep1", "synopsis": "Ein langer Kampf.",
                "character_introductions": [],
            }],
            "cues": [
                {"id": "c001", "order": 0, "text": "Erste Szene",
                 "source_context": "Die gruene Zunge des runden Monsters schnellte zwischen den Zaehnen hervor.",
                 "proposed_episode_id": "ep1", "proposed_anchor_ms": 10_000,
                 "nearby_subtitles": "Pass auf!", "book_edge": "none"},
                {"id": "c002", "order": 1, "text": "Spaetere Szene",
                 "source_context": "Die gruene Zunge des runden Monsters schnellte erneut zwischen den Zaehnen hervor.",
                 "proposed_episode_id": "ep1", "proposed_anchor_ms": 500_000,
                 "nearby_subtitles": "Schon wieder!", "book_edge": "none"},
            ],
            "segments": [
                {"id": "s001", "episode_id": "ep1", "episode_order": 0, "start_ms": 10_000, "end_ms": 11_000, "text": "Pass auf!"},
                {"id": "s002", "episode_id": "ep1", "episode_order": 0, "start_ms": 500_000, "end_ms": 501_000, "text": "Schon wieder!"},
            ],
        }
        common = {
            "purpose": "visual_action", "introduced_characters": [],
            "information_gain": "Zeigt die sichtbare Aktion.", "source_detail_coverage": 1.0,
            "native_audio_relation": "no_relevant_existing_narration",
            "retained_visual_details": ["gruene Zunge", "runder Koerper"],
            "beat_type": "action_sync", "audio_strategy": "pause_at_scene_boundary",
            "target_duration_ms": 7_400, "placement": "before_anchor",
            "confidence": 0.95, "reasoning": "Am Anker.",
        }
        text = (
            "Die gruene Zunge des runden Monsters schnellte zwischen den gelben Zaehnen "
            "hervor und peitschte quer durch die staubige Halle."
        )
        answer = {
            "alignments": [
                {**common, "cue_id": "c001", "anchor_segment_id": "s001", "narration_text": text},
                {**common, "cue_id": "c002", "anchor_segment_id": "s002", "narration_text": text},
            ],
            "character_introductions": [], "name_aliases": [], "notes": [],
        }
        execution = {"provider": "codex", "model": "m", "reasoning_effort": "xhigh", "timeout_seconds": 900}

        # 490 s apart: the listener does not perceive this as a repetition.
        self.assertGreater(500_000 - 10_000, runner.CROSS_CUE_REPETITION_WINDOW_MS)
        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=answer),
        ):
            result = research_scene_alignment(payload, execution)
        self.assertEqual(len(result["alignments"]), 2)

    def test_audio_drama_rejects_recycled_visual_details_across_beats(self):
        payload = {
            "series": {"title": "Beispielserie"},
            "preferred_names": [],
            "narration_density": "audio_drama",
            "episode_contexts": [{
                "episode_id": "ep1", "synopsis": "Zwei Szenen.",
                "character_introductions": [],
            }],
            "cues": [
                {"id": "c001", "order": 0, "text": "Erste Szene", "source_context": "Eine rote Tür mit goldenem Griff öffnete sich langsam zum dunklen Hof.", "proposed_episode_id": "ep1", "proposed_anchor_ms": 1_000, "nearby_subtitles": "Wer ist da?", "book_edge": "none"},
                {"id": "c002", "order": 1, "text": "Zweite Szene", "source_context": "Die rote Tür mit goldenem Griff stand nun offen zum dunklen Hof.", "proposed_episode_id": "ep1", "proposed_anchor_ms": 10_000, "nearby_subtitles": "Komm heraus!", "book_edge": "none"},
            ],
            "segments": [
                {"id": "s001", "episode_id": "ep1", "episode_order": 0, "start_ms": 1_000, "end_ms": 2_000, "text": "Wer ist da?"},
                {"id": "s002", "episode_id": "ep1", "episode_order": 0, "start_ms": 10_000, "end_ms": 11_000, "text": "Komm heraus!"},
            ],
        }
        common = {
            "purpose": "visual_action", "introduced_characters": [],
            "information_gain": "Beschreibt das sichtbare Bild.",
            "source_detail_coverage": 1.0,
            "native_audio_relation": "complements_existing_audio",
            "retained_visual_details": ["rote Tür", "goldener Griff", "dunkler Hof"],
            "beat_type": "action_sync", "audio_strategy": "pause_at_scene_boundary",
            "target_duration_ms": 6_300, "placement": "before_anchor",
            "confidence": 0.95, "reasoning": "Am Szenenanker.",
        }
        answer = {
            "alignments": [
                {**common, "cue_id": "c001", "anchor_segment_id": "s001", "narration_text": "Die rote Tür mit goldenem Griff öffnete sich langsam nach außen zum dunklen Hof."},
                {**common, "cue_id": "c002", "anchor_segment_id": "s002", "narration_text": "Die rote Tür mit goldenem Griff stand nun weit nach außen zum dunklen Hof."},
            ],
            "character_introductions": [], "name_aliases": [], "notes": [],
        }
        execution = {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh", "timeout_seconds": 900}

        for locked in ([], ["c001"], ["c002"]):
            with self.subTest(locked=locked):
                with (
                    patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
                    patch.object(runner, "run_structured_agent", return_value=json.loads(json.dumps(answer))),
                    self.assertRaises(runner.AlignmentValidationError) as caught,
                ):
                    research_scene_alignment({**payload, "_engine_locked_cue_ids": locked}, execution)
                self.assertIn("bereits erzählte Bilddetails", str(caught.exception))
                self.assertEqual(caught.exception.invalid_cue_ids, {"c001", "c002"} - set(locked))

    def test_scene_validator_rejects_paraphrased_native_audio(self):
        payload = {
            "series": {"title": "Beispielserie", "year": 1986},
            "preferred_names": [],
            "narration_density": "detailed",
            "episode_contexts": [{
                "episode_id": "ep1",
                "synopsis": "Bulma nennt ihren Wunsch.",
                "character_introductions": [],
            }],
            "cues": [{
                "id": "c001",
                "order": 0,
                "text": "Bulmas Wunsch",
                "source_context": (
                    "Bulma trommelte mit den Fingern. Eine Zeit lang hatte sie über einen "
                    "Vorrat Erdbeeren nachgedacht. Nun wollte sie einen süßen Freund."
                ),
                "proposed_episode_id": "ep1",
                "proposed_anchor_ms": 100_000,
                "nearby_subtitles": "Erdbeeren wären toll, aber lieber hätte ich einen süßen Freund.",
                "book_edge": "none",
            }],
            "segments": [{
                "id": "s001",
                "episode_id": "ep1",
                "episode_order": 0,
                "start_ms": 100_000,
                "text": "Erdbeeren wären toll, aber lieber hätte ich einen süßen Freund.",
            }],
        }
        answer = {
            "alignments": [{
                "cue_id": "c001",
                "narration_text": (
                    "Eine Zeit lang hatte Bulma mit dem Gedanken an Erdbeeren gespielt. "
                    "Nun wollte sie einen unglaublich süßen Freund."
                ),
                "purpose": "internal_motivation",
                "introduced_characters": [],
                "information_gain": "Beschreibt ihren Wunsch.",
                "source_detail_coverage": 0.9,
                "native_audio_relation": "complements_existing_audio",
                "retained_visual_details": [],
                "anchor_segment_id": "s001",
                "placement": "after_anchor",
                "confidence": 0.95,
                "reasoning": "Am Wunschdialog.",
            }],
            "character_introductions": [],
            "name_aliases": [],
            "notes": [],
        }
        execution = {
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "timeout_seconds": 900,
        }

        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=answer),
            self.assertRaisesRegex(ValueError, "paraphrasiert bereits hörbares Serienaudio"),
        ):
            research_scene_alignment(payload, execution)

    def test_scene_validator_uses_source_cue_order_not_json_array_order(self):
        payload = {
            "series": {"title": "Beispielserie", "year": 1986},
            "preferred_names": [],
            "narration_density": "detailed",
            "episode_contexts": [{
                "episode_id": "ep1",
                "synopsis": "Zwei aufeinanderfolgende sichtbare Handlungen.",
                "character_introductions": [],
            }],
            "cues": [
                {
                    "id": "c001",
                    "order": 0,
                    "text": "Erste Handlung",
                    "source_context": "Eine Hand hob eine rote Schale vom Tisch.",
                    "proposed_episode_id": "ep1",
                    "proposed_anchor_ms": 100_000,
                    "nearby_subtitles": "Was ist das?",
                    "book_edge": "none",
                },
                {
                    "id": "c002",
                    "order": 1,
                    "text": "Zweite Handlung",
                    "source_context": "Danach stellte die Hand die rote Schale ins Regal.",
                    "proposed_episode_id": "ep1",
                    "proposed_anchor_ms": 200_000,
                    "nearby_subtitles": "Stell es weg.",
                    "book_edge": "none",
                },
            ],
            "segments": [
                {
                    "id": "s001",
                    "episode_id": "ep1",
                    "episode_order": 0,
                    "start_ms": 100_000,
                    "text": "Was ist das?",
                },
                {
                    "id": "s002",
                    "episode_id": "ep1",
                    "episode_order": 0,
                    "start_ms": 200_000,
                    "text": "Stell es weg.",
                },
            ],
        }
        first = {
            "cue_id": "c001",
            "narration_text": "Eine Hand hob die rote Schale vom Tisch.",
            "purpose": "visual_action",
            "introduced_characters": [],
            "information_gain": "Zeigt die erste Bewegung.",
            "source_detail_coverage": 1.0,
            "native_audio_relation": "complements_existing_audio",
            "retained_visual_details": ["rote Schale", "vom Tisch gehoben"],
            "anchor_segment_id": "s001",
            "placement": "before_anchor",
            "confidence": 0.95,
            "reasoning": "Erste Szene.",
        }
        second = {
            "cue_id": "c002",
            "narration_text": "Danach stellte die Hand die rote Schale ins Regal.",
            "purpose": "visual_action",
            "introduced_characters": [],
            "information_gain": "Zeigt die folgende Bewegung.",
            "source_detail_coverage": 1.0,
            "native_audio_relation": "complements_existing_audio",
            "retained_visual_details": ["rote Schale", "ins Regal gestellt"],
            "anchor_segment_id": "s002",
            "placement": "before_anchor",
            "confidence": 0.95,
            "reasoning": "Zweite Szene.",
        }
        answer = {
            "alignments": [second, first],
            "character_introductions": [],
            "name_aliases": [],
            "notes": [],
        }
        execution = {
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "timeout_seconds": 900,
        }

        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=answer),
        ):
            result = research_scene_alignment(payload, execution)

        self.assertEqual(
            [item["cue_id"] for item in result["alignments"]],
            ["c001", "c002"],
        )

        crossed = {
            **answer,
            "alignments": [
                {**first, "anchor_segment_id": "s002"},
                {**second, "anchor_segment_id": "s001"},
            ],
        }
        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=crossed),
        ):
            with self.assertRaisesRegex(ValueError, "chronologisch ausgerichtet") as raised:
                research_scene_alignment(payload, execution)
        self.assertIsInstance(raised.exception, AlignmentValidationError)
        self.assertEqual(raised.exception.invalid_cue_ids, {"c001", "c002"})
        self.assertIn("c001@s002 liegt nach c002@s001", str(raised.exception))

        floating_introduction = {
            **answer,
            "alignments": [
                {
                    **first,
                    "anchor_segment_id": "s002",
                    "purpose": "character_introduction",
                    "introduced_characters": ["Mara"],
                    "narration_text": (
                        "Mara hob mit beiden Händen eine rote Schale vom Tisch."
                    ),
                    "retained_visual_details": ["beide Hände", "rote Schale"],
                },
                {**second, "anchor_segment_id": "s001"},
            ],
        }
        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(
                runner,
                "run_structured_agent",
                return_value=floating_introduction,
            ),
        ):
            floated = research_scene_alignment(payload, execution)
        self.assertEqual(
            [item["cue_id"] for item in floated["alignments"]],
            ["c002", "c001"],
        )

        duplicate = {
            **answer,
            "alignments": [
                first,
                {**second, "anchor_segment_id": "s001"},
            ],
        }
        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=duplicate),
        ):
            with self.assertRaisesRegex(ValueError, "Szenenanker mehrfach") as raised:
                research_scene_alignment(payload, execution)
        self.assertIsInstance(raised.exception, AlignmentValidationError)
        self.assertEqual(raised.exception.invalid_cue_ids, {"c001", "c002"})
        self.assertIn("s001: c001, c002", str(raised.exception))

    def test_duplicate_anchor_repair_identifies_only_conflicting_cues(self):
        answer = {
            "alignments": [
                {"cue_id": "c001", "anchor_segment_id": "s001"},
                {"cue_id": "c002", "anchor_segment_id": "s002"},
                {"cue_id": "c003", "anchor_segment_id": "s002"},
            ],
            "character_introductions": [],
            "name_aliases": [],
            "notes": [],
        }
        error = AlignmentValidationError(
            "Codex-Skript verwendet einen Szenenanker mehrfach; "
            "verteile ausschließlich diese Cue-Gruppen auf unterschiedliche "
            "chronologische Anker: s002: c002, c003",
            answer,
            {"c002", "c003"},
        )
        payloads = []

        def run(payload, _execution):
            payloads.append(payload)
            if len(payloads) == 1:
                raise error
            return {"ok": True}

        result, _ = execute_with_fallbacks(
            {"cues": []},
            [{
                "provider": "codex",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "xhigh",
                "timeout_seconds": 900,
            }],
            run,
            "scene-alignment",
            "disabled",
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            [item["cue_id"] for item in payloads[1]["_locked_alignments"]],
            ["c001"],
        )
        self.assertEqual(
            validation_failure_summary(error),
            {"rule": "duplicate_anchor", "cue_ids": ["c002", "c003"]},
        )

    def test_scene_validator_reports_anchor_and_text_failures_together(self):
        payload = {
            "series": {"title": "Beispielserie", "year": 1986},
            "preferred_names": [],
            "narration_density": "detailed",
            "episode_contexts": [{
                "episode_id": "ep1",
                "synopsis": "Zwei sichtbare Handlungen.",
                "character_introductions": [],
            }],
            "cues": [
                {
                    "id": "c001",
                    "order": 0,
                    "text": "Erste Handlung",
                    "source_context": "Eine Hand hob eine rote Schale vom Tisch.",
                    "proposed_episode_id": "ep1",
                    "proposed_anchor_ms": 100_000,
                    "nearby_subtitles": "Was ist das?",
                    "book_edge": "none",
                },
                {
                    "id": "c002",
                    "order": 1,
                    "text": "Zweite Handlung",
                    "source_context": "Danach stellte sie die rote Schale ins hohe Regal.",
                    "proposed_episode_id": "ep1",
                    "proposed_anchor_ms": 200_000,
                    "nearby_subtitles": "Stell es weg.",
                    "book_edge": "none",
                },
            ],
            "segments": [
                {
                    "id": "s001",
                    "episode_id": "ep1",
                    "episode_order": 0,
                    "start_ms": 100_000,
                    "text": "Was ist das?",
                },
                {
                    "id": "s002",
                    "episode_id": "ep1",
                    "episode_order": 0,
                    "start_ms": 200_000,
                    "text": "Stell es weg.",
                },
            ],
        }
        answer = {
            "alignments": [
                {
                    "cue_id": "c001",
                    "narration_text": "Eine rote Schale.",
                    "purpose": "visual_action",
                    "introduced_characters": [],
                    "information_gain": "Zeigt die Schale.",
                    "source_detail_coverage": 1.0,
                    "native_audio_relation": "complements_existing_audio",
                    "retained_visual_details": ["rote Schale"],
                    "anchor_segment_id": "s002",
                    "placement": "before_anchor",
                    "confidence": 0.95,
                    "reasoning": "Erste Szene.",
                },
                {
                    "cue_id": "c002",
                    "narration_text": "Danach blieb alles still.",
                    "purpose": "visual_action",
                    "introduced_characters": [],
                    "information_gain": "Überbrückt die Szene.",
                    "source_detail_coverage": 1.0,
                    "native_audio_relation": "complements_existing_audio",
                    "retained_visual_details": ["stille Szene"],
                    "anchor_segment_id": "s002",
                    "placement": "before_anchor",
                    "confidence": 0.95,
                    "reasoning": "Zweite Szene.",
                },
            ],
            "character_introductions": [],
            "name_aliases": [],
            "notes": [],
        }
        execution = {
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "timeout_seconds": 900,
        }

        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=answer),
        ):
            with self.assertRaises(AlignmentValidationError) as raised:
                research_scene_alignment(payload, execution)

        self.assertIn("Szenenanker mehrfach", str(raised.exception))
        self.assertIn("Romanmerkmale", str(raised.exception))
        self.assertEqual(raised.exception.invalid_cue_ids, {"c001", "c002"})
        self.assertEqual(
            validation_failure_summary(raised.exception),
            {"rule": "multiple", "cue_ids": ["c001", "c002"]},
        )

    def test_character_repair_feedback_includes_grounded_visual_description(self):
        payload = {
            "series": {"title": "Beispielserie", "year": 1986},
            "preferred_names": [],
            "narration_density": "detailed",
            "episode_contexts": [{
                "episode_id": "ep1",
                "synopsis": "Oolong betritt das Dorf.",
                "character_introductions": [{
                    "name": "Oolong",
                    "role": "Gestaltwandler",
                    "distinguishing_traits": "",
                    "visual_description": "Kleines rosafarbenes anthropomorphes Schwein.",
                    "first_appearance": "Im Dorf.",
                    "introduction_required": True,
                }],
            }],
            "cues": [{
                "id": "c001",
                "order": 0,
                "text": "Oolong erscheint",
                "source_context": (
                    "Oolong trat aus dem Haus. Er hob die Arme und stampfte über den Dorfplatz, "
                    "während sich alle Türen schlossen. In Wahrheit war er ein kleines "
                    "rosafarbenes anthropomorphes Schwein."
                ),
                "proposed_episode_id": "ep1",
                "proposed_anchor_ms": 100_000,
                "nearby_subtitles": "Wer kommt dort?",
                "book_edge": "none",
            }],
            "segments": [{
                "id": "s001",
                "episode_id": "ep1",
                "episode_order": 0,
                "start_ms": 100_000,
                "text": "Wer kommt dort?",
            }],
        }
        answer = {
            "alignments": [{
                "cue_id": "c001",
                "narration_text": (
                    "Oolong trat aus dem Haus, hob die Arme und stampfte über den Dorfplatz. "
                    "Hinter ihm schlossen sich alle Türen. Das Schwein blieb stehen."
                ),
                "purpose": "character_introduction",
                "introduced_characters": ["Oolong"],
                "information_gain": "Zeigt Oolongs Auftreten.",
                "source_detail_coverage": 0.9,
                "native_audio_relation": "complements_existing_audio",
                "retained_visual_details": ["Schwein"],
                "anchor_segment_id": "s001",
                "placement": "before_anchor",
                "confidence": 0.95,
                "reasoning": "Erster Auftritt.",
            }],
            "character_introductions": [{
                "research_name": "Oolong",
                "spoken_name": "Oolong",
                "cue_id": "c001",
                "episode_id": "ep1",
            }],
            "name_aliases": [],
            "notes": [],
        }
        execution = {
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "timeout_seconds": 900,
        }

        with (
            patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
            patch.object(runner, "run_structured_agent", return_value=answer),
        ):
            with self.assertRaises(AlignmentValidationError) as raised:
                research_scene_alignment(payload, execution)

        self.assertIn("Kleines rosafarbenes anthropomorphes Schwein", str(raised.exception))
        self.assertEqual(raised.exception.invalid_cue_ids, {"c001"})

    def test_character_introduction_uses_two_visible_traits(self):
        self.assertGreaterEqual(
            visual_detail_match_count(
                "Violettes Haar, blaues Kleid und schmale Statur.",
                "Bulma hatte violettes Haar und trug ein blaues Kleid.",
                "Bulma",
            ),
            2,
        )


class NarrationPromptTimingTest(unittest.TestCase):
    def test_prompt_uses_validator_word_timing_for_every_density(self):
        for density in ("audio_drama", "detailed"):
            with self.subTest(density=density):
                payload = {"series": {"title": "Test"}, "cues": [], "segments": [],
                           "episode_contexts": [], "narration_density": density}
                with (
                    patch.object(runner, "NARRATION_MS_PER_WORD", 480),
                    patch.object(runner, "provider_auth_status", return_value={"authenticated": True}),
                    patch.object(runner, "run_structured_agent", side_effect=RuntimeError("captured prompt")) as call,
                    self.assertRaisesRegex(RuntimeError, "captured prompt"),
                ):
                    research_scene_alignment(payload, {"provider": "zai"})
                prompt = call.call_args.kwargs["prompt"]
                self.assertNotIn("390 ms", prompt)
                self.assertIn("Wortzahl mal ungefähr 480 ms", prompt)
                if density == "audio_drama":
                    self.assertIn("ungefähr Wortzahl mal 480 ms", prompt)


class GroundedNameAliasTest(unittest.TestCase):
    cues = [{
        "source_context": "Kuririn kommt mit Puar. Später hilft Yamchas Freund. Kinto’un trägt beide.",
    }]
    segments = [{
        "text": "Krillin begrüßt Pool, während Yamchu von Jindujun springt.",
    }]

    def test_keeps_only_names_grounded_in_both_corpora(self):
        accepted, rejected = filter_grounded_name_aliases(
            [
                {"canonical": "Krillin", "aliases": ["Kuririn"], "evidence": "Beide Texte", "confidence": 0.98},
                {"canonical": "Giran", "aliases": ["Girran"], "evidence": "Modellwissen", "confidence": 0.91},
                {"canonical": "Yamchu", "aliases": ["Yamcha", "nicht vorhanden"], "evidence": "Beide Texte", "confidence": 0.95},
                {"canonical": "Pool", "aliases": ["Pool"], "evidence": "Identisch", "confidence": 0.99},
                {"canonical": "Jindujun", "aliases": ["Kinto'un"], "evidence": "Beide Texte", "confidence": 0.96},
            ],
            self.cues,
            self.segments,
        )

        self.assertEqual(rejected, 2)
        self.assertEqual(
            [(entry["canonical"], entry["aliases"]) for entry in accepted],
            [
                ("Krillin", ["Kuririn"]),
                ("Yamchu", ["Yamcha"]),
                ("Jindujun", ["Kinto'un"]),
            ],
        )

    def test_accepts_unicode_punctuation_and_genitive_evidence_without_substrings(self):
        corpus = normalized_evidence_text("Kinto’un und Yamchas Freund, aber kein Namensschild.")

        self.assertTrue(evidence_contains_name(corpus, "Kinto'un"))
        self.assertTrue(evidence_contains_name(corpus, "Yamcha"))
        self.assertFalse(evidence_contains_name(corpus, "Nam"))

    def test_low_confidence_name_is_optional_and_filtered(self):
        accepted, rejected = filter_grounded_name_aliases(
            [{"canonical": "Krillin", "aliases": ["Kuririn"], "evidence": "unsicher", "confidence": 0.6}],
            self.cues,
            self.segments,
        )

        self.assertEqual(accepted, [])
        self.assertEqual(rejected, 1)


class ReviewedPictureRepetitionTest(unittest.TestCase):
    def test_two_consecutive_empty_answers_stop_without_fourteen_blind_retries(self):
        calls = []
        def empty(payload, execution):
            calls.append(payload)
            raise runner.EmptyAlignmentError("No alignments: missing source scenes")
        with self.assertRaisesRegex(runner.EmptyAlignmentError, "missing source scenes"):
            execute_with_fallbacks({"cues": []}, [{"provider": "codex", "model": "gpt-6-astra",
                "reasoning_effort": "automatic", "timeout_seconds": 600}], empty, "scene-alignment", "disabled")
        self.assertEqual(len(calls), 2)

    def test_short_word_resemblance_does_not_repeat_a_command(self):
        source = "Tenshinhans drittes Auge sitzt mitten auf der angespannten Stirn."
        segments = [{"id": "spoken", "episode_id": "ep", "start_ms": 370000,
                     "text": "Kommt, setzt den Kampf bitte fort!"}]
        self.assertEqual(native_audio_repetition_findings(source, source, segments, "spoken", ["Tenshinhan"]), [])

    def test_explicit_name_spacing_does_not_make_a_body_part_a_repetition(self):
        source = "Chaozu hält einen hellblauen Beutel an seinen Kopf."
        segments = [{"id": "spoken", "episode_id": "ep", "start_ms": 490000,
                     "text": "Chaozus Kopf ist härter als Diamant."}]
        self.assertEqual(native_audio_repetition_findings(source, source, segments, "spoken", ["Chao Zu"]), [])

    def test_picture_evidence_is_not_audible_but_nearby_dialogue_still_is(self):
        text = "Chaozu schwebt vor Krillin mit angewinkelten gelben Hosenbeinen über grauen Steinplatten."
        picture = {"id": "visual", "episode_id": "ep", "start_ms": 700000,
                   "text": text, "visual_scene_anchor": True}
        self.assertEqual(native_audio_repetition_findings(text, text, [picture], "visual"), [])
        dialogue = {**picture, "id": "spoken", "visual_scene_anchor": False}
        findings = native_audio_repetition_findings(text, text, [picture, dialogue], "visual")
        self.assertEqual([f["segment_id"] for f in findings], ["spoken"])


class CoverageGapSpeechDensityTest(unittest.TestCase):
    """A conversation needs no narrator; silence does."""

    MESSAGE = (
        "Folge 56820 lässt den Hörer zu lange ohne Kommentar: "
        "454–866 s (411 s ohne Kommentar, nimm davon c064, c065)"
    )

    def _segments(self, spoken_ms: int) -> list[dict[str, int]]:
        return [{"start_ms": 454_000, "end_ms": 454_000 + spoken_ms}]

    def test_measures_spoken_share_of_reported_stretch(self):
        ratio = coverage_gap_speech_ratio(self.MESSAGE, self._segments(206_000))

        self.assertAlmostEqual(ratio, 0.5, places=2)

    def test_dialogue_dense_overrun_is_deliverable(self):
        self.assertFalse(_oversized_coverage_gap(self.MESSAGE, speech_ratio=0.47))

    def test_quiet_overrun_still_fails_loudly(self):
        self.assertTrue(_oversized_coverage_gap(self.MESSAGE, speech_ratio=0.05))

    def test_without_measurement_the_tight_bound_applies(self):
        self.assertTrue(_oversized_coverage_gap(self.MESSAGE))

    def test_speech_ratio_is_none_when_no_stretch_reported(self):
        self.assertIsNone(coverage_gap_speech_ratio("keine Lücke", self._segments(1_000)))


if __name__ == "__main__":
    unittest.main()


class QualityRepairContractTests(unittest.TestCase):
    def test_explicit_repair_does_not_accept_an_exhausted_coverage_gap(self):
        error = runner.AlignmentValidationError(
            "Folge ep laesst den Hoerer zu lange ohne Kommentar: 100-310 s",
            {"alignments": [{"cue_id": "c001"}], "character_introductions": [], "notes": []},
            set(), {"c002"},
        )
        def failing_runner(payload, execution):
            raise error
        with patch.object(runner, "MAX_INVALID_OUTPUT_ATTEMPTS", 2), \
             patch.object(runner, "repair_researched_character_introductions", return_value=None), \
             patch.object(runner, "salvage_optional_audio_drama_result", return_value=None):
            with self.assertRaisesRegex(ValueError, "Quality repair could not satisfy"):
                runner.execute_with_fallbacks(
                    {"narration_density": "audio_drama", "_repair_cue_ids": ["c002"]},
                    [{"provider": "codex", "model": "m", "reasoning_effort": "high", "timeout_seconds": 60}],
                    failing_runner, "scene-alignment", "disabled",
                )


class CoverageRepairAnchorHintTests(unittest.TestCase):
    def test_displaced_cue_gets_source_anchor_without_unlocking_other_cues(self):
        payload = {"_engine_locked_cue_ids": ["locked"], "cues": [
            {"id": "repair", "proposed_episode_id": "ep", "proposed_anchor_ms": 372747},
            {"id": "locked", "proposed_episode_id": "ep", "proposed_anchor_ms": 100000}],
            "segments": [{"id": "source", "episode_id": "ep", "start_ms": 372747},
                         {"id": "wrong", "episode_id": "ep", "start_ms": 396000},
                         {"id": "other", "episode_id": "other", "start_ms": 372747}]}
        answer = {"alignments": [{"cue_id": "repair", "anchor_segment_id": "wrong"}]}
        self.assertEqual(runner.coverage_repair_anchor_hints(payload, answer, {"repair", "locked"}),
                         ["repair -> source@372747 ms"])
        answer["alignments"].append({"cue_id": "locked", "anchor_segment_id": "source"})
        self.assertEqual(runner.coverage_repair_anchor_hints(payload, answer, {"repair"}),
                         ["repair -> wrong@396000 ms"])

    def test_hint_closes_gap_instead_of_only_approaching_source(self):
        payload = {"cues": [{"id": "repair", "proposed_episode_id": "ep", "proposed_anchor_ms": 595386}],
                   "segments": [{"id": "source", "episode_id": "ep", "start_ms": 595386},
                                {"id": "closes", "episode_id": "ep", "start_ms": 560000}]}
        self.assertEqual(runner.coverage_repair_anchor_hints(
            payload, {"alignments": []}, {"repair"}, {"repair": [(413000, 598000, 150000)]}),
            ["repair -> closes@560000 ms"])

    def test_gap_feedback_keeps_subsecond_overrun_visible(self):
        with self.assertRaises(runner.CoverageGapError) as caught:
            runner.validate_alignment_density(
                "audio_drama", {"a", "b", "c", "d", "missing"}, ["a", "b", "c", "d"],
                {"ep": 5}, {"ep": 4}, {"ep": {"min_cues": 4, "max_gap_ms": 150000}},
                {"ep": [0, 150001, 300000, 450000]}, {"ep": [0, 100000, 150001, 300000, 450000]},
                {"ep": [(100000, "missing")]},
            )
        self.assertIn("150001 ms > 150000 ms", str(caught.exception))


class UnverifiedCharacterResearchTests(unittest.TestCase):
    def test_research_rejects_explicitly_unverified_traits_before_alignment(self):
        character = {'name': 'Dracula', 'role': 'Kämpfer', 'introduction_required': True,
                     'visual_description': 'Fledermaus; weitere sichtbare Einzelmerkmale konnten nicht verifiziert werden.'}
        answer = {'episode_contexts': [{'episode_id': 'ep',
                  'sources': [{'url': 'https://example.com/episode'}], 'character_introductions': [character]}]}
        with self.assertRaisesRegex(ValueError, 'verifiziert'):
            runner.validate_episode_context_result({'episodes': [{'id': 'ep'}]}, answer)
        character['visual_description'] = 'Unsichtbar; weder Körper noch Kleidung sichtbar.'
        self.assertIs(runner.validate_episode_context_result({'episodes': [{'id': 'ep'}]}, answer), answer)

    def test_timing_words_do_not_count_as_visible_character_traits(self):
        self.assertEqual(runner.visual_detail_match_count(
            'Beim ersten Auftreten zunächst als Fledermaus; weitere sichtbare Einzelmerkmale konnten nicht verifiziert werden.',
            'Dracula, ein Vampir, der zunächst als dunkle Fledermaus heranflattert.', 'Dracula'), 1)


class AnchorOnlyRepairTests(unittest.TestCase):
    def test_anchor_repair_preserves_narration_and_purpose(self):
        previous = {'cue_id': 'c001', 'narration_text': 'Belegter kurzer Text.',
                    'purpose': 'character_introduction', 'anchor_segment_id': 'old',
                    'placement': 'before_anchor', 'target_duration_ms': 5000}
        answer = {'alignments': [{**previous, 'narration_text': 'Unnötig völlig neu geschrieben.',
                  'purpose': 'visual_action', 'anchor_segment_id': 'new', 'placement': 'after_anchor'}]}
        runner.restore_anchor_only_alignment_content({'_anchor_only_alignments': [previous]}, answer)
        self.assertEqual(answer['alignments'][0]['narration_text'], previous['narration_text'])
        self.assertEqual(answer['alignments'][0]['purpose'], previous['purpose'])
        self.assertEqual(answer['alignments'][0]['anchor_segment_id'], 'new')
        self.assertEqual(answer['alignments'][0]['placement'], 'after_anchor')

    def test_text_is_released_again_when_content_validation_fails(self):
        partial = {'alignments': [{'cue_id': 'c001', 'narration_text': 'Text'}], 'character_introductions': []}
        seen = []
        def fake(request, execution):
            seen.append(request)
            if len(seen) == 1:
                raise runner.AlignmentValidationError('Codex-Skript muss chronologisch ausgerichtet sein', partial, {'c001'})
            if len(seen) == 2:
                raise runner.AlignmentValidationError('Cue c001 verletzt den Hörspielvertrag: Text zu lang', partial, {'c001'})
            return partial
        with patch.object(runner, 'repair_researched_character_introductions', return_value=None), \
             patch.object(runner, 'salvage_optional_audio_drama_result', return_value=None):
            runner.execute_with_fallbacks({'_repair_cue_ids': ['c001']},
                [{'provider': 'codex', 'model': 'm', 'reasoning_effort': 'high', 'timeout_seconds': 60}],
                fake, 'scene-alignment', 'disabled')
        self.assertEqual(seen[1]['_anchor_only_alignments'], partial['alignments'])
        self.assertFalse(seen[2].get('_anchor_only_alignments'))


class ActualTimelineGapRepairTests(unittest.TestCase):
    def test_missing_and_displaced_gap_candidate_remains_required(self):
        payload = {'cues': [{'id': 'c', 'proposed_episode_id': 'ep',
                             'coverage_repair_window': {'start_ms': 100, 'end_ms': 200}}],
                   'segments': [{'id': 'wrong', 'episode_id': 'ep', 'start_ms': 500},
                                {'id': 'right', 'episode_id': 'ep', 'start_ms': 150}]}
        for answer in [{'alignments': []}, {'alignments': [{'cue_id': 'c', 'anchor_segment_id': 'wrong'}]}]:
            with self.assertRaises(runner.AlignmentValidationError) as caught:
                runner.validate_coverage_repair_windows(payload, answer)
            self.assertEqual(caught.exception.required_cue_ids, {'c'})
        runner.validate_coverage_repair_windows(payload, {'alignments': [{'cue_id': 'c', 'anchor_segment_id': 'right'}]})

    def test_verified_picture_anchor_is_not_treated_as_spoken_audio(self):
        self.assertEqual(runner.native_context_for_alignment([
            {'id': 'visual', 'episode_id': 'ep', 'start_ms': 100, 'text': 'Bildbeleg: Panzer', 'visual_scene_anchor': True},
            {'id': 'spoken', 'episode_id': 'ep', 'start_ms': 200, 'text': 'Komm her!'}], 'visual'), 'Komm her!')

    def test_retry_shows_accepted_text_and_chronological_order(self):
        payload = {'cues': [{'id': 'c900', 'order': 2}, {'id': 'c001', 'order': 3}],
                   'segments': [{'id': 's', 'start_ms': 300}], '_repair_cue_ids': ['c900'],
                   '_locked_alignments': [{'cue_id': 'c001', 'anchor_segment_id': 's', 'narration_text': 'Die Wand bricht auf.', 'purpose': 'visual_action'}]}
        model = runner.scene_alignment_model_payload(payload)
        beat = model['repair_scope']['placed_beats'][0]
        self.assertEqual(beat['narration_text'], 'Die Wand bricht auf.')
        self.assertEqual(beat['order'], 3)
        self.assertNotIn('Cue-Nummer', model['repair_scope']['placement_rule'])

class MinimumCueRepairScopeTests(unittest.TestCase):
    def test_count_failure_reopens_missing_candidates_after_narrow_repair(self):
        cues = [{"id": f"c{i}", "order": i, "text": f"Passage {i}", "source_context": f"Passage {i}", "proposed_episode_id": "ep", "proposed_anchor_ms": i*60000, "book_edge": "none"} for i in range(1, 6)]
        segments = [{"id": f"s{i}", "episode_id": "ep", "episode_order": 0, "start_ms": i*60000, "end_ms": i*60000+500, "text": "Dialog"} for i in range(1, 6)]
        rows = [{"cue_id": f"c{i}", "anchor_segment_id": f"s{i}", "narration_text": f"Passage {i}", "purpose": "offscreen_context", "introduced_characters": [], "information_gain": "Nicht hörbarer Kontext", "source_detail_coverage": 1.0, "native_audio_relation": "complements_existing_audio", "retained_visual_details": [], "beat_type": "dialogue_bridge", "audio_strategy": "pause_at_scene_boundary", "target_duration_ms": 1800, "placement": "before_anchor", "confidence": .95, "reasoning": "Belegte Szene"} for i in range(1, 6)]
        payload = {"series": {}, "preferred_names": [], "narration_density": "balanced", "episode_contexts": [{"episode_id": "ep", "character_introductions": []}], "cues": cues, "segments": segments, "coverage_targets": {"ep": {"min_cues": 4, "max_cues": 4}}, "_locked_alignments": rows[:2], "_repair_cue_ids": ["c3"]}
        execution = {"provider": "codex", "model": "m", "reasoning_effort": "high", "timeout_seconds": 60}
        seen = []
        original_model_payload = runner.scene_alignment_model_payload
        def capture_payload(value):
            result = original_model_payload(value)
            seen.append(result)
            return result
        def structured(*args, **kwargs):
            selected = rows[2:3] if len(seen) == 1 else rows[3:4]
            return {"alignments": json.loads(json.dumps(selected)), "character_introductions": [], "name_aliases": [], "notes": []}
        with patch.object(runner, "MAX_INVALID_OUTPUT_ATTEMPTS", 2), patch.object(runner, "provider_auth_status", return_value={"authenticated": True}), patch.object(runner, "run_structured_agent", side_effect=structured), patch.object(runner, "scene_alignment_model_payload", side_effect=capture_payload):
            result = runner.execute_with_fallbacks(payload, [execution], runner.research_scene_alignment, "scene-alignment", "disabled")
        self.assertEqual(set(seen[1]["repair_scope"]["cue_ids"]), {"c4", "c5"})
        self.assertEqual({a["cue_id"] for a in result[0]["alignments"]}, {"c1", "c2", "c3", "c4"})

class CoverageBoundaryInputTests(unittest.TestCase):
    def test_gap_context_cannot_be_an_additional_book_edge(self):
        with self.assertRaisesRegex(ValueError, 'Coverage repair candidate'):
            runner.validated_alignment_request({
                'episode_contexts': [{'episode_id': 'ep', 'synopsis': 'Eine Szene', 'character_introductions': []}],
                'cues': [{'id': 'gap', 'text': 'Belegter Kontext', 'book_edge': 'start',
                          'coverage_repair_window': {'start_ms': 1000, 'end_ms': 2000}}],
                'segments': [{'id': 's', 'episode_id': 'ep', 'text': 'Dialog', 'start_ms': 1000}],
            })

class CanonicalIntroductionRetryTests(unittest.TestCase):
    def test_missing_mapping_reopens_canonical_raw_text_instead_of_first_other_cue(self):
        cues = [{'id': f'c{i}', 'order': i, 'text': 'Yamchu wartet.' if i == 2 else f'Passage {i}',
                 'source_context': 'Yamcha wartet.' if i == 2 else f'Passage {i}',
                 'proposed_episode_id': 'ep', 'proposed_anchor_ms': i*60000, 'book_edge': 'none'} for i in range(1, 5)]
        segments = [{'id': f's{i}', 'episode_id': 'ep', 'episode_order': 0, 'start_ms': i*60000, 'end_ms': i*60000+500, 'text': 'Dialog'} for i in range(1, 5)]
        rows = [{'cue_id': f'c{i}', 'anchor_segment_id': f's{i}', 'narration_text': 'Eine ruhige Szene.', 'purpose': 'offscreen_context', 'introduced_characters': [], 'information_gain': 'Nicht hörbarer Kontext', 'source_detail_coverage': 1.0, 'native_audio_relation': 'complements_existing_audio', 'retained_visual_details': [], 'beat_type': 'dialogue_bridge', 'audio_strategy': 'pause_at_scene_boundary', 'target_duration_ms': 1800, 'placement': 'before_anchor', 'confidence': .95, 'reasoning': 'Belegte Szene'} for i in range(1, 5)]
        payload = {'series': {}, 'preferred_names': ['Yamchu'], 'narration_density': 'balanced', 'episode_contexts': [{'episode_id': 'ep', 'character_introductions': [{'name': 'Yamchu', 'introduction_required': True, 'visual_description': 'Grüner Anzug, orangefarbenes Halstuch.'}]}], 'cues': cues, 'segments': segments}
        answer = {'alignments': rows, 'character_introductions': [], 'name_aliases': [], 'notes': []}
        with patch.object(runner, 'provider_auth_status', return_value={'authenticated': True}), patch.object(runner, 'run_structured_agent', return_value=answer), self.assertRaises(runner.AlignmentValidationError) as caught:
            runner.research_scene_alignment(payload, {'provider': 'codex', 'model': 'm', 'reasoning_effort': 'high', 'timeout_seconds': 60})
        self.assertEqual(caught.exception.invalid_cue_ids, {'c2'})

    def test_missing_unnamed_carrier_reopens_episode_except_engine_locks(self):
        cues = [{'id': f'c{i}', 'order': i, 'text': f'Passage {i}',
                 'source_context': f'Passage {i}',
                 'proposed_episode_id': 'ep', 'proposed_anchor_ms': i*60000, 'book_edge': 'none'} for i in range(1, 5)]
        segments = [{'id': f's{i}', 'episode_id': 'ep', 'episode_order': 0, 'start_ms': i*60000, 'end_ms': i*60000+500, 'text': 'Dialog'} for i in range(1, 5)]
        rows = [{'cue_id': f'c{i}', 'anchor_segment_id': f's{i}', 'narration_text': 'Eine ruhige Szene.', 'purpose': 'offscreen_context', 'introduced_characters': [], 'information_gain': 'Nicht hörbarer Kontext', 'source_detail_coverage': 1.0, 'native_audio_relation': 'complements_existing_audio', 'retained_visual_details': [], 'beat_type': 'dialogue_bridge', 'audio_strategy': 'pause_at_scene_boundary', 'target_duration_ms': 1800, 'placement': 'before_anchor', 'confidence': .95, 'reasoning': 'Belegte Szene'} for i in range(1, 5)]
        payload = {'series': {}, 'preferred_names': ['Yamchu'], 'narration_density': 'balanced', 'episode_contexts': [{'episode_id': 'ep', 'character_introductions': [{'name': 'Yamchu', 'introduction_required': True, 'visual_description': 'Grüner Anzug, orangefarbenes Halstuch.'}]}], 'cues': cues, 'segments': segments}
        payload['_engine_locked_cue_ids'] = ['c1']
        answer = {'alignments': rows, 'character_introductions': [], 'name_aliases': [], 'notes': []}
        with patch.object(runner, 'provider_auth_status', return_value={'authenticated': True}), patch.object(runner, 'run_structured_agent', return_value=answer), self.assertRaises(runner.AlignmentValidationError) as caught:
            runner.research_scene_alignment(payload, {'provider': 'codex', 'model': 'm', 'reasoning_effort': 'high', 'timeout_seconds': 60})
        self.assertEqual(caught.exception.invalid_cue_ids, {'c2', 'c3', 'c4'})
