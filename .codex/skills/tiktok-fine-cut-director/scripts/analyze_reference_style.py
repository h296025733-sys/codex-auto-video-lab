#!/usr/bin/env python3
"""Create bounded, reproducible evidence from a local reference-video job.

The script is deliberately mechanical. It reads the job manifest and the
workspace analysis report, verifies the copied inputs, and derives scene,
soundtrack-rhythm, loudness, and sparse visual-sampling evidence. It does not
perform OCR or make semantic, font-identity, retention, or conversion claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
METHOD_VERSION = "2026-08-22.1"
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SCENE_PTS_RE = re.compile(r"pts_time:([0-9.]+)")
LOUDNORM_JSON_RE = re.compile(r'\{\s*"input_i".*?\}', re.DOTALL)
CONTACT_SHEET_SAMPLES = 12
SENSITIVE_SCENE_THRESHOLD = 0.12
AUDIO_SAMPLE_RATE = 22_050
AUDIO_HOP_LENGTH = 512


class EvidenceError(RuntimeError):
    """Raised when evidence cannot be derived without weakening its contract."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise EvidenceError(f"Required file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"Expected a JSON object in {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_text(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    process = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        check=False,
    )
    if check and process.returncode != 0:
        tail = "\n".join((process.stderr or process.stdout).splitlines()[-20:])
        raise EvidenceError(
            f"Command failed with exit code {process.returncode}: {args[0]}\n{tail}"
        )
    return process


def _run_bytes(args: list[str]) -> bytes:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    process = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        creationflags=creationflags,
        check=False,
    )
    if process.returncode != 0:
        stderr = process.stderr.decode("utf-8", errors="replace")
        tail = "\n".join(stderr.splitlines()[-20:])
        raise EvidenceError(
            f"Command failed with exit code {process.returncode}: {args[0]}\n{tail}"
        )
    return process.stdout


def _resolve_toolchain(workspace: Path) -> dict[str, Any]:
    config_path = workspace / "config" / "toolchain.json"
    config = _read_json(config_path)
    ffmpeg_config = config.get("ffmpeg")
    if not isinstance(ffmpeg_config, dict):
        raise EvidenceError(f"Missing ffmpeg configuration in {config_path}")

    tools: dict[str, Any] = {}
    for name, path_key, hash_key in (
        ("ffmpeg", "ffmpegPath", "ffmpegSha256"),
        ("ffprobe", "ffprobePath", "ffprobeSha256"),
    ):
        relative = ffmpeg_config.get(path_key)
        if not isinstance(relative, str):
            raise EvidenceError(f"Missing {path_key} in {config_path}")
        path = (workspace / relative).resolve()
        if not path.is_file():
            raise EvidenceError(f"Configured {name} is missing: {path}")
        actual_hash = _sha256(path)
        expected_hash = str(ffmpeg_config.get(hash_key, "")).lower()
        if expected_hash and actual_hash != expected_hash:
            raise EvidenceError(
                f"Configured {name} hash mismatch: expected {expected_hash}, got {actual_hash}"
            )
        version_process = _run_text([str(path), "-version"])
        version_line = (version_process.stdout or version_process.stderr).splitlines()[0]
        tools[name] = {
            "path": path,
            "sha256": actual_hash,
            "matches_configured_sha256": bool(expected_hash and actual_hash == expected_hash),
            "version_line": version_line,
        }
    return tools


