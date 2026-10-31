"""Modbus servers: TCP (MBAP), RTU over TCP, and RTU on a serial line or a pty.

All of them hand a request to :func:`dispatch`, which picks the device by unit ID,
applies the transport faults and asks the device for the answer.
"""
from __future__ import annotations

import os
import select
import socket
import socketserver
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Iterator

from . import frame
from .device import Device

TCP_PORT = 5020
_WRITE_FUNCTIONS = {0x05, 0x06, 0x0F, 0x10, 0x16}
_POLL = 0.2  # s; how often idle loops look at the stop flag


@dataclass
class Outcome:
    pdu: bytes | None          # None: no answer at all
    unit: int
    drop: bool = False
    delay: float = 0.0
    bad_crc: bool = False
    wrong_transaction: bool = False
    truncate: int = 0


def dispatch(devices: dict[int, Device], unit: int, pdu: bytes, framing: str) -> Outcome:
    """Answer one request. `framing` is "tcp", "rtu" (RTU over TCP) or "serial"."""
    if framing != "tcp" and unit == 0:            # serial broadcast: execute, never answer
        if pdu and pdu[0] in _WRITE_FUNCTIONS:
            for device in devices.values():
                device.requests += 1
                device.handle(pdu)
        return Outcome(None, unit)
    device = devices.get(unit)
    if device is None and framing == "tcp" and len(devices) == 1 and unit in (0, 0xFF):
        device = next(iter(devices.values()))     # a single TCP device answers 0 and 255
    if device is None:
        if framing == "tcp" and pdu:              # behave like a gateway
            return Outcome(bytes([(pdu[0] | 0x80) & 0xFF, frame.GATEWAY_TARGET_FAILED]), unit)
        return Outcome(None, unit)                # nobody on the bus answers
    device.requests += 1
    faults = device.faults
    if faults.take("no_response") is not None:
        return Outcome(None, unit)
    if faults.take("drop_connection") is not None:
        return Outcome(None, unit, drop=framing != "serial")
    delay = faults.take("delay")
    outcome = Outcome(device.handle(pdu), unit, delay=delay["seconds"] if delay else 0.0)
    if faults.take("wrong_unit") is not None:
        outcome.unit = unit % 247 + 1 if unit else 1
    if framing == "tcp" and faults.take("wrong_transaction") is not None:
        outcome.wrong_transaction = True
    if framing != "tcp" and faults.take("bad_crc") is not None:
        outcome.bad_crc = True
    truncate = faults.take("truncate")
    if truncate is not None:
        outcome.truncate = truncate["bytes"]
    return outcome


def rtu_packet(outcome: Outcome) -> bytes:
    packet = frame.add_crc(bytes([outcome.unit]) + outcome.pdu)
    if outcome.bad_crc:
        packet = packet[:-1] + bytes([packet[-1] ^ 0xFF])
    if outcome.truncate:
        packet = packet[:max(len(packet) - outcome.truncate, 0)]
    return packet


def rtu_frames(read: Callable[[float], bytes | None], gap: float,
               stopped: Callable[[], bool]) -> Iterator[bytes]:
    """Split a byte stream into RTU request frames (length from the function code,
    the silent gap for unknown function codes). `read(timeout)` returns b"" on timeout
    and None at the end of the stream."""
    buffer = b""
    while not stopped():
        need = frame.request_length(buffer)
        if need is not None and len(buffer) >= need:
            yield buffer[:need]
            buffer = buffer[need:]
            continue
        chunk = read(gap if buffer else _POLL)
        if chunk is None:
            return
        if not chunk:
            if buffer and need is None and len(buffer) >= 4:
                yield buffer                       # unknown function: the gap ends it
            buffer = b""                           # else: incomplete frame, discard
            continue
        buffer += chunk
        if len(buffer) > 512:
            buffer = b""                           # noise; wait for the next gap


def handle_rtu_frame(devices: dict[int, Device], packet: bytes, framing: str,
                     ) -> Outcome | None:
    if not frame.check_crc(packet):
        return None                                # a real slave ignores a bad frame
    return dispatch(devices, packet[0], packet[1:-2], framing)


# --- TCP ---------------------------------------------------------------------------

class _TcpHandler(socketserver.BaseRequestHandler):
    def setup(self) -> None:
        self.server.connections.add(self.request)

    def finish(self) -> None:
        self.server.connections.discard(self.request)

    def _recv_exact(self, n: int) -> bytes | None:
        data = b""
        while len(data) < n:
            try:
                chunk = self.request.recv(n - len(data))
            except OSError:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def _send(self, data: bytes, delay: float) -> bool:
        if delay and self.server.stopping.wait(delay):
            return False
        try:
            self.request.sendall(data)
        except OSError:
            return False
        return True

    def handle(self) -> None:
        if self.server.framing == "rtu":
            self._handle_rtu()
        else:
            self._handle_tcp()

    def _handle_tcp(self) -> None:
        devices = self.server.devices
        while True:
            header = self._recv_exact(frame.MBAP_LEN)
            if header is None:
                return
            transaction, protocol, length, unit = frame.parse_mbap(header)
            if not 2 <= length <= frame.MAX_PDU + 1:
                return                              # framing lost: close like most devices
            pdu = self._recv_exact(length - 1)
            if pdu is None:
                return
            if protocol != 0:
                continue                            # not Modbus: ignore
            outcome = dispatch(devices, unit, pdu, "tcp")
            if outcome.drop:
                return
            if outcome.pdu is None:
                continue
            if outcome.wrong_transaction:
                transaction = (transaction + 1) & 0xFFFF
            packet = frame.mbap(transaction, outcome.unit, outcome.pdu)
            if outcome.truncate:
                packet = packet[:max(len(packet) - outcome.truncate, 0)]
            if not self._send(packet, outcome.delay):
                return

    def _handle_rtu(self) -> None:
        sock = self.request

        def read(timeout: float) -> bytes | None:
            try:
                ready, _, _ = select.select([sock], [], [], timeout)
                if not ready:
                    return b""
                return sock.recv(512) or None
            except OSError:
                return None

        for packet in rtu_frames(read, 0.05, self.server.stopping.is_set):
            outcome = handle_rtu_frame(self.server.devices, packet, "rtu")
            if outcome is None:
                continue
            if outcome.drop:
                return
            if outcome.pdu is not None and not self._send(rtu_packet(outcome), outcome.delay):
                return


