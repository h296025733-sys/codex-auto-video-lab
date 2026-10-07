import cv2
import numpy as np
from auto_video_lab.watermark_timing import choose_interval,choose_contrast_interval,visible_glyph
from auto_video_lab.watermark_glyphs import general_glyph_mask
from auto_video_lab.watermark_telea import merge_overlapping_regions

def test_separate_text_and_icon_with_overlapping_searches_are_one_group():
    text=dict(source='inputs/a.mp4',start=0,end=12,x=.025,y=.93,width=.126,height=.016)
    icon={**text,'x':.019,'y':.948,'width':.052,'height':.028}
    merged=merge_overlapping_regions([text,icon])
    assert len(merged)==1 and merged[0]['grouped_regions']==2
    assert len(merge_overlapping_regions([text,{**icon,'start':6}]))==2
    assert len(merge_overlapping_regions([text,{**icon,'y':.6}]))==2

def test_clock_refines_model_early_end_and_start():
    # Real appearance0..6s and6..12s, model0..4.11 and4.74..12.
    args=dict(source_frames=288,fps=24)
    assert choose_interval([True]*144+[False]*3,first_frame=0,witnessed_times=[0,1,2,3,4.0],**args)[0]==(0,6)
    assert choose_interval([False]*79+[True]*144,first_frame=65,witnessed_times=[6.525,7.42,8.32,10,11.88],**args)[0]==(6,12)

def test_clock_does_not_extrapolate_or_merge_separate_appearances():
    args=dict(first_frame=30,source_frames=300,fps=24,witnessed_times=[2,3])
    assert choose_interval([True]*100,**args)[0] is None
    assert choose_interval([False]*10+[True]*40+[False]*10+[True]*30+[False]*10,**args)[0] is None


def test_model_edge_samples_outside_actual_shot_are_trimmed_only_after_five_contiguous_witnesses():
    # Source #553: top-left appears in decoded frames117..169, although the
    # model's last 7.28s sample is already after the shot boundary.
    top=[False]*117+[True]*53+[False]*(313-170)
    observed_top=[4.955,5.342,5.73,6.117,6.505,6.893,7.28]
    assert choose_interval(top,first_frame=0,source_frames=313,fps=24,
                           witnessed_times=observed_top)[0]==(117/24,170/24)
    # Bottom-right starts after the first sample and disappears before the last.
    bottom=[False]*207+[True]*65+[False]*(313-272)
    observed_bottom=[8.27,8.69,9.11,9.53,9.95,10.37,10.79,11.21,11.63]
    assert choose_interval(bottom,first_frame=0,source_frames=313,fps=24,
                           witnessed_times=observed_bottom)[0]==(207/24,272/24)
    # A gap inside the witnessed mark is not permission to join separate runs.
    split=[False]*20+[True]*30+[False]*10+[True]*30+[False]*20
    assert choose_interval(split,first_frame=0,source_frames=110,fps=24,
                           witnessed_times=[1,1.25,1.5,1.75,2,2.5,2.75,3,3.25,3.5])[0] is None

def test_template_needs_same_local_shape_and_color():
    clean=np.full((130,160,3),165,np.uint8)
    frame=clean.copy();cv2.putText(frame,'@jump',(20,70),cv2.FONT_HERSHEY_SIMPLEX,.55,(220,30,30),2)
    audit,_=general_glyph_mask([frame]*9,dilate=False)
    assert audit is not None
    assert visible_glyph(frame,audit,frame.astype(np.float32))
    assert not visible_glyph(clean,audit,frame.astype(np.float32))
    other=clean.copy();cv2.putText(other,'@jump',(20,70),cv2.FONT_HERSHEY_SIMPLEX,.55,(20,220,20),2)
    assert not visible_glyph(other,audit,frame.astype(np.float32))


def test_adaptive_contour_keeps_fixed_glyph_across_changing_background_but_not_moving_text():
    levels=[20,32,44,56,68,80,92,104,116]
    frames=[]
    for level in levels:
        frame=np.full((100,230,3),level,np.uint8)
        cv2.putText(frame,'AI MARK',(18,67),cv2.FONT_HERSHEY_SIMPLEX,.9,
                    (min(255,level+90),)*3,3)
        frames.append(frame)
    strict,_=general_glyph_mask(frames,dilate=False)
    adaptive,_=general_glyph_mask(frames,dilate=False,vote_share=.55,variation_limit=60)
    assert adaptive is not None
    assert np.count_nonzero(adaptive)>max(0,np.count_nonzero(strict) if strict is not None else 0)
    moving=[]
    for i in range(9):
        frame=np.full((100,230,3),45,np.uint8)
        cv2.putText(frame,'AI',(10+i*20,67),cv2.FONT_HERSHEY_SIMPLEX,.9,(210,210,210),3)
        moving.append(frame)
    assert general_glyph_mask(moving,dilate=False,vote_share=.55,variation_limit=60)[0] is None


def test_low_opacity_shoulder_extends_one_core_without_crossing_a_gap():
    scores=[.02]*110+[.18,.22,.28,.34,.53]+[.8]*45+[.63]*22+[.24,.20]+[.03]*15
    interval,reason=choose_contrast_interval(scores,first_frame=0,source_frames=len(scores),
                                             fps=24,core=(115/24,161/24),threshold=.16)
    assert interval==(110/24,184/24),reason
    split=scores+[.01]*10+[.8]*12+[.01]*10
    assert choose_contrast_interval(split,first_frame=0,source_frames=len(split),
                                    fps=24,core=(115/24,161/24),threshold=.16)[0]==interval
    clipped=[.8]*20+[.02]*20
    assert choose_contrast_interval(clipped,first_frame=100,source_frames=300,
                                    fps=24,core=(110/24,115/24),threshold=.16)[0] is None
