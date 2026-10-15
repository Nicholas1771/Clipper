# ClipAI

ClipAI sits between ClipSourcer and ClipEditor. It reads newly sourced queue items and uses Google Gemini to decide whether a KICK overlay is already present, then writes a short three-paragraph SEO description for the clip.

Source watermark detection: ClipAI first measures (ffmpeg + Pillow, no AI) which areas of the source video never move, then sends one Gemini call with those areas numbered (`ai/watermark_candidates.jpg` and the zoomed `ai/watermark_candidates_zoom.jpg`) asking it to label each one. Only areas labelled as a watermark are blurred, always at the measured position. No still areas, an unsure answer, or Kick/Twitch branding means no blur.

Kick strip placement: the caption call also returns the vertical extent of every caption/title text block (`caption_bands`; `caption_position` is `BOTH` when there is text at the top and bottom). When ClipAI adds the Kick strip, it draws numbered strip positions on three frames (`ai/kick_candidates.jpg`). The positions always lie between `KICK_OVERLAY_TOP_Y` and `KICK_OVERLAY_BOTTOM_Y` and never touch the account watermark. One Gemini call labels which positions hide no captions, faces or on-screen graphics. The clear position nearest the configured spot is saved as `kick_overlay_y`. If nothing is confirmed clear, the editor falls back to its own placement. These settings are read from the process/profile env first, then `apps/ClipEditor/.env`.

Requirements:
- Set `GOOGLE_API_KEY` with a valid Google AI key.
- `QUEUE_DIR` should match the shared queue directory used by the rest of the pipeline.

Typical run:

```powershell
cd apps\ClipAI
python main.py
```

`--once` runs a single AI pass and exits.
