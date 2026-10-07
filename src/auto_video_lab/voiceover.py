from __future__ import annotations

import asyncio
import re
from pathlib import Path

import edge_tts

from .media import duration_seconds, ffprobe
from .paths import JobPaths, ffmpeg_path
from .util import read_json, run_command, safe_resolve_within, sha256_file, write_json


class VoiceoverSpecError(ValueError):
    pass


EMOTION_PRESETS = {
    "frustrated": {"rate": "+6%", "pitch": "+2Hz", "volume": "+1%"},
    "curious": {"rate": "+2%", "pitch": "+8Hz", "volume": "+0%"},
    "surprised": {"rate": "+10%", "pitch": "+12Hz", "volume": "+1%"},
    "playful": {"rate": "+7%", "pitch": "+8Hz", "volume": "+1%"},
    "confident": {"rate": "-2%", "pitch": "-2Hz", "volume": "+1%"},
    "relieved": {"rate": "-6%", "pitch": "-4Hz", "volume": "+0%"},
    "urgent": {"rate": "+8%", "pitch": "+4Hz", "volume": "+1%"},
    "warm": {"rate": "-3%", "pitch": "+0Hz", "volume": "+0%"},
}

ROLES = {"hook", "problem", "proof", "benefit", "objection", "cta"}
ANGLES = {
    "pain_solution",
    "ugc_discovery",
    "demo_proof",
    "comparison",
    "confession",
    "warning",
}
GENERIC_HYPE = {
    "game changer",
    "must have",
    "life changing",
    "revolutionary",
    "best ever",
    "everyone needs this",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise VoiceoverSpecError(message)


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text))


def validate_voiceover_spec(job_id: str, spec: dict) -> dict:
    _require(spec.get("schema_version") == 1, "schema_version must be 1")
    _require(spec.get("job_id") == job_id, "spec job_id must match the selected job")
    voice = spec.get("voice")
    _require(isinstance(voice, str) and voice.strip(), "voice is required")
    output_filename = spec.get("output_filename", "voiceover-directed.mp3")
    _require(
        isinstance(output_filename, str)
        and Path(output_filename).name == output_filename
        and output_filename.lower().endswith(".mp3"),
        "output_filename must be a plain .mp3 filename",
    )

    audience = spec.get("target_audience")
    _require(isinstance(audience, dict), "target_audience must be an object")
    for field in ("persona", "situation", "pain_point", "desired_outcome"):
        value = audience.get(field)
        _require(
            isinstance(value, str) and value.strip(),
            f"target_audience.{field} is required",
        )
    objections = audience.get("objections", [])
    _require(isinstance(objections, list), "target_audience.objections must be an array")
    _require(
        all(isinstance(item, str) and item.strip() for item in objections),
        "target_audience.objections entries must be non-empty strings",
    )

    angle = spec.get("angle")
    _require(angle in ANGLES, f"angle must be one of: {', '.join(sorted(ANGLES))}")
    verified_claims = spec.get("verified_claims")
    _require(isinstance(verified_claims, dict), "verified_claims must be an object")
    for claim_id, evidence in verified_claims.items():
        _require(isinstance(claim_id, str) and claim_id, "verified claim ids must be strings")
        _require(
            isinstance(evidence, str) and evidence.strip(),
            f"verified_claims.{claim_id} needs evidence",
        )
    forbidden_claims = spec.get("forbidden_claims", [])
    _require(isinstance(forbidden_claims, list), "forbidden_claims must be an array")
    _require(
        all(isinstance(item, str) and item.strip() for item in forbidden_claims),
        "forbidden_claims entries must be non-empty strings",
    )

    segments = spec.get("segments")
    _require(
        isinstance(segments, list) and 3 <= len(segments) <= 10, "segments must have 3-10 items"
    )
    checked_segments = []
    seen_ids: set[str] = set()
    for index, segment in enumerate(segments):
        prefix = f"segments[{index}]"
        _require(isinstance(segment, dict), f"{prefix} must be an object")
        segment_id = segment.get("id")
        _require(isinstance(segment_id, str) and segment_id, f"{prefix}.id is required")
        _require(segment_id not in seen_ids, f"duplicate segment id: {segment_id}")
        seen_ids.add(segment_id)
        role = segment.get("role")
        _require(role in ROLES, f"{prefix}.role is unsupported")
        emotion = segment.get("emotion")
        _require(emotion in EMOTION_PRESETS, f"{prefix}.emotion is unsupported")
        text = segment.get("text")
        _require(isinstance(text, str) and text.strip(), f"{prefix}.text is required")
        _require(_word_count(text) <= 28, f"{prefix}.text is too long; split the performance beat")
        claim_ids = segment.get("claim_ids", [])
        _require(isinstance(claim_ids, list), f"{prefix}.claim_ids must be an array")
        unknown_claims = [claim_id for claim_id in claim_ids if claim_id not in verified_claims]
        _require(not unknown_claims, f"{prefix} references unverified claims: {unknown_claims}")
        pause_after_ms = int(segment.get("pause_after_ms", 80))
        _require(0 <= pause_after_ms <= 1500, f"{prefix}.pause_after_ms is invalid")
        delivery = dict(EMOTION_PRESETS[emotion])
        custom_delivery = segment.get("delivery", {})
        _require(isinstance(custom_delivery, dict), f"{prefix}.delivery must be an object")
        for key, pattern in {
            "rate": r"^[+-][0-9]{1,2}%$",
            "pitch": r"^[+-][0-9]{1,2}Hz$",
            "volume": r"^[+-][0-9]{1,2}%$",
        }.items():
            if key in custom_delivery:
                _require(
                    re.fullmatch(pattern, str(custom_delivery[key])) is not None,
                    f"{prefix}.delivery.{key} has an invalid format",
                )
                delivery[key] = str(custom_delivery[key])
        checked_segments.append(
            {
                **segment,
                "id": segment_id,
                "role": role,
                "emotion": emotion,
                "text": text.strip(),
                "caption": str(segment.get("caption", text)).strip(),
                "claim_ids": claim_ids,
                "pause_after_ms": pause_after_ms,
                "delivery": delivery,
            }
        )

    _require(checked_segments[0]["role"] == "hook", "the first segment must be the hook")
    _require(checked_segments[-1]["role"] == "cta", "the last segment must be the CTA")
    return {
        **spec,
        "voice": voice.strip(),
        "output_filename": output_filename,
        "target_audience": {
            "persona": audience["persona"].strip(),
            "situation": audience["situation"].strip(),
            "pain_point": audience["pain_point"].strip(),
            "desired_outcome": audience["desired_outcome"].strip(),
            "objections": [item.strip() for item in objections],
        },
        "angle": angle,
        "verified_claims": verified_claims,
        "forbidden_claims": forbidden_claims,
        "segments": checked_segments,
    }


