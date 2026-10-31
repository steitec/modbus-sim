"""Device models: the behaviour behind the registers.

A model owns named variables that profile points bind to (``model: <variable>``).
Time comes from the device clock, so tests can drive it; ``time_scale`` speeds
the simulated process up (a VFD ramp of 10 s takes 1 s at ``time_scale: 10``).

Models are deliberately simple and plausible rather than accurate:

- ``energy_meter``: three phases, voltage/current/power factor with slow load
  variation, powers and energy counters integrated over time. Fault ``phase_loss``.
- ``temperature_controller``: a PI controller heating a first-order plant;
  run/stop, auto/manual, alarm limits. Fault ``sensor_break``.
- ``vfd``: control word, speed reference, acceleration/deceleration ramps, status
  word, output current/voltage, fault trip with reset. Fault ``trip``.
"""
from __future__ import annotations

import math
import random
import threading
from typing import Any, Callable

from .profile import ProfileError


class ModelError(ValueError):
    """A write the model refuses (reported to the Modbus master as exception 03)."""


def _param(params: dict, name: str, default: float, low: float = -math.inf,
           high: float = math.inf) -> float:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or not low <= value <= high:
        raise ProfileError(f"model_params: {name} must be a number {low:g}..{high:g}, "
                           f"got {value!r}")
    return float(value)


class Model:
    """Base class. Subclasses fill VARIABLES (name -> writable), PARAMS and FAULTS."""

    VARIABLES: dict[str, bool] = {}
    PARAMS: tuple[str, ...] = ()
    FAULTS: dict[str, tuple[str, ...]] = {}

    def __init__(self, params: dict[str, Any], clock: Callable[[], float], seed=None):
        unknown = set(params) - set(self.PARAMS) - {"time_scale"}
        if unknown:
            raise ProfileError(f"model_params: unknown parameter {sorted(unknown)[0]!r} "
                               f"(allowed: {', '.join(sorted({*self.PARAMS, 'time_scale'}))})")
        self.time_scale = _param(params, "time_scale", 1.0, 1e-3, 1e4)
        self._clock = clock
        self._t = clock()
        self._rng = random.Random(seed)
        self.lock = threading.RLock()
        self.active: dict[str, dict[str, Any]] = {}

    # --- time ---

    def _update(self) -> None:
        now = self._clock()
        dt = (now - self._t) * self.time_scale
        self._t = now
        if dt > 0:
            self.advance(dt)

    def advance(self, dt: float) -> None:
        """Move the simulated process forward by `dt` seconds (already time-scaled)."""

    # --- variables ---

    def get(self, name: str) -> Any:
        with self.lock:
            self._update()
            return self.read(name)

    def set(self, name: str, value: Any) -> None:
        if not self.VARIABLES.get(name):
            raise ModelError(f"{name} is read-only")
        with self.lock:
            self._update()
            self.write(name, value)

    def read(self, name: str) -> Any:
        raise NotImplementedError

    def write(self, name: str, value: Any) -> None:
        raise NotImplementedError

    # --- faults ---

    def inject(self, kind: str, params: dict[str, Any]) -> None:
        with self.lock:
            self._update()
            self.active[kind] = params
            self.on_inject(kind, params)

    def clear(self, kind: str | None = None) -> None:
        with self.lock:
            self._update()
            for name in [kind] if kind else list(self.active):
                if self.active.pop(name, None) is not None:
                    self.on_clear(name)

    def on_inject(self, kind: str, params: dict[str, Any]) -> None:
        pass

    def on_clear(self, kind: str) -> None:
        pass


# --- energy meter -------------------------------------------------------------------

