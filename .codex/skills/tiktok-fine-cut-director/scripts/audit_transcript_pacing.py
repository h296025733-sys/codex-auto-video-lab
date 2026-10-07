#!/usr/bin/env python3
"""Measure transcript pacing without judging creative or persuasive quality.

The input contract matches the useful subset of faster-whisper JSON: a root
``language`` string and a ``segments`` array containing ``start``, ``end``, and
``text``.  Word-level timing and other metadata are deliberately ignored.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import unicodedata
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
METHOD_VERSION = "2026-08-25.1"
GAP_THRESHOLDS = (0.25, 0.4, 0.6, 0.8)
CJK_LANGUAGE_CODES = {
    "zh",
    "cmn",
    "yue",
    "wuu",
    "nan",
    "hak",
    "ja",
    "ko",
    "chinese",
    "japanese",
    "korean",
}


class AuditInputError(ValueError):
    """Raised when the transcript cannot support a deterministic audit."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise AuditInputError(f"transcript does not exist: {path}") from exc
    except OSError as exc:
        raise AuditInputError(f"could not read transcript {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AuditInputError(
            f"transcript is not valid JSON at line {exc.lineno}, "
            f"column {exc.colno}: {exc.msg}"
        ) from exc


def _finite_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AuditInputError(f"{path} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise AuditInputError(f"{path} must be a finite number")
    return number


def _validated_transcript(payload: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise AuditInputError("transcript root must be an object")

    language = payload.get("language")
    if not isinstance(language, str) or not language.strip():
        raise AuditInputError("$.language must be a non-empty string")

    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        raise AuditInputError("$.segments must be an array")

    segments: list[dict[str, Any]] = []
    previous_start: float | None = None
    for index, raw in enumerate(raw_segments):
        path = f"$.segments[{index}]"
        if not isinstance(raw, dict):
            raise AuditInputError(f"{path} must be an object")
        for key in ("start", "end", "text"):
            if key not in raw:
                raise AuditInputError(f"{path}.{key} is required")

        start = _finite_number(raw["start"], f"{path}.start")
        end = _finite_number(raw["end"], f"{path}.end")
        text = raw["text"]
        if start < 0:
            raise AuditInputError(f"{path}.start must be at least 0")
        if end <= start:
            raise AuditInputError(f"{path}.end must be greater than start")
        if not isinstance(text, str) or not text.strip():
            raise AuditInputError(f"{path}.text must be a non-empty string")
        if previous_start is not None and start < previous_start:
            raise AuditInputError(
                f"{path}.start must not be earlier than the preceding segment start"
            )
        previous_start = start
        segments.append({"start": start, "end": end, "text": text})

    return language.strip(), segments


def _is_cjk_character(character: str) -> bool:
    value = ord(character)
    return any(
        lower <= value <= upper
        for lower, upper in (
            (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
            (0x4E00, 0x9FFF),  # CJK Unified Ideographs
            (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
            (0x20000, 0x2EBEF),  # Supplementary CJK ideograph extensions
            (0x3040, 0x309F),  # Hiragana
            (0x30A0, 0x30FF),  # Katakana
            (0x31F0, 0x31FF),  # Katakana Phonetic Extensions
            (0xFF66, 0xFF9D),  # Halfwidth Katakana
            (0x1100, 0x11FF),  # Hangul Jamo
            (0x3130, 0x318F),  # Hangul Compatibility Jamo
            (0xA960, 0xA97F),  # Hangul Jamo Extended-A
            (0xAC00, 0xD7AF),  # Hangul Syllables
            (0xD7B0, 0xD7FF),  # Hangul Jamo Extended-B
        )
    )


def _is_latin_letter(character: str) -> bool:
    return unicodedata.category(character).startswith("L") and "LATIN" in unicodedata.name(
        character, ""
    )


def _is_decimal_digit(character: str) -> bool:
    return unicodedata.category(character) == "Nd"


def _count_latin_digit_tokens(text: str) -> int:
    """Count Latin/digit word-like runs, retaining internal apostrophes/hyphens."""

    normalized = unicodedata.normalize("NFKC", text)
    count = 0
    index = 0
    while index < len(normalized):
        if not (_is_latin_letter(normalized[index]) or _is_decimal_digit(normalized[index])):
            index += 1
            continue

        count += 1
        index += 1
        while index < len(normalized):
            character = normalized[index]
            if _is_latin_letter(character) or _is_decimal_digit(character):
                index += 1
                continue
            has_next = index + 1 < len(normalized)
            if character in {"'", "’", "-"} and has_next and (
                _is_latin_letter(normalized[index + 1])
                or _is_decimal_digit(normalized[index + 1])
            ):
                index += 1
                continue
            if (
                character in {".", ","}
                and has_next
                and _is_decimal_digit(normalized[index - 1])
                and _is_decimal_digit(normalized[index + 1])
            ):
                index += 1
                continue
            break
    return count


def _language_family(language: str) -> str:
    normalized = language.strip().lower().replace("_", "-")
    primary = normalized.split("-", 1)[0]
    return "cjk" if normalized in CJK_LANGUAGE_CODES or primary in CJK_LANGUAGE_CODES else "latin"


def _count_units(text: str, language: str) -> tuple[str, int]:
    family = _language_family(language)
    latin_digit_tokens = _count_latin_digit_tokens(text)
    if family == "cjk":
        normalized = unicodedata.normalize("NFKC", text)
        cjk_characters = sum(1 for character in normalized if _is_cjk_character(character))
        return "cjk_characters_plus_latin_digit_tokens", cjk_characters + latin_digit_tokens
    return "latin_word_like_tokens", latin_digit_tokens


def _merge_intervals(segments: list[dict[str, Any]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for segment in segments:
        start = float(segment["start"])
        end = float(segment["end"])
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _percentile_linear(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    weight = position - lower_index
    return ordered[lower_index] * (1.0 - weight) + ordered[upper_index] * weight


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def audit_transcript(payload: Any, *, source_path: Path | None = None) -> dict[str, Any]:
    """Validate and audit one transcript payload."""

    language, segments = _validated_transcript(payload)
    merged = _merge_intervals(segments)
    active_seconds = sum(end - start for start, end in merged)
    spoken_start = merged[0][0] if merged else None
    spoken_end = merged[-1][1] if merged else None
    spoken_span = (
        None if spoken_start is None or spoken_end is None else spoken_end - spoken_start
    )
    gaps = [merged[index][0] - merged[index - 1][1] for index in range(1, len(merged))]
    phrase_durations = [float(segment["end"]) - float(segment["start"]) for segment in segments]
    full_text = " ".join(str(segment["text"]) for segment in segments)
    unit_method, unit_count = _count_units(full_text, language)

    occupancy = None if not spoken_span else active_seconds / spoken_span
    units_per_minute_wall = None if not spoken_span else unit_count * 60.0 / spoken_span
    units_per_minute_active = None if not active_seconds else unit_count * 60.0 / active_seconds

    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "source": {
            "transcript_path": None if source_path is None else str(source_path.resolve()),
            "language": language,
        },
        "scope": {
            "basis": "validated transcript segment boundaries and segment text",
            "segment_boundaries_merged_for_active_time": True,
            "word_level_timing_used": False,
            "audio_signal_analyzed": False,
            "media_playback_speed_analyzed": False,
            "creative_or_persuasive_quality_judged": False,
        },
        "limitations": [
            (
                "Segment boundaries are ASR outputs, not verified breath, phoneme, "
                "or phrase boundaries."
            ),
            (
                "Language units are mechanical text counts, not syllables, semantic "
                "load, or listener effort."
            ),
            (
                "Unit rates are not directly comparable across language families or "
                "transcription conventions."
            ),
            "Internal gaps can reflect ASR segmentation behavior as well as audible pauses.",
            (
                "This audit does not establish comprehension, emotion, naturalness, "
                "persuasion, or a universally suitable speaking speed."
            ),
            (
                "This audit does not measure caption reading load, edit density, media "
                "duration, or playback-rate changes."
            ),
        ],
        "metrics": {
            "segment_count": len(segments),
            "spoken_span": {
                "start_seconds": _rounded(spoken_start),
                "end_seconds": _rounded(spoken_end),
                "duration_seconds": _rounded(spoken_span),
                "definition": "earliest segment start through latest segment end",
            },
            "merged_active_seconds": _rounded(active_seconds),
            "speech_occupancy": _rounded(occupancy),
            "language_units": {
                "method": unit_method,
                "count": unit_count,
                "units_per_minute_wall": _rounded(units_per_minute_wall),
                "units_per_minute_active": _rounded(units_per_minute_active),
            },
            "phrase_duration_seconds": {
                "basis": "one transcript segment is treated as one phrase proxy",
                "median": _rounded(statistics.median(phrase_durations))
                if phrase_durations
                else None,
                "p90": _rounded(_percentile_linear(phrase_durations, 0.9)),
                "p90_method": "linear interpolation at index (n - 1) * 0.9",
            },
            "gaps": {
                "basis": "positive internal gaps between merged active intervals",
                "threshold_counts": {
                    f"{threshold:g}": sum(1 for gap in gaps if gap + 1e-12 >= threshold)
                    for threshold in GAP_THRESHOLDS
                },
                "longest_seconds": _rounded(max(gaps)) if gaps else None,
            },
        },
    }


def _render_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_atomic(path: Path, rendered: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_text(rendered, encoding="utf-8", newline="\n")
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise AuditInputError(f"could not write output {path}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit faster-whisper transcript pacing from segment timing and text; "
            "does not judge whether the pace is good or persuasive."
        )
    )
    parser.add_argument("transcript", type=Path, help="Path to faster-whisper-style JSON")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON destination; the same report is also written to stdout",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    transcript_path = args.transcript.resolve()
    output_path = args.output.resolve() if args.output is not None else None
    try:
        if output_path is not None and output_path == transcript_path:
            raise AuditInputError("--output must not overwrite the input transcript")
        report = audit_transcript(_load_json(transcript_path), source_path=transcript_path)
        rendered = _render_json(report)
        if output_path is not None:
            _write_atomic(output_path, rendered)
    except AuditInputError as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
