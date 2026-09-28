"""Command-line tool: python -m axia80 [--config FILE] [--host IP] [command] ...

With no command, opens the graphical console (axia80/gui.py).

Commands: info, read, monitor, stream, bias, config, set-ip, find
Defaults come from config.yaml (see axia80/config.py); flags override them.
"""

import argparse
import csv
import ipaddress
import os
import select
import sys
import time

from . import protocol as p
from .config import load_config
from .sensor import AXES, AxiaSensor


def _norm_mac(mac):
    return str(mac).strip().upper().replace("-", ":")


def cmd_info(sensor, args):
    info = sensor.info()
    cfg = info["config"]
    print(f"Sensor {sensor.host}")
    mac = cfg.get("nethwaddr", cfg.get("mfgdighwa", "?"))
    expected = getattr(args, "mac", None)
    note = ""
    if expected and mac != "?":
        note = "  (matches config)" if _norm_mac(mac) == _norm_mac(expected) else \
               f"  WARNING: config.yaml expects {expected}; is this the right sensor?"
    print(f"  MAC address        {mac}{note}")
    print(f"  IP in use          {cfg.get('netip', '?')}   DHCP setting: {cfg.get('comnetdhcp', '?')}")
    print(f"  Firmware / HW rev  {cfg.get('mfgdigver', '?')} / {cfg.get('mfgdigrev', '?')}")
    print(f"  Digital board S/N  {cfg.get('mfgdigsn', '?')}")
    print(f"  Active calibration {cfg.get('cfgcalsel', '?')}  (S/N {cfg.get('cfgcalsn', '?')})")
    print(f"  Units              force {cfg.get('scfgfu', '?')}, torque {cfg.get('scfgtu', '?')}")
    print(f"  Counts per force   {cfg.get('cfgcpf', '?')}")
    print(f"  Counts per torque  {cfg.get('cfgcpt', '?')}")
    print(f"  ADC sample rate    {cfg.get('runrate', '?')} Hz   filter setting {cfg.get('setiirshift', '?')}")
    print(f"  RDT output rate    {cfg.get('commrdtrate', '?')} Hz   RDT buffer size {cfg.get('comrdtbsiz', '?')}")
    status = cfg.get("runstat", 0)
    if isinstance(status, str):
        status = int(status, 16)
    flags = p.decode_status(status)
    print(f"  Status             0x{status:08X} {'OK' if not flags else ', '.join(flags)}")
    for idx, cal in enumerate(info["calibrations"]):
        ranges = cal.get("calmr", [])
        if isinstance(ranges, list):
            ranges = "  ".join(f"{a}={r:g}" for a, r in zip(AXES, ranges))
        print(f"  Calibration {idx}: S/N {cal.get('calsn', '?')}  type {cal.get('calpn', '?')}"
              f"  date {cal.get('caldt', '?')}")
        print(f"      ranges ({cal.get('scalfu', '?')}, {cal.get('scaltu', '?')}): {ranges}")


def _format_sample(sensor, s):
    fu, tu = sensor.force_units, sensor.torque_units
    width = max(len(str(fu)), len(str(tu)))
    lines = [f"{a:>3} = {v:+11.4f} {str(fu if a[0] == 'F' else tu):<{width}}" for a, v in zip(AXES, s.ft)]
    flags = s.flags
    lines.append(f"status 0x{s.status:08X} {'OK' if not flags else ', '.join(flags)}")
    return lines


def cmd_read(sensor, args):
    s = sensor.read()
    print("\n".join(_format_sample(sensor, s)))


def _active_ranges(sensor):
    try:
        cfg = sensor.config_xml()
        ranges = sensor.calibration_xml(cfg.get("cfgcalsel", 0)).get("calmr")
        return ranges if isinstance(ranges, list) and len(ranges) == 6 else None
    except Exception:
        return None


class _Keys:
    """Non-blocking single-key reads from a terminal (no-op if stdin is not a tty)."""

    def __enter__(self):
        self.old = None
        if sys.stdin.isatty():
            import termios
            import tty
            self.old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        return self

    def __exit__(self, *exc):
        if self.old is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old)

    def get(self):
        if self.old is None:
            return None
        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1)
        return None


