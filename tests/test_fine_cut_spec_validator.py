from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = (
    ROOT
    / ".codex"
    / "skills"
    / "tiktok-fine-cut-director"
    / "scripts"
    / "validate_fine_cut_spec.py"
)


def valid_spec() -> dict[str, object]:
    return {
        "schema_version": "1.1",
        "job_id": "fan-ad-en-us",
        "audience": {
            "locale": "en-US",
            "market": "TikTok US",
            "viewer_need": "portable relief from summer heat",
        },
        "regime": "product-proof",
        "route": "auto-video-editor",
        "creative_thesis": "Show the cooling proof before explaining the product.",
        "target_duration_seconds": 15,
        "claims": [
            {
                "claim_id": "portable",
                "text": "The supplied product can be carried by hand.",
                "status": "verified",
                "evidence": ["Visible in source-001."],
            },
            {
                "claim_id": "airflow",
                "text": "The operating fan visibly moves a tissue.",
                "status": "verified",
                "evidence": ["Visible in source-001 between 2.5s and 8.0s."],
            },
        ],
        "retention_events": [
            {
                "start": 0,
                "end": 2.5,
                "spoken_idea": "This fits in your bag.",
                "visual_job": "hook",
                "confidence": "observed",
                "evidence_source": "source-001",
                "source_asset_id": "source-001",
                "source_in": 0,
                "source_out": 2.5,
                "claim_ids": ["portable"],
                "shot_or_overlay": "Product is readable in the first frame.",
                "text": "PORTABLE",
                "text_treatment": "Anton hook preset with one pop.",
                "motion": "hard cut on hand movement",
                "exit_condition": "product is identifiable",
            },
            {
                "start": 2.5,
                "end": 8.0,
                "spoken_idea": "Watch the airflow move the tissue.",
                "visual_job": "proof",
                "confidence": "observed",
                "evidence_source": "source-001",
                "source_asset_id": "source-001",
                "source_in": 2.5,
                "source_out": 8.0,
                "claim_ids": ["airflow"],
                "shot_or_overlay": "Hold the tissue proof until the result is readable.",
                "text": "WATCH THE AIRFLOW",
                "text_treatment": "Montserrat phrase caption; AIRFLOW is the sole accent.",
                "motion": "hold while proof changes internally",
                "exit_condition": "proof result is readable",
            },
        ],
        "typography": {
            "body_font": "Montserrat",
            "hook_font": "Anton",
            "accent_font": "DM Serif Display Italic",
            "caption_mode": "phrase",
            "safe_zone_policy": "keep text outside platform UI zones",
            "palette": ["#FFFFFF", "#151515", "#16C5FF"],
        },
        "evidence_inserts": [
            {
                "asset_id": "source-001",
                "kind": "source-frame",
                "semantic_trigger": "airflow proof claim",
                "enter_at": 2.5,
                "exit_at": 8.0,
                "provenance": "user-authorized source video",
                "illustrative": False,
                "claim_boundary": "This frame proves visible movement, not a cooling temperature.",
            }
        ],
        "audio": {
            "voiceover": "generated-en-US",
            "music_policy": "voiceover-only",
            "music_asset_id": None,
            "sfx_policy": "licensed local whooshes only",
            "mix_notes": "Voiceover remains intelligible; no music is used.",
        },
        "rights": [
            {
                "asset_id": "source-001",
                "source": "user upload",
                "license_or_authorization": "user supplied and authorized",
                "usage_scope": "Use in this local test render.",
                "reference_only": False,
                "release_status": "confirmed",
                "sha256": "a" * 64,
            }
        ],
        "verification": {
            "planned_checks": ["full decode", "sample-frame review"],
            "not_proven": ["conversion lift", "platform policy acceptance"],
        },
    }


def run_validator(tmp_path: Path, spec: dict[str, object]) -> subprocess.CompletedProcess[str]:
    spec_path = tmp_path / "fine-cut-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(spec_path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_valid_spec_returns_structured_success(tmp_path: Path) -> None:
    result = run_validator(tmp_path, valid_spec())

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["error_count"] == 0
    assert payload["errors"] == []
    assert payload["summary"] == {
        "evidence_insert_count": 1,
        "retention_event_count": 2,
        "rights_entry_count": 1,
    }


