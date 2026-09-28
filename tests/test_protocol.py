import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from axia80 import protocol as p  # noqa: E402


class ProtocolTest(unittest.TestCase):
    def test_rdt_request_layout(self):
        # Manual 12.1.1: header 0x1234, command, sample_count, big-endian, 8 bytes
        self.assertEqual(p.pack_rdt_request(p.RDT_START_SINGLE, 0), bytes.fromhex("1234000200000000"))
        self.assertEqual(p.pack_rdt_request(p.RDT_BIAS, 5), bytes.fromhex("1234004200000005"))
        self.assertEqual(p.unpack_rdt_request(p.pack_rdt_request(3, 1000)), (3, 1000))

    def test_rdt_record_roundtrip(self):
        counts = (1, -2, 300000, -4000000, 5, -2147483648)
        pkt = p.pack_rdt_record(7, 123456, 0x80000002, counts)
        self.assertEqual(len(pkt), 36)
        self.assertEqual(p.unpack_rdt_packet(pkt), [(7, 123456, 0x80000002, counts)])

    def test_buffered_packet(self):
        pkt = b"".join(p.pack_rdt_record(i, i * 2, 0, (i,) * 6) for i in range(1, 41))
        recs = p.unpack_rdt_packet(pkt)
        self.assertEqual(len(recs), 40)
        self.assertEqual(recs[-1][0], 40)

    def test_bad_packet_length(self):
        with self.assertRaises(ValueError):
            p.unpack_rdt_packet(b"\x00" * 35)

    def test_seq_gap(self):
        self.assertEqual(p.seq_gap(1, 2), 0)
        self.assertEqual(p.seq_gap(1, 5), 3)
        self.assertEqual(p.seq_gap(0xFFFFFFFF, 0), 0)
        self.assertEqual(p.seq_gap(0xFFFFFFFE, 1), 2)

    def test_tcp_commands(self):
        self.assertEqual(len(p.pack_tcp_readft()), 20)
        self.assertEqual(p.pack_tcp_readft(bias=True)[-2:], b"\x00\x01")
        cal = p.pack_tcp_readcalinfo()
        self.assertEqual(len(cal), 20)
        self.assertEqual(cal[0], 1)

    def test_tcp_calinfo(self):
        import struct
        data = struct.pack(">HBBII6H", 0x1234, 2, 3, 1000000, 1000000, *([1] * 6))
        info = p.unpack_tcp_calinfo(data)
        self.assertEqual((info["force_units"], info["torque_units"], info["cpf"]), ("N", "N-m", 1000000))

    def test_decode_status(self):
        self.assertEqual(p.decode_status(0), [])
        self.assertEqual(p.decode_status(0x80000002), ["supply voltage out of range", "error"])
        self.assertIn("F/T out of range", p.decode_status(0xC0000000))


if __name__ == "__main__":
    unittest.main()
