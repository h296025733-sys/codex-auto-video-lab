"""Bounded CFR appearance clocks for already semantically approved fixed marks.

No moving-object tracking, new semantic regions, or timing changes to the video.
"""
from __future__ import annotations
import math
import cv2
import numpy as np
from .watermark_glyphs import contrast_candidates


def visible_glyph(roi, audit_mask, reference):
    selected=audit_mask>0
    difference=np.abs(roi.astype(np.float32)-reference).max(axis=2)
    observed=(contrast_candidates(roi)>0)&(difference<40)
    return bool(np.count_nonzero(observed&selected)/max(1,np.count_nonzero(selected))>=.65)


def choose_interval(matches, *, first_frame, source_frames, fps, witnessed_times):
    groups=[];begin=None
    for i,value in enumerate([*matches,False]):
        if value and begin is None:begin=i
        if not value and begin is not None:
            groups.append((begin+first_frame,i+first_frame));begin=None
    # Every retained sample must agree with this single contiguous appearance.
    witnesses=[math.ceil(t*fps-1e-6) for t in witnessed_times]
    valid=[(a,b) for a,b in groups if all(a<=w<b for w in witnesses)]
    if len(valid)!=1:return None,'samples do not confirm one continuous fixed-position interval'
    a,b=valid[0]
    if (a==first_frame and a>0) or (b==first_frame+len(matches) and b<source_frames):
        return None,'appearance extends beyond bounded two-second timing search'
    if any(d-c>=max(2,round(fps*.25)) for c,d in groups if (c,d)!=(a,b)):
        return None,'additional appearance requires separate interval evidence'
    return (a/fps,b/fps),'CFR appearance boundaries confirmed from original decoded frames'


def refine_interval(source, region, bounds, mask, frames, sample_times, *, fps, duration):
    # Decode once with a bounded eight-second reserve. First apply the original
    # two-second decision; expand ONLY if that window truncated a witnessed run.
    # No extrapolation, gap filling, moving tracking or merging recurring runs.
    first=max(0,math.floor((region['start']-8)*fps))
    stop=min(math.ceil(duration*fps),math.ceil((region['end']+8)*fps))
    reference=np.median(np.stack(frames).astype(np.float32),axis=0)
    cap=cv2.VideoCapture(str(source),cv2.CAP_FFMPEG,[cv2.CAP_PROP_N_THREADS,2])
    expected=int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if expected<=0:
        cap.release()
        return None,'source has no reliable CFR frame count'
    stop=min(stop,expected)
    x,y,right,bottom=bounds
    matches=[];count=0
    try:
        while count<stop:
            ok,frame=cap.read()
            if not ok:break
            if count>=first:matches.append(visible_glyph(frame[y:bottom,x:right],mask,reference))
            count+=1
    finally:cap.release()
    if count<min(stop,expected):return None,'source timing verification could not decode expected frames'
    near_first=max(0,math.floor((region['start']-2)*fps))
    near_stop=min(expected,math.ceil((region['end']+2)*fps))
    interval,reason=choose_interval(matches[near_first-first:near_stop-first],first_frame=near_first,
        source_frames=expected,fps=fps,witnessed_times=sample_times)
    if interval is None and reason=='appearance extends beyond bounded two-second timing search':
        interval,reason=choose_interval(matches,first_frame=first,source_frames=expected,
            fps=fps,witnessed_times=sample_times)
        if interval is not None:
            reason='bounded eight-second recovery; every decoded glyph and both boundaries witnessed'
        elif reason=='appearance extends beyond bounded two-second timing search':
            reason='appearance extends beyond bounded eight-second recovery; semantic reinspection required'
    return interval,reason
