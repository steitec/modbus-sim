"""Device models: the behaviour behind the registers.

A model owns named variables that profile points bind to (``model: <variable>``).
Time comes from the device clock, so tests can drive it; ``time_scale`` speeds
the simulated process up (a VFD ramp of 10 s takes 1 s at ``time_scale: 10``).

Models are deliberately simple and plausible rather than accurate:

- ``energy_meter``: three phases, voltage/current/power factor with slow load
  variation; active, reactive and apparent powers, per-phase and total energy
  counters, demand values with maxima. Fault ``phase_loss``.
- ``temperature_controller``: a PI controller heating a first-order plant;
  run/stop, auto/manual, alarm limits, an optional operation command register
  with a write enable (as on Omron controllers). Fault ``sensor_break``.
- ``vfd``: control word, speed reference, acceleration/deceleration ramps, status
  word, output current/voltage, fault trip with reset. Fault ``trip``.
- ``pv_inverter``: three-phase PV inverter on a day curve with SunSpec operating
  states and event bits. Faults ``grid_loss``, ``ground_fault``, ``over_temp``.
"""
from __future__ import annotations

import math
import random
import threading
from typing import Any, Callable

from .profile import ProfileError


class ModelError(ValueError):
    """A write the model refuses, reported to the Modbus master as exception `code`
    (03 illegal data value by default, 04 when the device refuses it in its state)."""

    def __init__(self, message: str, code: int = 0x03):
        super().__init__(message)
        self.code = code


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

_PHASES = (1, 2, 3)
# model variable -> demand key
_DEMAND_VARS = {"power_demand": "power", "apparent_power_demand": "apparent_power",
                "neutral_current_demand": "neutral_current",
                **{f"current_demand_l{i}": f"current_l{i}" for i in _PHASES}}
_DEMAND_MAX_VARS = {"power_demand_max": "power",
                    "apparent_power_demand_max": "apparent_power",
                    "neutral_current_demand_max": "neutral_current",
                    **{f"current_demand_max_l{i}": f"current_l{i}" for i in _PHASES}}


