"""Values <-> 16-bit registers, with the word and byte orders found in real devices.

Word order names follow the usual convention for a 32-bit value whose big-endian
bytes are A B C D:

=====  ===============  ================================================
order  registers        seen in
=====  ===============  ================================================
ABCD   [AB, CD]         the Modbus default (big endian), most PLCs
CDAB   [CD, AB]         word-swapped: many energy meters, Schneider, Eastron
BADC   [BA, DC]         byte-swapped
DCBA   [DC, BA]         little endian
=====  ===============  ================================================

64-bit values extend the same rule (word order and byte order). Only types of
32 bits or more are affected; 16-bit types are always one plain register.
"""
from __future__ import annotations

import math
import struct

WORD_ORDERS = ("ABCD", "CDAB", "BADC", "DCBA")

# type -> (struct format, registers)
_NUMERIC = {
    "uint16": (">H", 1), "int16": (">h", 1),
    "uint32": (">I", 2), "int32": (">i", 2), "float32": (">f", 2),
    "uint64": (">Q", 4), "int64": (">q", 4), "float64": (">d", 4),
}
_RANGES = {
    "uint16": (0, 0xFFFF), "int16": (-0x8000, 0x7FFF),
    "uint32": (0, 0xFFFFFFFF), "int32": (-0x80000000, 0x7FFFFFFF),
    "uint64": (0, 0xFFFFFFFFFFFFFFFF), "int64": (-(1 << 63), (1 << 63) - 1),
}
REGISTER_TYPES = (*_NUMERIC, "string")
BIT_TYPES = ("bool",)
TYPES = REGISTER_TYPES + BIT_TYPES


def is_integer(kind: str) -> bool:
    return kind in _RANGES


def is_float(kind: str) -> bool:
    return kind in ("float32", "float64")


def registers(kind: str, length: int | None = None) -> int:
    """Number of registers a value of `kind` occupies (`length` for strings)."""
    if kind == "string":
        if not length or length < 1:
            raise ValueError("a string needs a length in registers")
        return length
    if kind in _NUMERIC:
        return _NUMERIC[kind][1]
    raise ValueError(f"{kind!r} is not a register type")


def _order(raw: bytes, order: str) -> bytes:
    if len(raw) <= 2 or order == "ABCD":
        return raw
    words = [raw[i:i + 2] for i in range(0, len(raw), 2)]
    if order in ("CDAB", "DCBA"):
        words.reverse()
    if order in ("BADC", "DCBA"):
        words = [w[::-1] for w in words]
    return b"".join(words)


def _to_words(raw: bytes) -> list[int]:
    return list(struct.unpack(f">{len(raw) // 2}H", raw))


def _from_words(words: list[int]) -> bytes:
    return struct.pack(f">{len(words)}H", *words)


def saturate(kind: str, raw: float) -> int:
    """Round and clamp to the integer type's range (devices saturate rather than wrap)."""
    low, high = _RANGES[kind]
    if math.isnan(raw):
        return 0
    if math.isinf(raw):
        return high if raw > 0 else low
    return int(min(max(round(raw), low), high))


def encode(value, kind: str, order: str = "ABCD", scale: float = 1.0,
           length: int | None = None) -> list[int]:
    """Engineering value -> registers. Integers are value/scale, rounded and clamped."""
    if kind == "string":
        data = str(value).encode("latin-1", "replace")[:2 * length]
        return _to_words(data.ljust(2 * length, b"\0"))
    fmt, _ = _NUMERIC[kind]
    if is_float(kind):
        raw = float(value) / scale
        if kind == "float32" and math.isfinite(raw) and abs(raw) > 3.4028234663852886e38:
            raw = math.copysign(math.inf, raw)
        data = struct.pack(fmt, raw)
    else:
        data = struct.pack(fmt, saturate(kind, float(value) / scale))
    return _to_words(_order(data, order))


def decode(words: list[int], kind: str, order: str = "ABCD", scale: float = 1.0):
    """Registers -> engineering value (inverse of :func:`encode`)."""
    data = _from_words(words)
    if kind == "string":
        return data.rstrip(b"\0").decode("latin-1")
    fmt, _ = _NUMERIC[kind]
    raw = struct.unpack(fmt, _order(data, order))[0]
    if scale == 1.0:
        return raw if is_integer(kind) else float(raw)
    return raw * scale


def fits(value: float, kind: str, scale: float = 1.0) -> bool:
    """True if `value` can be written to an integer register of `kind` without clamping."""
    if not is_integer(kind):
        return True
    low, high = _RANGES[kind]
    return low <= round(value / scale) <= high