def _probe(ffprobe: Path, source: Path) -> dict[str, Any]:
    process = _run_text(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,width,height,avg_frame_rate,sample_rate,channels",
            "-of",
            "json",
            str(source),
        ]
    )
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"ffprobe returned invalid JSON for {source}: {exc}") from exc
    streams = payload.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    duration_raw = payload.get("format", {}).get("duration")
    duration = float(duration_raw) if duration_raw not in (None, "N/A") else None
    return {
        "duration_seconds": None if duration is None else round(duration, 6),
        "video": None
        if video is None
        else {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "avg_frame_rate": video.get("avg_frame_rate"),
        },
        "audio": None
        if audio is None
        else {
            "codec": audio.get("codec_name"),
            "sample_rate": _int_or_none(audio.get("sample_rate")),
            "channels": audio.get("channels"),
        },
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finite_float(value: Any, *, digits: int = 3) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if math.isfinite(number) else None


def _scene_candidates(ffmpeg: Path, source: Path, threshold: float) -> list[float]:
    process = _run_text(
        [
            str(ffmpeg),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            f"select='gt(scene,{threshold})',showinfo",
            "-fps_mode",
            "vfr",
            "-f",
            "null",
            "-",
        ],
        check=False,
    )
    if process.returncode != 0:
        tail = "\n".join(process.stderr.splitlines()[-20:])
        raise EvidenceError(f"Scene-candidate detection failed for {source}:\n{tail}")
    return sorted({round(float(value), 3) for value in SCENE_PTS_RE.findall(process.stderr)})


def _candidate_interval_stats(duration: float, candidates: list[float]) -> dict[str, Any]:
    boundaries = [0.0]
    boundaries.extend(sorted({value for value in candidates if 0.0 < value < duration}))
    boundaries.append(duration)
    intervals = [
        round(boundaries[index + 1] - boundaries[index], 3)
        for index in range(len(boundaries) - 1)
        if boundaries[index + 1] > boundaries[index]
    ]
    if not intervals:
        return {
            "candidate_interval_count": 0,
            "mean_seconds": None,
            "median_seconds": None,
            "minimum_seconds": None,
            "maximum_seconds": None,
        }
    return {
        "candidate_interval_count": len(intervals),
        "mean_seconds": round(statistics.fmean(intervals), 3),
        "median_seconds": round(statistics.median(intervals), 3),
        "minimum_seconds": round(min(intervals), 3),
        "maximum_seconds": round(max(intervals), 3),
    }


def _loudness_measurement(ffmpeg: Path, source: Path) -> dict[str, Any]:
    process = _run_text(
        [
            str(ffmpeg),
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-af",
            "loudnorm=I=-16:LRA=11:TP=-1.5:print_format=json",
            "-f",
            "null",
            "-",
        ],
        check=False,
    )
    if process.returncode != 0:
        tail = "\n".join(process.stderr.splitlines()[-20:])
        raise EvidenceError(f"Loudness measurement failed for {source}:\n{tail}")
    matches = LOUDNORM_JSON_RE.findall(process.stderr)
    if not matches:
        raise EvidenceError(f"Could not parse loudnorm measurement for {source}")
    raw = json.loads(matches[-1])
    return {
        "integrated_lufs": _finite_float(raw.get("input_i"), digits=2),
        "true_peak_dbtp": _finite_float(raw.get("input_tp"), digits=2),
        "loudness_range_lu": _finite_float(raw.get("input_lra"), digits=2),
        "threshold_lufs": _finite_float(raw.get("input_thresh"), digits=2),
        "method": "FFmpeg loudnorm input measurement on the flattened soundtrack",
    }


def _soundtrack_rhythm(ffmpeg: Path, source: Path, duration: float) -> dict[str, Any]:
    try:
        import librosa
        import numpy as np
    except ImportError as exc:
        raise EvidenceError(
            "The workspace Python environment is missing its existing librosa/numpy dependencies"
        ) from exc

    raw_pcm = _run_bytes(
        [
            str(ffmpeg),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-f",
            "f32le",
            "-",
        ]
    )
    samples = np.frombuffer(raw_pcm, dtype="<f4")
    if samples.size < AUDIO_HOP_LENGTH * 4:
        return {
            "decoded_sample_rate_hz": AUDIO_SAMPLE_RATE,
            "decoded_sample_count": int(samples.size),
            "primary_tempo_candidate_bpm": None,
            "tempo_candidates_bpm": [],
            "beat_candidates_seconds": [],
            "onset_candidates_seconds": [],
            "prominent_onset_candidates_seconds": [],
            "onset_candidate_density_per_second": 0.0,
        }

    onset_envelope = librosa.onset.onset_strength(
        y=samples,
        sr=AUDIO_SAMPLE_RATE,
        hop_length=AUDIO_HOP_LENGTH,
    )
    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset_envelope,
        sr=AUDIO_SAMPLE_RATE,
        hop_length=AUDIO_HOP_LENGTH,
        units="frames",
        backtrack=False,
        normalize=True,
    )
    onset_times_array = librosa.frames_to_time(
        onset_frames,
        sr=AUDIO_SAMPLE_RATE,
        hop_length=AUDIO_HOP_LENGTH,
    )
    onset_times = [
        round(float(value), 3)
        for value in onset_times_array
        if 0.0 <= float(value) <= duration + 0.05
    ]

    strengths = onset_envelope[onset_frames] if onset_frames.size else np.array([], dtype=float)
    if strengths.size:
        prominence_threshold = float(np.quantile(strengths, 0.75))
        prominent_onsets = [
            round(float(time_value), 3)
            for time_value, strength in zip(onset_times_array, strengths, strict=True)
            if float(strength) >= prominence_threshold
            and 0.0 <= float(time_value) <= duration + 0.05
        ]
    else:
        prominent_onsets = []

    tempo_raw, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_envelope,
        sr=AUDIO_SAMPLE_RATE,
        hop_length=AUDIO_HOP_LENGTH,
        trim=False,
    )
    tempo_values = np.asarray(tempo_raw).reshape(-1)
    primary_tempo = float(tempo_values[0]) if tempo_values.size else float("nan")
    primary_tempo_value = round(primary_tempo, 2) if math.isfinite(primary_tempo) else None
    tempo_candidates: list[float] = []
    if primary_tempo_value is not None and primary_tempo_value > 0:
        for candidate in (
            primary_tempo_value / 2.0,
            primary_tempo_value,
            primary_tempo_value * 2.0,
        ):
            if 45.0 <= candidate <= 240.0:
                tempo_candidates.append(round(candidate, 2))
    tempo_candidates = sorted(set(tempo_candidates))

    beat_times_array = librosa.frames_to_time(
        beat_frames,
        sr=AUDIO_SAMPLE_RATE,
        hop_length=AUDIO_HOP_LENGTH,
    )
    beat_times = [
        round(float(value), 3)
        for value in beat_times_array
        if 0.0 <= float(value) <= duration + 0.05
    ]
    return {
        "decoded_sample_rate_hz": AUDIO_SAMPLE_RATE,
        "decoded_sample_count": int(samples.size),
        "primary_tempo_candidate_bpm": primary_tempo_value,
        "tempo_candidates_bpm": tempo_candidates,
        "beat_candidates_seconds": beat_times,
        "onset_candidates_seconds": onset_times,
        "prominent_onset_candidates_seconds": prominent_onsets,
        "onset_candidate_density_per_second": round(len(onset_times) / max(duration, 0.001), 3),
    }


