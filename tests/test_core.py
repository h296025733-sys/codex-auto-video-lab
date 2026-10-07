import json
import shutil
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_video_lab import render
from auto_video_lab.analysis import _annotate_word_acoustics
from auto_video_lab.cli import _build_parser
from auto_video_lab.media import media_kind
from auto_video_lab.paths import validate_job_id
from auto_video_lab.plan import (
    PlanValidationError,
    clip_output_duration,
    plan_duration,
    transition_overlap_duration,
    validate_plan,
)
from auto_video_lab.render import (
    _ass_bgr_color,
    _ass_overlay_position,
    _ass_time,
    _atempo_filters,
    _audio_edge_fade_filters,
    _bottom_crop_cleanup_filter,
    _final_audio_filter,
    _loudness_result_within_limits,
    _motion_filter,
    _next_loudness_normalization_targets,
    _parse_loudnorm_json,
    _true_peak_repair_filter,
    _verified_fine_cut_font_files,
    build_render_command,
    write_ass,
)
from auto_video_lab.voiceover import assess_voiceover_spec, validate_voiceover_spec


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("clip.mp4", "video"),
        ("photo.PNG", "image"),
        ("music.wav", "audio"),
        ("notes.txt", "unknown"),
    ],
)
def test_media_kind(filename: str, expected: str) -> None:
    assert media_kind(Path(filename)) == expected


def test_validate_job_id() -> None:
    assert validate_job_id("sample_job-01") == "sample_job-01"
    with pytest.raises(ValueError):
        validate_job_id("../escape")


def test_analyze_cli_accepts_only_supported_caption_languages() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        ["analyze", "--job", "sample-job", "--transcribe", "--language", "es"]
    )
    assert args.language == "es"
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["analyze", "--job", "sample-job", "--transcribe", "--language", "fr"]
        )


