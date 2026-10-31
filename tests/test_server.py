import socket
import struct
import sys
import time

import pytest

from modbus_sim import datatypes as dt
from modbus_sim import frame
from modbus_sim.bench import Bench, BenchError
from modbus_sim.client import ModbusClient, ModbusError, ProtocolError
from modbus_sim.control import send_control

PLANT = {"devices": [
    {"profile": "energy-meter", "unit": 1, "name": "meter"},
    {"profile": "temperature-controller", "unit": 2, "name": "oven"},
    {"profile": "vfd", "unit": 3, "name": "pump", "model_params": {"time_scale": 20}},
]}

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="pty needs POSIX")


@pytest.fixture
def bench():
    b = Bench.from_dict(PLANT)
    b.start(port=0, control_port=0)
    yield b
    b.stop()


def test_tcp_read_write_and_units(bench):
    with bench.client() as c:
        volts = dt.decode(c.read_input_registers(0, 2, unit=1), "float32", "CDAB")
        assert volts == pytest.approx(230, rel=0.02)
        c.write_register(1, 2500, unit=3)
        c.write_register(0, 1, unit=3)
        time.sleep(0.4)  # 20x time scale: 8 s simulated, ramp 5 Hz/s -> 25 Hz reached
        assert c.read_holding_registers(101, 1, unit=3) == [2500]
        with pytest.raises(ModbusError) as err:
            c.read_holding_registers(0, 1, unit=9)
        assert err.value.code == frame.GATEWAY_TARGET_FAILED
        ident = c.read_device_identification(unit=2)
        assert ident[0] == "steitec" and ident[1] == "SIM-TC1"
        assert c.read_coils(0, 1, unit=2) == [True]


def test_tcp_single_device_answers_unit_255():
    with Bench.single("vfd") as b, b.client() as c:
        assert c.read_holding_registers(100, 1, unit=255) == [1]


def test_transport_faults_over_tcp(bench):
    with bench.client(timeout=0.3) as c:
        bench.inject("pump", "no_response")
        with pytest.raises(TimeoutError):
            c.read_holding_registers(100, 1, unit=3)
    with bench.client(timeout=0.5) as c:
        bench.inject(3, "wrong_transaction")
        with pytest.raises(ProtocolError, match="transaction"):
            c.read_holding_registers(100, 1, unit=3)
    with bench.client(timeout=0.5) as c:
        bench.inject(3, "wrong_unit")
        with pytest.raises(ProtocolError, match="unit 4"):
            c.read_holding_registers(100, 1, unit=3)
    with bench.client(timeout=0.5) as c:
        bench.inject(3, "drop_connection")
        with pytest.raises(ConnectionError):
            c.read_holding_registers(100, 1, unit=3)
    with bench.client(timeout=0.3) as c:
        bench.inject(3, "truncate", bytes=2)
        with pytest.raises(TimeoutError):
            c.read_holding_registers(100, 1, unit=3)
    with bench.client(timeout=2) as c:
        bench.inject(3, "delay", seconds=0.3, count=1)
        start = time.monotonic()
        c.read_holding_registers(100, 1, unit=3)
        assert time.monotonic() - start >= 0.28  # timer granularity on Windows
        assert c.read_holding_registers(100, 1, unit=3) == [1]


def test_bad_mbap_length_closes_connection(bench):
    with socket.create_connection(("127.0.0.1", bench.port), timeout=2) as sock:
        sock.sendall(struct.pack(">HHHB", 1, 0, 0, 1))
        try:
            assert sock.recv(10) == b""
        except ConnectionResetError:  # Windows may reset instead of closing
            pass


def test_rtu_over_tcp():
    b = Bench.single("temperature-controller", unit=5)
    b.start(port=0, framing="rtu", control_port=None)
    try:
        with b.client(timeout=0.5) as c:
            assert c.read_holding_registers(0, 1, unit=5) == [600]
            b.inject(5, "bad_crc")
            with pytest.raises(ProtocolError, match="CRC"):
                c.read_holding_registers(0, 1, unit=5)
            assert c.read_holding_registers(0, 1, unit=5) == [600]
            with pytest.raises(TimeoutError):  # other unit: silence, like on a bus
                c.read_holding_registers(0, 1, unit=6)
        with socket.create_connection(("127.0.0.1", b.port), timeout=0.5) as sock:
            sock.sendall(b"\x05\x03\x00\x00\x00\x01\x00\x00")  # wrong CRC: ignored
            with pytest.raises(socket.timeout):
                sock.recv(10)
    finally:
        b.stop()