class EnergyMeter(Model):
    PARAMS = ("voltage", "frequency", "current", "power_factor", "variation", "period",
              "export", "pf_sign", "demand_period")
    FAULTS = {"phase_loss": ("phase",)}
    VARIABLES = {
        **{f"{q}_l{i}": False for q in (
            "voltage", "current", "power", "power_factor", "apparent_power",
            "reactive_power", "phase_angle", "energy_import", "energy_export",
            "energy_total", "reactive_energy_import", "reactive_energy_export",
            "reactive_energy_total", "current_demand", "current_demand_max")
           for i in _PHASES},
        "voltage_l12": False, "voltage_l23": False, "voltage_l31": False,
        "voltage_avg": False, "voltage_ll_avg": False, "current_avg": False,
        "current_sum": False, "neutral_current": False,
        "power_total": False, "reactive_power_total": False, "apparent_power_total": False,
        "power_factor_total": False, "phase_angle_total": False, "frequency": False,
        "energy_import": False, "energy_export": False, "energy_total": False,
        "reactive_energy_import": False, "reactive_energy_export": False,
        "reactive_energy_total": False, "apparent_energy": False, "charge": False,
        "power_demand": False, "power_demand_max": False,
        "apparent_power_demand": False, "apparent_power_demand_max": False,
        "neutral_current_demand": False, "neutral_current_demand_max": False,
        "reset_energy": True,
    }
    # demand values: exponentially smoothed over demand_period, with a running maximum
    _DEMANDS = ("power", "apparent_power", "neutral_current",
                *(f"current_l{i}" for i in _PHASES))

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
        sign = params.get("pf_sign", 1)
        if sign not in (1, -1) or isinstance(sign, bool):
            raise ProfileError("model_params: pf_sign is 1 or -1")
        self.pf_sign = sign
        self.demand_period = _param(params, "demand_period", 900.0, 1.0, 1e6)
        self.elapsed = 0.0
        self._reset()

    def _reset(self) -> None:
        # per phase, all in k-units (kWh, kvarh)
        self.imported = [0.0, 0.0, 0.0]
        self.exported = [0.0, 0.0, 0.0]
        self.q_imported = [0.0, 0.0, 0.0]
        self.q_exported = [0.0, 0.0, 0.0]
        self.apparent_energy = 0.0   # kVAh
        self.charge = 0.0            # Ah
        self.demand = dict.fromkeys(self._DEMANDS, 0.0)
        self.demand_max = dict.fromkeys(self._DEMANDS, 0.0)

    def _phase(self, i: int) -> tuple[float, float, float]:
        """(voltage, current, power factor) of phase i (0-based) at the current time."""
        if self.active.get("phase_loss", {}).get("phase") == i + 1:
            return 0.0, 0.0, 0.0
        shift = 2 * math.pi * i / 3
        load = 1 + self.variation * math.sin(2 * math.pi * self.elapsed / self.period + shift)
        noise = self._rng.gauss(0, 0.002)
        return (self.voltage * (1 + noise), self.current[i] * load, self.pf)

    def _snapshot(self) -> dict[str, list[float]]:
        phases = [self._phase(i) for i in range(3)]
        sign = -1 if self.export else 1
        v = [ph[0] for ph in phases]
        c = [ph[1] for ph in phases]
        s = [vi * ci for vi, ci in zip(v, c)]
        p = [sign * si * ph[2] for si, ph in zip(s, phases)]
        q = [math.sqrt(max(si * si - pi * pi, 0.0)) for si, pi in zip(s, p)]
        pf = [ph[2] for ph in phases]
        return {"v": v, "c": c, "s": s, "p": p, "q": q, "pf": pf}

    @staticmethod
    def _neutral(c: list[float]) -> float:
        """Neutral current of three currents 120 degrees apart (same power factor)."""
        a, b, d = c
        return math.sqrt(max(a * a + b * b + d * d - a * b - b * d - d * a, 0.0))

    def advance(self, dt: float) -> None:
        steps = max(1, min(1000, math.ceil(dt / 1.0)))
        h = dt / steps
        alpha = 1 - math.exp(-h / self.demand_period)
        for _ in range(steps):
            self.elapsed += h
            snap = self._snapshot()
            for i in range(3):
                kwh = snap["p"][i] * h / 3600 / 1000
                kvarh = snap["q"][i] * h / 3600 / 1000
                if kwh >= 0:
                    self.imported[i] += kwh
                    self.q_imported[i] += kvarh
                else:
                    self.exported[i] -= kwh
                    self.q_exported[i] += kvarh
            self.apparent_energy += sum(snap["s"]) * h / 3600 / 1000
            self.charge += sum(snap["c"]) * h / 3600
            now = {"power": max(sum(snap["p"]), 0.0),   # import power only
                   "apparent_power": sum(snap["s"]),
                   "neutral_current": self._neutral(snap["c"]),
                   **{f"current_l{i + 1}": snap["c"][i] for i in range(3)}}
            for key, value in now.items():
                self.demand[key] += (value - self.demand[key]) * alpha
                self.demand_max[key] = max(self.demand_max[key], self.demand[key])

    def read(self, name: str) -> Any:
        if name == "reset_energy":
            return False
        if name == "frequency":
            return self.frequency
        if name in ("apparent_energy", "charge"):
            return getattr(self, name)
        energies = {"energy_import": self.imported, "energy_export": self.exported,
                    "reactive_energy_import": self.q_imported,
                    "reactive_energy_export": self.q_exported}
        if name in energies:
            return sum(energies[name])
        if name == "energy_total":
            return sum(self.imported) + sum(self.exported)
        if name == "reactive_energy_total":
            return sum(self.q_imported) + sum(self.q_exported)
        if name in _DEMAND_VARS:
            return self.demand[_DEMAND_VARS[name]]
        if name in _DEMAND_MAX_VARS:
            return self.demand_max[_DEMAND_MAX_VARS[name]]
        if name.startswith("voltage_l") and len(name) == 11:   # line-to-line
            a, b = int(name[-2]) - 1, int(name[-1]) - 1
            va, vb = self._phase(a)[0], self._phase(b)[0]
            return math.sqrt(va * va + vb * vb + va * vb)  # 120 degrees apart
        if name == "voltage_ll_avg":
            return sum(self.read(f"voltage_l{a}{b}") for a, b in ("12", "23", "31")) / 3
        snap = self._snapshot()
        if name.endswith(("_l1", "_l2", "_l3")):
            i = int(name[-1]) - 1
            quantity = name[:-3]
            per_phase = {
                "voltage": lambda: snap["v"][i], "current": lambda: snap["c"][i],
                "power": lambda: snap["p"][i], "apparent_power": lambda: snap["s"][i],
                "reactive_power": lambda: snap["q"][i],
                "power_factor": lambda: self.pf_sign * snap["pf"][i],
                "phase_angle": lambda: math.degrees(math.acos(snap["pf"][i]))
                if snap["s"][i] else 0.0,
                "energy_import": lambda: self.imported[i],
                "energy_export": lambda: self.exported[i],
                "energy_total": lambda: self.imported[i] + self.exported[i],
                "reactive_energy_import": lambda: self.q_imported[i],
                "reactive_energy_export": lambda: self.q_exported[i],
                "reactive_energy_total": lambda: self.q_imported[i] + self.q_exported[i],
            }
            return per_phase[quantity]()
        total_p, total_s = sum(snap["p"]), sum(snap["s"])
        totals = {
            "voltage_avg": lambda: sum(snap["v"]) / 3,
            "current_avg": lambda: sum(snap["c"]) / 3,
            "current_sum": lambda: sum(snap["c"]),
            "neutral_current": lambda: self._neutral(snap["c"]),
            "power_total": lambda: total_p,
            "apparent_power_total": lambda: total_s,
            "reactive_power_total": lambda: sum(snap["q"]),
            "power_factor_total": lambda: self.pf_sign * abs(total_p) / total_s
            if total_s else 0.0,
            "phase_angle_total": lambda: math.degrees(math.acos(min(abs(total_p) / total_s,
                                                                    1.0))) if total_s else 0.0,
        }
        if name in totals:
            return totals[name]()
        raise KeyError(name)

    def write(self, name: str, value: Any) -> None:
        if name == "reset_energy" and value:
            self._reset()


