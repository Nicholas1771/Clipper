"""Fit a full-width strip into gaps between Flash-detected protected regions."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920


@dataclass(frozen=True)
class Layout:
    source_width: int
    source_height: int
    strip_height: int

    @property
    def scale(self) -> float:
        return min(OUTPUT_WIDTH / self.source_width, OUTPUT_HEIGHT / self.source_height)

    @property
    def offset_y(self) -> float:
        return (OUTPUT_HEIGHT - self.source_height * self.scale) / 2

    @property
    def max_y(self) -> int:
        return OUTPUT_HEIGHT - self.strip_height


@dataclass(frozen=True)
class Placement:
    fraction: float
    margin: int
    clearance: float


def draw_region_ruler(frame_path: Path, output_path: Path) -> Path:
    """Give Flash coordinate references without drawing overlay candidates."""
    from PIL import Image, ImageDraw, ImageFont

    with Image.open(frame_path) as source:
        source = source.convert("RGB")
        source.thumbnail((540, 960))
    image = Image.new("RGB", (source.width + 70, source.height), "white")
    image.paste(source, (70, 0))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=16)
    for coordinate in range(0, 1001, 50):
        pixel = round(coordinate * source.height / 1000)
        draw.text((2, max(0, min(source.height - 18, pixel - 8))),
                  str(coordinate), font=font, fill="black")
        draw.line((58, pixel, image.width - 1, pixel), fill=(150, 150, 150), width=1)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, quality=95)
    return output_path


def protected_intervals(payload: dict, layouts: list[Layout]) -> list[tuple[float, float]]:
    """Validate complete frame coverage and map source coordinates to fitted output pixels."""
    regions = payload.get("regions")
    if not isinstance(regions, list) or not layouts:
        raise ValueError("Placement response requires a regions list and sampled frames.")
    seen: set[int] = set()
    intervals = []
    for region in regions:
        if not isinstance(region, dict):
            raise ValueError("Invalid protected region.")
        number = region.get("frame")
        if type(number) is not int or not 1 <= number <= len(layouts):
            raise ValueError("Invalid protected-region frame number.")
        seen.add(number)
        layout = layouts[number - 1]
        top, bottom, kind = region.get("top"), region.get("bottom"), region.get("kind")
        if (type(top) is not int or type(bottom) is not int or not 0 <= top < bottom <= 1000
                or not isinstance(kind, str) or not kind.strip()):
            raise ValueError("Protected regions require a kind and integer 0 <= top < bottom <= 1000.")
        height = layout.source_height * layout.scale
        intervals.append((layout.offset_y + top * height / 1000,
                          layout.offset_y + bottom * height / 1000))
    if seen != set(range(1, len(layouts) + 1)):
        raise ValueError("Protected regions must cover every sampled frame; empty or missing frames use fallback.")
    return intervals


def choose_position(
    intervals: list[tuple[float, float]], strip_height: int, top_y: float, bottom_y: float,
) -> Placement | None:
    """Maximize minimum clearance across all frames; relax only the extra margin, never overlaps."""
    if (type(strip_height) is not int or not 0 < strip_height < OUTPUT_HEIGHT
            or not all(math.isfinite(value) and 0 <= value <= 1 for value in (top_y, bottom_y))):
        raise ValueError("Invalid Kick placement geometry.")
    low, high = sorted((top_y, bottom_y))
    max_y = OUTPUT_HEIGHT - strip_height
    for margin in (12, 0):
        best: Placement | None = None
        for step in range(math.ceil(low * 1000), math.floor(high * 1000) + 1):
            fraction = step / 1000
            top = round(max_y * fraction)
            bottom = top + strip_height
            clearance = min(
                (max(start - margin - bottom, top - end - margin) for start, end in intervals),
                default=OUTPUT_HEIGHT,
            )
            if clearance >= 0 and (best is None or (clearance, fraction) > (best.clearance, best.fraction)):
                best = Placement(fraction, margin, clearance)
        if best is not None:
            return best
    return None
