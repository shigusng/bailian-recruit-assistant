"""Aspect-preserving captures and recruitment regions derived from live anchors."""
from __future__ import annotations

from dataclasses import dataclass
import math

import cv2
import numpy as np


ANALYSIS_WIDTH = 1080
MAX_ANALYSIS_HEIGHT = 3840
REFERENCE_SIZE = (1080, 2400)
REFERENCE_REGIONS = ((35, 975, 330, 1320), (350, 1060, 710, 1410), (740, 975, 1070, 1320))


@dataclass(frozen=True)
class CaptureGeometry:
    original_size: tuple[int, int]
    viewport: tuple[int, int, int, int]
    analysis_size: tuple[int, int]

    @classmethod
    def from_image(cls, image):
        height, width = image.shape[:2]
        if width <= 0 or height <= 0:
            raise ValueError('设备截图尺寸无效')
        # Trim flat letterbox margins on a thumbnail so large screenshots do
        # not add a full-size mask allocation on every scan.
        factor = min(1., 480 / max(width, height))
        small_width = max(1, round(width * factor))
        small_height = max(1, round(height * factor))
        small = cv2.resize(image, (small_width, small_height), interpolation=cv2.INTER_AREA)

        def content_indices(sample, axis, ignore_edge_icons=False):
            bright = sample.max(axis=(axis, 2)) > 18
            spread = (sample.max(axis=axis) - sample.min(axis=axis)).max(axis=1)
            active = bright & (spread > 6)
            if not active.any():
                active = bright
            if ignore_edge_icons and active.any():
                changes = np.diff(np.concatenate(([False], active, [False])).astype(np.int8))
                starts, ends = np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)
                lengths = ends - starts
                largest = int(np.argmax(lengths))
                others = [index for index in range(len(starts)) if index != largest]
                # Android navigation icons can occupy an otherwise flat side
                # margin. Discard only small, isolated components at an edge.
                if others and all(lengths[index] < lengths[largest] * .15 and
                                  (ends[index] < len(active) * .1 or starts[index] > len(active) * .9)
                                  for index in others):
                    active[:] = False
                    active[starts[largest]:ends[largest]] = True
            return np.flatnonzero(active)

        columns = content_indices(small, 0, ignore_edge_icons=True)
        horizontal = small[:, columns[0]:columns[-1] + 1] if len(columns) else small
        rows = content_indices(horizontal, 1)
        left = top = 0
        right, bottom = width, height
        if len(rows) and len(columns):
            xscale, yscale = width / small_width, height / small_height
            proposed = (max(0, math.floor((columns[0] - 1) * xscale)),
                        max(0, math.floor((rows[0] - 1) * yscale)),
                        min(width, math.ceil((columns[-1] + 2) * xscale)),
                        min(height, math.ceil((rows[-1] + 2) * yscale)))
            # Ignore tiny dark borders and keep a pixel margin around content.
            if proposed[0] >= max(3, width * .01):
                left = proposed[0]
            if width - proposed[2] >= max(3, width * .01):
                right = proposed[2]
            if proposed[1] >= max(3, height * .01):
                top = proposed[1]
            if height - proposed[3] >= max(3, height * .01):
                bottom = proposed[3]
        # A loading animation can be only a small bright patch on black. It
        # is not sufficient evidence for replacing the whole game viewport.
        if right - left < min(width, height) * .15:
            left, right = 0, width
        if bottom - top < min(width, height) * .25:
            top, bottom = 0, height
        content_width, content_height = right - left, bottom - top
        scale = min(ANALYSIS_WIDTH / content_width, MAX_ANALYSIS_HEIGHT / content_height)
        analysis_size = (max(1, round(content_width * scale)), max(1, round(content_height * scale)))
        return cls((width, height), (left, top, content_width, content_height),
                   analysis_size)

    def normalize(self, image):
        left, top, width, height = self.viewport
        cropped = image[top:top + height, left:left + width]
        interpolation = cv2.INTER_AREA if width > self.analysis_size[0] else cv2.INTER_CUBIC
        return cv2.resize(cropped, self.analysis_size, interpolation=interpolation)

    def to_device_point(self, point):
        x, y = map(float, point)
        analysis_width, analysis_height = self.analysis_size
        if not (math.isfinite(x) and math.isfinite(y) and
                0 <= x < analysis_width and 0 <= y < analysis_height):
            raise ValueError('识别出的点击位置不在当前游戏画面内，已停止')
        left, top, width, height = self.viewport
        return (min(left + width - 1, max(left, round(left + x * width / analysis_width))),
                min(top + height - 1, max(top, round(top + y * height / analysis_height))))

    def describe(self):
        width, height = self.original_size
        left, top, content_width, content_height = self.viewport
        text = f'{width}×{height} · 自动适配'
        if (left, top, content_width, content_height) != (0, 0, width, height):
            text += f'；有效画面 {content_width}×{content_height}'
        return text


def reference_regions(shape):
    """Fallback regions for callers without OCR anchors; never reshape pixels."""
    height, width = shape[:2]
    return tuple((round(x * width / REFERENCE_SIZE[0]), round(y * height / REFERENCE_SIZE[1]),
                  round(x2 * width / REFERENCE_SIZE[0]), round(y2 * height / REFERENCE_SIZE[1]))
                 for x, y, x2, y2 in REFERENCE_REGIONS)


