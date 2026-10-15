"""Find regions that stay visually identical for most of a video (burned-in text, logos, watermarks).

People, cameras and backgrounds move, so their edges change from frame to frame. Anything burned on
top of the video keeps exactly the same edges in every frame. We sample frames, keep only the edges
that are present in most of them, and group those edges into boxes. Pure ffmpeg + Pillow; no AI.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe

logger = logging.getLogger(__name__)

SCAN_WIDTH = 540
SCAN_HEIGHT = 960
TARGET_FRAMES = 40
MIN_FRAMES = 8
PERSISTENCE = 0.7          # an edge pixel must appear in at least 70% of sampled frames
CELL = 4                   # grouping grid cell size in scan pixels
CELL_MIN_PIXELS = 2        # static edge pixels needed for a cell to count
JOIN_X_CELLS = 3           # join pieces up to ~24px apart horizontally (letters, words)
JOIN_Y_CELLS = 1
MAX_STATIC_FRACTION = 0.35 # above this the whole video is near-still; nothing can be singled out
MAX_CANDIDATES = 12
MAX_CANDIDATE_WIDTH = 0.6
ZOOM_ROW_HEIGHT = 110      # pixel height of each enlarged box in the zoom sheet
ZOOM_SHEET_WIDTH = 900
EDGE_THRESHOLD = 24        # brightness change that counts as an edge (0-255)


@dataclass(frozen=True)
class StaticRegion:
    x: float
    y: float
    w: float
    h: float
    cells: int

    def box(self) -> dict[str, float]:
        return {"x": round(self.x, 4), "y": round(self.y, 4), "w": round(self.w, 4), "h": round(self.h, 4)}


def _duration_seconds(ffmpeg_exe: str, video_path: Path) -> float | None:
    completed = subprocess.run([ffmpeg_exe, "-hide_banner", "-i", str(video_path)], capture_output=True, text=True)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", completed.stderr or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def static_edge_mask(video_path: Path):
    """Return (mask, frame_count): a SCAN_WIDTH x SCAN_HEIGHT Pillow 'L' image, 255 where edges never move."""
    from PIL import Image, ImageChops, ImageFilter

    ffmpeg_exe = str(get_ffmpeg_exe())
    duration = _duration_seconds(ffmpeg_exe, video_path) or 0
    fps = min(4.0, max(0.2, TARGET_FRAMES / duration)) if duration > 0 else 1.0
    vf = f"fps={fps:.4f},scale={SCAN_WIDTH}:{SCAN_HEIGHT},format=gray"
    completed = subprocess.run(
        [ffmpeg_exe, "-v", "error", "-i", str(video_path), "-vf", vf, "-frames:v", str(TARGET_FRAMES * 2),
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True,
    )
    frame_size = SCAN_WIDTH * SCAN_HEIGHT
    count = len(completed.stdout) // frame_size
    if completed.returncode != 0 or count < MIN_FRAMES:
        logger.info("Static-region scan skipped for %s: %s usable frame(s).", video_path.name, count)
        return None, count
    total = Image.new("L", (SCAN_WIDTH, SCAN_HEIGHT), 0)
    for index in range(count):
        frame = Image.frombytes("L", (SCAN_WIDTH, SCAN_HEIGHT), completed.stdout[index * frame_size:(index + 1) * frame_size])
        edges = frame.filter(ImageFilter.FIND_EDGES).point(lambda value: 255 if value >= EDGE_THRESHOLD else 0)
        # Widen edges by a pixel so compression jitter does not break persistence.
        edges = edges.filter(ImageFilter.MaxFilter(3)).point(lambda value: 1 if value else 0)
        total = ImageChops.add(total, edges)
    needed = max(1, int(count * PERSISTENCE + 0.5))
    return total.point(lambda value: 255 if value >= needed else 0), count


def _cell_grid(mask) -> tuple[list[list[bool]], int, int]:
    from PIL import Image

    cols, rows = SCAN_WIDTH // CELL, SCAN_HEIGHT // CELL
    # BOX resize = mean of each cell; convert the pixel-count threshold to a mean-brightness threshold.
    means = mask.resize((cols, rows), Image.BOX)
    threshold = 255 * CELL_MIN_PIXELS / (CELL * CELL)
    data = list(means.getdata())
    return [[data[r * cols + c] >= threshold for c in range(cols)] for r in range(rows)], cols, rows


def group_regions(grid: list[list[bool]], cols: int, rows: int) -> list[StaticRegion]:
    """Connected groups of static cells (joined across small gaps); boxes cover only the real static cells."""
    seen = [[False] * cols for _ in range(rows)]
    regions: list[StaticRegion] = []
    for r0 in range(rows):
        for c0 in range(cols):
            if not grid[r0][c0] or seen[r0][c0]:
                continue
            stack = [(r0, c0)]
            seen[r0][c0] = True
            top, bottom, left, right, cells = r0, r0, c0, c0, 0
            while stack:
                r, c = stack.pop()
                cells += 1
                top, bottom, left, right = min(top, r), max(bottom, r), min(left, c), max(right, c)
                for nr in range(max(0, r - JOIN_Y_CELLS), min(rows, r + JOIN_Y_CELLS + 1)):
                    for nc in range(max(0, c - JOIN_X_CELLS), min(cols, c + JOIN_X_CELLS + 1)):
                        if grid[nr][nc] and not seen[nr][nc]:
                            seen[nr][nc] = True
                            stack.append((nr, nc))
            regions.append(StaticRegion(
                x=left / cols, y=top / rows, w=(right - left + 1) / cols, h=(bottom - top + 1) / rows, cells=cells,
            ))
    return regions


def is_candidate(region: StaticRegion) -> bool:
    """Watermark-sized: not a speck, not a whole picture/half-screen."""
    if region.cells < 12 or region.w < 0.03 or region.h < 0.01:
        return False
    # Edge-to-edge bars (scoreboards, tickers, stream bars) are broadcast graphics, not watermarks.
    return region.w * region.h <= 0.15 and region.h <= 0.15 and region.w <= MAX_CANDIDATE_WIDTH


def find_static_regions(video_path: Path) -> list[StaticRegion]:
    """Candidate watermark boxes (normalized 0-1 x/y/w/h), largest first. Empty when nothing can be measured."""
    mask, _ = static_edge_mask(video_path)
    if mask is None:
        return []
    grid, cols, rows = _cell_grid(mask)
    static_cells = sum(sum(row) for row in grid)
    if static_cells > MAX_STATIC_FRACTION * cols * rows:
        logger.info("Video %s is mostly still (%.0f%% static); no watermark candidates.", video_path.name, 100 * static_cells / (cols * rows))
        return []
    candidates = [region for region in group_regions(grid, cols, rows) if is_candidate(region)]
    candidates.sort(key=lambda region: region.cells, reverse=True)
    return sorted(candidates[:MAX_CANDIDATES], key=lambda region: (region.y, region.x))


def union_box(regions: list[StaticRegion]) -> dict[str, float]:
    left = min(region.x for region in regions)
    top = min(region.y for region in regions)
    right = max(region.x + region.w for region in regions)
    bottom = max(region.y + region.h for region in regions)
    return {"x": round(left, 4), "y": round(top, 4), "w": round(right - left, 4), "h": round(bottom - top, 4)}


def draw_candidates(frame_path: Path, regions: list[StaticRegion], output_path: Path) -> Path:
    """Copy of the frame with each candidate outlined and numbered (1-based), for the AI to choose from."""
    from PIL import Image, ImageDraw, ImageFont

    with Image.open(frame_path) as source:
        image = source.convert("RGB")
    width, height = image.size
    draw = ImageDraw.Draw(image)
    font_size = max(18, height // 45)
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:
        font = ImageFont.load_default()
    pad = max(4, width // 150)
    for number, region in enumerate(regions, start=1):
        left = max(0, int(region.x * width) - pad)
        top = max(0, int(region.y * height) - pad)
        right = min(width - 1, int((region.x + region.w) * width) + pad)
        bottom = min(height - 1, int((region.y + region.h) * height) + pad)
        draw.rectangle((left, top, right, bottom), outline=(255, 0, 255), width=3)
        label = str(number)
        text_box = draw.textbbox((0, 0), label, font=font)
        label_w, label_h = text_box[2] - text_box[0] + 8, text_box[3] - text_box[1] + 8
        label_top = top - label_h if top - label_h >= 0 else min(height - label_h, bottom)
        draw.rectangle((left, label_top, left + label_w, label_top + label_h), fill=(255, 0, 255))
        draw.text((left + 4 - text_box[0], label_top + 4 - text_box[1]), label, fill=(255, 255, 255), font=font)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, quality=90)
    return output_path


def draw_zoom_sheet(frame_path: Path, regions: list[StaticRegion], output_path: Path) -> Path:
    """One enlarged, numbered crop per candidate stacked vertically, so faint text inside each box is readable."""
    from PIL import Image, ImageDraw, ImageFont

    with Image.open(frame_path) as source:
        image = source.convert("RGB")
    width, height = image.size
    try:
        font = ImageFont.load_default(size=40)
    except TypeError:
        font = ImageFont.load_default()
    label_w = 80
    gap = 10
    sheet = Image.new("RGB", (ZOOM_SHEET_WIDTH, len(regions) * (ZOOM_ROW_HEIGHT + gap)), (40, 40, 40))
    draw = ImageDraw.Draw(sheet)
    for index, region in enumerate(regions):
        pad_x, pad_y = region.w * 0.1, region.h * 0.3
        left = max(0, int((region.x - pad_x) * width))
        top = max(0, int((region.y - pad_y) * height))
        right = min(width, max(left + 1, int((region.x + region.w + pad_x) * width)))
        bottom = min(height, max(top + 1, int((region.y + region.h + pad_y) * height)))
        crop = image.crop((left, top, right, bottom))
        scale = min(ZOOM_ROW_HEIGHT / crop.height, (ZOOM_SHEET_WIDTH - label_w - gap) / crop.width)
        crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))), Image.LANCZOS)
        row_top = index * (ZOOM_ROW_HEIGHT + gap)
        draw.rectangle((0, row_top, label_w - 4, row_top + ZOOM_ROW_HEIGHT), fill=(255, 0, 255))
        draw.text((14, row_top + 30), str(index + 1), fill=(255, 255, 255), font=font)
        sheet.paste(crop, (label_w + gap, row_top))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, quality=90)
    return output_path
