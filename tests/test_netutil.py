import contextlib
import io
import os
import socket
import struct
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from axia80 import netutil as n  # noqa: E402
from axia80.cli import main  # noqa: E402
from fake_axia import FakeAxia  # noqa: E402

SENSOR_MAC = "00:16:BD:00:4D:EC"
ARP_TABLE = """IP address       HW type     Flags       HW address            Mask     Device
192.168.0.3      0x1         0x2         00:16:bd:00:4d:ec     *        enx00e04c
192.168.0.9      0x1         0x0         00:16:bd:00:4d:ec     *        enx00e04c
10.0.0.1         0x1         0x2         aa:bb:cc:dd:ee:ff     *        wlp2s0
"""


class NetutilTest(unittest.TestCase):
    def test_normalize_mac(self):
        for form in ("00-16-bd-00-4d-ec", "0016.bd00.4dec", "00:16:BD:00:4D:EC"):
            self.assertEqual(n.normalize_mac(form), SENSOR_MAC)
        with self.assertRaises(ValueError):
            n.normalize_mac("00:16:bd")

    def test_neigh_lookup_skips_incomplete(self):
        self.assertEqual(n.neigh_lookup(SENSOR_MAC, ARP_TABLE), [("192.168.0.3", "enx00e04c")])
        self.assertEqual(n.neigh_lookup("11:22:33:44:55:66", ARP_TABLE), [])

    def test_arp_frames(self):
        mine, sensor = n.mac_bytes("02:00:00:00:00:01"), n.mac_bytes(SENSOR_MAC)
        req = n.build_arp_request(mine, "192.168.0.1", "192.168.0.3")
        self.assertEqual(len(req), 42)
        self.assertEqual(req[:6], b"\xff" * 6)
        self.assertEqual(n.sender_ip_from_frame(req), "192.168.0.1")
        self.assertIsNone(n.sender_ip_from_frame(req, sensor))          # not from the sensor
        # ARP reply from the sensor
        reply = mine + sensor + struct.pack("!H", 0x0806) + struct.pack(
            "!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, 2, sensor, socket.inet_aton("192.168.0.3"),
            mine, socket.inet_aton("192.168.0.1"))
        self.assertEqual(n.sender_ip_from_frame(reply, sensor), "192.168.0.3")
        # ARP probe (sender 0.0.0.0) reveals nothing
        probe = n.build_arp_request(sensor, "0.0.0.0", "192.168.0.3")
        self.assertIsNone(n.sender_ip_from_frame(probe, sensor))

    def test_ipv4_frame(self):
        sensor = n.mac_bytes(SENSOR_MAC)
        ip_hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20, 0, 0, 64, 17, 0,
                             socket.inet_aton("10.1.2.3"), socket.inet_aton("10.1.2.255"))
        frame = b"\xff" * 6 + sensor + struct.pack("!H", 0x0800) + ip_hdr
        self.assertEqual(n.sender_ip_from_frame(frame, sensor), "10.1.2.3")

    def test_sweep_targets(self):
        hosts = list(n.sweep_targets(["192.168.0.0/24", "192.168.0.0/25"]))
        self.assertEqual(len(hosts), 254)
        self.assertEqual(len(list(n.sweep_targets(["10.0.0.0/8"], max_hosts=1000))), 1000)

    def test_set_config_host_keeps_comment(self):
        text = "# header\nhost: 192.168.1.1            # sensor IP\nmac: x\n"
        out = n.set_config_host(text, "192.168.0.3")
        self.assertEqual(out, "# header\nhost: 192.168.0.3            # sensor IP\nmac: x\n")
        self.assertEqual(n.set_config_host("timeout: 1\n", "10.0.0.2"), "host: 10.0.0.2\ntimeout: 1\n")


class CliNetworkTest(unittest.TestCase):
    def setUp(self):
        self.sim = FakeAxia("127.0.0.1", rdt_port=0, http_port=0).start()
        fd, self.cfg = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write(f"host: 127.0.0.1    # sensor\nhttp_port: {self.sim.http_port}\n"
                    f"rdt_port: {self.sim.rdt_port}\nmac: '{SENSOR_MAC}'\n")

    def tearDown(self):
        self.sim.stop()
        os.unlink(self.cfg)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(["--config", self.cfg, *argv])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_set_ip(self):
        code, out, err = self.run_cli("set-ip", "192.168.77.3", "--yes", "--no-wait")
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.sim.net, {"comnetdhcp": 1, "comnetip": "192.168.77.3",
                                        "comnetmsk": "255.255.255.0", "comnetgw": "192.168.77.1"})
        self.assertIn("POWER-CYCLE", out)
        with open(self.cfg) as f:
            self.assertTrue(f.read().startswith("host: 192.168.77.3    # sensor\n"))

    def test_set_ip_needs_confirmation(self):
        code, out, _ = self.run_cli("set-ip", "192.168.77.3", "--no-wait")   # stdin is not a tty
        self.assertEqual(code, 0)
        self.assertIn("Aborted", out)
        self.assertEqual(self.sim.net["comnetip"], "192.168.1.1")

    def test_set_ip_rejects_bad_input(self):
        for bad in (["300.1.1.1"], ["192.168.77.0"], ["192.168.77.3", "--gateway", "10.0.0.1"]):
            code, _, err = self.run_cli("set-ip", *bad, "--yes", "--no-wait")
            self.assertEqual(code, 2, bad)
            self.assertIn("error", err)
        self.assertEqual(self.sim.net["comnetip"], "192.168.1.1")

    def test_find_at_configured_address(self):
        code, out, _ = self.run_cli("find")
        self.assertIn(code, (0, None))
        self.assertIn("Found sensor at 127.0.0.1", out)


if __name__ == "__main__":
    unittest.main()
