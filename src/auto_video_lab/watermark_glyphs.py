"""Position- and hue-independent contours inside semantically approved regions.

Raster evidence is not semantic watermark recognition. No whole-box masks,
source writes, GPU use, or assumption that a logo is a particular platform.
"""
from __future__ import annotations
import math
import cv2
import numpy as np


def color_residual_ratio(before: np.ndarray, after: np.ndarray, mask: np.ndarray) -> float:
    # Gray-only QA is blind to equal-luminance colored marks.
    edge0=np.abs(cv2.Laplacian(before,cv2.CV_32F)).max(axis=2)
    edge1=np.abs(cv2.Laplacian(after,cv2.CV_32F)).max(axis=2)
    selected=(mask>0)&(edge0>24)
    return float(edge1[selected].mean()/max(1.,edge0[selected].mean())) if selected.any() else 1.


def contrast_candidates(frame: np.ndarray) -> np.ndarray:
    # Both polarities and all three channels: black/red/green/blue lettering
    # must not be invisible merely because it is neither white nor cyan/pink.
    # Morphology is local; a uniform sky/shirt/rectangle is not itself a mask.
    side=max(21,min(51,round(min(frame.shape[:2])*.45))) | 1
    kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(side,side))
    bright=cv2.morphologyEx(frame,cv2.MORPH_TOPHAT,kernel).max(axis=2)
    dark=cv2.morphologyEx(frame,cv2.MORPH_BLACKHAT,kernel).max(axis=2)
    # Closing colored letter groups also creates an inverse-polarity envelope
    # through the *background gaps*. A robust local background comparison keeps
    # those gaps out of the contour instead of erasing the entire word's box.
    background=cv2.medianBlur(frame,side)
    deviation=np.abs(frame.astype(np.int16)-background.astype(np.int16)).max(axis=2)
    return (((bright>24)|(dark>24))&(deviation>24)).astype(np.uint8)


def general_glyph_mask(frames: list[np.ndarray], *, dilate: bool=True) -> tuple[np.ndarray | None,str]:
    if len(frames)<5 or any(f.shape!=frames[0].shape for f in frames):
        return None,'insufficient consistent samples'
    candidates=[contrast_candidates(frame) for frame in frames]
    votes=np.stack(candidates).sum(axis=0)
    # A screen-fixed mark has relatively stable foreground values. Without
    # this guard, both-polarity morphology can join alternating wood grain or
    # background highlights into a large false contour across unrelated frames.
    variation=np.std(np.stack(frames).astype(np.float32),axis=0).max(axis=2)
    stable=((votes>=math.ceil(len(frames)*.78))&(variation<24)).astype(np.uint8)
    n,labels,stats,_=cv2.connectedComponentsWithStats(stable,8)
    mask=np.zeros(stable.shape,np.uint8)
    for i in range(1,n):
        if 3<=stats[i,cv2.CC_STAT_AREA]<=mask.size*.18:
            mask[labels==i]=255
    count=np.count_nonzero(mask)
    if not .004<=count/mask.size<=.35:
        return None,'no small temporally stable contrast contours'
    support=[np.count_nonzero((c>0)&(mask>0))/max(1,count) for c in candidates]
    if min(support)<.55:
        return None,'established contour changes position or disappears in sampled range'
    if dilate:
        radius=max(3,min(9,round(min(mask.shape)*.035)))
        mask=cv2.dilate(mask,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*radius+1,2*radius+1)))
    return mask,'temporal local contrast, both polarities and all color channels'
