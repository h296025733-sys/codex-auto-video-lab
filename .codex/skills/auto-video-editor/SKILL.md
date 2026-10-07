---
name: auto-video-editor
description: Turn user-provided local video, image, and audio assets plus natural-language editing requirements into an isolated, auditable edit job, a validated JSON timeline, a rendered MP4, and a bounded local technical QA report. Use when the user asks Codex to automatically edit, trim, reframe, caption, sequence, score, or package uploaded media. Do not use for knowledge-base construction, social-platform publishing, or pure reference-video prompt writing.
---

# Auto Video Editor

## Objective

Produce a real local edit while preserving originals. Codex performs semantic decisions; the workspace CLI performs deterministic media analysis, validation, FFmpeg rendering, and technical QA.

Workspace root: `D:\workspace\codex-auto-video-lab`.

## Workflow

If the brief asks to learn from reference videos, perform `精剪`, build a tier/list overlay, time product cutouts to speech, or use layered caption motion, load `$tiktok-fine-cut-director` before drafting the timeline. Its fine-cut specification becomes the semantic input to this deterministic render workflow.

1. Read the user's requirements and resolve every supplied local asset path. Make a safe, concise job id. Do not edit the source files in place.
2. Run the workspace doctor if this is the first edit in the task:

   `powershell -ExecutionPolicy Bypass -File .\scripts\doctor.ps1`

3. Create the isolated job. Pass `--asset` once per source and include the user's exact brief:

   `.\studio.ps1 create --job <job-id> --asset <path> --brief <requirements>`

4. Analyze the job. Add `--transcribe` when speech meaning, captions, filler removal, or quote selection affects the edit:

   `.\studio.ps1 analyze --job <job-id> --transcribe`

5. Read `jobs\<job-id>\brief.txt`, `manifest.json`, `reports\analysis.json`, and relevant transcript JSON. Use scene candidates and silence spans as evidence, not as automatic editorial truth.
6. Generate the conservative draft:

   `.\studio.ps1 draft-plan --job <job-id>`

7. Edit `jobs\<job-id>\edit-plan.json` to satisfy the brief. Read [edit-plan.md](references/edit-plan.md) before changing the plan. Keep an `intent_summary` that explains selection, ordering, pacing, text, aspect ratio, and audio decisions. Never invent unavailable B-roll or factual claims.
8. Validate, render, and run QA:

   `.\studio.ps1 validate --job <job-id>`

   `.\studio.ps1 render --job <job-id>`

   `.\studio.ps1 qa --job <job-id>`

9. Inspect all paths in `reports\qa.json` under `sample_frames` and `critical_overlay_frames` with the local image viewer. If captions, crop, title safe areas, font glyphs, critical overlays, or black frames look wrong, revise the plan and rerender.
10. Deliver the MP4, edit plan, analysis, and QA paths. State separately what was actually executed: source inspection, transcription, local rendering, full decode, sampled-frame visual inspection, or platform playback. Never call technical QA a semantic or publishing verification.

## Decision Rules

- Make reasonable editorial assumptions when the brief leaves small details open; record them in `intent_summary`.
- Ask the user only when a missing choice would materially change the story, product claim, identity, or licensed asset usage.
- Prefer hard cuts for action, speech, proof, and ordinary montage. Use a non-cut transition only for a real time/place/chapter shift, soft bridge, or visibly matched directional movement; use text scale only on a supported hook, contrast, number, proof, payoff, or CTA. Use pace and shot selection before effects.
- Keep dialogue intelligible. Background music defaults near `-18 dB` with ducking enabled.
- For vertical short-form output, default to 1080x1920 at 30 fps. Use `contain` instead of destructive cropping when important content would leave frame.
- Do not burn new captions over source footage that already contains readable captions unless the user asks for restyling.
- Treat scene detection, silence detection, and speech recognition as fallible evidence. Check transcript boundaries before semantic cutting.

## Fast Baseline

For a request that explicitly needs no semantic deletion--only ordered concatenation, reframing, optional title, and captions--`baseline-auto` may run the whole mechanical pipeline. Label it as a baseline assembly, not custom editorial judgment:

`.\studio.ps1 baseline-auto --job <job-id> [--transcribe] [--title <text>] [--burn-captions]`

## Current Boundary

Supported: local video/image ingestion, probing, scene candidates, silence spans, optional offline transcription, segment trims, 0.25x-4x speed, fill/contain reframing, exact source muting or source gain, controlled push/pull motion, evidence-reasoned dissolve/dip/directional-slide transitions, styled animated titles/captions/labels, one exact semantic color run inside a caption/title/label with optional bounded `pulse` or `shake` during that word's real speaking window, paired numbered-step labels, generated voiceover, background music with narration ducking, timed sound effects, commerce color finishing, H.264/AAC MP4, full-decode QA, black-frame scan, audio-level scan, and sampled frames.

Not supported directly in this FFmpeg renderer: asset knowledge bases, generative B-roll, layered Remotion motion graphics, object tracking, multicam sync, editable Premiere/DaVinci project export, or platform publishing. Route layered Remotion work through `$openchatcut` as specified by `$tiktok-fine-cut-director`.
