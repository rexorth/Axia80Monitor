"""RingBuffer and peak_decimate (GUI data path). Skipped when the GUI packages aren't installed."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
try:
    import numpy as np
    from axia80.gui import RingBuffer, TestHistory, next_y_range, peak_decimate, y_limits
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
        self.assertEqual(xo.shape, (80000 - 700 * 114 + 1400,))   # 200 raw leftovers + 700 min/max pairs
        self.assertEqual(yo.max(), 5.0)
        self.assertEqual(yo.min(), -3.0)
        self.assertEqual(xo[-1], x[-1])    # newest sample still at the right edge
        self.assertTrue(np.all(np.diff(xo) >= 0))

    def test_oldest_samples_are_not_dropped(self):
        # few samples per bucket leaves a large remainder; it must still be drawn
        n = 2000
        x = np.arange(n) / 1000.0
        y = np.arange(n, dtype=float)[None, :]
        xo, yo = peak_decimate(x, y, 700)
        self.assertEqual(xo[0], 0.0)
        self.assertEqual(yo[0, 0], 0.0)
        self.assertEqual(xo[-1], x[-1])
        self.assertLessEqual(len(xo), n)


class YLimitsTest(unittest.TestCase):
    def test_min_span_applies_to_quiet_signal(self):
        lo, hi = y_limits(1.000, 1.002, min_span=0.05, pad=0)
        self.assertAlmostEqual(hi - lo, 0.05)
        self.assertAlmostEqual((hi + lo) / 2, 1.001)        # centred on the data

    def test_large_signal_uses_data_range_plus_padding(self):
        lo, hi = y_limits(-2.0, 3.0, min_span=0.05, pad=0.05)
        self.assertAlmostEqual(lo, -2.25)
        self.assertAlmostEqual(hi, 3.25)


class NextYRangeTest(unittest.TestCase):
    def test_axis_holds_still_while_data_fits(self):
        r = next_y_range(None, 0.00, 0.02, min_span=0.5)
        self.assertAlmostEqual(r[1] - r[0], 0.5)
        self.assertAlmostEqual(sum(r) / 2, 0.01)             # first frame: centred on the data
        # force applied, but still inside the window: axis must not move
        self.assertEqual(next_y_range(r, 0.10, 0.20, min_span=0.5), r)
        self.assertEqual(next_y_range(r, -0.20, -0.18, min_span=0.5), r)

    def test_recentres_once_when_data_leaves_window(self):
        r = next_y_range(None, 0.0, 0.02, min_span=0.5)          # window ~[-0.24, 0.26]
        r2 = next_y_range(r, 0.30, 0.32, min_span=0.5)          # small, but outside
        self.assertAlmostEqual(r2[1] - r2[0], 0.5)
        self.assertAlmostEqual(sum(r2) / 2, 0.31)
        self.assertEqual(next_y_range(r2, 0.28, 0.35, min_span=0.5), r2)

    def test_scales_beyond_min_span_and_returns(self):
        r = next_y_range(None, 0.0, 0.02, min_span=0.5)
        big = next_y_range(r, -1.0, 3.0, min_span=0.5)
        self.assertAlmostEqual(big[0], -1.2)
        self.assertAlmostEqual(big[1], 3.2)
        # data changes while wide: keeps autoscaling
        self.assertAlmostEqual(next_y_range(big, -1.0, 2.0, min_span=0.5)[1], 2.15)
        # back to a quiet signal: fixed min-span window centred on it again
        back = next_y_range(big, 0.05, 0.07, min_span=0.5)
        self.assertAlmostEqual(back[1] - back[0], 0.5)
        self.assertAlmostEqual(sum(back) / 2, 0.06)
        self.assertEqual(next_y_range(back, 0.0, 0.1, min_span=0.5), back)


class TestHistoryTest(unittest.TestCase):
    def test_starts_at_zero_and_keeps_everything_below_limit(self):
        h = TestHistory(limit=1000)
        t = 100.0 + np.arange(600) / 100.0
        y = np.stack([np.sin(t)] * 6, axis=1)
        for i in range(0, 600, 64):
            h.extend(t[i:i + 64], y[i:i + 64])
        self.assertEqual(h.n, 600)
        self.assertEqual(h.t[0], 0.0)
        self.assertAlmostEqual(h.duration(), 5.99)
        x, yd = h.decimated([0, 3], buckets=1000)          # small: returned undecimated
        np.testing.assert_allclose(x, t - 100.0)
        self.assertEqual(yd.shape, (2, 600))

    def test_compaction_is_bounded_and_keeps_extremes(self):
        h = TestHistory(limit=4000)
        n = 50_000
        t = np.arange(n) / 1000.0
        y = np.zeros((n, 6))
        y[123, 2] = 9.0                                     # early spike must survive compaction
        y[40_000, 2] = -7.0
        for i in range(0, n, 333):
            h.extend(t[i:i + 333], y[i:i + 333])
        self.assertLessEqual(h.n, 4000)
        self.assertEqual(h.t[0], 0.0)
        self.assertAlmostEqual(h.duration(), t[-1])         # newest sample kept
        self.assertTrue(np.all(np.diff(h.t[:h.n]) >= 0))
        self.assertEqual(h.y[2, :h.n].max(), 9.0)
        self.assertEqual(h.y[2, :h.n].min(), -7.0)


if __name__ == "__main__":
    unittest.main()
