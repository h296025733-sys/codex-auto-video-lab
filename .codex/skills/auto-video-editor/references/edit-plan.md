# Edit plan reference

The authoritative machine schema is `D:\workspace\codex-auto-video-lab\config\edit-plan.schema.json`. The CLI also performs runtime validation against actual media durations and paths.

## Top-level fields

- `schema_version`: always `1`.
- `job_id`: must match the job directory.
- `intent_summary`: concise editorial reasoning and explicit assumptions.
- `output`: MP4 name, even-numbered dimensions, 12–60 fps, encoder settings.
- `clips`: ordered visual timeline.
- `transitions`: optional motivated non-cut clip boundaries; an omitted boundary is a normal hard cut.
- `overlays`: timed title, caption, or label events on the final output timeline.
- `music`: `null` or a job-relative audio source with volume and ducking.
- `voiceover`: optional job-relative narration with final-timeline start and gain.
- `sfx`: optional timed effects with source trim bounds and gain.
- `finishing`: optional commerce color treatment and short transition flashes.

## Clips

Video clip example:

```json
{
  "source": "inputs/01_talking-head.mp4",
  "kind": "video",
  "start": 1.25,
  "end": 7.8,
  "speed": 1.0,
  "fit": "fill",
  "audio_gain_db": 0.0,
  "mute": false
}
```

Image clip example:

```json
{
  "source": "inputs/02_product.jpg",
  "kind": "image",
  "duration": 2.5,
  "speed": 1.0,
  "fit": "contain",
  "audio_gain_db": 0.0
}
```

Video `start` and `end` use source time. Overlay times use final output time after trims and speed changes. `fill` crops to the output aspect ratio; `contain` preserves the whole source and pads unused space. Set `mute` to `true` when the source audio must be completely excluded; the renderer replaces it with generated silence instead of merely lowering its volume.

Use `motion` for controlled push-ins or pull-outs. `zoom_start` and `zoom_end` accept 1.0-2.0, while normalized focus coordinates accept 0.0-1.0.

## Transition example

```json
{
  "after_clip": 1,
  "type": "dissolve",
  "duration": 0.22,
  "reason": "time_change"
}
```

Supported types are `dissolve`, `dip_to_black`, `slide_left`, and `slide_right`. These are real video/audio overlaps, so each duration shortens the final timeline. Use slides only with `reason=directional_motion` and visible same-direction evidence in both adjacent shots. Do not declare ordinary hard cuts in this array.

## Overlay example

```json
{
  "kind": "caption",
  "start": 0.4,
  "end": 2.2,
  "text": "先说结论：这个方法能省很多时间。"
}
```

Use `title` near the top, `caption` near the bottom, and `label` in the upper-left. Avoid overlapping title and source-burned text.
For commerce finishing, `preset` may be `hook`, `feature`, `badge`, `cta`, or `micro`; `animation` may use only the values admitted by the schema. Reference-driven English/Spanish fine cuts may also use `fine_caption` (Montserrat), `fine_hook` (Anton), `fine_accent` (DM Serif Display Italic), `fine_cta` (Anton), and `fine_micro` (Montserrat). A real numbered procedure may pair `fine_step_number` with `fine_step_action`; do not use those presets for an ordinary montage. Those presets use the project-local, license-tracked fonts bundled with `$tiktok-fine-cut-director`; they do not install fonts system-wide or fetch them at render time. Explicit `x`, `y`, and ASS `align` may place a callout while respecting platform UI. When both `x` and `y` are between `0` and `1`, they are normalized output coordinates (`0.5, 0.5` is the frame center); otherwise they are interpreted as absolute ASS PlayRes pixels for backward compatibility. Do not pre-convert normalized coordinates in callers.
Optional per-event `color` and `outline_color` values must use strict `#RRGGBB` syntax. They override the preset's primary text and outline colors for only that overlay event.

Each overlay may carry `effect_reason`: `readability`, `hook`, `pain`, `contrast`, `number`, `step`, `proof`, `payoff`, or `cta`. Ordinary captions use `readability`; an enlarged or kinetic text event must state the semantic job that earns the effect.

A readable `caption`, compact `title`, or factual `label` may include one `highlights` item containing an exact proper substring, one strict `#RRGGBB` color, `motion=none|pulse|shake`, and absolute final-output `start/end` inside its parent overlay. `none` is the normal static semantic color run. `pulse` renders one brief scale spring only while that exact word is spoken; `shake` renders one brief scale/rotation interruption and is reserved for the strongest supported hook, pain, or contrast peak. Prefer the large hook itself when it contains the strongest spoken word; do not animate the title, caption, and a sticker together. Neither motion animates the rest of the overlay. Use an empty array when no word deserves emphasis. Explicit newlines in `text` are preserved, so use at most one semantic line break and keep the display to one or two lines.

## Music example

```json
{
  "source": "inputs/03_music.mp3",
  "volume_db": -18.0,
  "ducking": true
}
```

Music loops to the output duration. Ducking lowers it under source dialogue; it does not prove broadcast loudness compliance.
Set `music.start` to select a beat-aligned source offset. When `voiceover` exists, ducking keys from narration. Timed `sfx` are mixed after ducking and passed through a final limiter.