class _TcpServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, devices, framing):
        self.address_family = socket.getaddrinfo(address[0], address[1],
                                                 type=socket.SOCK_STREAM)[0][0]
        self.devices = devices
        self.framing = framing
        self.connections: set[socket.socket] = set()
        self.stopping = threading.Event()
        super().__init__(address, _TcpHandler)


class TcpServer:
    """Modbus TCP (``framing="tcp"``) or RTU over TCP (``framing="rtu"``)."""

    def __init__(self, devices: list[Device], host: str = "127.0.0.1", port: int = TCP_PORT,
                 framing: str = "tcp"):
        if framing not in ("tcp", "rtu"):
            raise ValueError("framing is tcp or rtu")
        self.devices = {d.unit: d for d in devices}
        self.host, self.port, self.framing = host, port, framing
        self._server: _TcpServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        self._server = _TcpServer((self.host, self.port), self.devices, self.framing)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True,
                                        name="modbus-sim-tcp")
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.stopping.set()
        self._server.shutdown()
        self._server.server_close()
        for connection in list(self._server.connections):
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._thread.join(timeout=5)
        self._server = self._thread = None

    def describe(self) -> str:
        kind = "modbus tcp" if self.framing == "tcp" else "rtu over tcp"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{kind} {host}:{self.port}"


# --- serial ------------------------------------------------------------------------

PARITIES = {"N": "none", "E": "even", "O": "odd"}


class SerialServer:
    """RTU on a serial port (needs pyserial) or on a new pseudo terminal (POSIX only).

    With ``port=None`` a pty is created; :attr:`path` is the device a master opens
    (e.g. ``/dev/pts/7``). Baud rate and parity only matter on a real port, and for
    the 3.5-character gap.
    """

    def __init__(self, devices: list[Device], port: str | None = None,
                 baudrate: int = 19200, parity: str = "E", stopbits: int = 1):
        if parity not in PARITIES:
            raise ValueError("parity is N, E or O")
        self.devices = {d.unit: d for d in devices}
        self.port, self.baudrate, self.parity, self.stopbits = port, baudrate, parity, stopbits
        self.path: str | None = port
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._serial = None
        self._master: int | None = None
        self._slave: int | None = None

    def start(self) -> str:
        if self.port is None:
            self._open_pty()
        else:
            self._open_serial()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="modbus-sim-rtu")
        self._thread.start()
        return self.path

    def _open_pty(self) -> None:
        if sys.platform == "win32":
            raise OSError("a virtual serial port (pty) needs Linux or macOS; on Windows use "
                          "--serial COMx with a virtual null-modem pair (com0com)")
        import tty
        master, slave = os.openpty()
        tty.setraw(slave)
        tty.setraw(master)
        self._master, self._slave = master, slave
        self.path = os.ttyname(slave)

    def _open_serial(self) -> None:
        try:
            import serial  # type: ignore[import-untyped]
        except ImportError:
            raise OSError("a real serial port needs pyserial: pip install 'modbus-sim[serial]'"
                          ) from None
        parity = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN,
                  "O": serial.PARITY_ODD}[self.parity]
        try:
            self._serial = serial.Serial(self.port, self.baudrate, parity=parity,
                                         stopbits=self.stopbits, bytesize=8, timeout=0)
        except serial.SerialException as exc:
            raise OSError(str(exc)) from None

    def _read(self, timeout: float) -> bytes | None:
        if self._serial is not None:
            self._serial.timeout = timeout
            try:
                first = self._serial.read(1)
                if not first:
                    return b""
                return first + self._serial.read(self._serial.in_waiting)
            except Exception:  # pyserial raises its own exception types
                return None
        try:
            ready, _, _ = select.select([self._master], [], [], timeout)
            if not ready:
                return b""
            return os.read(self._master, 512)
        except OSError:
            return None

    def _write(self, data: bytes) -> None:
        try:
            if self._serial is not None:
                self._serial.write(data)
            else:
                os.write(self._master, data)
        except Exception:
            pass

    def _run(self) -> None:
        gap = max(frame.t35(self.baudrate), 0.005 if self._serial is None else 0)
        for packet in rtu_frames(self._read, gap, self._stop.is_set):
            outcome = handle_rtu_frame(self.devices, packet, "serial")
            if outcome is None or outcome.pdu is None:
                continue
            if outcome.delay and self._stop.wait(outcome.delay):
                return
            self._write(rtu_packet(outcome))

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._serial is not None:
            self._serial.close()
            self._serial = None
        for fd in (self._master, self._slave):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._master = self._slave = None

    def describe(self) -> str:
        return f"serial RTU {self.path} ({self.baudrate} 8{self.parity}{self.stopbits})"

