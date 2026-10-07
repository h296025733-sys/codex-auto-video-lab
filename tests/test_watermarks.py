import copy
from types import SimpleNamespace
from pathlib import Path
import pytest
from auto_video_lab.watermarks import approved_regions, source_watermark_filter, display_dimensions
from auto_video_lab.render import build_render_command

def cleanup():
    return {"inspected":True,"regions":[{"source":"inputs/a.mp4","start":2,"end":6,"x":.8,"y":.95,"width":.15,"height":.025,"kind":"overlay_watermark","safe_to_remove":True,"evidence_times":[2,5.9],"evidence":"Fixed platform mark over empty wall in all sampled frames."}]}

def test_source_clock_filter_and_no_geometry_loss():
    cache={"inputs/a.mp4":(720,1280,10)}
    s=source_watermark_filter(None,{"watermark_cleanup":cleanup()},"inputs/a.mp4",3,8,cache)
    assert "gte(t,3.000000)*lt(t,6.000000)" in s
    assert "delogo=x=556:y=1212:w=152:h=44" in s
    assert s.endswith("crop=iw-4:ih-4:2:2,")
    assert "setpts" not in s and "audio" not in s and "scale" not in s

def test_source_identity_and_unaffected_span():
    assert source_watermark_filter(None,{"watermark_cleanup":cleanup()},"inputs/b.mp4",2,6,{})==""
    assert source_watermark_filter(None,{"watermark_cleanup":cleanup()},"inputs/a.mp4",6,8,{})==""
    assert approved_regions({})==[]

def test_full_source_bottom_edge_crop_uses_bounded_uniform_zoom():
    c=cleanup();r=c['regions'][0];r.update({'start':0,'end':10,'y':.966,'height':.022,'safe_to_remove':True,'method':'edge_crop','crop_fraction':.04,'evidence_times':[0,5,9.8]})
    s=source_watermark_filter(None,{'watermark_cleanup':c},'inputs/a.mp4',2,6,{'inputs/a.mp4':(1080,1920,10)})
    assert s=='crop=1080:1842:0:0,scale=1126:1920:flags=lanczos,crop=1080:1920:23:0,'
    assert 'delogo' not in s and 'setpts' not in s and 'audio' not in s

def test_bottom_edge_crop_must_span_source_and_stay_at_edge():
    for patch in ({'end':9},{'y':.9},{'crop_fraction':.08}):
        c=cleanup();r=c['regions'][0];r.update({'start':0,'end':10,'y':.966,'height':.022,'method':'edge_crop','crop_fraction':.04,'evidence_times':[0,5,9.8],**patch})
        with pytest.raises(ValueError):source_watermark_filter(None,{'watermark_cleanup':c},'inputs/a.mp4',0,5,{'inputs/a.mp4':(1080,1920,10)})

def test_product_and_uncertain_marks_not_executed():
    c=cleanup();c['regions'][0]['kind']='embedded_brand';assert approved_regions(c)==[]
    c=cleanup();c['regions'][0]['safe_to_remove']=False;assert approved_regions(c)==[]

def test_bad_geometry_and_evidence_rejected():
    for key,value in [('width',.5),('x',float('nan')),('evidence_times',[2,2.1])]:
        c=cleanup();c['regions'][0][key]=value
        with pytest.raises(ValueError):approved_regions(c)

def test_upright_coordinate_space():
    assert display_dimensions({'streams':[{'codec_type':'video','width':1280,'height':720,'side_data_list':[{'rotation':-90}]}]})==(720,1280)

def test_main_and_pip_cleanup_precede_retime_scale_and_captions(tmp_path,monkeypatch):
    import auto_video_lab.render as renderer
    monkeypatch.setattr(renderer,'source_watermark_filter',lambda *args: 'delogo=x=10:y=10:w=20:h=20,')
    (tmp_path/'work').mkdir();(tmp_path/'output').mkdir()
    clip={'source':'inputs/a.mp4','kind':'video','start':2,'end':6,'speed':1.2,'fit':'contain','has_audio':True,'mute':False,'audio_gain_db':0}
    inset={'source':'inputs/b.mp4','kind':'video','source_start':1,'source_end':3,'start':.5,'end':2.5,'x':.5,'y':.1,'width':.3,'height':.3,'fit':'contain','callout_side':None}
    p={'output':{'filename':'a.mp4','width':720,'height':1280,'fps':30},'clips':[clip],'picture_in_picture':[inset],'overlays':[],'music':None,'finishing':{'preset':'none','flashes':[]}}
    _,_,graph=build_render_command(SimpleNamespace(root=tmp_path,work=tmp_path/'work',output=tmp_path/'output'),p)
    assert graph.count('delogo=')==2
    assert 'trim=start=2:end=6,delogo=' in graph
    assert 'trim=start=1:end=3,delogo=' in graph
    assert 'setpts=(PTS-STARTPTS)/1.2' in graph
    assert '[0:a]atrim=start=2:end=6' in graph
