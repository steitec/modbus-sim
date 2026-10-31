"""Bench: several simulated devices (unit IDs) behind one server, from a YAML file.

::

    devices:
      - {profile: energy-meter, unit: 1, name: meter}
      - {profile: temperature-controller, unit: 2, name: oven}
      - {profile: vfd, unit: 3, name: pump, model_params: {accel_time: 5}}

Besides ``profile``, ``unit`` and ``name``, an entry takes the profile overrides
``word_order``, ``gaps``, ``model_params``, ``identification`` and ``values``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import yaml

from .control import CONTROL_PORT, ControlServer
from .device import Device
from .profile import ProfileError, bundled_profiles, load_profile
from .server import TCP_PORT, SerialServer, TcpServer

_ENTRY_KEYS = {"profile", "unit", "name"}


class BenchError(ValueError):
    """Invalid bench description; the message names the offending entry."""


class Bench:
    def __init__(self) -> None:
        self.devices: dict[str, Device] = {}
        self.server: TcpServer | SerialServer | None = None
        self.control: ControlServer | None = None
        self.control_port = 0

    # --- construction ---

    @classmethod
    def from_yaml(cls, path: str | Path, clock: Callable[[], float] = time.monotonic
                  ) -> "Bench":
        path = Path(path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise BenchError(f"{path}: {exc.strerror or exc}") from None
        except UnicodeDecodeError:
            raise BenchError(f"{path}: not a UTF-8 text file") from None
        except yaml.YAMLError as exc:
            raise BenchError(f"{path}: invalid YAML: {exc}") from None
        return cls.from_dict(data, base=path.parent, clock=clock)

    @classmethod
    def single(cls, profile: str, unit: int = 1,
               clock: Callable[[], float] = time.monotonic) -> "Bench":
        return cls.from_dict({"devices": [{"profile": profile, "unit": unit}]}, clock=clock)

    @classmethod
    def from_dict(cls, data: Any, base: Path | None = None,
                  clock: Callable[[], float] = time.monotonic) -> "Bench":
        if not isinstance(data, dict) or not isinstance(data.get("devices"), list) \
                or not data["devices"]:
            raise BenchError("a bench is a mapping with a non-empty 'devices' list")
        unknown = set(data) - {"devices"}
        if unknown:
            raise BenchError(f"unknown key {sorted(unknown)[0]!r} (allowed: devices)")
        bench = cls()
        units: dict[int, str] = {}
        entries = []
        for index, entry in enumerate(data["devices"], 1):
            where = f"devices[{index}]"
            if not isinstance(entry, dict):
                raise BenchError(f"{where}: expected a mapping")
            ref = entry.get("profile")
            if not isinstance(ref, str):
                raise BenchError(f"{where}: missing 'profile' (a bundled name or a file)")
            unit = entry.get("unit")
            if isinstance(unit, bool) or not isinstance(unit, int) or not 1 <= unit <= 247:
                raise BenchError(f"{where}: unit must be an integer 1-247, got {unit!r}")
            if unit in units:
                raise BenchError(f"{where}: unit {unit} is already used by {units[unit]}")
            name = entry.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                raise BenchError(f"{where}: name must be a non-empty string")
            if base is not None and ref not in bundled_profiles():
                candidate = base / ref
                if candidate.is_file():
                    ref = str(candidate)
            overrides = {k: v for k, v in entry.items() if k not in _ENTRY_KEYS}
            try:
                profile = load_profile(ref, overrides)
            except ProfileError as exc:
                raise BenchError(f"{where}: {exc}") from None
            units[unit] = name or profile.name
            entries.append((where, profile, unit, name))
        names = [p.name for _, p, _, n in entries if n is None]
        for where, profile, unit, name in entries:
            if name is None:
                name = profile.name if names.count(profile.name) == 1 else \
                    f"{profile.name}-{unit}"
            if name in bench.devices:
                raise BenchError(f"{where}: duplicate name {name!r}")
            try:
                bench.devices[name] = Device(profile, unit=unit, name=name, clock=clock)
            except ProfileError as exc:
                raise BenchError(f"{where}: {exc}") from None
        return bench

    def find(self, ref: Any) -> Device:
        """A device by name or unit ID."""
        if isinstance(ref, str) and ref in self.devices:
            return self.devices[ref]
        if isinstance(ref, str) and ref.isdigit():
            ref = int(ref)
        if isinstance(ref, int) and not isinstance(ref, bool):
            for device in self.devices.values():
                if device.unit == ref:
                    return device
        raise ValueError(f"unknown device {ref!r} (bench has: "
                         + ", ".join(f"{n} (unit {d.unit})" for n, d in self.devices.items())
                         + ")")

    # --- lifecycle ---

    def start(self, host: str = "127.0.0.1", port: int = TCP_PORT, framing: str = "tcp",
              serial: str | None = None, pty: bool = False, baudrate: int = 19200,
              parity: str = "E", stopbits: int = 1, control_port: int | None = CONTROL_PORT,
              control_host: str = "127.0.0.1") -> None:
        """Start the Modbus server and the control channel; port 0 picks a free port.

        `framing` is "tcp" or "rtu" (RTU over TCP). With `pty=True` or a `serial`
        port the devices speak RTU on that line instead of TCP. The control channel
        has no authentication, so it stays on loopback unless `control_host` says
        otherwise; `control_port=None` leaves it off.
        """
        if self.server is not None:
            raise BenchError("bench is already running")
        devices = list(self.devices.values())
        if pty or serial:
            server: TcpServer | SerialServer = SerialServer(
                devices, port=serial, baudrate=baudrate, parity=parity, stopbits=stopbits)
        else:
            server = TcpServer(devices, host=host, port=port, framing=framing)
        server.start()
        self.server = server
        if control_port is not None:
            control = ControlServer(self, host=control_host, port=control_port)
            try:
                self.control_port = control.start()
            except OSError:
                server.stop()
                self.server = None
                raise
            self.control = control

    def stop(self) -> None:
        if self.control is not None:
            self.control.stop()
            self.control = None
        if self.server is not None:
            self.server.stop()
            self.server = None

    def __enter__(self) -> "Bench":
        if self.server is None:
            self.start(port=0, control_port=0)
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def port(self) -> int | None:
        return getattr(self.server, "port", None)

    @property
    def path(self) -> str | None:
        """The serial device of an RTU bench (e.g. /dev/pts/7)."""
        return getattr(self.server, "path", None)

    def client(self, timeout: float = 2.0):
        """A connected :class:`~modbus_sim.client.ModbusClient` for this bench."""
        from .client import ModbusClient
        if isinstance(self.server, SerialServer):
            return ModbusClient.serial(self.server.path, timeout=timeout).connect()
        if self.server is None:
            raise BenchError("bench is not running")
        host = self.server.host
        if host in ("0.0.0.0", ""):
            host = "127.0.0.1"
        elif host == "::":
            host = "::1"
        return ModbusClient(host, self.server.port, timeout=timeout,
                            framing=self.server.framing).connect()

    def inject(self, device: str | int, kind: str, **params: Any) -> None:
        self.find(device).faults.inject(kind, **params)

    def clear(self, device: str | int, kind: str | None = None) -> None:
        self.find(device).faults.clear(kind)

    def value(self, device: str | int, point: str) -> Any:
        dev = self.find(device)
        with dev.lock:
            return ControlServer._point(dev, point).value()

    def describe(self) -> str:
        units = ", ".join(str(d.unit) for d in self.devices.values())
        n = len(self.devices)
        text = f"{n} device{'s' if n != 1 else ''} (unit{'s' if n != 1 else ''} {units})"
        if self.server is not None:
            text += f" on {self.server.describe()}"
        if self.control is not None:
            text += f", control on :{self.control_port}"
        return text
