"""Software stand-in for an ATI Ethernet Axia80, for testing without hardware.

Serves the UDP RDT protocol (start/stop/bias/count/buffered) and the HTTP
XML/CGI pages the client uses. Loads are slow sine waves plus a constant
"tool weight" on Fz, so bias has something to remove.

    python tools/fake_axia.py [--host 127.0.0.1] [--rdt-port 49152] [--http-port 8080] [--rate 1000]
    python -m axia80 --host 127.0.0.1 --http-port 8080 info
"""

import argparse
import math
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from axia80 import protocol as p  # noqa: E402

CPF = 1000000
CPT = 1000000
CALS = [
    {"calsn": "FT54714", "calpn": "SI-150-8", "calmr": [150, 150, 470, 8, 8, 8]},
    {"calsn": "FT54715", "calpn": "SI-75-4", "calmr": [75, 75, 235, 4, 4, 4]},
]


class FakeAxia:
    def __init__(self, host="127.0.0.1", rdt_port=p.RDT_PORT, http_port=8080, rate=1000):
        self.rate = rate
        self.adc_rate = 7912
        self.filter = 0
        self.rdt_buffer = 10
        self.cal = 0
        self.bias = [0] * 6
        self.t0 = time.time()
        self.lock = threading.Lock()
        self.client = None       # (addr, command, count, rdt_seq, start_time, sent)
        self.drop_every = 0      # test hook: skip every Nth record to simulate loss
        self.net = {"comnetdhcp": 0, "comnetip": "192.168.1.1", "comnetmsk": "255.255.255.0",
                    "comnetgw": "0.0.0.0"}   # stored settings; a real sensor applies them at power-up

        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind((host, rdt_port))
        self.rdt_port = self.udp.getsockname()[1]
        handler = type("Handler", (_HttpHandler,), {"sensor": self})
        self.http = ThreadingHTTPServer((host, http_port), handler)
        self.http_port = self.http.server_address[1]
        self._stop = threading.Event()

    # --- simulated physics --------------------------------------------------

    def raw_counts(self, ft_seq):
        t = ft_seq / self.adc_rate
        ft = [2.0 * math.sin(2 * math.pi * 0.5 * t),
              1.0 * math.cos(2 * math.pi * 0.3 * t),
              -9.81 * 0.5 + 0.5 * math.sin(2 * math.pi * 0.2 * t),   # 0.5 kg tool
              0.05 * math.sin(2 * math.pi * 0.4 * t),
              0.03 * math.cos(2 * math.pi * 0.25 * t),
              0.01 * math.sin(2 * math.pi * 0.1 * t)]
        return [int(v * (CPF if i < 3 else CPT)) for i, v in enumerate(ft)]

    def counts(self, ft_seq):
        return [c - b for c, b in zip(self.raw_counts(ft_seq), self.bias)]

    def ft_seq_now(self):
        return int((time.time() - self.t0) * self.adc_rate)

    # --- servers ------------------------------------------------------------

    def start(self):
        threading.Thread(target=self._udp_rx, daemon=True).start()
        threading.Thread(target=self._udp_tx, daemon=True).start()
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
        self.http.shutdown()
        self.http.server_close()
        self.udp.close()

    def _udp_rx(self):
        while not self._stop.is_set():
            try:
                data, addr = self.udp.recvfrom(64)
                command, count = p.unpack_rdt_request(data)
            except (OSError, ValueError, Exception):
                if self._stop.is_set():
                    return
                continue
            with self.lock:
                if command == p.RDT_STOP:
                    self.client = None
                elif command in (p.RDT_START_SINGLE, p.RDT_START_BUFFERED):
                    self.client = {"addr": addr, "cmd": command, "count": count,
                                   "seq": 0, "start": time.time(), "sent": 0}
                elif command == p.RDT_BIAS:
                    self.bias = self.raw_counts(self.ft_seq_now())

    def _udp_tx(self):
        while not self._stop.is_set():
            time.sleep(0.0005)
            with self.lock:
                c = self.client
                if c is None:
                    continue
                due = int((time.time() - c["start"]) * self.rate) + 1 - c["sent"]
                if c["count"]:
                    due = min(due, c["count"] - c["sent"])
                block = self.rdt_buffer if c["cmd"] == p.RDT_START_BUFFERED else 1
                packets = []
                while due >= block:
                    recs = b""
                    for _ in range(block):
                        c["seq"] = (c["seq"] + 1) & 0xFFFFFFFF
                        c["sent"] += 1
                        if self.drop_every and c["seq"] % self.drop_every == 0 and block == 1:
                            continue
                        ft_seq = self.ft_seq_now()
                        recs += p.pack_rdt_record(c["seq"], ft_seq, 0, self.counts(ft_seq))
                    if recs:
                        packets.append(recs)
                    due -= block
                if c["count"] and c["sent"] >= c["count"]:
                    self.client = None
                addr = c["addr"]
            for pkt in packets:
                try:
                    self.udp.sendto(pkt, addr)
                except OSError:
                    pass

    # --- HTTP pages ---------------------------------------------------------

    def netftapi2(self):
        cal = CALS[self.cal]
        runft = ";".join(str(v) for v in self.counts(self.ft_seq_now()))
        fields = {
            "runstat": "0x00000000", "runft": runft, "setrate": self.adc_rate,
            "setiirshift": self.filter, "cfgcalsel": self.cal, "cfgcalsn": cal["calsn"],
            "cfgfu": 2, "scfgfu": "N", "cfgtu": 3, "scfgtu": "Nm",
            "cfgcpf": CPF, "cfgcpt": CPT, **self.net,
            "nethwaddr": "00:16:BD:00:4D:EC", "commrdtrate": self.rate,
            "comrdtbsiz": self.rdt_buffer, "mfgdigver": "SIM", "mfgdigrev": "SIM",
            "mfgdigsn": "SIM0001", "netip": "192.168.1.1", "runrate": self.adc_rate,
        }
        return _xml("netft", fields)

    def netftcalapi(self, index):
        cal = CALS[self.cal if index is None else index]
        fields = {"calsn": cal["calsn"], "calpn": cal["calpn"], "caldt": "2024/01/01",
                  "calfu": 2, "scalfu": "N", "caltu": 3, "scaltu": "Nm",
                  "calmr": ";".join(str(v) for v in cal["calmr"]),
                  "calcpf": CPF, "calcpt": CPT}
        return _xml("netcal", fields)

    def cgi(self, page, q):
        if page == "setting.cgi":
            if "setadcrate" in q:
                self.adc_rate = {7812: 7912}.get(int(q["setadcrate"]), int(q["setadcrate"]))
            if "setuserfilter" in q:
                self.filter = int(q["setuserfilter"])
            if any(k.startswith("setbias") for k in q):
                self.bias = [0] * 6
        elif page == "comm.cgi":
            if "comrdtbsiz" in q:
                self.rdt_buffer = int(q["comrdtbsiz"])
            for key in ("comnetip", "comnetmsk", "comnetgw"):
                if key in q:
                    self.net[key] = q[key]
            if "comnetdhcp" in q:
                self.net["comnetdhcp"] = int(q["comnetdhcp"])
        elif page == "config.cgi" and "cfgcalsel" in q:
            self.cal = int(q["cfgcalsel"])


def _xml(root, fields):
    body = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    return f'<?xml version="1.0"?><{root}>{body}</{root}>'.encode()


class _HttpHandler(BaseHTTPRequestHandler):
    sensor = None

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        page = url.path.lstrip("/")
        with self.sensor.lock:
            if page == "netftapi2.xml":
                body, ctype = self.sensor.netftapi2(), "text/xml"
            elif page == "netftcalapi.xml":
                idx = int(q["index"]) if "index" in q else None
                body, ctype = self.sensor.netftcalapi(idx), "text/xml"
            elif page.endswith(".cgi"):
                self.sensor.cgi(page, q)
                body, ctype = b"<html>OK</html>", "text/html"
            else:
                self.send_error(404)
                return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--rdt-port", type=int, default=p.RDT_PORT)
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--rate", type=int, default=1000, help="RDT output rate, Hz")
    args = ap.parse_args()
    sim = FakeAxia(args.host, args.rdt_port, args.http_port, args.rate).start()
    print(f"Fake Axia80: RDT udp://{args.host}:{sim.rdt_port}  HTTP http://{args.host}:{sim.http_port}"
          f"  rate {args.rate} Hz")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()
