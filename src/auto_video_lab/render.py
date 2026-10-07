from __future__ import annotations

import json
import math
import re
import shutil
from pathlib import Path

from .paths import JobPaths, ffmpeg_path
from .watermarks import source_watermark_filter
from .plan import clip_output_duration, plan_duration, validate_plan
from .util import read_json, run_command, sha256_file, write_json
from .presentation import sticker_events, plate_events, annotation_events, position_presented_overlays
from .text_colors import color_text

_FINE_CUT_FONT_ASSETS = (
    Path(__file__).resolve().parents[2]
    / ".codex"
    / "skills"
    / "tiktok-fine-cut-director"
    / "assets"
    / "fonts"
)


def _verified_fine_cut_font_files() -> list[Path]:
    if not _FINE_CUT_FONT_ASSETS.is_dir():
        raise FileNotFoundError(
            "Fine-cut font assets are missing; restore the project-local licensed font bundle"
        )
    manifest_path = _FINE_CUT_FONT_ASSETS / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("Fine-cut font manifest is missing")
    manifest = read_json(manifest_path)
    families = manifest.get("families")
    if not isinstance(families, list) or not families:
        raise ValueError("Fine-cut font manifest has no families")

    root = _FINE_CUT_FONT_ASSETS.resolve()
    font_files: list[Path] = []
    destination_names: set[str] = set()
    for family in families:
        if not isinstance(family, dict):
            raise ValueError("Fine-cut font manifest contains an invalid family entry")
        family_files = family.get("files")
        if not isinstance(family_files, list):
            raise ValueError("Fine-cut font manifest family has no file list")
        entries = [family.get("license_file"), *family_files]
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Fine-cut font manifest contains an invalid file entry")
            relative = entry.get("path")
            expected_hash = str(entry.get("sha256", "")).lower()
            expected_size = entry.get("size_bytes")
            if not isinstance(relative, str) or not relative:
                raise ValueError("Fine-cut font manifest file path is missing")
            source = (root / relative).resolve()
            try:
                source.relative_to(root)
            except ValueError as exc:
                raise ValueError("Fine-cut font manifest path escapes its asset directory") from exc
            if not source.is_file():
                raise FileNotFoundError(f"Fine-cut font asset is missing: {relative}")
            if not isinstance(expected_size, int) or source.stat().st_size != expected_size:
                raise ValueError(f"Fine-cut font asset size mismatch: {relative}")
            if len(expected_hash) != 64 or sha256_file(source) != expected_hash:
                raise ValueError(f"Fine-cut font asset SHA-256 mismatch: {relative}")
            if source.suffix.lower() == ".ttf":
                if source.name in destination_names:
                    raise ValueError(f"Fine-cut font filenames collide: {source.name}")
                destination_names.add(source.name)
                font_files.append(source)
    if not font_files:
        raise FileNotFoundError("Fine-cut font bundle contains no verified TrueType fonts")
    return font_files


def _prepare_fine_cut_fonts(job: JobPaths) -> Path:
    destination = job.work / "fonts"
    destination.mkdir(parents=True, exist_ok=True)
    verified_sources = _verified_fine_cut_font_files()
    expected_names = {source.name for source in verified_sources}
    unexpected_fonts = sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".ttf", ".otf"}
        and (path.parent != destination or path.name not in expected_names)
    )
    if unexpected_fonts:
        names = ", ".join(unexpected_fonts)
        raise ValueError(f"Fine-cut render font directory contains untracked files: {names}")
    for source in verified_sources:
        shutil.copy2(source, destination / source.name)
    return destination


def _number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _atempo_filters(speed: float) -> list[str]:
    values: list[float] = []
    remaining = speed
    while remaining > 2.0 + 1e-9:
        values.append(2.0)
        remaining /= 2.0
    while remaining < 0.5 - 1e-9:
        values.append(0.5)
        remaining /= 0.5
    if not math.isclose(remaining, 1.0, rel_tol=1e-9, abs_tol=1e-9):
        values.append(remaining)
    return [f"atempo={_number(value)}" for value in values]


def _audio_edge_fade_filters(duration: float, fade_ms: int) -> list[str]:
    if fade_ms <= 0:
        return []
    fade_seconds = min(fade_ms / 1000.0, duration / 4.0)
    fade_out_start = max(0.0, duration - fade_seconds)
    return [
        f"afade=t=in:st=0:d={_number(fade_seconds)}",
        f"afade=t=out:st={_number(fade_out_start)}:d={_number(fade_seconds)}",
    ]


def _final_audio_filter(total_duration: float, commerce_pop: bool) -> str:
    # Loudness is normalized only after the first render, using FFmpeg's
    # measured two-pass values. Keeping this pass to PTS/duration repair avoids
    # applying a blind one-pass gain before the exact normalization stage.
    _ = commerce_pop
    return (
        "aresample=48000,asetpts=N/SR/TB,apad,"
        f"atrim=duration={_number(total_duration)}"
    )


