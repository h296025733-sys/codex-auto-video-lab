import cv2
import numpy as np
import json
from types import SimpleNamespace
import pytest
import auto_video_lab.watermark_telea as telea
from auto_video_lab.watermark_telea import (localization_bounds, stable_glyph_mask,
    glyph_touches_search_boundary, repair_roi, residual_ratio)

def test_shifted_model_box_covers_complete_glyph_not_only_top_edge():
    r=dict(x=.847,y=.943,width=.128,height=.023)
    bounds=localization_bounds(r,(720,1280))
    x,y,right,bottom=bounds
    frames=[]
    for i in range(9):
        f=np.full((1280,720,3),50+i*5,np.uint8)
        cv2.putText(f,'Dola AI',(610,1260),cv2.FONT_HERSHEY_SIMPLEX,.6,(240,240,240),2)
        frames.append(f[y:bottom,x:right])
    audit,_=stable_glyph_mask(frames,dilate=False)
    mask,_=stable_glyph_mask(frames)
    assert audit is not None and mask is not None
    assert not glyph_touches_search_boundary(audit,bounds,(720,1280))
    assert np.all(mask[audit>0]>0)
    # Old box ended at1245: more than half the complete text was never audited.
    old_mask=mask.copy();old_mask[1245-y:]=0
    assert np.count_nonzero(old_mask & audit)/np.count_nonzero(audit)<.6
    assert residual_ratio(frames[0],repair_roi(frames[0],old_mask),audit)>.55
    assert residual_ratio(frames[0],repair_roi(frames[0],mask),audit)<.55

def test_incomplete_search_contour_is_not_accepted():
    mask=np.zeros((50,100),np.uint8);mask[-1,50]=255
    assert glyph_touches_search_boundary(mask,(20,40,120,90),(720,1280))
    assert not glyph_touches_search_boundary(mask,(20,1230,120,1280),(720,1280))

def test_margin_never_wraps_outside_picture():
    assert localization_bounds(dict(x=0,y=0,width=1,height=1),(720,1280))==(0,0,720,1280)

@pytest.mark.parametrize('fault',['none','residual','missing_frame'])
def test_final_every_frame_audit_catches_unsampled_fault(tmp_path,monkeypatch,fault):
    folder=tmp_path/'reports/watermark-masks';folder.mkdir(parents=True)
    source=tmp_path/'input.mp4';output=tmp_path/'output.mp4'
    source.touch();output.touch()
    f=np.full((140,220,3),30,np.uint8)
    cv2.putText(f,'Dola AI',(50,70),cv2.FONT_HERSHEY_SIMPLEX,.8,(245,245,245),2)
    mask,_=stable_glyph_mask([f]*9)
    audit,_=stable_glyph_mask([f]*9,dilate=False)
    cv2.imwrite(str(folder/'mask.png'),audit)
    # Keep margins outside the approved bounds, matching real final-frame QA.
    before=cv2.copyMakeBorder(f,5,5,5,5,cv2.BORDER_CONSTANT)
    cleaned=cv2.copyMakeBorder(repair_roi(f,mask),5,5,5,5,cv2.BORDER_CONSTANT)
    outputs=[cleaned.copy() for _ in range(10)]
    if fault=='residual': outputs[5]=before.copy()
    if fault=='missing_frame': outputs.pop()
    report={'source':'input.mp4','start':0,'end':2,'bounds':[5,5,225,145],
            'processed':True,'mask':'mask.png','audit_mask':'mask.png','sample_times':[0,1.8]}
    (folder/'execution.json').write_text(json.dumps({'processed':1,'deferred':0,'regions':[report]}))
    monkeypatch.setattr(telea,'sample_frame',lambda p,t:before if p==source else cleaned)
    class Capture:
        def __init__(self,path,*args):self.frames=iter([before]*10 if path==str(source) else outputs)
        def read(self):
            f=next(self.frames,None);return f is not None,f
        def get(self,key):return 5
        def release(self):pass
    monkeypatch.setattr(telea.cv2,'VideoCapture',Capture)
    run=lambda:telea.verify_final_watermarks(SimpleNamespace(root=tmp_path),{'watermark_only':True,'watermark_cleanup':{'version':3}},output)
    if fault=='missing_frame':
        with pytest.raises(RuntimeError,match='preserve all source frames'):run()
    else:
        run()
        result=json.loads((folder/'execution.json').read_text())
        assert result['regions'][0]['all_frame_checks']==10
        assert result['deferred']==(1 if fault=='residual' else 0)
