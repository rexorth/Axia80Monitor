"""Host-side client for the ATI Ethernet (NET) Axia80 force/torque sensor.

Data path:   UDP RDT on port 49152 (up to ~7.9 kHz, counts -> divide by cpf/cpt)
Config path: HTTP XML pages (netftapi2.xml, netftcalapi.xml) and CGI pages
Fallback:    TCP port 49151 READCALINFO for scaling if HTTP is unavailable
"""

import socket
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from . import protocol as p

AXES = ("Fx", "Fy", "Fz", "Tx", "Ty", "Tz")


@dataclass
class FTSample:
    t_host: float          # host time.time() when the packet arrived
    rdt_seq: int           # position in the current RDT stream (starts at 1)
    ft_seq: int            # sensor-internal sample counter (advances at the ADC rate)
    status: int            # 32-bit status word, 0 = healthy
    counts: tuple          # raw Fx..Tz counts
    ft: tuple              # Fx,Fy,Fz in force units; Tx,Ty,Tz in torque units

    @property
    def flags(self):
        return p.decode_status(self.status)


def _parse_xml_value(text):
    """Converts an XML element's text to int/float/list where it looks numeric."""
    if text is None:
        return None
    text = text.strip()
    if ";" in text or "," in text:
        parts = [s for s in text.replace(",", ";").split(";") if s.strip()]
        return [_parse_xml_value(s) for s in parts]
    for conv in (int, float):
        try:
            return conv(text, 0) if conv is int else conv(text)
        except ValueError:
            pass
    return text