def monitor_lines(sensor, s, ranges=None, rate=None):
    """Plain-text lines of the live monitor view for sample s (shared with the GUI)."""
    fmt = _format_sample(sensor, s)
    lines = []
    for i, line in enumerate(fmt[:6]):
        if ranges:
            pct = 100.0 * abs(s.ft[i]) / ranges[i]
            bar = "#" * min(20, int(pct / 5))
            line += f"   {pct:5.1f}% |{bar:<20}|"
        lines.append(line)
    lines.append("")
    lines.append(fmt[6])
    rx = f"  rx {rate:7.0f} samples/s" if rate is not None else ""
    lines.append(f"rdt_seq {s.rdt_seq}  ft_seq {s.ft_seq}{rx}  dropped {sensor.dropped}")
    return lines


def cmd_monitor(sensor, args):
    ranges = _active_ranges(sensor)
    period = 1.0 / args.hz
    next_draw = 0.0
    n = 0
    t0 = time.time()
    sys.stdout.write("\x1b[2J")
    try:
        with _Keys() as keys:
            for s in sensor.stream(buffered=args.buffered):
                n += 1
                if s.t_host < next_draw:
                    continue
                next_draw = s.t_host + period
                key = keys.get()
                if key in ("b", "B"):
                    sensor.bias()
                elif key in ("q", "Q"):
                    break
                rate = n / max(s.t_host - t0, 1e-9)
                out = [f"ATI Axia80 @ {sensor.host}    [b] bias   [q] quit", ""]
                out += monitor_lines(sensor, s, ranges, rate)
                sys.stdout.write("\x1b[H" + "\n".join(line + "\x1b[K" for line in out) + "\n")
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass


def cmd_stream(sensor, args):
    sensor.load_scaling()
    fu, tu = sensor.force_units, sensor.torque_units
    header = ["t_host", "rdt_seq", "ft_seq", "status"] + \
             [f"{a}_{fu if a[0] == 'F' else tu}" for a in AXES]
    out = open(args.csv, "w", newline="", buffering=1 << 20) if args.csv != "-" else sys.stdout
    writer = csv.writer(out)
    writer.writerow(header)
    n = 0
    first = last = None
    err_samples = 0
    deadline = time.time() + args.duration if args.duration else None
    next_report = time.time() + 1.0
    try:
        for s in sensor.stream(count=args.count, buffered=args.buffered):
            writer.writerow([f"{s.t_host:.6f}", s.rdt_seq, s.ft_seq, f"0x{s.status:08X}"] +
                            [f"{v:.6f}" for v in s.ft])
            n += 1
            if first is None:
                first = s
            last = s
            if s.status:
                err_samples += 1
            if s.t_host >= next_report:
                next_report = s.t_host + 1.0
                if out is not sys.stdout:
                    print(f"\r{n} samples, {sensor.dropped} dropped", end="", file=sys.stderr, flush=True)
            if deadline and s.t_host >= deadline:
                break
    except KeyboardInterrupt:
        pass
    finally:
        if out is not sys.stdout:
            out.close()
    if n and out is not sys.stdout:
        span = last.t_host - first.t_host
        rate = (n - 1) / span if span > 0 else float("nan")
        print(f"\r\x1b[KWrote {n} samples to {args.csv} over {span:.2f} s ({rate:.0f} samples/s), "
              f"{sensor.dropped} dropped, {err_samples} with nonzero status", file=sys.stderr)


def cmd_bias(sensor, args):
    if args.clear:
        sensor.clear_bias()
        print("Bias cleared")
    else:
        sensor.bias()
        print("Bias set (current load is now zero)")


def cmd_config(sensor, args):
    changed = False
    if args.adc_rate is not None:
        sensor.set_adc_rate(args.adc_rate)
        changed = True
    if args.filter is not None:
        sensor.set_filter(args.filter)
        changed = True
    if args.rdt_buffer is not None:
        sensor.set_rdt_buffer(args.rdt_buffer)
        changed = True
    if args.calibration is not None:
        sensor.select_calibration(args.calibration)
        changed = True
    if not changed:
        print("Nothing to change; current settings:")
    cmd_info(sensor, args)