@posix_only
def test_rtu_on_pty_with_broadcast():
    b = Bench.from_dict({"devices": [{"profile": "vfd", "unit": 1, "name": "a"},
                                     {"profile": "vfd", "unit": 2, "name": "b"}]})
    b.start(pty=True, control_port=None)
    try:
        assert b.path.startswith("/dev/")
        with b.client(timeout=1) as c:
            assert c.read_holding_registers(100, 1, unit=2) == [1]
            c.write_register(1, 1234, unit=0)          # broadcast: both, no answer
            time.sleep(0.1)
            assert c.read_holding_registers(1, 1, unit=1) == [1234]
            assert c.read_holding_registers(1, 1, unit=2) == [1234]
            with pytest.raises(ModbusError) as err:
                c.read_holding_registers(50, 1, unit=1)
            assert err.value.code == 2
    finally:
        b.stop()


def test_control_channel(bench):
    port = bench.control_port
    answer = send_control({"device": "pump", "inject": "trip", "params": {"code": 9}},
                          port=port)
    assert answer == {"ok": True}
    with bench.client() as c:
        assert c.read_holding_registers(105, 1, unit=3) == [9]
    listing = send_control({"list": True}, port=port)["devices"]
    assert listing["pump"]["faults"] == {"trip": {"code": 9}}
    assert listing["pump"]["requests"] >= 1
    assert send_control({"device": 2, "set": {"pv": 99.5}}, port=port) == {"ok": True}
    assert send_control({"device": "oven", "get": ["pv"]}, port=port)["values"] == {"pv": 99.5}
    with bench.client() as c:
        assert c.read_input_registers(0, 1, unit=2) == [995]
    send_control({"device": "oven", "set": {"pv": None}}, port=port)
    assert send_control({"device": "oven", "inject": "nope"}, port=port)["ok"] is False
    assert "unknown device" in send_control({"device": "x", "clear": True}, port=port)["error"]
    assert send_control({"device": "oven", "set": {"pv": "hot"}}, port=port)["ok"] is False
    assert send_control({"device": "oven", "get": True}, port=port)["ok"] is True


def test_pymodbus_reads_and_controls_the_simulator(bench):
    """Cross-check with an independent implementation."""
    pymodbus = pytest.importorskip("pymodbus.client")
    from pymodbus import ModbusException as PyModbusException  # noqa: F401
    client = pymodbus.ModbusTcpClient("127.0.0.1", port=bench.port, timeout=1)
    assert client.connect()
    try:
        regs = client.read_input_registers(0, count=2, device_id=1).registers
        volts = client.convert_from_registers(regs, client.DATATYPE.FLOAT32, word_order="little")
        assert volts == pytest.approx(230, rel=0.02)
        assert not client.write_register(1, 1000, device_id=3).isError()
        assert not client.write_coil(0, True, device_id=2).isError()
        status = client.read_holding_registers(100, count=1, device_id=3).registers[0]
        assert status & 1
        bench.inject("pump", "exception", code=4)
        response = client.read_holding_registers(100, count=1, device_id=3)
        assert response.isError() and response.exception_code == 4
        bits = client.read_discrete_inputs(0, count=3, device_id=2).bits[:3]
        assert bits == [False, False, False]
        response = client.read_holding_registers(500, count=1, device_id=2)
        assert response.isError() and response.exception_code == 2
    finally:
        client.close()


def test_bench_errors():
    with pytest.raises(BenchError, match="unit"):
        Bench.from_dict({"devices": [{"profile": "vfd", "unit": 0}]})
    with pytest.raises(BenchError, match="already used"):
        Bench.from_dict({"devices": [{"profile": "vfd", "unit": 1},
                                     {"profile": "vfd", "unit": 1}]})
    with pytest.raises(BenchError, match="unknown profile"):
        Bench.from_dict({"devices": [{"profile": "toaster", "unit": 1}]})
    b = Bench.from_dict({"devices": [{"profile": "vfd", "unit": 1},
                                     {"profile": "vfd", "unit": 2}]})
    assert list(b.devices) == ["vfd-1", "vfd-2"]