def _font_for_timestamp_overlay() -> Path:
    candidates = (
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeui.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise EvidenceError(
        "No local Arial or Segoe UI font was found for deterministic timestamp overlays"
    )


def _filter_path(path: Path) -> str:
    return path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")


def _contact_sheet(
    ffmpeg: Path,
    source: Path,
    destination: Path,
    duration: float,
    *,
    force: bool,
) -> dict[str, Any]:
    if destination.exists() and not force:
        raise EvidenceError(f"Refusing to overwrite without --force: {destination}")
    if duration <= 0:
        raise EvidenceError(f"Cannot sample a non-positive duration for {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fontfile = _filter_path(_font_for_timestamp_overlay())
    sampling_rate = CONTACT_SHEET_SAMPLES / duration
    filter_graph = (
        f"fps=fps={CONTACT_SHEET_SAMPLES}/{duration:.6f}:start_time=0:round=up,"
        "scale=240:426:force_original_aspect_ratio=decrease,"
        "pad=240:426:(ow-iw)/2:(oh-ih)/2:color=0x101010,"
        f"drawtext=fontfile='{fontfile}':text='%{{pts\\:hms}}':"
        "fontcolor=white:fontsize=18:box=1:boxcolor=black@0.68:boxborderw=4:"
        "x=8:y=h-th-8,"
        f"tile=4x3:nb_frames={CONTACT_SHEET_SAMPLES}:padding=8:margin=8:color=0x181818"
    )
    process = _run_text(
        [
            str(ffmpeg),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            filter_graph,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-update",
            "1",
            str(destination),
        ],
        check=False,
    )
    if process.returncode != 0 or not destination.is_file():
        tail = "\n".join(process.stderr.splitlines()[-20:])
        raise EvidenceError(f"Contact-sheet generation failed for {source}:\n{tail}")
    target_times = [
        round(index / sampling_rate, 3) for index in range(CONTACT_SHEET_SAMPLES)
    ]
    return {
        "path": destination.as_posix(),
        "sha256": _sha256(destination),
        "sample_count": CONTACT_SHEET_SAMPLES,
        "grid": {"columns": 4, "rows": 3},
        "target_sample_times_seconds": target_times,
        "timestamp_label": "decoded frame PTS rendered by FFmpeg drawtext",
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _require_job_path(job_root: Path, relative: str) -> Path:
    candidate = (job_root / relative).resolve()
    try:
        candidate.relative_to(job_root.resolve())
    except ValueError as exc:
        raise EvidenceError(f"Manifest path escapes the job directory: {relative}") from exc
    if not candidate.is_file():
        raise EvidenceError(f"Manifest asset is missing: {candidate}")
    return candidate


def _preflight_assets(
    job_root: Path,
    manifest: dict[str, Any],
    analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    manifest_assets = manifest.get("assets")
    analysis_assets = analysis.get("assets")
    if not isinstance(manifest_assets, list) or not isinstance(analysis_assets, list):
        raise EvidenceError("Both manifest.json and analysis.json must contain an assets list")
    analysis_by_id = {str(item.get("asset_id")): item for item in analysis_assets}
    if len(analysis_by_id) != len(analysis_assets):
        raise EvidenceError("analysis.json contains duplicate or missing asset_id values")

    resolved: list[dict[str, Any]] = []
    manifest_ids: set[str] = set()
    for manifest_asset in manifest_assets:
        asset_id = str(manifest_asset.get("asset_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", asset_id):
            raise EvidenceError(f"Invalid asset_id in manifest: {asset_id!r}")
        if asset_id in manifest_ids:
            raise EvidenceError(f"Duplicate asset_id in manifest: {asset_id}")
        manifest_ids.add(asset_id)
        analysis_asset = analysis_by_id.get(asset_id)
        if not isinstance(analysis_asset, dict):
            raise EvidenceError(f"analysis.json is missing {asset_id}")
        relative = manifest_asset.get("job_path")
        if not isinstance(relative, str) or analysis_asset.get("job_path") != relative:
            raise EvidenceError(f"job_path mismatch for {asset_id}")
        source = _require_job_path(job_root, relative)
        actual_hash = _sha256(source)
        manifest_hash = str(manifest_asset.get("sha256", "")).lower()
        analysis_hash = str(analysis_asset.get("sha256", "")).lower()
        if not manifest_hash or actual_hash != manifest_hash:
            raise EvidenceError(f"Source SHA-256 does not match manifest for {asset_id}")
        if analysis_hash and actual_hash != analysis_hash:
            raise EvidenceError(f"Source SHA-256 does not match analysis report for {asset_id}")
        resolved.append(
            {
                "asset_id": asset_id,
                "source": source,
                "relative": relative,
                "sha256": actual_hash,
                "kind": str(manifest_asset.get("kind") or analysis_asset.get("kind") or ""),
                "analysis": analysis_asset,
            }
        )
    if set(analysis_by_id) != manifest_ids:
        extras = sorted(set(analysis_by_id) - manifest_ids)
        raise EvidenceError(f"analysis.json has assets not present in manifest: {extras}")
    return resolved


def analyze_reference_job(workspace: Path, job_id: str, *, force: bool) -> Path:
    if not JOB_ID_RE.fullmatch(job_id):
        raise EvidenceError(
            "Job id must start with an ASCII letter or digit and contain only letters, "
            "digits, underscores, or hyphens (maximum 64 characters)"
        )
    workspace = workspace.resolve()
    job_root = (workspace / "jobs" / job_id).resolve()
    manifest_path = job_root / "manifest.json"
    analysis_path = job_root / "reports" / "analysis.json"
    report_path = job_root / "reports" / "fine-cut-evidence.json"
    contact_dir = job_root / "reports" / "fine-cut-evidence"

    manifest = _read_json(manifest_path)
    analysis = _read_json(analysis_path)
    if manifest.get("job_id") != job_id or analysis.get("job_id") != job_id:
        raise EvidenceError("Requested job id does not match manifest.json and analysis.json")
    resolved_assets = _preflight_assets(job_root, manifest, analysis)
    expected_sheets = [contact_dir / f"{item['asset_id']}.jpg" for item in resolved_assets]
    conflicts = [path for path in [report_path, *expected_sheets] if path.exists()]
    if conflicts and not force:
        lines = "\n".join(str(path) for path in conflicts)
        raise EvidenceError(
            f"Derived outputs already exist; rerun with --force to overwrite:\n{lines}"
        )

    tools = _resolve_toolchain(workspace)
    ffmpeg = tools["ffmpeg"]["path"]
    ffprobe = tools["ffprobe"]["path"]

    try:
        import librosa
        import numpy as np
    except ImportError as exc:
        raise EvidenceError(
            "The selected Python environment does not contain the workspace's "
            "librosa/numpy dependencies"
        ) from exc

    report_assets: list[dict[str, Any]] = []
    for item in resolved_assets:
        asset_id = item["asset_id"]
        source = item["source"]
        kind = item["kind"]
        analysis_asset = item["analysis"]
        actual_probe = _probe(ffprobe, source)
        duration = actual_probe.get("duration_seconds")
        if duration is None:
            duration = _finite_float(
                analysis_asset.get("probe", {}).get("duration_seconds"), digits=6
            )
        if duration is None or duration <= 0:
            raise EvidenceError(f"No positive duration is available for {asset_id}")

        analysis_duration = _finite_float(
            analysis_asset.get("probe", {}).get("duration_seconds"), digits=6
        )
        if analysis_duration is not None and abs(duration - analysis_duration) > 0.1:
            raise EvidenceError(
                f"Duration mismatch for {asset_id}: analysis={analysis_duration}, "
                f"ffprobe={duration}"
            )

        analysis_scene_candidates = [
            round(float(value), 3)
            for value in analysis_asset.get("scene_candidates_seconds", [])
            if isinstance(value, int | float) and 0.0 < float(value) < duration
        ]
        asset_report: dict[str, Any] = {
            "asset_id": asset_id,
            "job_path": item["relative"],
            "kind": kind,
            "sha256_verified_against_manifest": item["sha256"],
            "probe_cross_check": actual_probe,
            "analysis_scene_candidates": {
                "label": (
                    "candidates from reports/analysis.json; detector threshold is not "
                    "encoded in that report"
                ),
                "times_seconds": analysis_scene_candidates,
                "candidate_interval_stats": _candidate_interval_stats(
                    duration, analysis_scene_candidates
                ),
            },
            "sensitive_scene_candidates": None,
            "flattened_soundtrack": None,
            "contact_sheet": None,
        }

        if actual_probe.get("video") is not None:
            sensitive_candidates = _scene_candidates(
                ffmpeg, source, SENSITIVE_SCENE_THRESHOLD
            )
            asset_report["sensitive_scene_candidates"] = {
                "label": "FFmpeg scene-score candidates, not verified editorial cuts",
                "threshold": SENSITIVE_SCENE_THRESHOLD,
                "times_seconds": sensitive_candidates,
                "candidate_interval_stats": _candidate_interval_stats(
                    duration, sensitive_candidates
                ),
            }
            destination = contact_dir / f"{asset_id}.jpg"
            contact = _contact_sheet(
                ffmpeg,
                source,
                destination,
                duration,
                force=force,
            )
            contact["path"] = destination.relative_to(job_root).as_posix()
            asset_report["contact_sheet"] = contact

        if actual_probe.get("audio") is not None:
            asset_report["flattened_soundtrack"] = {
                "loudness": _loudness_measurement(ffmpeg, source),
                "rhythm_candidates": _soundtrack_rhythm(ffmpeg, source, duration),
                "interpretation_boundary": (
                    "Tempo, beat, and onset values are signal-derived candidates from the "
                    "flattened mix; narration, sound effects, and edit transients can be mistaken "
                    "for music rhythm. They do not identify a track or license."
                ),
            }
        report_assets.append(asset_report)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "job_id": job_id,
        "inputs": {
            "manifest": manifest_path.relative_to(job_root).as_posix(),
            "analysis": analysis_path.relative_to(job_root).as_posix(),
            "source_files_mutated": False,
        },
        "scope": {
            "source_hash_verification": True,
            "ffprobe_cross_check": True,
            "scene_candidate_detection": True,
            "flattened_soundtrack_loudness_measurement": True,
            "tempo_beat_onset_candidate_estimation": True,
            "timestamped_contact_sheet_sampling": True,
            "ocr": False,
            "exact_font_identification": False,
            "semantic_editorial_selection": False,
            "retention_or_conversion_verification": False,
        },
        "method": {
            "scene": {
                "sensitive_threshold": SENSITIVE_SCENE_THRESHOLD,
                "detector": "FFmpeg scene score with select/showinfo",
            },
            "rhythm": {
                "decoder": f"FFmpeg mono float32 PCM at {AUDIO_SAMPLE_RATE} Hz",
                "analyzer": "librosa onset strength, onset detection, and beat tracking",
                "hop_length_samples": AUDIO_HOP_LENGTH,
                "half_and_double_time_candidates_included_between_bpm": [45, 240],
            },
            "contact_sheet": {
                "sample_count": CONTACT_SHEET_SAMPLES,
                "sampling": "uniform decoded-frame sampling over full duration starting at PTS 0",
                "timestamp": "FFmpeg decoded frame PTS burned into each cell",
                "grid": "4x3",
            },
        },
        "runtime": {
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "librosa": librosa.__version__,
            "media_tools": {
                key: {
                    "version_line": value["version_line"],
                    "sha256": value["sha256"],
                    "matches_configured_sha256": value["matches_configured_sha256"],
                }
                for key, value in tools.items()
            },
        },
        "limitations": [
            "Scene scores are cut candidates, not editorial truth.",
            (
                "Tempo, beat, and onset estimates are heuristics on the flattened "
                "soundtrack; speech and edit transients can dominate them."
            ),
            "No OCR is performed.",
            (
                "Burned-in pixels cannot establish an exact font family, font license, "
                "or editable text properties."
            ),
            "Sparse contact sheets can miss short events between sampled frames.",
            (
                "This report cannot identify music title, source, or usage rights from a "
                "flattened soundtrack."
            ),
            (
                "This report does not prove viewer retention, persuasion, conversion, "
                "semantic quality, platform acceptance, or publishing compatibility."
            ),
        ],
        "assets": report_assets,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(report_path, payload)
    return report_path


def _default_workspace() -> Path:
    return Path(__file__).resolve().parents[4]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Derive read-only fine-cut evidence and timestamped contact sheets from an "
            "existing auto-video-lab job."
        )
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=_default_workspace(),
        help="Auto Video Lab workspace root (defaults to the script's containing workspace)",
    )
    parser.add_argument("--job", required=True, help="Existing isolated job id")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite only this script's derived JSON report and expected contact sheets",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        output = analyze_reference_job(args.workspace, args.job, force=args.force)
    except EvidenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "job_id": args.job,
                "report": str(output),
                "contact_sheet_directory": str(output.parent / "fine-cut-evidence"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
