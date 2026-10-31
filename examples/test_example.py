"""How a test of your own Modbus code could look (run: pytest examples/test_example.py).

`poll_pump` stands for the code under test: it reads a drive and decides what to report.
"""
import time

import pytest

from modbus_sim.client import ModbusClient, ModbusError


def poll_pump(client: ModbusClient, unit: int) -> str:
    try:
        status, frequency = client.read_holding_registers(100, 2, unit=unit)
    except (TimeoutError, ModbusError) as exc:
        return f"offline ({exc.__class__.__name__})"
    if status & 0x08:
        fault = client.read_holding_registers(105, 1, unit=unit)[0]
        return f"fault {fault}"
    return f"running at {frequency / 100:.1f} Hz" if status & 0x02 else "stopped"


@pytest.fixture
def plant(modbus_bench_factory):
    bench = modbus_bench_factory({"devices": [
        {"profile": "vfd", "unit": 3, "name": "pump", "model_params": {"time_scale": 50}}]})
    return bench


def test_reports_running(plant):
    with plant.client() as c:
        c.write_register(1, 3000, unit=3)   # 30.00 Hz
        c.write_register(0, 1, unit=3)      # run
        time.sleep(0.2)                     # 10 s of simulated time
        assert poll_pump(c, 3) == "running at 30.0 Hz"


def test_reports_a_trip(plant):
    plant.inject("pump", "trip", code=4)
    with plant.client() as c:
        assert poll_pump(c, 3) == "fault 4"


def test_survives_a_silent_device(plant):
    plant.inject("pump", "no_response")
    with plant.client(timeout=0.2) as c:
        assert poll_pump(c, 3) == "offline (TimeoutError)"


def test_survives_a_busy_device(plant):
    plant.inject("pump", "busy")
    with plant.client() as c:
        assert poll_pump(c, 3) == "offline (ModbusError)"
