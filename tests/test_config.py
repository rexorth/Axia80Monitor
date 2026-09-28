import contextlib
import ipaddress
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from axia80 import config as C  # noqa: E402
from axia80.cli import main  # noqa: E402
from fake_axia import FakeAxia  # noqa: E402

PROJECT_CONFIG = os.path.join(ROOT, "config.yaml")


def _write(text):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


class ConfigTest(unittest.TestCase):
    def test_project_config_loads(self):
        cfg, path = C.load_config(PROJECT_CONFIG)
        ipaddress.IPv4Address(str(cfg["host"]))            # any valid address; users edit this
        self.assertEqual(cfg["mac"], "00:16:BD:00:4D:EC")
        self.assertEqual(cfg["sensor"]["adc_rate"], None)
        self.assertEqual(cfg["monitor"]["hz"], 20)
        self.assertIs(cfg["stream"]["buffered"], False)

    def test_fallback_parser_matches_pyyaml(self):
        with open(PROJECT_CONFIG) as f:
            text = f.read()
        simple = C._parse_simple_yaml(text)
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        self.assertEqual(simple, yaml.safe_load(text))

    def test_values_and_unknown_keys(self):
        path = _write("host: 10.0.0.5  # lab\nsensor:\n  filter: 3\nstream:\n  csv: 'a #1.csv'\n")
        try:
            cfg, _ = C.load_config(path)
            self.assertEqual((cfg["host"], cfg["sensor"]["filter"], cfg["stream"]["csv"]),
                             ("10.0.0.5", 3, "a #1.csv"))
            self.assertEqual(cfg["timeout"], 1.0)   # default kept
        finally:
            os.unlink(path)
        path = _write("hots: 1.2.3.4\n")
        try:
            with self.assertRaises(ValueError):
                C.load_config(path)
        finally:
            os.unlink(path)

    def test_missing_explicit_file(self):
        with self.assertRaises(FileNotFoundError):
            C.load_config("/nonexistent/config.yaml")


class CliConfigTest(unittest.TestCase):
    def setUp(self):
        self.sim = FakeAxia("127.0.0.1", rdt_port=0, http_port=0, rate=1000).start()
        self.csv = tempfile.mktemp(suffix=".csv")
        self.cfg = _write(
            f"host: 127.0.0.1\nrdt_port: {self.sim.rdt_port}\nhttp_port: {self.sim.http_port}\n"
            f"mac: '00-16-bd-00-4d-ec'\nsensor:\n  filter: 4\n  rdt_buffer: 5\n"
            f"bias_on_start: true\nstream:\n  csv: {self.csv}\n  count: 200\n")

    def tearDown(self):
        self.sim.stop()
        os.unlink(self.cfg)
        if os.path.exists(self.csv):
            os.unlink(self.csv)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", self.cfg, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_info_mac_check(self):
        code, out, _ = self.run_cli("info")
        self.assertEqual(code, 0)
        self.assertIn("(matches config)", out)

    def test_stream_uses_config(self):
        code, _, err = self.run_cli("stream")
        self.assertEqual(code, 0, err)
        with open(self.csv) as f:
            rows = f.read().splitlines()
        self.assertEqual(len(rows), 201)                    # header + count from config
        self.assertEqual((self.sim.filter, self.sim.rdt_buffer), (4, 5))   # launch settings applied
        fz = float(rows[1].split(",")[6])
        self.assertLess(abs(fz), 0.5)                       # bias_on_start removed the tool weight

    def test_flag_overrides_config(self):
        code, _, err = self.run_cli("stream", "--count", "50")
        self.assertEqual(code, 0, err)
        with open(self.csv) as f:
            self.assertEqual(len(f.read().splitlines()), 51)


if __name__ == "__main__":
    unittest.main()
