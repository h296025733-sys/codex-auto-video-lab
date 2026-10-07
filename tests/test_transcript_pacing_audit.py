from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDITOR = (
    ROOT
    / ".codex"
    / "skills"
    / "tiktok-fine-cut-director"
    / "scripts"
    / "audit_transcript_pacing.py"
)


def run_audit(
    tmp_path: Path,
    payload: object,
    *,
    output: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    command = [sys.executable, str(AUDITOR), str(transcript)]
    if output is not None:
        command.extend(["--output", str(output)])
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_cjk_units_wall_active_time_and_gap_thresholds(tmp_path: Path) -> None:
    result = run_audit(
        tmp_path,
        {
            "language": "zh",
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "你好 AI 2026"},
                {"start": 1.3, "end": 2.3, "text": "测试 TikTok"},
            ],
        },
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    metrics = report["metrics"]
    assert metrics["segment_count"] == 2
    assert metrics["spoken_span"] == {
        "definition": "earliest segment start through latest segment end",
        "duration_seconds": 2.3,
        "end_seconds": 2.3,
        "start_seconds": 0.0,
    }
    assert metrics["merged_active_seconds"] == 2.0
    assert metrics["speech_occupancy"] == 0.869565
    assert metrics["language_units"] == {
        "count": 7,
        "method": "cjk_characters_plus_latin_digit_tokens",
        "units_per_minute_active": 210.0,
        "units_per_minute_wall": 182.608696,
    }
    assert metrics["phrase_duration_seconds"]["median"] == 1.0
    assert metrics["phrase_duration_seconds"]["p90"] == 1.0
    assert metrics["gaps"]["threshold_counts"] == {
        "0.25": 1,
        "0.4": 0,
        "0.6": 0,
        "0.8": 0,
    }
    assert metrics["gaps"]["longest_seconds"] == 0.3


def test_latin_tokens_and_overlapping_segments_are_not_double_timed(tmp_path: Path) -> None:
    result = run_audit(
        tmp_path,
        {
            "language": "es-US",
            "segments": [
                {"start": 0.0, "end": 2.0, "text": "Hola, mundo 2026"},
                {"start": 1.5, "end": 3.0, "text": "Fine-cut isn't static"},
            ],
        },
    )

    assert result.returncode == 0, result.stderr
    metrics = json.loads(result.stdout)["metrics"]
    assert metrics["merged_active_seconds"] == 3.0
    assert metrics["speech_occupancy"] == 1.0
    assert metrics["language_units"] == {
        "count": 6,
        "method": "latin_word_like_tokens",
        "units_per_minute_active": 120.0,
        "units_per_minute_wall": 120.0,
    }
    assert metrics["phrase_duration_seconds"]["median"] == 1.75
    assert metrics["phrase_duration_seconds"]["p90"] == 1.95
    assert metrics["gaps"]["longest_seconds"] is None


def test_gap_counts_include_exact_threshold_boundaries(tmp_path: Path) -> None:
    result = run_audit(
        tmp_path,
        {
            "language": "en",
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "one"},
                {"start": 1.25, "end": 2.0, "text": "two"},
                {"start": 2.65, "end": 3.0, "text": "three"},
                {"start": 3.8, "end": 4.0, "text": "four"},
            ],
        },
    )

    assert result.returncode == 0, result.stderr
    gaps = json.loads(result.stdout)["metrics"]["gaps"]
    assert gaps["threshold_counts"] == {
        "0.25": 3,
        "0.4": 2,
        "0.6": 2,
        "0.8": 1,
    }
    assert gaps["longest_seconds"] == 0.8


def test_empty_transcript_is_reported_without_division_claims(tmp_path: Path) -> None:
    result = run_audit(tmp_path, {"language": "en", "segments": []})

    assert result.returncode == 0, result.stderr
    metrics = json.loads(result.stdout)["metrics"]
    assert metrics["segment_count"] == 0
    assert metrics["spoken_span"]["duration_seconds"] is None
    assert metrics["merged_active_seconds"] == 0
    assert metrics["speech_occupancy"] is None
    assert metrics["language_units"]["count"] == 0
    assert metrics["language_units"]["units_per_minute_wall"] is None
    assert metrics["language_units"]["units_per_minute_active"] is None
    assert metrics["phrase_duration_seconds"]["median"] is None
    assert metrics["phrase_duration_seconds"]["p90"] is None
    assert metrics["gaps"]["longest_seconds"] is None


def test_invalid_segment_returns_structured_error(tmp_path: Path) -> None:
    result = run_audit(
        tmp_path,
        {
            "language": "en",
            "segments": [{"start": 2.0, "end": 1.0, "text": "invalid"}],
        },
    )

    assert result.returncode == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["status"] == "error"
    assert "end must be greater than start" in error["error"]


def test_output_file_matches_stdout_and_input_cannot_be_overwritten(tmp_path: Path) -> None:
    destination = tmp_path / "reports" / "pacing.json"
    result = run_audit(
        tmp_path,
        {
            "language": "en",
            "segments": [{"start": 0.0, "end": 1.0, "text": "one phrase"}],
        },
        output=destination,
    )

    assert result.returncode == 0, result.stderr
    assert destination.read_text(encoding="utf-8") == result.stdout

    transcript = tmp_path / "same.json"
    transcript.write_text(
        json.dumps(
            {
                "language": "en",
                "segments": [{"start": 0.0, "end": 1.0, "text": "keep me"}],
            }
        ),
        encoding="utf-8",
    )
    before = transcript.read_bytes()
    overwrite = subprocess.run(
        [sys.executable, str(AUDITOR), str(transcript), "--output", str(transcript)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert overwrite.returncode == 2
    assert transcript.read_bytes() == before
