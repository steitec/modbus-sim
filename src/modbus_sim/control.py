"""Control channel of a running bench: inject faults, set and read values from outside.

One JSON object per line in, one per line out. ``device`` is a device name or unit ID::

    {"device": "vfd", "inject": "trip", "params": {"code": 7}}     -> {"ok": true}
    {"device": "vfd", "clear": true}         (or "clear": "trip")  -> {"ok": true}
    {"device": 3, "set": {"speed_reference": 25.0}}  (null = back to model/source)
    {"device": "vfd", "get": ["output_frequency"]}   (or "get": true for all points)
        -> {"ok": true, "values": {"output_frequency": 12.5}}
    {"list": true} -> {"ok": true, "devices": {"vfd": {"unit": 3, "profile": "vfd",
                                                      "faults": {...}, "requests": 12}}}

Anything invalid gets ``{"ok": false, "error": "..."}``; the connection stays open.
"""
from __future__ import annotations

import json
import math
import os
import socket
import socketserver
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .bench import Bench
    from .device import Device, Point

CONTROL_PORT = 5021
_MAX_LINE = 65536


class _Handler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.server.connections.add(self.request)

    def finish(self) -> None:
        self.server.connections.discard(self.request)
        super().finish()

    def handle(self) -> None:
        try:
            while True:
                raw = self.rfile.readline(_MAX_LINE)
                if not raw:
                    return
                if len(raw) >= _MAX_LINE and not raw.endswith(b"\n"):
                    self.wfile.write(json.dumps({"ok": False, "error": "line too long"})
                                     .encode() + b"\n")
                    self.wfile.flush()
                    self.request.shutdown(socket.SHUT_WR)
                    return
                if not raw.strip():
                    continue
                answer = self.server.control.handle(raw)
                self.wfile.write(json.dumps(answer).encode() + b"\n")
        except OSError:
            return


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, handler):
        self.address_family = socket.getaddrinfo(address[0], address[1],
                                                 type=socket.SOCK_STREAM)[0][0]
        self.connections: set[socket.socket] = set()
        super().__init__(address, handler)


class ControlServer:
    def __init__(self, bench: "Bench", host: str = "127.0.0.1", port: int = CONTROL_PORT):
        self.bench, self.host, self.port = bench, host, port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        self._server = _Server((self.host, self.port), _Handler)
        self._server.control = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True,
                                        name="modbus-sim-control")
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        for connection in list(self._server.connections):
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._thread.join(timeout=5)
        self._server = self._thread = None

    def handle(self, raw: bytes | str) -> dict[str, Any]:
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("a request is a JSON object")
            return self._handle(request)
        except ValueError as error:  # includes JSONDecodeError and FaultError
            return {"ok": False, "error": str(error)}
        except Exception as error:  # never drop the connection with a traceback
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    def _handle(self, request: dict) -> dict[str, Any]:
        if request.get("list"):
            return {"ok": True, "devices": {name: self._describe(dev)
                                            for name, dev in self.bench.devices.items()}}
        device = self.bench.find(request.get("device"))
        if request.get("clear"):
            kind = request["clear"]
            if kind is True:
                device.faults.clear()
            elif isinstance(kind, str):
                device.faults.clear(kind)
            else:
                raise ValueError("'clear' must be true or a fault name")
        elif "inject" in request:
            kind, params = request["inject"], request.get("params") or {}
            if not isinstance(kind, str):
                raise ValueError("'inject' must be a fault name")
            if not isinstance(params, dict):
                raise ValueError("'params' must be an object")
            device.faults.inject(kind, **params)
        elif "set" in request:
            self._set(device, request["set"])
        elif "get" in request:
            return {"ok": True, "values": self._get(device, request["get"])}
        else:
            raise ValueError("a request needs 'list', 'clear', 'inject', 'set' or 'get'")
        return {"ok": True}

    @staticmethod
    def _point(device: "Device", name: Any) -> "Point":
        point = device.points.get(name) if isinstance(name, str) else None
        if point is None:
            raise ValueError(f"{device.name} has no point {name!r} "
                             f"(points: {', '.join(device.points)})")
        return point

    def _set(self, device: "Device", values: Any) -> None:
        if not isinstance(values, dict) or not values:
            raise ValueError("'set' must be a non-empty object like {\"setpoint\": 80}")
        parsed = []
        for name, value in values.items():
            point = self._point(device, name)
            kind = point.spec.type
            if value is None:
                pass
            elif kind == "string":
                if not isinstance(value, str):
                    raise ValueError(f"{name}: expected a string")
            elif kind == "bool":
                if not isinstance(value, bool):
                    raise ValueError(f"{name}: expected true or false")
            elif isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value):
                raise ValueError(f"{name}: expected a finite number or null")
            parsed.append((point, value))
        with device.lock:  # all or nothing, atomic against the Modbus side
            for point, value in parsed:
                if point.spec.model or point.signal is not None:
                    point.override = value
                elif value is not None:
                    point.stored = value
                if device.frozen is not None:
                    device.frozen[point.name] = point.live()

    def _get(self, device: "Device", names: Any) -> dict[str, Any]:
        if names is True:
            names = list(device.points)
        if not isinstance(names, list):
            raise ValueError("'get' must be true or a list of point names")
        with device.lock:
            result = {}
            for name in names:
                value = self._point(device, name).value()
                if isinstance(value, float) and not math.isfinite(value):
                    value = str(value)  # JSON has no inf/nan
                result[name] = value
            return result

    @staticmethod
    def _describe(device: "Device") -> dict[str, Any]:
        return {"unit": device.unit, "profile": device.profile.name,
                "faults": device.faults.snapshot(), "requests": device.requests}


def send_control(request: dict, host: str = "127.0.0.1", port: int = CONTROL_PORT,
                 timeout: float = 2.0) -> dict:
    """Send one request to a bench's control port; raises OSError if it cannot be reached."""
    with socket.create_connection((host, port), timeout=timeout) as sock, \
            sock.makefile("rwb") as stream:
        stream.write(json.dumps(request).encode() + b"\n")
        stream.flush()
        line = stream.readline()
    if not line:
        raise OSError("control port closed the connection")
    return json.loads(line)