def test_word_acoustics_identifies_a_real_local_intensity_peak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_decode(args: list[str]) -> None:
        samples = array("h", [800] * 3200 + [0] * 800 + [8000] * 3200 + [0] * 800 + [800] * 3200)
        with wave.open(args[-1], "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(samples.tobytes())

    monkeypatch.setattr("auto_video_lab.analysis.run_command", fake_decode)
    words = [
        {"start": 0.0, "end": 0.2, "word": "this"},
        {"start": 0.25, "end": 0.45, "word": "really"},
        {"start": 0.5, "end": 0.7, "word": "works"},
    ]
    result = _annotate_word_acoustics(
        tmp_path / "voice.mp4",
        tmp_path,
        [{"start": 0.0, "end": 0.7, "text": "this really works", "words": words}],
    )

    assert result["status"] == "available"
    assert words[1]["acoustics"]["relative_energy_db"] > 10
    assert words[1]["acoustics"]["emphasis_hint"] == "high"
    assert words[0]["acoustics"]["emphasis_hint"] == "none"


def test_plan_duration_accounts_for_speed() -> None:
    plan = {
        "clips": [
            {"kind": "video", "start": 2.0, "end": 8.0, "speed": 2.0},
            {"kind": "image", "duration": 2.5},
        ]
    }
    assert clip_output_duration(plan["clips"][0]) == 3.0
    assert plan_duration(plan) == 5.5


def test_plan_duration_subtracts_real_transition_overlap() -> None:
    plan = {
        "clips": [
            {"kind": "image", "duration": 2.0},
            {"kind": "image", "duration": 2.5},
        ],
        "transitions": [
            {
                "after_clip": 0,
                "type": "dissolve",
                "duration": 0.25,
                "reason": "soft_bridge",
            }
        ],
    }
    assert transition_overlap_duration(plan) == 0.25
    assert plan_duration(plan) == 4.25


def test_render_command_uses_declared_transition_instead_of_template_cut(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    output = tmp_path / "output"
    work.mkdir()
    output.mkdir()
    plan = {
        "output": {
            "filename": "transition.mp4",
            "width": 1080,
            "height": 1920,
            "fps": 30,
        },
        "clips": [
            {
                "source": "one.png",
                "kind": "image",
                "duration": 2.0,
                "fit": "fill",
                "speed": 1.0,
                "mute": True,
                "audio_gain_db": 0.0,
                "has_audio": False,
            },
            {
                "source": "two.png",
                "kind": "image",
                "duration": 2.0,
                "fit": "fill",
                "speed": 1.0,
                "mute": True,
                "audio_gain_db": 0.0,
                "has_audio": False,
            },
        ],
        "transitions": [
            {
                "after_clip": 0,
                "type": "slide_left",
                "duration": 0.2,
                "reason": "directional_motion",
            }
        ],
        "overlays": [],
        "music": None,
        "voiceover": None,
        "sfx": [],
        "finishing": {"preset": "none", "flashes": []},
    }
    job = SimpleNamespace(root=tmp_path, work=work, output=output)
    _, _, filtergraph = build_render_command(job, plan)

    assert "xfade=transition=slideleft:duration=0.2:offset=1.8" in filtergraph
    assert "acrossfade=d=0.2:c1=tri:c2=tri" in filtergraph
    assert filtergraph.count("settb=AVTB") == 2


def test_render_command_builds_visual_only_picture_in_picture(tmp_path: Path) -> None:
    work = tmp_path / "work"
    output = tmp_path / "output"
    work.mkdir()
    output.mkdir()
    plan = {
        "output": {"filename": "pip.mp4", "width": 1080, "height": 1920, "fps": 30},
        "clips": [
            {
                "source": "main.png",
                "kind": "image",
                "duration": 4.0,
                "fit": "fill",
                "speed": 1.0,
                "mute": True,
                "audio_gain_db": 0.0,
                "has_audio": False,
            }
        ],
        "transitions": [],
        "picture_in_picture": [
            {
                "source": "detail.png",
                "kind": "image",
                "start": 1.0,
                "end": 2.5,
                "source_start": 0.0,
                "source_end": 1.5,
                "x": 0.58,
                "y": 0.08,
                "width": 0.34,
                "height": 0.28,
                "fit": "contain",
                "selection_reason": "Shows the supplied detail during the matching sentence.",
                "callout_side": "left",
            }
        ],
        "overlays": [],
        "music": None,
        "voiceover": None,
        "sfx": [],
        "finishing": {"preset": "none", "flashes": []},
    }
    job = SimpleNamespace(root=tmp_path, work=work, output=output)

    args, _, filtergraph = build_render_command(job, plan)

    assert str(tmp_path / "detail.png") in args
    assert "[1:v]trim=duration=1.5,setpts=PTS-STARTPTS+1/TB" in filtergraph
    assert "overlay=x=" in filtergraph
    assert "enable='between(t,1,2.5)'" in filtergraph
    assert "drawbox=" in filtergraph
    assert "0xFFD84D@0.96" in filtergraph
    assert "➜" in (work / "overlays.ass").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("speed", "expected"),
    [
        (1.0, []),
        (0.25, ["atempo=0.5", "atempo=0.5"]),
        (0.75, ["atempo=0.75"]),
        (2.0, ["atempo=2"]),
        (4.0, ["atempo=2", "atempo=2"]),
    ],
)
def test_atempo_chain(speed: float, expected: list[str]) -> None:
    assert _atempo_filters(speed) == expected


def test_ass_time_and_utf8_overlay(tmp_path: Path) -> None:
    assert _ass_time(61.239) == "0:01:01.24"
    output = tmp_path / "captions.ass"
    write_ass(
        [{"kind": "caption", "start": 0.0, "end": 1.25, "text": "这是字幕"}],
        output,
        1080,
        1920,
    )
    text = output.read_text(encoding="utf-8")
    assert "Microsoft YaHei" in text
    assert "这是字幕" in text


def test_ass_overlay_event_color_overrides_use_bgr(tmp_path: Path) -> None:
    assert _ass_bgr_color("#12ABEF") == "&HEFAB12&"
    output = tmp_path / "colored.ass"
    write_ass(
        [
            {
                "kind": "caption",
                "start": 0.0,
                "end": 1.0,
                "text": "EVENT COLOR",
                "color": "#12ABEF",
                "outline_color": "#345678",
            }
        ],
        output,
        1080,
        1920,
    )
    text = output.read_text(encoding="utf-8")
    assert r"{\1c&HEFAB12&\3c&H785634&}EVENT COLOR" in text


def test_ass_phrase_highlight_preserves_line_breaks_and_step_hierarchy(
    tmp_path: Path,
) -> None:
    output = tmp_path / "editorial-captions.ass"
    write_ass(
        [
            {
                "kind": "caption",
                "preset": "fine_caption",
                "start": 0.0,
                "end": 1.8,
                "text": "Y AYUDA A RETIRAR\nLA ACUMULACIÓN",
                "highlights": [
                    {
                        "text": "RETIRAR",
                        "color": "#68E0C2",
                        "motion": "pulse",
                        "start": 0.72,
                        "end": 1.08,
                    }
                ],
            },
            {
                "kind": "label",
                "preset": "fine_step_number",
                "start": 1.8,
                "end": 3.0,
                "text": "01",
                "x": 0.08,
                "y": 0.24,
                "align": 7,
            },
            {
                "kind": "label",
                "preset": "fine_step_action",
                "start": 1.8,
                "end": 3.0,
                "text": "APLICA",
                "x": 0.18,
                "y": 0.24,
                "align": 7,
            },
        ],
        output,
        1080,
        1920,
    )
    text = output.read_text(encoding="utf-8")
    assert "Style: FineStepNumber,Montserrat ExtraBold" in text
    assert "Style: FineStepAction,Anton" in text
    assert r"Y AYUDA A {\1c&HC2E068&\fscx100\fscy100\t(720,821,\fscx126\fscy126)" in text
    assert r"\t(821,907,\fscx104\fscy104)" in text
    assert r"\t(907,979,\fscx111\fscy111)" in text
    assert r"\t(979,1080,\fscx100\fscy100)" in text
    assert r"\hRETIRAR\h{\1c&HFFFFFF&\fscx100\fscy100\frz0}\NLA ACUMULACIÓN" in text
    assert "Dialogue: 0,0:00:01.80,0:00:03.00,FineStepNumber" in text
    assert "Dialogue: 0,0:00:01.80,0:00:03.00,FineStepAction" in text


def test_ass_normalized_overlay_coordinates_render_in_playres_pixels(tmp_path: Path) -> None:
    overlay = {
        "kind": "title",
        "start": 0.0,
        "end": 1.6,
        "text": "产品细节",
        "animation": "fade",
        "x": 0.5,
        "y": 0.14,
        "align": 5,
    }
    assert _ass_overlay_position(overlay, 1080, 1920) == (540.0, 268.8)

    output = tmp_path / "normalized-position.ass"
    write_ass([overlay], output, 1080, 1920)

    text = output.read_text(encoding="utf-8")
    assert r"\an5\pos(540,268.8)" in text
    assert "产品细节" in text


def test_ass_highlight_shake_is_bounded_to_the_exact_phrase(tmp_path: Path) -> None:
    output = tmp_path / "shake-highlight.ass"
    write_ass(
        [
            {
                "kind": "caption",
                "preset": "fine_caption",
                "start": 0.0,
                "end": 1.4,
                "text": "These BUMPS changed everything",
                "highlights": [
                    {
                        "text": "BUMPS",
                        "color": "#FF6B6B",
                        "motion": "shake",
                        "start": 0.40,
                        "end": 0.70,
                    }
                ],
            }
        ],
        output,
        1080,
        1920,
    )
    text = output.read_text(encoding="utf-8")
    assert r"These {\1c&H6B6BFF&\fscx100\fscy100\frz0\t(400,450,\fscx120\fscy120)" in text
    assert r"\t(400,450,\frz-7)" in text
    assert r"\t(450,500,\frz6)" in text
    assert r"\hBUMPS\h{\1c&HFFFFFF&\fscx100\fscy100\frz0} changed\Neverything" in text
    assert text.count(r"\frz-7") == 1


def test_ass_absolute_overlay_coordinates_remain_backward_compatible(
    tmp_path: Path,
) -> None:
    overlay = {
        "kind": "label",
        "start": 0.0,
        "end": 1.0,
        "text": "ABSOLUTE",
        "animation": "tag",
        "x": 70,
        "y": 160,
        "align": 7,
    }
    assert _ass_overlay_position(overlay, 1080, 1920) == (70.0, 160.0)

    output = tmp_path / "absolute-position.ass"
    write_ass([overlay], output, 1080, 1920)

    text = output.read_text(encoding="utf-8")
    assert r"\an7\move(0,160,70,160,0,180)" in text


def _minimal_image_plan(job_id: str, image_name: str) -> dict:
    return {
        "schema_version": 1,
        "job_id": job_id,
        "output": {
            "filename": "output.mp4",
            "width": 1080,
            "height": 1920,
            "fps": 30,
        },
        "clips": [{"source": image_name, "kind": "image", "duration": 1.0}],
        "picture_in_picture": [],
        "overlays": [
            {
                "kind": "caption",
                "start": 0.0,
                "end": 1.0,
                "text": "COLOR",
                "color": "#a1b2c3",
                "outline_color": "#010203",
            }
        ],
        "music": None,
    }


def test_plan_validates_non_overlapping_picture_in_picture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main.png"
    detail = tmp_path / "detail.png"
    main.write_bytes(b"main")
    detail.write_bytes(b"detail")
    monkeypatch.setattr(
        "auto_video_lab.plan.JobPaths.for_id",
        lambda job_id: SimpleNamespace(root=tmp_path),
    )
    plan = _minimal_image_plan("pip-test", main.name)
    plan["picture_in_picture"] = [
        {
            "source": detail.name,
            "kind": "image",
            "start": 0.1,
            "end": 0.9,
            "source_start": 0.0,
            "source_end": 0.8,
            "x": 0.6,
            "y": 0.08,
            "width": 0.32,
            "height": 0.28,
            "fit": "contain",
            "selection_reason": "Shows the supplied detail while the main image remains visible.",
            "callout_side": "left",
        }
    ]

    result = validate_plan("pip-test", plan)

    assert result["plan"]["picture_in_picture"][0]["source"] == detail.name
    assert result["plan"]["picture_in_picture"][0]["callout_side"] == "left"


def test_plan_rejects_picture_in_picture_callout_without_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main.png"
    detail = tmp_path / "detail.png"
    main.write_bytes(b"main")
    detail.write_bytes(b"detail")
    monkeypatch.setattr(
        "auto_video_lab.plan.JobPaths.for_id",
        lambda job_id: SimpleNamespace(root=tmp_path),
    )
    plan = _minimal_image_plan("pip-callout-test", main.name)
    plan["picture_in_picture"] = [
        {
            "source": detail.name,
            "kind": "image",
            "start": 0.1,
            "end": 0.9,
            "source_start": 0.0,
            "source_end": 0.8,
            "x": 0.04,
            "y": 0.08,
            "width": 0.32,
            "height": 0.28,
            "fit": "contain",
            "selection_reason": "The inset is valid but leaves no space for a left arrow.",
            "callout_side": "left",
        }
    ]

    with pytest.raises(PlanValidationError, match="no room for a left callout arrow"):
        validate_plan("pip-callout-test", plan)


def test_plan_rejects_picture_in_picture_outside_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main.png"
    detail = tmp_path / "detail.png"
    main.write_bytes(b"main")
    detail.write_bytes(b"detail")
    monkeypatch.setattr(
        "auto_video_lab.plan.JobPaths.for_id",
        lambda job_id: SimpleNamespace(root=tmp_path),
    )
    plan = _minimal_image_plan("pip-test", main.name)
    plan["picture_in_picture"] = [
        {
            "source": detail.name,
            "kind": "image",
            "start": 0.1,
            "end": 0.9,
            "source_start": 0.0,
            "source_end": 0.8,
            "x": 0.8,
            "y": 0.08,
            "width": 0.32,
            "height": 0.28,
            "fit": "contain",
            "selection_reason": "This inset would cross the right frame edge.",
        }
    ]

    with pytest.raises(PlanValidationError, match="right frame edge"):
        validate_plan("pip-test", plan)


def test_plan_validates_and_normalizes_overlay_colors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = tmp_path / "source.png"
    image_path.write_bytes(b"not decoded during image plan validation")
    monkeypatch.setattr(
        "auto_video_lab.plan.JobPaths.for_id",
        lambda job_id: SimpleNamespace(root=tmp_path),
    )

    result = validate_plan("color-test", _minimal_image_plan("color-test", image_path.name))

    assert result["plan"]["overlays"][0]["color"] == "#A1B2C3"
    assert result["plan"]["overlays"][0]["outline_color"] == "#010203"


def test_plan_validates_grounded_transition_and_effect_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"not decoded during image plan validation")
    second.write_bytes(b"not decoded during image plan validation")
    monkeypatch.setattr(
        "auto_video_lab.plan.JobPaths.for_id",
        lambda job_id: SimpleNamespace(root=tmp_path),
    )
    plan = _minimal_image_plan("transition-test", first.name)
    plan["clips"].append({"source": second.name, "kind": "image", "duration": 1.0})
    plan["transitions"] = [
        {
            "after_clip": 0,
            "type": "dissolve",
            "duration": 0.2,
            "reason": "soft_bridge",
        }
    ]
    plan["overlays"][0]["effect_reason"] = "proof"

    result = validate_plan("transition-test", plan)

    assert result["duration_seconds"] == 1.8
    assert result["plan"]["transitions"][0]["reason"] == "soft_bridge"
    assert result["plan"]["overlays"][0]["effect_reason"] == "proof"


