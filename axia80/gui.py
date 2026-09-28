"""Graphical console for the ATI Axia80: python3 -m axia80 (no command).

Layout (quadrants, all resizable with the splitters):

    +-----------------------+-----------------------+
    |  torque plot          |  force plot           |   <- configurable (gui.top_left / top_right);
    |                       |                       |      one plot fills the top if the other is empty
    +-----------------------+-----------------------+
    |  live readout         |  console output       |
    |  (monitor view)       |  axia80> command line |
    +-----------------------+-----------------------+

Threads:
  - GUI thread: widgets, console input, redraw timer (gui.refresh_hz).
  - Stream thread: owns the RDT stream, writes samples into a ring buffer and the CSV recorder.
  - Command thread: runs the existing CLI commands (info, bias, config, set-ip, find, ...) one at
    a time so slow network calls never freeze the window.
Plotting uses pyqtgraph on Qt, which draws numpy arrays directly. Each redraw reduces the
window to min/max pairs per screen pixel first (peak_decimate), so full-rate 7.9 kHz data
redraws with low overhead and latency without hiding spikes.
"""

import collections
import csv
import math
import os
import queue
import re
import shlex
import sys
import threading
import time

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from . import cli
from .sensor import AXES, AxiaSensor

MAX_RATE = 8000            # Hz, the sensor's top output rate
MAX_WINDOW = 60.0          # s, longest plot history
AXIS_COLORS = {"x": "#ff5c5c", "y": "#4cd964", "z": "#4da3ff"}
GROUPS = {"force": ("Fx", "Fy", "Fz"), "torque": ("Tx", "Ty", "Tz")}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\r")

HELP = """\
Streaming and display
  start [--no-buffered]      start streaming (auto-starts if gui.autostart is true)
  stop                       stop streaming
  record FILE.csv [SECONDS]  record to CSV (record stop / record = status). The plots then show
                             only the recording, scaled to fit the whole test.
  live                       after a recording, return the plots to the rolling live window
  plot                       show the plot channel menu
  plot CH... [on|off]        toggle or set channels: fx fy fz tx ty tz, 1-6, force, torque, all, none
  plot swap                  swap the two plots; plot left|right torque|force|none
  window SECONDS             plot history length (0.5-60 s)
  pause / resume             freeze or resume the plots (streaming and recording continue)
  connect [IP]               reconnect, optionally to a different sensor address
  clear                      clear this console
  quit                       close the window
Sensor commands (same as the command line; add -h for options)
  info  read  bias [--clear]  config [...]  set-ip IP [...]  find [...]
  stream --csv FILE [--duration S] [--count N]   same as record
  Up/Down browse history, Tab completes command names."""


# --- data ---------------------------------------------------------------------------

class RingBuffer:
    """Fixed-size time series of t and Fx..Tz; written by the stream thread, read by the GUI.

    Channels are stored as rows (6 x capacity) so per-channel reductions run over contiguous memory.
    """

    def __init__(self, capacity):
        self.t = np.zeros(capacity)
        self.y = np.zeros((6, capacity))
        self.cap = capacity
        self.total = 0
        self.lock = threading.Lock()

    def clear(self):
        with self.lock:
            self.total = 0

    def extend(self, t, y):
        """t: (n,), y: (n, 6) as produced by the stream thread."""
        n = len(t)
        if n == 0:
            return
        if n > self.cap:
            t, y, n = t[-self.cap:], y[-self.cap:], self.cap
        y = y.T
        with self.lock:
            i = self.total % self.cap
            first = min(n, self.cap - i)
            self.t[i:i + first] = t[:first]
            self.y[:, i:i + first] = y[:, :first]
            if first < n:
                self.t[:n - first] = t[first:]
                self.y[:, :n - first] = y[:, first:]
            self.total += n

    def last(self, seconds, rows=range(6)):
        """Copies of t (n,) and the requested channel rows (len(rows), n) for the last `seconds`."""
        rows = list(rows)                       # list index => numpy returns copies
        with self.lock:
            n = min(self.total, self.cap)
            if n == 0:
                return np.empty(0), np.empty((len(rows), 0))
            end = self.total % self.cap         # one past the newest sample
            if n < self.cap or end == 0:        # data is contiguous in [0, n)
                start = np.searchsorted(self.t[:n], self.t[n - 1] - seconds)
                return self.t[start:n].copy(), self.y[rows, start:n]
            cutoff = self.t[end - 1] - seconds  # wrapped: oldest [end:cap], newest [0:end]
            if self.t[0] <= cutoff:             # window lies entirely in [0:end)
                start = np.searchsorted(self.t[:end], cutoff)
                return self.t[start:end].copy(), self.y[rows, start:end]
            start = end + np.searchsorted(self.t[end:], cutoff)
            t = np.concatenate((self.t[start:], self.t[:end]))
            y = np.concatenate((self.y[rows, start:], self.y[rows, :end]), axis=1)
            return t, y