def test_invalid_spec_reports_each_core_error(tmp_path: Path) -> None:
    spec = valid_spec()
    spec["unexpected"] = True
    spec["audience"] = {"locale": "fr-FR", "market": "", "viewer_need": "hot weather"}
    spec["regime"] = "montage"
    spec["retention_events"] = [
        {
            "start": 9,
            "end": 8,
            "spoken_idea": "bad range",
            "visual_job": "hook",
            "confidence": "observed",
            "evidence_source": "source-001",
            "source_asset_id": "source-001",
            "source_in": 9,
            "source_out": 8,
            "claim_ids": ["portable"],
            "shot_or_overlay": "bad range",
            "text": "BAD",
            "text_treatment": "hook",
            "motion": "cut",
            "exit_condition": "done",
        },
        {
            "start": 3,
            "end": 16,
            "spoken_idea": "out of order and too long",
            "visual_job": "proof",
            "confidence": "observed",
            "evidence_source": "source-001",
            "source_asset_id": "source-001",
            "source_in": 3,
            "source_out": 16,
            "claim_ids": ["airflow"],
            "shot_or_overlay": "too long",
            "text": None,
            "text_treatment": "no text",
            "motion": "hold",
            "exit_condition": "done",
        },
    ]
    spec["evidence_inserts"] = [
        {
            "asset_id": "unlicensed-overlay",
            "kind": "source-frame",
            "semantic_trigger": "claim",
            "enter_at": 7,
            "exit_at": 7,
            "provenance": "unknown",
            "illustrative": True,
            "claim_boundary": "Does not prove a product result.",
        }
    ]
    spec["audio"] = {
        "voiceover": "generated-fr-FR",
        "music_policy": "guessed-trending",
        "music_asset_id": None,
        "sfx_policy": "none",
        "mix_notes": "unverified",
    }
    spec["rights"] = [
        {
            "asset_id": "source-001",
            "source": "user upload",
            "license_or_authorization": "authorized",
            "usage_scope": "test",
            "reference_only": False,
            "release_status": "confirmed",
            "sha256": "not-a-sha256",
        }
    ]
    spec["verification"] = {"planned_checks": "full decode", "not_proven": [""]}

    result = run_validator(tmp_path, spec)

    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["valid"] is False
    codes = {error["code"] for error in payload["errors"]}
    assert {
        "unknown_key",
        "enum",
        "empty",
        "time_range",
        "time_order",
        "duration",
        "sha256",
        "rights_missing",
        "type",
    } <= codes
    paths = {error["path"] for error in payload["errors"]}
    assert "$.retention_events[0]" in paths
    assert "$.retention_events[1].start" in paths
    assert "$.evidence_inserts[0]" in paths
    assert "$.rights[0].sha256" in paths


def test_empty_retention_and_rights_arrays_are_rejected(tmp_path: Path) -> None:
    spec = valid_spec()
    spec["retention_events"] = []
    spec["evidence_inserts"] = []
    spec["rights"] = []

    result = run_validator(tmp_path, spec)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    min_item_paths = {
        error["path"] for error in payload["errors"] if error["code"] == "min_items"
    }
    assert min_item_paths == {"$.retention_events", "$.rights"}


def test_music_and_reference_only_assets_are_rejected(tmp_path: Path) -> None:
    spec = valid_spec()
    spec["audio"] = {
        "voiceover": "generated-en-US",
        "music_policy": "verified-local",
        "music_asset_id": None,
        "sfx_policy": "none",
        "mix_notes": "No unlicensed audio should be used.",
    }
    spec["rights"][0]["reference_only"] = True
    spec["claims"][0]["status"] = "conditional"

    result = run_validator(tmp_path, spec)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    codes = {error["code"] for error in payload["errors"]}
    assert {"music_rights", "reference_only", "conditional_claim"} <= codes