def test_plan_rejects_directional_transition_without_motion_evidence_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"not decoded during image plan validation")
    second.write_bytes(b"not decoded during image plan validation")
    monkeypatch.setattr(
        "auto_video_lab.plan.JobPaths.for_id",
        lambda job_id: SimpleNamespace(root=tmp_path),
    )
    plan = _minimal_image_plan("transition-test", first.name)
    plan["clips"].append({"source": second.name, "kind": "image", "duration": 1.0})
    plan["transitions"] = [
        {
            "after_clip": 0,
            "type": "slide_left",
            "duration": 0.2,
            "reason": "soft_bridge",
        }
    ]

    with pytest.raises(PlanValidationError, match="directional_motion evidence"):
        validate_plan("transition-test", plan)


def test_plan_validates_and_normalizes_one_caption_highlight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = tmp_path / "source.png"
    image_path.write_bytes(b"not decoded during image plan validation")
    monkeypatch.setattr(
        "auto_video_lab.plan.JobPaths.for_id",
        lambda job_id: SimpleNamespace(root=tmp_path),
    )
    plan = _minimal_image_plan("highlight-test", image_path.name)
    plan["overlays"][0]["text"] = "SKIN FEELS MORE COMFORTABLE"
    plan["overlays"][0]["highlights"] = [
        {
            "text": "MORE COMFORTABLE",
            "color": "#68e0c2",
            "motion": "shake",
            "start": 0.4,
            "end": 0.75,
        }
    ]

    result = validate_plan("highlight-test", plan)

    assert result["plan"]["overlays"][0]["highlights"] == [
        {
            "text": "MORE COMFORTABLE",
            "color": "#68E0C2",
            "motion": "shake",
            "start": 0.4,
            "end": 0.75,
        }
    ]