def peak_decimate(x, y, buckets):
    """Reduces x (n,) and channel rows y (k, n) to 2 points per bucket (min and max).

    Drawing ~2 points per screen pixel looks identical to drawing every sample but costs a
    fraction of the time; at 7.9 kHz x 10 s that is ~80k samples down to ~2k per curve,
    and the min/max pairs keep short spikes visible.
    """
    n = len(x)
    if buckets < 1 or n <= 2 * buckets:
        return x, y
    step = n // buckets
    start = n - step * buckets                   # buckets align to the newest sample; the
    xs = x[start:].reshape(buckets, step)        # < `buckets` oldest leftovers stay as raw points
    ys = y[:, start:].reshape(y.shape[0], buckets, step)
    xo = np.empty(start + 2 * buckets)
    yo = np.empty((y.shape[0], start + 2 * buckets))
    xo[:start], yo[:, :start] = x[:start], y[:, :start]
    xo[start::2], xo[start + 1::2] = xs[:, 0], xs[:, -1]
    yo[:, start::2], yo[:, start + 1::2] = ys.min(axis=2), ys.max(axis=2)
    return xo, yo


def y_limits(ymin, ymax, min_span, pad=0.05):
    """Y range around the data, never narrower than min_span (so noise isn't magnified)."""
    span = max(ymax - ymin, min_span)
    half = span * (1 + 2 * pad) / 2
    mid = (ymax + ymin) / 2
    return mid - half, mid + half


def next_y_range(current, ymin, ymax, min_span, pad=0.05):
    """Y range for the next frame, holding still while the data fits in a minimum-span window.

    - Data fits in min_span (with padding) and inside the current fixed window: keep it,
      so the trace moves inside a still axis instead of the axis re-centring every frame.
    - Data fits in min_span but left the window: re-centre a min_span window on it, once.
    - Data needs more than min_span: autoscale to the data (+ padding) every frame.
    """
    if (ymax - ymin) * (1 + 2 * pad) <= min_span:
        if current is not None:
            lo, hi = current
            fixed = math.isclose(hi - lo, min_span, rel_tol=1e-9, abs_tol=1e-12)
            if fixed and lo <= ymin and ymax <= hi:
                return current
        mid = (ymax + ymin) / 2
        return mid - min_span / 2, mid + min_span / 2
    return y_limits(ymin, ymax, min_span, pad)


