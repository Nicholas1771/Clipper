# ClipAI

ClipAI sits between ClipSourcer and ClipEditor. It reads newly sourced queue items and uses Google Gemini to decide whether a KICK overlay is already present, then writes a short three-paragraph SEO description for the clip.

Requirements:
- Set `GOOGLE_API_KEY` with a valid Google AI key.
- `QUEUE_DIR` should match the shared queue directory used by the rest of the pipeline.

Typical run:

```powershell
cd apps\ClipAI
python main.py
```

`--once` runs a single AI pass and exits.