def _parse_loudnorm_json(stderr: str) -> dict[str, str] | None:
    matches = re.findall(r'\{\s*"input_i"\s*:.*?\}', stderr, flags=re.DOTALL)
    if not matches:
        return None
    try:
        value = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    required = {
        "input_i",
        "input_tp",
        "input_lra",
        "input_thresh",
        "target_offset",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        return None
    return {str(key): str(item) for key, item in value.items()}


def _finite_loudnorm_value(measurement: dict[str, str], key: str) -> float | None:
    try:
        value = float(measurement[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


_LOUDNESS_TOLERANCE_LU = 0.5
_TRUE_PEAK_TOLERANCE_DB = 0.2
_AAC_TRUE_PEAK_HEADROOM_DB = 0.35
_MAX_LOUDNESS_NORMALIZATION_ATTEMPTS = 2
_MAX_TRUE_PEAK_REPAIR_ATTEMPTS = 2
_TRUE_PEAK_REPAIR_HEADROOM_DB = 0.55


def _loudness_result_within_limits(
    measurement: dict[str, str] | None,
    *,
    target_lufs: float,
    true_peak_limit_db: float,
) -> tuple[bool, bool]:
    measured_i = _finite_loudnorm_value(measurement or {}, "input_i")
    measured_tp = _finite_loudnorm_value(measurement or {}, "input_tp")
    return (
        measured_i is not None
        and abs(measured_i - target_lufs) <= _LOUDNESS_TOLERANCE_LU,
        measured_tp is not None
        and measured_tp <= true_peak_limit_db + _TRUE_PEAK_TOLERANCE_DB,
    )


def _next_loudness_normalization_targets(
    *,
    target_lufs: float,
    true_peak_limit_db: float,
    measured_i: float,
    measured_tp: float,
    previous_normalization_peak_db: float,
) -> tuple[float, float]:
    # AAC encoding can shift integrated loudness slightly and create a small
    # true-peak overshoot after FFmpeg's loudnorm filter has already met its
    # internal target. Compensate only for the measured miss, and leave extra
    # peak headroom before the final lossy encode instead of relaxing QA.
    loudness_correction = max(-0.75, min(0.75, target_lufs - measured_i))
    corrected_target = max(-69.0, min(-5.0, target_lufs + loudness_correction))
    peak_overshoot = max(0.0, measured_tp - true_peak_limit_db)
    corrected_peak = min(
        true_peak_limit_db - _AAC_TRUE_PEAK_HEADROOM_DB,
        previous_normalization_peak_db - max(0.2, peak_overshoot + 0.15),
    )
    return corrected_target, corrected_peak


def _true_peak_repair_filter(
    *,
    target_lufs: float,
    measured_i: float,
    true_peak_limit_db: float,
    attempt_number: int,
) -> tuple[str, float, float]:
    """Build a bounded final limiter pass after loudnorm misses AAC true peak.

    FFmpeg's loudnorm target is evaluated before the final lossy AAC encode.
    Sparse inter-sample peaks can therefore reappear even when integrated
    loudness is already correct.  A look-ahead limiter with automatic makeup
    disabled constrains those peaks without turning a recoverable delivery
    into a failed edit.  A second pass adds only a small amount of headroom.
    """

    bounded_attempt = max(1, min(_MAX_TRUE_PEAK_REPAIR_ATTEMPTS, attempt_number))
    gain_db = max(-2.0, min(2.0, target_lufs - measured_i))
    ceiling_db = (
        true_peak_limit_db
        - _TRUE_PEAK_REPAIR_HEADROOM_DB
        - 0.2 * (bounded_attempt - 1)
    )
    ceiling_linear = 10 ** (ceiling_db / 20.0)
    filter_chain = (
        f"volume={_number(gain_db)}dB,"
        f"alimiter=limit={_number(ceiling_linear)}:attack=5:release=50:"
        "level=false:latency=true,aresample=48000"
    )
    return filter_chain, gain_db, ceiling_db


def _measure_loudness(
    output_path: Path,
    *,
    target_lufs: float,
    true_peak_limit_db: float,
) -> tuple[dict[str, str] | None, list[str]]:
    process = run_command(
        [
            str(ffmpeg_path()),
            "-hide_banner",
            "-nostats",
            "-i",
            str(output_path),
            "-vn",
            "-af",
            (
                f"loudnorm=I={_number(target_lufs)}:LRA=7:"
                f"TP={_number(true_peak_limit_db)}:print_format=json"
            ),
            "-f",
            "null",
            "-",
        ],
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError("FFmpeg loudness measurement failed:\n" + process.stderr[-4000:])
    return _parse_loudnorm_json(process.stderr), process.stderr.splitlines()[-20:]


def _normalize_output_audio(
    job: JobPaths,
    output_path: Path,
    plan: dict,
) -> dict:
    finishing = plan.get("finishing", {})
    target = finishing.get("loudness_target_lufs")
    peak_limit = float(finishing.get("true_peak_limit_db", -1.5))
    if target is None:
        report = {
            "status": "SKIPPED",
            "reason": "loudness_target_lufs is null",
            "target_lufs": None,
            "true_peak_limit_db": peak_limit,
        }
        write_json(job.reports / "loudness-normalization.json", report)
        return report

    target_lufs = float(target)
    normalization_target = target_lufs
    normalization_peak = peak_limit - _AAC_TRUE_PEAK_HEADROOM_DB
    source_path = output_path
    attempts: list[dict] = []
    first_measurement: dict[str, str] | None = None

    for attempt_number in range(1, _MAX_LOUDNESS_NORMALIZATION_ATTEMPTS + 1):
        before, measurement_tail = _measure_loudness(
            source_path,
            target_lufs=normalization_target,
            true_peak_limit_db=normalization_peak,
        )
        if attempt_number == 1:
            first_measurement = before
        if before is None or _finite_loudnorm_value(before, "input_i") is None:
            report = {
                "status": "SKIPPED_SILENCE",
                "reason": "the rendered audio has no finite integrated loudness",
                "target_lufs": target_lufs,
                "true_peak_limit_db": peak_limit,
                "measurement_stderr_tail": measurement_tail,
            }
            write_json(job.reports / "loudness-normalization.json", report)
            return report

        measured_i = _finite_loudnorm_value(before, "input_i")
        measured_lra = _finite_loudnorm_value(before, "input_lra")
        measured_tp = _finite_loudnorm_value(before, "input_tp")
        measured_thresh = _finite_loudnorm_value(before, "input_thresh")
        offset = _finite_loudnorm_value(before, "target_offset")
        if None in {measured_i, measured_lra, measured_tp, measured_thresh, offset}:
            raise RuntimeError("FFmpeg returned incomplete finite loudness measurements")

        normalized_path = job.work / (
            f"{output_path.stem}.loudness-attempt-{attempt_number}.mp4"
        )
        normalized_path.unlink(missing_ok=True)
        loudnorm_filter = (
            f"loudnorm=I={_number(normalization_target)}:LRA=7:"
            f"TP={_number(normalization_peak)}:"
            f"measured_I={_number(measured_i)}:measured_LRA={_number(measured_lra)}:"
            f"measured_TP={_number(measured_tp)}:measured_thresh={_number(measured_thresh)}:"
            f"offset={_number(offset)}:linear=true:print_format=json,aresample=48000"
        )
        normalized = run_command(
            [
                str(ffmpeg_path()),
                "-hide_banner",
                "-y",
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-c:v",
                "copy",
                "-af",
                loudnorm_filter,
                "-c:a",
                "aac",
                "-b:a",
                str(plan.get("output", {}).get("audio_bitrate", "192k")),
                "-movflags",
                "+faststart",
                str(normalized_path),
            ],
            cwd=job.root,
        )
        if not normalized_path.is_file() or normalized_path.stat().st_size <= 0:
            raise RuntimeError("Two-pass loudness normalization produced no output")

        after, final_measurement_tail = _measure_loudness(
            normalized_path,
            target_lufs=target_lufs,
            true_peak_limit_db=peak_limit,
        )
        within_target, within_peak = _loudness_result_within_limits(
            after,
            target_lufs=target_lufs,
            true_peak_limit_db=peak_limit,
        )
        attempts.append(
            {
                "phase": "loudnorm",
                "attempt": attempt_number,
                "normalization_target_lufs": normalization_target,
                "normalization_true_peak_db": normalization_peak,
                "before": before,
                "after": after,
                "within_target_half_lu": within_target,
                "within_true_peak_tolerance": within_peak,
                "normalization_stderr_tail": normalized.stderr.splitlines()[-20:],
                "measurement_stderr_tail": final_measurement_tail,
            }
        )
        if within_target and within_peak:
            normalized_path.replace(output_path)
            report = {
                "status": "PASS",
                "target_lufs": target_lufs,
                "true_peak_limit_db": peak_limit,
                "before": first_measurement,
                "after": after,
                "within_target_half_lu": True,
                "within_true_peak_tolerance": True,
                "attempt_count": attempt_number,
                "attempts": attempts,
            }
            write_json(job.reports / "loudness-normalization.json", report)
            return report

        after_i = _finite_loudnorm_value(after or {}, "input_i")
        after_tp = _finite_loudnorm_value(after or {}, "input_tp")
        if after_i is None or after_tp is None:
            break
        source_path = normalized_path
        normalization_target, normalization_peak = _next_loudness_normalization_targets(
            target_lufs=target_lufs,
            true_peak_limit_db=peak_limit,
            measured_i=after_i,
            measured_tp=after_tp,
            previous_normalization_peak_db=normalization_peak,
        )

    final_after = attempts[-1].get("after") if attempts else None
    final_i = _finite_loudnorm_value(final_after or {}, "input_i")
    final_tp = _finite_loudnorm_value(final_after or {}, "input_tp")
    if final_i is not None and final_tp is not None:
        repair_source = source_path
        for repair_number in range(1, _MAX_TRUE_PEAK_REPAIR_ATTEMPTS + 1):
            repair_filter, gain_db, ceiling_db = _true_peak_repair_filter(
                target_lufs=target_lufs,
                measured_i=final_i,
                true_peak_limit_db=peak_limit,
                attempt_number=repair_number,
            )
            repaired_path = job.work / (
                f"{output_path.stem}.true-peak-repair-{repair_number}.mp4"
            )
            repaired_path.unlink(missing_ok=True)
            repaired = run_command(
                [
                    str(ffmpeg_path()),
                    "-hide_banner",
                    "-y",
                    "-i",
                    str(repair_source),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-c:v",
                    "copy",
                    "-af",
                    repair_filter,
                    "-c:a",
                    "aac",
                    "-b:a",
                    str(plan.get("output", {}).get("audio_bitrate", "192k")),
                    "-movflags",
                    "+faststart",
                    str(repaired_path),
                ],
                cwd=job.root,
            )
            if not repaired_path.is_file() or repaired_path.stat().st_size <= 0:
                raise RuntimeError("True-peak repair produced no output")

            repaired_after, repaired_measurement_tail = _measure_loudness(
                repaired_path,
                target_lufs=target_lufs,
                true_peak_limit_db=peak_limit,
            )
            within_target, within_peak = _loudness_result_within_limits(
                repaired_after,
                target_lufs=target_lufs,
                true_peak_limit_db=peak_limit,
            )
            attempts.append(
                {
                    "phase": "true_peak_repair",
                    "attempt": repair_number,
                    "gain_db": gain_db,
                    "limiter_ceiling_db": ceiling_db,
                    "before": final_after,
                    "after": repaired_after,
                    "within_target_half_lu": within_target,
                    "within_true_peak_tolerance": within_peak,
                    "normalization_stderr_tail": repaired.stderr.splitlines()[-20:],
                    "measurement_stderr_tail": repaired_measurement_tail,
                }
            )
            if within_target and within_peak:
                repaired_path.replace(output_path)
                report = {
                    "status": "PASS",
                    "target_lufs": target_lufs,
                    "true_peak_limit_db": peak_limit,
                    "before": first_measurement,
                    "after": repaired_after,
                    "within_target_half_lu": True,
                    "within_true_peak_tolerance": True,
                    "attempt_count": len(attempts),
                    "repair_method": "bounded_true_peak_limiter",
                    "attempts": attempts,
                }
                write_json(job.reports / "loudness-normalization.json", report)
                return report

            repaired_i = _finite_loudnorm_value(repaired_after or {}, "input_i")
            repaired_tp = _finite_loudnorm_value(repaired_after or {}, "input_tp")
            if repaired_i is None or repaired_tp is None:
                break
            repair_source = repaired_path
            final_after = repaired_after
            final_i = repaired_i
            final_tp = repaired_tp

    report = {
        "status": "FAIL",
        "target_lufs": target_lufs,
        "true_peak_limit_db": peak_limit,
        "before": first_measurement,
        "after": final_after,
        "within_target_half_lu": bool(attempts and attempts[-1]["within_target_half_lu"]),
        "within_true_peak_tolerance": bool(
            attempts and attempts[-1]["within_true_peak_tolerance"]
        ),
        "attempt_count": len(attempts),
        "attempts": attempts,
    }
    write_json(job.reports / "loudness-normalization.json", report)
    raise RuntimeError(
        "Measured loudness normalization still missed its limits after "
        f"{len(attempts)} attempts (I={final_i}, TP={final_tp}, "
        f"target={target_lufs}, peak_limit={peak_limit})"
    )


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{centis:02}"


def _wrap_text(text: str, *, width: int = 18) -> str:
    """Wrap readable text while preserving editor-selected semantic line breaks."""
    paragraphs = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for paragraph in paragraphs:
        normalized = " ".join(paragraph.split())
        if not normalized:
            continue
        if " " not in normalized:
            lines.extend(
                normalized[index : index + width]
                for index in range(0, len(normalized), width)
            )
            continue
        current = ""
        for word in normalized.split(" "):
            candidate = word if not current else f"{current} {word}"
            if len(candidate) <= width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
    return "\\N".join(lines)


def _overlay_wrap_width(overlay: dict) -> int:
    text = str(overlay.get("text", ""))
    contains_cjk = re.search(r"[\u3400-\u9fff\uf900-\ufaff]", text) is not None
    preset = str(overlay.get("preset", "default"))
    if preset in {"fine_hook", "hook", "fine_step_action"}:
        return 10 if contains_cjk else 18
    if overlay.get("kind") == "caption":
        return 14 if contains_cjk else 24
    return 14 if contains_cjk else 22


def _ass_escape(text: str, *, width: int = 18) -> str:
    return _wrap_text(
        text.replace("\\", "／").replace("{", "（").replace("}", "）"),
        width=width,
    )


def _highlight_motion_tags(
    highlight: dict,
    *,
    overlay_start: float,
) -> tuple[str, str]:
    """Return renderer-owned inline ASS tags for one timed spoken emphasis."""
    motion = str(highlight.get("motion", "none"))
    if motion == "none":
        return "", ""
    start_ms = max(0, round((float(highlight["start"]) - overlay_start) * 1000))
    end_ms = max(start_ms + 1, round((float(highlight["end"]) - overlay_start) * 1000))
    duration_ms = end_ms - start_ms
    if motion == "pulse":
        # One compact editorial spring on the spoken word. The old 138% peak
        # read as a generic template pop; keep the hit obvious while adding a
        # small rebound before returning to the caption scale.
        rise_end = min(end_ms, start_ms + max(55, round(duration_ms * 0.28)))
        rebound_end = min(end_ms, rise_end + max(45, round(duration_ms * 0.24)))
        echo_end = min(end_ms, rebound_end + max(35, round(duration_ms * 0.20)))
        before = (
            r"\fscx100\fscy100"
            + rf"\t({start_ms},{rise_end},\fscx126\fscy126)"
            + rf"\t({rise_end},{rebound_end},\fscx104\fscy104)"
            + rf"\t({rebound_end},{echo_end},\fscx111\fscy111)"
            + rf"\t({echo_end},{end_ms},\fscx100\fscy100)"
        )
        return before, r"\fscx100\fscy100\frz0"
    if motion == "shake":
        # Shake is one interruption, never a perpetual wobble. Pair a short
        # scale hit with diminishing rotations so the peak reads on a phone
        # without destabilising the rest of the subtitle.
        beat = max(24, min(54, duration_ms // 6))
        keyframes = [
            (start_ms, min(end_ms, start_ms + beat), -7),
            (min(end_ms, start_ms + beat), min(end_ms, start_ms + beat * 2), 6),
            (min(end_ms, start_ms + beat * 2), min(end_ms, start_ms + beat * 3), -4),
            (min(end_ms, start_ms + beat * 3), min(end_ms, start_ms + beat * 4), 2),
            (min(end_ms, start_ms + beat * 4), min(end_ms, start_ms + beat * 5), -1),
            (min(end_ms, start_ms + beat * 5), end_ms, 0),
        ]
        transforms = "".join(
            rf"\t({first},{last},\frz{angle})"
            for first, last, angle in keyframes
            if last > first
        )
        return (
            r"\fscx100\fscy100\frz0"
            + rf"\t({start_ms},{min(end_ms, start_ms + beat)},\fscx120\fscy120)"
            + rf"\t({min(end_ms, start_ms + beat)},{min(end_ms, start_ms + beat * 3)},\fscx104\fscy104)"
            + rf"\t({min(end_ms, start_ms + beat * 3)},{end_ms},\fscx100\fscy100)"
            + transforms,
            r"\fscx100\fscy100\frz0",
        )
    return "", ""


def _apply_ass_highlights(
    text: str,
    highlights: list[dict],
    reset_rgb: str,
    *,
    overlay_start: float,
) -> str:
    """Apply one validated phrase-level color/motion run without raw user markup."""
    rendered = text
    for highlight in highlights:
        phrase = str(highlight.get("text", "")).strip()
        color = str(highlight.get("color", ""))
        if not phrase or not color:
            continue
        safe_phrase = phrase.replace("\\", "／").replace("{", "（").replace("}", "）")
        words = safe_phrase.split()
        if not words:
            continue
        pattern = r"(?:\s|\\N)+".join(re.escape(word) for word in words)
        match = re.search(pattern, rendered, flags=re.IGNORECASE)
        if match is None:
            continue
        motion_before, motion_after = _highlight_motion_tags(
            highlight,
            overlay_start=overlay_start,
        )
        # Inline ASS scale changes do not reflow neighboring glyphs. Reserve
        # one hard-space on each side only for a kinetic phrase so its peak
        # stays legible instead of colliding with adjacent caption words.
        motion_padding = r"\h" if motion_before else ""
        emphasized = (
            rf"{{\1c{_ass_bgr_color(color)}{motion_before}}}"
            + motion_padding
            + match.group(0)
            + motion_padding
            + rf"{{\1c{_ass_bgr_color(reset_rgb)}{motion_after}}}"
        )
        rendered = rendered[: match.start()] + emphasized + rendered[match.end() :]
    return rendered


def _ass_bgr_color(rgb: str) -> str:
    """Convert validated #RRGGBB input to an ASS &HBBGGRR& color."""
    red, green, blue = rgb[1:3], rgb[3:5], rgb[5:7]
    return f"&H{blue}{green}{red}&".upper()


def _ass_overlay_position(
    overlay: dict, width: int, height: int
) -> tuple[float, float]:
    """Resolve paired overlay coordinates to ASS PlayRes pixels.

    Planner-facing plans use normalized coordinates for portable placement. Keep
    legacy absolute-pixel plans working by converting only when *both* values
    are in the inclusive 0..1 range; a mixed or larger pair remains absolute.
    """
    x = float(overlay["x"])
    y = float(overlay["y"])
    if 0 <= x <= 1 and 0 <= y <= 1:
        return x * width, y * height
    return x, y


def write_ass(overlays: list[dict], path: Path, width: int, height: int, presentation: dict | None = None) -> None:
    scale = height / 1920
    title_size = max(28, round(68 * scale))
    caption_size = max(24, round(54 * scale))
    label_size = max(20, round(40 * scale))
    # Reserve vertical UI space for short-form platforms. The defaults keep
    # titles below the account header and captions above commerce controls.
    title_margin = max(40, round(160 * scale))
    caption_margin = max(60, round(280 * scale))
    outline = max(2, round(4 * scale))
    style_format = (
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding"
    )
    title_style = (
        f"Style: Title,Microsoft YaHei,{title_size},&H00FFFFFF,&H000000FF,&H00101010,"
        f"&H78000000,-1,0,0,0,100,100,0,0,1,{outline},1,8,60,60,{title_margin},1"
    )
    caption_style = (
        f"Style: Caption,Microsoft YaHei,{caption_size},&H00FFFFFF,&H000000FF,"
        f"&H00101010,&H78000000,-1,0,0,0,100,100,0,0,1,{outline},1,2,55,55,"
        f"{caption_margin},1"
    )
    label_style = (
        f"Style: Label,Microsoft YaHei,{label_size},&H00FFFFFF,&H000000FF,&H00101010,"
        "&H78000000,-1,0,0,0,100,100,0,0,3,1,0,7,70,70,170,1"
    )
    hook_style = (
        f"Style: Hook,Arial Black,{max(36, round(82 * scale))},&H00FFFFFF,&H000000FF,"
        f"&H00101010,&H00000000,-1,0,0,0,100,100,0,0,1,{max(3, round(6 * scale))},"
        f"1,8,65,65,{max(50, round(185 * scale))},1"
    )
    feature_style = (
        f"Style: Feature,Arial,{max(32, round(68 * scale))},&H00FFFFFF,&H000000FF,"
        f"&H00101010,&HCC101010,-1,0,0,0,100,100,0,0,3,{max(8, round(18 * scale))},"
        "0,7,70,70,170,1"
    )
    badge_style = (
        f"Style: Badge,Arial Black,{max(32, round(68 * scale))},&H00101010,&H000000FF,"
        f"&H004DD8FF,&H004DD8FF,-1,0,0,0,100,100,0,0,3,{max(7, round(14 * scale))},"
        "0,7,70,70,170,1"
    )
    cta_style = (
        f"Style: CTA,Arial Black,{max(32, round(62 * scale))},&H00101010,&H000000FF,"
        f"&H00EEF425,&H00EEF425,-1,0,0,0,100,100,0,0,3,{max(10, round(22 * scale))},"
        f"0,2,80,80,{max(70, round(315 * scale))},1"
    )
    micro_style = (
        f"Style: Micro,Arial,{max(20, round(34 * scale))},&H00FFFFFF,&H000000FF,"
        f"&H00101010,&H00000000,-1,0,0,0,100,100,1,0,1,{outline},0,8,70,70,"
        f"{max(40, round(125 * scale))},1"
    )
    fine_caption_style = (
        f"Style: FineCaption,Montserrat ExtraBold,{max(28, round(62 * scale))},&H00FFFFFF," 
        f"&H000000FF,&H00101010,&H00000000,-1,0,0,0,100,100,0,0,1,"
        f"{max(2, round(3 * scale))},2,2,70,70,{max(60, round(300 * scale))},1"
    )
    fine_hook_style = (
        f"Style: FineHook,Anton,{max(42, round(104 * scale))},&H00FFFFFF,&H000000FF,"
        f"&H00101010,&H00000000,0,0,0,0,100,100,0,0,1,{max(4, round(7 * scale))},"
        f"1,8,70,70,{max(50, round(190 * scale))},1"
    )
    fine_accent_style = (
        f"Style: FineAccent,DM Serif Display,{max(34, round(84 * scale))},&H004DD8FF,"
        f"&H000000FF,&H00101010,&H00000000,0,-1,0,0,100,100,0,0,1,"
        f"{max(2, round(4 * scale))},1,5,70,70,170,1"
    )
    fine_cta_style = (
        f"Style: FineCTA,Anton,{max(34, round(72 * scale))},&H00101010,&H000000FF,"
        f"&H004DD8FF,&H004DD8FF,0,0,0,0,100,100,0,0,3,{max(10, round(22 * scale))},"
        f"0,2,85,85,{max(70, round(315 * scale))},1"
    )
    fine_micro_style = (
        f"Style: FineMicro,Montserrat SemiBold,{max(20, round(32 * scale))},&H00101010,"
        f"&H000000FF,&H20C1E655,&H20C1E655,0,0,0,0,100,100,{max(1, round(2 * scale))},"
        f"0,3,{max(8, round(14 * scale))},0,7,70,70,{max(40, round(150 * scale))},1"
    )
    fine_reaction_style = (
        f"Style: FineReaction,Segoe UI Emoji,{max(46, round(118 * scale))},&H00FFFFFF,"
        f"&H000000FF,&H00101010,&H00000000,-1,0,0,0,100,100,0,0,1,"
        f"{max(3, round(6 * scale))},2,5,70,70,{max(40, round(150 * scale))},1"
    )
    fine_step_number_style = (
        f"Style: FineStepNumber,Montserrat ExtraBold,{max(24, round(44 * scale))},&H00101010,"
        f"&H000000FF,&H00C2E068,&H00C2E068,-1,0,0,0,100,100,0,0,3,"
        f"{max(8, round(15 * scale))},0,7,70,70,{max(40, round(150 * scale))},1"
    )
    fine_step_action_style = (
        f"Style: FineStepAction,Anton,{max(30, round(68 * scale))},&H00FFFFFF,&H000000FF,"
        f"&H00101010,&H00000000,0,0,0,0,100,100,0,0,1,"
        f"{max(3, round(5 * scale))},1,7,70,70,{max(40, round(150 * scale))},1"
    )
    pip_arrow_style = (
        f"Style: PipArrow,Segoe UI Symbol,{max(46, round(112 * scale))},&H004DD8FF,"
        f"&H000000FF,&H00101010,&H00000000,-1,0,0,0,100,100,0,0,1,"
        f"{max(4, round(7 * scale))},2,5,20,20,20,1"
    )
    header = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "ScaledBorderAndShadow: yes",
            "WrapStyle: 0",
            "",
            "[V4+ Styles]",
            style_format,
            title_style,
            caption_style,
            label_style,
            hook_style,
            feature_style,
            badge_style,
            cta_style,
            micro_style,
            fine_caption_style,
            fine_hook_style,
            fine_accent_style,
            fine_cta_style,
            fine_micro_style,
            fine_reaction_style,
            fine_step_number_style,
            fine_step_action_style,
            pip_arrow_style,
            "",
            "[Events]",
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
            "",
        ]
    )
    default_styles = {"title": "Title", "caption": "Caption", "label": "Label"}
    preset_styles = {
        "hook": "Hook",
        "feature": "Feature",
        "badge": "Badge",
        "cta": "CTA",
        "micro": "Micro",
        "fine_caption": "FineCaption",
        "fine_hook": "FineHook",
        "fine_accent": "FineAccent",
        "fine_cta": "FineCTA",
        "fine_micro": "FineMicro",
        "fine_reaction": "FineReaction",
        "fine_step_number": "FineStepNumber",
        "fine_step_action": "FineStepAction",
        "pip_arrow": "PipArrow",
    }
    preset_primary_rgb = {
        "badge": "#101010",
        "cta": "#101010",
        "fine_accent": "#FFD84D",
        "fine_cta": "#101010",
        "fine_micro": "#101010",
        "fine_step_number": "#101010",
    }
    overlays = position_presented_overlays([{**o,'_display_lines':_ass_escape(str(o.get('text','')),width=_overlay_wrap_width(o)).split(r'\N')} for o in overlays],width,height)
    events = []
    for a in annotation_events(presentation or {},width,height):
        events.append('Dialogue: {layer},{start},{end},FineCaption,,0,0,0,,{text}'.format(layer=a['layer'],start=_ass_time(a['start']),end=_ass_time(a['end']),text=a['drawing']))
    for plate in plate_events(overlays, width, height):
        events.append('Dialogue: {layer},{start},{end},FineCaption,,0,0,0,,{text}'.format(
            layer=plate['layer'], start=_ass_time(float(plate['start'])), end=_ass_time(float(plate['end'])), text=plate['drawing']))
    for overlay in overlays:
        preset = overlay.get("preset", "default")
        raw_overlay_text = str(overlay["text"]).strip()
        style = preset_styles.get(preset, default_styles[overlay["kind"]])
        tags: list[str] = []
        if overlay.get('_presentation') and preset != 'fine_reaction':
            # v2 plates are renderer-owned. Remove legacy boxed/shadowed presets
            # so a caption cannot acquire two competing background rectangles.
            style='FineCaption'
            face='DM Serif Display' if preset=='fine_accent' else 'Anton' if overlay['kind']=='title' else 'Montserrat SemiBold' if overlay['kind']=='label' else 'Montserrat ExtraBold'
            tags.extend([rf'\fn{face}',r'\q2',rf'\bord{_number(max(.7,min(width,height)*.0017))}',r'\shad0',r'\b0',r'\i1' if preset=='fine_accent' else r'\i0'])
        if "color" in overlay:
            primary_color = overlay['color']
            # v2 removes the old badge's filled rectangle. Its old black ink
            # must not survive on the now-transparent impact underline.
            if overlay.get('_presentation') and preset == 'badge' and primary_color.upper() == '#101010':
                primary_color = '#FFFFFF'
            tags.append(rf"\1c{_ass_bgr_color(primary_color)}")
        if "outline_color" in overlay:
            tags.append(rf"\3c{_ass_bgr_color(overlay['outline_color'])}")
        if "font_size" in overlay:
            tags.append(rf"\fs{_number(float(overlay['font_size']))}")
        if "tracking" in overlay:
            tags.append(rf"\fsp{_number(float(overlay['tracking']))}")
        if "rotation" in overlay:
            tags.append(rf"\frz{_number(float(overlay['rotation']))}")
        if "outline_width" in overlay:
            tags.append(rf"\bord{_number(float(overlay['outline_width']))}")
        if "shadow" in overlay:
            tags.append(rf"\shad{_number(float(overlay['shadow']))}")
        animation = overlay.get("animation", "none")
        duration_ms = max(1, round((float(overlay["end"]) - float(overlay["start"])) * 1000))
        if animation in {
            "fade",
            "pop",
            "punch",
            "bounce",
            "slide_left",
            "slide_right",
            "slide_up",
            "drop",
            "tag",
        }:
            tags.append(r"\fad(70,110)")
        elif animation == "cta_hold":
            tags.append(r"\fad(80,0)")
        if animation == "pop":
            tags.extend([r"\fscx118\fscy118", r"\t(0,150,\fscx100\fscy100)"])
        elif animation == "punch":
            tags.extend(
                [
                    r"\fscx62\fscy62",
                    r"\t(0,85,\fscx126\fscy126)",
                    r"\t(85,175,\fscx100\fscy100)",
                ]
            )
        elif animation == "bounce":
            tags.extend(
                [
                    r"\fscx78\fscy78",
                    r"\t(0,90,\fscx112\fscy112)",
                    r"\t(90,175,\fscx96\fscy96)",
                    r"\t(175,245,\fscx100\fscy100)",
                ]
            )
        elif animation == "cta_hold":
            tags.extend(
                [
                    r"\fscx92\fscy92",
                    r"\t(0,100,\fscx104\fscy104)",
                    r"\t(100,190,\fscx100\fscy100)",
                ]
            )
        if animation in {"punch", "bounce"}:
            exit_start = max(0, duration_ms - 110)
            tags.append(rf"\t({exit_start},{duration_ms},\fscx92\fscy92)")
        if preset == "fine_reaction" and animation in {"pop", "punch", "bounce", "tag"}:
            # A reaction mark should feel placed by an editor, not printed like
            # a second subtitle. Keep the movement short and glyph-specific so
            # it follows the reaction beat without wobbling for its full stay.
            initial_angle = -10 if raw_overlay_text in {"?!", "?", "😳", "☹", ":("} else 8
            settle_angle = 3 if initial_angle < 0 else -2
            settle_ms = min(duration_ms, 190)
            finish_ms = min(duration_ms, 310)
            tags.extend(
                [
                    rf"\frz{initial_angle}",
                    rf"\t(0,{settle_ms},\frz{settle_angle})",
                    rf"\t({settle_ms},{finish_ms},\frz0)",
                ]
            )
        if "x" in overlay:
            x_position, y_position = _ass_overlay_position(overlay, width, height)
            x = _number(x_position)
            y = _number(y_position)
            align = int(overlay.get("align", 5))
            tags.append(rf"\an{align}")
            if animation == "slide_left":
                tags.append(rf"\move({_number(x_position - 90)},{y},{x},{y},0,170)")
            elif animation == "slide_right":
                tags.append(rf"\move({_number(x_position + 90)},{y},{x},{y},0,170)")
            elif animation == "slide_up":
                tags.append(rf"\move({x},{_number(y_position + 55)},{x},{y},0,160)")
            elif animation == "drop":
                tags.append(rf"\move({x},{_number(y_position - 70)},{x},{y},0,160)")
            elif animation == "tag":
                tags.append(rf"\move({_number(x_position - 70)},{y},{x},{y},0,180)")
            else:
                tags.append(rf"\pos({x},{y})")
        text = _ass_escape(
            raw_overlay_text,
            width=_overlay_wrap_width(overlay),
        )
        highlights = overlay.get("highlights", [])
        if isinstance(highlights, list) and highlights:
            reset_rgb = str(
                overlay.get(
                    "color",
                    preset_primary_rgb.get(preset, "#FFFFFF"),
                )
            )
            text = _apply_ass_highlights(
                text,
                highlights,
                reset_rgb,
                overlay_start=float(overlay["start"]),
            )
        if tags:
            if (presentation or {}).get('typography') and preset != 'fine_reaction':
                tags.extend([r'\3c&H151015&', rf'\bord{_number(max(1.5,min(width,height)*.003))}', r'\shad0'])
        busy_text = any(
            other is not overlay and float(other['start']) < float(overlay['end']) and float(other['end']) > float(overlay['start'])
            and (other.get('animation','none') not in {'none','fade'} or any(h.get('motion','none') != 'none' for h in other.get('highlights',[])))
            for other in overlays
        ) if (presentation or {}).get('typography') else False
        text = color_text(text, {**overlay,'_color_static':busy_text}, (presentation or {}).get('typography'))
        if tags:
            text = "{" + "".join(tags) + "}" + text
        events.append(
            "Dialogue: {layer},{start},{end},{style},,0,0,0,,{text}".format(
                layer=int(overlay.get("layer", 0)),
                start=_ass_time(float(overlay["start"])),
                end=_ass_time(float(overlay["end"])),
                style=style,
                text=text,
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def _video_scale_filter(width: int, height: int, fit: str) -> str:
    if fit == "contain":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"


def _bottom_crop_cleanup_filter(width: int, height: int, bottom_crop_pixels: int) -> str:
    if bottom_crop_pixels <= 0:
        return ""
    cropped_height = height - bottom_crop_pixels
    scaled_width = math.ceil((width * height / cropped_height) / 2) * 2
    horizontal_crop = (scaled_width - width) // 2
    return (
        f"crop={width}:{cropped_height}:0:0,"
        f"scale={scaled_width}:{height}:flags=lanczos,"
        f"crop={width}:{height}:{horizontal_crop}:0,setsar=1"
    )


def _motion_filter(clip: dict, width: int, height: int, fps: float, duration: float) -> str:
    motion = clip.get("motion")
    if not motion:
        return ""
    frame_count = max(2, round(duration * fps))
    denominator = frame_count - 1
    zoom_start = _number(float(motion["zoom_start"]))
    zoom_delta = _number(float(motion["zoom_end"]) - float(motion["zoom_start"]))
    focus_x_start = _number(float(motion["focus_x_start"]))
    focus_x_delta = _number(float(motion["focus_x_end"]) - float(motion["focus_x_start"]))
    focus_y_start = _number(float(motion["focus_y_start"]))
    focus_y_delta = _number(float(motion["focus_y_end"]) - float(motion["focus_y_start"]))
    progress = f"min(on/{denominator},1)"
    zoom = f"{zoom_start}+({zoom_delta})*{progress}"
    focus_x = f"{focus_x_start}+({focus_x_delta})*{progress}"
    focus_y = f"{focus_y_start}+({focus_y_delta})*{progress}"
    return (
        f",zoompan=z='{zoom}':x='(iw-iw/zoom)*({focus_x})':"
        f"y='(ih-ih/zoom)*({focus_y})':d=1:s={width}x{height}:fps={_number(fps)}"
    )


def build_render_command(job: JobPaths, plan: dict, prepared_sources: dict | None = None) -> tuple[list[str], Path, str]:
    prepared_sources = prepared_sources or {}
    output = plan["output"]
    width = int(output["width"])
    height = int(output["height"])
    fps = float(output["fps"])
    total_duration = plan_duration(plan)
    args = [str(ffmpeg_path()), "-hide_banner", "-y"]

    for clip in plan["clips"]:
        source = prepared_sources.get(clip["source"], job.root / clip["source"])
        if clip["kind"] == "image":
            args.extend(
                [
                    "-loop",
                    "1",
                    "-framerate",
                    _number(fps),
                    "-t",
                    _number(float(clip["duration"])),
                    "-i",
                    str(source),
                ]
            )
        else:
            args.extend(["-i", str(source)])

    picture_in_picture = plan.get("picture_in_picture", [])
    picture_in_picture_input_indexes: list[int] = []
    for inset in picture_in_picture:
        input_index = len(plan["clips"]) + len(picture_in_picture_input_indexes)
        picture_in_picture_input_indexes.append(input_index)
        source = prepared_sources.get(inset["source"], job.root / inset["source"])
        if inset["kind"] == "image":
            args.extend(
                [
                    "-loop",
                    "1",
                    "-framerate",
                    _number(fps),
                    "-t",
                    _number(float(inset["end"]) - float(inset["start"])),
                    "-i",
                    str(source),
                ]
            )
        else:
            args.extend(["-i", str(source)])

    music_input_index: int | None = None
    if plan.get("music"):
        music_input_index = len(plan["clips"]) + len(picture_in_picture)
        args.extend(
            [
                "-stream_loop",
                "-1",
                "-i",
                str(job.root / plan["music"]["source"]),
            ]
        )

    voice_input_index: int | None = None
    if plan.get("voiceover"):
        voice_input_index = (
            len(plan["clips"])
            + len(picture_in_picture)
            + (1 if music_input_index is not None else 0)
        )
        args.extend(["-i", str(job.root / plan["voiceover"]["source"])])

    sfx_input_indexes: list[int] = []
    for effect in plan.get("sfx", []):
        input_index = (
            len(plan["clips"])
            + len(picture_in_picture)
            + (1 if music_input_index is not None else 0)
        )
        input_index += 1 if voice_input_index is not None else 0
        input_index += len(sfx_input_indexes)
        sfx_input_indexes.append(input_index)
        args.extend(["-i", str(job.root / effect["source"])])

    stickers = sticker_events(plan)
    sticker_inputs = []
    next_input = len(plan['clips']) + len(picture_in_picture) + (music_input_index is not None) + (voice_input_index is not None) + len(sfx_input_indexes)
    for i, (_, file) in enumerate(stickers):
        sticker_inputs.append(next_input+i)
        args.extend(['-loop','1','-framerate',_number(fps),'-t',_number(total_duration),'-i',str(file)])

    filters: list[str] = []
    concat_inputs: list[str] = []
    clip_durations: list[float] = []
    watermark_probe_cache: dict = {}
    for index, clip in enumerate(plan["clips"]):
        duration = clip_output_duration(clip)
        clip_durations.append(duration)
        scale_filter = _video_scale_filter(width, height, clip["fit"])
        motion_filter = _motion_filter(clip, width, height, fps, duration)
        if clip["kind"] == "image":
            video_chain = (
                f"[{index}:v]trim=duration={_number(duration)},setpts=PTS-STARTPTS,"
                f"fps={_number(fps)},{scale_filter}{motion_filter},"
                f"trim=duration={_number(duration)},"
                f"settb=AVTB,setpts=N/({_number(fps)}*TB),setsar=1,"
                f"format=yuv420p[v{index}]"
            )
        else:
            watermark_filter = source_watermark_filter(job, plan, clip["source"], float(clip["start"]), float(clip["end"]), watermark_probe_cache)
            video_chain = (
                f"[{index}:v]trim=start={_number(float(clip['start']))}:"
                f"end={_number(float(clip['end']))},"
                f"{watermark_filter}"
                f"setpts=(PTS-STARTPTS)/{_number(float(clip['speed']))},"
                f"fps={_number(fps)},{scale_filter}{motion_filter},"
                f"trim=duration={_number(duration)},"
                f"settb=AVTB,setpts=N/({_number(fps)}*TB),setsar=1,"
                f"format=yuv420p[v{index}]"
            )
        filters.append(video_chain)

        if clip["kind"] == "video" and clip["has_audio"] and not clip["mute"]:
            audio_edge_fade_ms = int(
                plan.get("finishing", {}).get("audio_edge_fade_ms", 0)
            )
            audio_filters = [
                f"atrim=start={_number(float(clip['start']))}:end={_number(float(clip['end']))}",
                "asetpts=PTS-STARTPTS",
                *_atempo_filters(float(clip["speed"])),
                f"volume={_number(float(clip['audio_gain_db']))}dB",
                *_audio_edge_fade_filters(duration, audio_edge_fade_ms),
                "aresample=48000:async=1:first_pts=0",
                "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
                "apad",
                f"atrim=duration={_number(duration)}",
            ]
            filters.append(f"[{index}:a]" + ",".join(audio_filters) + f"[a{index}]")
        else:
            filters.append(
                "anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=duration={_number(duration)}[a{index}]"
            )
        concat_inputs.extend([f"[v{index}]", f"[a{index}]"])

    transitions = {
        int(item["after_clip"]): item for item in plan.get("transitions", [])
    }
    if not transitions:
        filters.append(
            "".join(concat_inputs)
            + f"concat=n={len(plan['clips'])}:v=1:a=1[vcat][acat]"
        )
    else:
        xfade_names = {
            "dissolve": "fade",
            "dip_to_black": "fadeblack",
            "slide_left": "slideleft",
            "slide_right": "slideright",
        }
        current_video = "v0"
        current_audio = "a0"
        current_duration = clip_durations[0]
        for index in range(1, len(plan["clips"])):
            joined_video = f"vjoin{index}"
            joined_audio = f"ajoin{index}"
            transition = transitions.get(index - 1)
            if transition is None:
                filters.append(
                    f"[{current_video}][v{index}]concat=n=2:v=1:a=0[{joined_video}]"
                )
                filters.append(
                    f"[{current_audio}][a{index}]concat=n=2:v=0:a=1[{joined_audio}]"
                )
                current_duration += clip_durations[index]
            else:
                duration = float(transition["duration"])
                offset = current_duration - duration
                transition_name = xfade_names[str(transition["type"])]
                filters.append(
                    f"[{current_video}][v{index}]xfade=transition={transition_name}:"
                    f"duration={_number(duration)}:offset={_number(offset)}[{joined_video}]"
                )
                filters.append(
                    f"[{current_audio}][a{index}]acrossfade=d={_number(duration)}:"
                    f"c1=tri:c2=tri[{joined_audio}]"
                )
                current_duration += clip_durations[index] - duration
            current_video = joined_video
            current_audio = joined_audio
        filters.append(f"[{current_video}]null[vcat]")
        filters.append(f"[{current_audio}]anull[acat]")

    video_label = "vcat"
    finishing = plan.get("finishing", {})
    if finishing.get('preset') == 'natural_balance':
        filters.append(f'[{video_label}]eq=contrast=1.018:saturation=1.015:brightness=0:gamma=1[vbalance]')
        video_label = 'vbalance'
    if finishing.get("preset") == "commerce_pop":
        filters.append(
            f"[{video_label}]eq=contrast=1.035:saturation=1.1:brightness=0:gamma=1.0,"
            "unsharp=5:5:0.42:5:5:0.0[vgrade]"
        )
        video_label = "vgrade"
    for flash_index, flash in enumerate(finishing.get("flashes", [])):
        next_label = f"vflash{flash_index}"
        start = float(flash["start"])
        end = start + float(flash["duration"])
        color = str(flash["color"])
        alpha = _number(float(flash["alpha"]))
        filters.append(
            f"[{video_label}]drawbox=x=0:y=0:w=iw:h=ih:color={color}@{alpha}:t=fill:"
            f"enable='between(t,{_number(start)},{_number(end)})'[{next_label}]"
        )
        video_label = next_label

    bottom_crop_pixels = int(finishing.get("bottom_crop_pixels", 0))
    cleanup_filter = _bottom_crop_cleanup_filter(width, height, bottom_crop_pixels)
    if cleanup_filter:
        filters.append(f"[{video_label}]{cleanup_filter}[vclean]")
        video_label = "vclean"

    render_overlays = list(plan["overlays"])
    for inset_index, (input_index, inset) in enumerate(
        zip(
            picture_in_picture_input_indexes,
            picture_in_picture,
            strict=True,
        )
    ):
        start = float(inset["start"])
        end = float(inset["end"])
        display_duration = end - start
        box_width = max(2, round(width * float(inset["width"]) / 2) * 2)
        box_height = max(2, round(height * float(inset["height"]) / 2) * 2)
        box_width = min(width, box_width)
        box_height = min(height, box_height)
        x = min(width - box_width, max(0, round(width * float(inset["x"]))))
        y = min(height - box_height, max(0, round(height * float(inset["y"]))))
        scale_filter = _video_scale_filter(box_width, box_height, inset["fit"])
        inset_label = f"vpipsrc{inset_index}"
        if inset["kind"] == "image":
            filters.append(
                f"[{input_index}:v]trim=duration={_number(display_duration)},"
                f"setpts=PTS-STARTPTS+{_number(start)}/TB,fps={_number(fps)},"
                f"{scale_filter},setsar=1,format=yuv420p[{inset_label}]"
            )
        else:
            watermark_filter = source_watermark_filter(job, plan, inset["source"], float(inset["source_start"]), float(inset["source_end"]), watermark_probe_cache)
            filters.append(
                f"[{input_index}:v]trim=start={_number(float(inset['source_start']))}:"
                f"end={_number(float(inset['source_end']))},"
                f"{watermark_filter}"
                f"setpts=PTS-STARTPTS+{_number(start)}/TB,fps={_number(fps)},"
                f"{scale_filter},trim=duration={_number(display_duration)},"
                f"setsar=1,format=yuv420p[{inset_label}]"
            )
        next_label = f"vpip{inset_index}"
        filters.append(
            f"[{video_label}][{inset_label}]overlay=x={x}:y={y}:"
            f"eof_action=pass:shortest=0:enable='between(t,{_number(start)},{_number(end)})'"
            f"[{next_label}]"
        )
        video_label = next_label
        callout_side = inset.get("callout_side")
        if callout_side in {"left", "right", "top", "bottom"}:
            border_padding = max(3, round(min(width, height) * 0.006))
            border_x = max(0, x - border_padding)
            border_y = max(0, y - border_padding)
            border_right = min(width, x + box_width + border_padding)
            border_bottom = min(height, y + box_height + border_padding)
            border_label = f"vpipborder{inset_index}"
            filters.append(
                f"[{video_label}]drawbox=x={border_x}:y={border_y}:"
                f"w={border_right - border_x}:h={border_bottom - border_y}:"
                f"color=0xFFD84D@0.96:t={border_padding}:"
                f"enable='between(t,{_number(start)},{_number(end)})'[{border_label}]"
            )
            video_label = border_label

            inset_x = float(inset["x"])
            inset_y = float(inset["y"])
            inset_width = float(inset["width"])
            inset_height = float(inset["height"])
            if callout_side == "left":
                arrow_text = "➜"
                arrow_x = max(0.045, inset_x - 0.065)
                arrow_y = inset_y + inset_height / 2
            elif callout_side == "right":
                arrow_text = "←"
                arrow_x = min(0.955, inset_x + inset_width + 0.065)
                arrow_y = inset_y + inset_height / 2
            elif callout_side == "top":
                arrow_text = "↓"
                arrow_x = inset_x + inset_width / 2
                arrow_y = max(0.045, inset_y - 0.055)
            else:
                arrow_text = "↑"
                arrow_x = inset_x + inset_width / 2
                arrow_y = min(0.955, inset_y + inset_height + 0.055)
            render_overlays.append(
                {
                    "kind": "label",
                    "start": start,
                    "end": end,
                    "text": arrow_text,
                    "preset": "pip_arrow",
                    "animation": "punch",
                    "effect_reason": "proof",
                    "color": "#FFD84D",
                    "outline_color": "#101010",
                    "x": arrow_x,
                    "y": arrow_y,
                    "align": 5,
                    "layer": 80,
                    "font_size": max(64, round(min(width, height) * 0.105)),
                    "outline_width": max(4, round(min(width, height) * 0.006)),
                    "shadow": max(1, round(min(width, height) * 0.002)),
                }
            )

    if plan.get('presentation', {}).get('version') == 2:
        style = plan['presentation']['style']
        accent = plan['presentation']['accent']
        render_overlays = [{**o, '_presentation': style, '_accent': accent} for o in render_overlays if not (o.get('preset') == 'fine_reaction' and o.get('text','').strip() in {'😳','🙈','✨','👇'})]
    for i, ((event, _), input_index) in enumerate(zip(stickers, sticker_inputs)):
        start, end = float(event['start']), float(event['end'])
        size = max(30, round(min(width,height)*.145))
        # Bounded spring entrance followed by stillness; no permanent wobble.
        x = max(0,min(width-size,round(float(event.get('x',.75))*width-size/2)))
        y = max(0,min(height-size,round(float(event.get('y',.4))*height-size/2)))
        filters.append(f'[{input_index}:v]scale={size}:{size},format=rgba,fade=t=in:st={_number(start)}:d=0.13:alpha=1,fade=t=out:st={_number(max(start,end-.15))}:d=0.15:alpha=1[sticker{i}]')
        filters.append(f"[{video_label}][sticker{i}]overlay=x={x}:y='{y}+{round(size*.16)}*exp(-10*(t-{_number(start)}))*cos(20*(t-{_number(start)}))':eof_action=pass:enable='between(t,{_number(start)},{_number(end)})'[vsticker{i}]")
        video_label = f'vsticker{i}'

    if render_overlays or plan.get('presentation',{}).get('graphics'):
        ass_path = job.work / "overlays.ass"
        write_ass(render_overlays, ass_path, width, height, plan.get('presentation'))
        uses_fine_cut_fonts = any(
            str(overlay.get("preset", "")).startswith("fine_")
            for overlay in render_overlays
        )
        if uses_fine_cut_fonts:
            _prepare_fine_cut_fonts(job)
            filters.append(
                f"[{video_label}]subtitles=filename='work/overlays.ass':fontsdir='work/fonts'[vstyled]"
            )
        else:
            filters.append(f"[{video_label}]subtitles=filename='work/overlays.ass'[vstyled]")
    else:
        filters.append(f"[{video_label}]null[vstyled]")
    filters.append(f"[vstyled]fps={_number(fps)},setpts=N/({_number(fps)}*TB)[vout]")

    spoken_label = "acat"
    voice_side_label: str | None = None
    if voice_input_index is not None:
        voice = plan["voiceover"]
        delay_ms = max(0, round(float(voice["start"]) * 1000))
        voice_needs_sidechain = bool(plan.get("music") and plan["music"]["ducking"])
        filters.append(
            f"[{voice_input_index}:a]asetpts=PTS-STARTPTS,aresample=48000,"
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"volume={_number(float(voice['volume_db']))}dB,adelay={delay_ms}:all=1,"
            f"apad,atrim=duration={_number(total_duration)}[voicefull]"
        )
        if voice_needs_sidechain:
            filters.append("[voicefull]asplit=2[voiceout][voiceside]")
            voice_mix_label = "voiceout"
            voice_side_label = "voiceside"
        else:
            voice_mix_label = "voicefull"
        filters.append(f"[acat][{voice_mix_label}]amix=inputs=2:duration=first:normalize=0[spoken]")
        spoken_label = "spoken"

    if music_input_index is not None:
        volume_db = float(plan["music"]["volume_db"])
        music_start = float(plan["music"].get("start", 0.0))
        filters.append(
            f"[{music_input_index}:a]atrim=start={_number(music_start)}:"
            f"duration={_number(total_duration)},"
            "asetpts=PTS-STARTPTS,aresample=48000,"
            f"volume={_number(volume_db)}dB[music]"
        )
        if plan["music"]["ducking"]:
            side_label = voice_side_label
            if side_label is None:
                filters.append(f"[{spoken_label}]asplit=2[spokenout][spokenside]")
                spoken_label = "spokenout"
                side_label = "spokenside"
            filters.append(
                f"[music][{side_label}]sidechaincompress=threshold=0.018:ratio=9:"
                "attack=12:release=280[ducked]"
            )
            bed_label = "ducked"
        else:
            bed_label = "music"
    else:
        bed_label = None

    audio_mix_inputs = [f"[{spoken_label}]"]
    if bed_label is not None:
        audio_mix_inputs.append(f"[{bed_label}]")
    for effect_index, (input_index, effect) in enumerate(
        zip(sfx_input_indexes, plan.get("sfx", []), strict=True)
    ):
        delay_ms = max(0, round(float(effect["start"]) * 1000))
        filters.append(
            f"[{input_index}:a]atrim=start={_number(float(effect['trim_start']))}:"
            f"end={_number(float(effect['trim_end']))},asetpts=PTS-STARTPTS,"
            "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:"
            f"channel_layouts=stereo,volume={_number(float(effect['volume_db']))}dB,"
            f"adelay={delay_ms}:all=1,apad,atrim=duration={_number(total_duration)}"
            f"[sfx{effect_index}]"
        )
        audio_mix_inputs.append(f"[sfx{effect_index}]")
    if len(audio_mix_inputs) == 1:
        filters.append(f"{audio_mix_inputs[0]}anull[amixed]")
    else:
        filters.append(
            "".join(audio_mix_inputs)
            + f"amix=inputs={len(audio_mix_inputs)}:duration=first:normalize=0,"
            "alimiter=limit=0.95[amixed]"
        )
    filters.append(
        f"[amixed]{_final_audio_filter(total_duration, finishing.get('preset') == 'commerce_pop')}"
        "[aout]"
    )

    filtergraph = ";\n".join(filters)
    filter_path = job.work / "filtergraph.txt"
    filter_path.write_text(filtergraph + "\n", encoding="utf-8")
    output_path = job.output / output["filename"]
    if plan.get('watermark_cleanup', {}).get('version') == 3:
        args.extend(['-filter_complex_threads', '2', '-threads', '4'])
    args.extend(
        [
            "-filter_complex_script",
            str(filter_path),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264" if plan.get('watermark_only') is True and plan.get('watermark_cleanup', {}).get('version') == 3 else output.get("video_codec", "libx264"),
            "-preset",
            output.get("preset", "medium"),
            "-crf",
            str(min(10, int(output.get("crf", 20)))) if plan.get('watermark_only') is True and plan.get('watermark_cleanup', {}).get('version') == 3 else str(int(output.get("crf", 20))),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            output.get("audio_bitrate", "192k"),
            "-movflags",
            "+faststart",
            "-shortest",
            str(output_path),
        ]
    )
    return args, output_path, filtergraph


def render_job(job_id: str, *, plan_path: Path | None = None) -> dict:
    job = JobPaths.for_id(job_id)
    selected_plan_path = plan_path or job.plan
    if not selected_plan_path.is_file():
        raise FileNotFoundError(f"Edit plan is missing: {selected_plan_path}")
    validation = validate_plan(job_id, read_json(selected_plan_path))
    plan = validation["plan"]
    from .watermark_telea import prepare_sources, preserve_original_audio, verify_final_watermarks
    prepared_sources = prepare_sources(job, plan)
    args, output_path, filtergraph = build_render_command(job, plan, prepared_sources)
    write_json(
        job.reports / "render-command.json",
        {
            "scope": "resolved argv and filter graph for the local renderer",
            "argv": args,
            "filtergraph": filtergraph,
            "expected_duration_seconds": validation["duration_seconds"],
        },
    )
    process = run_command(args, cwd=job.root)
    loudness = _normalize_output_audio(job, output_path, plan)
    preserve_original_audio(job, plan, output_path)
    verify_final_watermarks(job, plan, output_path)
    from .watermark_fidelity import audit_delivery
    watermark_summary = read_json(job.reports/'watermark-masks'/'execution.json') if plan.get('watermark_cleanup', {}).get('version') == 3 else {}
    fidelity = audit_delivery(job, plan, output_path, watermark_summary)
    if fidelity and fidelity['status'] == 'below_threshold':
        # One bounded higher-fidelity encode; reuse actual repairs, never a new
        # AI request or a fake sharpen/upscale. Do not publish a failed audit.
        args[args.index('-crf')+1] = '4'
        process = run_command(args, cwd=job.root)
        preserve_original_audio(job, plan, output_path)
        verify_final_watermarks(job, plan, output_path)
        watermark_summary = read_json(job.reports/'watermark-masks'/'execution.json')
        fidelity = audit_delivery(job, plan, output_path, watermark_summary)
        fidelity['bounded_reencode'] = True
        write_json(job.reports/'watermark-masks'/'fidelity.json', fidelity)
        write_json(job.reports/'fidelity-reencode-command.json', {'argv':args})
        if fidelity['status'] != 'passed':
            raise RuntimeError('Watermark final picture fidelity below threshold after bounded re-encode')
    result = {
        "status": "LOCAL_RENDER_COMPLETE",
        "job_id": job_id,
        "output": str(output_path),
        "expected_duration_seconds": validation["duration_seconds"],
        "ffmpeg_stderr_tail": process.stderr.splitlines()[-20:],
        "loudness_normalization": loudness,
        "picture_fidelity": fidelity,
        "qa_run": False,
    }
    write_json(job.reports / "render.json", result)
    return result
