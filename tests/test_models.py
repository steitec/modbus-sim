import math

import pytest

from modbus_sim import datatypes as dt
from modbus_sim.faults import FaultError
from modbus_sim.profile import ProfileError

from conftest import read, write


def f32(words, order="CDAB"):
    return dt.decode(words, "float32", order)


# --- vfd ---

def test_vfd_ramps_up_and_down(make_device, clock):
    dev = make_device("vfd")
    assert write(dev, 1, 4000) == "ok"         # 40.00 Hz
    assert write(dev, 0, 1) == "ok"            # run
    clock.advance(4)                           # 50 Hz / 10 s -> 5 Hz/s
    assert read(dev, 3, 101, 1) == [2000]
    status = read(dev, 3, 100, 1)[0]
    assert status & 0b11 == 0b11 and not status & 0b100   # ready, running, not at ref
    clock.advance(10)
    assert read(dev, 3, 101, 1) == [4000]
    assert read(dev, 3, 100, 1)[0] & 0b100
    assert read(dev, 3, 104, 1) == [1200]       # 40 Hz, 2 pole pairs -> 1200 rpm
    assert read(dev, 3, 103, 1) == [320]        # V/f
    write(dev, 0, 0)
    clock.advance(2)
    assert read(dev, 3, 101, 1) == [3000]
    clock.advance(10)
    assert read(dev, 3, 101, 1) == [0]
    assert read(dev, 3, 102, 1) == [0]


def test_vfd_reverses_through_zero(make_device, clock):
    dev = make_device("vfd")
    write(dev, 1, 1000)
    write(dev, 0, 1)
    clock.advance(5)
    write(dev, 0, 0b11)                         # run + reverse
    clock.advance(1)
    assert read(dev, 3, 104, 1) == [150]          # 5 Hz, still forward
    clock.advance(2)
    assert read(dev, 3, 104, 1) == [dt.encode(-150, "int16")[0]]
    assert read(dev, 3, 100, 1)[0] & 0x10


def test_vfd_trip_latches_until_reset_and_restart(make_device, clock):
    dev = make_device("vfd")
    write(dev, 1, 5000)
    write(dev, 0, 1)
    clock.advance(5)
    dev.faults.inject("trip", code=7)
    assert read(dev, 3, 105, 1) == [7]
    assert read(dev, 3, 101, 1) == [0]
    assert read(dev, 2, 0, 4) == [False, False, False, True]
    write(dev, 0, 0b101)                         # reset while fault still injected: no effect
    assert read(dev, 3, 105, 1) == [7]
    dev.faults.clear("trip")
    assert read(dev, 3, 105, 1) == [7]           # still latched
    write(dev, 0, 0b001)
    write(dev, 0, 0b101)                         # rising edge of reset
    assert read(dev, 3, 105, 1) == [0]
    clock.advance(5)
    assert read(dev, 3, 101, 1) == [0]           # RUN still set: needs a new edge
    write(dev, 0, 0)
    write(dev, 0, 1)
    clock.advance(2)
    assert read(dev, 3, 101, 1) == [1000]


def test_vfd_speed_reference_limit(make_device):
    dev = make_device("vfd")
    assert write(dev, 1, 5001) == ("exception", 3)


def test_vfd_time_scale(make_device, clock):
    dev = make_device("vfd", {"model_params": {"time_scale": 10}})
    write(dev, 1, 5000)
    write(dev, 0, 1)
    clock.advance(1)
    assert read(dev, 3, 101, 1) == [5000]


# --- temperature controller ---

def test_controller_reaches_setpoint(make_device, clock):
    dev = make_device("temperature-controller")
    assert read(dev, 4, 0, 1)[0] == pytest.approx(220, abs=2)
    clock.advance(1800)
    assert dev.points["pv"].value() == pytest.approx(60.0, abs=0.5)
    write(dev, 0, 1000)                          # 100.0 degC
    clock.advance(1800)
    assert dev.points["pv"].value() == pytest.approx(100.0, abs=0.5)


def test_controller_stop_cools_down(make_device, clock):
    dev = make_device("temperature-controller")
    clock.advance(1800)
    dev.handle(bytes([5, 0, 0, 0, 0]))           # run off
    clock.advance(3600)
    assert dev.points["pv"].value() == pytest.approx(22.0, abs=0.5)
    assert read(dev, 4, 1, 1) == [0]


def test_controller_manual_mode(make_device, clock):
    dev = make_device("temperature-controller")
    assert write(dev, 1, 2) == ("exception", 3)
    write(dev, 1, 1)
    write(dev, 2, 500)                           # 50 %
    clock.advance(3600)
    assert dev.points["pv"].value() == pytest.approx(22 + 278 * 0.5, abs=1)
    assert read(dev, 4, 2, 1)[0] & 0b11 == 0b11  # running, manual


def test_controller_sensor_break(make_device, clock):
    dev = make_device("temperature-controller")
    dev.faults.inject("sensor_break")
    assert read(dev, 4, 0, 1) == [0x7FFF]
    assert read(dev, 2, 0, 3) == [True, False, True]
    clock.advance(60)
    assert read(dev, 4, 1, 1) == [0]
    dev.faults.clear("sensor_break")
    assert read(dev, 2, 2, 1) == [False]


def test_controller_alarm_high(make_device, clock):
    dev = make_device("temperature-controller")
    write(dev, 3, 300)                           # alarm above 30.0
    clock.advance(1800)
    assert read(dev, 2, 0, 1) == [True]


# --- energy meter ---

def test_meter_values_are_cdab_floats(make_device):
    dev = make_device("energy-meter")
    words = read(dev, 4, 0, 2)
    assert f32(words) == pytest.approx(230, rel=0.02)
    assert f32(words, "ABCD") != pytest.approx(230, rel=0.5)
    assert f32(read(dev, 4, 30, 2)) == 50.0
    v12 = f32(read(dev, 4, 24, 2))
    assert v12 == pytest.approx(230 * math.sqrt(3), rel=0.02)


def test_meter_energy_counts_and_resets(make_device, clock):
    dev = make_device("energy-meter")
    power = f32(read(dev, 4, 32, 2))
    assert power == pytest.approx(230 * 30 * 0.95, rel=0.1)
    clock.advance(3600)
    energy = f32(read(dev, 4, 40, 2))
    assert energy == pytest.approx(power / 1000, rel=0.1)
    wh = dt.decode(read(dev, 4, 100, 4), "uint64", "CDAB")
    assert wh == pytest.approx(energy * 1000, abs=1)
    dev.handle(bytes([5, 0, 0, 0xFF, 0]))
    assert f32(read(dev, 4, 40, 2)) == 0.0
    assert read(dev, 1, 0, 1) == [False]


def test_meter_phase_loss(make_device):
    dev = make_device("energy-meter")
    dev.faults.inject("phase_loss", phase=2)
    assert f32(read(dev, 4, 2, 2)) == 0.0
    assert f32(read(dev, 4, 8, 2)) == 0.0
    assert f32(read(dev, 4, 0, 2)) > 200
    with pytest.raises(FaultError):
        dev.faults.inject("phase_loss", phase=4)


def test_unknown_fault_and_params(make_device):
    dev = make_device("energy-meter")
    with pytest.raises(FaultError, match="unknown fault"):
        dev.faults.inject("trip")
    with pytest.raises(FaultError):
        dev.faults.inject("delay")
    with pytest.raises(FaultError):
        dev.faults.inject("drift", point="label", per_second=1)


def test_unknown_model_param(make_device):
    with pytest.raises(ProfileError, match="unknown parameter"):
        make_device("vfd", {"model_params": {"warp": 9}})
