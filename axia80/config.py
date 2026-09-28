"""Loads launch options from config.yaml.

Precedence: command-line flag > environment (AXIA_HOST) > config.yaml > built-in default.

Uses PyYAML when it is installed (Debian: python3-yaml). Otherwise a small
built-in parser handles the subset used by config.yaml: `key: value` pairs,
one level of nested sections, comments, and scalar values.
"""

import copy
import os

from . import protocol as p

DEFAULTS = {
    "host": "192.168.1.1",
    "mac": None,
    "rdt_port": p.RDT_PORT,
    "http_port": 80,
    "timeout": 1.0,
    "sensor": {"adc_rate": None, "filter": None, "rdt_buffer": None, "calibration": None},
    "bias_on_start": False,
    "monitor": {"hz": 20.0, "buffered": False},
    "stream": {"csv": None, "duration": 0.0, "count": 0, "buffered": False},
    "gui": {
        "top_left": "torque",        # torque | force | none
        "top_right": "force",
        "channels": "Fx Fy Fz Tx Ty Tz",
        "window": 10.0,              # seconds of history shown in the plots
        "min_span_force": 0.05,      # smallest y-axis span on the force plot (force units, N)
        "min_span_torque": 0.005,    # smallest y-axis span on the torque plot (torque units, Nm)
        "hold_test_view": True,      # keep showing a finished recording until `live`
        "refresh_hz": 30.0,          # plot/readout redraw rate
        "autostart": True,           # start streaming when the window opens
        "buffered": True,            # RDT buffered packets (lower CPU at high rates)
    },
}

SEARCH_PATHS = (
    "config.yaml",                                                       # current directory
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml"),  # project root
)


def find_config(path=None):
    if path:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"config file not found: {path}")
        return path
    for candidate in SEARCH_PATHS:
        if os.path.isfile(candidate):
            return os.path.normpath(candidate)
    return None


def load_config(path=None):
    """Returns (config dict merged over DEFAULTS, path used or None)."""
    cfg = copy.deepcopy(DEFAULTS)
    found = find_config(path)
    if found:
        with open(found) as f:
            text = f.read()
        try:
            import yaml
            data = yaml.safe_load(text) or {}
        except ImportError:
            data = _parse_simple_yaml(text)
        if not isinstance(data, dict):
            raise ValueError(f"{found}: top level must be a mapping")
        _merge(cfg, data, found)
    return cfg, found


def _merge(base, data, source, prefix=""):
    for key, value in data.items():
        name = prefix + str(key)
        if key not in base:
            raise ValueError(f"{source}: unknown option '{name}'")
        if isinstance(base[key], dict):
            if value is None:
                continue
            if not isinstance(value, dict):
                raise ValueError(f"{source}: '{name}' must be a section")
            _merge(base[key], value, source, name + ".")
        else:
            base[key] = value


def _scalar(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    low = text.lower()
    if low in ("", "~", "null", "none"):
        return None
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    for conv in (int, float):
        try:
            return conv(text)
        except ValueError:
            pass
    return text


def _strip_comment(line):
    quote = None
    for i, ch in enumerate(line):
        if ch in "'\"":
            quote = None if quote == ch else (quote or ch)
        elif ch == "#" and quote is None and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _parse_simple_yaml(text):
    data, section = {}, None
    for n, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"config.yaml line {n}: expected 'key: value'")
        indented = line[0] in " \t"
        key, _, value = line.strip().partition(":")
        key = key.strip()
        if indented:
            if section is None:
                raise ValueError(f"config.yaml line {n}: unexpected indentation")
            data[section][key] = _scalar(value)
        elif value.strip():
            data[key], section = _scalar(value), None
        else:
            data[key], section = {}, key
    return data
