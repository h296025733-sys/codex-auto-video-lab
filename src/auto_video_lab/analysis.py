from __future__ import annotations

import math
import re
import sys
import wave
from array import array
from pathlib import Path
from statistics import median

from .media import ffprobe, media_kind, summarize_probe
from .paths import JobPaths, ffmpeg_path, workspace_root
from .util import read_json, run_command, sha256_file, write_json

SCENE_RE = re.compile(r"pts_time:([0-9.]+)")
SILENCE_START_RE = re.compile(r"silence_start: ([0-9.]+)")
SILENCE_END_RE = re.compile(r"silence_end: ([0-9.]+)")


def _annotate_word_acoustics(path: Path, output_dir: Path, segments: list[dict]) -> dict:
    """Add conservative, locally measured vocal-intensity cues to timed words.

    The cue is deliberately not called emotion detection: it measures short-window
    RMS energy and neighboring pauses, then compares each word with nearby speech.
    Semantic planning still decides whether a louder word deserves typography.
    """
    timed_words = [
        word
        for segment in segments
        for word in segment.get("words", [])
        if isinstance(word.get("start"), (int, float))
        and isinstance(word.get("end"), (int, float))
        and float(word["end"]) > float(word["start"])
    ]
    if not timed_words:
        return {"status": "unavailable", "reason": "no_timed_words"}

    output_dir.mkdir(parents=True, exist_ok=True)
    pcm_path = output_dir / f".{path.stem}.word-acoustics.wav"
    try:
        if pcm_path.exists():
            pcm_path.unlink()
        run_command(
            [
                str(ffmpeg_path()),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(pcm_path),
            ]
        )
        energies: list[float] = []
        with wave.open(str(pcm_path), "rb") as handle:
            sample_rate = handle.getframerate()
            frame_count = handle.getnframes()
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                return {"status": "unavailable", "reason": "unexpected_pcm_format"}
            for word in timed_words:
                start = max(0.0, float(word["start"]) - 0.025)
                end = max(start + 0.02, float(word["end"]) + 0.025)
                first_frame = min(frame_count, max(0, int(start * sample_rate)))
                last_frame = min(frame_count, max(first_frame + 1, int(end * sample_rate)))
                handle.setpos(first_frame)
                samples = array("h", handle.readframes(last_frame - first_frame))
                if sys.byteorder != "little":
                    samples.byteswap()
                if not samples:
                    energies.append(0.0)
                    continue
                mean_square = sum(sample * sample for sample in samples) / len(samples)
                energies.append(math.sqrt(mean_square))

        high_count = 0
        medium_count = 0
        for index, (word, energy) in enumerate(zip(timed_words, energies, strict=True)):
            window_start = max(0, index - 4)
            window_end = min(len(energies), index + 5)
            neighbor_energies = [
                value
                for neighbor_index, value in enumerate(
                    energies[window_start:window_end],
                    start=window_start,
                )
                if neighbor_index != index and value > 0
            ]
            baseline = median(neighbor_energies) if neighbor_energies else max(energy, 1.0)
            relative_db = 20 * math.log10(max(energy, 1.0) / max(baseline, 1.0))
            energy_dbfs = 20 * math.log10(max(energy, 1.0) / 32768.0)
            previous_end = (
                float(timed_words[index - 1]["end"])
                if index > 0
                else float(word["start"])
            )
            next_start = (
                float(timed_words[index + 1]["start"])
                if index + 1 < len(timed_words)
                else float(word["end"])
            )
            pre_pause = max(0.0, min(2.0, float(word["start"]) - previous_end))
            post_pause = max(0.0, min(2.0, next_start - float(word["end"])))
            strongest_pause = max(pre_pause, post_pause)
            if relative_db >= 3.8 or (relative_db >= 2.6 and strongest_pause >= 0.12):
                emphasis = "high"
                high_count += 1
            elif relative_db >= 2.2 or (relative_db >= 1.4 and strongest_pause >= 0.16):
                emphasis = "medium"
                medium_count += 1
            else:
                emphasis = "none"
            word["acoustics"] = {
                "energy_dbfs": round(energy_dbfs, 2),
                "relative_energy_db": round(relative_db, 2),
                "pre_pause_seconds": round(pre_pause, 3),
                "post_pause_seconds": round(post_pause, 3),
                "emphasis_hint": emphasis,
            }
        return {
            "status": "available",
            "method": "local_word_rms_plus_neighbor_pauses",
            "high_cues": high_count,
            "medium_cues": medium_count,
            "word_count": len(timed_words),
        }
    except Exception:
        return {"status": "unavailable", "reason": "audio_measurement_failed"}
    finally:
        try:
            pcm_path.unlink(missing_ok=True)
        except OSError:
            pass


def detect_scenes(path: Path, threshold: float = 0.32) -> list[float]:
    process = run_command(
        [
            str(ffmpeg_path()),
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            str(path),
            "-an",
            "-vf",
            f"select='gt(scene,{threshold})',showinfo",
            "-vsync",
            "vfr",
            "-f",
            "null",
            "-",
        ],
        check=False,
    )
    return sorted({round(float(value), 3) for value in SCENE_RE.findall(process.stderr)})


