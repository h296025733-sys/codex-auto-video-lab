from __future__ import annotations

import re
from pathlib import Path

from .media import ffprobe, first_stream, fps_value, summarize_probe
from .paths import JobPaths, ffmpeg_path
from .plan import plan_duration, validate_plan
from .render import _finite_loudnorm_value, _measure_loudness
from .util import read_json, run_command, write_json

BLACK_RE = re.compile(
    r"black_start:(?P<start>[0-9.]+) black_end:(?P<end>[0-9.]+) "
    r"black_duration:(?P<duration>[0-9.]+)"
)
MEAN_VOLUME_RE = re.compile(r"mean_volume: (?P<value>-?inf|-?[0-9.]+) dB")
MAX_VOLUME_RE = re.compile(r"max_volume: (?P<value>-?inf|-?[0-9.]+) dB")


def _parse_number(value: str | None) -> float | str | None:
    if value is None:
        return None
    if value == "-inf":
        return value
    return float(value)


def _extract_frames(output_path: Path, target_dir: Path, duration: float) -> list[str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    frame_paths = []
    for label, fraction in (("start", 0.08), ("middle", 0.5), ("end", 0.92)):
        timestamp = max(0.0, duration * fraction)
        frame_path = target_dir / f"{label}.jpg"
        extracted = _extract_frame(output_path, frame_path, timestamp)
        if extracted is not None:
            frame_paths.append(extracted)
    return frame_paths


def _extract_frame(output_path: Path, frame_path: Path, timestamp: float) -> str | None:
    process = run_command(
        [
            str(ffmpeg_path()),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(output_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(frame_path),
        ],
        check=False,
    )
    return str(frame_path) if process.returncode == 0 and frame_path.is_file() else None


def _extract_critical_overlay_frames(
    output_path: Path,
    target_dir: Path,
    duration: float,
    overlays: list[dict],
) -> list[dict]:
    target_dir.mkdir(parents=True, exist_ok=True)
    critical = [
        overlay
        for overlay in overlays
        if str(overlay.get("preset", "")).startswith("fine_")
    ][:24]
    results: list[dict] = []
    for index, overlay in enumerate(critical):
        start = float(overlay["start"])
        end = float(overlay["end"])
        timestamp = min(max(0.0, (start + end) / 2), max(0.0, duration - 0.001))
        preset = str(overlay["preset"])
        frame_path = target_dir / f"critical-{index:03d}-{preset}.jpg"
        extracted = _extract_frame(output_path, frame_path, timestamp)
        if extracted is not None:
            results.append(
                {
                    "time_seconds": round(timestamp, 3),
                    "preset": preset,
                    "text": overlay["text"],
                    "path": extracted,
                }
            )
    return results


def qa_job(job_id: str) -> dict:
    job = JobPaths.for_id(job_id)
    validation = validate_plan(job_id, read_json(job.plan))
    plan = validation["plan"]
    expected_duration = plan_duration(plan)
    output_path = job.output / plan["output"]["filename"]
    if not output_path.is_file():
        raise FileNotFoundError(f"Rendered output is missing: {output_path}")

    probe = ffprobe(output_path)
    summary = summarize_probe(probe)
    video = first_stream(probe, "video")
    audio = first_stream(probe, "audio")
    observed_duration = float(summary["duration_seconds"])

    decode = run_command(
        [
            str(ffmpeg_path()),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(output_path),
            "-f",
            "null",
            "-",
        ],
        check=False,
    )
    black = run_command(
        [
            str(ffmpeg_path()),
            "-hide_banner",
            "-i",
            str(output_path),
            "-vf",
            "blackdetect=d=0.5:pix_th=0.10",
            "-an",
            "-f",
            "null",
            "-",
        ],
        check=False,
    )
    volume = run_command(
        [
            str(ffmpeg_path()),
            "-hide_banner",
            "-i",
            str(output_path),
            "-vn",
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        check=False,
    )

    black_spans = [
        {
            "start": float(match.group("start")),
            "end": float(match.group("end")),
            "duration": float(match.group("duration")),
        }
        for match in BLACK_RE.finditer(black.stderr)
    ]
    mean_match = MEAN_VOLUME_RE.search(volume.stderr)
    max_match = MAX_VOLUME_RE.search(volume.stderr)
    finishing = plan.get("finishing", {})
    loudness_target = finishing.get("loudness_target_lufs")
    true_peak_limit = float(finishing.get("true_peak_limit_db", -1.5))
    loudness_measurement = None
    loudness_stderr_tail: list[str] = []
    if audio is not None and loudness_target is not None:
        loudness_measurement, loudness_stderr_tail = _measure_loudness(
            output_path,
            target_lufs=float(loudness_target),
            true_peak_limit_db=true_peak_limit,
        )
    measured_i = _finite_loudnorm_value(loudness_measurement or {}, "input_i")
    measured_tp = _finite_loudnorm_value(loudness_measurement or {}, "input_tp")
    measured_silence = (
        loudness_measurement is not None
        and str(loudness_measurement.get("input_i", "")).lower() == "-inf"
    )
    actual_fps = fps_value(video)
    checks = {
        "full_decode": decode.returncode == 0,
        "video_stream_present": video is not None,
        "audio_stream_present": audio is not None,
        "resolution_matches_plan": video is not None
        and video.get("width") == plan["output"]["width"]
        and video.get("height") == plan["output"]["height"],
        "duration_within_tolerance": abs(observed_duration - expected_duration) <= 0.30,
        "fps_within_tolerance": actual_fps is not None
        and abs(actual_fps - float(plan["output"]["fps"])) <= 0.05,
        "loudness_target_met": loudness_target is None
        or measured_silence
        or (
            measured_i is not None
            and abs(measured_i - float(loudness_target)) <= 0.5
        ),
        "true_peak_limit_met": loudness_target is None
        or measured_silence
        or (
            measured_tp is not None
            and measured_tp <= true_peak_limit + 0.2
        ),
    }
    passed = all(checks.values())
    frames = _extract_frames(output_path, job.reports / "qa-frames", observed_duration)
    critical_overlay_frames = _extract_critical_overlay_frames(
        output_path,
        job.reports / "qa-frames",
        observed_duration,
        plan["overlays"],
    )
    report = {
        "status": "LOCAL_TECHNICAL_QA_PASS" if passed else "LOCAL_TECHNICAL_QA_FAIL",
        "scope": (
            "local container/stream inspection, full decode, resolution, duration, frame-rate, "
            "black-frame detection, audio level scan, measured integrated loudness and true peak, "
            "three sampled frame extractions, "
            "and up to 24 fine-cut overlay midpoint extractions"
        ),
        "job_id": job_id,
        "output": str(output_path),
        "expected_duration_seconds": round(expected_duration, 3),
        "observed": summary,
        "checks": checks,
        "black_spans_over_0_5_seconds": black_spans,
        "audio_levels": {
            "mean_db": _parse_number(mean_match.group("value") if mean_match else None),
            "max_db": _parse_number(max_match.group("value") if max_match else None),
            "target_lufs": loudness_target,
            "true_peak_limit_db": true_peak_limit,
            "loudnorm_measurement": loudness_measurement,
            "loudnorm_stderr_tail": loudness_stderr_tail,
        },
        "sample_frames": frames,
        "critical_overlay_frames": critical_overlay_frames,
        "not_tested": [
            "semantic compliance with the natural-language brief",
            "speech transcript correctness",
            "caption wording and safe-area aesthetics",
            "human-perceived pacing and story quality",
            "social-platform upload or playback",
        ],
        "decode_stderr": decode.stderr,
    }
    write_json(job.qa, report)
    return report
