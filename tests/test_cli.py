import os
import signal
import subprocess
import sys
import time

import pytest

from modbus_sim.bench import Bench
from modbus_sim.cli import main


@pytest.fixture
def bench():
    b = Bench.from_dict({"devices": [{"profile": "vfd", "unit": 3, "name": "pump"},
                                     {"profile": "energy-meter", "unit": 1}]})
    b.start(port=0, control_port=0)
    yield b
    b.stop()


def run(capsys, *argv):
    code = main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_profiles_and_map(capsys):
    code, out, _ = run(capsys, "profiles")
    assert code == 0 and "vfd" in out and "energy-meter" in out
    code, out, _ = run(capsys, "map", "energy-meter")
    assert "voltage_l1" in out and "float32 CDAB" in out and "300001" in out
    code, out, _ = run(capsys, "map", "temperature-controller", "--markdown")
    assert out.splitlines()[3].startswith("| table |") and "000001" in out


def test_read_write_ident(bench, capsys):
    port = bench.port
    assert run(capsys, "write", "hr", "1", "12.5", "--scale", "0.01", "-u", 3,
               "--port", port)[0] == 0
    code, out, _ = run(capsys, "read", "holding", "1", "--scale", "0.01", "-u", 3,
                       "--port", port)
    assert out.strip() == "holding 1: 12.5  [04e2]"
    code, out, _ = run(capsys, "read", "input", "0", "-c", 2, "-t", "float32", "-o", "CDAB",
                       "--port", port)
    assert out.count("input") == 2 and "input 2:" in out
    code, out, _ = run(capsys, "read", "di", "0", "-c", 4, "-u", 3, "--port", port)
    assert out.splitlines() == ["discrete 0: 1", "discrete 1: 0", "discrete 2: 0",
                                "discrete 3: 0"]
    code, out, _ = run(capsys, "ident", "-u", 3, "--port", port)
    assert "product code: SIM-FC1" in out
    code, _, err = run(capsys, "read", "hr", "2", "-u", 3, "--port", port)
    assert code == 1 and "illegal data address" in err
    code, _, err = run(capsys, "write", "hr", "1", "70000", "-u", 3, "--port", port)
    assert code != 0


def test_write_float_and_coil(capsys, tmp_path):
    profile = tmp_path / "p.yaml"
    profile.write_text("name: p\npoints:\n"
                       "  - {name: f, table: holding, address: 0, type: float32}\n"
                       "  - {name: c, table: coil, address: 0}\n")
    with Bench.single(str(profile)) as b:
        assert run(capsys, "write", "hr", 0, "-2.5", "-t", "float32", "--port", b.port)[0] == 0
        assert b.value(1, "f") == -2.5
        assert run(capsys, "write", "coil", 0, "1", "--port", b.port)[0] == 0
        assert b.value(1, "c") is True
        code, _, err = run(capsys, "write", "coil", 0, "2", "--port", b.port)
        assert code != 0


def test_control_commands(bench, capsys):
    ctl = ("--control-port", bench.control_port)
    assert run(capsys, "inject", "pump", "trip", "code=5", *ctl)[0] == 0
    code, out, _ = run(capsys, "list", *ctl)
    assert 'trip {"code": 5}' in out and "unit   1  energy-meter" in out
    code, out, _ = run(capsys, "get", 3, "fault_code", *ctl)
    assert out.strip() == "fault_code: 5"
    assert run(capsys, "set", "pump", "speed_reference=20", *ctl)[0] == 0
    assert bench.value("pump", "speed_reference") == 20
    assert run(capsys, "clear", "pump", *ctl)[0] == 0
    code, _, err = run(capsys, "inject", "pump", "bogus", *ctl)
    assert code != 0 and "unknown fault" in str(err) + "bogus"


def test_serve_needs_profile_or_bench(capsys):
    code, _, err = run(capsys, "serve")
    assert code == 2 and "give a profile" in err


def test_unknown_profile(capsys):
    code, _, err = run(capsys, "map", "toaster")
    assert code == 2 and "unknown profile" in err


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM")
def test_serve_process_stops_cleanly_on_sigterm():
    proc = subprocess.Popen([sys.executable, "-m", "modbus_sim", "serve", "vfd", "--port", "0",
                             "--control-port", "0"], stdout=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        assert line.startswith("modbus-sim: 1 device (unit 1) on modbus tcp 127.0.0.1:")
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