def test_plan_accepts_one_timed_keyword_inside_a_large_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = tmp_path / "source.png"
    image_path.write_bytes(b"not decoded during image plan validation")
    monkeypatch.setattr(
        "auto_video_lab.plan.JobPaths.for_id",
        lambda job_id: SimpleNamespace(root=tmp_path),
    )
    plan = _minimal_image_plan("title-highlight-test", image_path.name)
    plan["overlays"][0].update(
        {
            "kind": "title",
            "text": "CAN WE TALK ABOUT BODY BUMPS",
            "preset": "fine_hook",
            "animation": "punch",
            "effect_reason": "hook",
            "highlights": [
                {
                    "text": "BODY",
                    "color": "#ffd84d",
                    "motion": "shake",
                    "start": 0.42,
                    "end": 0.62,
                }
            ],
        }
    )

    result = validate_plan("title-highlight-test", plan)

    assert result["plan"]["overlays"][0]["highlights"][0] == {
        "text": "BODY",
        "color": "#FFD84D",
        "motion": "shake",
        "start": 0.42,
        "end": 0.62,
    }

@pytest.mark.parametrize(
    "highlight",
    [
        {
            "text": "MORE COMFORTABLE",
            "color": "#68E0C2",
            "motion": "shake",
            "start": 0.4,
            "end": 1.1,
        },
        {
            "text": "MORE COMFORTABLE",
            "color": "#68E0C2",
            "motion": "pulse",
            "start": 1.0,
            "end": 1.2,
        },
        {
            "text": "MORE COMFORTABLE",
            "color": "#68E0C2",
            "motion": "bounce_forever",
            "start": 0.4,
            "end": 0.75,
        },
    ],
)
def test_plan_rejects_invalid_timed_highlight_motion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    highlight: dict,
) -> None:
    image_path = tmp_path / "source.png"
    image_path.write_bytes(b"not decoded during image plan validation")
    monkeypatch.setattr(
        "auto_video_lab.plan.JobPaths.for_id",
        lambda job_id: SimpleNamespace(root=tmp_path),
    )
    plan = _minimal_image_plan("highlight-motion-test", image_path.name)
    plan["overlays"][0]["text"] = "SKIN FEELS MORE COMFORTABLE"
    plan["overlays"][0]["highlights"] = [highlight]

    with pytest.raises(PlanValidationError):
        validate_plan("highlight-motion-test", plan)