class TestHistory:
    """Every sample of the current recording, for display; memory stays bounded.

    Recordings can outlast the live ring buffer, so they get their own store. It holds
    full-rate data up to `limit` samples (~50 s at 7.9 kHz); when full, it compacts itself
    to min/max pairs at half the size, so older data gets coarser but spikes stay visible.
    Times are seconds since the first recorded sample.
    """

    def __init__(self, limit=400_000):
        self.limit = limit
        self.t = np.empty(limit)
        self.y = np.empty((6, limit))
        self.n = 0
        self.t0 = None
        self.lock = threading.Lock()

    def extend(self, t, y):
        """t: (n,), y: (n, 6) in stream-thread order."""
        if len(t) == 0:
            return
        with self.lock:
            if self.t0 is None:
                self.t0 = t[0]
            t, y = t - self.t0, y.T
            while len(t):
                if self.n == self.limit:
                    self._compact()
                k = min(len(t), self.limit - self.n)
                self.t[self.n:self.n + k] = t[:k]
                self.y[:, self.n:self.n + k] = y[:, :k]
                self.n += k
                t, y = t[k:], y[:, k:]

    def _compact(self):
        x, y = peak_decimate(self.t[:self.n], self.y[:, :self.n], self.limit // 4)
        m = len(x)                                # <= limit/4 raw leftovers + limit/2 min/max points
        self.t[:m] = x
        self.y[:, :m] = y
        self.n = m

    def duration(self):
        with self.lock:
            return float(self.t[self.n - 1]) if self.n else 0.0

    def decimated(self, rows, buckets):
        """(x, y[len(rows), m]) reduced to ~2 points per bucket; copies, safe to keep."""
        with self.lock:
            if self.n == 0:
                return np.empty(0), np.empty((len(rows), 0))
            x, y = peak_decimate(self.t[:self.n], self.y[list(rows), :self.n], buckets)
            return x.copy(), y


class Recorder:
    """CSV writer fed from the stream thread (same columns as `stream --csv`)."""

    def __init__(self, path, sensor, duration=0.0, count=0):
        self.path = path
        self.duration = duration
        self.count = count
        self.n = self.errors = 0
        self.t_first = self.t_last = None
        self.stop_requested = False
        self.file = open(path, "w", newline="", buffering=1 << 20)
        self.writer = csv.writer(self.file)
        fu, tu = sensor.force_units, sensor.torque_units
        self.writer.writerow(["t_host", "rdt_seq", "ft_seq", "status"] +
                             [f"{a}_{fu if a[0] == 'F' else tu}" for a in AXES])

    def write(self, s):
        """Returns False once the recording is complete."""
        if self.stop_requested:
            return False
        self.writer.writerow([f"{s.t_host:.6f}", s.rdt_seq, s.ft_seq, f"0x{s.status:08X}"] +
                             [f"{v:.6f}" for v in s.ft])
        self.n += 1
        self.errors += 1 if s.status else 0
        if self.t_first is None:
            self.t_first = s.t_host
        self.t_last = s.t_host
        if self.count and self.n >= self.count:
            return False
        if self.duration and s.t_host - self.t_first >= self.duration:
            return False
        return True

    def elapsed(self):
        return 0.0 if self.t_first is None else self.t_last - self.t_first

    def close(self, dropped):
        self.file.close()
        span = self.elapsed()
        rate = (self.n - 1) / span if span > 0 else 0.0
        return (f"Recording saved: {self.n} samples to {self.path} over {span:.2f} s "
                f"({rate:.0f} samples/s), {dropped} dropped, {self.errors} with nonzero status")


# --- Qt plumbing ------------------------------------------------------------------------

class Bridge(QtCore.QObject):
    """Carries text and callables from worker threads to the GUI thread."""
    text = QtCore.Signal(str)
    call = QtCore.Signal(object)


class StreamToConsole:
    """sys.stdout/sys.stderr replacement: everything printed shows up in the console quadrant."""

    def __init__(self, bridge):
        self.bridge = bridge

    def write(self, text):
        text = ANSI.sub("", text)
        if text:
            self.bridge.text.emit(text)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


class CommandLine(QtWidgets.QLineEdit):
    """Input line with history (Up/Down) and command-name completion (Tab)."""
    submitted = QtCore.Signal(str)

    def __init__(self, words):
        super().__init__()
        self.history = []
        self.pos = 0
        completer = QtWidgets.QCompleter(sorted(words), self)
        completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
        completer.setCompletionMode(QtWidgets.QCompleter.InlineCompletion)
        self.setCompleter(completer)
        self.returnPressed.connect(self._submit)

    def _submit(self):
        line = self.text()
        if line.strip() and (not self.history or self.history[-1] != line):
            self.history.append(line)
        self.pos = len(self.history)
        self.clear()
        self.submitted.emit(line)

    def event(self, e):
        if e.type() == QtCore.QEvent.KeyPress and e.key() == QtCore.Qt.Key_Tab:
            self.end(False)          # accept the inline completion instead of moving focus
            return True
        return super().event(e)

    def keyPressEvent(self, e):
        if e.key() in (QtCore.Qt.Key_Up, QtCore.Qt.Key_Down) and self.history:
            self.pos = max(0, self.pos - 1) if e.key() == QtCore.Qt.Key_Up else \
                min(len(self.history), self.pos + 1)
            self.setText(self.history[self.pos] if self.pos < len(self.history) else "")
            return
        super().keyPressEvent(e)


def _panel(title, widget):
    box = QtWidgets.QGroupBox(title)
    layout = QtWidgets.QVBoxLayout(box)
    layout.setContentsMargins(6, 6, 6, 6)
    layout.addWidget(widget)
    return box


# --- main window ----------------------------------------------------------------------------

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, args, cfg, cfg_path):
        super().__init__()
        self.args, self.cfg, self.cfg_path = args, cfg, cfg_path
        g = cfg["gui"]
        self.window_s = float(np.clip(float(g["window"]), 0.5, MAX_WINDOW))
        self.buffered = bool(g["buffered"])
        self.min_span = {"force": float(g["min_span_force"]), "torque": float(g["min_span_torque"])}
        self.y_range = {"force": None, "torque": None}   # last range set per plot (see next_y_range)
        self.hold_test_view = bool(g["hold_test_view"])
        self.test = None                       # TestHistory of the current/last recording
        self.view = "live"                     # "live" rolling window, or "test" = the recording
        self.slots = [str(g["top_left"]).lower(), str(g["top_right"]).lower()]
        for slot in self.slots:
            if slot not in ("torque", "force", "none"):
                raise ValueError(f"gui.top_left/top_right must be torque, force or none (got {slot!r})")
        chans = g["channels"]
        chans = chans.split() if isinstance(chans, str) else list(chans or [])
        self.visible = {a: a.lower() in [c.lower() for c in chans] for a in AXES}

        self.sensor = AxiaSensor(args.host, rdt_port=args.rdt_port, http_port=args.http_port,
                                 timeout=args.timeout)
        self.ring = RingBuffer(int(MAX_RATE * MAX_WINDOW))
        self.io_lock = threading.Lock()        # held by the stream thread; guards the RDT socket
        self.stream_thread = None
        self.stream_stop = threading.Event()
        self.latest = None
        self.ranges = None
        self.recorder = None
        self.launch_applied = False
        self.paused = False
        self.rate_hist = collections.deque(maxlen=64)
        self.pending_answer = None             # queue.Queue while a command waits for y/N
        self.commands = queue.Queue()
        self.busy = False

        self.bridge = Bridge()
        self.bridge.text.connect(self._append_console)
        self.bridge.call.connect(lambda fn: fn())
        self.parser = cli.build_parser(prog="")
        self._build_ui()

        self._stdout, self._stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = StreamToConsole(self.bridge)
        cli.ASK = self._ask
        threading.Thread(target=self._command_loop, daemon=True, name="commands").start()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(int(1000 / max(1.0, float(g["refresh_hz"]))))
        self.readout_every = max(1, round(float(g["refresh_hz"]) / 10))   # readout at ~10 Hz
        self.ticks = 0

        print(f"ATI Axia80 console. Sensor {args.host}"
              + (f", settings from {cfg_path}" if cfg_path else "") + ". Type `help` for commands.")
        if g["autostart"]:
            self.start_stream()

    # --- layout ---------------------------------------------------------------------------

    def _build_ui(self):
        self.setWindowTitle(f"ATI Axia80 - {self.args.host}")
        self.resize(1400, 900)
        pg.setConfigOptions(antialias=False, background="#1e1e1e", foreground="#d0d0d0")
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)

        self.plots, self.curves = {}, {}
        for group, axes in GROUPS.items():
            pw = pg.PlotWidget()
            pw.showGrid(x=True, y=True, alpha=1.0)
            for name in ("left", "bottom"):
                # Opaque, major-only grid lines: the minor grid alone cost ~2/3 of a CPU core at 30 fps.
                ax = pw.getAxis(name)
                ax.setPen(pg.mkPen("#3a3a3a"))
                ax.setTextPen(pg.mkPen("#c8c8c8"))
                ax.setStyle(maxTickLevel=0)
            pw.setLabel("bottom", "time (s)")
            pw.hideButtons()
            pw.disableAutoRange()               # x and y ranges are set every frame in _refresh
            pw.setXRange(-self.window_s, 0, padding=0)
            pw.setYRange(*y_limits(0, 0, self.min_span[group]), padding=0)
            pw.addLegend(offset=(8, 8), brush=pg.mkBrush(30, 30, 30, 210), pen=pg.mkPen("#555"))
            pw.getPlotItem().legend.setAcceptedMouseButtons(QtCore.Qt.NoButton)   # visibility is set with `plot`
            for a in axes:
                self.curves[a] = pw.plot(name=a, pen=pg.mkPen(AXIS_COLORS[a[1].lower()], width=1),
                                         skipFiniteCheck=True)
            self.plots[group] = pw
        self._set_plot_titles()

        self.top = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        for group in ("torque", "force"):
            self.top.addWidget(self.plots[group])

        self.readout = QtWidgets.QLabel("Not streaming. Type `start`.")
        self.readout.setFont(mono)
        self.readout.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        self.readout.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.readout_box = _panel("Live readout", self.readout)

        self.console = QtWidgets.QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setFont(mono)
        self.console.setMaximumBlockCount(5000)
        words = set(cli.HANDLERS) | {"start", "stop", "record", "live", "plot", "window", "pause", "resume",
                                     "connect", "clear", "help", "quit", "exit"}
        self.input = CommandLine(words)
        self.input.setFont(mono)
        self.input.setPlaceholderText("axia80> type a command (help for a list)")
        self.input.submitted.connect(self._on_command)
        console_w = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(console_w)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self.console)
        cl.addWidget(self.input)
        console_box = _panel("Console", console_w)

        bottom = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        bottom.addWidget(self.readout_box)
        bottom.addWidget(console_box)

        main = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        main.addWidget(self.top)
        main.addWidget(bottom)
        main.setSizes([560, 340])
        bottom.setSizes([700, 700])
        self.setCentralWidget(main)
        self._apply_layout()
        self.input.setFocus()

    def _set_plot_titles(self):
        fu = self.sensor.force_units or "N"
        tu = self.sensor.torque_units or "Nm"
        self.plots["force"].setTitle(f"Force ({fu})")
        self.plots["torque"].setTitle(f"Torque ({tu})")

    def _apply_layout(self):
        """Places plots per self.slots, hides empty ones; a lone plot fills the top half."""
        for i, group in enumerate(g for g in self.slots if g != "none"):
            self.top.insertWidget(i, self.plots[group])
        shown = 0
        for group, pw in self.plots.items():
            on = group in self.slots and any(self.visible[a] for a in GROUPS[group])
            pw.setVisible(on)
            shown += on
        for a, curve in self.curves.items():
            curve.setVisible(self.visible[a])
        for group, pw in self.plots.items():          # legend lists only the plotted channels
            legend = pw.getPlotItem().legend
            legend.clear()
            for a in GROUPS[group]:
                if self.visible[a]:
                    legend.addItem(self.curves[a], a)
        self.top.setVisible(shown > 0)
        if shown == 2:
            self.top.setSizes([1, 1])

    # --- console I/O --------------------------------------------------------------------------

    def _append_console(self, text):
        cursor = self.console.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        cursor.insertText(text)
        self.console.setTextCursor(cursor)
        self.console.ensureCursorVisible()

    def log(self, message):
        """Thread-safe print to the console."""
        self.bridge.text.emit(message + "\n")

    def _ask(self, question):
        """Called from the command thread: shows question, waits for the next console line."""
        answers = queue.Queue()
        self.pending_answer = answers
        self.log(question)
        return answers.get()

    def _on_command(self, line):
        self._append_console(f"axia80> {line}\n")
        if self.pending_answer is not None:
            answers, self.pending_answer = self.pending_answer, None
            answers.put(line)
            return
        try:
            tokens = shlex.split(line)
        except ValueError as e:
            print(f"error: {e}")
            return
        if not tokens:
            return
        name, rest = tokens[0].lower(), tokens[1:]
        builtin = getattr(self, f"cmd_{name.replace('-', '_')}", None)
        if name in ("quit", "exit"):
            self.close()
        elif name in ("start", "stop", "record", "live", "plot", "window", "pause", "resume", "connect",
                      "clear", "help", "monitor", "stream", "read") and builtin:
            try:
                builtin(rest)
            except (ValueError, OSError) as e:
                print(f"error: {e}")
        elif name in cli.HANDLERS:
            self._run_cli(tokens)
        else:
            print(f"unknown command '{tokens[0]}'. Type `help` for a list.")

    # --- CLI commands on the command thread -------------------------------------------------------

    def _parse_cli(self, tokens):
        try:
            args = self.parser.parse_args(tokens)
        except SystemExit:            # argparse printed usage/help to the console
            return None
        if args.cmd is None:
            return None
        for name in ("host", "rdt_port", "http_port", "timeout"):
            if getattr(args, name) is None:
                setattr(args, name, getattr(self.args, name))
        try:
            cli._resolve(args, self.cfg)
        except SystemExit as e:       # e.g. `stream` without an output file
            print(e)
            return None
        args.cfg_path = self.cfg_path
        return args

    def _run_cli(self, tokens):
        args = self._parse_cli(tokens)
        if args is None:
            return
        if self.busy:
            print("(queued: another command is still running)")
        self.commands.put(args)

    def _command_loop(self):
        while True:
            args = self.commands.get()
            self.busy = True
            try:
                if args.cmd == "read" and not self.streaming():
                    with self.io_lock:
                        cli.run_command(args, self.cfg, self.cfg_path, sensor=self.sensor,
                                        apply_launch=False)
                else:
                    cli.run_command(args, self.cfg, self.cfg_path, sensor=self.sensor,
                                    apply_launch=False)
                if args.cmd == "config" and self.streaming() and \
                        (args.adc_rate is not None or args.calibration is not None):
                    self.log("Restarting the stream to apply the new rate/calibration.")
                    self.bridge.call.emit(self.restart_stream)
            except SystemExit:
                pass
            except Exception as e:
                self.log(f"error: {e}")
            finally:
                self.busy = False

    # --- built-in commands (GUI thread) ------------------------------------------------------------

    def cmd_help(self, rest):
        print(HELP)

    def cmd_clear(self, rest):
        self.console.clear()

    def cmd_start(self, rest):
        if "--no-buffered" in rest:
            self.buffered = False
        elif "--buffered" in rest:
            self.buffered = True
        self.start_stream()

    def cmd_stop(self, rest):
        if not self.streaming():
            print("Not streaming.")
        self.stop_stream()

    def cmd_pause(self, rest):
        self.paused = True
        print("Plots paused (streaming and recording continue). `resume` to continue.")

    def cmd_resume(self, rest):
        self.paused = False
        print("Plots resumed.")

    def cmd_monitor(self, rest):
        print("The live readout is always shown in the lower-left panel.")

    def cmd_read(self, rest):
        if self.streaming() and self.latest is not None:
            print("\n".join(cli._format_sample(self.sensor, self.latest)))
        else:
            self._run_cli(["read", *rest])

    def cmd_stream(self, rest):
        args = self._parse_cli(["stream", *rest])
        if args is None:
            return
        self.cmd_record([args.csv] + ([str(args.duration)] if args.duration else []),
                        count=args.count or 0)

    def cmd_record(self, rest, count=0):
        if not rest:
            if self.recorder:
                print(f"Recording to {self.recorder.path}: {self.recorder.n} samples, "
                      f"{self.recorder.elapsed():.1f} s")
            else:
                print("Not recording. Usage: record FILE.csv [SECONDS] | record stop")
            return
        if rest[0].lower() == "stop":
            if not self.recorder:
                print("Not recording.")
            elif self.streaming():
                self.recorder.stop_requested = True     # stream thread closes it
            else:
                self._finish_recording()
            return
        if self.recorder:
            print(f"Already recording to {self.recorder.path}; `record stop` first.")
            return
        path = os.path.expanduser(rest[0])
        duration = float(rest[1]) if len(rest) > 1 else 0.0
        if self.sensor.cpf is None:
            self.sensor.load_scaling()
        recorder = Recorder(path, self.sensor, duration=duration, count=count)
        # Only recorded data is plotted during a test: drop the live history and show the test.
        self.test = TestHistory()
        self.ring.clear()
        self.rate_hist.clear()
        self._set_view("test")
        self.recorder = recorder               # the stream thread starts writing from here
        limit = f" for {duration:g} s" if duration else (f" for {count} samples" if count else "")
        print(f"Recording to {path}{limit}. `record stop` to finish.")
        if not self.streaming():
            self.start_stream()

    def _finish_recording(self):
        rec, self.recorder = self.recorder, None
        if rec is not None:
            self.log(rec.close(self.sensor.dropped))
            self._recording_ended()

    def _set_view(self, view):
        self.view = view
        self.y_range = {"force": None, "torque": None}      # different data: rescale from scratch
        label = "time since recording start (s)" if view == "test" else "time (s)"
        for pw in self.plots.values():
            pw.setLabel("bottom", label)
            if view == "live":
                pw.setXRange(-self.window_s, 0, padding=0)

    def cmd_live(self, rest):
        if self.recorder is not None:
            print("Recording in progress; the plots show only the recording until it ends.")
            return
        self._set_view("live")
        print(f"Showing the live rolling {self.window_s:g} s window.")

    def cmd_window(self, rest):
        if not rest:
            print(f"Plot window: {self.window_s:g} s")
            return
        self.window_s = float(np.clip(float(rest[0]), 0.5, MAX_WINDOW))
        print(f"Plot window: {self.window_s:g} s"
              + (" (applies to the live view; the recording view always shows the whole test)"
                 if self.view == "test" else ""))

    def cmd_connect(self, rest):
        was_streaming = self.streaming()
        self.stop_stream(wait=True)
        if rest:
            self.args.host = rest[0]
        self.sensor.close()
        self.sensor = AxiaSensor(self.args.host, rdt_port=self.args.rdt_port,
                                 http_port=self.args.http_port, timeout=self.args.timeout)
        self.setWindowTitle(f"ATI Axia80 - {self.args.host}")
        print(f"Connected to {self.args.host}.")
        if was_streaming or rest:
            self.start_stream()

    def cmd_plot(self, rest):
        words = [w.lower() for w in rest]
        if not words:
            self._print_plot_menu()
            return
        if words[0] == "swap":
            self.slots.reverse()
        elif words[0] in ("left", "right") and len(words) == 2 and words[1] in ("torque", "force", "none"):
            idx = 0 if words[0] == "left" else 1
            other = 1 - idx
            if self.slots[other] == words[1] and words[1] != "none":
                self.slots[other] = self.slots[idx]        # moving a plot to the other side swaps them
            self.slots[idx] = words[1]
        else:
            state = None
            if words[-1] in ("on", "off"):
                state, words = words[-1] == "on", words[:-1]
            chans = []
            for w in words:
                if w in ("all", "none"):
                    chans += list(AXES)
                    state = (w == "all") if state is None else state
                elif w.rstrip("s") in GROUPS:
                    chans += GROUPS[w.rstrip("s")]
                elif w.isdigit() and 1 <= int(w) <= 6:
                    chans.append(AXES[int(w) - 1])
                elif w.capitalize() in AXES:
                    chans.append(w.capitalize())
                else:
                    raise ValueError(f"unknown channel '{w}' (fx fy fz tx ty tz, 1-6, force, torque, all, none)")
            for a in dict.fromkeys(chans):
                self.visible[a] = (not self.visible[a]) if state is None else state
        self._apply_layout()
        self._print_plot_menu()

    def _print_plot_menu(self):
        def mark(a):
            return "[x]" if self.visible[a] else "[ ]"
        rows = []
        for group in ("force", "torque"):
            cells = "   ".join(f"{AXES.index(a) + 1} {mark(a)} {a}" for a in GROUPS[group])
            side = ("top-left" if self.slots[0] == group else
                    "top-right" if self.slots[1] == group else "hidden")
            rows.append(f"  {group:<6} ({side:<9})  {cells}")
        print("Plot channels:\n" + "\n".join(rows) +
              "\n  plot fz off | plot 1 2 (toggle) | plot torque off | plot all | plot swap")

    # --- streaming ----------------------------------------------------------------------------------

    def streaming(self):
        return self.stream_thread is not None and self.stream_thread.is_alive()

    def start_stream(self):
        if self.streaming():
            print("Already streaming.")
            return
        self.stream_stop.clear()
        self.stream_thread = threading.Thread(target=self._stream_loop, daemon=True, name="stream")
        self.stream_thread.start()

    def stop_stream(self, wait=False):
        self.stream_stop.set()
        if wait and self.stream_thread is not None:
            self.stream_thread.join(timeout=3)

    def restart_stream(self):
        self.stop_stream(wait=True)
        self.start_stream()

    def _stream_loop(self):
        sensor = self.sensor
        with self.io_lock:
            gen = None
            tt, ty = [], []                      # samples that went into the recording
            try:
                if not self.launch_applied:
                    cli._apply_launch_settings(sensor, self.cfg)
                    self.launch_applied = True
                sensor.load_scaling()
                self.bridge.call.emit(self._set_plot_titles)
                self.ranges = cli._active_ranges(sensor)
                try:
                    adc_rate = float(sensor.config_xml().get("runrate") or 0)
                except Exception:
                    adc_rate = 0.0
                self.ring.clear()
                self.rate_hist.clear()
                self.log(f"Streaming from {sensor.host} ({'buffered' if self.buffered else 'single'} packets"
                         + (f", ADC {adc_rate:g} Hz" if adc_rate else "") + ").")
                gen = sensor.stream(buffered=self.buffered)
                ft0 = t0 = None
                bt, by = [], []
                last_flush = time.time()
                for s in gen:
                    if ft0 is None:
                        ft0, t0 = s.ft_seq, s.t_host
                    t = ((s.ft_seq - ft0) & 0xFFFFFFFF) / adc_rate if adc_rate else s.t_host - t0
                    bt.append(t)
                    by.append(s.ft)
                    self.latest = s
                    rec = self.recorder
                    if rec is not None:
                        before = rec.n
                        more = rec.write(s)
                        if rec.n > before:
                            tt.append(t)
                            ty.append(s.ft)
                        if not more:
                            self._flush_test(tt, ty)
                            tt, ty = [], []
                            self._close_recorder_from_stream(rec)
                    if len(bt) >= 512 or s.t_host - last_flush > 0.02:
                        self.ring.extend(np.asarray(bt), np.asarray(by))
                        bt, by = [], []
                        self._flush_test(tt, ty)
                        tt, ty = [], []
                        last_flush = s.t_host
                        if self.stream_stop.is_set():
                            break
                self.ring.extend(np.asarray(bt), np.asarray(by))
            except Exception as e:
                self.log(f"Stream stopped: {e}")
            finally:
                if gen is not None:
                    gen.close()              # sends RDT stop
                if self.recorder is not None:
                    self._flush_test(tt, ty)
                    self._close_recorder_from_stream(self.recorder)
                self.log("Streaming stopped.")

    def _flush_test(self, tt, ty):
        test = self.test
        if test is not None and tt:
            test.extend(np.asarray(tt), np.asarray(ty))

    def _close_recorder_from_stream(self, rec):
        # The stream thread is the recorder's only writer, so it closes it too.
        if self.recorder is rec:
            self.recorder = None
        self.log(rec.close(self.sensor.dropped))
        self.bridge.call.emit(self._recording_ended)

    def _recording_ended(self):
        if self.recorder is not None:          # a new recording already started
            return
        if self.hold_test_view:
            print("Plots are showing the finished recording. `live` returns to the rolling view.")
        else:
            self._set_view("live")

    # --- redraw ---------------------------------------------------------------------------------------

    def _refresh(self):
        now = time.time()
        self.rate_hist.append((now, self.ring.total))
        while len(self.rate_hist) > 2 and now - self.rate_hist[0][0] > 1.0:
            self.rate_hist.popleft()
        (t_a, n_a), (t_b, n_b) = self.rate_hist[0], self.rate_hist[-1]
        rate = (n_b - n_a) / (t_b - t_a) if t_b > t_a else 0.0

        if not self.paused and self.top.isVisible():
            self._redraw_plots()

        self.ticks += 1
        if self.ticks % self.readout_every == 0:
            self._update_readout(rate)

    def _redraw_plots(self):
        rows = [i for i, a in enumerate(AXES) if self.visible[a]]
        if not rows:
            return
        buckets = max(200, max(pw.width() for pw in self.plots.values() if pw.isVisible()))
        if self.view == "test" and self.test is not None:
            x, y = self.test.decimated(rows, buckets)
            xr = (0.0, max(1.0, float(x[-1]) if len(x) else 0.0))     # whole test, growing
        else:
            t, y = self.ring.last(self.window_s, rows)
            x, y = peak_decimate(t - t[-1], y, buckets) if len(t) else (t, y)
            xr = (-self.window_s, 0.0)
        for k, i in enumerate(rows):
            self.curves[AXES[i]].setData(x, y[k])
        for group, pw in self.plots.items():
            if not pw.isVisible():
                continue
            pw.setXRange(*xr, padding=0)
            ks = [k for k, i in enumerate(rows) if AXES[i] in GROUPS[group]]
            if ks and y.shape[1]:
                yr = next_y_range(self.y_range[group], float(y[ks].min()), float(y[ks].max()),
                                  self.min_span[group])
                if yr != self.y_range[group]:
                    pw.setYRange(*yr, padding=0)
                    self.y_range[group] = yr

    def _update_readout(self, rate):
        state = "STREAMING" if self.streaming() else "stopped"
        head = [f"{state} @ {self.sensor.host}" + ("   [plots paused]" if self.paused else "")]
        if self.recorder:
            head.append(f"REC {os.path.basename(self.recorder.path)}  {self.recorder.n} samples  "
                        f"{self.recorder.elapsed():.1f} s   (plots show this recording)")
        elif self.view == "test" and self.test is not None:
            head.append(f"Plots: finished recording ({self.test.duration():.1f} s). `live` to return.")
        s = self.latest
        if s is None or self.sensor.cpf is None:
            body = ["No data yet." if self.streaming() else "Not streaming. Type `start`."]
        else:
            body = cli.monitor_lines(self.sensor, s, self.ranges, rate if self.streaming() else None)
        self.readout.setText("\n".join(head + [""] + body))

    # --- shutdown -----------------------------------------------------------------------------------------

    def closeEvent(self, e):
        self.timer.stop()
        self.stop_stream(wait=True)
        if self.recorder is not None:
            self._stdout.write(self.recorder.close(self.sensor.dropped) + "\n")
            self.recorder = None
        self.sensor.close()
        sys.stdout, sys.stderr = self._stdout, self._stderr
        cli.ASK = None
        super().closeEvent(e)


def _dark_palette(app):
    app.setStyle("Fusion")
    p = QtGui.QPalette()
    base, text = QtGui.QColor("#252526"), QtGui.QColor("#d4d4d4")
    for role, color in ((QtGui.QPalette.Window, "#2d2d30"), (QtGui.QPalette.WindowText, text),
                        (QtGui.QPalette.Base, base), (QtGui.QPalette.AlternateBase, "#2d2d30"),
                        (QtGui.QPalette.Text, text), (QtGui.QPalette.Button, "#3c3c3c"),
                        (QtGui.QPalette.ButtonText, text), (QtGui.QPalette.Highlight, "#264f78"),
                        (QtGui.QPalette.HighlightedText, "#ffffff"),
                        (QtGui.QPalette.PlaceholderText, "#808080")):
        p.setColor(role, QtGui.QColor(color))
    app.setPalette(p)


def run(args, cfg, cfg_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    _dark_palette(app)
    win = MainWindow(args, cfg, cfg_path)
    win.show()
    return app.exec()
