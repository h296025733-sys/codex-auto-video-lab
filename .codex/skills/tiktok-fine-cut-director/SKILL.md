---
name: tiktok-fine-cut-director
description: Analyze authorized reference videos and turn their evidence-backed pacing, typography, semantic overlays, caption motion, product-proof structure, and retention devices into a production-ready fine-cut specification for English- or Spanish-language TikTok/Reels/Shorts edits. Use when the user asks for 精剪, reference-driven editing, product-demo ads, talking-head explainers, pros/cons lists, tier lists, animated captions, timed product cutouts, or a more sophisticated cut than basic trimming and subtitles. Pair with auto-video-editor for local renders and OpenChatCut for layered Remotion timelines. Do not use to copy protected creative expression shot-for-shot, fabricate product evidence, identify an exact burned-in font, or claim conversion performance.
---

# TikTok Fine-Cut Director

## Objective

Convert reference-video evidence into an auditable editorial system: what must happen, why it happens, when it happens, and which rendering route can reproduce it. Preserve source files and distinguish measured facts, visual observations, editorial inference, and unverified claims.

Workspace root: `D:\workspace\codex-auto-video-lab`.

## Required reading

Read only the references needed for the task:

- For pacing, hooks, proof shots, lists, tier grids, and image timing, read [fine-cut-grammar.md](references/fine-cut-grammar.md).
- For English/Spanish typography, safe zones, emphasis, and motion tokens, read [typography-motion.md](references/typography-motion.md).
- For commerce positioning, audience relevance, and turning verified features into truthful reasons to order, read [commerce-message-architecture.md](references/commerce-message-architecture.md).
- Before choosing an editor or dependency, read [editor-routing.md](references/editor-routing.md).

## Workflow

