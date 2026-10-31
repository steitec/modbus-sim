import struct

import pytest

from modbus_sim import datatypes as dt
from modbus_sim.profile import ProfileError

from conftest import read, write

GENERIC = {
    "name": "generic",
    "points": [
        {"name": "a", "table": "holding", "address": 0, "value": 7},
        {"name": "b", "table": "holding", "address": 1, "type": "float32", "value": 1.5},
        {"name": "c", "table": "holding", "address": 5, "type": "int16", "scale": 0.1,
         "min": -10, "max": 10, "value": 2.5},
        {"name": "ro", "table": "holding", "address": 6, "access": "ro", "value": 3},
        {"name": "s", "table": "input", "address": 0, "source": {"type": "constant",
                                                                  "value": 42}},
        {"name": "coil", "table": "coil", "address": 3, "value": True},
        {"name": "di", "table": "discrete", "address": 0, "value": True},
        {"name": "text", "table": "holding", "address": 10, "type": "string", "length": 2,
         "value": "OK"},
    ],
}


def test_read_holding_registers(make_device):
    dev = make_device(GENERIC)
    assert read(dev, 3, 0, 3) == [7, *dt.encode(1.5, "float32")]
    assert read(dev, 3, 5, 2) == [25, 3]
    assert read(dev, 3, 10, 2) == [0x4F4B, 0]


def test_gaps_are_exception_02_or_zero(make_device):
    assert read(make_device(GENERIC), 3, 0, 4) == ("exception", 2)
    zero = make_device(GENERIC, {"gaps": "zero"})
    assert read(zero, 3, 0, 5) == [7, *dt.encode(1.5, "float32"), 0, 0]


def test_quantity_limits_are_exception_03(make_device):
    dev = make_device(GENERIC, {"gaps": "zero"})
    assert read(dev, 3, 0, 0) == ("exception", 3)
    assert read(dev, 3, 0, 126) == ("exception", 3)
    assert read(dev, 3, 0, 125) != ("exception", 3)
    assert read(dev, 1, 0, 2001) == ("exception", 3)
    assert read(dev, 3, 0xFFFF, 2) == ("exception", 2)


def test_wrong_pdu_length_is_exception_03(make_device):
    dev = make_device(GENERIC)
    assert dev.handle(b"\x03\x00\x00\x00") == b"\x83\x03"


def test_unknown_function_is_exception_01(make_device):
    assert make_device(GENERIC).handle(b"\x41\x00") == b"\xc1\x01"


def test_bits(make_device):
    dev = make_device(GENERIC, {"gaps": "zero"})
    assert read(dev, 1, 0, 4) == [False, False, False, True]
    assert read(dev, 2, 0, 1) == [True]
    assert dev.handle(struct.pack(">BHH", 5, 3, 0x0000)) == struct.pack(">BHH", 5, 3, 0)
    assert read(dev, 1, 3, 1) == [False]
    assert dev.handle(struct.pack(">BHH", 5, 3, 0x1234)) == b"\x85\x03"
    assert dev.handle(struct.pack(">BHH", 5, 4, 0xFF00)) == b"\x85\x02"


def test_write_coils(make_device):
    dev = make_device(GENERIC)
    assert dev.handle(bytes([15, 0, 3, 0, 1, 1, 1])) == bytes([15, 0, 3, 0, 1])
    assert dev.handle(bytes([15, 0, 3, 0, 1, 2, 1, 0])) == b"\x8f\x03"  # bad byte count


def test_write_and_read_back(make_device):
    dev = make_device(GENERIC)
    assert write(dev, 0, 99) == "ok"
    assert write(dev, 1, *dt.encode(-2.25, "float32")) == "ok"
    assert read(dev, 3, 0, 3) == [99, *dt.encode(-2.25, "float32")]
    assert dev.points["b"].value() == -2.25


def test_partial_multi_register_write_is_exception_02(make_device):
    dev = make_device(GENERIC)
    assert write(dev, 1, 0x4000) == ("exception", 2)
    assert write(dev, 0, 1, 0x4000) == ("exception", 2)
    assert read(dev, 3, 0, 1) == [7]  # nothing written


def test_read_only_and_limits(make_device):
    dev = make_device(GENERIC)
    assert write(dev, 6, 1) == ("exception", 2)
    assert write(dev, 5, 101) == ("exception", 3)  # 10.1 > max 10
    assert write(dev, 5, 0xFF9C) == "ok"           # -10.0
    assert dev.points["c"].value() == pytest.approx(-10.0)


