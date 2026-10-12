from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from imageio_ffmpeg import get_ffmpeg_exe

from common.alerts import send_failure_alert
from common.platforms import YOUTUBE, read_slot_platforms
from clipai.config import AppConfig

logger = logging.getLogger(__name__)


class GeminiRateLimitError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


MAX_RETRIES = 5
MAX_BACKOFF_SECONDS = 900


class GeminiRetryableServiceError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class ClipAIService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._tz = ZoneInfo(config.timezone_name)

    def _queue_state_path(self, run_dir: Path) -> Path:
        return run_dir / "queue_state.json"

    def _analysis_path(self, run_dir: Path) -> Path:
        return run_dir / "ai" / "analysis.json"

    def _summary_path(self, run_dir: Path) -> Path:
        return run_dir / "ai" / "summary.txt"

    def _youtube_description_path(self, run_dir: Path) -> Path:
        return run_dir / "ai" / "youtube_description.txt"

    def _youtube_title_path(self, run_dir: Path) -> Path:
        return run_dir / "ai" / "youtube_title.txt"

    def _manifest_path(self, run_dir: Path) -> Path | None:
        source_dir = run_dir / "source"
        candidates = sorted(source_dir.glob("*.json"), key=lambda path: path.name)
        if candidates:
            return candidates[0]
        candidates = sorted(run_dir.glob("*.json"), key=lambda path: path.name)
        return candidates[0] if candidates else None

    def _ready_items(self) -> list[Path]:
        if not self.config.queue_dir.exists():
            return []
        items: list[Path] = []
        for child in sorted(self.config.queue_dir.iterdir(), key=lambda path: (path.stat().st_mtime, path.name), reverse=True):
            if not child.is_dir() or child.name in {"processing", "Archive", "Test", "source"}:
                continue
            state = _read_json(self._queue_state_path(child))
            if state.get("phase") not in {"sourced", "source_ready"}:
                continue
            if self._manifest_path(child) is None:
                continue
            items.append(child)
        return items

    def _trim_fallback_text(self, value: str) -> str:
        cleaned = re.sub(r"\s+", " ", value).strip()
        return cleaned

    def _strip_hashtags_and_emojis(self, value: str) -> str:
        without_hashtags = re.sub(r"(?<!\w)#[^\s#]+", " ", value)
        without_emoji = re.sub(
            "["
            "\U0001F300-\U0001F5FF"
            "\U0001F600-\U0001F64F"
            "\U0001F680-\U0001F6FF"
            "\U0001F700-\U0001F77F"
            "\U0001F780-\U0001F7FF"
            "\U0001F800-\U0001F8FF"
            "\U0001F900-\U0001F9FF"
            "\U0001FA00-\U0001FA6F"
            "\U0001FA70-\U0001FAFF"
            "\U00002600-\U000026FF"
            "\U00002700-\U000027BF"
            "]+",
            " ",
            without_hashtags,
        )
        return self._trim_fallback_text(without_emoji)

    def _summarize_context(self, manifest: dict[str, Any]) -> str:
        text = self._strip_hashtags_and_emojis(str(manifest.get("text") or ""))
        return text

    def _read_prompt_template(self, filename: str) -> str:
        if self.config.prompts_dir is None:
            raise FileNotFoundError(f"Prompts directory not found; cannot load {filename}.")
        content = (self.config.prompts_dir / filename).read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError(f"Prompt file {filename} is empty.")
        return content

    def _fill_prompt(self, template: str, replacements: dict[str, str]) -> str:
        prompt = template
        for key, value in replacements.items():
            prompt = prompt.replace(f"__{key}__", value)
        return prompt

    def _build_overlay_prompt(self, context: str) -> str:
        template = self._read_prompt_template("overlay_detection.txt")
        return self._fill_prompt(template, {"CONTEXT": context or "No metadata provided."})

    def _build_watermark_prompt(self, author_handle: str) -> str:
        template = self._read_prompt_template("watermark_detection.txt")
        return self._fill_prompt(template, {"AUTHOR": author_handle or "unknown"})

    def _build_caption_prompt(self, context: str) -> str:
        template = self._read_prompt_template("caption_detection.txt")
        return self._fill_prompt(template, {"CONTEXT": context or "No metadata provided."})

    def _build_context_prompt(self, caption: str) -> str:
        template = self._read_prompt_template("context_analysis.txt")
        return self._fill_prompt(template, {"CAPTION": caption or "No caption provided.", "STREAMER": self.config.streamer_name})

    def _build_summary_prompt(self, caption: str, transcript: str, context_sufficient: bool) -> str:
        template = self._read_prompt_template("summary_clip.txt" if context_sufficient else "summary_streamer.txt")
        return self._fill_prompt(
            template,
            {"CAPTION": caption, "TRANSCRIPT": transcript, "STREAMER": self.config.streamer_name},
        )

    def _build_youtube_prompt(self, caption: str, transcript: str, context_sufficient: bool) -> str:
        template = self._read_prompt_template("youtube_description.txt")
        return self._fill_prompt(
            template,
            {
                "CAPTION": caption,
                "TRANSCRIPT": transcript,
                "STREAMER": self.config.streamer_name,
                "CONTEXT_SUFFICIENT": "yes" if context_sufficient else "no",
            },
        )

    def _extract_retry_delay_seconds(self, payload_error: dict[str, Any]) -> int | None:
        details = payload_error.get("error", {}).get("details", [])
        if not isinstance(details, list):
            return None
        for item in details:
            if not isinstance(item, dict):
                continue
            raw_delay = item.get("retryDelay")
            if not isinstance(raw_delay, str):
                continue
            match = re.fullmatch(r"(\d+)s", raw_delay.strip())
            if match:
                return int(match.group(1))
        return None

    def _call_gemini(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any] | None = None,
        image_paths: list[Path] | None = None,
        audio_paths: list[Path] | None = None,
        model: str | None = None,
        plain_text: bool = False,
    ) -> dict[str, Any]:
        """Returns parsed JSON, or {"text": ...} when plain_text is set (free-form output keeps length and paragraphs)."""
        if not self.config.google_api_key:
            raise ValueError("GOOGLE_API_KEY is required before ClipAI can analyze queue items.")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model or self.config.gemini_model}:generateContent?key={self.config.google_api_key}"
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for image_path in image_paths or []:
            if not image_path.exists():
                continue
            mime_type = "image/jpeg"
            if image_path.suffix.lower() == ".png":
                mime_type = "image/png"
            data = base64.b64encode(image_path.read_bytes()).decode("utf-8")
            parts.append({"inline_data": {"mime_type": mime_type, "data": data}})
        for audio_path in audio_paths or []:
            if audio_path.exists():
                data = base64.b64encode(audio_path.read_bytes()).decode("utf-8")
                parts.append({"inline_data": {"mime_type": "audio/wav", "data": data}})

        payload: dict[str, Any] = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": 0.2,
                "topP": 0.9,
            },
        }
        if response_schema is not None and not plain_text:
            payload["generationConfig"]["responseMimeType"] = "application/json"
            payload["generationConfig"]["responseSchema"] = response_schema

        try:
            response = requests.post(url, json=payload, timeout=(10, self.config.timeout_seconds))
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
            raise GeminiRetryableServiceError(f"Gemini request failed due to a network error: {error}") from error
        if response.status_code >= 400:
            body = response.text
            payload_error: dict[str, Any] = {}
            try:
                payload_error = response.json()
                body = json.dumps(payload_error, indent=2)
            except ValueError:
                pass
            status = str(payload_error.get("error", {}).get("status", "")).upper()
            if response.status_code == 429 or status == "RESOURCE_EXHAUSTED":
                retry_after_seconds = self._extract_retry_delay_seconds(payload_error)
                raise GeminiRateLimitError(
                    f"Gemini quota exhausted (HTTP {response.status_code}). Response: {body}",
                    retry_after_seconds=retry_after_seconds,
                )
            if response.status_code in {500, 502, 503, 504} or status in {"UNAVAILABLE", "DEADLINE_EXCEEDED"}:
                retry_after_seconds = self._extract_retry_delay_seconds(payload_error)
                raise GeminiRetryableServiceError(
                    f"Gemini service is temporarily unavailable (HTTP {response.status_code}). Response: {body}",
                    retry_after_seconds=retry_after_seconds,
                )
            raise RuntimeError(f"Gemini API request failed: HTTP {response.status_code}. Response: {body}")

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini API returned no candidates: {data}")

        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        if not parts:
            raise RuntimeError(f"Gemini API returned empty content: {data}")

        raw_text = parts[0].get("text")
        if not raw_text:
            raise RuntimeError(f"Gemini API returned no text payload: {data}")

        if plain_text:
            return {"text": raw_text}
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```\s*$", "", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Gemini response was not valid JSON: {cleaned}") from exc

    def _source_video_path(self, run_dir: Path) -> Path | None:
        source_dir = run_dir / "source"
        candidates = sorted(source_dir.glob("*.mp4"), key=lambda path: path.name)
        if candidates:
            return candidates[0]
        candidates = sorted(run_dir.glob("*.mp4"), key=lambda path: path.name)
        return candidates[0] if candidates else None

    def _extract_frame(self, video_path: Path, *, output_path: Path, seek_seconds: float = 1.0) -> Path | None:
        ffmpeg_exe = str(get_ffmpeg_exe())
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            ffmpeg_exe,
            "-y",
            "-ss",
            f"{seek_seconds:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output_path),
        ]
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0:
            logger.warning("Frame extraction failed for %s: %s", video_path, completed.stderr.strip() or completed.stdout.strip())
            return None
        return output_path if output_path.exists() else None

    def _extract_sample_frames(self, run_dir: Path, video_path: Path) -> list[Path]:
        sample_points = [0.5, 1.5, 3.0]
        extracted: list[Path] = []
        for index, seconds in enumerate(sample_points, start=1):
            frame_path = run_dir / "ai" / f"reference_frame_{index}.jpg"
            frame = self._extract_frame(video_path, output_path=frame_path, seek_seconds=seconds)
            if frame is not None:
                extracted.append(frame)
        return extracted

    def _normalize_caption_position(self, value: Any) -> str:
        normalized = str(value or "").strip().upper()
        if normalized in {"TOP", "MIDDLE", "BOTTOM", "NONE"}:
            return normalized
        return "NONE"

    def _normalize_confidence(self, value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0.0
        if numeric < 0:
            return 0.0
        if numeric > 1:
            return 1.0
        return numeric

    def _collect_source_frames(self, run_dir: Path | None) -> list[Path]:
        if run_dir is None:
            return []
        video_path = self._source_video_path(run_dir)
        if video_path is None:
            return []
        return self._extract_sample_frames(run_dir, video_path)

    def _detect_overlay_visual(self, manifest: dict[str, Any], *, run_dir: Path | None = None) -> dict[str, Any]:
        context = self._summarize_context(manifest)
        image_paths: list[Path] = self._collect_source_frames(run_dir)
        schema = {
            "type": "OBJECT",
            "properties": {
                "has_kick_overlay": {"type": "BOOLEAN"},
                "confidence": {"type": "NUMBER"},
                "evidence": {
                    "type": "OBJECT",
                    "properties": {
                        "kick_wordmark_visible": {"type": "BOOLEAN"},
                        "black_bar_visible": {"type": "BOOLEAN"},
                        "url_text_visible": {"type": "BOOLEAN"},
                        "same_frame": {"type": "BOOLEAN"},
                    },
                    "required": ["kick_wordmark_visible", "black_bar_visible", "url_text_visible", "same_frame"],
                },
            },
            "required": ["has_kick_overlay", "confidence", "evidence"],
        }
        payload = self._call_gemini(self._build_overlay_prompt(context), response_schema=schema, image_paths=image_paths)
        evidence = payload.get("evidence")
        evidence_payload = evidence if isinstance(evidence, dict) else {}
        confidence = self._normalize_confidence(payload.get("confidence"))
        has_all_evidence = (
            bool(evidence_payload.get("kick_wordmark_visible", False))
            and bool(evidence_payload.get("black_bar_visible", False))
            and bool(evidence_payload.get("url_text_visible", False))
            and bool(evidence_payload.get("same_frame", False))
        )
        strict_overlay = bool(payload.get("has_kick_overlay", False)) and has_all_evidence and confidence >= 0.85
        return {
            "has_kick_overlay": strict_overlay,
            "overlay_confidence": confidence,
            "overlay_evidence": {
                "kick_wordmark_visible": bool(evidence_payload.get("kick_wordmark_visible", False)),
                "black_bar_visible": bool(evidence_payload.get("black_bar_visible", False)),
                "url_text_visible": bool(evidence_payload.get("url_text_visible", False)),
                "same_frame": bool(evidence_payload.get("same_frame", False)),
            },
        }

    def _detect_caption_position(self, manifest: dict[str, Any], *, run_dir: Path | None = None) -> dict[str, Any]:
        context = self._summarize_context(manifest)
        image_paths: list[Path] = self._collect_source_frames(run_dir)
        schema = {
            "type": "OBJECT",
            "properties": {
                "has_caption": {"type": "BOOLEAN"},
                "caption_position": {"type": "STRING"},
                "confidence": {"type": "NUMBER"},
            },
            "required": ["has_caption", "caption_position", "confidence"],
        }
        payload = self._call_gemini(self._build_caption_prompt(context), response_schema=schema, image_paths=image_paths)
        caption_position = self._normalize_caption_position(payload.get("caption_position"))
        has_caption = bool(payload.get("has_caption", False))
        confidence = self._normalize_confidence(payload.get("confidence"))
        if caption_position == "NONE" or confidence < 0.5:
            has_caption = False
            caption_position = "NONE"
        if not has_caption:
            caption_position = "NONE"
        return {
            "has_caption": has_caption,
            "caption_position": caption_position,
            "caption_confidence": confidence,
        }

    def _parse_watermark_box(self, value: Any) -> dict[str, float] | None:
        """Convert Gemini's [ymin, xmin, ymax, xmax] (0-1000) into a normalized x/y/w/h box."""
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        try:
            ymin, xmin, ymax, xmax = (max(0.0, min(1000.0, float(item))) / 1000.0 for item in value)
        except (TypeError, ValueError):
            return None
        width, height = xmax - xmin, ymax - ymin
        # A creator watermark is small; reject empty or implausibly large boxes.
        if width <= 0.01 or height <= 0.005 or width * height > 0.15:
            return None
        return {"x": round(xmin, 4), "y": round(ymin, 4), "w": round(width, 4), "h": round(height, 4)}

    def _detect_source_watermark(self, manifest: dict[str, Any], *, run_dir: Path | None = None) -> dict[str, Any]:
        """Check a single frame for a burned-in creator watermark (moving watermarks are not handled)."""
        none = {"has_source_watermark": False, "source_watermark_box": None, "source_watermark_confidence": 0.0}
        if run_dir is None:
            return none
        frame = run_dir / "ai" / "reference_frame_2.jpg"
        if not frame.exists():
            return none
        schema = {
            "type": "OBJECT",
            "properties": {
                "has_watermark": {"type": "BOOLEAN"},
                "confidence": {"type": "NUMBER"},
                "box_2d": {"type": "ARRAY", "items": {"type": "NUMBER"}},
            },
            "required": ["has_watermark", "confidence"],
        }
        author = str(manifest.get("author_handle") or "").strip()
        payload = self._call_gemini(
            self._build_watermark_prompt(author),
            response_schema=schema,
            image_paths=[frame],
            model=self.config.watermark_model,
        )
        confidence = self._normalize_confidence(payload.get("confidence"))
        box = self._parse_watermark_box(payload.get("box_2d"))
        if not bool(payload.get("has_watermark", False)) or confidence < 0.7 or box is None:
            return {**none, "source_watermark_confidence": confidence}
        box = self._refine_watermark_box(frame, box, schema, run_dir) or box
        return {"has_source_watermark": True, "source_watermark_box": box, "source_watermark_confidence": confidence}

    def _refine_watermark_box(self, frame: Path, box: dict[str, float], schema: dict[str, Any], run_dir: Path) -> dict[str, float] | None:
        """Re-locate the watermark inside a zoomed crop around the first guess, since full-frame boxes can be vertically off."""
        try:
            from PIL import Image
        except ImportError:
            return None
        try:
            with Image.open(frame) as image:
                width, height = image.size
                pad_y = max(0.08, box["h"] * 2)
                top = max(0.0, box["y"] - pad_y)
                bottom = min(1.0, box["y"] + box["h"] + pad_y)
                crop = image.crop((0, int(top * height), width, int(bottom * height)))
                scale = max(1, round(1600 / max(1, crop.width)))
                crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
                crop_path = run_dir / "ai" / "watermark_crop.png"
                crop.save(crop_path)
            prompt = self._read_prompt_template("watermark_crop.txt")
            payload = self._call_gemini(prompt, response_schema=schema, image_paths=[crop_path], model=self.config.watermark_model)
            local = self._parse_watermark_box(payload.get("box_2d"))
            if local is None or not bool(payload.get("has_watermark", False)):
                return None
            span = bottom - top
            # Keep the coarse horizontal extent (reliable); only the vertical position needs the zoomed pass.
            center_y = top + (local["y"] + local["h"] / 2) * span
            new_h = max(local["h"] * span, box["h"], 0.02)
            return {
                "x": round(min(box["x"], local["x"]), 4),
                "y": round(max(0.0, center_y - new_h / 2), 4),
                "w": round(max(box["x"] + box["w"], local["x"] + local["w"]) - min(box["x"], local["x"]), 4),
                "h": round(new_h, 4),
            }
        except Exception:
            return None

    def _analyze_visual(self, manifest: dict[str, Any], *, run_dir: Path | None = None) -> dict[str, Any]:
        overlay = self._detect_overlay_visual(manifest, run_dir=run_dir)
        caption = self._detect_caption_position(manifest, run_dir=run_dir)
        watermark = self._detect_source_watermark(manifest, run_dir=run_dir)
        combined = dict(overlay)
        combined.update(caption)
        combined.update(watermark)
        return combined

    def _extract_audio(self, video_path: Path, output_path: Path) -> Path | None:
        """Mono 16 kHz wav (about 2 MB/min); Gemini transcribes it at roughly 32 tokens per second of audio."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [str(get_ffmpeg_exe()), "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", str(output_path)]
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0 or not output_path.exists():
            logger.info("No usable audio in %s; using the caption only.", video_path)
            return None
        return output_path

    def _analyze_clip_context(self, manifest: dict[str, Any], *, run_dir: Path) -> dict[str, Any]:
        """One call: transcript plus whether the audio and caption describe a clear, specific event."""
        caption = self._summarize_context(manifest)
        video_path = self._source_video_path(run_dir)
        audio = self._extract_audio(video_path, run_dir / "ai" / "audio.wav") if video_path else None
        schema = {
            "type": "OBJECT",
            "properties": {
                "transcript": {"type": "STRING"},
                "context_sufficient": {"type": "BOOLEAN"},
                "person_hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
            "required": ["context_sufficient"],
        }
        try:
            payload = self._call_gemini(
                self._build_context_prompt(caption),
                response_schema=schema,
                audio_paths=[audio] if audio else None,
                model=self.config.context_model,
            )
        finally:
            if audio is not None:
                audio.unlink(missing_ok=True)
        return {
            "caption": caption,
            "transcript": str(payload.get("transcript") or "").strip(),
            "context_sufficient": bool(payload.get("context_sufficient", False)),
            "hashtags": self._normalize_hashtags(payload.get("person_hashtags")),
        }

    def _normalize_hashtags(self, value: Any) -> list[str]:
        streamer = re.sub(r"[^a-z0-9]", "", self.config.streamer_name.lower())
        tags: list[str] = []
        for item in value if isinstance(value, list) else []:
            tag = re.sub(r"[^a-z0-9]", "", str(item).lower())
            if tag and tag != streamer and tag not in tags:
                tags.append(tag)
        return tags[:2]

    def _generate_summary(self, caption: str, transcript: str, context_sufficient: bool) -> str:
        payload = self._call_gemini(
            self._build_summary_prompt(caption, transcript, context_sufficient), plain_text=True
        )
        summary = str(payload.get("text") or "").replace("\r", "\n").strip()
        if not summary:
            raise RuntimeError("Gemini returned an empty summary.")
        parts = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n\s*\n", summary)]
        return "\n\n".join(part for part in parts if part)

    def _generate_youtube_description(self, caption: str, transcript: str, context_sufficient: bool) -> str:
        payload = self._call_gemini(
            self._build_youtube_prompt(caption, transcript, context_sufficient), plain_text=True
        )
        text = str(payload.get("text") or "").replace("\r", "\n").strip()
        if not text:
            raise RuntimeError("Gemini returned an empty YouTube description.")
        parts = [re.sub(r"[ \t]+", " ", part).strip() for part in re.split(r"\n\s*\n", text)]
        return "\n\n".join(part for part in parts if part)

    def _youtube_title(self, caption: str) -> str:
        """Title (100 character YouTube limit) from the cleaned source caption, cut on a word boundary."""
        title = re.sub(r"\s+", " ", caption).strip().replace("<", "").replace(">", "")
        if not title:
            return f"{self.config.streamer_name} clip".strip() or "Clip"
        if len(title) > 100:
            title = title[:100].rsplit(" ", 1)[0].rstrip(" ,;:-") or title[:100]
        return title

    def _write_youtube_files(self, run_dir: Path, clip_context: dict[str, Any], fallback_description: str) -> str:
        description = fallback_description
        try:
            description = self._generate_youtube_description(
                clip_context["caption"], clip_context["transcript"], clip_context["context_sufficient"]
            )
        except Exception:
            logger.exception("Could not generate the YouTube description for %s; using the Instagram summary instead.", run_dir.name)
        self._youtube_description_path(run_dir).write_text(description, encoding="utf-8")
        self._youtube_title_path(run_dir).write_text(self._youtube_title(clip_context["caption"]), encoding="utf-8")
        return description

    def _load_state(self, run_dir: Path) -> dict[str, Any]:
        return _read_json(self._queue_state_path(run_dir))

    def _has_pending_source_stage(self) -> bool:
        if not self.config.queue_dir.exists():
            return False
        for child in sorted(self.config.queue_dir.iterdir(), key=lambda path: (path.stat().st_mtime, path.name), reverse=True):
            if not child.is_dir() or child.name in {"processing", "Archive", "Test", "source"}:
                continue
            state = _read_json(self._queue_state_path(child))
            if not isinstance(state, dict):
                continue
            if state.get("phase") in {"sourced", "source_ready", "source_consumed"}:
                return True
        return False

    def _mark_scheduler_ai_processed(self) -> None:
        path = self.config.queue_dir / "scheduler_state.json"
        state = _read_json(path) if path.exists() else None
        if not isinstance(state, dict) or state.get("phase") not in {"scheduled", "source_consumed"}:
            return
        state.update(
            {
                "phase": "ai_processed",
                "next_stage": "editor",
                "updated_at": datetime.now(self._tz).isoformat(),
            }
        )
        _write_json(path, state)

    def process_once(self) -> bool:
        for run_dir in self._ready_items():
            state = self._load_state(run_dir)
            if state.get("phase") == "ai_processed":
                continue
            manifest_path = self._manifest_path(run_dir)
            if manifest_path is None:
                continue
            manifest = _read_json(manifest_path)
            ai_dir = run_dir / "ai"
            ai_dir.mkdir(parents=True, exist_ok=True)

            visual = self._analyze_visual(manifest, run_dir=run_dir)
            has_kick_overlay = bool(visual.get("has_kick_overlay", False))
            has_caption = bool(visual.get("has_caption", False))
            caption_position = self._normalize_caption_position(visual.get("caption_position"))
            has_source_watermark = bool(visual.get("has_source_watermark", False))
            source_watermark_box = visual.get("source_watermark_box") if has_source_watermark else None
            clip_context = self._analyze_clip_context(manifest, run_dir=run_dir)
            context_sufficient = clip_context["context_sufficient"]
            summary = self._generate_summary(clip_context["caption"], clip_context["transcript"], context_sufficient)
            hashtags = clip_context["hashtags"]
            (ai_dir / "transcript.txt").write_text(clip_context["transcript"], encoding="utf-8")
            platforms = read_slot_platforms(self.config.queue_dir)
            youtube_description = (
                self._write_youtube_files(run_dir, clip_context, summary) if YOUTUBE in platforms else None
            )
            analysis = {
                "has_kick_overlay": has_kick_overlay,
                "has_caption": has_caption,
                "caption_position": caption_position,
                "has_source_watermark": has_source_watermark,
                "source_watermark_box": source_watermark_box,
                "context_sufficient": context_sufficient,
                "hashtags": hashtags,
                "summary": summary,
                "platforms": platforms,
                "youtube_description": youtube_description,
                "updated_at": datetime.now(self._tz).isoformat(),
            }
            _write_json(self._analysis_path(run_dir), analysis)
            hashtag_line = " ".join(f"#{tag}" for tag in hashtags)
            self._summary_path(run_dir).write_text(
                f"{summary}\n\n{hashtag_line}" if hashtag_line else summary, encoding="utf-8"
            )
            state_payload = {
                "phase": "ai_processed",
                "next_stage": "editor",
                "ready_for_next": "editor",
                "has_kick_overlay": has_kick_overlay,
                "has_caption": has_caption,
                "caption_position": caption_position,
                "has_source_watermark": has_source_watermark,
                "ai_summary_file": "ai/summary.txt",
                "updated_at": datetime.now(self._tz).isoformat(),
            }
            _write_json(self._queue_state_path(run_dir), state_payload)
            self._mark_scheduler_ai_processed()
            logger.info(
                "ClipAI processed %s: kick_overlay=%s, has_caption=%s, caption_position=%s, source_watermark=%s, context_sufficient=%s",
                run_dir.name,
                has_kick_overlay,
                has_caption,
                caption_position,
                has_source_watermark,
                context_sufficient,
            )
            return True
        if self._has_pending_source_stage():
            logger.info("ClipAI is waiting for a sourced queue item to reach the AI stage.")
        return False

    def run_forever(self) -> None:
        logger.info("Watching %s for sourced queue items.", self.config.queue_dir)
        consecutive_retryable_failures = 0
        retryable_alert_sent = False
        while True:
            try:
                self.process_once()
                consecutive_retryable_failures = 0
                retryable_alert_sent = False
                time.sleep(self.config.poll_interval_seconds)
            except GeminiRateLimitError as error:
                consecutive_retryable_failures += 1
                if consecutive_retryable_failures >= 3 and not retryable_alert_sent:
                    send_failure_alert(
                        app_name="ClipAI",
                        error=error,
                        context=f"retryable Gemini failure persisted for {consecutive_retryable_failures} consecutive cycles",
                    )
                    retryable_alert_sent = True
                if consecutive_retryable_failures > MAX_RETRIES:
                    raise
                retry_after = error.retry_after_seconds or max(self.config.poll_interval_seconds, 60)
                sleep_for = max(self.config.poll_interval_seconds, retry_after)
                sleep_for = min(sleep_for * 2 ** (consecutive_retryable_failures - 1), MAX_BACKOFF_SECONDS)
                logger.warning(
                    "Gemini quota exhausted; sleeping %s seconds before retrying ClipAI. %s",
                    sleep_for,
                    error,
                )
                time.sleep(sleep_for)
            except GeminiRetryableServiceError as error:
                consecutive_retryable_failures += 1
                if consecutive_retryable_failures >= 3 and not retryable_alert_sent:
                    send_failure_alert(
                        app_name="ClipAI",
                        error=error,
                        context=f"retryable Gemini failure persisted for {consecutive_retryable_failures} consecutive cycles",
                    )
                    retryable_alert_sent = True
                if consecutive_retryable_failures > MAX_RETRIES:
                    raise
                retry_after = error.retry_after_seconds or 120
                sleep_for = max(self.config.poll_interval_seconds, retry_after)
                sleep_for = min(sleep_for * 2 ** (consecutive_retryable_failures - 1), MAX_BACKOFF_SECONDS)
                logger.warning(
                    "Gemini is temporarily unavailable; sleeping %s seconds before retrying ClipAI. %s",
                    sleep_for,
                    error,
                )
                time.sleep(sleep_for)
