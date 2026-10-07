# Commerce voiceover direction

Text-to-speech is the last step, not the strategy. For a shoppable short-form edit, use this order.

Default audience languages are US English and US/Latin American Spanish unless the user specifies another market. Build separate native scripts and performance direction for each language; do not translate line by line.

## 1. Establish evidence

List only claims visibly demonstrated by the supplied assets or explicitly supplied by the user. Keep unsupported capacity, charging speed, compatibility, certification, price, discount, and shipping claims in `forbidden_claims`.

## 2. Choose one primary audience

Write down the persona, the moment when the problem occurs, the pain point, the desired outcome, and the main objections. A script aimed at "everyone" is not ready.

## 3. Choose one selling angle

Use one dominant angle per variant: pain-to-solution, UGC discovery, demonstration proof, comparison, confession, or warning. Generate multiple angle variants when performance testing is possible; do not cram them into one script.

Before scripting, write one evidence-bounded bridge:

`verified feature -> target situation -> present friction or desire -> truthful relief/gain -> one action`

A product parameter is not yet a reason to order. Do not invent urgency, scarcity, safety risk, fear, embarrassment, or a guaranteed outcome to fill a missing link. Scenario footage may illustrate the audience moment, but it does not prove a product capability.

## 4. Build the spoken beats

1. Hook: interrupt a relevant viewer in the opening seconds.
2. Problem: name a familiar frustration without exaggeration.
3. Proof: synchronize a concrete product claim with visible evidence.
4. Benefit: translate the feature into a life outcome for the target audience.
5. Objection: answer only an objection the footage or supplied facts can support.
6. CTA: tell the viewer what to do on TikTok Shop; mention promotions only when verified.

The hook should not merely announce a product category. Prefer a specific situation, open loop, surprising demonstration, or audience-identifying line.

## 5. Direct the performance

Give every beat an emotion and performance purpose. A useful commerce arc is tension or playful frustration, followed by surprise, confidence, relief, and warm urgency. Control pacing, pitch, volume, punctuation, emphasis, and silence per beat. Avoid shouting the entire script; contrast is what makes the CTA land.

Classify pauses before trimming them:

- `artifact`: synthetic break, duplicated blank, or edit defect; this is the only class that may be batch-compressed.
- `syntax`: phrase or clause boundary.
- `performance`: surprise, hesitation, emphasis, or relief.
- `structure`: hook-to-proof, case change, payoff, or proof-to-CTA.

Approve a natural-performance audition before picture lock. For this workspace and the current user preference, start a "slightly fast" audition at roughly `+3%` to `+5%` beside the natural version. Do not generate above `+8%` by default; if the user explicitly asks to explore it, make only a preview audition and block selection or delivery until the user listens and approves. These percentages are project-level synthesis starting points, not cross-model perceptual standards. A globally accelerated narration or completed picture master is never a runtime repair; requested speed-ramp aesthetics must be designed at specific beats or shots. Reject the faster take if diction, stress, emotion, breath, caption scanning, or proof comprehension worsens. If the script is too long, remove repetition or rewrite before increasing speed.

## 6. Keep words and pictures synchronized

Every proof statement must appear over the shot that proves it. Captions should compress the idea rather than blindly duplicate every word when the visual is already busy. Never cover the product with large overlays.

## 7. Audit before synthesis

Run `voiceover-check`. It is a structural heuristic, not a prediction of retention or sales. A passing plan still requires intelligibility checks, human listening, and eventually A/B results.

After synthesis, record more than a nominal speed value: measured duration, language-appropriate speech rate, phrase durations, speech occupancy, counts of meaningful pauses at or above 0.4 and 0.6 seconds, and the pauses at major logic changes. The current `voiceover-check` does not measure those rendered-audio properties; transcribe the audition and run `.codex/skills/tiktok-fine-cut-director/scripts/audit_transcript_pacing.py` as a separate post-synthesis audit. Its segment boundaries and text units are ASR proxies, not verified breaths or listener effort. Background music can fill low-level gaps, so waveform silence alone is not a pause audit. English and Spanish require separate listening and caption-readability checks.

## 8. Learn from outcomes without a knowledge base

For each published variant, record the hook and angle beside 2-second hold, 6-second hold, completion rate, product clicks, add-to-cart rate, and conversion rate. Compare like-for-like variants and change one major variable at a time. Until those metrics exist, the system can apply informed creative rules but cannot truthfully claim it has learned the account's winning voice.