@pytest.mark.parametrize(
    "highlight",
    [
        {"text": "INVENTED", "color": "#68E0C2"},
        {"text": "COLOR", "color": "#68E0C2"},
        {"text": "COL", "color": "mint"},
    ],
)
def test_plan_rejects_unsafe_caption_highlights(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    highlight: dict,
) -> None:
    image_path = tmp_path / "source.png"
    image_path.write_bytes(b"not decoded during image plan validation")
    monkeypatch.setattr(
        "auto_video_lab.plan.JobPaths.for_id",
        lambda job_id: SimpleNamespace(root=tmp_path),
    )
    plan = _minimal_image_plan("highlight-test", image_path.name)
    plan["overlays"][0]["highlights"] = [highlight]

    with pytest.raises(PlanValidationError):
        validate_plan("highlight-test", plan)


@pytest.mark.parametrize("field", ["color", "outline_color"])
@pytest.mark.parametrize("invalid", ["red", "#123", "#1234567", "123456", "#12FG56", None])
def test_plan_rejects_non_strict_overlay_colors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    invalid: object,
) -> None:
    image_path = tmp_path / "source.png"
    image_path.write_bytes(b"not decoded during image plan validation")
    monkeypatch.setattr(
        "auto_video_lab.plan.JobPaths.for_id",
        lambda job_id: SimpleNamespace(root=tmp_path),
    )
    plan = _minimal_image_plan("color-test", image_path.name)
    plan["overlays"][0][field] = invalid

    with pytest.raises(PlanValidationError, match=rf"{field} must use strict #RRGGBB format"):
        validate_plan("color-test", plan)


