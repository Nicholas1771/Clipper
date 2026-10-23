# ClipAI

ClipAI sits between ClipSourcer and ClipEditor. It reads newly sourced queue items and uses Google Gemini to decide whether a KICK overlay is already present, then writes a short three-paragraph SEO description for the clip.

Source watermark detection: ClipAI first measures (ffmpeg + Pillow, no AI) which areas of the source video never move, then sends one Gemini call with those areas numbered (`ai/watermark_candidates.jpg` and the zoomed `ai/watermark_candidates_zoom.jpg`) asking it to label each one. Only areas labelled as a watermark are blurred, always at the measured position. No still areas, an unsure answer, or Kick/Twitch branding means no blur.

The existing caption-detection call also transcribes readable on-screen captions from
the sampled frames (0.5, 1.5 and 3 seconds), without another API call. This text is saved
as `on_screen_caption` in `ai/analysis.json` and passed separately from the TikTok post
text into context analysis, Instagram summaries and YouTube caption generation.
Absent, unreadable or low-confidence captions contribute no text. YouTube metadata
backfill reuses saved on-screen text when available; it does not perform new OCR.

Kick strip placement: all positions from two staggered grids are sent to AI, without caption/watermark filtering. Each grid is non-overlapping with a gap of 7.5% of strip height; the second grid lies halfway between consecutive first-grid positions. The default range is 0.10-0.80 of available overlay travel. Candidate boxes match the full-width strip's integer-scaled height; previews are half-size with left-aligned numbers. `ai/kick_candidates.jpg` is an overview only. Each of three sampled timestamps gets its own `KICK_PLACEMENT_MODEL` call (default `gemini-3.1-pro-preview`), with two separate images: `kick_frame_<n>_primary.jpg` and `kick_frame_<n>_staggered.jpg`. Numbering continues across grids and is consistent across calls. AI ranks clear positions and supplies confidence per position. A candidate must be clear with confidence >=0.70 in every frame. Its combined score is the mean of `confidence * (N - zero_based_rank) / N`, where N is the total candidate count; highest score wins, with candidate number breaking exact ties. `kick_placement_results.json` saves all responses, fractions and aggregate scores. The concise prompt protects faces/text/graphics, prefers safe still-photo/background areas and caption adjacency, and describes account-watermark context. Single-frame calls cannot reliably determine motion. The editor honors the AI winner without content-based vetoes. Invalid responses or no common clear candidate retain the existing geometric fallback; clips are not rejected. Placement geometry is read from the process/account env first, then `apps/ClipEditor/.env`. Sampled frames do not guarantee whole-video safety.

Requirements:
- Set `GOOGLE_API_KEY` with a valid Google AI key.
- `QUEUE_DIR` should match the shared queue directory used by the rest of the pipeline.

Typical run:

```powershell
cd apps\ClipAI
python main.py
```

`--once` runs a single AI pass and exits.
### Audio/context generation limits

The audio/context request keeps its transcription, context-sufficiency, and person-hashtag
schema and inputs. It uses a 4096-token output cap and disables thinking specifically
for Gemini 2.5 Flash models (including Flash-Lite); other configured context models retain
their default thinking behavior for API compatibility. Other AI requests retain their
existing generation settings and the configured request timeout is unchanged.
Truncated (`MAX_TOKENS`) responses are rejected before parsing, even if they contain valid
JSON. Audio/context truncation retries once with the same limits, then fails explicitly;
it does not enter the temporary-service retry loop. The prompt forbids invented speech
and extending repetitions beyond the audio. Disabling thinking can change model judgments;
it preserves the task and schema, not guaranteed identical classification accuracy.
