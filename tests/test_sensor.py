"""AxiaSensor end-to-end against the simulator in tools/fake_axia.py."""

import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from axia80 import AxiaSensor  # noqa: E402
from fake_axia import FakeAxia  # noqa: E402


class SensorTest(unittest.TestCase):
    def setUp(self):
        self.sim = FakeAxia("127.0.0.1", rdt_port=0, http_port=0, rate=2000).start()
        self.sensor = AxiaSensor("127.0.0.1", rdt_port=self.sim.rdt_port,
                                 http_port=self.sim.http_port, timeout=1.0)

    def tearDown(self):
        self.sensor.close()
        self.sim.stop()

    def test_info(self):
        info = self.sensor.info()
        self.assertEqual(info["config"]["nethwaddr"], "00:16:BD:00:4D:EC")
        self.assertEqual(info["config"]["cfgcpf"], 1000000)
        self.assertEqual([c["calsn"] for c in info["calibrations"]], ["FT54714", "FT54715"])
        self.assertEqual(info["calibrations"][0]["calmr"], [150, 150, 470, 8, 8, 8])

    def test_read_scaled(self):
        s = self.sensor.read()
        self.assertEqual(self.sensor.force_units, "N")
        self.assertAlmostEqual(s.ft[2], -4.905, delta=0.6)   # 0.5 kg tool weight on Fz
        self.assertEqual(s.status, 0)
        self.assertEqual(s.flags, [])

    def test_bias(self):
        self.sensor.read()
        self.sensor.bias()
        s = self.sensor.read()
        self.assertLess(abs(s.ft[2]), 0.2)
        self.sensor.clear_bias()
        s = self.sensor.read()
        self.assertLess(s.ft[2], -4.0)

    def test_stream_count(self):
        samples = list(self.sensor.stream(count=500))
        self.assertEqual(len(samples), 500)
        self.assertEqual([s.rdt_seq for s in samples], list(range(1, 501)))
        self.assertEqual(self.sensor.dropped, 0)

    def test_stream_buffered(self):
        self.sensor.set_rdt_buffer(20)
        samples = []
        for s in self.sensor.stream(buffered=True):
            samples.append(s)
            if len(samples) >= 400:
                break
        self.assertEqual([s.rdt_seq for s in samples], list(range(1, 401)))

    def test_stream_detects_drops(self):
        self.sim.drop_every = 10
        samples = list(self.sensor.stream(count=100))
        self.assertEqual(len(samples) + self.sensor.dropped, 100)
        self.assertEqual(self.sensor.dropped, 10)

    def test_config(self):
        self.sensor.set_adc_rate(1000)
        self.sensor.set_filter(3)
        self.sensor.select_calibration(1)
        cfg = self.sensor.config_xml()
        self.assertEqual((cfg["setrate"], cfg["setiirshift"], cfg["cfgcalsel"]), (976, 3, 1))
        with self.assertRaises(ValueError):
            self.sensor.set_adc_rate(1234)


if __name__ == "__main__":
    unittest.main()