def test_motion_filter_uses_keyframed_zoom_and_focus() -> None:
    result = _motion_filter(
        {
            "motion": {
                "zoom_start": 1.0,
                "zoom_end": 1.15,
                "focus_x_start": 0.4,
                "focus_x_end": 0.6,
                "focus_y_start": 0.5,
                "focus_y_end": 0.5,
            }
        },
        1080,
        1920,
        30,
        1.0,
    )
    assert "zoompan=" in result
    assert "s=1080x1920" in result
    assert "on/29" in result


def test_bottom_crop_cleanup_uses_small_aspect_preserving_reframe() -> None:
    assert _bottom_crop_cleanup_filter(1080, 1920, 50) == (
        "crop=1080:1870:0:0,scale=1110:1920:flags=lanczos,"
        "crop=1080:1920:15:0,setsar=1"
    )
    assert _bottom_crop_cleanup_filter(1080, 1920, 70) == (
        "crop=1080:1850:0:0,scale=1122:1920:flags=lanczos,"
        "crop=1080:1920:21:0,setsar=1"
    )
    assert _bottom_crop_cleanup_filter(1080, 1920, 0) == ""


def test_audio_edge_fade_preserves_clip_duration_and_limits_short_clips() -> None:
    assert _audio_edge_fade_filters(1.25, 18) == [
        "afade=t=in:st=0:d=0.018",
        "afade=t=out:st=1.232:d=0.018",
    ]
    assert _audio_edge_fade_filters(0.04, 30) == [
        "afade=t=in:st=0:d=0.01",
        "afade=t=out:st=0.03:d=0.01",
    ]
    assert _audio_edge_fade_filters(1.0, 0) == []


def test_final_audio_filter_rebuilds_continuous_pts_and_exact_duration() -> None:
    assert _final_audio_filter(19.992131, True) == (
        "aresample=48000,asetpts=N/SR/TB,apad,atrim=duration=19.992131"
    )
    assert _final_audio_filter(2.0, False) == (
        "aresample=48000,asetpts=N/SR/TB,apad,atrim=duration=2"
    )


def test_loudnorm_measurement_parser_uses_the_last_complete_ffmpeg_block() -> None:
    stderr = """
    noise {not json}
    {
      "input_i" : "-23.40",
      "input_tp" : "-4.20",
      "input_lra" : "2.10",
      "input_thresh" : "-33.80",
      "target_offset" : "0.05"
    }
    """
    assert _parse_loudnorm_json(stderr) == {
        "input_i": "-23.40",
        "input_tp": "-4.20",
        "input_lra": "2.10",
        "input_thresh": "-33.80",
        "target_offset": "0.05",
    }
    assert _parse_loudnorm_json("no measurement") is None