def detect_silences(
    path: Path,
    *,
    noise_db: float = -35.0,
    min_duration: float = 0.6,
) -> list[dict]:
    process = run_command(
        [
            str(ffmpeg_path()),
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_duration}",
            "-f",
            "null",
            "-",
        ],
        check=False,
    )
    starts = [
        (match.start(), float(match.group(1)))
        for match in SILENCE_START_RE.finditer(process.stderr)
    ]
    ends = [
        (match.start(), float(match.group(1))) for match in SILENCE_END_RE.finditer(process.stderr)
    ]
    spans: list[dict] = []
    end_index = 0
    for start_position, start_time in starts:
        while end_index < len(ends) and ends[end_index][0] < start_position:
            end_index += 1
        if end_index < len(ends):
            end_time = ends[end_index][1]
            spans.append(
                {
                    "start": round(start_time, 3),
                    "end": round(end_time, 3),
                    "duration": round(end_time - start_time, 3),
                }
            )
            end_index += 1
    return spans


def _format_srt_time(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def transcribe_media(
    path: Path,
    output_dir: Path,
    model_path: Path,
    *,
    language: str | None = None,
) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "Transcription dependency is missing. Install the workspace package first."
        ) from exc

    if not model_path.is_dir():
        raise FileNotFoundError(
            f"Local Whisper model is missing: {model_path}. "
            "Place a faster-whisper model there before requesting transcription."
        )

    model = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=2, num_workers=1)
    segments_iterator, info = model.transcribe(
        str(path),
        beam_size=5,
        vad_filter=True,
        word_timestamps=True,
        language=language,
        condition_on_previous_text=False,
        vad_parameters={"min_silence_duration_ms": 450},
    )
    segments = []
    for segment in segments_iterator:
        segments.append(
            {
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "text": segment.text.strip(),
                "words": [
                    {
                        "start": None if word.start is None else round(word.start, 3),
                        "end": None if word.end is None else round(word.end, 3),
                        "word": word.word,
                        "probability": round(word.probability, 4),
                    }
                    for word in (segment.words or [])
                ],
            }
        )

    prosody = _annotate_word_acoustics(path, output_dir, segments)
    result = {
        "engine": "faster-whisper",
        "model": model_path.name,
        "device": "cpu",
        "compute_type": "int8",
        "language": info.language,
        "language_probability": round(info.language_probability, 4),
        "prosody": prosody,
        "segments": segments,
    }
    stem = path.stem
    write_json(output_dir / f"{stem}.transcript.json", result)
    srt_lines: list[str] = []
    for index, segment in enumerate(segments, start=1):
        srt_lines.extend(
            [
                str(index),
                f"{_format_srt_time(segment['start'])} --> {_format_srt_time(segment['end'])}",
                segment["text"],
                "",
            ]
        )
    (output_dir / f"{stem}.transcript.srt").write_text("\n".join(srt_lines), encoding="utf-8-sig")
    return result


def analyze_job(
    job_id: str,
    *,
    transcribe: bool = False,
    language: str | None = None,
) -> dict:
    job = JobPaths.for_id(job_id)
    if not job.manifest.is_file():
        raise FileNotFoundError(f"Job manifest is missing: {job.manifest}")
    manifest = read_json(job.manifest)
    results = []
    model_path = workspace_root() / "models" / "faster-whisper-small"

    for asset in manifest["assets"]:
        path = job.root / asset["job_path"]
        kind = media_kind(path)
        probe = ffprobe(path)
        summary = {
            "asset_id": asset["asset_id"],
            "job_path": asset["job_path"],
            "kind": kind,
            "sha256": sha256_file(path),
            "probe": summarize_probe(probe),
            "scene_candidates_seconds": [],
            "silence_spans": [],
            "transcript_path": None,
        }
        if kind == "video":
            summary["scene_candidates_seconds"] = detect_scenes(path)
            if summary["probe"]["audio"] is not None:
                summary["silence_spans"] = detect_silences(path)
                if transcribe:
                    transcribe_media(path, job.reports, model_path, language=language)
                    summary["transcript_path"] = (
                        Path("reports") / f"{path.stem}.transcript.json"
                    ).as_posix()
        elif kind == "audio" and transcribe:
            transcribe_media(path, job.reports, model_path, language=language)
            summary["transcript_path"] = (
                Path("reports") / f"{path.stem}.transcript.json"
            ).as_posix()
        results.append(summary)

    analysis = {
        "schema_version": 1,
        "job_id": job_id,
        "scope": {
            "probe": True,
            "scene_candidate_detection": True,
            "silence_detection": True,
            "speech_transcription": transcribe,
            "requested_transcript_language": language,
            "semantic_selection": False,
        },
        "assets": results,
    }
    write_json(job.analysis, analysis)
    return analysis