class EnergyMeter(Model):
    PARAMS = ("voltage", "frequency", "current", "power_factor", "variation", "period",
              "export")
    FAULTS = {"phase_loss": ("phase",)}
    VARIABLES = {
        **{f"{q}_l{i}": False for q in ("voltage", "current", "power", "power_factor")
           for i in (1, 2, 3)},
        "voltage_l12": False, "voltage_l23": False, "voltage_l31": False,
        "power_total": False, "reactive_power_total": False, "apparent_power_total": False,
        "power_factor_total": False, "frequency": False,
        "energy_import": False, "energy_export": False, "reset_energy": True,
    }

    def __init__(self, params, clock, seed=None):
        super().__init__(params, clock, seed)
        self.voltage = _param(params, "voltage", 230.0, 0, 1e5)
        self.frequency = _param(params, "frequency", 50.0, 1, 1000)
        current = params.get("current", [10.0, 12.0, 8.0])
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            current = [current] * 3
        if not (isinstance(current, list) and len(current) == 3):
            raise ProfileError("model_params: current is a number or a list of 3 numbers")
        self.current = [_param({"current": c}, "current", 0, 0, 1e5) for c in current]
        self.pf = _param(params, "power_factor", 0.95, 0.0, 1.0)
        self.variation = _param(params, "variation", 0.1, 0.0, 1.0)
        self.period = _param(params, "period", 60.0, 1e-3)
        self.export = bool(params.get("export", False))
        self.elapsed = 0.0
        self.energy_import = 0.0  # kWh
        self.energy_export = 0.0

    def _phase(self, i: int) -> tuple[float, float, float]:
        """(voltage, current, power factor) of phase i (0-based) at the current time."""
        if self.active.get("phase_loss", {}).get("phase") == i + 1:
            return 0.0, 0.0, 0.0
        shift = 2 * math.pi * i / 3
        load = 1 + self.variation * math.sin(2 * math.pi * self.elapsed / self.period + shift)
        noise = self._rng.gauss(0, 0.002)
        return (self.voltage * (1 + noise), self.current[i] * load, self.pf)

    def _power(self, i: int) -> float:
        v, c, pf = self._phase(i)
        return v * c * pf * (-1 if self.export else 1)

    def advance(self, dt: float) -> None:
        steps = max(1, min(1000, math.ceil(dt / 1.0)))
        h = dt / steps
        for _ in range(steps):
            self.elapsed += h
            total = sum(self._power(i) for i in range(3))
            kwh = abs(total) * h / 3600 / 1000
            if total >= 0:
                self.energy_import += kwh
            else:
                self.energy_export += kwh

    def read(self, name: str) -> Any:
        if name == "reset_energy":
            return False
        if name == "frequency":
            return self.frequency
        if name in ("energy_import", "energy_export"):
            return getattr(self, name)
        if name.startswith("voltage_l") and len(name) == 11:   # line-to-line
            a, b = int(name[-2]) - 1, int(name[-1]) - 1
            va, vb = self._phase(a)[0], self._phase(b)[0]
            return math.sqrt(va * va + vb * vb + va * vb)  # 120 degrees apart
        phases = [self._phase(i) for i in range(3)]
        p = [v * c * pf for v, c, pf in phases]
        s = [v * c for v, c, _ in phases]
        sign = -1 if self.export else 1
        if name.endswith(("_l1", "_l2", "_l3")):
            i = int(name[-1]) - 1
            quantity = name[:-3]
            if quantity == "voltage":
                return phases[i][0]
            if quantity == "current":
                return phases[i][1]
            if quantity == "power":
                return sign * p[i]
            return phases[i][2]
        if name == "power_total":
            return sign * sum(p)
        if name == "apparent_power_total":
            return sum(s)
        if name == "reactive_power_total":
            return sum(math.sqrt(max(si * si - pi * pi, 0.0)) for si, pi in zip(s, p))
        if name == "power_factor_total":
            return sum(p) / sum(s) if sum(s) else 0.0
        raise KeyError(name)

    def write(self, name: str, value: Any) -> None:
        if name == "reset_energy" and value:
            self.energy_import = self.energy_export = 0.0


# --- temperature controller ---------------------------------------------------------