def test_loudness_result_enforces_integrated_and_true_peak_limits() -> None:
    measurement = {"input_i": "-14.30", "input_tp": "-1.31"}
    assert _loudness_result_within_limits(
        measurement,
        target_lufs=-14.0,
        true_peak_limit_db=-1.5,
    ) == (True, True)
    assert _loudness_result_within_limits(
        {"input_i": "-14.60", "input_tp": "-1.29"},
        target_lufs=-14.0,
        true_peak_limit_db=-1.5,
    ) == (False, False)


def test_loudness_retry_compensates_measured_aac_shift_without_relaxing_qa() -> None:
    corrected_target, corrected_peak = _next_loudness_normalization_targets(
        target_lufs=-14.0,
        true_peak_limit_db=-1.5,
        measured_i=-14.65,
        measured_tp=-1.20,
        previous_normalization_peak_db=-1.85,
    )
    assert corrected_target == pytest.approx(-13.35)
    assert corrected_peak == pytest.approx(-2.3)


def test_true_peak_repair_uses_bounded_gain_and_disables_limiter_makeup() -> None:
    filter_chain, gain_db, ceiling_db = _true_peak_repair_filter(
        target_lufs=-14.0,
        measured_i=-13.73,
        true_peak_limit_db=-1.5,
        attempt_number=1,
    )
    assert gain_db == pytest.approx(-0.27)
    assert ceiling_db == pytest.approx(-2.05)
    assert filter_chain.startswith("volume=-0.27dB,alimiter=limit=")
    assert ":level=false:latency=true,aresample=48000" in filter_chain

    _, second_gain_db, second_ceiling_db = _true_peak_repair_filter(
        target_lufs=-14.0,
        measured_i=-17.5,
        true_peak_limit_db=-1.5,
        attempt_number=2,
    )
    assert second_gain_db == 2.0
    assert second_ceiling_db == pytest.approx(-2.25)


def test_ass_commerce_preset_and_pop_animation(tmp_path: Path) -> None:
    output = tmp_path / "commerce.ass"
    write_ass(
        [
            {
                "kind": "title",
                "preset": "hook",
                "animation": "pop",
                "start": 0.0,
                "end": 1.0,
                "text": "BUILT-IN CABLES",
            }
        ],
        output,
        1080,
        1920,
    )
    text = output.read_text(encoding="utf-8")
    assert "Style: Hook" in text
    assert r"\fscx118\fscy118" in text


def test_ass_fine_cut_typography_presets(tmp_path: Path) -> None:
    output = tmp_path / "fine-cut.ass"
    write_ass(
        [
            {
                "kind": "title",
                "preset": "fine_hook",
                "animation": "pop",
                "start": 0.0,
                "end": 1.0,
                "text": "STOP SCROLLING",
            },
            {
                "kind": "caption",
                "preset": "fine_caption",
                "start": 1.0,
                "end": 2.0,
                "text": "Built for summer heat",
            },
            {
                "kind": "label",
                "preset": "fine_accent",
                "start": 2.0,
                "end": 3.0,
                "text": "finally cool",
            },
            {
                "kind": "label",
                "preset": "fine_micro",
                "animation": "tag",
                "start": 2.0,
                "end": 3.0,
                "text": "VISUALIZACIÓN ILUSTRATIVA",
                "x": 70,
                "y": 160,
                "align": 7,
            },
            {
                "kind": "label",
                "preset": "fine_reaction",
                "animation": "bounce",
                "start": 1.2,
                "end": 2.0,
                "text": "😳",
                "x": 0.15,
                "y": 0.22,
                "align": 5,
            },
        ],
        output,
        1080,
        1920,
    )
    text = output.read_text(encoding="utf-8")
    assert "Style: FineHook,Anton" in text
    assert "Style: FineCaption,Montserrat ExtraBold" in text
    assert "Style: FineAccent,DM Serif Display" in text
    assert "Style: FineMicro,Montserrat SemiBold" in text
    assert "Style: FineReaction,Segoe UI Emoji,118" in text
    assert "Style: Feature,Arial,68" in text
    assert "Style: Badge,Arial Black,68" in text
    assert "Dialogue: 0,0:00:01.20,0:00:02.00,FineReaction" in text
    assert "😳" in text
    assert r"\frz-10" in text
    assert r"\move(0,160,70,160,0,180)" in text


