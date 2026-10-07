import cv2
import numpy as np
import pytest
import auto_video_lab.watermark_timing as timing
from auto_video_lab.watermarks import approved_regions
from auto_video_lab.watermark_telea import merge_overlapping_regions,semantic_contours,glyph_touches_search_boundary,fill_small_glyph_holes,localization_bounds,stable_glyph_mask

def region(**patch):
    return dict(dict(source='a',start=0,end=30,x=.38,y=.973,width=.614,height=.027,
        kind='overlay_watermark',safe_to_remove=True,evidence_times=[0,29.9],
        evidence='Screen-fixed platform attribution over non-critical background.'),**patch)

def test_thin_strip_is_not_a_broad_or_semantically_unsafe_wipe():
    assert len(approved_regions(dict(version=3,inspected=True,regions=[region()])))==1
    assert approved_regions(dict(version=3,inspected=True,regions=[region(safe_to_remove=False)]))==[]
    for version,r in [(2,region()),(3,region(y=.8,height=.15))]:
        with pytest.raises(ValueError,match='bounded'):
            approved_regions(dict(version=version,inspected=True,regions=[r]))

def test_account_and_icon_group_preserves_member_permissions():
    account=region()
    icon=region(x=.85,y=.89,width=.13,height=.085)
    merged=merge_overlapping_regions([account,icon])
    assert len(merged)==1 and len(merged[0]['semantic_members'])==2
    assert len(merge_overlapping_regions([account,{**icon,'end':10}]))==2
    assert len(merge_overlapping_regions([account,{**icon,'y':.1}]))==2

def test_icon_counter_is_repaired_without_bridging_words_or_filling_large_background():
    mask=np.zeros((90,140),np.uint8)
    cv2.circle(mask,(25,25),10,255,3)
    cv2.rectangle(mask,(60,10),(130,80),255,3)
    fixed=fill_small_glyph_holes(mask,(720,1280))
    assert fixed[25,25]==255
    assert fixed[45,90]==0 # large background remains
    assert not fixed[:,42:55].any() # open gap between glyphs remains

def test_shifted_box_preserves_whole_word_not_only_seeded_capitals():
    r=dict(x=.847,y=.943,width=.128,height=.023)
    bounds=localization_bounds(r,(720,1280));x,y,right,bottom=bounds
    frames=[]
    for i in range(9):
        f=np.full((1280,720,3),50+i*5,np.uint8)
        cv2.putText(f,'Dola AI',(610,1260),cv2.FONT_HERSHEY_SIMPLEX,.6,(240,240,240),2)
        frames.append(f[y:bottom,x:right])
    audit,_=stable_glyph_mask(frames,dilate=False)
    assert np.array_equal(semantic_contours(audit,r,bounds,(720,1280)),audit)

def test_unrelated_search_edge_is_not_erased_but_clipped_target_is_rejected():
    audit=np.zeros((150,300),np.uint8)
    audit[0:40,0:3]=255 # unrelated seam above/left of semantic mark
    audit[60:80,130:145]=255 # target text extending beyond a model's estimate
    audit[60:80,295:]=255 # actual target clipped at search edge
    r=dict(x=.45,y=.45,width=.55,height=.15)
    mask=semantic_contours(audit,r,(10,10,310,160),(400,160))
    assert not mask[:40,:3].any()
    # Test a target seeded by its actual semantic extent; preserve whole component.
    r=dict(x=.3,y=.45,width=.5,height=.15)
    mask=semantic_contours(audit,r,(10,10,310,160),(400,160))
    assert np.array_equal(mask[60:80,130:145],audit[60:80,130:145])
    assert glyph_touches_search_boundary(mask,(10,10,310,160),(400,160))

@pytest.mark.parametrize('actual_end,expected',[(6.5,(0,6.5)),(15,None)])
def test_clock_recovers_beyond_two_seconds_only_with_real_bounded_frames(monkeypatch,actual_end,expected):
    class Capture:
        def __init__(self,*args):self.n=0
        def get(self,key):return 300
        def read(self):
            self.n+=1
            return True,np.full((4,4,3),int((self.n-1)/10<actual_end),np.uint8)
        def release(self):pass
    monkeypatch.setattr(timing.cv2,'VideoCapture',Capture)
    monkeypatch.setattr(timing,'visible_glyph',lambda roi,*a:bool(roi[0,0,0]))
    result,reason=timing.refine_interval('source',dict(start=0,end=2.95),(0,0,4,4),np.ones((4,4)),
        [np.ones((4,4,3))]*9,[0,.3,1,2,2.8],fps=10,duration=30)
    assert result==expected
    assert 'eight-second' in reason