1. Confirm that the supplied references and assets are local or otherwise authorized for analysis. Preserve originals and create an isolated job under `jobs\` as required by `$auto-video-editor`.
2. Run the workspace doctor, create the job, and run real analysis. Add transcription whenever speech controls cuts, captions, picture timing, or voiceover. For a read-only follow-up, reuse an existing isolated job only after its manifest paths and SHA256 values match the current inputs; do not rerun a writing analyzer merely to recreate unchanged evidence.
3. Run the bundled reference-style analyzer after the base analysis:

   `D:\workspace\codex-auto-video-lab\.venv\Scripts\python.exe .codex\skills\tiktok-fine-cut-director\scripts\analyze_reference_style.py --workspace D:\workspace\codex-auto-video-lab --job <job-id>`

4. Inspect the transcripts, contact sheets, source frames around candidate changes, and audio measurements. Scene scores and onset estimates are candidates, not edit decisions. Burned-in pixels cannot establish an exact font family, source project, editor, easing curve, music identity, or commercial outcome.

   When narration pace is part of the request or a generated voice will be delivered, audit the relevant faster-whisper transcript after synthesis:

   `D:\workspace\codex-auto-video-lab\.venv\Scripts\python.exe .codex\skills\tiktok-fine-cut-director\scripts\audit_transcript_pacing.py <transcript.json> --output <job>\reports\pacing-audit.json`

   The report measures transcript span, merged active time, phrase proxies, language-unit rates, speech occupancy, and gap counts. It does not hear emotion, establish comprehension, or define a universal good speed; pair it with human listening and caption/proof readability checks.
5. Choose one primary regime:

   - `product-proof`: fast physical demonstration, compact feature proof, product-first CTA.
   - `expert-overlay`: talking head with semantic captions, evidence cards, accumulating lists, or a persistent tier grid.
   - `hybrid`: product demonstration plus a speaker or voiceover-led argument.

   For commerce work, also write one evidence-bounded audience chain before scripting: `verified feature -> target situation -> present friction or desire -> truthful relief/gain -> action`. Product parameters alone are not a buying reason, and urgency, fear, scarcity, safety risk, or universal outcomes may not be invented.

6. When the request includes an edit direction, production plan, timeline, or render, build a `fine-cut-spec.json` using [fine-cut-spec.schema.json](assets/fine-cut-spec.schema.json). Select its render `route`; record the claim ledger and rights scope; and make every retention event state its output time, confidence, spoken idea, visual job, source asset/timecodes (or explicit nulls for a pending capture), claim IDs, shot/overlay, text treatment, motion, and exit condition. Mark every generated or stock insert as illustrative or non-illustrative and define its claim boundary. For tutorial learning or reference review that asks only for findings, deliver a timecoded evidence memo instead of inventing a production spec.
   Validate it before timeline work:

   `D:\workspace\codex-auto-video-lab\.venv\Scripts\python.exe .codex\skills\tiktok-fine-cut-director\scripts\validate_fine_cut_spec.py <path-to-fine-cut-spec.json>`

   The bundled validator checks this skill's deterministic core contract without installing a general JSON Schema dependency. A successful result checks the declared structure, enums, time/source ranges, claim references, music-policy linkage, referenced-asset rights entries, `reference_only` exclusions, and verification fields. It does not independently prove the truth of a claim, license validity, release validity, editorial quality, or platform behavior.
7. Apply a truth-first visual hierarchy:

   - Prefer real demonstrations, product close-ups, labels, packaging, and user-provided cutouts as proof.
   - Add a diagram, icon, product PNG, or generated image only when it answers “what is it, who is it for, why, where is the evidence, or what is the result?”
   - Use ImageGen only for truthful explanatory or decorative assets. Never use it to invent a product result, testimonial, certification, ingredient, before/after, UI state, or physical capability.
   - Treat generated assets as separate local files with provenance in the spec.
   - If a reasonable viewer could mistake a generated insert for a real product, person, scene, result, or piece of evidence, label it visibly as illustrative as well as recording its provenance. Obvious icons, abstract textures, and clearly non-photoreal decorative plates only require the spec record.

8. Route the production:

   - Use `$auto-video-editor` and FFmpeg/libass for deterministic hard cuts, reframing, basic captions, labels, voiceover, music/SFX, color, and technical QA.
   - For its English/Spanish local-font path, use `fine_caption`, `fine_hook`, `fine_accent`, and `fine_cta` overlay presets. They load the pinned Montserrat, Anton, and DM Serif Display files from this skill's D-drive assets without a system font installation or runtime download.
   - Use `$openchatcut` when the cut needs layered Remotion composition: phrase-level style changes, spring/scale motion, gradient or mask text, timed transparent product cutouts, persistent lists/grids, arrows, paths, or picture-in-picture evidence.
   - Do not install another framework simply because a reference contains an effect. First use the verified local stack described in `editor-routing.md`.
   - The fine-cut specification is the semantic contract; it is not a render timeline. Resolve accepted source ranges, caption cues, layer order, and exact transforms into the validated `edit-plan.json` or OpenChatCut timeline before rendering.

9. Audio is editorial evidence, not a guess. Use only user-supplied, licensed, platform-cleared, or otherwise verified music. If a suitable track and rights cannot be established, produce voiceover-only or silent output rather than arbitrary music. Design voiceover natively for `en-US` or `es-US`/Latin American Spanish and follow the [commerce voiceover workflow](../../../docs/commerce-voiceover-direction.md). Approve the natural-performance voiceover before locking picture duration. Do not globally accelerate narration or the completed visual master to force a fixed runtime; audition any "slightly fast" version as a small, separate candidate and reject it when diction, emotion, breath, caption scanning, or proof comprehension worsens.
10. Validate the spec, render the actual timeline, perform a full decode, inspect sampled frames and critical overlay moments, and check dialogue intelligibility and peaks. State separately what was source-inspected, automatically measured, visually judged, locally rendered, and not tested on a social platform.

## Quality gates

- The first visual state is immediately readable; no setup or hand-positioning waste is retained.
- Each inserted picture or graphic has a semantic trigger and an exit condition.
- A longer shot contains visible internal change or carries necessary comprehension time.
- Art typography is sparse and semantic; ordinary captions remain fast to read.
- Automatic captions are manually repaired into natural phrases before styling. When an art/keyword layer fully replaces a spoken phrase, the duplicate baseline caption is hidden for that interval.
- Major transitions indicate a structural change. Ordinary sentences use clean cuts or restrained jump cuts.
- Product rankings, pros/cons, and steps persist as spatial memory instead of disappearing after each subtitle.
- Voiceover and picture pacing have contrast: proof actions receive enough time to complete, list/montage beats may be faster, and CTA/product identity receives a stable read. A single global speed conform is not accepted as rhythm design.
- Every commerce angle can trace a verified product fact to one audience situation and one honest reason to care; unsupported fear, urgency, scarcity, safety, or guaranteed outcomes are excluded.
- Claims are traceable to user instructions, packaging, speech, or visible source evidence.
- Only claim IDs marked `verified` may enter a retention event or final render. `conditional` claims block use until evidence is supplied; `forbidden` claims never enter the timeline.
- Music, fonts, images, animations, and models have a recorded source and license or are omitted.
- The final report never presents local technical QA as proof of persuasion, conversion, rights clearance, or TikTok acceptance.

## Output contract

For tutorial learning or reference review with no requested edit plan, deliver a timecoded evidence memo and limitations; a `fine-cut-spec.json` is optional. For analysis intended to drive a future edit, deliver the evidence report and validated `fine-cut-spec.json`; do not imply that a video was rendered. For an editing task, also deliver the validated timeline, rendered MP4, QA report, and the exact checks actually executed.
