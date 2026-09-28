"""Linux network helpers for finding the sensor by MAC address and re-addressing it.

Lookup order used by `find`:
  1. The kernel ARP/neighbour cache (/proc/net/arp) - instant, no root needed.
  2. Active ARP sweep of candidate subnets on wired interfaces. Devices answer an
     ARP request for their own address even when the asker is on another subnet,
     so this finds the sensor wherever it is. Needs root (raw sockets).
  3. Passive listen while the user power-cycles the sensor: it announces its
     address with ARP packets when it boots. Needs root.

Raw sockets (AF_PACKET) are Linux-only; everything here uses the standard library.
"""

import fcntl
import ipaddress
import os
import re
import select
import socket
import struct
import time

ETH_P_ALL = 0x0003
ETH_P_ARP = 0x0806
ETH_P_IP = 0x0800
BROADCAST = b"\xff" * 6
SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B

# Subnets swept by default in addition to each interface's own subnet. Covers the
# sensor's factory address and the common 192.168.x.x ranges (~65k addresses).
DEFAULT_SWEEP = ("192.168.0.0/16",)


# --- MAC helpers ---------------------------------------------------------------

def normalize_mac(mac):
    """'00-16-bd-00-4d-ec' / '0016.bd00.4dec' -> '00:16:BD:00:4D:EC'."""
    digits = re.sub(r"[^0-9A-Fa-f]", "", str(mac))
    if len(digits) != 12:
        raise ValueError(f"not a MAC address: {mac!r}")
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)).upper()


def mac_bytes(mac):
    return bytes.fromhex(normalize_mac(mac).replace(":", ""))


# --- local interfaces -----------------------------------------------------------

def wired_interfaces():
    """Names of up, non-loopback, non-wireless Ethernet interfaces."""
    result = []
    base = "/sys/class/net"
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        try:
            if open(os.path.join(path, "type")).read().strip() != "1":        # ARPHRD_ETHER
                continue
            if os.path.exists(os.path.join(path, "wireless")) or \
               os.path.exists(os.path.join(path, "phy80211")):
                continue
            if not os.path.exists(os.path.join(path, "device")):              # skip bridges/veth/docker
                continue
            if open(os.path.join(path, "operstate")).read().strip() not in ("up", "unknown"):
                continue
        except OSError:
            continue
        result.append(name)
    return result


def iface_mac(iface):
    with open(f"/sys/class/net/{iface}/address") as f:
        return f.read().strip()


