from __future__ import annotations

import re
from pathlib import Path

from .media import duration_seconds, ffprobe, first_stream, media_kind
from .paths import JobPaths
from .util import read_json, safe_resolve_within, write_json
from .presentation import validate_presentation
from .watermarks import approved_regions


class PlanValidationError(ValueError):
    pass


_HEX_RGB_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
_NARRATIVE_REGIMES = {"general", "product_proof", "expert_overlay", "hybrid"}
_VISUAL_JOBS = {
    "hook",
    "setup",
    "problem",
    "identity",
    "action",
    "feature",
    "proof",
    "comparison",
    "progress",
    "objection",
    "reaction",
    "payoff",
    "cta",
    "bridge",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PlanValidationError(message)


def clip_output_duration(clip: dict) -> float:
    if clip["kind"] == "image":
        return float(clip["duration"])
    return (float(clip["end"]) - float(clip.get("start", 0.0))) / float(clip.get("speed", 1.0))


def transition_overlap_duration(plan: dict) -> float:
    transitions = plan.get("transitions", [])
    if not isinstance(transitions, list):
        return 0.0
    total = 0.0
    for transition in transitions:
        if not isinstance(transition, dict):
            continue
        try:
            duration = float(transition.get("duration", 0.0))
        except (TypeError, ValueError):
            continue
        if duration > 0:
            total += duration
    return total


def plan_duration(plan: dict) -> float:
    return max(
        0.0,
        sum(clip_output_duration(clip) for clip in plan["clips"])
        - transition_overlap_duration(plan),
    )


def validate_plan(job_id: str, plan: dict) -> dict:
    job = JobPaths.for_id(job_id)
    validate_presentation(plan.get('presentation'))
    _require(plan.get("schema_version") == 1, "schema_version must be 1")
    _require(plan.get("job_id") == job_id, "plan job_id must match the selected job")
    narrative_regime = plan.get("narrative_regime")
    if narrative_regime is not None:
        _require(
            narrative_regime in _NARRATIVE_REGIMES,
            "narrative_regime is unsupported",
        )

    output = plan.get("output")
    _require(isinstance(output, dict), "output must be an object")
    filename = output.get("filename")
    _require(isinstance(filename, str) and filename, "output.filename is required")
    _require(Path(filename).name == filename, "output.filename must not contain a directory")
    _require(filename.lower().endswith(".mp4"), "output.filename must end with .mp4")
    width = output.get("width")
    height = output.get("height")
    fps = output.get("fps")
    _require(isinstance(width, int) and 240 <= width <= 3840 and width % 2 == 0, "invalid width")
    _require(
        isinstance(height, int) and 240 <= height <= 3840 and height % 2 == 0,
        "invalid height",
    )
    _require(isinstance(fps, int | float) and 12 <= fps <= 60, "fps must be between 12 and 60")
    _require(0 <= int(output.get("crf", 20)) <= 40, "crf must be between 0 and 40")
    _require(
        output.get("video_codec", "libx264") in {"libx264", "h264_nvenc"},
        "video_codec must be libx264 or h264_nvenc",
    )
    _require(
        output.get("preset", "medium")
        in {
            "ultrafast",
            "superfast",
            "veryfast",
            "faster",
            "fast",
            "medium",
            "slow",
            "slower",
            "veryslow",
            "p1",
            "p2",
            "p3",
            "p4",
            "p5",
            "p6",
            "p7",
        },
        "unsupported encoder preset",
    )
    _require(
        re.fullmatch(r"[0-9]{2,4}k", output.get("audio_bitrate", "192k")) is not None,
        "audio_bitrate must look like 192k",
    )

    clips = plan.get("clips")
    _require(isinstance(clips, list) and clips, "clips must be a non-empty array")
    checked_clips = []
    for index, clip in enumerate(clips):
        prefix = f"clips[{index}]"
        _require(isinstance(clip, dict), f"{prefix} must be an object")
        source = clip.get("source")
        _require(isinstance(source, str) and source, f"{prefix}.source is required")
        try:
            source_path = safe_resolve_within(job.root / source, job.root)
        except ValueError as exc:
            raise PlanValidationError(f"{prefix}.source escapes the job directory") from exc
        _require(source_path.is_file(), f"{prefix}.source does not exist: {source}")
        detected_kind = media_kind(source_path)
        kind = clip.get("kind", detected_kind)
        _require(kind in {"video", "image"}, f"{prefix}.kind must be video or image")
        _require(kind == detected_kind, f"{prefix}.kind does not match the source file")
        fit = clip.get("fit", "fill")
        _require(fit in {"fill", "contain"}, f"{prefix}.fit must be fill or contain")
        speed = float(clip.get("speed", 1.0))
        _require(0.25 <= speed <= 4.0, f"{prefix}.speed must be between 0.25 and 4.0")

        checked = dict(clip)
        checked["source"] = Path(source).as_posix()
        checked["kind"] = kind
        visual_job = clip.get("visual_job")
        if visual_job is not None:
            _require(visual_job in _VISUAL_JOBS, f"{prefix}.visual_job is unsupported")
            checked["visual_job"] = visual_job
        for field in ("selection_reason", "exit_condition"):
            explanation = clip.get(field)
            if explanation is not None:
                _require(
                    isinstance(explanation, str) and 4 <= len(explanation.strip()) <= 300,
                    f"{prefix}.{field} must be a concrete 4-300 character explanation",
                )
                checked[field] = explanation.strip()
        checked["fit"] = fit
        checked["speed"] = speed
        checked["audio_gain_db"] = float(clip.get("audio_gain_db", 0.0))
        checked["mute"] = bool(clip.get("mute", False))
        motion = clip.get("motion")
        if motion is not None:
            _require(isinstance(motion, dict), f"{prefix}.motion must be an object")
            zoom_start = float(motion.get("zoom_start", 1.0))
            zoom_end = float(motion.get("zoom_end", zoom_start))
            _require(1.0 <= zoom_start <= 2.0, f"{prefix}.motion.zoom_start is invalid")
            _require(1.0 <= zoom_end <= 2.0, f"{prefix}.motion.zoom_end is invalid")
            checked["motion"] = {
                "zoom_start": zoom_start,
                "zoom_end": zoom_end,
                "focus_x_start": float(motion.get("focus_x_start", 0.5)),
                "focus_x_end": float(motion.get("focus_x_end", 0.5)),
                "focus_y_start": float(motion.get("focus_y_start", 0.5)),
                "focus_y_end": float(motion.get("focus_y_end", 0.5)),
            }
            for focus_name in (
                "focus_x_start",
                "focus_x_end",
                "focus_y_start",
                "focus_y_end",
            ):
                _require(
                    0.0 <= checked["motion"][focus_name] <= 1.0,
                    f"{prefix}.motion.{focus_name} is invalid",
                )
        if kind == "video":
            probe = ffprobe(source_path)
            media_duration = duration_seconds(probe)
            start = float(clip.get("start", 0.0))
            end = float(clip.get("end", media_duration))
            _require(start >= 0, f"{prefix}.start must be non-negative")
            _require(end > start, f"{prefix}.end must be greater than start")
            _require(
                end <= media_duration + 0.05,
                f"{prefix}.end exceeds source duration ({media_duration:.3f}s)",
            )
            checked["start"] = start
            checked["end"] = min(end, media_duration)
            checked["has_audio"] = first_stream(probe, "audio") is not None
        else:
            duration = float(clip.get("duration", 3.0))
            _require(0.1 <= duration <= 300, f"{prefix}.duration must be between 0.1 and 300")
            checked["duration"] = duration
            checked["has_audio"] = False
        checked_clips.append(checked)

    checked_plan = dict(plan)
    checked_plan["clips"] = checked_clips

    transitions = plan.get("transitions", [])
    _require(isinstance(transitions, list), "transitions must be an array")
    checked_transitions: list[dict] = []
    used_boundaries: set[int] = set()
    transition_types = {"dissolve", "dip_to_black", "slide_left", "slide_right"}
    transition_reasons = {
        "time_change",
        "location_change",
        "soft_bridge",
        "directional_motion",
        "chapter_break",
    }
    for index, transition in enumerate(transitions):
        prefix = f"transitions[{index}]"
        _require(isinstance(transition, dict), f"{prefix} must be an object")
        after_clip = transition.get("after_clip")
        _require(
            isinstance(after_clip, int) and not isinstance(after_clip, bool),
            f"{prefix}.after_clip must be an integer",
        )
        _require(
            0 <= after_clip < len(checked_clips) - 1,
            f"{prefix}.after_clip must identify a real clip boundary",
        )
        _require(after_clip not in used_boundaries, f"{prefix} duplicates a clip boundary")
        used_boundaries.add(after_clip)
        transition_type = transition.get("type")
        _require(transition_type in transition_types, f"{prefix}.type is unsupported")
        reason = transition.get("reason")
        _require(reason in transition_reasons, f"{prefix}.reason is unsupported")
        duration = float(transition.get("duration", 0.0))
        _require(0.08 <= duration <= 0.5, f"{prefix}.duration must be 0.08 to 0.5")
        if transition_type in {"slide_left", "slide_right"}:
            _require(duration <= 0.35, f"{prefix} directional slide must be at most 0.35s")
            _require(
                reason == "directional_motion",
                f"{prefix} directional slide requires directional_motion evidence",
            )
        if transition_type == "dip_to_black":
            _require(
                reason in {"time_change", "location_change", "chapter_break"},
                f"{prefix} dip_to_black requires a real chapter, time, or location change",
            )
        previous_duration = clip_output_duration(checked_clips[after_clip])
        next_duration = clip_output_duration(checked_clips[after_clip + 1])
        _require(
            duration <= min(previous_duration, next_duration) * 0.45,
            f"{prefix}.duration is too long for its adjacent clips",
        )
        checked_transitions.append(
            {
                "after_clip": after_clip,
                "type": transition_type,
                "duration": duration,
                "reason": reason,
            }
        )
    checked_transitions.sort(key=lambda item: item["after_clip"])
    checked_plan["transitions"] = checked_transitions
    total_duration = plan_duration(checked_plan)
    _require(total_duration <= 21_600, "planned output exceeds six hours")
    primary_video_source = next(
        (
            str(clip["source"])
            for clip in checked_clips
            if clip.get("kind") == "video"
        ),
        None,
    )

    picture_in_picture = plan.get("picture_in_picture", [])
    _require(
        isinstance(picture_in_picture, list) and len(picture_in_picture) <= 4,
        "picture_in_picture must be an array with at most four items",
    )
    checked_picture_in_picture: list[dict] = []
    for index, inset in enumerate(picture_in_picture):
        prefix = f"picture_in_picture[{index}]"
        _require(isinstance(inset, dict), f"{prefix} must be an object")
        source = inset.get("source")
        _require(isinstance(source, str) and source, f"{prefix}.source is required")
        try:
            source_path = safe_resolve_within(job.root / source, job.root)
        except ValueError as exc:
            raise PlanValidationError(f"{prefix}.source escapes the job directory") from exc
        _require(source_path.is_file(), f"{prefix}.source does not exist: {source}")
        _require(
            primary_video_source is None or Path(source).as_posix() != primary_video_source,
            f"{prefix}.source must use a supporting video or image, not repeat the main video",
        )
        detected_kind = media_kind(source_path)
        kind = inset.get("kind", detected_kind)
        _require(kind in {"video", "image"}, f"{prefix}.kind must be video or image")
        _require(kind == detected_kind, f"{prefix}.kind does not match the source file")
        start = float(inset.get("start", 0.0))
        end = float(inset.get("end", 0.0))
        _require(0 <= start < end, f"{prefix} has invalid start/end")
        _require(end <= total_duration + 0.1, f"{prefix}.end exceeds output duration")
        source_start = float(inset.get("source_start", 0.0))
        source_end = float(inset.get("source_end", end - start))
        _require(
            0 <= source_start < source_end,
            f"{prefix} has invalid source_start/source_end",
        )
        _require(
            source_end - source_start + 0.05 >= end - start,
            f"{prefix} source segment is shorter than its display duration",
        )
        if kind == "video":
            media_duration = duration_seconds(ffprobe(source_path))
            _require(
                source_end <= media_duration + 0.05,
                f"{prefix}.source_end exceeds source duration ({media_duration:.3f}s)",
            )
            source_end = min(source_end, media_duration)
        x = float(inset.get("x", 0.0))
        y = float(inset.get("y", 0.0))
        width_fraction = float(inset.get("width", 0.35))
        height_fraction = float(inset.get("height", 0.35))
        _require(0 <= x <= 1, f"{prefix}.x is invalid")
        _require(0 <= y <= 1, f"{prefix}.y is invalid")
        _require(0.15 <= width_fraction <= 0.65, f"{prefix}.width is invalid")
        _require(0.15 <= height_fraction <= 0.65, f"{prefix}.height is invalid")
        _require(x + width_fraction <= 1.0001, f"{prefix} exceeds the right frame edge")
        _require(y + height_fraction <= 1.0001, f"{prefix} exceeds the bottom frame edge")
        fit = inset.get("fit", "fill")
        _require(fit in {"fill", "contain"}, f"{prefix}.fit must be fill or contain")
        selection_reason = inset.get("selection_reason")
        _require(
            isinstance(selection_reason, str)
            and 4 <= len(selection_reason.strip()) <= 300,
            f"{prefix}.selection_reason must be a concrete 4-300 character explanation",
        )
        callout_side = inset.get("callout_side")
        _require(
            callout_side in {None, "left", "right", "top", "bottom"},
            f"{prefix}.callout_side is invalid",
        )
        if callout_side == "left":
            _require(x >= 0.12, f"{prefix} has no room for a left callout arrow")
        elif callout_side == "right":
            _require(
                x + width_fraction <= 0.88,
                f"{prefix} has no room for a right callout arrow",
            )
        elif callout_side == "top":
            _require(y >= 0.10, f"{prefix} has no room for a top callout arrow")
        elif callout_side == "bottom":
            _require(
                y + height_fraction <= 0.90,
                f"{prefix} has no room for a bottom callout arrow",
            )
        checked_picture_in_picture.append(
            {
                "source": Path(source).as_posix(),
                "kind": kind,
                "start": start,
                "end": end,
                "source_start": source_start,
                "source_end": source_end,
                "x": x,
                "y": y,
                "width": width_fraction,
                "height": height_fraction,
                "fit": fit,
                "selection_reason": selection_reason.strip(),
                "callout_side": callout_side,
            }
        )
    checked_picture_in_picture.sort(key=lambda item: item["start"])
    for previous, current in zip(
        checked_picture_in_picture,
        checked_picture_in_picture[1:],
        strict=False,
    ):
        _require(
            float(current["start"]) >= float(previous["end"]) - 0.001,
            "picture_in_picture items must not overlap",
        )
    checked_plan["picture_in_picture"] = checked_picture_in_picture
    used_video_sources = {c["source"] for c in checked_clips + checked_picture_in_picture if c["kind"] == "video"}
    for region in approved_regions(plan.get("watermark_cleanup")):
        _require(region["source"] in used_video_sources, "Watermark cleanup must refer to a used video source")

    overlays = plan.get("overlays", [])
    _require(isinstance(overlays, list), "overlays must be an array")
    checked_overlays = []
    for index, overlay in enumerate(overlays):
        prefix = f"overlays[{index}]"
        _require(isinstance(overlay, dict), f"{prefix} must be an object")
        kind = overlay.get("kind", "caption")
        _require(kind in {"title", "caption", "label"}, f"{prefix}.kind is unsupported")
        text = overlay.get("text")
        _require(isinstance(text, str) and text.strip(), f"{prefix}.text is required")
        start = float(overlay.get("start", 0.0))
        end = float(overlay.get("end", total_duration))
        _require(0 <= start < end, f"{prefix} has invalid start/end")
        _require(end <= total_duration + 0.1, f"{prefix}.end exceeds output duration")
        preset = overlay.get("preset", "default")
        _require(
            preset
            in {
                "default",
                "hook",
                "feature",
                "badge",
                "cta",
                "micro",
                "fine_caption",
                "fine_hook",
                "fine_accent",
                "fine_cta",
                "fine_micro",
                "fine_reaction",
                "fine_step_number",
                "fine_step_action",
            },
            f"{prefix}.preset is unsupported",
        )
        animation = overlay.get("animation", "none")
        _require(
            animation
            in {
                "none",
                "fade",
                "pop",
                "punch",
                "bounce",
                "slide_left",
                "slide_right",
                "slide_up",
                "drop",
                "tag",
                "cta_hold",
            },
            f"{prefix}.animation is unsupported",
        )
        effect_reason = overlay.get("effect_reason", "readability")
        _require(
            effect_reason
            in {
                "readability",
                "hook",
                "pain",
                "contrast",
                "number",
                "step",
                "proof",
                "payoff",
                "cta",
            },
            f"{prefix}.effect_reason is unsupported",
        )
        checked_colors: dict[str, str] = {}
        for color_field in ("color", "outline_color"):
            if color_field not in overlay:
                continue
            color_value = overlay[color_field]
            _require(
                isinstance(color_value, str)
                and _HEX_RGB_PATTERN.fullmatch(color_value) is not None,
                f"{prefix}.{color_field} must use strict #RRGGBB format",
            )
            checked_colors[color_field] = color_value.upper()
        highlights = overlay.get("highlights", [])
        _require(isinstance(highlights, list), f"{prefix}.highlights must be an array")
        _require(len(highlights) <= 1, f"{prefix}.highlights supports at most one phrase")
        checked_highlights: list[dict] = []
        normalized_text = " ".join(text.split()).casefold()
        for highlight_index, highlight in enumerate(highlights):
            highlight_prefix = f"{prefix}.highlights[{highlight_index}]"
            _require(
                isinstance(highlight, dict),
                f"{highlight_prefix} must be an object",
            )
            highlight_text = highlight.get("text")
            highlight_color = highlight.get("color")
            _require(
                isinstance(highlight_text, str) and highlight_text.strip(),
                f"{highlight_prefix}.text is required",
            )
            _require(
                isinstance(highlight_color, str)
                and _HEX_RGB_PATTERN.fullmatch(highlight_color) is not None,
                f"{highlight_prefix}.color must use strict #RRGGBB format",
            )
            normalized_highlight = " ".join(highlight_text.split()).casefold()
            _require(
                normalized_highlight in normalized_text,
                f"{highlight_prefix}.text must be an exact phrase inside overlay text",
            )
            _require(
                normalized_highlight != normalized_text,
                f"{highlight_prefix}.text must not cover the entire overlay; use color instead",
            )
            motion = highlight.get("motion", "none")
            _require(
                motion in {"none", "pulse", "shake"},
                f"{highlight_prefix}.motion is unsupported",
            )
            has_explicit_timing = "start" in highlight or "end" in highlight
            _require(
                ("start" in highlight) == ("end" in highlight),
                f"{highlight_prefix}.start and end must be paired",
            )
            highlight_start = float(highlight.get("start", start))
            highlight_end = float(highlight.get("end", end))
            _require(
                start - 0.01 <= highlight_start < highlight_end <= end + 0.01,
                f"{highlight_prefix} timing must stay inside its overlay",
            )
            if motion != "none":
                _require(
                    has_explicit_timing,
                    f"{highlight_prefix} animated motion requires explicit timing",
                )
                motion_duration = highlight_end - highlight_start
                if motion == "pulse":
                    _require(
                        0.10 <= motion_duration <= 0.95,
                        f"{highlight_prefix}.pulse must last 0.10 to 0.95 seconds",
                    )
                else:
                    _require(
                        0.10 <= motion_duration <= 0.65,
                        f"{highlight_prefix}.shake must last 0.10 to 0.65 seconds",
                    )
            checked_highlights.append(
                {
                    "text": highlight_text.strip(),
                    "color": highlight_color.upper(),
                    "motion": motion,
                    "start": highlight_start,
                    "end": highlight_end,
                }
            )
        x = overlay.get("x")
        y = overlay.get("y")
        align = int(overlay.get("align", 0))
        if x is not None or y is not None:
            _require(x is not None and y is not None, f"{prefix}.x and y must be paired")
            x = float(x)
            y = float(y)
            _require(0 <= x <= width, f"{prefix}.x is outside the output")
            _require(0 <= y <= height, f"{prefix}.y is outside the output")
            _require(1 <= align <= 9, f"{prefix}.align must be between 1 and 9")
        typography_overrides: dict[str, float] = {}
        if "font_size" in overlay:
            font_size = float(overlay["font_size"])
            _require(18 <= font_size <= 260, f"{prefix}.font_size must be 18 to 260")
            typography_overrides["font_size"] = font_size
        if "tracking" in overlay:
            tracking = float(overlay["tracking"])
            _require(-10 <= tracking <= 40, f"{prefix}.tracking must be -10 to 40")
            typography_overrides["tracking"] = tracking
        if "rotation" in overlay:
            rotation = float(overlay["rotation"])
            _require(-20 <= rotation <= 20, f"{prefix}.rotation must be -20 to 20")
            typography_overrides["rotation"] = rotation
        if "outline_width" in overlay:
            outline_width = float(overlay["outline_width"])
            _require(0 <= outline_width <= 20, f"{prefix}.outline_width must be 0 to 20")
            typography_overrides["outline_width"] = outline_width
        if "shadow" in overlay:
            shadow = float(overlay["shadow"])
            _require(0 <= shadow <= 20, f"{prefix}.shadow must be 0 to 20")
            typography_overrides["shadow"] = shadow
        checked_overlays.append(
            {
                **overlay,
                "kind": kind,
                "text": text.strip(),
                "start": start,
                "end": end,
                "preset": preset,
                "animation": animation,
                "effect_reason": effect_reason,
                **checked_colors,
                "highlights": checked_highlights,
                **typography_overrides,
                **({"x": x, "y": y, "align": align} if x is not None else {}),
            }
        )
    checked_plan["overlays"] = checked_overlays

    music = plan.get("music")
    if music is not None:
        _require(isinstance(music, dict), "music must be null or an object")
        source = music.get("source")
        _require(isinstance(source, str) and source, "music.source is required")
        try:
            music_path = safe_resolve_within(job.root / source, job.root)
        except ValueError as exc:
            raise PlanValidationError("music.source escapes the job directory") from exc
        _require(music_path.is_file(), f"music source does not exist: {source}")
        probe = ffprobe(music_path)
        _require(first_stream(probe, "audio") is not None, "music source has no audio stream")
        checked_plan["music"] = {
            **music,
            "source": Path(source).as_posix(),
            "volume_db": float(music.get("volume_db", -18.0)),
            "ducking": bool(music.get("ducking", True)),
            "start": float(music.get("start", 0.0)),
        }
        _require(checked_plan["music"]["start"] >= 0, "music.start must be non-negative")

    voiceover = plan.get("voiceover")
    if voiceover is not None:
        _require(isinstance(voiceover, dict), "voiceover must be null or an object")
        source = voiceover.get("source")
        _require(isinstance(source, str) and source, "voiceover.source is required")
        try:
            voice_path = safe_resolve_within(job.root / source, job.root)
        except ValueError as exc:
            raise PlanValidationError("voiceover.source escapes the job directory") from exc
        _require(voice_path.is_file(), f"voiceover source does not exist: {source}")
        _require(first_stream(ffprobe(voice_path), "audio") is not None, "voiceover has no audio")
        voice_start = float(voiceover.get("start", 0.0))
        _require(0 <= voice_start < total_duration, "voiceover.start is outside the output")
        checked_plan["voiceover"] = {
            **voiceover,
            "source": Path(source).as_posix(),
            "start": voice_start,
            "volume_db": float(voiceover.get("volume_db", 0.0)),
        }

    sfx = plan.get("sfx", [])
    _require(isinstance(sfx, list), "sfx must be an array")
    checked_sfx = []
    for index, effect in enumerate(sfx):
        prefix = f"sfx[{index}]"
        _require(isinstance(effect, dict), f"{prefix} must be an object")
        source = effect.get("source")
        _require(isinstance(source, str) and source, f"{prefix}.source is required")
        try:
            effect_path = safe_resolve_within(job.root / source, job.root)
        except ValueError as exc:
            raise PlanValidationError(f"{prefix}.source escapes the job directory") from exc
        _require(effect_path.is_file(), f"{prefix}.source does not exist: {source}")
        probe = ffprobe(effect_path)
        _require(first_stream(probe, "audio") is not None, f"{prefix} has no audio")
        source_duration = duration_seconds(probe)
        start = float(effect.get("start", 0.0))
        trim_start = float(effect.get("trim_start", 0.0))
        trim_end = float(effect.get("trim_end", source_duration))
        _require(0 <= start < total_duration, f"{prefix}.start is outside the output")
        _require(0 <= trim_start < trim_end, f"{prefix} has invalid trim bounds")
        _require(trim_end <= source_duration + 0.05, f"{prefix}.trim_end exceeds source")
        checked_sfx.append(
            {
                **effect,
                "source": Path(source).as_posix(),
                "start": start,
                "trim_start": trim_start,
                "trim_end": min(trim_end, source_duration),
                "volume_db": float(effect.get("volume_db", -8.0)),
            }
        )
    checked_plan["sfx"] = checked_sfx

    finishing = plan.get("finishing", {})
    _require(isinstance(finishing, dict), "finishing must be an object")
    preset = finishing.get("preset", "none")
    _require(preset in {"none", "natural_balance", "commerce_pop"}, "finishing.preset is unsupported")
    flashes = finishing.get("flashes", [])
    _require(isinstance(flashes, list), "finishing.flashes must be an array")
    checked_flashes = []
    for index, flash in enumerate(flashes):
        prefix = f"finishing.flashes[{index}]"
        _require(isinstance(flash, dict), f"{prefix} must be an object")
        start = float(flash.get("start", 0.0))
        duration = float(flash.get("duration", 0.06))
        alpha = float(flash.get("alpha", 0.16))
        _require(0 <= start < total_duration, f"{prefix}.start is outside the output")
        _require(0.01 <= duration <= 0.25, f"{prefix}.duration is invalid")
        _require(0 <= alpha <= 1, f"{prefix}.alpha is invalid")
        checked_flashes.append(
            {
                "start": start,
                "duration": duration,
                "color": str(flash.get("color", "white")),
                "alpha": alpha,
            }
        )
    bottom_crop_pixels = finishing.get("bottom_crop_pixels", 0)
    _require(
        isinstance(bottom_crop_pixels, int) and not isinstance(bottom_crop_pixels, bool),
        "finishing.bottom_crop_pixels must be an integer",
    )
    _require(
        0 <= bottom_crop_pixels <= min(160, height // 10),
        "finishing.bottom_crop_pixels exceeds the safe cleanup range",
    )
    audio_edge_fade_ms = finishing.get("audio_edge_fade_ms", 0)
    _require(
        isinstance(audio_edge_fade_ms, int) and not isinstance(audio_edge_fade_ms, bool),
        "finishing.audio_edge_fade_ms must be an integer",
    )
    _require(
        0 <= audio_edge_fade_ms <= 100,
        "finishing.audio_edge_fade_ms must be between 0 and 100",
    )
    loudness_target_lufs = finishing.get("loudness_target_lufs")
    if loudness_target_lufs is not None:
        loudness_target_lufs = float(loudness_target_lufs)
        _require(
            -18.0 <= loudness_target_lufs <= -11.0,
            "finishing.loudness_target_lufs must be between -18 and -11",
        )
    true_peak_limit_db = float(finishing.get("true_peak_limit_db", -1.5))
    _require(
        -3.0 <= true_peak_limit_db <= -0.5,
        "finishing.true_peak_limit_db must be between -3 and -0.5",
    )
    checked_plan["finishing"] = {
        "preset": preset,
        "flashes": checked_flashes,
        "bottom_crop_pixels": bottom_crop_pixels,
        "audio_edge_fade_ms": audio_edge_fade_ms,
        "loudness_target_lufs": loudness_target_lufs,
        "true_peak_limit_db": true_peak_limit_db,
    }

    return {
        "valid": True,
        "job_id": job_id,
        "duration_seconds": round(total_duration, 3),
        "plan": checked_plan,
    }


def _transcript_overlays(job: JobPaths, asset: dict, offset: float) -> list[dict]:
    transcript_path = asset.get("transcript_path")
    if not transcript_path:
        return []
    transcript = read_json(job.root / transcript_path)
    return [
        {
            "kind": "caption",
            "start": round(offset + float(segment["start"]), 3),
            "end": round(offset + float(segment["end"]), 3),
            "text": segment["text"],
        }
        for segment in transcript.get("segments", [])
        if segment.get("text")
    ]


def draft_plan(
    job_id: str,
    *,
    title: str | None = None,
    burn_captions: bool = False,
) -> dict:
    job = JobPaths.for_id(job_id)
    if not job.analysis.is_file():
        raise FileNotFoundError(f"Run analysis first: {job.analysis}")
    analysis = read_json(job.analysis)
    clips = []
    overlays = []
    offset = 0.0
    for asset in analysis["assets"]:
        if asset["kind"] == "video":
            duration = float(asset["probe"]["duration_seconds"])
            clips.append(
                {
                    "source": asset["job_path"],
                    "kind": "video",
                    "visual_job": "bridge",
                    "selection_reason": "Conservative draft retains the available source for later evidence review.",
                    "exit_condition": "The retained source span reaches its current safe endpoint.",
                    "start": 0.0,
                    "end": duration,
                    "speed": 1.0,
                    "fit": "fill",
                    "audio_gain_db": 0.0,
                    "mute": False,
                }
            )
            if burn_captions:
                overlays.extend(_transcript_overlays(job, asset, offset))
            offset += duration
        elif asset["kind"] == "image":
            clips.append(
                {
                    "source": asset["job_path"],
                    "kind": "image",
                    "visual_job": "identity",
                    "selection_reason": "Conservative draft keeps the supplied still as an identity reference.",
                    "exit_condition": "The still has remained readable long enough for later planning.",
                    "duration": 3.0,
                    "speed": 1.0,
                    "fit": "contain",
                    "audio_gain_db": 0.0,
                    "mute": False,
                }
            )
            offset += 3.0

    if title and clips:
        overlays.insert(
            0,
            {
                "kind": "title",
                "start": 0.0,
                "end": min(3.5, offset),
                "text": title,
            },
        )
    plan = {
        "schema_version": 1,
        "job_id": job_id,
        "intent_summary": "Draft only: preserve all visual assets in manifest order.",
        "narrative_regime": "general",
        "output": {
            "filename": f"{job_id}.mp4",
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "video_codec": "libx264",
            "crf": 20,
            "preset": "medium",
            "audio_bitrate": "192k",
        },
        "clips": clips,
        "transitions": [],
        "picture_in_picture": [],
        "overlays": overlays,
        "music": None,
        "finishing": {
            "preset": "none",
            "flashes": [],
            "bottom_crop_pixels": 0,
            "audio_edge_fade_ms": 18,
            "loudness_target_lufs": -14.0,
            "true_peak_limit_db": -1.5,
        },
    }
    validated = validate_plan(job_id, plan)
    # Persist only the public schema. Runtime-only fields such as has_audio are
    # recomputed by validate_plan before rendering.
    write_json(job.plan, plan)
    return validated
