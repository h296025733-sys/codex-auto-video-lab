import numpy as np
import cv2
import sys
import pytest
from auto_video_lab.util import run_command,CommandError
from auto_video_lab.watermark_telea import stable_glyph_mask,repair_roi,residual_ratio,merge_overlapping_regions
from auto_video_lab.watermarks import approved_regions
from types import SimpleNamespace
import auto_video_lab.watermark_telea as telea

def test_silent_source_keeps_renderer_compatibility_track(tmp_path,monkeypatch):
    output=tmp_path/'final.mp4';output.write_bytes(b'untouched-render')
    monkeypatch.setattr(telea,'ffprobe',lambda p:{'streams':[{'codec_type':'video'}]})
    monkeypatch.setattr(telea.subprocess,'run',lambda *a,**k:pytest.fail('silent source must not remux away its compatibility track'))
    telea.preserve_original_audio(SimpleNamespace(root=tmp_path),{'watermark_only':True,'clips':[{'source':'source.mp4','start':0,'speed':1}]},output)
    assert output.read_bytes()==b'untouched-render'

def frames(moving=False):
    result=[]
    for i in range(9):
        f=np.full((140,220,3),30+i*3,np.uint8)
        cv2.putText(f,'TikTok',(8+i*20 if moving else 50,70),cv2.FONT_HERSHEY_SIMPLEX,.8,(245,245,245),2)
        result.append(f)
    return result

def test_stable_glyph_and_pixel_preservation():
    source=frames();mask,_=stable_glyph_mask(source)
    assert mask is not None
    for f in source:
        fixed=repair_roi(f,mask)
        assert np.array_equal(f[mask==0],fixed[mask==0])
        assert residual_ratio(f,fixed,mask)<.55

def test_blank_and_moving_marks_not_large_union():
    assert stable_glyph_mask([np.full((140,220,3),255,np.uint8)]*9)[0] is None
    assert stable_glyph_mask(frames(True))[0] is None
    assert stable_glyph_mask(frames()[:2])[0] is None

def test_v3_search_box_does_not_override_semantic_rejection():
    r=dict(source='inputs/a.mp4',start=0,end=30.08,x=.754,y=.875,width=.228,height=.117,
           kind='overlay_watermark',safe_to_remove=True,evidence_times=[0,15,29.9],evidence='Fixed lower-right overlay, safe background only.')
    c=dict(version=3,inspected=True,regions=[r])
    assert len(approved_regions(c))==1
    r['safe_to_remove']=False
    assert approved_regions(c)==[]
    r['safe_to_remove']=True;r['kind']='caption'
    assert approved_regions(c)==[]

def test_overlapping_same_time_marks_merge_but_jumping_ranges_never_merge():
    base=dict(source='a',start=0,end=30,x=.858,y=.866,width=.126,height=.091)
    account={**base,'x':.756,'y':.951,'width':.234,'height':.031}
    generator={**base,'x':.844,'y':.963,'width':.137,'height':.026}
    merged=merge_overlapping_regions([base,account,generator])
    assert len(merged)==1 and merged[0]['grouped_regions']==3
    assert len(merge_overlapping_regions([base,{**account,'start':5}]))==2

def test_native_command_preserves_stdout_stderr_and_error_contract():
    r=run_command([sys.executable,'-c',"import sys; print('out'); print('err',file=sys.stderr)"])
    assert r.stdout.strip()=='out' and r.stderr.strip()=='err'
    with pytest.raises(CommandError):
        run_command([sys.executable,'-c','raise SystemExit(3)'])
    assert run_command([sys.executable,'-c','raise SystemExit(3)'],check=False).returncode==3