class AxiaSensor:
    def __init__(self, host="192.168.1.1", rdt_port=p.RDT_PORT, http_port=80,
                 tcp_port=p.TCP_PORT, timeout=1.0):
        self.host = host
        self.rdt_port = rdt_port
        self.http_port = http_port
        self.tcp_port = tcp_port
        self.timeout = timeout
        self.cpf = None
        self.cpt = None
        self.force_units = None
        self.torque_units = None
        self.dropped = 0
        self._udp = None

    # --- context manager ----------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._udp is not None:
            try:
                self._udp.send(p.pack_rdt_request(p.RDT_STOP))
            except OSError:
                pass
            self._udp.close()
            self._udp = None

    # --- HTTP ---------------------------------------------------------------

    def _http_get(self, path, params=None):
        url = f"http://{self.host}:{self.http_port}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=max(self.timeout, 2.0)) as resp:
            return resp.read()

    def _xml(self, path, params=None):
        root = ET.fromstring(self._http_get(path, params))
        return {child.tag: _parse_xml_value(child.text) for child in root}

    def config_xml(self):
        """Active system/configuration settings (netftapi2.xml)."""
        return self._xml("netftapi2.xml")

    def calibration_xml(self, index=None):
        """Factory calibration info (netftcalapi.xml); index 0 or 1, None = active."""
        return self._xml("netftcalapi.xml", {"index": index} if index is not None else None)

    def info(self):
        cfg = self.config_xml()
        cals = []
        for idx in (0, 1):
            try:
                cals.append(self.calibration_xml(idx))
            except (OSError, ET.ParseError):
                break
        return {"config": cfg, "calibrations": cals}

    # --- scaling ------------------------------------------------------------

    def load_scaling(self):
        """Reads counts-per-force/torque for the active configuration."""
        try:
            cfg = self.config_xml()
            self.cpf = cfg["cfgcpf"]
            self.cpt = cfg["cfgcpt"]
            self.force_units = cfg.get("scfgfu", "N")
            self.torque_units = cfg.get("scfgtu", "Nm")
        except (OSError, KeyError, ET.ParseError):
            cal = self.tcp_calinfo()
            self.cpf = cal["cpf"]
            self.cpt = cal["cpt"]
            self.force_units = cal["force_units"]
            self.torque_units = cal["torque_units"]
        if not self.cpf or not self.cpt:
            raise RuntimeError(f"sensor reported invalid scaling cpf={self.cpf} cpt={self.cpt}")

    def tcp_calinfo(self):
        with socket.create_connection((self.host, self.tcp_port), timeout=self.timeout) as s:
            s.sendall(p.pack_tcp_readcalinfo())
            data = _recv_exact(s, p.TCP_CALINFO_RESPONSE_SIZE)
        return p.unpack_tcp_calinfo(data)

    def _scale(self, counts):
        cpf, cpt = self.cpf, self.cpt
        return (counts[0] / cpf, counts[1] / cpf, counts[2] / cpf,
                counts[3] / cpt, counts[4] / cpt, counts[5] / cpt)

    # --- UDP RDT ------------------------------------------------------------

    def _socket(self):
        if self._udp is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            s.connect((self.host, self.rdt_port))
            s.settimeout(self.timeout)
            self._udp = s
        return self._udp

    def _send(self, command, count=0):
        self._socket().send(p.pack_rdt_request(command, count))

    def _drain(self):
        """Discards stale packets left over from a previous stream."""
        s = self._socket()
        s.setblocking(False)
        try:
            while True:
                s.recv(65536)
        except (BlockingIOError, OSError):
            pass
        finally:
            s.settimeout(self.timeout)

    def bias(self):
        """Software bias (tare): current load becomes the zero reference."""
        self._send(p.RDT_BIAS)

    def clear_bias(self):
        """Removes the software bias by zeroing the strain gage offsets."""
        self._http_get("setting.cgi", {f"setbias{i}": 0 for i in range(6)})

    def read(self, retries=3):
        """Returns a single FTSample."""
        if self.cpf is None:
            self.load_scaling()
        for _ in range(retries):
            self._drain()
            self._send(p.RDT_START_SINGLE, 1)
            try:
                data = self._socket().recv(65536)
            except socket.timeout:
                continue
            rdt_seq, ft_seq, status, counts = p.unpack_rdt_packet(data)[-1]
            return FTSample(time.time(), rdt_seq, ft_seq, status, counts, self._scale(counts))
        raise TimeoutError(f"no RDT response from {self.host}:{self.rdt_port}")

    def stream(self, count=0, buffered=False):
        """Yields FTSamples continuously (count=0) or until `count` records are sent.

        buffered=True asks the sensor to pack several records per UDP packet
        (RDT buffer size, set via set_rdt_buffer), which greatly reduces the
        packet rate at high output rates. Lost records are counted in
        self.dropped. The stream is always stopped when the generator exits.
        """
        if self.cpf is None:
            self.load_scaling()
        self.dropped = 0
        self._drain()
        sock = self._socket()
        self._send(p.RDT_START_BUFFERED if buffered else p.RDT_START_SINGLE, count)
        prev_seq = None
        scale = self._scale
        try:
            while True:
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    if count and prev_seq is not None:
                        self.dropped += max(0, count - prev_seq)   # tail of a finite stream was lost
                        return
                    raise TimeoutError(f"RDT stream from {self.host} stalled") from None
                now = time.time()
                for rdt_seq, ft_seq, status, counts in p.unpack_rdt_packet(data):
                    if prev_seq is not None:
                        gap = p.seq_gap(prev_seq, rdt_seq)
                        if 0 < gap < 0x80000000:
                            self.dropped += gap
                    prev_seq = rdt_seq
                    yield FTSample(now, rdt_seq, ft_seq, status, counts, scale(counts))
                    if count and rdt_seq >= count:
                        return
        finally:
            try:
                self._send(p.RDT_STOP)
            except OSError:
                pass

    # --- configuration (CGI) ------------------------------------------------

    def set_adc_rate(self, rate_hz):
        """Internal sample rate: 500, 1000, 2000, 4000 or 8000 Hz (rounded values)."""
        value = p.ADC_RATES.get(rate_hz, rate_hz)
        if value not in p.ADC_RATES.values():
            raise ValueError(f"ADC rate must be one of {sorted(p.ADC_RATES)} Hz")
        self._http_get("setting.cgi", {"setadcrate": value})

    def set_filter(self, level):
        """Low-pass filter: 0 = off, 1..8 = progressively lower cutoff (see manual 4.5)."""
        if not 0 <= level <= 8:
            raise ValueError("filter level must be 0..8")
        self._http_get("setting.cgi", {"setuserfilter": level})

    def set_rdt_buffer(self, size):
        """Records per UDP packet in buffered mode (1..40)."""
        if not 1 <= size <= 40:
            raise ValueError("RDT buffer size must be 1..40")
        self._http_get("comm.cgi", {"comrdtbsiz": size})

    def network_settings(self):
        """Stored network settings as read back from netftapi2.xml (take effect at power-up)."""
        cfg = self.config_xml()
        return {"dhcp": cfg.get("comnetdhcp"), "ip": cfg.get("comnetip"), "netmask": cfg.get("comnetmsk"),
                "gateway": cfg.get("comnetgw"), "ip_in_use": cfg.get("netip"),
                "mac": cfg.get("nethwaddr", cfg.get("mfgdighwa"))}

    def set_network(self, ip, netmask="255.255.255.0", gateway=None, dhcp=False):
        """Stores a new static IP (and DHCP mode). The sensor only applies it after a power cycle.

        comnetdhcp values per manual table 9.5: 0 = use DHCP if available, 1 = static IP only.
        """
        params = {"comnetdhcp": 0 if dhcp else 1, "comnetip": ip, "comnetmsk": netmask}
        if gateway:
            params["comnetgw"] = gateway
        self._http_get("comm.cgi", params)
        return self.network_settings()

    def select_calibration(self, index):
        """0 = full range (Fxy 150 N / Fz 470 N / 8 Nm), 1 = half range, finer resolution."""
        if index not in (0, 1):
            raise ValueError("calibration index must be 0 or 1")
        self._http_get("config.cgi", {"cfgcalsel": index})
        self.cpf = None   # scaling changes with the calibration


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("sensor closed TCP connection")
        buf += chunk
    return buf
