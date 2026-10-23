"""Candidate positions for the Kick overlay strip, drawn on the clip frames so the AI can say which spots are clear.

All positions are in the editor's 1080x1920 output space, where the source is scaled to fit and letterboxed.
A placement is stored as a fraction t (0..1) with y = (1920 - strip_height) * t, the same units as
KICK_OVERLAY_TOP_Y / KICK_OVERLAY_BOTTOM_Y, and it is always kept within those two values.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920


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


def placement_grids(layout: Layout, top_y: float, bottom_y: float) -> tuple[list[float], list[float]]:
    low, high = config_range(top_y, bottom_y)
    if layout.max_y <= 0:
        return [], []
    step = layout.strip_height * 1.075
    count = max(1, int(((high - low) * layout.max_y) // step) + 1)
    primary = [low + step * index / layout.max_y for index in range(count)]
    staggered = [(left + right) / 2 for left, right in zip(primary, primary[1:])]
    return primary, staggered


def candidate_fractions(
    layout: Layout,
    top_y: float,
    bottom_y: float,
) -> list[float]:
    """All positions from both staggered grids, without content-based filtering."""
    primary, staggered = placement_grids(layout, top_y, bottom_y)
    return primary + staggered


def aggregate_rankings(results: list[dict], count: int) -> list[dict]:
    """Require agreement on safety; average confidence-weighted normalized ranks."""
    scores: dict[int, list[float]] = {number: [] for number in range(1, count + 1)}
    for result in results:
        boxes = result.get("boxes")
        ranking = result.get("ranked_clear_boxes")
        if not isinstance(boxes, list) or not isinstance(ranking, list):
            raise ValueError("Placement response must contain boxes and ranked_clear_boxes lists.")
        if any(not isinstance(box, dict) or type(box.get("number")) is not int for box in boxes):
            raise ValueError("Invalid placement assessment.")
        by_number = {box["number"]: box for box in boxes}
        if len(boxes) != count or set(by_number) != set(scores):
            raise ValueError("Placement response must assess every position exactly once.")
        for box in boxes:
            confidence = box.get("confidence")
            if type(box.get("clear")) is not bool or type(confidence) not in (float, int) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("Each position requires a boolean clear and confidence in [0, 1].")
        clear = {number for number, box in by_number.items() if box["clear"]}
        if any(type(number) is not int for number in ranking) or len(set(ranking)) != len(ranking) or set(ranking) != clear:
            raise ValueError("Ranking must contain each clear position exactly once.")
        for rank, number in enumerate(ranking):
            confidence = by_number[number]["confidence"]
            if confidence >= 0.7:
                scores[number].append(confidence * (count - rank) / count)
    return sorted(
        [{"number": number, "score": sum(values) / len(results)}
         for number, values in scores.items() if results and len(values) == len(results)],
        key=lambda row: (-row["score"], row["number"]),
    )


def draw_candidates(
    frame_paths: list[Path], layout: Layout, fractions: list[float], output_path: Path,
    *, staggered: list[float] | None = None,
) -> Path:
    """Sampled frames in columns, with each staggered grid on its own row."""
    from PIL import Image, ImageDraw, ImageFont

    half_w, half_h = OUTPUT_WIDTH // 2, OUTPUT_HEIGHT // 2
    rows = 2 if staggered is not None else 1
    sheet = Image.new("RGB", (half_w * len(frame_paths) + 12 * (len(frame_paths) - 1), half_h * rows + 12 * (rows - 1)), (255, 255, 255))
    try:
        font = ImageFont.load_default(size=20)
    except TypeError:
        font = ImageFont.load_default()
    for panel in range(len(frame_paths) * rows):
        row, column = divmod(panel, len(frame_paths))
        frame_path = frame_paths[column]
        canvas = Image.new("RGB", (half_w, half_h), (0, 0, 0))
        with Image.open(frame_path) as source:
            frame = source.convert("RGB")
        fitted_w = round(layout.source_width * layout.scale / 2)
        fitted_h = round(layout.source_height * layout.scale / 2)
        canvas.paste(frame.resize((fitted_w, fitted_h)), ((half_w - fitted_w) // 2, (half_h - fitted_h) // 2))
        draw = ImageDraw.Draw(canvas)
        for number, fraction in enumerate(fractions, start=1):
            if staggered is not None and (fraction in staggered) != (row == 1):
                continue
            top = round(fraction * layout.max_y) / 2
            bottom = top + layout.strip_height / 2
            draw.rectangle((0, round(top), half_w - 1, round(bottom) - 1), outline=(255, 0, 255), width=2)
            label = str(number)
            box = draw.textbbox((0, 0), label, font=font)
            label_w, label_h = box[2] - box[0] + 10, box[3] - box[1] + 10
            label_top = top + 2
            left = 4
            draw.rectangle((left, label_top, left + label_w, label_top + label_h), fill=(255, 0, 255))
            draw.text((left + 5 - box[0], label_top + 5 - box[1]), label, fill=(255, 255, 255), font=font)
        sheet.paste(canvas, (column * (half_w + 12), row * (half_h + 12)))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, quality=90)
    return output_path
