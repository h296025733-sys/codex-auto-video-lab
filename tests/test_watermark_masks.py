import unittest
import cv2
import numpy as np
from auto_video_lab.watermark_masks import glyph_mask


class GlyphMaskTests(unittest.TestCase):
    def sample(self, offset=0, background=25):
        im = np.full((120, 230, 3), background, np.uint8)
        cv2.putText(im, 'TikTok', (15+offset, 75), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255,255,255), 2)
        return im

    def test_stable_glyphs_not_whole_rectangle(self):
        mask, _ = glyph_mask([self.sample(background=b) for b in [20,30,40,50,60]])
        self.assertIsNotNone(mask)
        self.assertLess(np.count_nonzero(mask), mask.size*.5)
        self.assertEqual(int(mask[0,0]), 0)

    def test_blank_bright_background_rejected(self):
        self.assertIsNone(glyph_mask([np.full((100,200,3),255,np.uint8)]*5)[0])

    def test_missing_frames_rejected(self):
        self.assertIsNone(glyph_mask([self.sample()])[0])

    def test_moving_glyphs_do_not_become_a_large_union(self):
        self.assertIsNone(glyph_mask([self.sample(offset=i*15) for i in range(5)])[0])


if __name__ == '__main__':
    unittest.main()