def test_fine_cut_font_manifest_matches_assets() -> None:
    font_files = _verified_fine_cut_font_files()
    assert {path.name for path in font_files} == {
        "Anton-Regular.ttf",
        "DMSerifDisplay-Italic.ttf",
        "DMSerifDisplay-Regular.ttf",
        "Montserrat-ExtraBold.ttf",
        "Montserrat-Italic[wght].ttf",
        "Montserrat-SemiBold.ttf",
        "Montserrat[wght].ttf",
    }


def test_fine_cut_font_manifest_rejects_tampered_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied_assets = tmp_path / "fonts"
    shutil.copytree(render._FINE_CUT_FONT_ASSETS, copied_assets)
    target = copied_assets / "anton" / "Anton-Regular.ttf"
    payload = bytearray(target.read_bytes())
    payload[100] ^= 1
    target.write_bytes(payload)
    monkeypatch.setattr(render, "_FINE_CUT_FONT_ASSETS", copied_assets)

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        render._verified_fine_cut_font_files()


def test_fine_cut_font_manifest_rejects_path_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied_assets = tmp_path / "fonts"
    shutil.copytree(render._FINE_CUT_FONT_ASSETS, copied_assets)
    manifest_path = copied_assets / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["families"][0]["files"][0]["path"] = "../escaped.ttf"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(render, "_FINE_CUT_FONT_ASSETS", copied_assets)

    with pytest.raises(ValueError, match="escapes"):
        render._verified_fine_cut_font_files()


def test_fine_cut_font_staging_rejects_untracked_font(tmp_path: Path) -> None:
    font_dir = tmp_path / "work" / "fonts"
    font_dir.mkdir(parents=True)
    (font_dir / "rogue.ttf").write_bytes(b"not a tracked font")
    fake_job = SimpleNamespace(work=tmp_path / "work")

    with pytest.raises(ValueError, match="untracked files"):
        render._prepare_fine_cut_fonts(fake_job)


def _voiceover_spec() -> dict:
    return {
        "schema_version": 1,
        "job_id": "voice-test",
        "voice": "en-US-AvaMultilingualNeural",
        "target_audience": {
            "persona": "US commuters and travelers",
            "situation": "away from an outlet with a phone running low",
            "pain_point": "loose charging cables get tangled or forgotten",
            "desired_outcome": "simple grab-and-go backup power",
            "objections": ["Will I need a separate cable?"],
        },
        "angle": "pain_solution",
        "verified_claims": {
            "built_in_cables": "Supplied footage visibly shows cables attached to the product.",
            "battery_indicator": "Supplied footage visibly shows indicator lights.",
        },
        "forbidden_claims": ["battery capacity", "charging speed", "price"],
        "segments": [
            {
                "id": "hook",
                "role": "hook",
                "emotion": "playful",
                "text": "If your bag looks like a cable graveyard, watch this.",
            },
            {
                "id": "proof",
                "role": "proof",
                "emotion": "surprised",
                "text": "This little power bank keeps the charging cables built right in.",
                "claim_ids": ["built_in_cables"],
            },
            {
                "id": "benefit",
                "role": "benefit",
                "emotion": "relieved",
                "text": "Check the battery, pull out a cable, and skip the tangled mess.",
                "claim_ids": ["battery_indicator", "built_in_cables"],
            },
            {
                "id": "cta",
                "role": "cta",
                "emotion": "warm",
                "text": "If your phone always dies when you are out, tap the cart.",
            },
        ],
    }


def test_voiceover_strategy_passes_with_audience_proof_emotion_and_cta() -> None:
    spec = validate_voiceover_spec("voice-test", _voiceover_spec())
    result = assess_voiceover_spec(spec)
    assert result["ready_for_synthesis"] is True
    assert result["score"] >= 78
    assert result["distinct_emotions"] == 4
    assert spec["target_audience"]["objections"] == ["Will I need a separate cable?"]


def test_voiceover_strategy_rejects_unverified_claim() -> None:
    spec = _voiceover_spec()
    spec["segments"][1]["claim_ids"] = ["fast_charge"]
    with pytest.raises(ValueError, match="unverified claims"):
        validate_voiceover_spec("voice-test", spec)
