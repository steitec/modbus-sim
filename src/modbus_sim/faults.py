"""Faults that can be injected into a running simulated device.

=================  ==================================  ==========================================
kind               parameters                          effect
=================  ==================================  ==========================================
no_response        count=1 (null = until cleared)      requests get no answer (master times out)
delay              seconds (> 0), count=null           answers are sent ``seconds`` late
exception          code (1-255), function=null,        answers are exception ``code``, optionally
                   count=1                             only for one function code
busy               count=1                             exception 06 (server device busy)
drop_connection    count=1                             TCP: the connection is closed instead of
                                                       answering (serial: no answer)
bad_crc            count=1                             RTU: the answer carries a wrong CRC
wrong_transaction  count=1                             TCP: the answer has another transaction ID
wrong_unit         count=1                             the answer comes from another unit ID
truncate           bytes=1, count=1                    the answer is cut short by ``bytes``
freeze             -                                   all values stop changing until cleared
drift              point, per_second                   a numeric point runs away; adds up
trip               code=1                              vfd: fault trip, latched until reset
sensor_break       -                                   temperature controller: sensor broken
phase_loss         phase (1-3)                         energy meter: phase lost
=================  ==================================  ==========================================

``count`` faults wear off after that many requests to the device; the others stay
until cleared.
"""
from __future__ import annotations

import math
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .device import Device

TRANSPORT = ("no_response", "delay", "drop_connection", "bad_crc", "wrong_transaction",
             "wrong_unit", "truncate")
_COUNTED = {"no_response": 1, "delay": None, "exception": 1, "busy": 1,
            "drop_connection": 1, "bad_crc": 1, "wrong_transaction": 1, "wrong_unit": 1,
            "truncate": 1}


class FaultError(ValueError):
    """Invalid fault kind or parameter."""


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise FaultError(f"{what} must be a finite number, got {value!r}")
    return float(value)


def _whole(value: Any, what: str, low: int, high: int) -> int:
    if isinstance(value, str):
        try:
            value = int(value.strip(), 0)
        except ValueError:
            raise FaultError(f"{what} must be a whole number, got {value!r}") from None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise FaultError(f"{what} must be a whole number {low}-{high}, got {value!r}")
    return value


class Faults:
    def __init__(self, device: "Device"):
        self.device = device
        self.lock = threading.Lock()
        self.counted: dict[str, dict[str, Any]] = {}
        self.drifts: dict[str, float] = {}

    def kinds(self) -> list[str]:
        model = self.device.model
        extra = list(model.FAULTS) if model else []
        return [*_COUNTED, "freeze", "drift", *extra]

    def inject(self, kind: str, **params: Any) -> None:
        if kind in _COUNTED:
            self._inject_counted(kind, params)
        elif kind == "freeze":
            self._no_params(kind, params)
            self.device.freeze()
        elif kind == "drift":
            self._inject_drift(params)
        elif self.device.model and kind in self.device.model.FAULTS:
            self.device.model.inject(kind, self._model_params(kind, params))
        else:
            raise FaultError(f"unknown fault {kind!r} for {self.device.name} "
                             f"(known: {', '.join(self.kinds())})")

    @staticmethod
    def _no_params(kind: str, params: dict) -> None:
        if params:
            raise FaultError(f"{kind} takes no parameters")

    def _inject_counted(self, kind: str, params: dict) -> None:
        allowed = {"count"} | {"delay": {"seconds"}, "exception": {"code", "function"},
                               "truncate": {"bytes"}}.get(kind, set())
        unknown = set(params) - allowed
        if unknown:
            raise FaultError(f"{kind}: unknown parameter {sorted(unknown)[0]!r} "
                             f"(allowed: {', '.join(sorted(allowed))})")
        entry: dict[str, Any] = {}
        count = params.get("count", _COUNTED[kind])
        entry["count"] = None if count is None else _whole(count, "count", 1, 1_000_000)
        if kind == "delay":
            if "seconds" not in params:
                raise FaultError("delay needs seconds")
            seconds = _number(params["seconds"], "seconds")
            if not 0 < seconds <= 3600:
                raise FaultError("seconds must be > 0 and <= 3600")
            entry["seconds"] = seconds
        elif kind == "exception":
            if "code" not in params:
                raise FaultError("exception needs a code (e.g. 4 = server device failure)")
            entry["code"] = _whole(params["code"], "code", 1, 255)
            function = params.get("function")
            entry["function"] = None if function is None else _whole(function, "function",
                                                                       1, 127)
        elif kind == "truncate":
            entry["bytes"] = _whole(params.get("bytes", 1), "bytes", 1, 260)
        with self.lock:
            self.counted[kind] = entry

    def _inject_drift(self, params: dict) -> None:
        unknown = set(params) - {"point", "per_second"}
        if unknown or "point" not in params or "per_second" not in params:
            raise FaultError("drift needs point and per_second")
        name = params["point"]
        point = self.device.points.get(name) if isinstance(name, str) else None
        if point is None or not point.numeric:
            raise FaultError(f"drift: {name!r} is not a numeric point of {self.device.name}")
        rate = _number(params["per_second"], "per_second")
        point.add_drift(rate)
        with self.lock:
            self.drifts[name] = self.drifts.get(name, 0.0) + rate

    def _model_params(self, kind: str, params: dict) -> dict:
        allowed = self.device.model.FAULTS[kind]
        unknown = set(params) - set(allowed)
        if unknown:
            raise FaultError(f"{kind}: unknown parameter {sorted(unknown)[0]!r}"
                             + (f" (allowed: {', '.join(allowed)})" if allowed else
                                " (takes none)"))
        result = {}
        if kind == "trip":
            result["code"] = _whole(params.get("code", 1), "code", 1, 0xFFFF)
        elif kind == "phase_loss":
            if "phase" not in params:
                raise FaultError("phase_loss needs phase (1-3)")
            result["phase"] = _whole(params["phase"], "phase", 1, 3)
        return result

    def clear(self, kind: str | None = None) -> None:
        if kind is not None and kind not in self.kinds():
            raise FaultError(f"unknown fault {kind!r} (known: {', '.join(self.kinds())})")
        with self.lock:
            if kind is None or kind in _COUNTED:
                for name in [kind] if kind else list(self.counted):
                    self.counted.pop(name, None)
            if kind in (None, "drift"):
                self.drifts.clear()
        if kind in (None, "drift"):
            for point in self.device.points.values():
                point.clear_drift()
        if kind in (None, "freeze"):
            self.device.unfreeze()
        if self.device.model and (kind is None or kind in self.device.model.FAULTS):
            self.device.model.clear(kind)

    def take(self, kind: str, function: int | None = None) -> dict[str, Any] | None:
        """Return the parameters of an active fault and use up one of its count."""
        with self.lock:
            entry = self.counted.get(kind)
            if entry is None:
                return None
            if kind == "exception" and entry["function"] not in (None, function):
                return None
            if entry["count"] is not None:
                entry["count"] -= 1
                if entry["count"] <= 0:
                    del self.counted[kind]
            return dict(entry)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            result: dict[str, Any] = {k: dict(v) for k, v in self.counted.items()}
            if self.drifts:
                result["drift"] = dict(self.drifts)
        if self.device.frozen is not None:
            result["freeze"] = {}
        if self.device.model:
            with self.device.model.lock:
                result.update({k: dict(v) for k, v in self.device.model.active.items()})
        return result
