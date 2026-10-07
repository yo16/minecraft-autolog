import unittest
from datetime import datetime

import numpy

from autolog.capture import frame_diff, frame_file_name, thumbnail


class FrameDiffTest(unittest.TestCase):
    def test_identical_is_zero(self):
        img = numpy.full((480, 854, 3), 120, dtype=numpy.uint8)
        self.assertEqual(frame_diff(thumbnail(img), thumbnail(img)), 0.0)

    def test_black_to_white_is_one(self):
        black = numpy.zeros((480, 854, 3), dtype=numpy.uint8)
        white = numpy.full((480, 854, 3), 255, dtype=numpy.uint8)
        self.assertAlmostEqual(frame_diff(thumbnail(black), thumbnail(white)), 1.0)

    def test_first_frame(self):
        img = numpy.zeros((480, 854, 3), dtype=numpy.uint8)
        self.assertIsNone(frame_diff(None, thumbnail(img)))

    def test_file_name(self):
        self.assertEqual(frame_file_name(datetime(2026, 9, 27, 21, 3, 19), "webp"), "2026-09-27_21.03.19.webp")


if __name__ == "__main__":
    unittest.main()
