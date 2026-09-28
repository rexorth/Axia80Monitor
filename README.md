# Axia80Monitor: interface for the ATI NET-Axia80-M8

Host software for the ATI **Ethernet Axia80-M8** 6-axis force/torque sensor (P/N 9105-NET-Axia80-M8).

This unit's details:

| Item | Value |
|---|---|
| MAC address | `00:16:BD:00:4D:EC` |
| Calibration serials | FT54714 and FT54715 (the two factory calibration ranges) |

There are two ways to use it:

- **The graphical console** (`python3 -m axia80`) needs three packages in a local venv:

  ```bash
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  ```

  After that, plain `python3 -m axia80` finds the venv by itself, so you don't need to activate it.
- **The command-line subcommands** (`python3 -m axia80 info`, `stream`, and so on) use only the Python standard library and need nothing installed.

All sensor facts below come from ATI manual *9610-05-Ethernet Axia-10*:
[PDF](https://www.ati-ia.com/app_content/documents/9610-05-Ethernet%20Axia.pdf).

## What the sensor needs

| | Requirement |
|---|---|
| Power | **12–30 V DC, 24 V nominal, 1.5 W max** (about 63 mA at 24 V). Reverse-polarity protected. |
| Interface | 100BASE-TX Ethernet (a 4-wire twisted pair) on the same 6-pin M8 connector as power |
| Protocols | UDP "RDT" streaming (port 49152, up to 7912 Hz), TCP (port 49151), HTTP web pages, XML and CGI (port 80) |
| Default network | DHCP on; falls back to the static IP **192.168.1.1** when no DHCP server answers |
| Ranges (M8) | Calibration 0: Fxy 150 N, Fz 470 N, Txyz 8 N·m. Calibration 1: Fxy 75 N, Fz 235 N, Txyz 4 N·m |

**Host:** any Raspberry Pi with Ethernet works. A Pi 4 or Pi 5 is recommended for full-rate logging.

- The sensor uses Ethernet, not a serial bus, so an Arduino would need a W5500 Ethernet shield. It would also struggle to keep up with kHz rates.
- The Pi **cannot** power the sensor. Use the lab bench supply.

## Wiring

M8 6-pin male connector on the sensor:

| M8 pin | Signal | Goes to |
|---|---|---|
| 1 | TX+ | Ethernet pair A (RJ45 pin 1) |
| 2 | TX− | Ethernet pair A (RJ45 pin 2) |
| 3 | RX+ | Ethernet pair B (RJ45 pin 3) |
| 4 | RX− | Ethernet pair B (RJ45 pin 6) |
| 5 | V+ | Lab supply + (24 V) |
| 6 | V− | Lab supply − (0 V) |

**Option A: ATI cables.** The **9105-C-ZC22-ZC28** cable goes from the sensor's M8 to an M12 8-pin connector. The **9105-C-ZC28-U-RJ45S** cable goes from that M12 to an RJ45 plug plus bare power leads.

Plug the RJ45 into the computer's Ethernet port and connect the bare leads to the lab supply.

#### Cable colours (ATI drawing 9230-05-1555)

Flying leads (the unterminated power branch):

| Wire colour | M12 pin | Signal | Connect to |
|---|---|---|---|
| **Brown** | 2 | V+ | **Lab supply + (24 V)** |
| **Brown/white** (white with a brown stripe) | 3 | V− | **Lab supply − (0 V)** |
| Blue | 7 | Sync TP1− | Nothing. This sync pair is unused with the 6-pin Axia cable; insulate the end. |
| White/blue | 1 | Sync TP1+ | Nothing; insulate the end. |
| Bare braid | shell | Shield | Supply ground or earth terminal (optional, reduces noise) |

RJ45 branch (already terminated, listed for reference or re-termination):

| Wire colour | M12 pin | Signal | RJ45 pin |
|---|---|---|---|
| White/orange | 6 | TX+ | 1 |
| Orange | 4 | TX− | 2 |
| White/green | 5 | RX+ | 3 |
| Green | 8 | RX− | 6 |
| Braid | shell | Shield | RJ45 shield |

Before powering up, confirm the brown and brown/white leads with a continuity check to M12 pins 2 and 3.

- A wrong guess can't damage the sensor, because the input is reverse-polarity protected; the sensor just won't turn on.
- The sync leads aren't connected to anything, but keep them insulated anyway.
- The power leads never reach the RJ45, and Ethernet is transformer-isolated at both ends, so 24 V can't reach the computer.

**Option B: home-made adapter.** Use an M8 A-coded 6-pin female cable and a cut Ethernet patch cable.

- Wire it as in the table above, keeping TX± on one twisted pair and RX± on another.
- The Pi 4/5 Ethernet port has auto-MDIX, so it doesn't matter which pair lands on RJ45 1/2 and which on 3/6.
- Ground the cable shield.

**Lab supply:** set **24.0 V with a 0.15 A current limit** *before* connecting the sensor.

- Idle draw should be about 60 mA.
- If the supply hits the current limit, check the wiring.

## Raspberry Pi network setup (direct cable)

Give the Pi a static address on the sensor's subnet. This is done once and persists.

```bash
sudo nmcli con add type ethernet ifname eth0 con-name axia ipv4.method manual ipv4.addresses 192.168.1.100/24
sudo nmcli con up axia
ping 192.168.1.1
```

The sensor's own web interface is at http://192.168.1.1.

If the sensor isn't at 192.168.1.1, it probably got an address from a DHCP server. Find it by its MAC address with any of these:

- `ip neigh | grep -i 00:18:bd:00:4d:ec`
- `sudo arp-scan -l`
- the router or DHCP server's lease table

Then set that address as `host:` in `config.yaml`, or pass it with `--host` or `AXIA_HOST`. `sudo python3 -m axia80 find` automates this search; see [Changing or finding the sensor's IP address](#changing-or-finding-the-sensors-ip-address).

## Graphical console

```bash
python3 -m axia80                  # opens the console window (uses config.yaml)
python3 -m axia80 --host 10.0.0.5  # any global option still works
```

The window is split into four resizable quadrants:

| | Left | Right |
|---|---|---|
| **Top** | Torque plot (Tx, Ty, Tz) | Force plot (Fx, Fy, Fz) |
| **Bottom** | Live readout: the `monitor` view, with values, % of range, status, sample rate and drops | Console: everything the commands print, with an `axia80>` command line underneath |

- Which plot sits on which side is set with `gui.top_left` and `gui.top_right` in `config.yaml`, or live with `plot swap`.
- When every channel of one plot is turned off, the other plot fills the whole top half.

Streaming starts automatically when the window opens (`gui.autostart`). Type commands into the command line:

| Command | Does |
|---|---|
| `start` / `stop` | Start or stop streaming |
| `record FILE.csv [SECONDS]` | Record the live stream to CSV, in the same format as `stream`. `record stop` ends it; `record` alone shows progress. |
| `plot` | Show the channel menu, with `[x]` marking the channels that are on |
| `plot fz off`, `plot 1 2`, `plot torque off`, `plot all` | Turn channels on or off. Channels can be given as `fx`…`tz`, as numbers 1–6, or as `force`, `torque`, `all` or `none`. Without `on` or `off`, each channel toggles. |
| `plot swap`, `plot left force` | Change which plot is on which side |
| `window SECONDS` | Plot history length, 0.5–60 s |
| `pause` / `resume` | Freeze the plots so you can zoom or pan with the mouse. Streaming and recording continue. |
| `connect [IP]` | Reconnect, optionally to a different sensor address |
| `info`, `read`, `bias [--clear]`, `config …`, `set-ip …`, `find …` | The command-line commands, unchanged. Add `-h` for options. |
| `stream --csv FILE …` | Same as `record` |
| `help`, `clear`, `quit` | Show help, clear the console, close the window |

The command line has history (Up/Down) and completes command names (Tab). `set-ip` asks for confirmation in the console; type `y` or `n`.

**Performance:**

- Samples go into a 60 s ring buffer on a background thread.
- The plots redraw at `gui.refresh_hz` (30 Hz by default). Each redraw first reduces the data to a min/max pair per screen pixel, so short spikes stay visible.
- At the sensor's full 7.9 kHz this used about 25–35% of one CPU core with no dropped samples (measured with the simulator).
- `gui.buffered: true` (the default) gives the lowest CPU use. It takes effect when the sensor's RDT buffer size is above 1: `config --rdt-buffer 40`.

GUI options in `config.yaml`:

| Option | Default | Meaning |
|---|---|---|
| `gui.top_left`, `gui.top_right` | `torque`, `force` | Plot in each top quadrant: `torque`, `force` or `none` |
| `gui.channels` | `Fx Fy Fz Tx Ty Tz` | Channels plotted at startup |
| `gui.window` | `10` | Seconds of history shown |
| `gui.refresh_hz` | `30` | Redraw rate |
| `gui.autostart` | `true` | Start streaming when the window opens |
| `gui.buffered` | `true` | Use buffered RDT packets |

## Usage

Command-line subcommands, useful for scripts or when there's no display:

```bash
python3 -m axia80 info                                   # identity, calibration, units, rates, status
python3 -m axia80 read                                   # one sample
python3 -m axia80 monitor                                # live display: [b] = bias/tare, [q] = quit
python3 -m axia80 bias                                   # tare (bias --clear to undo)
python3 -m axia80 stream --csv run1.csv --duration 10    # log 10 s to CSV (Ctrl-C to stop early)
python3 -m axia80 stream --csv run1.csv --buffered       # buffered mode, recommended above ~2 kHz
python3 -m axia80 config --adc-rate 1000 --filter 3      # change sensor settings
python3 -m axia80 config --rdt-buffer 40 --calibration 1
python3 -m axia80 set-ip 192.168.0.3                     # change the sensor's IP (then power-cycle it)
sudo python3 -m axia80 find                              # locate the sensor by MAC if its IP is unknown
```

Global options:

- `--config FILE` chooses the config file (see [Configuration](#configuration-configyaml)).
- `--host IP` sets the sensor address.
- `--timeout S` sets the network timeout in seconds.

**CSV columns:** `t_host, rdt_seq, ft_seq, status, Fx, Fy, Fz, Tx, Ty, Tz`.

- The units are part of the header names, for example `Fx_N` and `Tx_Nm`.
- `ft_seq` is the sensor's own sample counter, so `Δft_seq / ADC rate` gives precise sensor time.
- A gap in `rdt_seq` means a packet was lost. The tool counts these and reports them as "dropped".

**Rates.** The ADC rate (`--adc-rate` 500/1000/2000/4000/8000) is how fast the sensor samples internally. The RDT output rate is how fast it sends samples.

- The RDT output rate can only be set on the sensor's **Communications** web page (http://192.168.1.1, then Communications).
- In `--buffered` mode each UDP packet carries `--rdt-buffer` records (1–40). At 7.9 kHz this keeps the Pi's packet load around 200 packets/s.
- `config --filter` sets the low-pass filter: 0 = off, and 1–8 give progressively lower cutoffs (manual section 4.5).

**As a library:**

```python
from axia80 import AxiaSensor
with AxiaSensor("192.168.1.1") as s:
    s.bias()
    print(s.read().ft)                    # (Fx, Fy, Fz, Tx, Ty, Tz) in N / N·m
    for sample in s.stream(count=1000, buffered=True):
        ...
```

## Changing or finding the sensor's IP address

The sensor stores its network settings in internal memory and only loads them at power-up. A new address therefore **takes effect after a power cycle and then persists** until you change it again.

The manual describes no reset button or factory reset. If the address is forgotten, find the sensor by its MAC address, which never changes, using `find` below.

### `set-ip`: change the address

```bash
python3 -m axia80 set-ip 192.168.0.3          # static IP, netmask 255.255.255.0, gateway 192.168.0.1
python3 -m axia80 set-ip 10.0.5.20 --netmask 255.255.0.0 --gateway 10.0.0.1
```

It connects to the sensor at its current address (`host:` in `config.yaml`, or `--host`) and then:

1. Shows the current and new settings and asks for confirmation. Pass `--yes` to skip the question.
2. Writes the new settings to the sensor and reads them back to check they were stored.
3. Updates `host:` in `config.yaml`, so the tool and the sensor stay in sync. Pass `--no-update-config` to skip this.
4. Tells you to **power-cycle the sensor**. If this computer has an address on the new subnet, it then waits up to 90 s and confirms the sensor answers at the new address. Otherwise it prints the `ip addr add` command to give the computer one.

DHCP is turned **off** by default, so the sensor always uses the static address. With `--dhcp` it stays on: the sensor takes an address from a DHCP server if one answers, and falls back to the static address after about 30 s.

To reach the sensor at its current address from a laptop on a different subnet, first add a temporary second address to the laptop's Ethernet port:

```bash
sudo ip addr add 192.168.1.100/24 dev <iface>
```

### `find`: locate the sensor by MAC

```bash
python3 -m axia80 find                       # quick checks, no root needed
sudo python3 -m axia80 find                  # full search
sudo python3 -m axia80 find --update-config  # also write the found address into config.yaml
```

`find` looks for the `mac:` from `config.yaml` (or `--mac`). It tries these in order and stops at the first hit:

1. The configured `host` address.
2. This computer's ARP cache.
3. *(root)* An **ARP sweep** of 192.168.0.0/16 plus each wired interface's own subnet. The sensor answers even when this computer is on a different subnet. Add other ranges with `--range 10.0.0.0/16` (repeatable), and pick an interface with `--iface`.
4. *(root)* A **power-up listen**. The tool asks you to power-cycle the sensor, then listens for up to 60 s (`--listen`) for the packets it sends when it boots.

It needs no extra packages; the raw-socket steps use Linux `AF_PACKET`. If `find` fails, ATI's Windows discovery tool (manual section 5.3) is the fallback.

## Configuration (`config.yaml`)

Launch options live in [config.yaml](config.yaml), so you don't have to type them each time. Edit it to change the sensor's IP address or any command default.

The file is looked for in this order:

1. The path given with `--config FILE`
2. `./config.yaml` in the current directory
3. The project's `config.yaml`

Each setting is taken from the first of these that provides it:

1. A command-line flag
2. The `AXIA_HOST` environment variable (host only)
3. `config.yaml`
4. The built-in default

| Option | Default | Meaning |
|---|---|---|
| `host` | `192.168.1.1` | Sensor IP address |
| `mac` | `00:16:BD:00:4D:EC` | Expected MAC address. `info` warns if the connected sensor reports a different one. |
| `rdt_port`, `http_port` | `49152`, `80` | Sensor ports. Only change these if they were changed on the sensor. |
| `timeout` | `1.0` | Seconds to wait for the sensor |
| `sensor.adc_rate` | empty | Internal sample rate: 500, 1000, 2000, 4000 or 8000 Hz |
| `sensor.filter` | empty | Low-pass filter: 0 = off, 1–8 = lower cutoffs |
| `sensor.rdt_buffer` | empty | Records per packet in buffered mode, 1–40 |
| `sensor.calibration` | empty | 0 = full range, 1 = half range |
| `bias_on_start` | `false` | Tare the sensor before `read`, `monitor` and `stream` |
| `monitor.hz`, `monitor.buffered` | `20`, `false` | Monitor refresh rate and packet mode |
| `stream.csv`, `.duration`, `.count`, `.buffered` | empty, `0`, `0`, `false` | Stream defaults. With `stream.csv` set, a bare `stream` command works. |

Notes on the `sensor:` settings:

- They are written to the sensor at the start of `read`, `monitor` and `stream`. The sensor saves them, so they persist across power cycles.
- Leave them empty to keep whatever the sensor already has.
- `config` on the command line changes the same settings once, without editing the file.

Booleans set in the file can be turned off for one run with `--no-buffered`.

If PyYAML is installed (Debian package `python3-yaml`) it is used to read the file. Otherwise a small built-in parser handles the file's simple format, so nothing needs to be installed.

## Bring-up checklist

1. With the supply off, wire the sensor. Then power it at 24 V, 0.15 A limit.
   - The LEDs run a self-test, then STATUS turns **green**.
   - Red means an error in the status word.
   - Red/green (orange) means an axis is out of range.
2. Connect the Pi, then run `ping 192.168.1.1`. The L/A (link/activity) LED should flicker.
3. `python3 -m axia80 info`: expect MAC `00:18:BD:00:4D:EC`, calibrations FT54714 and FT54715, units N / Nm, and status OK.
4. `python3 -m axia80 read`, then `bias`, then `read`: every axis should be close to 0 after the bias.
5. Put a known mass *m* on the tool side. |Fz| should be about *m* × 9.81 N.
6. `python3 -m axia80 stream --csv test.csv --duration 10 --buffered`: the sample count should be about the output rate × 10, with about 0 dropped.

## Troubleshooting

| Symptom | Check |
|---|---|
| `error: timed out` / no ping | Supply on and set to 24 V? Link LED lit? Pi on 192.168.1.x/24? Sensor may have taken a DHCP address; find it by MAC |
| Status "supply voltage out of range" | Supply outside 12–30 V, or a large voltage drop on long leads |
| Drops at high rates | Use `--buffered`, a direct cable (no switch or Wi-Fi bridge), and a Pi 4/5 |
| Only one program gets data | RDT serves one client at a time; the most recent requester wins |

## Testing without hardware

`tools/fake_axia.py` simulates the sensor's UDP and HTTP interfaces.

```bash
.venv/bin/python -m unittest discover -s tests  # all tests (plain python3 skips the GUI data tests)
python3 tools/fake_axia.py --http-port 8080 &  # run the simulator
python3 -m axia80 --host 127.0.0.1 --http-port 8080 monitor
```

## Layout

| Path | Contents |
|---|---|
| `axia80/protocol.py` | Wire formats: RDT, TCP, status bits, unit codes |
| `axia80/sensor.py` | `AxiaSensor` client: HTTP config, UDP streaming, bias, TCP fallback |
| `axia80/__main__.py` | Entry point for `python3 -m axia80` |
| `axia80/cli.py` | Command-line subcommands, and GUI launch when no command is given |
| `axia80/gui.py` | Graphical console: Qt window, pyqtgraph plots, stream and command threads |
| `axia80/config.py` | Loads `config.yaml` |
| `axia80/netutil.py` | Network helpers for `find` and `set-ip`: ARP cache, ARP sweep, power-up listen, `config.yaml` host update |
| `config.yaml` | Launch options (IP address, MAC, sensor settings, command and GUI defaults) |
| `requirements.txt` | GUI packages for `.venv`: numpy, pyqtgraph, PySide6-Essentials |
| `tools/fake_axia.py` | Sensor simulator |
| `tests/` | Unit tests |

## Not yet verified on the real sensor

The code follows the manual. These points are the ones to check during bring-up:

- **The exact XML element layout.** Array values are parsed as `;`- or `,`-separated.
- **The `setadcrate` value for 8 kHz.** The manual's table says 7912, but its CGI example says 7812, which is what the code sends.
- **The RDT output-rate CGI variable.** It isn't documented, so the rate is set through the web page instead.
- **`set-ip` and the DHCP flag.** The manual's table says `comnetdhcp=1` means static and `0` means DHCP, and the code follows it. After the power cycle, check on the Communications page that DHCP shows as expected. If DHCP is left on by mistake, the sensor still falls back to the static address after about 30 s.
- **The ARP sweep and power-up listen in `find`.** These need root and have only been unit-tested, not run against the real sensor.
