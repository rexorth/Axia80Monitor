"""RingBuffer and peak_decimate (GUI data path). Skipped when the GUI packages aren't installed."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
try:
    import numpy as np
    from axia80.gui import RingBuffer, peak_decimate
except ImportError as e:
    raise unittest.SkipTest(f"GUI packages not installed ({e.name}); run with .venv/bin/python")


def feed(ring, n, chunk=7, rate=100.0):
    t = np.arange(n) / rate
    y = np.stack([t * (k + 1) for k in range(6)], axis=1)
    for i in range(0, n, chunk):
        ring.extend(t[i:i + chunk], y[i:i + chunk])
    return t, y


class RingBufferTest(unittest.TestCase):
    def check(self, cap, n, seconds, rows=range(6)):
        ring = RingBuffer(cap)
        t, y = feed(ring, n)
        got_t, got_y = ring.last(seconds, rows)
        keep = min(n, cap)
        ref_t, ref_y = t[n - keep:], y[n - keep:]
        sel = ref_t >= ref_t[-1] - seconds
        np.testing.assert_allclose(got_t, ref_t[sel])
        np.testing.assert_allclose(got_y, ref_y[sel][:, list(rows)].T)

    def test_not_wrapped(self):
        self.check(cap=1000, n=250, seconds=1.0)
        self.check(cap=1000, n=250, seconds=100.0)

    def test_wrapped_window_in_newest_piece(self):
        self.check(cap=100, n=390, seconds=0.5)       # end=90: window fits in [0:end)

    def test_wrapped_window_spans_both_pieces(self):
        self.check(cap=100, n=330, seconds=0.8)
        self.check(cap=100, n=330, seconds=10.0)      # whole buffer

    def test_exactly_full_and_subset_rows(self):
        self.check(cap=100, n=300, seconds=0.5, rows=[2, 5])

    def test_empty_and_clear(self):
        ring = RingBuffer(10)
        self.assertEqual(ring.last(1.0)[0].size, 0)
        feed(ring, 5)
        ring.clear()
        self.assertEqual(ring.last(1.0, [0])[1].shape, (1, 0))


class PeakDecimateTest(unittest.TestCase):
    def test_small_input_untouched(self):
        x, y = np.arange(10.0), np.ones((2, 10))
        xo, yo = peak_decimate(x, y, 100)
        self.assertIs(xo, x)

    def test_keeps_spike_and_bounds(self):
        n = 80000
        x = np.linspace(-10, 0, n)
        y = np.zeros((1, n))
        y[0, 12345] = 5.0                  # single-sample spike
        y[0, 50000] = -3.0
        xo, yo = peak_decimate(x, y, 700)
        self.assertEqual(xo.shape, (1400,))
        self.assertEqual(yo.max(), 5.0)
        self.assertEqual(yo.min(), -3.0)
        self.assertEqual(xo[-1], x[-1])    # newest sample still at the right edge
        self.assertTrue(np.all(np.diff(xo) >= 0))


if __name__ == "__main__":
    unittest.main()
