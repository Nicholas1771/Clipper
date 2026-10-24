from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import time
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from imageio_ffmpeg import get_ffmpeg_exe

from common.gemini_usage import request_gemini
from common.platforms import YOUTUBE, read_slot_platforms
from common.creators import config_for_run
from clipai.config import AppConfig
from clipai import kick_placement
from clipai.static_regions import StaticRegion, draw_candidates, draw_zoom_sheet, find_static_regions, union_box

logger = logging.getLogger(__name__)


class GeminiRateLimitError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


MAX_RETRIES = 5
MAX_BACKOFF_SECONDS = 900
WATERMARK_KINDS = ("watermark", "caption", "stream_overlay", "broadcast_graphic", "scene", "photo", "other")
# Live-stream branding is part of the streamer's layout and must never be blurred as a source watermark.
STREAM_BRANDING = re.compile(r"\b(kick|twitch)\b", re.IGNORECASE)


class GeminiRetryableServiceError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class GeminiTruncatedResponseError(RuntimeError):
    pass


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
        self._base_config = config
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

    def _build_watermark_prompt(self, candidate_count: int) -> str:
        template = self._read_prompt_template("watermark_detection.txt")
        return self._fill_prompt(template, {"COUNT": str(candidate_count)})

    def _build_caption_prompt(self, context: str) -> str:
        template = self._read_prompt_template("caption_detection.txt")
        return self._fill_prompt(template, {"CONTEXT": context or "No metadata provided."})

    def _build_context_prompt(self, caption: str, on_screen_caption: str = "") -> str:
        template = self._read_prompt_template("context_analysis.txt")
        return self._fill_prompt(template, {
            "CAPTION": caption or "No caption provided.", "STREAMER": self.config.streamer_name,
            "ON_SCREEN_CAPTION": on_screen_caption or "None detected.",
        })

    def _build_summary_prompt(self, caption: str, transcript: str, context_sufficient: bool, on_screen_caption: str = "") -> str:
        template = self._read_prompt_template("summary_clip.txt" if context_sufficient else "summary_streamer.txt")
        return self._fill_prompt(
            template,
            {
                "CAPTION": caption, "TRANSCRIPT": transcript, "STREAMER": self.config.streamer_name,
                "ON_SCREEN_CAPTION": on_screen_caption or "None detected.",
            },
        )

    def _build_youtube_prompt(self, caption: str, transcript: str, context_sufficient: bool, on_screen_caption: str = "") -> str:
        template = self._read_prompt_template("youtube_description.txt")
        return self._fill_prompt(
            template,
            {
                "CAPTION": caption,
                "TRANSCRIPT": transcript,
                "STREAMER": self.config.streamer_name,
                "CONTEXT_SUFFICIENT": "yes" if context_sufficient else "no",
                "ON_SCREEN_CAPTION": on_screen_caption or "None detected.",
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
        temperature: float = 0.2,
        max_output_tokens: int | None = None,
        thinking_budget: int | None = None,
        stage: str = "analysis",
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
                "temperature": temperature,
                "topP": 0.9,
            },
        }
        if response_schema is not None and not plain_text:
            payload["generationConfig"]["responseMimeType"] = "application/json"
            payload["generationConfig"]["responseSchema"] = response_schema
        if max_output_tokens is not None:
            payload["generationConfig"]["maxOutputTokens"] = max_output_tokens
        if thinking_budget is not None:
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": thinking_budget}

        try:
            response = request_gemini(
                self.config.queue_dir, model=model or self.config.gemini_model, stage=stage,
                run_dir=getattr(self, "_usage_run_dir", None),
                url=url, json=payload, timeout=(10, self.config.timeout_seconds),
            )
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

        finish_reason = candidates[0].get("finishReason")
        if finish_reason == "MAX_TOKENS":
            raise GeminiTruncatedResponseError("Gemini response reached MAX_TOKENS; incomplete output was rejected.")
        if finish_reason and finish_reason != "STOP":
            raise RuntimeError(f"Gemini response did not finish successfully: {finish_reason}.")

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
        if normalized in {"TOP", "MIDDLE", "BOTTOM", "BOTH", "NONE"}:
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

    def source_has_kick_branding(self, video_path: Path, manifest: dict[str, Any]) -> bool:
        """Use the same strict visual detector for pre-cache sourcing admission."""
        with tempfile.TemporaryDirectory(prefix="clipper-branding-") as directory:
            run_dir = Path(directory)
            frames = self._extract_sample_frames(run_dir, video_path)
            if not frames:
                raise RuntimeError(f"No source frames could be extracted for Kick branding screening: {video_path}")
            return bool(self._detect_overlay_visual(manifest, image_paths=frames)["has_kick_overlay"])

    def _detect_overlay_visual(
        self, manifest: dict[str, Any], *, run_dir: Path | None = None, image_paths: list[Path] | None = None
    ) -> dict[str, Any]:
        context = self._summarize_context(manifest)
        image_paths = image_paths if image_paths is not None else self._collect_source_frames(run_dir)
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
        payload = self._call_gemini(self._build_overlay_prompt(context), response_schema=schema, image_paths=image_paths, stage="branding")
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
                "on_screen_caption": {"type": "STRING"},
                "caption_position": {"type": "STRING"},
                "text_bands": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {"top": {"type": "NUMBER"}, "bottom": {"type": "NUMBER"}},
                        "required": ["top", "bottom"],
                    },
                },
                "confidence": {"type": "NUMBER"},
            },
            "required": ["has_caption", "caption_position", "text_bands", "confidence", "on_screen_caption"],
        }
        payload = self._call_gemini(self._build_caption_prompt(context), response_schema=schema, image_paths=image_paths, stage="caption_detection")
        caption_position = self._normalize_caption_position(payload.get("caption_position"))
        has_caption = bool(payload.get("has_caption", False))
        confidence = self._normalize_confidence(payload.get("confidence"))
        if caption_position == "NONE" or confidence < 0.5:
            has_caption = False
            caption_position = "NONE"
        if not has_caption:
            caption_position = "NONE"
        bands = self._normalize_text_bands(payload.get("text_bands")) if has_caption else []
        if any(band["bottom"] <= 0.35 for band in bands) and any(band["top"] >= 0.65 for band in bands):
            caption_position = "BOTH"
        return {
            "has_caption": has_caption,
            "caption_position": caption_position,
            "caption_confidence": confidence,
            "caption_bands": bands,
            "on_screen_caption": (
                payload["on_screen_caption"].strip()
                if image_paths and has_caption and isinstance(payload.get("on_screen_caption"), str)
                else ""
            ),
        }

    def _normalize_text_bands(self, value: Any) -> list[dict[str, float]]:
        """Vertical extents (0=top..1=bottom of the full frame) of overlaid text; implausible bands are dropped."""
        bands: list[dict[str, float]] = []
        for item in value if isinstance(value, list) else []:
            try:
                top, bottom = float(item["top"]), float(item["bottom"])
            except (KeyError, TypeError, ValueError):
                continue
            if top > 1.0 or bottom > 1.0:
                top, bottom = top / 100.0, bottom / 100.0
            top, bottom = max(0.0, min(top, bottom)), min(1.0, max(top, bottom))
            if 0.0 < bottom - top <= 0.4:
                bands.append({"top": round(top, 4), "bottom": round(bottom, 4)})
        return sorted(bands, key=lambda band: band["top"])

    def _detect_source_watermark(self, manifest: dict[str, Any], *, run_dir: Path | None = None) -> dict[str, Any]:
        """Measure the regions that never move, then let Gemini pick which (if any) is a watermark.

        The box always comes from the measurement, never from AI coordinates. Anything unconfirmed means no blur.
        """
        del manifest
        none = {"has_source_watermark": False, "source_watermark_box": None, "source_watermark_confidence": 0.0}
        if run_dir is None:
            return none
        frame = run_dir / "ai" / "reference_frame_2.jpg"
        video = self._source_video_path(run_dir)
        if not frame.exists() or video is None:
            return none
        try:
            regions = find_static_regions(video)
            if not regions:
                logger.info("No still regions found in %s; no source watermark blur.", run_dir.name)
                return none
            annotated = draw_candidates(frame, regions, run_dir / "ai" / "watermark_candidates.jpg")
            zoomed = draw_zoom_sheet(frame, regions, run_dir / "ai" / "watermark_candidates_zoom.jpg")
        except Exception:
            logger.exception("Static-region scan failed for %s; no source watermark blur.", run_dir.name)
            return none
        logger.info(
            "Still-region candidates for %s: %s",
            run_dir.name,
            "; ".join(f"#{index} {region.box()}" for index, region in enumerate(regions, start=1)),
        )
        schema = {
            "type": "OBJECT",
            "properties": {
                "boxes": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "number": {"type": "INTEGER"},
                            "content": {"type": "STRING"},
                            "kind": {"type": "STRING", "enum": list(WATERMARK_KINDS)},
                        },
                        "required": ["number", "content", "kind"],
                        "propertyOrdering": ["number", "content", "kind"],
                    },
                },
                "confidence": {"type": "NUMBER"},
            },
            "required": ["boxes", "confidence"],
            "propertyOrdering": ["boxes", "confidence"],
        }
        payload = self._call_gemini(
            self._build_watermark_prompt(len(regions)),
            response_schema=schema,
            image_paths=[annotated, zoomed],
            model=self.config.watermark_model,
            temperature=0.0,
            stage="watermark_detection",
        )
        confidence = self._normalize_confidence(payload.get("confidence"))
        labels = payload.get("boxes") if isinstance(payload.get("boxes"), list) else []
        logger.info(
            "Watermark labels for %s: %s",
            run_dir.name,
            "; ".join(f"#{item.get('number')} {item.get('kind')}: {item.get('content')}" for item in labels if isinstance(item, dict)),
        )
        picked = [
            item.get("number")
            for item in labels
            if isinstance(item, dict)
            and item.get("kind") == "watermark"
            and not STREAM_BRANDING.search(str(item.get("content") or ""))
        ]
        chosen = self._chosen_regions(picked, regions)
        if not chosen or confidence < 0.7:
            logger.info("No confirmed source watermark for %s (picked=%s, confidence=%.2f).", run_dir.name, picked, confidence)
            return {**none, "source_watermark_confidence": confidence}
        box = union_box(chosen)
        if box["w"] * box["h"] > 0.15 or box["h"] > 0.15:
            logger.info("Picked boxes for %s are too spread out (%s); no source watermark blur.", run_dir.name, box)
            return {**none, "source_watermark_confidence": confidence}
        logger.info("Confirmed source watermark for %s at %s (confidence %.2f).", run_dir.name, box, confidence)
        return {"has_source_watermark": True, "source_watermark_box": box, "source_watermark_confidence": confidence}

    @staticmethod
    def _chosen_regions(value: Any, regions: list[StaticRegion]) -> list[StaticRegion]:
        chosen: list[StaticRegion] = []
        for item in value if isinstance(value, list) else []:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= number <= len(regions) and regions[number - 1] not in chosen:
                chosen.append(regions[number - 1])
        return chosen

    def _analyze_visual(self, manifest: dict[str, Any], *, run_dir: Path | None = None) -> dict[str, Any]:
        overlay = self._detect_overlay_visual(manifest, run_dir=run_dir)
        caption = self._detect_caption_position(manifest, run_dir=run_dir)
        watermark = self._detect_source_watermark(manifest, run_dir=run_dir)
        combined = dict(overlay)
        combined.update(caption)
        combined.update(watermark)
        combined["kick_overlay_y"] = None
        if not combined.get("has_kick_overlay"):
            try:
                combined["kick_overlay_y"] = self._pick_kick_overlay_y(run_dir, combined)
            except Exception:
                logger.exception("Kick overlay placement check failed; the editor will use its fallback position.")
        return combined

    def _pick_kick_overlay_y(self, run_dir: Path | None, visual: dict[str, Any]) -> float | None:
        """One Flash call locates protected content; code chooses a gap common to every frame."""
        from PIL import Image

        overlay_path = self.config.overlay_reference_image_path
        frames = [run_dir / "ai" / f"reference_frame_{index}.jpg" for index in (1, 2, 3)] if run_dir else []
        frames = [frame for frame in frames if frame.exists()]
        if overlay_path is None or not frames:
            logger.warning("Kick placement requires an overlay asset and sampled frames; using editor fallback.")
            return None
        with Image.open(overlay_path) as overlay:
            strip_height = round(kick_placement.OUTPUT_WIDTH * overlay.height / overlay.width)
        layouts = []
        for path in frames:
            with Image.open(path) as frame:
                layouts.append(kick_placement.Layout(frame.width, frame.height, strip_height))
        region_frames = [
            kick_placement.draw_region_ruler(path, run_dir / "ai" / f"kick_regions_{number}.jpg")
            for number, path in enumerate(frames, 1)
        ]
        prompt = self._fill_prompt(
            self._read_prompt_template("kick_placement.txt"),
            {"FRAME_COUNT": {1: "one", 2: "two", 3: "three"}[len(frames)],
             "FRAME_NUMBERS": ",".join(str(number) for number in range(1, len(frames) + 1))},
        )
        schema = {
            "type": "OBJECT",
            "properties": {
                "regions": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "frame": {"type": "INTEGER"},
                            "kind": {"type": "STRING"},
                            "top": {"type": "INTEGER"},
                            "bottom": {"type": "INTEGER"},
                        },
                        "required": ["frame", "kind", "top", "bottom"],
                    },
                },
            },
            "required": ["regions"],
        }
        result = self._call_gemini(
            prompt, response_schema=schema, image_paths=region_frames,
            model=self.config.kick_placement_model, temperature=0.0,
            thinking_budget=2048, max_output_tokens=4096, stage="kick_placement",
        )
        evidence = {
            "method": "protected_regions", "model": self.config.kick_placement_model,
            "response": result, "frame_files": [path.name for path in frames],
            "input_files": [path.name for path in region_frames],
            "strip_height": strip_height, "status": "pending",
        }
        evidence_path = run_dir / "ai" / "kick_placement_results.json"
        _write_json(evidence_path, evidence)
        try:
            intervals = kick_placement.protected_intervals(result, layouts)
            placement = kick_placement.choose_position(
                intervals, strip_height, self.config.kick_overlay_top_y, self.config.kick_overlay_bottom_y,
            )
        except ValueError as error:
            evidence.update(status="invalid", error=str(error))
            _write_json(evidence_path, evidence)
            logger.exception("Invalid Kick protected regions for %s; using editor fallback.", run_dir.name)
            return None
        evidence.update(
            status="selected" if placement else "no_fit",
            protected_intervals=intervals,
            fraction=placement.fraction if placement else None,
            margin=placement.margin if placement else None,
            clearance=placement.clearance if placement else None,
        )
        _write_json(evidence_path, evidence)
        if placement is None:
            logger.warning("No Kick strip fits the protected regions for %s; using editor fallback.", run_dir.name)
            return None
        if placement.margin == 0:
            logger.warning("Kick placement for %s requires a tight fit without the extra 12px margin.", run_dir.name)
        visual["kick_placement_method"] = "protected_regions"
        logger.info("Kick overlay for %s placed at %.4f (margin=%dpx, clearance=%.1fpx).",
                    run_dir.name, placement.fraction, placement.margin, placement.clearance)
        return placement.fraction

    def _extract_audio(self, video_path: Path, output_path: Path) -> Path | None:
        """Mono 16 kHz wav (about 2 MB/min); Gemini transcribes it at roughly 32 tokens per second of audio."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [str(get_ffmpeg_exe()), "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", str(output_path)]
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0 or not output_path.exists():
            logger.info("No usable audio in %s; using the caption only.", video_path)
            return None
        return output_path

    def _analyze_clip_context(self, manifest: dict[str, Any], *, run_dir: Path, on_screen_caption: str = "") -> dict[str, Any]:
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
            for attempt in range(2):
                try:
                    payload = self._call_gemini(
                        self._build_context_prompt(caption, on_screen_caption),
                        response_schema=schema,
                        audio_paths=[audio] if audio else None,
                        model=self.config.context_model,
                        max_output_tokens=4096,
                        thinking_budget=0 if self.config.context_model.startswith("gemini-2.5-flash") else None,
                        stage="transcription_context",
                    )
                    break
                except GeminiTruncatedResponseError:
                    if attempt:
                        raise
                    logger.warning("Gemini audio/context response was truncated; retrying once with the same bounded settings.")
        finally:
            if audio is not None:
                audio.unlink(missing_ok=True)
        return {
            "caption": caption,
            "on_screen_caption": on_screen_caption,
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

    def _generate_summary(self, caption: str, transcript: str, context_sufficient: bool, on_screen_caption: str = "") -> str:
        payload = self._call_gemini(
            self._build_summary_prompt(caption, transcript, context_sufficient, on_screen_caption), plain_text=True,
            stage="summary",
        )
        summary = str(payload.get("text") or "").replace("\r", "\n").strip()
        if not summary:
            raise RuntimeError("Gemini returned an empty summary.")
        parts = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n\s*\n", summary)]
        return "\n\n".join(part for part in parts if part)

    def _generate_youtube_description(self, caption: str, transcript: str, context_sufficient: bool, on_screen_caption: str = "") -> str:
        payload = self._call_gemini(
            self._build_youtube_prompt(caption, transcript, context_sufficient, on_screen_caption), plain_text=True,
            stage="youtube_metadata",
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
        self._usage_run_dir = run_dir
        description = fallback_description
        try:
            description = self._generate_youtube_description(
                clip_context["caption"], clip_context["transcript"], clip_context["context_sufficient"],
                on_screen_caption=clip_context.get("on_screen_caption", ""),
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

    def backfill_youtube_metadata(self, *, since: date) -> int:
        """Write missing YouTube metadata for archived, successfully published Instagram clips (no re-transcription)."""
        written = 0
        archive = self.config.queue_dir / "Archive"
        if not archive.is_dir():
            logger.info("No archive at %s; nothing to backfill.", archive)
            return 0
        for run_dir in sorted(path for path in archive.iterdir() if path.is_dir()):
            self.config = config_for_run(self._base_config, run_dir)
            publish = _read_json(run_dir / "publisher" / "publish_state.json")
            if not isinstance(publish, dict) or publish.get("phase") != "published" or publish.get("status") not in {"success", "partial"}:
                continue
            published_raw = (publish.get("published_at") or {}).get("instagram") if isinstance(publish.get("published_at"), dict) else None
            try:
                published = datetime.fromisoformat(str(published_raw or publish.get("updated_at"))).astimezone(self._tz).date()
            except ValueError:
                continue
            if published < since:
                continue
            editor_dir = run_dir / "editor"
            if (editor_dir / "youtube_description.txt").exists():
                continue
            manifest_path = self._manifest_path(run_dir)
            if manifest_path is None or not editor_dir.is_dir():
                logger.warning("Backfill skipped %s: missing manifest or editor output.", run_dir.name)
                continue
            analysis = _read_json(self._analysis_path(run_dir))
            analysis = analysis if isinstance(analysis, dict) else {}
            manifest = _read_json(manifest_path)
            transcript_path = run_dir / "ai" / "transcript.txt"
            summary_path = self._summary_path(run_dir)
            clip_context = {
                "caption": self._summarize_context(manifest if isinstance(manifest, dict) else {}),
                "transcript": transcript_path.read_text(encoding="utf-8").strip() if transcript_path.exists() else "",
                "context_sufficient": bool(analysis.get("context_sufficient", False)),
                "on_screen_caption": str(analysis.get("on_screen_caption") or ""),
            }
            fallback = str(analysis.get("summary") or "") or (summary_path.read_text(encoding="utf-8").strip() if summary_path.exists() else "")
            (run_dir / "ai").mkdir(exist_ok=True)
            description = self._write_youtube_files(run_dir, clip_context, fallback)
            for name in ("youtube_title.txt", "youtube_description.txt"):
                (editor_dir / name).write_text((run_dir / "ai" / name).read_text(encoding="utf-8").strip(), encoding="utf-8")
            logger.info("Backfilled YouTube metadata for %s: %s", run_dir.name, " ".join(description.split())[:120])
            written += 1
        logger.info("YouTube metadata backfill complete: %s clip(s) updated in %s.", written, archive)
        return written

    def process_once(self) -> bool:
        for run_dir in self._ready_items():
            self._usage_run_dir = run_dir
            self.config = config_for_run(self._base_config, run_dir)
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
            caption_bands = self._normalize_text_bands(visual.get("caption_bands")) if has_caption else []
            has_source_watermark = bool(visual.get("has_source_watermark", False))
            source_watermark_box = visual.get("source_watermark_box") if has_source_watermark else None
            on_screen_caption = str(visual.get("on_screen_caption") or "")
            clip_context = self._analyze_clip_context(manifest, run_dir=run_dir, on_screen_caption=on_screen_caption)
            context_sufficient = clip_context["context_sufficient"]
            summary = self._generate_summary(
                clip_context["caption"], clip_context["transcript"], context_sufficient,
                on_screen_caption=on_screen_caption,
            )
            hashtags = clip_context["hashtags"]
            (ai_dir / "transcript.txt").write_text(clip_context["transcript"], encoding="utf-8")
            platforms = read_slot_platforms(self.config.queue_dir)
            # In instagram_top mode any Instagram post may be replayed to YouTube the next day.
            needs_youtube = YOUTUBE in platforms or self.config.youtube_selection_mode == "instagram_top"
            youtube_description = (
                self._write_youtube_files(run_dir, clip_context, summary) if needs_youtube else None
            )
            analysis = {
                "has_kick_overlay": has_kick_overlay,
                "has_caption": has_caption,
                "caption_position": caption_position,
                "caption_bands": caption_bands,
                "on_screen_caption": on_screen_caption,
                "kick_overlay_y": visual.get("kick_overlay_y") if not has_kick_overlay else None,
                "kick_placement_method": visual.get("kick_placement_method"),
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
        while True:
            try:
                self.process_once()
                consecutive_retryable_failures = 0
                time.sleep(self.config.poll_interval_seconds)
            except GeminiRateLimitError as error:
                consecutive_retryable_failures += 1
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