# The GUI console sets this to a function(question) -> answer string, so commands that ask
# for confirmation (set-ip) can prompt in the console instead of the terminal.
ASK = None


def _confirm(question, assume_yes):
    if assume_yes:
        return True
    if ASK is not None:
        return ASK(f"{question} [y/N]").strip().lower() in ("y", "yes")
    if not sys.stdin.isatty():
        print(f"{question} -> not confirmed (no terminal); pass --yes to proceed", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _suggest_local_address(network, avoid):
    """An address on `network` for the computer to use, avoiding the sensor/gateway addresses."""
    for candidate in (100, 101, 102, 200):
        ip = network.network_address + candidate
        if ip in network and ip not in avoid and ip != network.broadcast_address:
            return ip
    return next(ip for ip in network.hosts() if ip not in avoid)


def _print_reach_hint(ip, network):
    from . import netutil
    reach = netutil.reachable_from(ip)
    if reach:
        print(f"This computer can reach {ip} via {reach[0]} ({reach[1]}).")
        return True
    ifaces = netutil.wired_interfaces()
    iface = ifaces[0] if len(ifaces) == 1 else "<iface>"
    local = _suggest_local_address(network, {ipaddress.IPv4Address(str(ip))})
    print(f"This computer has no address on {network}. To reach the sensor, add one, for example:\n"
          f"  sudo ip addr add {local}/{network.prefixlen} dev {iface}"
          + ("" if iface != "<iface>" else "      (see `ip -br link` for the interface name)"))
    return False


def _update_config(args, new_host):
    from . import netutil
    if not args.cfg_path:
        print(f"No config.yaml found; use --host {new_host} or create config.yaml with `host: {new_host}`.")
        return
    netutil.update_config_host(args.cfg_path, new_host)
    print(f"Updated host in {args.cfg_path} -> {new_host}")
    if os.environ.get("AXIA_HOST"):
        print(f"Note: AXIA_HOST={os.environ['AXIA_HOST']} is set and overrides config.yaml.")


def cmd_set_ip(sensor, args):
    try:
        new = ipaddress.IPv4Interface(f"{args.new_ip}/{args.netmask}")
    except ValueError as e:
        raise ValueError(f"invalid address/netmask: {e}") from None
    net = new.network
    if new.ip in (net.network_address, net.broadcast_address):
        raise ValueError(f"{new.ip} is the network/broadcast address of {net}")
    gateway = ipaddress.IPv4Address(args.gateway) if args.gateway else next(net.hosts())
    if gateway not in net:
        raise ValueError(f"gateway {gateway} is not on {net}")
    from . import netutil
    for name, local in netutil.local_ipv4_networks():
        if local.ip == new.ip:
            raise ValueError(f"{new.ip} is this computer's own address on {name}; pick another")

    current = sensor.network_settings()
    print(f"Sensor at {sensor.host}: MAC {current['mac']}, stored IP {current['ip']}, "
          f"netmask {current['netmask']}, gateway {current['gateway']}, DHCP {current['dhcp']}")
    if args.mac and current["mac"] and _norm_mac(current["mac"]) != _norm_mac(args.mac):
        print(f"WARNING: this sensor's MAC differs from config.yaml ({args.mac}).")
    mode = "DHCP with static fallback" if args.dhcp else "static"
    print(f"New settings: IP {new.ip}, netmask {new.netmask}, gateway {gateway}, mode {mode}")
    if not _confirm("Write these network settings to the sensor?", args.yes):
        print("Aborted; nothing changed.")
        return

    stored = sensor.set_network(str(new.ip), str(new.netmask), str(gateway), dhcp=args.dhcp)
    if str(stored["ip"]) != str(new.ip):
        raise RuntimeError(f"sensor did not store the new IP (reads back {stored['ip']}); "
                           f"set it on the Communications page at http://{sensor.host} instead")
    print(f"Stored on sensor: IP {stored['ip']}, netmask {stored['netmask']}, gateway {stored['gateway']}, "
          f"DHCP {stored['dhcp']}")
    if args.update_config:
        _update_config(args, str(new.ip))

    print("\nPOWER-CYCLE THE SENSOR NOW (switch the 24 V supply off for a few seconds, then on).")
    print("The new address only takes effect at power-up.")
    if args.dhcp:
        print("With DHCP on, the sensor may take ~30 s to fall back to the static address.")
    if not _print_reach_hint(new.ip, net) or args.no_wait:
        print(f"Then check with: python3 -m axia80 --host {new.ip} info")
        return

    waiter = AxiaSensor(str(new.ip), http_port=args.http_port, timeout=1.0)
    deadline = time.time() + args.wait
    print(f"Waiting up to {args.wait:.0f} s for the sensor at {new.ip} (Ctrl-C to stop waiting)...")
    try:
        while time.time() < deadline:
            try:
                mac = waiter.network_settings()["mac"]
                print(f"Sensor is up at {new.ip} (MAC {mac}).")
                return
            except Exception:
                time.sleep(2)
        print(f"No answer at {new.ip} yet. Was it power-cycled? Try `python3 -m axia80 find`.")
    except KeyboardInterrupt:
        print()


def cmd_find(sensor, args):
    from . import netutil
    mac = args.find_mac or args.mac
    if not mac:
        raise ValueError("no MAC address: set `mac:` in config.yaml or pass --mac")
    mac = netutil.normalize_mac(mac)
    print(f"Looking for sensor with MAC {mac}")

    def found(ip, how):
        print(f"\nFound sensor at {ip} ({how}).")
        net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
        _print_reach_hint(ip, net)
        if args.update_config:
            _update_config(args, ip)
        elif str(ip) != args.host:
            print(f"To use it: set `host: {ip}` in config.yaml, or rerun with --update-config.")
        return None

    # 0. configured address
    try:
        probe = AxiaSensor(args.host, http_port=args.http_port, timeout=0.5)
        got = probe.network_settings()["mac"]
        if got and netutil.normalize_mac(got) == mac:
            return found(args.host, "configured address answers")
    except Exception:
        pass
    print(f"  not answering at configured address {args.host}")

    # 1. ARP cache
    for ip, dev in netutil.neigh_lookup(mac):
        return found(ip, f"ARP cache on {dev}")
    print("  not in this computer's ARP cache")

    if not netutil.is_root():
        print("\nThe next steps (ARP sweep, power-up listen) need raw sockets. Rerun as root:\n"
              f"  sudo python3 -m axia80 find")
        raise SystemExit(1)

    ifaces = [args.iface] if args.iface else netutil.wired_interfaces()
    if not ifaces:
        raise ValueError("no wired Ethernet interface is up; plug the sensor in or pass --iface")

    # 2. ARP sweep
    if not args.no_sweep:
        for iface in ifaces:
            networks = []
            local = netutil.iface_ipv4(iface)
            if local and local.network.prefixlen >= 16:
                networks.append(local.network)
            networks += args.range or list(netutil.DEFAULT_SWEEP)
            total = sum(ipaddress.IPv4Network(str(n), strict=False).num_addresses for n in networks)
            print(f"  ARP sweep on {iface}: {', '.join(map(str, networks))} (~{total} addresses)")

            def progress(i, n):
                print(f"\r    {100 * i // max(n, 1):3d}%", end="", file=sys.stderr, flush=True)
            ip = netutil.arp_sweep(mac, iface, networks, progress=progress)
            print("\r\x1b[K", end="", file=sys.stderr)
            if ip:
                return found(ip, f"ARP sweep on {iface}")
        print("  no answer to the ARP sweep")

    # 3. passive listen during power-up
    if args.listen > 0:
        where = args.iface or "all interfaces"
        print(f"\nPOWER-CYCLE THE SENSOR NOW (24 V off for a few seconds, then on).\n"
              f"Listening on {where} for {args.listen:.0f} s...")
        try:
            ip = netutil.listen_for(mac, args.listen, args.iface)
        except KeyboardInterrupt:
            ip = None
        if ip:
            return found(ip, "announced itself at power-up")
    print("\nSensor not found. Check that it is powered (status LED lit) and the Ethernet link LED is on.\n"
          "Try a wider --range (e.g. 10.0.0.0/16), or ATI's Windows discovery tool.")
    raise SystemExit(1)


def _apply_launch_settings(sensor, cfg):
    """Writes the sensor: settings from config.yaml and optionally biases."""
    settings = cfg["sensor"]
    if settings["adc_rate"] is not None:
        sensor.set_adc_rate(int(settings["adc_rate"]))
    if settings["filter"] is not None:
        sensor.set_filter(int(settings["filter"]))
    if settings["rdt_buffer"] is not None:
        sensor.set_rdt_buffer(int(settings["rdt_buffer"]))
    if settings["calibration"] is not None:
        sensor.select_calibration(int(settings["calibration"]))
    if cfg["bias_on_start"]:
        sensor.bias()
        time.sleep(0.1)   # let the bias take effect before the first sample


def _resolve(args, cfg):
    """Fills options not given on the command line from config.yaml (flags win)."""
    def pick(name, value):
        if getattr(args, name, None) is None:
            setattr(args, name, value)

    pick("host", os.environ.get("AXIA_HOST") or cfg["host"])
    pick("rdt_port", cfg["rdt_port"])
    pick("http_port", cfg["http_port"])
    pick("timeout", cfg["timeout"])
    args.host = str(args.host)
    args.mac = cfg["mac"]
    if args.cmd == "monitor":
        pick("hz", cfg["monitor"]["hz"])
        pick("buffered", cfg["monitor"]["buffered"])
    elif args.cmd == "stream":
        pick("csv", cfg["stream"]["csv"])
        pick("duration", cfg["stream"]["duration"])
        pick("count", cfg["stream"]["count"])
        pick("buffered", cfg["stream"]["buffered"])
        if not args.csv:
            raise SystemExit("error: stream needs an output file: --csv FILE or stream.csv in config.yaml")
        args.csv = str(args.csv)


HANDLERS = {"info": cmd_info, "read": cmd_read, "monitor": cmd_monitor, "stream": cmd_stream,
            "bias": cmd_bias, "config": cmd_config, "set-ip": cmd_set_ip, "find": cmd_find}


def build_parser(prog="axia80"):
    ap = argparse.ArgumentParser(
        prog=prog, description="ATI Ethernet Axia80 F/T sensor tool. "
        "Run without a command to open the graphical console.")
    ap.add_argument("--config", help="config file (default: ./config.yaml, then the project's config.yaml)")
    ap.add_argument("--host", help="sensor IP (default: $AXIA_HOST, then config.yaml, then 192.168.1.1)")
    ap.add_argument("--rdt-port", type=int)
    ap.add_argument("--http-port", type=int)
    ap.add_argument("--timeout", type=float)
    sub = ap.add_subparsers(dest="cmd", metavar="command")

    sub.add_parser("info", help="show sensor identity, calibration and settings")
    sub.add_parser("read", help="read a single sample")

    m = sub.add_parser("monitor", help="live display ([b] bias, [q] quit)")
    m.add_argument("--hz", type=float, help="display refresh rate")
    m.add_argument("--buffered", action=argparse.BooleanOptionalAction,
                   help="use RDT buffered (multi-record) packets")

    s = sub.add_parser("stream", help="stream samples to CSV")
    s.add_argument("--csv", help="output file ('-' for stdout)")
    s.add_argument("--duration", type=float, help="seconds to record (0 = until Ctrl-C)")
    s.add_argument("--count", type=int, help="number of samples (0 = unlimited)")
    s.add_argument("--buffered", action=argparse.BooleanOptionalAction,
                   help="RDT buffered mode: several records per packet, recommended above ~2 kHz")

    b = sub.add_parser("bias", help="tare the sensor (or --clear)")
    b.add_argument("--clear", action="store_true", help="remove the bias instead")

    c = sub.add_parser("config", help="change sensor settings")
    c.add_argument("--adc-rate", type=int, choices=sorted(p.ADC_RATES), help="internal sample rate, Hz")
    c.add_argument("--filter", type=int, choices=range(9), help="low-pass filter 0 (off) .. 8 (lowest cutoff)")
    c.add_argument("--rdt-buffer", type=int, help="records per packet in buffered mode, 1..40")
    c.add_argument("--calibration", type=int, choices=(0, 1), help="0 = full range, 1 = half range")

    si = sub.add_parser("set-ip", help="change the sensor's IP address (takes effect after a power cycle)")
    si.add_argument("new_ip", help="new static IP address for the sensor, e.g. 192.168.0.3")
    si.add_argument("--netmask", default="255.255.255.0", help="subnet mask (default 255.255.255.0)")
    si.add_argument("--gateway", help="default gateway (default: first address of the subnet)")
    si.add_argument("--dhcp", action="store_true",
                    help="leave DHCP enabled (sensor uses the static IP only if no DHCP server answers)")
    si.add_argument("--no-update-config", dest="update_config", action="store_false",
                    help="don't write the new address into config.yaml")
    si.add_argument("--no-wait", action="store_true", help="don't wait for the sensor to come back up")
    si.add_argument("--wait", type=float, default=90, help="seconds to wait after power-cycle (default 90)")
    si.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    fd = sub.add_parser("find", help="locate the sensor on the network by its MAC address")
    fd.add_argument("--mac", dest="find_mac", help="MAC to look for (default: mac in config.yaml)")
    fd.add_argument("--iface", help="wired interface to search (default: all wired interfaces that are up)")
    fd.add_argument("--range", action="append", metavar="CIDR",
                    help="subnet to ARP-sweep, repeatable (default 192.168.0.0/16 plus the interface's own)")
    fd.add_argument("--no-sweep", action="store_true", help="skip the ARP sweep")
    fd.add_argument("--listen", type=float, default=60,
                    help="seconds to listen for the sensor's power-up announcement (0 = skip; default 60)")
    fd.add_argument("--update-config", action="store_true", help="write the found address into config.yaml")
    return ap


def _launch_gui(args, cfg, cfg_path, argv):
    """Starts the graphical console, switching to the project's .venv if Qt isn't importable here."""
    try:
        from . import gui
    except ImportError as e:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        venv_python = os.path.join(root, ".venv", "bin", "python")
        if os.path.exists(venv_python) and os.path.realpath(sys.prefix) != os.path.realpath(
                os.path.join(root, ".venv")):
            env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""))
            os.execve(venv_python, [venv_python, "-m", "axia80", *argv], env)
        print(f"error: the graphical console needs extra packages ({e.name}).\n"
              f"Create the venv once:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n"
              f"(the command-line subcommands work without it; see python3 -m axia80 --help)", file=sys.stderr)
        return 1
    return gui.run(args, cfg, cfg_path)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        cfg, cfg_path = load_config(args.config)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    _resolve(args, cfg)
    args.cfg_path = cfg_path

    if args.cmd is None:
        return _launch_gui(args, cfg, cfg_path, argv)
    return run_command(args, cfg, cfg_path)


def run_command(args, cfg, cfg_path, sensor=None, apply_launch=True):
    """Runs one parsed subcommand.

    The GUI passes its shared sensor instead of opening a new one, and apply_launch=False
    because it applies config.yaml's launch settings once, when streaming first starts.
    """
    own = sensor is None
    if own:
        sensor = AxiaSensor(args.host, rdt_port=args.rdt_port, http_port=args.http_port, timeout=args.timeout)
    try:
        if apply_launch and args.cmd in ("read", "monitor", "stream"):
            _apply_launch_settings(sensor, cfg)
        HANDLERS[args.cmd](sensor, args)
    except (TimeoutError, OSError) as e:
        where = f" (settings from {cfg_path})" if cfg_path else ""
        print(f"error: {e}\nIs the sensor powered (24 V) and reachable at {args.host}{where}? "
              f"Try: ping {args.host}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        if own:
            sensor.close()
    return 0