class TemperatureController(Model):
    PARAMS = ("ambient", "max_temperature", "time_constant", "setpoint", "kp", "ti", "run")
    FAULTS = {"sensor_break": ()}
    VARIABLES = {
        "pv": False, "setpoint": True, "output": False, "manual_output": True,
        "run": True, "mode": True, "alarm_high_limit": True, "alarm_low_limit": True,
        "alarm_high": False, "alarm_low": False, "sensor_break": False, "status": False,
    }
    _STEP = 0.5  # s of simulated time per integration step

    def __init__(self, params, clock, seed=None):
        super().__init__(params, clock, seed)
        self.ambient = _param(params, "ambient", 22.0, -100, 1000)
        self.max_temperature = _param(params, "max_temperature", 300.0, -100, 3000)
        if self.max_temperature <= self.ambient:
            raise ProfileError("model_params: max_temperature must be above ambient")
        self.tau = _param(params, "time_constant", 120.0, 0.1, 1e6)
        self.kp = _param(params, "kp", 5.0, 0, 1e4)
        self.ti = _param(params, "ti", 60.0, 0.1, 1e6)
        self.setpoint = _param(params, "setpoint", 60.0, -1000, 3000)
        self.running = bool(params.get("run", True))
        self.pv = self.ambient
        self.output = 0.0
        self.manual_output = 0.0
        self.mode = 0  # 0 auto, 1 manual
        self.integral = 0.0
        self.alarm_high_limit = self.setpoint + 20
        self.alarm_low_limit = self.ambient - 20

    def _broken(self) -> bool:
        return "sensor_break" in self.active

    def _step(self, h: float) -> None:
        if not self.running or self._broken():
            self.output, self.integral = 0.0, 0.0
        elif self.mode == 1:
            self.output = self.manual_output
        else:
            error = self.setpoint - self.pv
            unclamped = self.kp * (error + self.integral / self.ti)
            # anti-windup: stop integrating while the output is saturated in that direction
            if 0 <= unclamped <= 100 or (unclamped > 100 and error < 0) \
                    or (unclamped < 0 and error > 0):
                self.integral += error * h
            self.output = min(max(self.kp * (error + self.integral / self.ti), 0.0), 100.0)
        target = self.ambient + (self.max_temperature - self.ambient) * self.output / 100
        self.pv += (target - self.pv) * (1 - math.exp(-h / self.tau))

    def advance(self, dt: float) -> None:
        steps = min(20000, max(1, math.ceil(dt / self._STEP)))
        h = dt / steps
        for _ in range(steps):
            self._step(h)

    def _alarms(self) -> tuple[bool, bool]:
        if self._broken():
            return True, False
        return self.pv > self.alarm_high_limit, self.pv < self.alarm_low_limit

    def read(self, name: str) -> Any:
        high, low = self._alarms()
        if name == "pv":
            return math.inf if self._broken() else self.pv + self._rng.gauss(0, 0.02)
        if name == "alarm_high":
            return high
        if name == "alarm_low":
            return low
        if name == "sensor_break":
            return self._broken()
        if name == "run":
            return self.running
        if name == "status":
            return (self.running | (self.mode << 1) | (high << 2) | (low << 3)
                    | (self._broken() << 4))
        return getattr(self, name)

    def write(self, name: str, value: Any) -> None:
        if name == "run":
            self.running = bool(value)
        elif name == "mode":
            if value not in (0, 1):
                raise ModelError("mode is 0 (auto) or 1 (manual)")
            if value == 1 and self.mode == 0:
                self.manual_output = self.output  # bumpless
            self.mode = int(value)
        elif name == "manual_output":
            if not 0 <= value <= 100:
                raise ModelError("manual output is 0-100 %")
            self.manual_output = float(value)
        else:
            setattr(self, name, float(value))


# --- frequency converter ------------------------------------------------------------

