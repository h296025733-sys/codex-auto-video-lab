"""Bounded source-space watermark repair or bottom-edge crop. No audio/retiming.

Semantic detection belongs to the shared planner. This module executes only
approved small static overlay rectangles, never model-provided filter strings.
"""
from __future__ import annotations

import math
from .media import ffprobe, first_stream, duration_seconds
from .util import safe_resolve_within


def approved_regions(cleanup: object) -> list[dict]:
    if not isinstance(cleanup, dict) or cleanup.get("inspected") is not True:
        return []
    result = []
    raw = cleanup.get("regions", [])
    if not isinstance(raw, list):
        return []
    for r in raw[:12]:
        if not isinstance(r, dict) or r.get("safe_to_remove") is not True or r.get("kind") != "overlay_watermark":
            continue
        nums = [r.get(k) for k in ("start", "end", "x", "y", "width", "height")]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in nums):
            raise ValueError("Invalid watermark rectangle")
        start, end, x, y, w, h = nums
        v3 = cleanup.get('version') == 3
        compact = 0 < w <= (.38 if v3 else .3) and 0 < h <= (.20 if v3 else .12) and w*h <= (.06 if v3 else .025)
        thin_strip = v3 and min(w,h) > 0 and min(w,h) <= .04 and w*h <= .04
        if not (0 <= start < end and 0 <= x < x+w <= 1 and 0 <= y < y+h <= 1 and (compact or thin_strip)):
            raise ValueError("Watermark cleanup exceeds bounded area")
        method = r.get("method", "interpolate")
        if method not in ("interpolate", "edge_crop"):
            raise ValueError("Unknown watermark cleanup method")
        if method == "edge_crop":
            fraction = r.get("crop_fraction")
            if not isinstance(fraction, (int, float)) or isinstance(fraction, bool) or not math.isfinite(fraction) or not (.01 <= fraction <= .06):
                raise ValueError("Bottom-edge crop exceeds bounded area")
            if start > .75 or y < .94 or y+h < .985:
                raise ValueError("Bottom-edge crop is not a fixed edge watermark")
        times = sorted(set(t for t in r.get("evidence_times", []) if isinstance(t,(int,float)) and not isinstance(t,bool) and math.isfinite(t) and start-.05 <= t <= end+.05))
        if len(times) < 2 or times[0] > start+.75 or times[-1] < end-.75 or times[-1]-times[0] < min(.5,(end-start)*.5) or len(str(r.get("evidence", "")).strip()) < 12:
            raise ValueError("Watermark cleanup lacks spanning visual evidence")
        if not isinstance(r.get("source"), str):
            raise ValueError("Watermark source missing")
        result.append(r)
    return result


def display_dimensions(probe: dict) -> tuple[int, int]:
    stream = first_stream(probe, "video") or {}
    width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    rotation = float(stream.get("tags", {}).get("rotate", 0))
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            rotation = float(side["rotation"])
    if abs(rotation % 180 - 90) < 1:
        width, height = height, width
    return width, height


def interpolation_bounds(r: dict, width: int, height: int) -> tuple[int,int,int,int]:
    # Contact-sheet coordinates are estimates, not OCR-perfect glyph bounds.
    # A small context margin catches antialiasing/shadows and a partially clipped
    # first/last glyph. The shared planner checks this margin for protected content.
    x = max(0, math.floor((r["x"]-.03)*width))
    y = max(0, math.floor((r["y"]-.004)*height))
    right = min(width, math.ceil((r["x"]+r["width"]+.03)*width))
    bottom = min(height, math.ceil((r["y"]+r["height"]+.004)*height))
    # Margin must not turn a small repair into a large erasure.
    if (right-x)*(bottom-y) > width*height*.04:
        raise ValueError("Watermark cleanup context margin exceeds safe area")
    return x,y,right,bottom


def source_watermark_filter(job, plan: dict, source: str, source_start: float, source_end: float, cache: dict) -> str:
    # V3 is handled by a bounded, audited Telea prepass before input assembly.
    # Failed masks stay original; never silently substitute a large delogo box.
    if plan.get('watermark_cleanup', {}).get('version') == 3:
        return ""
    regions = [r for r in approved_regions(plan.get("watermark_cleanup")) if r["source"] == source and r["start"] < source_end and r["end"] > source_start]
    if not regions:
        return ""
    if source not in cache:
        file = safe_resolve_within(job.root / source, job.root)
        probe = ffprobe(file)
        cache[source] = (*display_dimensions(probe), duration_seconds(probe))
    width, height, duration = cache[source]
    if width < 16 or height < 16:
        raise ValueError("Watermark source has no usable picture")
    filters = []
    contour_filters = []
    edge_crop = 0.0
    for r in regions:
        if r["end"] > duration+.05:
            raise ValueError("Watermark clock exceeds source")
        if r.get("method", "interpolate") == "edge_crop":
            if r["start"] > .75 or r["end"] < duration-.75:
                raise ValueError("Bottom-edge crop does not span the source")
            edge_crop = max(edge_crop, float(r["crop_fraction"]))
            continue
        if plan.get("watermark_cleanup", {}).get("version") == 2:
            from .watermark_masks import build_glyph_mask
            import json
            key = ('glyph', json.dumps(r, sort_keys=True))
            if key not in cache:
                file = safe_resolve_within(job.root / source, job.root)
                cache[key] = build_glyph_mask(file, r, (width, height), job.root / 'reports' / 'watermark-masks')[0]
            mask_path = cache[key]
            if mask_path is not None:
                # Generated basename + job-relative path; no model-controlled
                # paths or filter fragments enter the FFmpeg expression.
                mask_name = mask_path.relative_to(job.root).as_posix()
                start, end = max(source_start,r['start']), min(source_end,r['end'])
                contour_filters.append(f"pad=iw+128:ih+128:64:64,fillborders=left=64:right=64:top=64:bottom=64:mode=reflect,removelogo=filename='{mask_name}':enable='gte(t,{start:.6f})*lt(t,{end:.6f})',crop=iw-128:ih-128:64:64")
                continue
        # A two-pixel smeared perimeter makes interpolation valid even at the
        # original image edge. Strip exactly that padding, not source pixels.
        x,y,right,bottom = interpolation_bounds(r,width,height)
        if right-x < 2 or bottom-y < 2:
            continue
        start, end = max(source_start,r["start"]), min(source_end,r["end"])
        filters.append(f"delogo=x={x+2}:y={y+2}:w={right-x}:h={bottom-y}:show=0:enable='gte(t,{start:.6f})*lt(t,{end:.6f})'")
    chain = (','.join(contour_filters) + ',') if contour_filters else ""
    if filters:
        chain += "pad=iw+4:ih+4:2:2,fillborders=left=2:right=2:top=2:bottom=2:mode=smear," + ",".join(filters) + ",crop=iw-4:ih-4:2:2,"
    if edge_crop:
        keep_height = max(2, math.floor(height*(1-edge_crop)/2)*2)
        scaled_width = max(width, math.ceil((width*height/keep_height)/2)*2)
        x_offset = max(0, (scaled_width-width)//2)
        chain += f"crop={width}:{keep_height}:0:0,scale={scaled_width}:{height}:flags=lanczos,crop={width}:{height}:{x_offset}:0,"
    return chain
