"""Candidate positions for the Kick overlay strip, drawn on the clip frames so the AI can say which spots are clear.

All positions are in the editor's 1080x1920 output space, where the source is scaled to fit and letterboxed.
A placement is stored as a fraction t (0..1) with y = (1920 - strip_height) * t, the same units as
KICK_OVERLAY_TOP_Y / KICK_OVERLAY_BOTTOM_Y, and it is always kept within those two values.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
BAND_MARGIN = 0.025 * OUTPUT_HEIGHT


@dataclass(frozen=True)
class Layout:
    source_width: int
    source_height: int
    strip_height: float

    @property
    def scale(self) -> float:
        return min(OUTPUT_WIDTH / self.source_width, OUTPUT_HEIGHT / self.source_height)

    @property
    def offset_y(self) -> float:
        return (OUTPUT_HEIGHT - self.source_height * self.scale) / 2

    @property
    def max_y(self) -> float:
        return OUTPUT_HEIGHT - self.strip_height


def config_range(top_y: float, bottom_y: float) -> tuple[float, float]:
    return min(top_y, bottom_y), max(top_y, bottom_y)


def default_fraction(top_y: float, bottom_y: float, caption_position: str) -> float:
    """Configured spot: middle when captions sit at top and bottom, top when they sit at the bottom, else bottom."""
    if caption_position == "BOTH":
        return (top_y + bottom_y) / 2
    return top_y if caption_position == "BOTTOM" else bottom_y


def blocked_spans(layout: Layout, bands: list[dict[str, float]]) -> list[tuple[float, float]]:
    spans = []
    for band in bands:
        top = layout.offset_y + float(band["top"]) * layout.source_height * layout.scale
        bottom = layout.offset_y + float(band["bottom"]) * layout.source_height * layout.scale
        spans.append((top - BAND_MARGIN, bottom + BAND_MARGIN))
    return spans


def watermark_span(
    layout: Layout, *, box: dict | None, watermark_y: float, size: float, opacity: float
) -> tuple[float, float] | None:
    """Rows used by our account watermark: on the blurred source watermark when there is one, else its configured spot."""
    font_size = round(size * OUTPUT_HEIGHT)
    has_text = opacity > 0 and font_size >= 1
    margin = 0.01 * OUTPUT_HEIGHT
    if isinstance(box, dict):
        try:
            top = layout.offset_y + float(box["y"]) * layout.source_height * layout.scale
            bottom = layout.offset_y + (float(box["y"]) + float(box["h"])) * layout.source_height * layout.scale
        except (KeyError, TypeError, ValueError):
            top = bottom = None
        if top is not None:
            center = (top + bottom) / 2
            if has_text:
                top, bottom = min(top, center - font_size / 2), max(bottom, center + font_size / 2)
            return top - margin, bottom + margin
    if not has_text:
        return None
    y = (OUTPUT_HEIGHT - font_size) * watermark_y
    return y - margin, y + font_size + margin


def is_clear(layout: Layout, fraction: float, spans: list[tuple[float, float]]) -> bool:
    y = fraction * layout.max_y
    return all(y + layout.strip_height <= top or y >= bottom for top, bottom in spans)


def candidate_fractions(
    layout: Layout,
    top_y: float,
    bottom_y: float,
    bands: list[dict[str, float]],
    keep_out: list[tuple[float, float]] | None = None,
) -> list[float]:
    """Non-overlapping strip positions spread across the configured range.

    Positions touching keep_out (our watermark) are always dropped; those hitting known caption text are dropped
    unless that would leave nothing.
    """
    low, high = config_range(top_y, bottom_y)
    if layout.max_y <= 0:
        return []
    span = (high - low) * layout.max_y
    step = layout.strip_height * 1.15
    count = max(1, int(span // step) + 1)
    fractions = [low + (high - low) * index / (count - 1) for index in range(count)] if count > 1 else [low]
    fractions = [fraction for fraction in fractions if is_clear(layout, fraction, keep_out or [])]
    blocked = blocked_spans(layout, bands)
    return [fraction for fraction in fractions if is_clear(layout, fraction, blocked)] or fractions


def draw_candidates(frame_paths: list[Path], layout: Layout, fractions: list[float], output_path: Path) -> Path:
    """Frames side by side in output layout (half size), each candidate strip outlined and numbered (1-based)."""
    from PIL import Image, ImageDraw, ImageFont

    half_w, half_h = OUTPUT_WIDTH // 2, OUTPUT_HEIGHT // 2
    sheet = Image.new("RGB", (half_w * len(frame_paths) + 12 * (len(frame_paths) - 1), half_h), (255, 255, 255))
    try:
        font = ImageFont.load_default(size=30)
    except TypeError:
        font = ImageFont.load_default()
    for column, frame_path in enumerate(frame_paths):
        canvas = Image.new("RGB", (half_w, half_h), (0, 0, 0))
        with Image.open(frame_path) as source:
            frame = source.convert("RGB")
        fitted_w = round(layout.source_width * layout.scale / 2)
        fitted_h = round(layout.source_height * layout.scale / 2)
        canvas.paste(frame.resize((fitted_w, fitted_h)), ((half_w - fitted_w) // 2, (half_h - fitted_h) // 2))
        draw = ImageDraw.Draw(canvas)
        for number, fraction in enumerate(fractions, start=1):
            top = fraction * layout.max_y / 2
            bottom = top + layout.strip_height / 2
            draw.rectangle((1, top, half_w - 2, bottom), outline=(255, 0, 255), width=3)
            label = str(number)
            box = draw.textbbox((0, 0), label, font=font)
            label_w, label_h = box[2] - box[0] + 10, box[3] - box[1] + 10
            label_top = top + (bottom - top - label_h) / 2
            draw.rectangle((4, label_top, 4 + label_w, label_top + label_h), fill=(255, 0, 255))
            draw.text((9 - box[0], label_top + 5 - box[1]), label, fill=(255, 255, 255), font=font)
        sheet.paste(canvas, (column * (half_w + 12), 0))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, quality=90)
    return output_path
