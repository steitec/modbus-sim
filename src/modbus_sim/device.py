"""A simulated Modbus device: points, tables and the function code handlers.

`Device.handle(pdu)` takes a request PDU (function code + data) and returns the
response PDU. Transport faults (no answer, delay, bad CRC, ...) are applied by the
server around it; exception faults and freeze/drift are applied here.
"""
from __future__ import annotations

import math
import struct
import threading
import time
from typing import Any, Callable

from . import datatypes as dt
from . import frame
from .faults import Faults
from .models import Model, ModelError, make_model
from .profile import BIT_TABLES, PointSpec, Profile, ProfileError
from .signals import Signal

MAX_READ_BITS = 2000
MAX_WRITE_BITS = 1968
MAX_READ_REGS = 125
MAX_WRITE_REGS = 123
MAX_RW_WRITE_REGS = 121

_ID_OBJECTS = ("vendor", "product", "revision", "vendor_url", "product_name", "model_name",
               "user_application_name")


class ModbusException(Exception):
    def __init__(self, code: int, message: str = ""):
        super().__init__(message or f"exception {code:#04x}")
        self.code = code


class Point:
    def __init__(self, spec: PointSpec, device: "Device", seed: int | None = None):
        self.spec = spec
        self.device = device
        self.stored = spec.value
        self.override: Any = None
        self.signal = Signal(spec.sources, device.clock(), seed) if spec.sources else None
        self._drift_rate = 0.0
        self._drift_offset = 0.0
        self._drift_t = 0.0

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def numeric(self) -> bool:
        return self.spec.type not in ("bool", "string")

    def _drift(self, now: float) -> float:
        return self._drift_offset + self._drift_rate * (now - self._drift_t)

    def add_drift(self, per_second: float) -> None:
        with self.device.lock:
            now = self.device.clock()
            self._drift_offset = self._drift(now)
            self._drift_t = now
            self._drift_rate += per_second

    def clear_drift(self) -> None:
        with self.device.lock:
            self._drift_rate = self._drift_offset = 0.0

    def live(self) -> Any:
        """The current value, ignoring freeze."""
        if self.override is not None:
            value = self.override
        elif self.spec.model:
            value = self.device.model.get(self.spec.model)
        elif self.signal is not None:
            value = self.signal.value(self.device.clock())
        else:
            value = self.stored
        if self.numeric and (self._drift_rate or self._drift_offset):
            value = float(value) + self._drift(self.device.clock())
        return value

    def value(self) -> Any:
        frozen = self.device.frozen
        if frozen is not None and self.name in frozen:
            return frozen[self.name]
        return self.live()

    def words(self) -> list[int]:
        s = self.spec
        value = self.value()
        if s.type == "bool":
            return [1 if value else 0]
        return dt.encode(value, s.type, s.word_order, s.scale, s.length)

    def check(self, value: Any) -> Any:
        """Validate a value written over Modbus; raises ModbusException(03)."""
        s = self.spec
        if s.type in ("bool", "string"):
            return value
        if isinstance(value, float) and math.isnan(value):
            raise ModbusException(frame.ILLEGAL_DATA_VALUE, f"{s.name}: NaN")
        if (s.min is not None and value < s.min) or (s.max is not None and value > s.max):
            raise ModbusException(frame.ILLEGAL_DATA_VALUE,
                                  f"{s.name}: {value} outside {s.min}..{s.max}")
        return value

    def assign(self, value: Any) -> None:
        """Store a value written over Modbus (already checked)."""
        self.override = None
        if self.spec.model:
            try:
                self.device.model.set(self.spec.model, value)
            except ModelError as exc:
                raise ModbusException(exc.code, str(exc)) from None
        elif self.signal is not None:
            self.override = value  # a written value holds until the control channel resets it
        else:
            self.stored = value
        frozen = self.device.frozen
        if frozen is not None:
            frozen[self.name] = self.live()


