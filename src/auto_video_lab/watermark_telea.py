"""V3: semantic fixed regions -> temporal glyph mask -> local Telea -> audit.

No network/GPU, no global blur/crop, no writes to inputs. Legacy V1/V2 untouched.
Raster checks measure residual high-frequency lettering, not semantic certainty.
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np
import av

from .media import ffprobe, first_stream, duration_seconds
from .paths import ffmpeg_path
from .util import safe_resolve_within, write_json
from .watermarks import approved_regions, display_dimensions
from .watermark_glyphs import general_glyph_mask, color_residual_ratio
from .watermark_timing import refine_interval
from .watermark_fidelity import native_420_supported, decoded_planes, repair_planes, color_args


def sample_frame(source: Path, time: float) -> np.ndarray | None:
    # OpenCV random seek misaddresses this HEVC/MP4 sample (7.5 s -> 11.9 s).
    # Use FFmpeg's timestamp-aware accurate seek, not CAP_PROP_POS_MSEC.
    # Nested Windows Node -> Python -> FFmpeg execution must not pass large PNG
    # payloads through inherited console pipes. A unique temporary file keeps
    # sampling cancellable and avoids waiting for an inherited pipe to close.
    with tempfile.TemporaryDirectory(prefix='jingxu-wm-frame-') as tmp:
        target=Path(tmp)/'frame.png'
        for attempt in range(2):
            # Second attempt decodes forward rather than trusting the container index.
            seeking=['-ss',f'{time:.6f}','-i',str(source)] if attempt==0 else ['-i',str(source),'-ss',f'{time:.6f}']
            try:
                p=subprocess.run([str(ffmpeg_path()),'-nostdin','-y','-v','error','-threads','2',*seeking,
                                  '-map','0:v:0','-frames:v','1','-vcodec','png','-threads','1',str(target)],
                                 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
                if p.returncode==0 and target.is_file():
                    frame=cv2.imdecode(np.fromfile(str(target),np.uint8),cv2.IMREAD_COLOR)
                    if frame is not None:
                        return frame
            except subprocess.TimeoutExpired:
                pass
        return None


def repair_roi(frame: np.ndarray, mask: np.ndarray) -> np.ndarray:
    # Reflect context outside the picture too; never feed a full-image edge to Telea.
    padded = cv2.copyMakeBorder(frame, 16, 16, 16, 16, cv2.BORDER_REFLECT_101)
    padded_mask = cv2.copyMakeBorder(mask, 16, 16, 16, 16, cv2.BORDER_CONSTANT)
    fixed = cv2.inpaint(padded, padded_mask, 4, cv2.INPAINT_TELEA)
    return fixed[16:-16, 16:-16]


def residual_ratio(before: np.ndarray, after: np.ndarray, mask: np.ndarray) -> float:
    return color_residual_ratio(before, after, mask)


def stable_glyph_mask(frames: list[np.ndarray], *, dilate: bool = True) -> tuple[np.ndarray | None,str]:
    return general_glyph_mask(frames, dilate=dilate)


def localization_bounds(r: dict, size: tuple[int, int]) -> tuple[int, int, int, int]:
    """A bounded search margin corrects approximate model coordinates, not a wipe box.

    A 720x1280 model box could end above the complete lettering. The old 8px
    margin erased its top edge and then audited only those erased pixels.
    Search up to 3% nearby; only stable glyph contours may be modified.
    """
    width, height = size
    pad_x, pad_y = round(width*.02), round(height*.03)
    return (max(0, math.floor(r['x']*width)-pad_x),
            max(0, math.floor(r['y']*height)-pad_y),
            min(width, math.ceil((r['x']+r['width'])*width)+pad_x),
            min(height, math.ceil((r['y']+r['height'])*height)+pad_y))


def glyph_touches_search_boundary(mask: np.ndarray, bounds: tuple, size: tuple) -> bool:
    """A clipped glyph cannot prove complete removal; the real picture edge is OK."""
    x,y,right,bottom=bounds
    width,height=size
    return bool((x>0 and mask[:,:2].any()) or (y>0 and mask[:2,:].any()) or
                (right<width and mask[:,-2:].any()) or (bottom<height and mask[-2:,:].any()))


def semantic_contours(audit: np.ndarray, region: dict, bounds: tuple, size: tuple) -> np.ndarray:
    """Keep entire components seeded by the approved semantic areas, not their L-shaped hull.

    Extra localization context can include unrelated shirt seams or neighboring
    lettering. It is evidence, not permission to erase that unrelated component.
    A retained component is NEVER cut at the semantic boundary; the separate
    search-boundary guard still rejects incomplete authorized lettering.
    """
    x,y,_,_=bounds
    width,height=size
    seed=np.zeros_like(audit)
    for member in region.get('semantic_members',[region]):
        left=max(0,math.floor(member['x']*width)-x-round(width*.005))
        top=max(0,math.floor(member['y']*height)-y-round(height*.01))
        right=min(audit.shape[1],math.ceil((member['x']+member['width'])*width)-x+round(width*.005))
        bottom=min(audit.shape[0],math.ceil((member['y']+member['height'])*height)-y+round(height*.01))
        seed[top:bottom,left:right]=255
    count,labels,stats,_=cv2.connectedComponentsWithStats((audit>0).astype(np.uint8),8)
    keep=np.zeros_like(audit)
    selected=set()
    for i in range(1,count):
        component=labels==i
        if np.any(component&(seed>0)):
            selected.add(i)
    # An approximate box can touch capitals but miss the lower-case letters
    # on the SAME word baseline. Restore adjacent complete components, not a
    # rectangular row or a different line of neighboring attribution.
    changed=True
    while changed:
        changed=False
        for i in range(1,count):
            if i in selected:
                continue
            a,b,w,h,_=stats[i]
            for j in tuple(selected):
                c,d,v,k,_=stats[j]
                overlap=min(b+h,d+k)-max(b,d)
                gap=max(0,max(a,c)-min(a+w,c+v))
                aligned=overlap>=.5*min(h,k) and abs((b+h/2)-(d+k/2))<=.5*max(h,k)
                if aligned and gap<=max(3,min(12,max(h,k)*.6)):
                    selected.add(i);changed=True;break
    for i in selected:
        keep[labels==i]=255
    return keep


def fill_small_glyph_holes(mask: np.ndarray, size: tuple) -> np.ndarray:
    """Include enclosed icon interiors; never bridge open background word gaps."""
    result=mask.copy()
    count,labels,stats,_=cv2.connectedComponentsWithStats((mask==0).astype(np.uint8),8)
    maximum=min(512,round(size[0]*size[1]*.0005))
    height,width=mask.shape
    for i in range(1,count):
        x,y,w,h,area=stats[i]
        if 0<x and 0<y and x+w<width and y+h<height and area<=maximum:
            result[labels==i]=255
    return result


def merge_overlapping_regions(regions: list[dict]) -> list[dict]:
    """Intersecting bounded searches share a mask, not overlapping partial repairs.

    Both regions were semantically approved. Adjacent text/icon searches can
    include each other and be falsely rejected as clipped unless grouped.
    Never merge jumping time ranges. A sparse L-shaped hull may be larger,
    but each member's semantic extent and the actual 2% pixel budget remain.
    """
    result=[]
    for original in regions:
        r=dict(original)
        r['grouped_regions']=1
        r['semantic_members']=[dict(original)]
        changed=True
        while changed:
            changed=False
            for i,other in enumerate(result):
                if other['source']!=r['source'] or abs(other['start']-r['start'])>.001 or abs(other['end']-r['end'])>.001:
                    continue
                x=min(r['x'],other['x']);y=min(r['y'],other['y'])
                right=max(r['x']+r['width'],other['x']+other['width'])
                bottom=max(r['y']+r['height'],other['y']+other['height'])
                overlap=min(r['x']+r['width'],other['x']+other['width'])+.04>max(r['x'],other['x']) and min(r['y']+r['height'],other['y']+other['height'])+.06>max(r['y'],other['y'])
                if overlap and (right-x)*(bottom-y)<=.10:
                    r.update(x=x,y=y,width=right-x,height=bottom-y,
                             grouped_regions=r['grouped_regions']+other['grouped_regions'],
                             semantic_members=r['semantic_members']+other['semantic_members'])
                    result.pop(i);changed=True;break
        result.append(r)
    return result


def prepare_region(source: Path, r: dict, size: tuple[int, int], folder: Path, *, clock: tuple | None = None) -> tuple[dict | None, dict]:
    width, height = size
    x,y,right,bottom = localization_bounds(r,size)
    report = {'source': r['source'], 'start': r['start'], 'end': r['end'],
              'bounds': [x,y,right,bottom], 'method': 'telea_contour', 'processed': False,
              'semantic_regions': r.get('grouped_regions',1), 'localization_version':2,
              'contour_model':'polarity_color_semantic_components_v2',
              'semantic_bounds':[r['x']*width,r['y']*height,(r['x']+r['width'])*width,(r['y']+r['height'])*height]}
    times = np.linspace(r['start'], max(r['start'], r['end']-.12), 9).tolist()
    frames = []
    actual_times = []
    for t in times:
        frame=sample_frame(source,t)
        if frame is not None and frame.shape[:2] == (height,width):
            frames.append(frame[y:bottom,x:right].copy())
            actual_times.append(t)
        else:
            report.update(sample_times=actual_times,reason='timestamp sample could not decode after bounded seek retry')
            return None,report
    report['sample_times'] = actual_times
    if len(frames) != len(times):
        report['reason'] = 'not all first/middle/tail samples decoded'
        return None, report
    partial = clock is not None and (r['start'] > 0 or r['end'] < clock[1]-.12)
    mask, reason = stable_glyph_mask(frames)
    # A model can read a contact-sheet timestamp too early/late. Only for a
    # partial interval, try a contiguous witnessed subset (>=5) to establish
    # the same fixed glyph; do not loosen stable-mask tests on absent frames.
    if mask is None and partial:
        original_frames, original_times = frames, actual_times
        for length in range(len(frames)-1,4,-1):
            for offset in range(len(original_frames)-length+1):
                group=original_frames[offset:offset+length]
                candidate,_=stable_glyph_mask(group)
                if candidate is not None:
                    frames=group;actual_times=original_times[offset:offset+length]
                    mask=candidate;break
            if mask is not None:break
    if mask is None:
        report['reason'] = reason
        return None, report
    # Independent, un-dilated complete semantic glyph evidence drives final QA.
    # Select it from the full search, never from the already-dilated repair mask.
    audit_mask, _ = stable_glyph_mask(frames, dilate=False)
    if audit_mask is not None:
        full_search_pixels=int(np.count_nonzero(audit_mask))
        audit_mask=semantic_contours(audit_mask,r,(x,y,right,bottom),size)
        report['search_contour_pixels']=full_search_pixels
        report['unrelated_context_pixels']=full_search_pixels-int(np.count_nonzero(audit_mask))
        if not audit_mask.any():
            report['reason']='no stable contour intersects the approved semantic area'
            return None,report
        # Merging a long account line with an icon must not widen every letter
        # just because the union rectangle is taller than each original word.
        mask=cv2.dilate(audit_mask,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(7,7)))
        contour_pixels=int(np.count_nonzero(mask))
        mask=fill_small_glyph_holes(mask,size)
        report['enclosed_glyph_pixels']=int(np.count_nonzero(mask))-contour_pixels
    if audit_mask is None or glyph_touches_search_boundary(audit_mask,(x,y,right,bottom),size):
        report['reason'] = 'glyph evidence clipped by search boundary; localization needs review'
        return None, report
    if partial:
        interval,reason=refine_interval(source,r,(x,y,right,bottom),audit_mask,frames,actual_times,
                                        fps=clock[0],duration=clock[1])
        if interval is None:
            report['reason']=reason
            return None,report
        report['timing_proposal']=[r['start'],r['end']]
        r={**r,'start':interval[0],'end':interval[1]}
        report.update(start=r['start'],end=r['end'],timing_method='bounded_cfr_glyph_appearance',timing_evidence=reason,sample_times=actual_times)
    coverage = np.count_nonzero((audit_mask>0)&(mask>0))/max(1,np.count_nonzero(audit_mask))
    report['glyph_coverage'] = coverage
    if coverage < .995:
        report['reason'] = 'repair mask does not cover complete independent glyph evidence'
        return None, report
    # Bound actual changed pixels independently of the larger semantic rectangle.
    for attempt in range(2):
        if np.count_nonzero(mask) > width*height*.02 or np.count_nonzero(mask)/mask.size > .65:
            report['reason'] = 'actual glyph mask exceeds pixel budget'
            return None, report
        ratios = [residual_ratio(frame,repair_roi(frame,mask),audit_mask) for frame in frames]
        if max(ratios) <= .55:
            break
        if attempt == 0:
            mask = cv2.dilate(mask,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5)))
    else:
        report['reason'] = 'sampled lettering residual remains after bounded mask refinement'
        report['residual_ratios'] = ratios
        return None, report
    key = hashlib.sha256(json.dumps(r,sort_keys=True).encode()).hexdigest()[:16]
    target = folder/f'telea-{key}.png'
    cv2.imencode('.png',mask)[1].tofile(str(target))
    audit_target = folder/f'telea-{key}-audit.png'
    cv2.imencode('.png',audit_mask)[1].tofile(str(audit_target))
    report.update(mask=target.name,mask_pixels=int(np.count_nonzero(mask)),
                  audit_mask=audit_target.name,audit_pixels=int(np.count_nonzero(audit_mask)),
                  residual_ratios=ratios,refinement_attempts=attempt,
                  reason=f'{len(frames)} witnessed local repairs passed bounded raster check')
    return {'region':r,'bounds':(x,y,right,bottom),'mask':mask,'audit_mask':audit_mask,'frames':frames,'report':report},report


def prepare_sources(job, plan: dict) -> dict[str, Path]:
    cleanup = plan.get('watermark_cleanup',{})
    if cleanup.get('version') != 3:
        return {}
    cv2.setNumThreads(1)
    folder = job.root/'reports'/'watermark-masks'
    folder.mkdir(parents=True,exist_ok=True)
    regions = merge_overlapping_regions(approved_regions(cleanup))
    summary = {'version':3,'inspected':cleanup.get('inspected') is True,'processed':0,
               'deferred':int(cleanup.get('deferred',0)), 'regions':[],
               'scope':'per-frame local Telea; nine-frame raster residual checks, not semantic proof'}
    replacements = {}
    for source_name in dict.fromkeys(r['source'] for r in regions):
        source = safe_resolve_within(job.root/source_name,job.root)
        probe = ffprobe(source)
        stream = first_stream(probe,'video') or {}
        width,height = display_dimensions(probe)
        source_regions = [r for r in regions if r['source']==source_name]
        fps = float(Fraction(stream.get('avg_frame_rate') or '0/1'))
        nominal = float(Fraction(stream.get('r_frame_rate') or '0/1'))
        # OpenCV cannot preserve arbitrary VFR timestamps. Do not silently retime.
        if fps <= 0 or abs(fps-nominal) > .001 or source.stat().st_size > 1024**3:
            summary['deferred'] += len(source_regions)
            summary['regions'].append({'source':source_name,'processed':False,'reason':'unsupported variable frame clock or source size'})
            continue
        prepared = []
        for r in source_regions:
            item,report = prepare_region(source,r,(width,height),folder,clock=(fps,duration_seconds(probe)))
            summary['regions'].append(report)
            if item is not None:
                prepared.append(item)
            else:
                summary['deferred'] += 1
        if not prepared:
            continue
        token = hashlib.sha256(json.dumps([source_name,source.stat().st_mtime_ns,source_regions],sort_keys=True).encode()).hexdigest()[:20]
        cache_dir = job.root/'cache'/'watermark-v3'
        cache_dir.mkdir(parents=True,exist_ok=True)
        output = cache_dir/f'{token}.mkv'
        native_planes = native_420_supported(stream)
        cap = cv2.VideoCapture(str(source))
        expected = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        decoder = av.open(str(source)) if native_planes else None
        if decoder:
            decoder.streams.video[0].thread_count = 2
            decoded = iter(decoder.decode(video=0))
        count = 0
        # Lossless intermediate. Copying AAC into Matroska shifts the video by
        # its negative decoder-priming timestamp (46ms in incident465), causing
        # the later trim to drop a frame. Float PCM removes that container delay;
        # watermark-only delivery still remuxes the ORIGINAL AAC packets below.
        argv = [str(ffmpeg_path()),'-v','error','-y','-f','rawvideo','-pix_fmt','yuv420p' if native_planes else 'bgr24',
                '-s:v',f'{width}x{height}','-r',str(fps),'-i','-','-i',str(source),
                '-map','0:v:0','-map','1:a?','-c:v','ffv1','-level','3','-threads','2',
                *(color_args(stream) if native_planes else []),'-c:a','pcm_f32le',str(output)]
        with (folder/f'encode-{token}.log').open('wb') as log:
            process = subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=log)
            try:
                while True:
                    t = count/fps
                    if native_planes:
                        native = next(decoded, None)
                        if native is None:
                            break
                        if (native.width,native.height) != (width,height):
                            raise ValueError('Decoded plane dimensions changed')
                        planes = repair_planes(decoded_planes(native), prepared, t)
                        for plane in planes:
                            process.stdin.write(plane.tobytes())
                    else:
                        ok,frame = cap.read()
                        if not ok:
                            break
                        if frame.shape[:2] != (height,width):
                            raise ValueError('Decoded orientation does not match evidence')
                        for item in prepared:
                            r = item['region']
                            if r['start'] <= t < r['end']:
                                x,y,right,bottom = item['bounds']
                                frame[y:bottom,x:right] = repair_roi(frame[y:bottom,x:right],item['mask'])
                        process.stdin.write(frame.tobytes())
                    count += 1
                process.stdin.close()
                if process.wait(timeout=120) != 0 or count != expected or count < 1:
                    raise RuntimeError('Watermark repair frame count/encoder mismatch')
            finally:
                cap.release()
                if decoder:
                    decoder.close()
                if process.poll() is None:
                    process.kill();process.wait()
        # Inspect the actual encoded intermediate too, not only a simulated repair.
        for item in prepared:
            report = item['report']
            report['processed'] = True
            report['pixel_pipeline'] = 'native_yuv420_local_planes' if native_planes else 'legacy_bgr_conversion'
            if native_planes:
                report['chroma_repair']='navier_stokes_local_mask'
            report['frames_preserved'] = count
            x,y,right,bottom = item['bounds']
            for i,t in enumerate(report['sample_times']):
                frame = sample_frame(output,t)
                if frame is None:
                    raise RuntimeError('Watermark intermediate sample could not decode')
                ratio=residual_ratio(item['frames'][i],frame[y:bottom,x:right],item['audit_mask'])
                report.setdefault('encoded_residual_ratios',[]).append(ratio)
                if ratio>.55:
                    report['processed']=False
                cv2.imencode('.jpg',frame[y:bottom,x:right])[1].tofile(str(folder/f'{token}-sample-{i}.jpg'))
            if report['processed']:
                summary['processed'] += 1
            else:
                summary['deferred'] += 1
                report['reason']='encoded result needs review; not labelled complete'
        replacements[source_name] = output
    write_json(folder/'execution.json',summary)
    return replacements


def preserve_original_audio(job, plan: dict, output: Path) -> None:
    if plan.get('watermark_only') is not True:
        return
    clips=plan.get('clips',[])
    if len(clips)!=1 or clips[0].get('start')!=0 or clips[0].get('speed')!=1:
        raise ValueError('Watermark-only audio preservation requires complete original timeline')
    source=safe_resolve_within(job.root/clips[0]['source'],job.root)
    target=output.with_name(output.stem+'.original-audio.mp4')
    probe=ffprobe(source)
    if not any(s.get('codec_type')=='audio' for s in probe.get('streams',[])):
        # The shared renderer supplies a silent compatibility track for mute
        # sources. Keep it: there are no original audio packets to restore.
        return
    compatible=all(s.get('codec_name') in {'aac','mp3','ac3','eac3','alac'} for s in probe.get('streams',[]) if s.get('codec_type')=='audio')
    subprocess.run([str(ffmpeg_path()),'-v','error','-y','-i',str(output),'-i',str(source),
                    '-map','0:v:0','-map','1:a?','-c:v','copy','-c:a','copy' if compatible else 'aac',
                    *([] if compatible else ['-b:a','192k']),'-movflags','+faststart',str(target)],
                   check=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
    target.replace(output)


def verify_final_watermarks(job,plan:dict,output:Path) -> None:
    if plan.get('watermark_only') is not True or plan.get('watermark_cleanup',{}).get('version')!=3:
        return
    folder=job.root/'reports'/'watermark-masks'
    summary=json.loads((folder/'execution.json').read_text(encoding='utf8'))
    for report in summary['regions']:
        if not report.get('processed'):
            continue
        source=safe_resolve_within(job.root/report['source'],job.root)
        mask=cv2.imdecode(np.fromfile(str(folder/report.get('audit_mask',report['mask'])),np.uint8),cv2.IMREAD_GRAYSCALE)
        ratios=[]
        x,y,right,bottom=report['bounds']
        outside=None
        for t in report['sample_times']:
            frame0=sample_frame(source,t);frame1=sample_frame(output,t)
            if frame0 is None or frame1 is None or frame0.shape!=frame1.shape:
                ratios.append(1.)
            else:
                ratios.append(residual_ratio(frame0[y:bottom,x:right],frame1[y:bottom,x:right],mask))
                if outside is None:
                    outside=np.ones(frame0.shape[:2],dtype=bool)
                    for other in summary['regions']:
                        if other.get('source')==report['source'] and 'bounds' in other:
                            a,b,c,d=other['bounds'];outside[b:d,a:c]=False
                delta=float(np.abs(frame0.astype(np.int16)-frame1.astype(np.int16))[outside].mean())
                report.setdefault('outside_region_mean_absolute_error',[]).append(delta)
                if delta>15:
                    ratios[-1]=1.
        report['final_residual_ratios']=ratios
        if max(ratios,default=1.)>.55:
            report['processed']=False
            report['reason']='final MP4 raster check requires review'
            summary['processed']-=1;summary['deferred']+=1
    summary['final_mp4_checked']=True
    # A sparse check can miss a returning watermark. CFR watermark-only clips
    # retain1:1 frames, so audit the complete independent glyph on EVERY frame.
    # This measures raster residue, not semantic recognition or texture quality.
    for source_name in dict.fromkeys(r['source'] for r in summary['regions'] if r.get('processed')):
        source=safe_resolve_within(job.root/source_name,job.root)
        before=cv2.VideoCapture(str(source),cv2.CAP_FFMPEG,[cv2.CAP_PROP_N_THREADS,2])
        after=cv2.VideoCapture(str(output),cv2.CAP_FFMPEG,[cv2.CAP_PROP_N_THREADS,2])
        fps=before.get(cv2.CAP_PROP_FPS)
        checks=[]
        for report in summary['regions']:
            if report.get('processed') and report['source']==source_name:
                mask=cv2.imdecode(np.fromfile(str(folder/report.get('audit_mask',report['mask'])),np.uint8),cv2.IMREAD_GRAYSCALE)
                checks.append((report,mask))
                report.update(all_frame_checks=0,all_frame_max_residual=0.,all_frame_residual_failures=0)
        count=0
        try:
            while True:
                ok0,frame0=before.read();ok1,frame1=after.read()
                if ok0!=ok1:
                    raise RuntimeError('Watermark-only final video does not preserve all source frames')
                if not ok0:
                    break
                if frame0.shape!=frame1.shape:
                    raise RuntimeError('Watermark-only final video dimensions changed')
                for report,mask in checks:
                    if report['start'] <= count/fps < report['end']:
                        x,y,right,bottom=report['bounds']
                        ratio=residual_ratio(frame0[y:bottom,x:right],frame1[y:bottom,x:right],mask)
                        report['all_frame_checks']+=1
                        report['all_frame_max_residual']=max(report['all_frame_max_residual'],ratio)
                        report['all_frame_residual_failures']+=int(ratio>.55)
                count+=1
        finally:
            before.release();after.release()
        if count<1:
            raise RuntimeError('No frames decoded during final watermark verification')
        for report,_ in checks:
            report['final_frames_preserved']=count
            if not report['all_frame_checks'] or report['all_frame_residual_failures']:
                report['processed']=False
                report['reason']='complete-frame glyph audit requires review'
                summary['processed']-=1;summary['deferred']+=1
    summary['scope']='per-frame local Telea; independent full-glyph final raster audit on every frame, not semantic proof'
    write_json(folder/'execution.json',summary)
