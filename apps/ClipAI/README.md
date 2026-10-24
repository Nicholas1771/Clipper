# ClipAI

ClipAI sits between ClipSourcer and ClipEditor. It reads newly sourced queue items and uses Google Gemini to decide whether a KICK overlay is already present, then writes a short three-paragraph SEO description for the clip.

Source watermark detection: ClipAI first measures (ffmpeg + Pillow, no AI) which areas of the source video never move, then sends one Gemini call with those areas numbered (`ai/watermark_candidates.jpg` and the zoomed `ai/watermark_candidates_zoom.jpg`) asking it to label each one. Only areas labelled as a watermark are blurred, always at the measured position. No still areas, an unsure answer, or Kick/Twitch branding means no blur.

The existing caption-detection call also transcribes readable on-screen captions from
the sampled frames (0.5, 1.5 and 3 seconds), without another API call. This text is saved
as `on_screen_caption` in `ai/analysis.json` and passed separately from the TikTok post
text into context analysis, Instagram summaries and YouTube caption generation.
Absent, unreadable or low-confidence captions contribute no text. YouTube metadata
backfill reuses saved on-screen text when available; it does not perform new OCR.

Kick strip placement: when no existing Kick overlay is detected, one `KICK_PLACEMENT_MODEL` call (default `gemini-2.5-flash`) sees all available sampled frames together. Each input has a 0-1000 vertical coordinate ruler and thin guide lines (`ai/kick_regions_<n>.jpg`) to help localization, not overlay candidate boxes. Flash reports a flat list of vertical intervals for faces, caption panels, readable UI text and important logos, not overlay positions. Code maps the source-relative intervals through fitting/letterboxing to 1080x1920 output and chooses the full-width strip position with greatest minimum clearance across all frames, within the default 0.10-0.80 travel range at 0.001 increments. Exact ties choose the lower position. A 12-output-pixel margin is tried first; if nothing fits, only that extra margin is removed, never the detected protected regions. Tight fits log a warning. The call uses a 2048-token thinking budget and 4096-token output limit. `ai/kick_placement_results.json` saves the response, input filenames, mapped intervals, selected fraction, margin, clearance and status. Numbered candidate grids and AI ranking are no longer generated. The editor honors the selected fraction. Invalid responses, missing frame assessments (including empty detections), or no fit log an explicit warning and retain the existing geometric fallback, which is not guaranteed to avoid all detected regions. Clips are not rejected. Placement geometry is read from the process/account env first, then `apps/ClipEditor/.env`. Region detection is approximate, and sampled frames do not guarantee whole-video safety.

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