class Device:
    def __init__(self, profile: Profile, unit: int = 1, name: str | None = None,
                 clock: Callable[[], float] = time.monotonic, seed: int | None = None):
        self.profile = profile
        self.unit = unit
        self.name = name or profile.name
        self.clock = clock
        self.lock = threading.RLock()
        self.model: Model | None = (make_model(profile.model, profile.model_params, clock,
                                               seed) if profile.model else None)
        self.points: dict[str, Point] = {}
        self.tables: dict[str, dict[int, tuple[Point, int]]] = {
            t: {} for t in ("coil", "discrete", "holding", "input")}
        for index, spec in enumerate(profile.points):
            if spec.model:
                if spec.model not in self.model.VARIABLES:
                    raise ProfileError(
                        f"point {spec.name!r}: model {profile.model} has no variable "
                        f"{spec.model!r} (has: {', '.join(self.model.VARIABLES)})")
                if spec.writable and not self.model.VARIABLES[spec.model]:
                    raise ProfileError(f"point {spec.name!r}: model variable "
                                       f"{spec.model!r} is read-only, make the point ro")
            point = Point(spec, self, None if seed is None else seed + index)
            self.points[spec.name] = point
            for offset in range(spec.count):
                self.tables[spec.table][spec.address + offset] = (point, offset)
        self.faults = Faults(self)
        self.frozen: dict[str, Any] | None = None
        self.requests = 0  # requests addressed to this device (for the control channel)

    # --- freeze ---

    def freeze(self) -> None:
        with self.lock:
            self.frozen = None
            snapshot = {name: point.live() for name, point in self.points.items()}
            self.frozen = snapshot

    def unfreeze(self) -> None:
        with self.lock:
            self.frozen = None

    # --- request handling ---

    def handle(self, pdu: bytes) -> bytes:
        if not pdu:
            return bytes([0x80, frame.ILLEGAL_FUNCTION])
        function = pdu[0]
        try:
            busy = self.faults.take("busy")
            if busy is not None:
                raise ModbusException(frame.DEVICE_BUSY)
            injected = self.faults.take("exception", function)
            if injected is not None:
                raise ModbusException(injected["code"])
            handler = _HANDLERS.get(function)
            if handler is None:
                raise ModbusException(frame.ILLEGAL_FUNCTION)
            with self.lock:
                return handler(self, pdu)
        except ModbusException as exc:
            return bytes([(function | 0x80) & 0xFF, exc.code])

    # --- table access ---

    def _lookup(self, table: str, address: int, count: int) -> list[tuple[Point, int] | None]:
        if address + count > 0x10000:
            raise ModbusException(frame.ILLEGAL_DATA_ADDRESS)
        cells = [self.tables[table].get(a) for a in range(address, address + count)]
        if self.profile.gaps == "error" and any(cell is None for cell in cells):
            raise ModbusException(frame.ILLEGAL_DATA_ADDRESS)
        return cells

    def read_bits(self, table: str, address: int, count: int) -> list[bool]:
        return [bool(cell[0].value()) if cell else False
                for cell in self._lookup(table, address, count)]

    def read_registers(self, table: str, address: int, count: int) -> list[int]:
        cells = self._lookup(table, address, count)
        cache: dict[str, list[int]] = {}
        result = []
        for cell in cells:
            if cell is None:
                result.append(0)
                continue
            point, offset = cell
            if point.name not in cache:
                cache[point.name] = point.words()
            result.append(cache[point.name][offset])
        return result

    def _writable(self, table: str, address: int, count: int) -> list[tuple[Point, int]]:
        if address + count > 0x10000:
            raise ModbusException(frame.ILLEGAL_DATA_ADDRESS)
        cells = [self.tables[table].get(a) for a in range(address, address + count)]
        for cell in cells:
            if cell is None or not cell[0].spec.writable:
                raise ModbusException(frame.ILLEGAL_DATA_ADDRESS)
        return cells

    def write_bits(self, address: int, values: list[bool]) -> None:
        cells = self._writable("coil", address, len(values))
        for (point, _), value in zip(cells, values):
            point.assign(bool(value))

    def write_registers(self, address: int, words: list[int]) -> None:
        cells = self._writable("holding", address, len(words))
        pending: dict[str, tuple[Point, list[int | None]]] = {}
        for (point, offset), word in zip(cells, words):
            slot = pending.setdefault(point.name, (point, [None] * point.spec.count))
            slot[1][offset] = word
        updates = []
        for point, parts in pending.values():
            if any(part is None for part in parts):  # only part of a multi-register value
                raise ModbusException(frame.ILLEGAL_DATA_ADDRESS)
            s = point.spec
            value = dt.decode(parts, s.type, s.word_order, s.scale)
            updates.append((point, point.check(value)))
        for point, value in updates:
            point.assign(value)

    def exception_status(self) -> int:
        point = self.points.get("exception_status")
        if point is None:
            return 0
        return point.words()[0] & 0xFF

    def identification(self) -> dict[int, bytes]:
        ident = self.profile.identification
        return {i: ident[key].encode("latin-1", "replace")[:245]
                for i, key in enumerate(_ID_OBJECTS) if key in ident}