def recruitment_regions(shape, title_y, controls_y, shard_points):
    """Locate cards from their fragment bars, or the title/control span."""
    height, width = shape[:2]
    span = controls_y - title_y
    if span <= 0:
        return reference_regions(shape)
    centers = [width / 6, width / 2, width * 5 / 6]
    for slot, point in shard_points.items():
        centers[slot] = point[0]
    if not centers[0] < centers[1] < centers[2]:
        centers = [width / 6, width / 2, width * 5 / 6]
    edges = [max(0, centers[0] - (centers[1] - centers[0]) / 2),
             (centers[0] + centers[1]) / 2, (centers[1] + centers[2]) / 2,
             min(width, centers[2] + (centers[2] - centers[1]) / 2)]
    card_unit = min(width, 3 * float(np.median(np.diff(centers))))
    stagger = min(card_unit / 12, span * .09)
    if shard_points:
        baseline = float(np.median([point[1] - (stagger if slot == 1 else 0)
                                    for slot, point in shard_points.items()]))
        card_height = min(card_unit * .34, span * .45)
        bars = [shard_points[slot][1] if slot in shard_points else
                baseline + (stagger if slot == 1 else 0) for slot in range(3)]
        tops = [bar - card_height for bar in bars]
        bottoms = [bar + card_unit * .018 for bar in bars]
    else:
        tops = [title_y + span * .30 + (stagger if slot == 1 else 0) for slot in range(3)]
        bottoms = [title_y + span * .72 + (stagger if slot == 1 else 0) for slot in range(3)]
    regions = []
    for slot in range(3):
        margin = min(width * .012, (edges[slot + 1] - edges[slot]) * .035)
        left = min(width - 1, max(0, round(edges[slot] + margin)))
        right = min(width, max(left + 1, round(edges[slot + 1] - margin)))
        top = min(height - 1, max(0, round(tops[slot])))
        bottom = min(height, max(top + 1, round(bottoms[slot])))
        regions.append((left, top, right, bottom))
    return tuple(regions)


def shard_bar_centers(image, title_y, controls_y):
    """Find the three cyan fragment bars even if some numbers are unreadable."""
    if image is None or controls_y <= title_y:
        return ()
    height, width = image.shape[:2]
    span = controls_y - title_y
    top = max(0, round(title_y + span * .2))
    bottom = min(height, round(title_y + span * .88))
    if bottom <= top:
        return ()
    hsv = cv2.cvtColor(image[top:bottom], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (80, 100, 100), (115, 255, 255))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for contour in contours:
        x, y, bar_width, bar_height = cv2.boundingRect(contour)
        if not (bar_width >= 12 and 3 <= bar_height <= span * .15 and
                3 <= bar_width / bar_height <= 16 and
                cv2.contourArea(contour) >= bar_width * bar_height * .55):
            continue
        point = (round(x + bar_width / 2), round(top + y + bar_height / 2))
        score = abs(point[1] - (title_y + span * .67)) + 2 * bar_height
        candidates.append((score, point, bar_width))
    chosen = []
    for _, point, bar_width in sorted(candidates):
        if all(abs(point[0] - other[0][0]) > min(bar_width, other[1]) * .65 for other in chosen):
            chosen.append((point, bar_width))
        if len(chosen) == 3:
            break
    if len(chosen) != 3 or max(point[1] for point, _ in chosen) - min(point[1] for point, _ in chosen) > span * .25:
        return ()
    return tuple(point for point, _ in sorted(chosen, key=lambda value: value[0][0]))


def find_decrease(image, multiplier, maximum, items):
    """Confirm the minus glyph beside the recognized multiplier controls."""
    mx, my = multiplier
    spacing = maximum[0] - mx
    if spacing <= 0:
        return None
    band = max(8, spacing * .13)
    for text, point in items:
        if text in ('-', '−', '—', '－') and mx - spacing * 1.2 < point[0] < mx and abs(point[1] - my) < band:
            return point
    if image is None:
        return None
    height, width = image.shape[:2]
    left, right = max(0, round(mx - spacing * 1.2)), min(width, round(mx - spacing * .18))
    top, bottom = max(0, round(my - band)), min(height, round(my + band))
    if right <= left or bottom <= top:
        return None
    hsv = cv2.cvtColor(image[top:bottom, left:right], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, 200), (179, 90, 255))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    expected_x = mx - spacing * 2 / 3
    for contour in contours:
        x, y, glyph_width, glyph_height = cv2.boundingRect(contour)
        if not (max(5, spacing * .04) <= glyph_width <= spacing * .26 and
                2 <= glyph_height <= band * .8 and 2.3 <= glyph_width / glyph_height <= 12):
            continue
        if cv2.contourArea(contour) < glyph_width * glyph_height * .55:
            continue
        point = (round(left + x + glyph_width / 2), round(top + y + glyph_height / 2))
        distance = abs(point[1] - my) * 2 + abs(point[0] - expected_x)
        candidates.append((distance, point))
    return min(candidates)[1] if candidates else None