class Vfd(Model):
    PARAMS = ("max_frequency", "accel_time", "decel_time", "rated_current", "rated_voltage",
              "pole_pairs")
    FAULTS = {"trip": ("code",)}
    VARIABLES = {
        "control_word": True, "speed_reference": True, "status_word": False,
        "output_frequency": False, "output_current": False, "output_voltage": False,
        "motor_speed": False, "fault_code": False, "run_hours": False,
        "ready": False, "running": False, "at_reference": False, "fault": False,
    }
    RUN, REVERSE, RESET = 0x0001, 0x0002, 0x0004

    def __init__(self, params, clock, seed=None):
        super().__init__(params, clock, seed)
        self.max_frequency = _param(params, "max_frequency", 50.0, 1, 1000)
        self.accel = self.max_frequency / _param(params, "accel_time", 10.0, 0.01, 3600)
        self.decel = self.max_frequency / _param(params, "decel_time", 10.0, 0.01, 3600)
        self.rated_current = _param(params, "rated_current", 10.0, 0, 1e4)
        self.rated_voltage = _param(params, "rated_voltage", 400.0, 0, 1e5)
        self.pole_pairs = _param(params, "pole_pairs", 2, 1, 32)
        self.control_word = 0
        self.speed_reference = 0.0
        self.frequency = 0.0     # signed, negative = reverse
        self.fault_code = 0
        self.needs_restart = False  # after a trip, RUN must go 0 -> 1 again
        self.run_hours = 0.0
        self.accelerating = False

    def _target(self) -> float:
        if self.fault_code or self.needs_restart or not self.control_word & self.RUN:
            return 0.0
        sign = -1.0 if self.control_word & self.REVERSE else 1.0
        return sign * min(self.speed_reference, self.max_frequency)

    def advance(self, dt: float) -> None:
        self.accelerating = False
        if abs(self.frequency) > 0:
            self.run_hours += dt / 3600
        while dt > 1e-12:
            target = self._target()
            f = self.frequency
            if f == target:
                break
            if f != 0 and (target == 0 or (target > 0) != (f > 0)):
                goal, rate = 0.0, self.decel          # stop, or slow down to reverse
            elif abs(target) > abs(f):
                goal, rate = target, self.accel
                self.accelerating = True
            else:
                goal, rate = target, self.decel
            need = abs(goal - f) / rate
            if need <= dt:
                self.frequency, dt = goal, dt - need
            else:
                self.frequency = f + math.copysign(rate * dt, goal - f)
                dt = 0

    def _status(self) -> int:
        f, target = self.frequency, self._target()
        running = f != 0 or target != 0
        return ((not self.fault_code) | (running << 1)
                | ((running and abs(f - target) < 0.01) << 2)
                | (bool(self.fault_code) << 3) | ((f < 0 or (f == 0 and target < 0)) << 4))

    def read(self, name: str) -> Any:
        f = abs(self.frequency)
        if name == "output_frequency":
            return f
        if name == "output_voltage":
            return self.rated_voltage * min(f / self.max_frequency, 1.0)
        if name == "output_current":
            if f == 0:
                return 0.0
            load = 0.3 + 0.6 * (f / self.max_frequency) ** 2
            if self.accelerating:
                load += 0.4
            return self.rated_current * load * (1 + self._rng.gauss(0, 0.005))
        if name == "motor_speed":
            return self.frequency * 60 / self.pole_pairs
        if name == "status_word":
            return self._status()
        bits = {"ready": 0, "running": 1, "at_reference": 2, "fault": 3}
        if name in bits:
            return bool(self._status() >> bits[name] & 1)
        return getattr(self, name)

    def write(self, name: str, value: Any) -> None:
        if name == "speed_reference":
            if value < 0:
                raise ModelError("speed reference must be >= 0 (direction is in the "
                                 "control word)")
            self.speed_reference = float(value)
            return
        value = int(value)
        rising = value & ~self.control_word
        if rising & self.RESET and self.fault_code and "trip" not in self.active:
            self.fault_code = 0
            self.needs_restart = True
        if not value & self.RUN:
            self.needs_restart = False
        self.control_word = value

    def on_inject(self, kind: str, params: dict[str, Any]) -> None:
        if kind == "trip":
            self.fault_code = params.get("code", 1)
            self.frequency = 0.0  # coast to stop

    def on_clear(self, kind: str) -> None:
        pass  # a trip stays latched until the master resets it via the control word


MODELS: dict[str, type[Model]] = {
    "energy_meter": EnergyMeter,
    "temperature_controller": TemperatureController,
    "vfd": Vfd,
}


def make_model(name: str, params: dict, clock, seed=None) -> Model:
    try:
        cls = MODELS[name]
    except KeyError:
        raise ProfileError(f"unknown model {name!r} (models: {', '.join(MODELS)})") from None
    return cls(params, clock, seed)
