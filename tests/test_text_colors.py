import re, unittest
import tempfile
from pathlib import Path
from auto_video_lab.text_colors import color_text,validate_typography
from auto_video_lab.render import write_ass

class ColorTests(unittest.TestCase):
 def test_plain_legacy(self):
  self.assertEqual(color_text('Hello',{},None),'Hello')
 def test_preserve_words_and_accents(self):
  text='¡Qué señal!\\NColor útil'
  for c in ['candy','ice','aurora','sunset']:
   painted=color_text(text,{'start':0,'end':4,'animation':'fade'},{'version':1,'color':c})
   self.assertEqual(re.sub(r'\{[^}]*\}','',painted),text)
   self.assertEqual('\\t(' in painted,c in ['aurora','sunset'])
 def test_no_competing_motion(self):
  base={'start':0,'end':4,'animation':'punch'}
  self.assertNotIn('\\t(',color_text('Hello',base,{'color':'aurora'}))
  self.assertNotIn('\\t(',color_text('Hello',{'start':0,'end':4,'animation':'fade','_color_static':True},{'color':'aurora'}))
  base['highlights']=[{'text':'Hello','motion':'pulse'}]
  self.assertEqual(color_text('Hello',base,{'color':'aurora'}),'Hello')
 def test_validation(self):
  with self.assertRaises(ValueError):validate_typography({'version':7,'color':'aurora'})
  with self.assertRaises(ValueError):validate_typography({'version':1,'color':'x'})
 def test_resource_bound(self):
  painted=color_text('a'*500,{'start':0,'end':60},{'color':'aurora'})
  self.assertNotIn('\\t(',painted)
 def test_transparent_badge_keeps_readable_ink(self):
  with tempfile.TemporaryDirectory() as directory:
   file=Path(directory)/'check.ass'
   write_ass([{'kind':'label','preset':'badge','_presentation':'impact','_accent':'#FFD84D','text':'Readable ink','start':0,'end':2,'color':'#101010','font_size':40,'x':.5,'y':.5}],file,960,540)
   line=next(x for x in file.read_text(encoding='utf8').splitlines() if x.startswith('Dialogue:') and 'Readable ink' in x)
   self.assertIn(r'\1c&HFFFFFF&',line)

if __name__=='__main__':unittest.main()
