"""A small Modbus master for trying the simulator out (and for its own CLI).

::

    from modbus_sim.client import ModbusClient
    with ModbusClient("127.0.0.1", 5020) as c:
        c.read_holding_registers(100, 2, unit=3)        # -> [status, frequency]
        c.write_register(0, 1, unit=3)

``ModbusClient.serial("/dev/pts/7")`` talks RTU on a serial line or pty instead, and
``framing="rtu"`` speaks RTU over TCP. It is deliberately minimal: one request at a
time, no retries. For real projects use pymodbus or similar.
"""
from __future__ import annotations

import os
import select
import socket
import struct
import sys
import time

from . import frame

EXCEPTIONS = {
    1: "illegal function", 2: "illegal data address", 3: "illegal data value",
    4: "server device failure", 5: "acknowledge", 6: "server device busy",
    8: "memory parity error", 10: "gateway path unavailable",
    11: "gateway target device failed to respond",
}


class ModbusError(Exception):
    """The device answered with an exception code."""

    def __init__(self, function: int, code: int):
        self.function, self.code = function, code
        super().__init__(f"function {function:#04x}: exception {code:#04x} "
                         f"({EXCEPTIONS.get(code, 'unknown')})")


class ProtocolError(Exception):
    """The answer did not fit the request (wrong transaction, unit, CRC, length)."""


class ModbusClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 5020, timeout: float = 2.0,
                 framing: str = "tcp"):
        if framing not in ("tcp", "rtu"):
            raise ValueError("framing is tcp or rtu")
        self.host, self.port, self.timeout, self.framing = host, port, timeout, framing
        self._sock: socket.socket | None = None
        self._fd: int | None = None
        self._serial = None
        self._transaction = 0

    @classmethod
    def serial(cls, path: str, baudrate: int = 19200, parity: str = "E",
               timeout: float = 2.0) -> "ModbusClient":
        client = cls(timeout=timeout, framing="rtu")
        client.host, client.port = path, None
        client._serial_args = (baudrate, parity)
        return client

    # --- connection ---

    def connect(self) -> "ModbusClient":
        if self.port is None:
            self._open_serial(self.host, *self._serial_args)
        else:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return self

    def _open_serial(self, path: str, baudrate: int, parity: str) -> None:
        if is_pty(path):
            import tty
            self._fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
            tty.setraw(self._fd)
            return
        try:
            import serial  # type: ignore[import-untyped]
        except ImportError:
            raise OSError("a serial port needs pyserial: pip install 'modbus-sim[serial]'"
                          ) from None
        self._serial = serial.Serial(path, baudrate, parity=parity, timeout=self.timeout)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def __enter__(self) -> "ModbusClient":
        if self._sock is None and self._fd is None and self._serial is None:
            self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- raw I/O ---

    def _send(self, data: bytes) -> None:
        if self._sock is not None:
            self._sock.sendall(data)
        elif self._fd is not None:
            os.write(self._fd, data)
        elif self._serial is not None:
            self._serial.write(data)
        else:
            raise OSError("not connected")

    def _recv(self, n: int, deadline: float) -> bytes:
        data = b""
        while len(data) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("no (complete) answer")
            if self._sock is not None:
                self._sock.settimeout(left)
                try:
                    chunk = self._sock.recv(n - len(data))
                except socket.timeout:
                    raise TimeoutError("no (complete) answer") from None
                if not chunk:
                    raise ConnectionError("connection closed by the device")
            elif self._fd is not None:
                ready, _, _ = select.select([self._fd], [], [], left)
                chunk = os.read(self._fd, n - len(data)) if ready else b""
            else:
                self._serial.timeout = left
                chunk = self._serial.read(n - len(data))
            data += chunk
        return data

    def request(self, pdu: bytes, unit: int = 1) -> bytes:
        """Send a request PDU, return the response PDU; raises ModbusError on an exception."""
        deadline = time.monotonic() + self.timeout
        if self.framing == "tcp":
            self._transaction = (self._transaction + 1) & 0xFFFF
            self._send(frame.mbap(self._transaction, unit, pdu))
            transaction, protocol, length, answered = frame.parse_mbap(
                self._recv(frame.MBAP_LEN, deadline))
            body = self._recv(length - 1, deadline)
            if transaction != self._transaction or protocol != 0:
                raise ProtocolError(f"answer has transaction {transaction}, "
                                    f"expected {self._transaction}")
            if answered != unit:
                raise ProtocolError(f"answer from unit {answered}, expected {unit}")
        else:
            self._discard_input()
            self._send(frame.add_crc(bytes([unit]) + pdu))
            if unit == 0:
                return b""  # broadcast: nobody answers
            packet = self._recv(3, deadline)
            total = frame.response_length(packet)
            if total is None:
                raise ProtocolError(f"cannot frame the answer {packet.hex()}")
            packet += self._recv(total - 3, deadline)
            if not frame.check_crc(packet):
                raise ProtocolError("CRC error in the answer")
            if packet[0] != unit:
                raise ProtocolError(f"answer from unit {packet[0]}, expected {unit}")
            body = packet[1:-2]
        if body and body[0] == (pdu[0] | 0x80):
            raise ModbusError(pdu[0], body[1] if len(body) > 1 else 0)
        if not body or body[0] != pdu[0]:
            raise ProtocolError(f"unexpected answer {body.hex()}")
        return body

    def _discard_input(self) -> None:
        """Drop left-overs of an earlier, broken answer before a new RTU request."""
        if self._sock is not None:
            self._sock.setblocking(False)
            try:
                while self._sock.recv(512):
                    pass
            except (BlockingIOError, OSError):
                pass
            finally:
                self._sock.setblocking(True)
        elif self._fd is not None:
            while select.select([self._fd], [], [], 0)[0]:
                if not os.read(self._fd, 512):
                    break
        elif self._serial is not None:
            self._serial.reset_input_buffer()

    # --- function codes ---

    def _read_bits(self, function: int, address: int, count: int, unit: int) -> list[bool]:
        body = self.request(struct.pack(">BHH", function, address, count), unit)
        data = body[2:]
        return [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]

    def read_coils(self, address: int, count: int = 1, unit: int = 1) -> list[bool]:
        return self._read_bits(1, address, count, unit)

    def read_discrete_inputs(self, address: int, count: int = 1, unit: int = 1) -> list[bool]:
        return self._read_bits(2, address, count, unit)

    def _read_regs(self, function: int, address: int, count: int, unit: int) -> list[int]:
        body = self.request(struct.pack(">BHH", function, address, count), unit)
        if body[1] != 2 * count or len(body) != 2 + 2 * count:
            raise ProtocolError(f"expected {count} registers, got {body[1] // 2}")
        return list(struct.unpack(f">{count}H", body[2:]))

    def read_holding_registers(self, address: int, count: int = 1, unit: int = 1) -> list[int]:
        return self._read_regs(3, address, count, unit)

    def read_input_registers(self, address: int, count: int = 1, unit: int = 1) -> list[int]:
        return self._read_regs(4, address, count, unit)

    def write_coil(self, address: int, value: bool, unit: int = 1) -> None:
        self.request(struct.pack(">BHH", 5, address, 0xFF00 if value else 0), unit)

    def write_register(self, address: int, value: int, unit: int = 1) -> None:
        self.request(struct.pack(">BHH", 6, address, value & 0xFFFF), unit)

    def write_coils(self, address: int, values: list[bool], unit: int = 1) -> None:
        data = bytearray((len(values) + 7) // 8)
        for i, value in enumerate(values):
            if value:
                data[i // 8] |= 1 << (i % 8)
        self.request(struct.pack(">BHHB", 15, address, len(values), len(data)) + bytes(data),
                     unit)

    def write_registers(self, address: int, values: list[int], unit: int = 1) -> None:
        self.request(struct.pack(f">BHHB{len(values)}H", 16, address, len(values),
                                 2 * len(values), *[v & 0xFFFF for v in values]), unit)

    def read_device_identification(self, unit: int = 1, code: int = 2) -> dict[int, str]:
        """Objects 0.. (vendor, product code, revision, ...) as {id: text}."""
        result: dict[int, str] = {}
        object_id = 0
        while True:
            body = self.request(bytes([0x2B, 0x0E, code, object_id]), unit)
            more, next_id, count = body[4], body[5], body[6]
            offset = 7
            for _ in range(count):
                oid, length = body[offset], body[offset + 1]
                result[oid] = body[offset + 2:offset + 2 + length].decode("latin-1")
                offset += 2 + length
            if more != 0xFF:
                return result
            object_id = next_id


def is_pty(path: str) -> bool:
    return sys.platform != "win32" and (path.startswith("/dev/pts/")
                                        or path.startswith("/dev/ttys"))