def test_written_source_point_holds(make_device):
    dev = make_device({**GENERIC, "points": GENERIC["points"] + [
        {"name": "w", "table": "holding", "address": 20, "access": "rw",
         "source": {"type": "constant", "value": 5}}]})
    assert read(dev, 3, 20, 1) == [5]
    assert write(dev, 20, 9) == "ok"
    assert read(dev, 3, 20, 1) == [9]


def test_mask_write(make_device):
    dev = make_device(GENERIC)
    write(dev, 0, 0x12)
    # spec example: current 0x12, and 0xF2, or 0x25 -> 0x17
    assert dev.handle(struct.pack(">BHHH", 22, 0, 0xF2, 0x25)) == struct.pack(">BHHH", 22, 0,
                                                                                0xF2, 0x25)
    assert read(dev, 3, 0, 1) == [0x17]
    assert dev.handle(struct.pack(">BHHH", 22, 3, 0, 0)) == b"\x96\x02"


def test_read_write_multiple(make_device):
    dev = make_device(GENERIC)
    response = dev.handle(struct.pack(">BHHHHBH", 23, 0, 1, 0, 1, 2, 55))
    assert response == bytes([23, 2, 0, 55])


def test_diagnostics_echo(make_device):
    dev = make_device(GENERIC)
    assert dev.handle(b"\x08\x00\x00\xa5\x37") == b"\x08\x00\x00\xa5\x37"
    assert dev.handle(b"\x08\x00\x01\x00\x00") == b"\x88\x01"


def test_device_identification(make_device):
    dev = make_device("vfd")
    response = dev.handle(b"\x2b\x0e\x01\x00")
    assert response[:7] == bytes([0x2B, 0x0E, 1, 0x82, 0, 0, 3])
    assert b"steitec" in response and b"SIM-FC1" in response
    regular = dev.handle(b"\x2b\x0e\x02\x00")
    assert regular[6] == 6  # no user application name
    single = dev.handle(b"\x2b\x0e\x04\x05")
    assert single[6] == 1 and single.endswith(b"SIM-FC1")
    assert dev.handle(b"\x2b\x0e\x04\x40") == b"\xab\x02"
    assert dev.handle(b"\x2b\x0e\x05\x00") == b"\xab\x03"
    assert dev.handle(b"\x2b\x0e\x01\x40")[:7] == bytes([0x2B, 0x0E, 1, 0x82, 0, 0, 3])


def test_injected_exception_and_busy(make_device):
    dev = make_device(GENERIC)
    dev.faults.inject("exception", code=4, function=6)
    assert read(dev, 3, 0, 1) == [7]  # other function code is unaffected
    assert write(dev, 0, 1) == ("exception", 4)
    assert write(dev, 0, 1) == "ok"   # count=1 used up
    dev.faults.inject("busy", count=2)
    assert read(dev, 3, 0, 1) == ("exception", 6)
    assert read(dev, 3, 0, 1) == ("exception", 6)
    assert read(dev, 3, 0, 1) == [1]


def test_freeze_and_drift(make_device, clock):
    dev = make_device("temperature-controller")
    dev.faults.inject("freeze")
    before = read(dev, 4, 0, 2)
    clock.advance(300)
    assert read(dev, 4, 0, 2) == before
    dev.faults.clear("freeze")
    assert read(dev, 4, 0, 1) != before[:1]
    dev.faults.inject("drift", point="setpoint", per_second=0.5)
    start = dev.points["setpoint"].value()
    clock.advance(10)
    assert dev.points["setpoint"].value() == pytest.approx(start + 5)
    assert dev.faults.snapshot()["drift"] == {"setpoint": 0.5}
    dev.faults.clear()
    assert dev.faults.snapshot() == {}


def test_model_binding_errors(make_device):
    with pytest.raises(ProfileError, match="no variable"):
        make_device({"name": "x", "model": "vfd", "points": [
            {"name": "p", "table": "holding", "address": 0, "model": "nope"}]})
    with pytest.raises(ProfileError, match="read-only"):
        make_device({"name": "x", "model": "vfd", "points": [
            {"name": "p", "table": "holding", "address": 0, "model": "status_word"}]})


def test_exception_status(make_device):
    dev = make_device("vfd")
    assert dev.handle(b"\x07") == bytes([7, 0x01])  # ready
    dev.faults.inject("trip", code=3)
    assert dev.handle(b"\x07") == bytes([7, 0x08])  # fault
