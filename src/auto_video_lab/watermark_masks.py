"""Small, source-space glyph masks. Semantic approval is required by caller.

This is not a general text eraser or a reconstruction model. Multi-frame
consensus rejects moving/background highlights; uncertain masks use the old
bounded repair instead. No network, GPU, source writes, or shell strings.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np


def glyph_mask(frames: list[np.ndarray]) -> tuple[np.ndarray | None, str]:
    if len(frames) < 3 or any(f.shape != frames[0].shape for f in frames):
        return None, "insufficient matching samples"
    height, width = frames[0].shape[:2]
    candidates = []
    for frame in frames:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        # Bright white lettering plus the cyan/magenta of common platform logos.
        white = (gray > 170) & (hsv[:, :, 1] < 110)
        color = (hsv[:, :, 1] > 65) & (hsv[:, :, 2] > 115) & (
            ((hsv[:, :, 0] >= 75) & (hsv[:, :, 0] <= 110)) | (hsv[:, :, 0] >= 145))
        candidates.append((white | color).astype(np.uint8))
    votes = np.stack(candidates).sum(axis=0)
    stable = (votes >= math.ceil(len(frames) * .8)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(stable, 8)
    mask = np.zeros((height, width), np.uint8)
    for index in range(1, count):
        area = stats[index, cv2.CC_STAT_AREA]
        if 3 <= area <= height * width * .18:
            mask[labels == index] = 255
    occupancy = np.count_nonzero(mask) / mask.size
    if not .004 <= occupancy <= .38:
        return None, "no bounded stable glyphs"
    # Shadows/antialiasing need a margin, but never erase the whole box.
    radius = max(2, min(10, round(min(height, width) * .05)))
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius*2+1, radius*2+1)))
    if np.count_nonzero(mask) / mask.size > .65:
        return None, "glyph mask would erase too much background"
    # Check every sampled frame, not just the first: a jumping logo needs its
    # own time/position region, never a union spanning its motion.
    # A pale wall may light up the whole ROI in one shot; it must not veto
    # glyphs established by the other shots. Require at least two informative
    # samples rather than mistaking background luminance for moving lettering.
    recalls = [np.count_nonzero((c > 0) & (mask > 0)) / max(1, np.count_nonzero(c))
               for c in candidates if np.count_nonzero(c) / c.size < .45]
    if len(recalls) < 2 or min(recalls) < .6:
        return None, "unstable position or competing background detail"
    return mask, "multi-frame glyph consensus"


def build_glyph_mask(source: Path, region: dict, size: tuple[int, int], output_dir: Path) -> tuple[Path | None, dict]:
    width, height = size
    # Semantic rectangle is authoritative. Only a tiny raster margin is added.
    pad = max(2, min(8, round(min(width, height) * .005)))
    # Vision coordinates may miss the leading glyph. Search a bounded context
    # margin, then erase only the stable raster contours inside it.
    pad_x = max(pad, round(width * .04))
    pad_y = max(pad, round(height * .008))
    x = max(0, math.floor(region['x'] * width) - pad_x)
    y = max(0, math.floor(region['y'] * height) - pad_y)
    right = min(width, math.ceil((region['x'] + region['width']) * width) + pad_x)
    bottom = min(height, math.ceil((region['y'] + region['height']) * height) + pad_y)
    if (right-x)*(bottom-y) > width*height*.04:
        return None, {'source':region['source'], 'method':'interpolate', 'reason':'context exceeds bounded area'}
    times = np.linspace(region['start'] + min(.1, (region['end']-region['start'])/8),
                        max(region['start'], region['end'] - .35), 5).tolist()
    cap = cv2.VideoCapture(str(source))
    frames = []
    try:
        for time in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, time * 1000)
            ok, frame = cap.read()
            if ok and frame.shape[:2] == (height, width):
                frames.append(frame[y:bottom, x:right])
    finally:
        cap.release()
    mask, reason = glyph_mask(frames)
    report = {'source': region['source'], 'start': region['start'], 'end': region['end'],
              'sample_times': times, 'samples_read': len(frames), 'reason': reason,
              'method': 'contour' if mask is not None else 'interpolate'}
    output_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps([region, size], sort_keys=True).encode()).hexdigest()[:20]
    target = output_dir / f'glyph-{key}.png'
    if mask is not None:
        full = np.zeros((height, width), np.uint8)
        full[y:bottom, x:right] = mask
        # FFmpeg removelogo cannot interpolate a glyph touching its image edge:
        # extend both mask and picture with matching reflected context first.
        full = cv2.copyMakeBorder(full, 64, 64, 64, 64, cv2.BORDER_REFLECT)
        encoded_ok, encoded = cv2.imencode('.png', full)
        if not encoded_ok:
            raise RuntimeError('Watermark mask encoding failed')
        encoded.tofile(str(target))
        report['mask_pixels'] = int(np.count_nonzero(full))
        report['mask'] = target.name
    (output_dir / f'glyph-{key}.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return (target if mask is not None else None), report
