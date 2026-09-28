"""Wire formats for the ATI Ethernet Axia F/T sensor.

Reference: ATI manual 9610-05-Ethernet Axia, sections 4.8 (status code),
10 (TCP interface) and 12 (UDP RDT interface). All multi-byte values on the
wire are big-endian (network byte order).
"""

import struct

RDT_PORT = 49152
TCP_PORT = 49151
HEADER = 0x1234

# --- UDP RDT -----------------------------------------------------------------

RDT_STOP = 0x0000
RDT_START_SINGLE = 0x0002
RDT_START_BUFFERED = 0x0003
RDT_BIAS = 0x0042

_RDT_REQUEST = struct.Struct(">HHI")          # header, command, sample_count
_RDT_RECORD = struct.Struct(">III6i")         # rdt_seq, ft_seq, status, Fx..Tz
RDT_RECORD_SIZE = _RDT_RECORD.size            # 36 bytes


def pack_rdt_request(command, sample_count=0):
    return _RDT_REQUEST.pack(HEADER, command, sample_count)


def unpack_rdt_request(data):
    """Returns (command, sample_count). Used by the simulator."""
    header, command, count = _RDT_REQUEST.unpack(data[:_RDT_REQUEST.size])
    if header != HEADER:
        raise ValueError(f"bad RDT request header 0x{header:04x}")
    return command, count


def pack_rdt_record(rdt_seq, ft_seq, status, counts):
    return _RDT_RECORD.pack(rdt_seq, ft_seq, status, *counts)


def unpack_rdt_packet(data):
    """Splits a UDP payload into records of (rdt_seq, ft_seq, status, counts[6]).

    In buffered mode one packet carries 1-40 back-to-back records.
    """
    if len(data) == 0 or len(data) % RDT_RECORD_SIZE:
        raise ValueError(f"RDT packet length {len(data)} is not a multiple of {RDT_RECORD_SIZE}")
    records = []
    for fields in _RDT_RECORD.iter_unpack(data):
        records.append((fields[0], fields[1], fields[2], fields[3:9]))
    return records


def seq_gap(prev, cur):
    """Number of records missing between two consecutive rdt_seq values (32-bit rollover safe)."""
    return ((cur - prev) & 0xFFFFFFFF) - 1


# --- TCP ---------------------------------------------------------------------

TCP_READFT = 0
TCP_READCALINFO = 1

_TCP_READFT_RESPONSE = struct.Struct(">HH6h")            # header, status (upper 16 bits), counts
_TCP_CALINFO_RESPONSE = struct.Struct(">HBBII6H")        # header, fu, tu, cpf, cpt, scale[6]


def pack_tcp_readft(bias=False, clear_latch=False, mc_enable=0):
    sys_commands = (1 if bias else 0) | (2 if clear_latch else 0)
    return struct.pack(">B15xHH", TCP_READFT, mc_enable, sys_commands)


def pack_tcp_readcalinfo():
    return struct.pack(">B19x", TCP_READCALINFO)


def unpack_tcp_readft(data):
    """Returns (status_upper16, counts16[6]). Real value = counts * scale / cpf|cpt."""
    header, status, *counts = _TCP_READFT_RESPONSE.unpack(data[:_TCP_READFT_RESPONSE.size])
    if header != HEADER:
        raise ValueError(f"bad TCP response header 0x{header:04x}")
    return status, counts


TCP_READFT_RESPONSE_SIZE = _TCP_READFT_RESPONSE.size
TCP_CALINFO_RESPONSE_SIZE = _TCP_CALINFO_RESPONSE.size


def unpack_tcp_calinfo(data):
    header, fu, tu, cpf, cpt, *scale = _TCP_CALINFO_RESPONSE.unpack(data[:_TCP_CALINFO_RESPONSE.size])
    if header != HEADER:
        raise ValueError(f"bad TCP response header 0x{header:04x}")
    return {
        "force_units": FORCE_UNITS.get(fu, f"code {fu}"),
        "torque_units": TORQUE_UNITS.get(tu, f"code {tu}"),
        "cpf": cpf,
        "cpt": cpt,
        "scale_factors": scale,
    }


FORCE_UNITS = {1: "lbf", 2: "N", 3: "klbf", 4: "kN", 5: "kgf", 6: "gf"}
TORQUE_UNITS = {1: "lbf-in", 2: "lbf-ft", 3: "N-m", 4: "N-mm", 5: "kgf-cm", 6: "kN-m"}

# --- Status word (section 4.8) ----------------------------------------------

STATUS_BITS = {
    0: "temperature out of range",
    1: "supply voltage out of range",
    2: "broken gage",
    3: "busy",
    5: "other error",
    16: "monitor condition latched",
    27: "gage out of range",
    28: "simulated error",
    29: "calibration checksum error",
    30: "F/T out of range",
    31: "error",
}


def decode_status(status):
    """Returns a list of human-readable flags set in a 32-bit status word ([] = healthy)."""
    return [name for bit, name in sorted(STATUS_BITS.items()) if status & (1 << bit)]


# --- Sample rates (section 4.3 / 9.2) ---------------------------------------

# Rounded rate (Hz) -> value accepted by setting.cgi?setadcrate=
ADC_RATES = {500: 488, 1000: 976, 2000: 1953, 4000: 3906, 8000: 7812}
