"""Preserve decoded source planes outside actual glyphs; audit final delivery.

No sharpening/upscaling. CRF is not a fidelity measurement. Final MP4 is
checked separately against the source, excluding only repair pixels/context.
"""
from __future__ import annotations

import math
import cv2
import numpy as np
import av

from .media import ffprobe, first_stream
from .util import safe_resolve_within, write_json


def native_420_supported(stream: dict) -> bool:
    rotation = float(stream.get('tags', {}).get('rotate', 0))
    for side in stream.get('side_data_list', []):
        rotation = float(side.get('rotation', rotation))
    return (stream.get('pix_fmt') in {'yuv420p', 'yuvj420p'}
            and int(stream.get('width', 0)) % 2 == 0
            and int(stream.get('height', 0)) % 2 == 0
            and rotation % 360 == 0)


def color_args(stream: dict) -> list[str]:
    # Preserve known tags. Never guess BT.709 when the source is unspecified.
    args = []
    for key, option in [('color_range', '-color_range'), ('color_space', '-colorspace'),
                        ('color_transfer', '-color_trc'), ('color_primaries', '-color_primaries')]:
        value = stream.get(key)
        if value and value not in {'unknown', 'unspecified', 'reserved'}:
            args.extend([option, str(value)])
    return args


def decoded_planes(frame) -> list[np.ndarray]:
    """Copy native 8-bit YUV420 planes WITHOUT reformat/RGB round trip."""
    if frame.format.name not in {'yuv420p', 'yuvj420p'}:
        raise ValueError('Native plane format changed during decode')
    return [np.frombuffer(plane, np.uint8).reshape(plane.height, plane.line_size)[:, :plane.width].copy()
            for plane in frame.planes]


def repair_planes(planes: list[np.ndarray], prepared: list[dict], time: float) -> list[np.ndarray]:
    height, width = planes[0].shape
    # Union only currently active masks, not their bounding rectangles.
    for item in prepared:
        r = item['region']
        if not r['start'] <= time < r['end']:
            continue
        x, y, right, bottom = item['bounds']
        # Even coordinates align the corresponding chroma samples precisely.
        left, top = x//2*2, y//2*2
        end_x, end_y = min(width, (right+1)//2*2), min(height, (bottom+1)//2*2)
        mask = np.zeros((end_y-top, end_x-left), np.uint8)
        mask[y-top:bottom-top, x-left:right-left] = item['mask']
        chroma = mask.reshape(mask.shape[0]//2, 2, mask.shape[1]//2, 2).max(axis=(1, 3))
        for index, plane in enumerate(planes):
            scale = 1 if index == 0 else 2
            active_mask = mask if index == 0 else chroma
            roi = plane[top//scale:end_y//scale, left//scale:end_x//scale]
            padded = cv2.copyMakeBorder(roi, 16, 16, 16, 16, cv2.BORDER_REFLECT_101)
            padded_mask = cv2.copyMakeBorder(active_mask, 16, 16, 16, 16, cv2.BORDER_CONSTANT)
            # Telea's scalar-channel gradient term can overshoot nearly neutral
            # U/V and create purple/cyan patches. Keep luminance detail repair,
            # but use bounded Navier-Stokes interpolation for chroma only.
            method = cv2.INPAINT_TELEA if index == 0 else cv2.INPAINT_NS
            fixed = cv2.inpaint(padded, padded_mask, 4 if index == 0 else 2, method)[16:-16, 16:-16]
            # Explicit pixel assignment: untouched samples remain byte-identical.
            roi[active_mask != 0] = fixed[active_mask != 0]
    return planes


def fidelity_masks(shape: tuple[int, int], reports: list[dict], folder) -> list[np.ndarray]:
    h, w = shape
    changed = np.zeros((h, w), np.uint8)
    for r in reports:
        if not r.get('processed') or not r.get('mask'):
            continue
        x, y, right, bottom = r['bounds']
        local = cv2.imdecode(np.fromfile(str(folder/r['mask']), np.uint8), cv2.IMREAD_GRAYSCALE)
        changed[y:bottom, x:right] |= local
    # Exclude small codec/chroma neighborhoods, not the whole semantic rectangle.
    changed = cv2.dilate(changed, np.ones((17, 17), np.uint8))
    chroma = changed.reshape(h//2, 2, w//2, 2).max(axis=(1, 3))
    return [changed == 0, chroma == 0, chroma == 0]


def psnr(mse: float) -> float:
    return min(100., 10*math.log10(255**2/max(mse, 1e-10)))


def verify_fidelity(source, output, reports, folder) -> dict:
    """Every native decoded Y/U/V frame; no temporal subsampling or resizing."""
    stream = first_stream(ffprobe(source), 'video') or {}
    if not native_420_supported(stream):
        return {'status': 'not_supported', 'reason': 'native fidelity audit requires unrotated 8-bit YUV420'}
    totals = np.zeros(3, np.float64); worst = np.zeros(3, np.float64); count = 0
    with av.open(str(source)) as before, av.open(str(output)) as after:
        before.streams.video[0].thread_count = 2
        after.streams.video[0].thread_count = 2
        out_frames = iter(after.decode(video=0))
        masks = fidelity_masks((stream['height'], stream['width']), reports, folder)
        for a in before.decode(video=0):
            b = next(out_frames, None)
            if b is None or (a.width, a.height) != (b.width, b.height):
                raise RuntimeError('Fidelity audit: output frames/dimensions differ')
            if a.time is not None and b.time is not None and abs(a.time-b.time) > .001:
                raise RuntimeError('Fidelity audit: source/output timestamps differ')
            for i, (original, delivered, outside) in enumerate(zip(decoded_planes(a), decoded_planes(b), masks)):
                delta = original.astype(np.float32)-delivered.astype(np.float32)
                mse = float(np.mean(np.square(delta[outside])))
                totals[i] += mse; worst[i] = max(worst[i], mse)
            count += 1
        if count < 1 or next(out_frames, None) is not None:
            raise RuntimeError('Fidelity audit: unexpected final frame count')
    average = [psnr(v/count) for v in totals]
    minimum = [psnr(v) for v in worst]
    passed = min(average) >= 48 and min(minimum) >= 44
    return {'status': 'passed' if passed else 'below_threshold', 'frames': count,
            'scope': 'every-frame native Y/U/V outside dilated actual glyph masks; not repaired-texture proof',
            'mean_psnr_yuv_db': average, 'minimum_frame_psnr_yuv_db': minimum,
            'threshold_mean_db': 48, 'threshold_minimum_frame_db': 44}


def audit_delivery(job, plan, output, summary) -> dict | None:
    if plan.get('watermark_only') is not True or plan.get('watermark_cleanup', {}).get('version') != 3:
        return None
    source = safe_resolve_within(job.root/plan['clips'][0]['source'], job.root)
    folder = job.reports/'watermark-masks'
    result = verify_fidelity(source, output, summary['regions'], folder)
    write_json(folder/'fidelity.json', result)
    return result
