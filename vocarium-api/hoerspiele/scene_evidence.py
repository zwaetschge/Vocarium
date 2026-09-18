"""Reviewed original-picture evidence for scenes absent from the novel."""
from __future__ import annotations

import hashlib
from typing import Any


def source_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cue_source_context(project: dict[str, Any], cue: dict[str, Any]) -> str:
    fragments = {str(f["id"]): str(f.get("text") or "")
                 for chapter in project.get("chapters", []) for f in chapter.get("fragments", [])}
    return str(cue.get("editorial_source_context") or "") or "\n".join(
        fragments.get(str(fid), "") for fid in cue.get("source_fragment_ids", []))


def reviewed_anchor_evidence(segment: dict[str, Any]) -> dict[str, Any]:
    """Identity of the reviewed subtitle, independent of generated row IDs."""
    return {
        "episode_start_ms": int(segment.get("episode_start_ms") or 0),
        "episode_end_ms": int(segment.get("episode_end_ms") or 0),
        "text_digest": source_digest(str(segment.get("reconciled_text") or segment.get("text") or "")),
    }


def _resolve_reviewed_anchor(segments: dict[str, dict[str, Any]], override: dict[str, Any]) -> dict[str, Any]:
    episode_id = str(override["episode_id"])
    evidence = override.get("anchor_evidence")
    anchor = segments.get(str(override["anchor_segment_id"]))
    if anchor and str(anchor.get("episode_id")) == episode_id:
        if evidence is None or reviewed_anchor_evidence(anchor) == evidence:
            return anchor
    if isinstance(evidence, dict):
        matches = [segment for segment in segments.values()
                   if str(segment.get("episode_id")) == episode_id
                   and reviewed_anchor_evidence(segment) == evidence]
        if len(matches) == 1:
            return matches[0]
    raise ValueError("Reviewed narration anchor no longer exists in its episode or its evidence changed")


def apply_reviewed_scene_evidence(project: dict[str, Any], cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve novel text and alias order; append separately evidenced picture cues."""
    segments = {str(s["id"]): s for s in project.get("reconciled_transcript", [])}
    overrides = {(str(o["episode_id"]), str(o["source_digest"])): o
                 for o in project.get("reviewed_narration_anchors", [])}
    result = [dict(c) for c in cues]
    for cue in result:
        override = overrides.get((str(cue.get("anchor_episode_id")), source_digest(cue_source_context(project, cue))))
        if not override:
            continue
        anchor = _resolve_reviewed_anchor(segments, override)
        cue.update(anchor_segment_id=anchor["id"], anchor_episode_start_ms=int(anchor["episode_start_ms"]),
                   anchor_insert_ms=int(anchor["episode_start_ms"]),
                   anchor_global_start_sample=int(anchor.get("start_sample") or 0),
                   anchor_text=str(anchor.get("reconciled_text") or anchor.get("text") or ""))
    existing = {str(c["id"]) for c in result}
    for anchor in project.get("reviewed_visual_scene_anchors", []):
        if not anchor.get("narration_candidate") or not anchor.get("visual_evidence"):
            continue
        cue_id = "cue_visual_" + source_digest(str(anchor["id"]))[:20]
        if cue_id in existing:
            continue
        evidence = str(anchor["visual_evidence"])
        position = int(anchor["episode_start_ms"])
        result.append({
            "id": cue_id, "chapter_id": "", "chapter_title": "Original picture evidence",
            "text": evidence, "editorial_source_context": evidence, "source_fragment_ids": [],
            "visual_evidence": evidence, "visual_evidence_frames": list(anchor.get("evidence_frames") or []),
            "anchor_episode_id": str(anchor["episode_id"]), "anchor_segment_id": str(anchor["id"]),
            "anchor_episode_start_ms": position, "anchor_insert_ms": position,
            "anchor_global_start_sample": int(anchor.get("start_sample") or 0),
            "anchor_text": evidence, "anchor_match_score": 1.0, "confidence": 0.95,
            "placement_policy": "after_aligned_speech_gap", "estimated_duration_ms": 10000, "revision": 1,
            "coverage_repair_window": {"start_ms": position - 1000, "end_ms": position + 1000},
        })
        existing.add(cue_id)
    return result


def grounded_picture_cue_ids(project: dict[str, Any]) -> set[str]:
    """Accept the stored reviewed source, not an arbitrary picture flag on a cue."""
    markers = {str(s["id"]): s for s in project.get("reviewed_visual_scene_anchors", [])
               if s.get("visual_scene_anchor") and s.get("narration_candidate")
               and s.get("visual_evidence") and s.get("evidence_frames")}
    segments = {str(s["id"]): s for s in project.get("reconciled_transcript", [])}
    cue_markers = {"cue_visual_" + source_digest(marker_id)[:20]: marker
                   for marker_id, marker in markers.items()}
    grounded = set()
    for cue in project.get("cues", []):
        anchor_id = str(cue.get("anchor_segment_id") or "")
        marker = cue_markers.get(str(cue.get("id"))) or markers.get(anchor_id)
        segment = segments.get(str(marker["id"])) if marker else None
        timing_segment = segments.get(anchor_id)
        if not marker or not segment or not segment.get("visual_scene_anchor"):
            continue
        if not timing_segment:
            continue
        if (str(cue.get("anchor_episode_id")) == str(marker.get("episode_id")) == str(segment.get("episode_id"))
                == str(timing_segment.get("episode_id"))
                and int(marker.get("episode_start_ms") or 0) == int(segment.get("episode_start_ms") or 0)
                and int(cue.get("anchor_episode_start_ms") or 0) == int(timing_segment.get("episode_start_ms") or 0)
                and abs(int(cue.get("anchor_episode_start_ms") or 0) - int(marker.get("episode_start_ms") or 0)) <= 1000
                and cue.get("editorial_source_context") == marker["visual_evidence"] == segment.get("visual_evidence")
                and cue.get("visual_evidence_frames") == marker["evidence_frames"]):
            grounded.add(str(cue["id"]))
    return grounded
