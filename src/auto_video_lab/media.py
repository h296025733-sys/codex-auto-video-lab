from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from .paths import ffprobe_path
from .util import run_command

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


def media_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    return "unknown"


def ffprobe(path: Path) -> dict:
    process = run_command(
        [
            str(ffprobe_path()),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
    )
    return json.loads(process.stdout)


def first_stream(probe: dict, codec_type: str) -> dict | None:
    return next(
        (stream for stream in probe.get("streams", []) if stream.get("codec_type") == codec_type),
        None,
    )


def duration_seconds(probe: dict) -> float:
    raw = probe.get("format", {}).get("duration")
    if raw is not None:
        return float(raw)
    durations = [
        float(stream["duration"])
        for stream in probe.get("streams", [])
        if stream.get("duration") is not None
    ]
    return max(durations, default=0.0)


def fps_value(video_stream: dict | None) -> float | None:
    if not video_stream:
        return None
    raw = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
    if not raw or raw == "0/0":
        return None
    return float(Fraction(raw))


def summarize_probe(probe: dict) -> dict:
    video = first_stream(probe, "video")
    audio = first_stream(probe, "audio")
    return {
        "duration_seconds": round(duration_seconds(probe), 3),
        "video": None
        if video is None
        else {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": None if fps_value(video) is None else round(fps_value(video), 3),
            "pixel_format": video.get("pix_fmt"),
        },
        "audio": None
        if audio is None
        else {
            "codec": audio.get("codec_name"),
            "sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
            "channels": audio.get("channels"),
            "channel_layout": audio.get("channel_layout"),
        },
        "format": probe.get("format", {}).get("format_name"),
        "bit_rate": int(probe["format"]["bit_rate"])
        if probe.get("format", {}).get("bit_rate")
        else None,
    }