def iface_ipv4(iface):
    """Returns the interface's primary IPv4 as an IPv4Interface, or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        req = struct.pack("256s", iface.encode()[:15])
        addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFADDR, req)[20:24])
        mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFNETMASK, req)[20:24])
        return ipaddress.IPv4Interface(f"{addr}/{mask}")
    except OSError:
        return None
    finally:
        s.close()


def local_ipv4_networks():
    """[(iface, IPv4Interface)] for every interface with an IPv4 address (any type)."""
    out = []
    for name in sorted(os.listdir("/sys/class/net")):
        ip = iface_ipv4(name)
        if ip is not None and not ip.ip.is_loopback:
            out.append((name, ip))
    return out


def reachable_from(ip):
    """Returns (iface, local address) of an interface on the same subnet as ip, or None."""
    ip = ipaddress.IPv4Address(str(ip))
    for name, local in local_ipv4_networks():
        if ip in local.network:
            return name, local
    return None


def is_root():
    return os.geteuid() == 0


# --- 1. neighbour cache ---------------------------------------------------------

def neigh_lookup(mac, arp_table_text=None):
    """IP addresses the kernel ARP cache associates with mac (no root needed)."""
    if arp_table_text is None:
        try:
            with open("/proc/net/arp") as f:
                arp_table_text = f.read()
        except OSError:
            return []
    want = normalize_mac(mac)
    found = []
    for line in arp_table_text.splitlines()[1:]:
        parts = line.split()
        # IP address, HW type, Flags, HW address, Mask, Device
        if len(parts) >= 6 and parts[2] != "0x0":
            try:
                if normalize_mac(parts[3]) == want:
                    found.append((parts[0], parts[5]))
            except ValueError:
                pass
    return found


# --- frame building / parsing (pure, unit-tested) ---------------------------------

def build_arp_request(src_mac, src_ip, target_ip):
    """Ethernet broadcast ARP who-has target_ip."""
    eth = BROADCAST + src_mac + struct.pack("!H", ETH_P_ARP)
    arp = struct.pack("!HHBBH6s4s6s4s", 1, ETH_P_IP, 6, 4, 1,
                      src_mac, socket.inet_aton(str(src_ip)),
                      b"\x00" * 6, socket.inet_aton(str(target_ip)))
    return eth + arp


def sender_ip_from_frame(frame, mac=None):
    """IPv4 address the frame's sender claims (ARP sender or IPv4 source), or None.

    If mac (bytes) is given, only frames whose Ethernet source is that MAC count.
    0.0.0.0 (DHCP discover, ARP probes) is ignored.
    """
    if len(frame) < 34:
        return None
    src = frame[6:12]
    if mac is not None and src != mac:
        return None
    ethertype = struct.unpack("!H", frame[12:14])[0]
    ip = None
    if ethertype == ETH_P_ARP and len(frame) >= 42:
        if frame[16:18] == b"\x08\x00" and frame[18] == 6 and frame[19] == 4:
            ip = socket.inet_ntoa(frame[28:32])
    elif ethertype == ETH_P_IP and (frame[14] >> 4) == 4:
        ip = socket.inet_ntoa(frame[26:30])
    if ip in (None, "0.0.0.0"):
        return None
    return ip


def sweep_targets(networks, max_hosts=1 << 17):
    """Unique host addresses in the given networks, capped at max_hosts."""
    seen = set()
    for net in networks:
        net = ipaddress.IPv4Network(str(net), strict=False)
        for ip in net.hosts():
            if ip not in seen:
                seen.add(ip)
                yield ip
                if len(seen) >= max_hosts:
                    return


# --- 2. ARP sweep and 3. passive listen (root) ----------------------------------------

def _raw_socket(iface=None):
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    if iface:
        s.bind((iface, 0))
    return s


def _poll_for(sock, want_mac, deadline):
    """Reads frames until deadline; returns the first IPv4 claimed by want_mac, or None."""
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        ready, _, _ = select.select([sock], [], [], min(remaining, 0.05))
        if not ready:
            continue
        while True:
            try:
                frame = sock.recv(2048, socket.MSG_DONTWAIT)
            except BlockingIOError:
                break
            ip = sender_ip_from_frame(frame, want_mac)
            if ip:
                return ip


def arp_sweep(mac, iface, networks, pps=8000, progress=None):
    """Sends ARP who-has for every host in networks out of iface; returns the sensor's IP or None."""
    want = mac_bytes(mac)
    src_mac = mac_bytes(iface_mac(iface))
    local = iface_ipv4(iface)
    src_ip = local.ip if local else ipaddress.IPv4Address("0.0.0.0")
    targets = list(sweep_targets(networks))
    sock = _raw_socket(iface)
    try:
        interval = 1.0 / pps
        next_send = time.time()
        for i, ip in enumerate(targets):
            sock.send(build_arp_request(src_mac, src_ip, ip))
            next_send += interval
            if i % 256 == 0:
                found = _poll_for(sock, want, time.time() + 0.001)
                if found:
                    return found
                if progress:
                    progress(i, len(targets))
            delay = next_send - time.time()
            if delay > 0:
                time.sleep(delay)
        return _poll_for(sock, want, time.time() + 1.0)   # late replies
    finally:
        sock.close()


def listen_for(mac, seconds, iface=None):
    """Waits up to `seconds` for any frame from mac that reveals its IPv4 address."""
    sock = _raw_socket(iface)
    try:
        return _poll_for(sock, mac_bytes(mac), time.time() + seconds)
    finally:
        sock.close()


# --- config.yaml ----------------------------------------------------------------

_HOST_LINE = re.compile(r"^(host:[ \t]*)([^\s#]*)(.*)$", re.MULTILINE)


def set_config_host(text, new_host):
    """Returns config.yaml text with the top-level host value replaced (comments kept)."""
    if _HOST_LINE.search(text):
        return _HOST_LINE.sub(lambda m: f"{m.group(1)}{new_host}{m.group(3)}", text, count=1)
    return f"host: {new_host}\n" + text


def update_config_host(path, new_host):
    with open(path) as f:
        text = f.read()
    with open(path, "w") as f:
        f.write(set_config_host(text, new_host))
