import cv2
import numpy as np
import pytest
from auto_video_lab.watermark_glyphs import general_glyph_mask
from auto_video_lab.watermark_telea import repair_roi
from auto_video_lab.watermark_telea import localization_bounds
from auto_video_lab.watermark_glyphs import color_residual_ratio

@pytest.mark.parametrize('color',[(240,240,240),(5,5,5),(0,0,240),(0,220,0),(240,0,0),(180,30,180)])
@pytest.mark.parametrize('shape',['text','ring','star'])
def test_colors_and_shapes(color,shape):
    frames=[];truth=np.zeros((150,240),np.uint8)
    if shape=='text':cv2.putText(truth,'@sample',(40,80),cv2.FONT_HERSHEY_SIMPLEX,.7,255,2)
    elif shape=='ring':cv2.circle(truth,(100,75),17,255,4)
    else:cv2.fillPoly(truth,[np.array([[100,49],[107,65],[125,67],[111,79],[115,98],[100,88],[84,98],[89,79],[75,67],[93,65]])],255)
    backgrounds=[]
    for i in range(9):
        # Drifting smooth background, known clean truth. No real-video claim.
        clean=np.full((150,240,3),110+i*4,np.uint8)
        marked=clean.copy();marked[truth>0]=color
        frames.append(marked);backgrounds.append(clean)
    mask,reason=general_glyph_mask(frames)
    assert mask is not None,reason
    assert np.mean(mask[truth>0]>0)>.98
    fixed=repair_roi(frames[0],mask)
    assert np.abs(fixed.astype(float)-backgrounds[0])[truth>0].mean()<8
    assert np.array_equal(fixed[mask==0],frames[0][mask==0])

def test_translucent_gray_and_no_watermark():
    frames=[];truth=np.zeros((150,240),np.uint8)
    cv2.putText(truth,'SAMPLE',(50,80),cv2.FONT_HERSHEY_SIMPLEX,.7,255,2)
    for i in range(9):
        f=np.full((150,240,3),70+i*4,np.uint8)
        f[truth>0]=f[truth>0]*.55+240*.45;frames.append(f)
    mask,_=general_glyph_mask(frames)
    assert mask is not None and np.mean(mask[truth>0]>0)>.98
    assert general_glyph_mask([np.full((150,240,3),128,np.uint8)]*9)[0] is None

def test_continuous_motion_is_not_a_union_smear():
    frames=[]
    for i in range(9):
        f=np.full((150,240,3),110,np.uint8)
        cv2.circle(f,(20+24*i,70),8,(0,0,0),-1);frames.append(f)
    assert general_glyph_mask(frames)[0] is None

@pytest.mark.parametrize('x',[0,290,600])
@pytest.mark.parametrize('y',[0,610,1220])
def test_nine_positions_including_real_picture_edges(x,y):
    frames=[];truth=np.zeros((1280,720),np.uint8)
    cv2.putText(truth,'@mark',(x,y+25),cv2.FONT_HERSHEY_SIMPLEX,.55,255,2)
    r=dict(x=x/720,y=y/1280,width=112/720,height=30/1280)
    a,b,c,d=localization_bounds(r,(720,1280))
    for i in range(5):
        f=np.full((1280,720,3),140+i*2,np.uint8);f[truth>0]=(5,5,5)
        frames.append(f[b:d,a:c])
    mask,_=general_glyph_mask(frames)
    assert mask is not None
    assert np.mean(mask[truth[b:d,a:c]>0]>0)>.99
    fixed=repair_roi(frames[0],mask)
    assert color_residual_ratio(frames[0],fixed,mask)<.55

def test_equal_luminance_color_edges_are_actually_audited():
    # BGR red≈76 gray, background76: gray-only checks cannot see this well.
    f=np.full((140,220,3),76,np.uint8)
    cv2.putText(f,'@red',(35,75),cv2.FONT_HERSHEY_SIMPLEX,.8,(0,0,254),2)
    mask,_=general_glyph_mask([f]*9)
    assert mask is not None
    assert color_residual_ratio(f,f,mask)==1.
    assert color_residual_ratio(f,repair_roi(f,mask),mask)<.55

@pytest.mark.parametrize('color',[(20,20,235),(10,220,10),(220,30,30)])
def test_colored_word_and_icon_does_not_enclose_background_gaps(color):
    frames=[];truth=np.zeros((127,137),np.uint8)
    cv2.putText(truth,'@ring',(15,54),cv2.FONT_HERSHEY_SIMPLEX,.55,255,2,cv2.LINE_AA)
    cv2.circle(truth,(30,79),13,255,4,cv2.LINE_AA)
    for i in range(9):
        yy,xx=np.mgrid[:127,:137]
        base=165+12*np.sin((xx+i*40)/80)+10*np.cos((yy-i*50)/100)
        f=np.stack([base+5,base,base-5],axis=2).clip(0,255)
        alpha=truth[:,:,None]/255
        f=(f*(1-alpha)+np.array(color)*alpha).astype(np.uint8);frames.append(f)
    raw,reason=general_glyph_mask(frames,dilate=False)
    assert raw is not None,reason
    assert not raw[:,:2].any() and not raw[:,-2:].any()
    assert np.mean(raw[truth>128]>0)>.9