# --- temperature controller ---------------------------------------------------------

class TemperatureController(Model):
    PARAMS = ("ambient", "max_temperature", "time_constant", "setpoint", "kp", "ti", "run",
              "comms_write")
    FAULTS = {"sensor_break": ()}
    VARIABLES = {
        "pv": False, "setpoint": True, "output": False, "manual_output": True,
        "run": True, "mode": True, "alarm_high_limit": True, "alarm_low_limit": True,
        "alarm_high": False, "alarm_low": False, "sensor_break": False, "status": False,
        "command": True, "comms_write": False, "e5_status_low": False,
        "e5_status_high": False,
    }
    # operation command: high byte = command code, low byte = related information
    # (Omron E5@C layout); 00 = communications writing, 01 = run/stop, 09 = auto/manual
    _COMMANDS = {0x00: (0, 1), 0x01: (0, 1), 0x02: tuple(range(8)), 0x03: (0, 1, 2),
                 0x04: (0, 1), 0x05: (0,), 0x06: (0,), 0x07: (0,), 0x08: (0,), 0x09: (0, 1),
                 0x0B: (0,), 0x0C: (0, 1, 2, 3, 4, 5, 0x0F), 0x0D: (0, 1), 0x0E: (0, 1),
                 0x11: (0, 1)}
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
        # False: setting writes are refused until the operation command 0x0001 enables them
        self.comms_write = bool(params.get("comms_write", True))
        self.ram_write = False   # operation command 04: write mode
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
        if name == "command":
            return 0
        if name == "comms_write":
            return self.comms_write
        if name == "e5_status_low":
            # Omron E5@C status, lower word: bit 6 input error, bit 8 control output
            # (heating). Alarm, heater and CT bits are not simulated.
            return (self._broken() << 6) | ((self.output > 0) << 8)
        if name == "e5_status_high":
            # upper word (bits 16-31 -> 0-15): 20 write mode, 24 run/stop (1 = stop),
            # 25 communications writing, 26 auto/manual (1 = manual)
            return ((self.ram_write << 4) | ((not self.running) << 8)
                    | (self.comms_write << 9) | ((self.mode == 1) << 10))
        if name == "status":
            return (self.running | (self.mode << 1) | (high << 2) | (low << 3)
                    | (self._broken() << 4))
        return getattr(self, name)

    def _command(self, value: int) -> None:
        code, info = value >> 8, value & 0xFF
        if info not in self._COMMANDS.get(code, ()):
            raise ModelError(f"unknown operation command {value:#06x}")
        if code == 0x00:
            self.comms_write = bool(info)
            return
        if not self.comms_write:
            raise ModelError("communications writing is off (command 0x0001 turns it on)",
                             code=0x04)
        if code == 0x04:
            self.ram_write = bool(info)
        elif code == 0x01:
            self.running = info == 0          # 00 = run, 01 = stop
        elif code == 0x09:
            self.write("mode", info)          # 00 = auto, 01 = manual
        # the other commands are accepted and have no effect in the simulation

    def write(self, name: str, value: Any) -> None:
        if name == "command":
            self._command(int(value))
            return
        if not self.comms_write:
            raise ModelError("communications writing is off (command 0x0001 turns it on)",
                             code=0x04)
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