def assess_voiceover_spec(spec: dict) -> dict:
    segments = spec["segments"]
    roles = {segment["role"] for segment in segments}
    script = " ".join(segment["text"] for segment in segments)
    lowered = script.lower()
    hook = segments[0]["text"]
    cta = segments[-1]["text"].lower()
    total_words = _word_count(script)
    distinct_emotions = len({segment["emotion"] for segment in segments})
    proof_segments = [segment for segment in segments if segment["role"] == "proof"]

    checks = {
        "hook_is_concise": _word_count(hook) <= 14,
        "hook_addresses_viewer_or_interrupts_pattern": bool(
            re.search(r"\b(you|your|if|wait|stop|why|okay|watch)\b", hook, re.I) or "?" in hook
        ),
        "has_problem_or_pain": "problem" in roles or spec["angle"] == "pain_solution",
        "has_evidence_backed_proof": bool(
            proof_segments and any(segment["claim_ids"] for segment in proof_segments)
        ),
        "has_benefit": "benefit" in roles,
        "has_direct_cta": "cta" in roles
        and bool(re.search(r"\b(tap|shop|cart|link|grab|get)\b", cta)),
        "uses_emotional_arc": distinct_emotions >= 3,
        "short_form_word_count": 24 <= total_words <= 65,
        "avoids_generic_hype": not any(phrase in lowered for phrase in GENERIC_HYPE),
        "all_referenced_claims_have_evidence": all(
            claim_id in spec["verified_claims"]
            for segment in segments
            for claim_id in segment["claim_ids"]
        ),
    }
    weights = {
        "hook_is_concise": 8,
        "hook_addresses_viewer_or_interrupts_pattern": 12,
        "has_problem_or_pain": 8,
        "has_evidence_backed_proof": 16,
        "has_benefit": 10,
        "has_direct_cta": 12,
        "uses_emotional_arc": 12,
        "short_form_word_count": 8,
        "avoids_generic_hype": 6,
        "all_referenced_claims_have_evidence": 8,
    }
    score = sum(weights[name] for name, passed in checks.items() if passed)
    required = (
        "hook_addresses_viewer_or_interrupts_pattern",
        "has_evidence_backed_proof",
        "has_benefit",
        "has_direct_cta",
        "all_referenced_claims_have_evidence",
    )
    return {
        "scope": "Heuristic script and performance-plan audit; not a conversion prediction",
        "score": score,
        "ready_for_synthesis": score >= 78 and all(checks[name] for name in required),
        "word_count": total_words,
        "estimated_duration_seconds": round(total_words / 2.7, 2),
        "distinct_emotions": distinct_emotions,
        "checks": checks,
        "failed_checks": [name for name, passed in checks.items() if not passed],
    }


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