# --- function code handlers ----------------------------------------------------------

def _expect(pdu: bytes, length: int) -> None:
    if len(pdu) != length:
        raise ModbusException(frame.ILLEGAL_DATA_VALUE)


def _read_bits(device: Device, pdu: bytes) -> bytes:
    _expect(pdu, 5)
    address, count = struct.unpack(">HH", pdu[1:5])
    if not 1 <= count <= MAX_READ_BITS:
        raise ModbusException(frame.ILLEGAL_DATA_VALUE)
    table = "coil" if pdu[0] == 1 else "discrete"
    bits = device.read_bits(table, address, count)
    data = bytearray((count + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            data[i // 8] |= 1 << (i % 8)
    return bytes([pdu[0], len(data)]) + bytes(data)


def _read_registers(device: Device, pdu: bytes) -> bytes:
    _expect(pdu, 5)
    address, count = struct.unpack(">HH", pdu[1:5])
    if not 1 <= count <= MAX_READ_REGS:
        raise ModbusException(frame.ILLEGAL_DATA_VALUE)
    table = "holding" if pdu[0] == 3 else "input"
    words = device.read_registers(table, address, count)
    return bytes([pdu[0], 2 * count]) + struct.pack(f">{count}H", *words)


def _write_coil(device: Device, pdu: bytes) -> bytes:
    _expect(pdu, 5)
    address, value = struct.unpack(">HH", pdu[1:5])
    if value not in (0x0000, 0xFF00):
        raise ModbusException(frame.ILLEGAL_DATA_VALUE)
    device.write_bits(address, [value == 0xFF00])
    return pdu


def _write_register(device: Device, pdu: bytes) -> bytes:
    _expect(pdu, 5)
    address, value = struct.unpack(">HH", pdu[1:5])
    device.write_registers(address, [value])
    return pdu


def _read_exception_status(device: Device, pdu: bytes) -> bytes:
    _expect(pdu, 1)
    return bytes([0x07, device.exception_status()])


def _diagnostics(device: Device, pdu: bytes) -> bytes:
    if len(pdu) < 3:
        raise ModbusException(frame.ILLEGAL_DATA_VALUE)
    if struct.unpack(">H", pdu[1:3])[0] != 0x0000:  # only "return query data"
        raise ModbusException(frame.ILLEGAL_FUNCTION)
    return pdu


def _write_coils(device: Device, pdu: bytes) -> bytes:
    if len(pdu) < 6:
        raise ModbusException(frame.ILLEGAL_DATA_VALUE)
    address, count = struct.unpack(">HH", pdu[1:5])
    byte_count = pdu[5]
    if not 1 <= count <= MAX_WRITE_BITS or byte_count != (count + 7) // 8 \
            or len(pdu) != 6 + byte_count:
        raise ModbusException(frame.ILLEGAL_DATA_VALUE)
    bits = [bool(pdu[6 + i // 8] >> (i % 8) & 1) for i in range(count)]
    device.write_bits(address, bits)
    return pdu[:5]


def _write_registers(device: Device, pdu: bytes) -> bytes:
    if len(pdu) < 6:
        raise ModbusException(frame.ILLEGAL_DATA_VALUE)
    address, count = struct.unpack(">HH", pdu[1:5])
    byte_count = pdu[5]
    if not 1 <= count <= MAX_WRITE_REGS or byte_count != 2 * count \
            or len(pdu) != 6 + byte_count:
        raise ModbusException(frame.ILLEGAL_DATA_VALUE)
    device.write_registers(address, list(struct.unpack(f">{count}H", pdu[6:])))
    return pdu[:5]


def _mask_write(device: Device, pdu: bytes) -> bytes:
    _expect(pdu, 7)
    address, and_mask, or_mask = struct.unpack(">HHH", pdu[1:7])
    current = device.read_registers("holding", address, 1)[0] \
        if device.tables["holding"].get(address) else None
    if current is None:
        raise ModbusException(frame.ILLEGAL_DATA_ADDRESS)
    device.write_registers(address, [(current & and_mask) | (or_mask & ~and_mask & 0xFFFF)])
    return pdu


def _read_write_registers(device: Device, pdu: bytes) -> bytes:
    if len(pdu) < 10:
        raise ModbusException(frame.ILLEGAL_DATA_VALUE)
    r_address, r_count, w_address, w_count = struct.unpack(">HHHH", pdu[1:9])
    byte_count = pdu[9]
    if not 1 <= r_count <= MAX_READ_REGS or not 1 <= w_count <= MAX_RW_WRITE_REGS \
            or byte_count != 2 * w_count or len(pdu) != 10 + byte_count:
        raise ModbusException(frame.ILLEGAL_DATA_VALUE)
    device._lookup("holding", r_address, r_count)  # check the read before writing
    device.write_registers(w_address, list(struct.unpack(f">{w_count}H", pdu[10:])))
    words = device.read_registers("holding", r_address, r_count)
    return bytes([0x17, 2 * r_count]) + struct.pack(f">{r_count}H", *words)


def _device_identification(device: Device, pdu: bytes) -> bytes:
    if len(pdu) < 2:
        raise ModbusException(frame.ILLEGAL_DATA_VALUE)
    if pdu[1] != 0x0E:
        raise ModbusException(frame.ILLEGAL_FUNCTION)
    _expect(pdu, 4)
    code, object_id = pdu[2], pdu[3]
    objects = device.identification()
    conformity = 0x82  # regular identification, stream and individual access
    if code == 4:
        if object_id not in objects:
            raise ModbusException(frame.ILLEGAL_DATA_ADDRESS)
        chosen = [object_id]
    elif code in (1, 2, 3):
        last = 2 if code == 1 else 0x7F
        ids = [i for i in sorted(objects) if i <= last]
        if object_id not in ids:
            object_id = 0  # unknown start object: restart at the beginning
        chosen = [i for i in ids if i >= object_id]
    else:
        raise ModbusException(frame.ILLEGAL_DATA_VALUE)
    body = bytearray()
    more, next_id, sent = 0x00, 0x00, 0
    for i in chosen:
        item = bytes([i, len(objects[i])]) + objects[i]
        if 7 + len(body) + len(item) > frame.MAX_PDU:
            more, next_id = 0xFF, i
            break
        body += item
        sent += 1
    return bytes([0x2B, 0x0E, code, conformity, more, next_id, sent]) + bytes(body)


_HANDLERS: dict[int, Callable[[Device, bytes], bytes]] = {
    0x01: _read_bits, 0x02: _read_bits,
    0x03: _read_registers, 0x04: _read_registers,
    0x05: _write_coil, 0x06: _write_register,
    0x07: _read_exception_status, 0x08: _diagnostics,
    0x0F: _write_coils, 0x10: _write_registers,
    0x16: _mask_write, 0x17: _read_write_registers,
    0x2B: _device_identification,
}
FUNCTIONS = tuple(sorted(_HANDLERS))
BIT_TABLE_NAMES = BIT_TABLES
