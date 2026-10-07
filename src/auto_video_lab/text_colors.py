"""Opt-in glyph/word colors using existing libass, not mask or 3D text.

No extra images, processes, AI, fonts or per-frame raster buffers. Versioned
and renderer-owned; the planner cannot supply arbitrary ASS tags or palettes.
"""
import math
import re
import unicodedata

PALETTES = {
    'candy': ('#FF99C8', '#FFE680', '#84EDD2', '#8FCFFF'),
    'ice': ('#70D6FF', '#EEF8FF'),
    'aurora': ('#79F2DF', '#CAA6FF'),
    'sunset': ('#FFE48A', '#FF9AB5'),
}


def validate_typography(value):
    if value is None:
        return
    if not isinstance(value, dict) or value.get('version') != 1 or value.get('color') not in PALETTES:
        raise ValueError('Unsupported text color treatment')


def _ass(rgb):
    return '&H' + rgb[5:7] + rgb[3:5] + rgb[1:3] + '&'


def _mix(first, second, amount):
    return '#' + ''.join(f'{round(int(first[i:i+2],16)*(1-amount)+int(second[i:i+2],16)*amount):02X}' for i in (1,3,5))


def color_text(text, overlay, typography):
    """Input text is ALREADY escaped/wrapped. Never split ASS newline escapes,
    combining accents, or existing semantic highlight/motion tags.
    """
    if not typography or overlay.get('preset') == 'fine_reaction' or overlay.get('highlights'):
        return text
    palette = PALETTES[typography['color']]
    # Preexisting tags are not character data. Leave unsupported markup alone.
    if '{' in text or '}' in text:
        return text
    # Color is packaging, not a new semantic highlight; never invent emphasis.
    if typography['color'] == 'candy':
        word = 0
        result = []
        for part in re.split(r'(\\[Nnh]|\s+)', text):
            if not part or part.isspace() or part in (r'\N',r'\n',r'\h'):
                result.append(part)
            else:
                result.append('{\\1c'+_ass(palette[word % len(palette)])+'}'+part)
                word += 1
        return ''.join(result)
    units = []
    for token in re.findall(r'\\[Nnh]|.', text, re.DOTALL):
        if unicodedata.combining(token[0]) and units:
            units[-1] += token
        else:
            units.append(token)
    count = sum(not u.isspace() and not u.startswith('\\') for u in units)
    if count > 120:  # defensive complexity bound; no lost copy
        return '{\\1c'+_ass(palette[0])+'}'+text
    duration = max(1, round((overlay['end']-overlay['start'])*1000))
    animate = typography['color'] in {'aurora','sunset'} and overlay.get('animation','none') in {'none','fade'} and duration >= 850 and not overlay.get('_color_static')
    # One slow sweep over 2.4s, then hold. No flashing, looping or fast color cuts.
    sweep = min(2400, duration)
    result = []; index = 0
    for unit in units:
        if unit.isspace() or unit.startswith('\\'):
            result.append(unit); continue
        phase = index / max(1,count-1)
        amount = (1-math.cos(phase*math.pi))/2 if animate else phase
        tags = '\\1c'+_ass(_mix(*palette,amount))
        if animate:
            for step in range(1,7):
                at = round(sweep*step/6)
                amount = (1-math.cos((phase-step/6)*math.pi))/2
                tags += f'\\t({round(sweep*(step-1)/6)},{at},\\1c{_ass(_mix(*palette,amount))})'
        result.append('{'+tags+'}'+unit); index += 1
    return ''.join(result)
