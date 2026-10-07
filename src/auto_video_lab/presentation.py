"""Versioned information graphics. No footage selection, retiming, or factual copy.

Only server-authored v2 presentation opts in. Legacy timelines render unchanged.
Vector plates frame existing evidence-grounded text; licensed PNGs replace the
specific nonverbal emoji events already selected by the semantic planner.
"""
from pathlib import Path
import hashlib
import json
import math
from .text_colors import validate_typography

STICKERS = Path(__file__).resolve().parents[2] / 'assets/edit-stickers/v1'


def sticker_events(plan):
    if plan.get('presentation', {}).get('version') != 2:
        return []
    candidates = [o for o in plan.get('overlays', []) if o.get('preset') == 'fine_reaction' and o.get('text', '').strip() in {'😳','🙈','✨','👇'}]
    if not candidates:
        return []
    manifest = json.loads((STICKERS / 'manifest.json').read_text(encoding='utf8'))
    if not (STICKERS / 'LICENSE.txt').is_file():
        raise ValueError('Licensed sticker notice missing')
    result = []
    for event in candidates:
        entry = next(x for x in manifest['files'] if x['symbol'] == event['text'].strip())
        file = STICKERS / entry['file']
        if file.parent.resolve() != STICKERS.resolve() or hashlib.sha256(file.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Sticker integrity check failed')
        result.append((event, file))
    return result


def position_presented_overlays(overlays, width, height):
    """Move the text and its plate as ONE object, including unsafe old anchors."""
    result=[]
    for raw in overlays:
        o=dict(raw)
        if o.get('_presentation') and o.get('kind') != 'caption' and o.get('preset') != 'fine_reaction':
            size=float(o.get('font_size',74*min(width/1080,height/1920)))
            lines=o.get('_display_lines',[o.get('text','')])
            box_h=(len(lines)*size*1.10+24*min(width/1080,height/1920))/height
            advance=.48 if o.get('kind')=='title' else .57
            box_w=min(.78,(max(map(len,lines))*size*advance+40*min(width/1080,height/1920))/width)
            x,y=float(o.get('x',.5)),float(o.get('y',.3));align=int(o.get('align',5))
            if align in (1,4,7):x+=box_w/2
            if align in (3,6,9):x-=box_w/2
            if align in (1,2,3):y-=box_h/2
            if align in (7,8,9):y+=box_h/2
            o['x']=max(.055+box_w/2,min(.91-box_w/2,x))
            o['y']=max(.065+box_h/2,min(.84-box_h/2,y))
            o['align']=5
        result.append(o)
    return result


def plate_events(overlays, width, height):
    """ASS vector plates stay within the planned text's safe rectangle."""
    result = []
    scale = min(width/1080, height/1920)
    def rgb(c):
        return '&H'+c[5:7]+c[3:5]+c[1:3]+'&'
    for o in overlays:
        style = o.get('_presentation')
        if not style or o.get('kind') == 'caption' or o.get('preset') == 'fine_reaction':
            continue
        text = o.get('text','').strip()
        if not text:
            continue
        size = float(o.get('font_size', 74*scale))
        # Conservative estimation, bounded to the safe display region.
        lines = o.get('_display_lines', text.split('\n'))
        advance = .48 if o.get('kind') == 'title' else .57
        w = min(width*.78, max(100*scale, max(map(len,lines))*size*advance + 40*scale))
        h = max(38*scale, len(lines)*size*1.10 + 24*scale)
        x = float(o.get('x', .5))*width
        y = float(o.get('y', .3))*height
        align = int(o.get('align',5))
        if align in (1,4,7): x += w/2
        if align in (3,6,9): x -= w/2
        if align in (1,2,3): y -= h/2
        if align in (7,8,9): y += h/2
        left=max(width*.055,min(width*.91-w,x-w/2))
        top=max(height*.065,min(height*.84-h,y-h/2))
        right,bottom=left+w,top+h
        accent = rgb(o.get('_accent','#E4CEAD'))
        def shape(coords, color, alpha='00', layer_offset=-1):
            result.append({'start':o['start'],'end':o['end'],'layer':max(0,int(o.get('layer',5))+layer_offset),'drawing':f'{{\\an7\\pos(0,0)\\bord0\\shad0\\1c{color}\\1a&H{alpha}&\\fad(100,120)\\p1}}{coords}{{\\p0}}'})
        if style == 'clear':
            line=bottom+5*scale
            shape(f'm {left:.1f} {line:.1f} l {right:.1f} {line:.1f} l {right:.1f} {line+2*scale:.1f} l {left:.1f} {line+2*scale:.1f}',accent)
        elif style == 'focus':
            shape(f'm {left:.1f} {top:.1f} l {right:.1f} {top:.1f} l {right:.1f} {bottom:.1f} l {left:.1f} {bottom:.1f}', '&H211B17&','25')
            shape(f'm {left:.1f} {top:.1f} l {left+4*scale:.1f} {top:.1f} l {left+4*scale:.1f} {bottom:.1f} l {left:.1f} {bottom:.1f}',accent)
        elif style == 'social':
            shape(f'm {left:.1f} {top:.1f} l {right:.1f} {top:.1f} l {right:.1f} {bottom:.1f} l {left+25*scale:.1f} {bottom:.1f} l {left+10*scale:.1f} {bottom+14*scale:.1f} l {left+12*scale:.1f} {bottom:.1f} l {left:.1f} {bottom:.1f}', '&H302333&','18')
        elif style == 'impact':
            # One underline tab, not an opaque full-frame transition.
            shape(f'm {left:.1f} {bottom-5*scale:.1f} l {right:.1f} {bottom-5*scale:.1f} l {right-9*scale:.1f} {bottom+5*scale:.1f} l {left:.1f} {bottom+5*scale:.1f}',accent)
    return result


def validate_presentation(value):
    if value is None:
        return
    if not isinstance(value,dict) or value.get('version') != 2 or value.get('style') not in {'clear','focus','social','impact'}:
        raise ValueError('Unsupported visual presentation')
    validate_typography(value.get('typography'))
    import re
    if not re.fullmatch(r'#[0-9A-Fa-f]{6}',value.get('accent','')):
        raise ValueError('Invalid presentation accent')
    graphics=value.get('graphics',[])
    if not isinstance(graphics,list) or len(graphics)>6:raise ValueError('Invalid graphics count')
    for g in graphics:
        if g.get('kind') not in {'ring','arrow','bracket'} or g.get('direction') not in {'left','right','up','down'}:raise ValueError('Invalid graphic kind')
        if not all(isinstance(g.get(k),(int,float)) and math.isfinite(g[k]) for k in ['start','end','x','y','width','height']):raise ValueError('Invalid geometry')
        if not (0<=g['start']<g['end'] and .3<=g['end']-g['start']<=3 and .05<=g['x'] and .08<=g['y'] and .04<=g['width']<=.35 and .04<=g['height']<=.3 and g['x']+g['width']<=.92 and g['y']+g['height']<=.84):raise ValueError('Graphic outside safe area')


def annotation_events(presentation,width,height):
    """One simple pointer, no generated imagery or object tracking claim."""
    result=[]
    rgb=presentation.get('accent','#FFD84D');color='&H'+rgb[5:7]+rgb[3:5]+rgb[1:3]+'&'
    for g in presentation.get('graphics',[]):
        x,y,w,h=g['x']*width,g['y']*height,g['width']*width,g['height']*height
        paint=f'\\1a&H00&\\1c{color}\\bord0'
        if g['kind']=='ring':
            pts=[(x+w/2+math.cos(i/40*2*math.pi)*w/2,y+h/2+math.sin(i/40*2*math.pi)*h/2) for i in range(41)]
            coords='m '+' l '.join(f'{a:.1f} {b:.1f}' for a,b in pts)
            paint=f'\\1a&HFF&\\3c{color}\\bord{max(1.5,width*.004):.2f}'
        elif g['kind']=='bracket':
            t=max(2,width*.006)
            left=[(x+w*.2,y),(x,y),(x,y+h),(x+w*.2,y+h),(x+w*.2,y+h-t),(x+t,y+h-t),(x+t,y+t),(x+w*.2,y+t)]
            right=[(2*x+w-a,b) for a,b in left]
            coords=' '.join('m '+' l '.join(f'{a:.1f} {b:.1f}' for a,b in points) for points in [left,right])
        else:
            normal=[(0,.45),(.62,.45),(.62,.2),(1,.5),(.62,.8),(.62,.55),(0,.55)]
            if g['direction']=='left':normal=[(1-a,b) for a,b in normal]
            elif g['direction']=='down':normal=[(b,a) for a,b in normal]
            elif g['direction']=='up':normal=[(b,1-a) for a,b in normal]
            points=[(x+a*w,y+b*h) for a,b in normal]
            coords='m '+' l '.join(f'{a:.1f} {b:.1f}' for a,b in points)
        result.append({'start':g['start'],'end':g['end'],'layer':30,'drawing':f'{{\\an7\\pos(0,0){paint}\\shad0\\fad(90,130)\\p1}}{coords}{{\\p0}}'})
    return result