# --- PV inverter --------------------------------------------------------------------

class PvInverter(Model):
    """Three-phase grid-tied PV inverter following a day curve.

    The simulated time of day starts at ``start_hour`` and runs with the device
    clock (``time_scale: 3600`` makes a day pass in 24 s). Between ``sunrise`` and
    ``sunset`` the DC power follows a half sine; at night the inverter sleeps.
    States and event bits follow SunSpec model 103.
    """

    PARAMS = ("rated_power", "voltage", "frequency", "dc_voltage", "efficiency",
              "power_factor", "sunrise", "sunset", "start_hour", "ambient", "variation")
    FAULTS = {"grid_loss": (), "ground_fault": (), "over_temp": ()}
    VARIABLES = {
        **{f"{q}_l{i}": False for q in ("current", "voltage") for i in (1, 2, 3)},
        "voltage_l12": False, "voltage_l23": False, "voltage_l31": False,
        "current": False, "power": False, "frequency": False, "apparent_power": False,
        "reactive_power": False, "power_factor": False, "energy": False,
        "dc_current": False, "dc_voltage": False, "dc_power": False,
        "cabinet_temperature": False, "heatsink_temperature": False,
        "state": False, "events": False, "hour": False,
    }
    # SunSpec model 103 operating states and event bits
    SLEEPING, MPPT, THROTTLED, FAULT = 2, 4, 5, 7
    GROUND_FAULT, GRID_DISCONNECT, OVER_TEMP = 1 << 0, 1 << 4, 1 << 7

    def __init__(self, params, clock, seed=None):
        super().__init__(params, clock, seed)
        self.rated_power = _param(params, "rated_power", 10000.0, 1, 1e7)
        self.voltage = _param(params, "voltage", 230.0, 1, 1e5)
        self.frequency = _param(params, "frequency", 50.0, 1, 1000)
        self.dc_voltage = _param(params, "dc_voltage", 600.0, 1, 1e5)
        self.efficiency = _param(params, "efficiency", 0.97, 0.01, 1.0)
        self.pf = _param(params, "power_factor", 1.0, 0.0, 1.0)
        self.sunrise = _param(params, "sunrise", 6.0, 0, 24)
        self.sunset = _param(params, "sunset", 20.0, 0, 24)
        if self.sunset <= self.sunrise:
            raise ProfileError("model_params: sunset must be after sunrise")
        self.start_hour = _param(params, "start_hour", 12.0, 0, 24)
        self.ambient = _param(params, "ambient", 20.0, -50, 80)
        self.variation = _param(params, "variation", 0.02, 0.0, 1.0)
        self.elapsed = 0.0
        self.energy = 0.0    # Wh
        self._tick = None

    def _draw(self) -> None:
        # one noise sample per simulated second, so the values of one read agree
        tick = math.floor(self.elapsed)
        if tick == self._tick:
            return
        self._tick = tick
        self.noise = 1 + self._rng.gauss(0, self.variation) if self.variation else 1.0
        self.grid_noise = 1 + self._rng.gauss(0, 0.002)

    def _hour(self) -> float:
        return (self.start_hour + self.elapsed / 3600) % 24

    def _sun(self) -> float:
        """Available DC power as a fraction of the rated input (0 at night)."""
        h = self._hour()
        if not self.sunrise < h < self.sunset:
            return 0.0
        return math.sin(math.pi * (h - self.sunrise) / (self.sunset - self.sunrise))

    def _state(self) -> int:
        if "ground_fault" in self.active or "grid_loss" in self.active:
            return self.FAULT
        if self._sun() <= 0:
            return self.SLEEPING
        if "over_temp" in self.active:
            return self.THROTTLED
        return self.MPPT

    def _ac_power(self, noise: float = 1.0) -> float:
        state = self._state()
        if state in (self.FAULT, self.SLEEPING):
            return 0.0
        power = min(self.rated_power * self._sun() * max(noise, 0.0), self.rated_power)
        if state == self.THROTTLED:
            power = min(power, self.rated_power / 2)
        return power

    def advance(self, dt: float) -> None:
        steps = max(1, min(2000, math.ceil(dt / 10.0)))
        h = dt / steps
        for _ in range(steps):
            self.elapsed += h
            self.energy += self._ac_power() * h / 3600

    def read(self, name: str) -> Any:
        if name == "energy":
            return self.energy
        if name == "hour":
            return self._hour()
        if name == "state":
            return self._state()
        if name == "events":
            return ((self.GROUND_FAULT if "ground_fault" in self.active else 0)
                    | (self.GRID_DISCONNECT if "grid_loss" in self.active else 0)
                    | (self.OVER_TEMP if "over_temp" in self.active else 0))
        self._draw()
        grid = "grid_loss" not in self.active
        power = self._ac_power(self.noise)
        load = power / self.rated_power
        if name == "frequency":
            return self.frequency if grid else 0.0
        if name.startswith("voltage_l"):
            v = self.voltage * self.grid_noise if grid else 0.0
            return v * math.sqrt(3) if len(name) == 11 else v
        apparent = power / self.pf if self.pf else 0.0
        current = apparent / (3 * self.voltage)
        if name.startswith("current_l"):
            return current
        sun = self._sun()
        dc_power = power / self.efficiency
        dc_voltage = self.dc_voltage * (0.9 + 0.1 * sun) if sun > 0 else 0.0
        values = {
            "current": 3 * current,
            "power": power,
            "apparent_power": apparent,
            "reactive_power": math.sqrt(max(apparent ** 2 - power ** 2, 0.0)),
            "power_factor": 100.0 * self.pf if power else 100.0,   # SunSpec: percent
            "dc_power": dc_power,
            "dc_voltage": dc_voltage,
            "dc_current": dc_power / dc_voltage if dc_voltage else 0.0,
            "cabinet_temperature": self.ambient + 15 * load,
            "heatsink_temperature": self.ambient + 35 * load
            + (40 if "over_temp" in self.active else 0),
        }
        return values[name]


MODELS: dict[str, type[Model]] = {
    "energy_meter": EnergyMeter,
    "temperature_controller": TemperatureController,
    "vfd": Vfd,
    "pv_inverter": PvInverter,
}


def make_model(name: str, params: dict, clock, seed=None) -> Model:
    try:
        cls = MODELS[name]
    except KeyError:
        raise ProfileError(f"unknown model {name!r} (models: {', '.join(MODELS)})") from None
    return cls(params, clock, seed)
