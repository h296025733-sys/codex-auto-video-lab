import numpy as np
import cv2
import pytest
from auto_video_lab.watermark_fidelity import native_420_supported, color_args, repair_planes, psnr


def test_native_path_never_silently_converts_hdr_or_rotation():
    assert native_420_supported({'pix_fmt':'yuv420p','width':720,'height':1280})
    assert not native_420_supported({'pix_fmt':'yuv420p10le','width':720,'height':1280})
    assert not native_420_supported({'pix_fmt':'yuv420p','width':720,'height':1280,'side_data_list':[{'rotation':180}]})
    assert color_args({'color_range':'tv','color_space':'unknown'}) == ['-color_range','tv']


@pytest.mark.parametrize('x,y,right,bottom', [(0,0,31,39),(17,23,51,53),(95,83,128,96)])
def test_yuv_outside_true_mask_is_exactly_unchanged(x,y,right,bottom):
    rng = np.random.default_rng(11)
    original = [rng.integers(16,220,(96,128),dtype=np.uint8), rng.integers(50,200,(48,64),dtype=np.uint8), rng.integers(50,200,(48,64),dtype=np.uint8)]
    mask = np.zeros((bottom-y,right-x),np.uint8)
    cv2.circle(mask,(mask.shape[1]//2,mask.shape[0]//2),7,255,-1)
    item = {'region':{'start':1,'end':2},'bounds':(x,y,right,bottom),'mask':mask}
    full = np.zeros((96,128),np.uint8);full[y:bottom,x:right]=mask
    chroma = full.reshape(48,2,64,2).max(axis=(1,3))
    changed = repair_planes([p.copy() for p in original],[item],1.5)
    for a,b,m in zip(original,changed,[full,chroma,chroma]):
        assert np.array_equal(a[m==0],b[m==0])
        assert not np.array_equal(a[m>0],b[m>0])
    inactive = repair_planes([p.copy() for p in original],[item],2.)
    assert all(np.array_equal(a,b) for a,b in zip(original,inactive))


def test_psnr_is_measurement_not_file_size():
    assert psnr(0) == 100
    assert psnr(1) > 48
    assert psnr(4) < 44


def test_neutral_chroma_repair_does_not_create_colored_gradient_overshoot():
    planes=[np.full((96,128),100,np.uint8),np.full((48,64),128,np.uint8),np.full((48,64),128,np.uint8)]
    mask=np.zeros((40,40),np.uint8);cv2.circle(mask,(20,20),9,255,-1)
    full=np.zeros((96,128),np.uint8);full[20:60,40:80]=mask
    chroma=full.reshape(48,2,64,2).max(axis=(1,3))
    planes[0][full>0]=235;planes[1][chroma>0]=40;planes[2][chroma>0]=210
    item={'region':{'start':0,'end':1},'bounds':(40,20,80,60),'mask':mask}
    result=repair_planes(planes,[item],0)
    for plane in result[1:]:
        assert np.max(np.abs(plane.astype(int)-128))<=1


@pytest.mark.parametrize('watermark_only,version,expected', [(True,3,'10'),(False,3,'20'),(True,2,'20')])
def test_fidelity_encoder_policy_does_not_change_legacy_or_edit_exports(tmp_path,watermark_only,version,expected):
    from types import SimpleNamespace
    from auto_video_lab.render import build_render_command
    (tmp_path/'work').mkdir(); (tmp_path/'output').mkdir()
    job=SimpleNamespace(root=tmp_path,work=tmp_path/'work',output=tmp_path/'output')
    plan={'watermark_only':watermark_only,'watermark_cleanup':{'version':version,'inspected':True,'regions':[]},
          'output':{'filename':'test.mp4','width':720,'height':1280,'fps':24,'crf':20},
          'clips':[{'source':'input.mp4','kind':'video','start':0,'end':2,'speed':1,'fit':'contain','has_audio':True,'mute':False,'audio_gain_db':0}],
          'overlays':[],'music':None,'finishing':{'preset':'none','flashes':[]}}
    argv,_,_=build_render_command(job,plan)
    assert argv[argv.index('-crf')+1] == expected
    assert argv[argv.index('-pix_fmt')+1] == 'yuv420p'


@pytest.mark.parametrize('second_status', ['passed','below_threshold'])
def test_delivery_quality_retry_is_bounded_and_cannot_publish_bad_fidelity(tmp_path,monkeypatch,second_status):
    from types import SimpleNamespace
    import auto_video_lab.render as renderer
    import auto_video_lab.watermark_telea as telea
    import auto_video_lab.watermark_fidelity as fidelity
    reports=tmp_path/'reports';reports.mkdir()
    selected=tmp_path/'plan.json';selected.write_text('{}')
    job=SimpleNamespace(root=tmp_path,reports=reports,plan=selected)
    plan={'watermark_only':True,'watermark_cleanup':{'version':3}}
    monkeypatch.setattr(renderer.JobPaths,'for_id',lambda _:job)
    monkeypatch.setattr(renderer,'read_json',lambda p:plan if p==selected else {'regions':[]})
    monkeypatch.setattr(renderer,'validate_plan',lambda *a:{'plan':plan,'duration_seconds':2})
    monkeypatch.setattr(telea,'prepare_sources',lambda *a:{})
    monkeypatch.setattr(telea,'preserve_original_audio',lambda *a:None)
    monkeypatch.setattr(telea,'verify_final_watermarks',lambda *a:None)
    monkeypatch.setattr(renderer,'_normalize_output_audio',lambda *a:{})
    monkeypatch.setattr(renderer,'build_render_command',lambda *a:(['ffmpeg','-crf','10','out.mp4'],tmp_path/'out.mp4','null'))
    calls=[]
    monkeypatch.setattr(renderer,'run_command',lambda argv,**kw:(calls.append(list(argv)) or SimpleNamespace(stderr='')))
    results=iter([{'status':'below_threshold'},{'status':second_status}])
    monkeypatch.setattr(fidelity,'audit_delivery',lambda *a:next(results))
    if second_status=='passed':
        assert renderer.render_job('test')['picture_fidelity']['bounded_reencode'] is True
    else:
        with pytest.raises(RuntimeError,match='fidelity below threshold'):
            renderer.render_job('test')
        assert not (reports/'render.json').exists()
    assert [a[2] for a in calls] == ['10','4']