async def _synthesize_segments(spec: dict, segment_dir: Path) -> list[Path]:
    paths = []
    for index, segment in enumerate(spec["segments"]):
        path = segment_dir / f"{index:02d}_{segment['id']}.mp3"
        delivery = segment["delivery"]
        communicate = edge_tts.Communicate(
            segment["text"],
            spec["voice"],
            rate=delivery["rate"],
            pitch=delivery["pitch"],
            volume=delivery["volume"],
        )
        await communicate.save(str(path))
        paths.append(path)
    return paths


def synthesize_voiceover(job_id: str, spec_path: Path) -> dict:
    job = JobPaths.for_id(job_id)
    job.ensure()
    resolved_spec = safe_resolve_within(spec_path, job.root)
    spec = validate_voiceover_spec(job_id, read_json(resolved_spec))
    assessment = assess_voiceover_spec(spec)
    if not assessment["ready_for_synthesis"]:
        raise VoiceoverSpecError(
            "voiceover strategy is not ready for synthesis: "
            + ", ".join(assessment["failed_checks"])
        )

    output_path = job.inputs / spec["output_filename"]
    subtitle_path = output_path.with_suffix(".srt")
    report_path = job.reports / f"{output_path.stem}-report.json"
    for path in (output_path, subtitle_path, report_path):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing voiceover artifact: {path}")

    segment_dir = job.work / f"{output_path.stem}-segments"
    segment_dir.mkdir(parents=True, exist_ok=False)
    segment_paths = asyncio.run(_synthesize_segments(spec, segment_dir))
    segment_durations = [duration_seconds(ffprobe(path)) for path in segment_paths]

    args = [str(ffmpeg_path()), "-hide_banner", "-loglevel", "error"]
    for path in segment_paths:
        args.extend(["-i", str(path)])
    filters = []
    concat_inputs = []
    timing = []
    cursor = 0.0
    concat_count = 0
    for index, (segment, duration) in enumerate(
        zip(spec["segments"], segment_durations, strict=True)
    ):
        label = f"seg{index}"
        filters.append(
            f"[{index}:a]aresample=48000,aformat=sample_fmts=fltp:"
            f"sample_rates=48000:channel_layouts=stereo[{label}]"
        )
        concat_inputs.append(f"[{label}]")
        concat_count += 1
        timing.append(
            {
                "id": segment["id"],
                "role": segment["role"],
                "emotion": segment["emotion"],
                "start": round(cursor, 3),
                "end": round(cursor + duration, 3),
                "text": segment["text"],
                "caption": segment["caption"],
                "delivery": segment["delivery"],
                "claim_ids": segment["claim_ids"],
            }
        )
        cursor += duration
        pause_ms = segment["pause_after_ms"] if index < len(spec["segments"]) - 1 else 0
        if pause_ms:
            pause_label = f"pause{index}"
            pause_seconds = pause_ms / 1000
            filters.append(
                "anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=duration={pause_seconds:.3f}[{pause_label}]"
            )
            concat_inputs.append(f"[{pause_label}]")
            concat_count += 1
            cursor += pause_seconds
    filters.append("".join(concat_inputs) + f"concat=n={concat_count}:v=0:a=1[outa]")
    args.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outa]",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(output_path),
        ]
    )
    run_command(args, cwd=job.root)

    srt_lines = []
    for index, item in enumerate(timing, start=1):
        srt_lines.extend(
            [
                str(index),
                f"{_srt_time(item['start'])} --> {_srt_time(item['end'])}",
                item["caption"],
                "",
            ]
        )
    subtitle_path.write_text("\n".join(srt_lines), encoding="utf-8-sig")
    final_duration = duration_seconds(ffprobe(output_path))
    report = {
        "status": "LOCAL_DIRECTED_VOICEOVER_COMPLETE",
        "scope": "Segmented online neural TTS synthesis plus local FFmpeg assembly",
        "job_id": job_id,
        "spec": str(resolved_spec),
        "voice": spec["voice"],
        "target_audience": spec["target_audience"],
        "angle": spec["angle"],
        "assessment": assessment,
        "output": str(output_path),
        "subtitles": str(subtitle_path),
        "duration_seconds": round(final_duration, 3),
        "sha256": sha256_file(output_path),
        "segments": timing,
        "limitations": [
            "Emotion is approximated through per-segment voice, rate, pitch, volume, "
            "punctuation, and pauses.",
            "The assessment is heuristic and does not measure retention, clicks, or sales.",
            "A human listening review and platform test are still required for performance claims.",
        ],
    }
    write_json(report_path, report)
    return report
