"""Tests for the Python layer of the browser demo (web/py/demo.py), run on CPython."""

import re
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "web" / "py"))

from demo import CommandError, Demo, parse  # noqa: E402

BENCH = ROOT / "examples" / "real-devices.yaml"

# the chips of web/site/app.js, in order, with a piece of what each prints
TOUR = [
    ("meter", "read input 0 -t float32", "input 0: 2"),
    ("meter", "read input 0 -t float32 -o CDAB", "input 0: "),
    ("meter", "read input 0 -c 3 -t float32", "input 4: 2"),
    ("meter", "read input 0x46 -t float32", "input 70: 50  [4248 0000]"),
    ("controller", "write holding 0x2103 150 --scale 0.1", "exception 0x04"),
    ("controller", "write holding 0 0x0001", "(ok)"),
    ("controller", "write holding 0x2103 150 --scale 0.1", "(ok)"),
    ("controller", "read holding 0x2000 -t int16 --scale 0.1", "holding 8192: 2"),
    ("inverter", "read holding 40000 -c 2 -t string", "'SunS'"),
    ("inverter", "read holding 40002 -c 2", "holding 40003: 66"),
    ("inverter", "read holding 40070 -c 2", "holding 40070: 103"),
    ("inverter", "read holding 40072", "holding 40072: "),
    ("inverter", "read holding 40076 -t int16", "holding 40076: -2"),
]


@pytest.fixture
def demo():
    return Demo(str(BENCH))


def texts(result, kind=None):
    return [line["text"] for line in result["lines"] if kind is None or line["kind"] == kind]


def test_tour(demo):
    """The page's chips, in order, give what the labels promise."""
    for target, line, expect in TOUR:
        result = demo.run(target, line)
        joined = "\n".join(texts(result))
        assert expect in joined, (line, joined)


def test_frames_are_modbus_tcp(demo):
    result = demo.run("meter", "read input 0 -t float32")
    tx, rx = texts(result, "tx")[0], texts(result, "rx")[0]
    assert tx == "tx  00 01 00 00 00 06 01 04 00 00 00 02"
    assert rx.startswith("rx  00 01 00 00 00 07 01 04 04 43")     # 230 V = 0x4366....
    assert demo.run("meter", "read input 0")["lines"][0]["text"].startswith("tx  00 02")


def test_word_order_pitfall(demo):
    good = texts(demo.run("meter", "read input 0 -t float32"), "value")[0]
    bad = texts(demo.run("meter", "read input 0 -t float32 -o CDAB"), "value")[0]
    assert float(good.split()[2]) == pytest.approx(230, rel=0.02)
    # The swapped value hangs on the meter's noise and can land anywhere, even near
    # 230, so check the decoding of the words on the wire and not its size.
    shown, high, low = re.fullmatch(r"input 0: (\S+)  \[(\w{4}) (\w{4})\]", bad).groups()
    assert struct.unpack(">f", bytes.fromhex(high + low))[0] == pytest.approx(230, rel=0.02)
    assert shown == f"{struct.unpack('>f', bytes.fromhex(low + high))[0]:g}"


def test_controller_write_protection(demo):
    result = demo.run("controller", "write holding 0x2103 150 --scale 0.1")
    assert texts(result, "error") == ["exception 0x04 (server device failure)"]
    assert not result["state"]["controller"]["comms_write"]
    demo.run("controller", "write holding 0 0x0001")
    result = demo.run("controller", "write holding 0x2103 150 --scale 0.1")
    assert texts(result, "quiet") == ["(ok)"]
    assert result["state"]["controller"]["setpoint"] == pytest.approx(150)
    demo.run("controller", "write holding 0 0x0101")                # stop
    assert not demo.state()["controller"]["running"]


def test_sunspec_chain(demo):
    assert texts(demo.run("inverter", "read holding 40000 -c 2 -t string"), "value") == \
        ["holding 40000: 'SunS'"]
    values = texts(demo.run("inverter", "read holding 40070 -c 2"), "value")
    assert values == ["holding 40070: 103  [0067]", "holding 40071: 50  [0032]"]
    assert texts(demo.run("inverter", "read holding 40076 -t int16"), "value") == \
        ["holding 40076: -2  [fffe]"]


def test_faults_and_reset(demo):
    state = demo.inject("meter-no-response")
    assert state["meter"]["faults"] == ["no_response"]
    result = demo.run("meter", "read input 0 -t float32")
    assert texts(result, "timeout") and not texts(result, "rx")
    assert texts(demo.run("meter", "read input 0 -t float32"), "rx")   # count was 1
    assert demo.inject("meter-phase-loss")["meter"]["voltage_l2"] == 0
    assert demo.inject("controller-sensor-break")["controller"]["pv"] is None
    state = demo.inject("inverter-grid-loss")["inverter"]
    assert state["state"] == "fault" and state["events"] == 16
    demo.run("controller", "write holding 0 1")
    state = demo.reset()
    assert all(not state[name]["faults"] for name in ("meter", "controller", "inverter"))
    assert not state["controller"]["comms_write"]


def test_other_commands(demo):
    assert texts(demo.run("inverter", "ident"), "value") == \
        ["vendor: steitec", "product code: SIM-PV10", "revision: 1.0.0"]
    assert texts(demo.run("meter", "help"))[0].startswith("read  TABLE")
    assert texts(demo.run("meter", "write coil 0 1"), "quiet") == ["(ok)"]
    assert texts(demo.run("meter", "read coil 0"), "value") == ["coil 0: 0"]
    assert texts(demo.run("meter", "read input 1000"), "value") == ["input 1000: 0  [0000]"]
    assert texts(demo.run("inverter", "read input 0"), "error") == \
        ["exception 0x02 (illegal data address)"]
    assert texts(demo.run("inverter", "write holding 40084 1"), "error") == \
        ["exception 0x02 (illegal data address)"]


@pytest.mark.parametrize("line, message", [
    ("", "empty"),
    ("fly", "unknown command"),
    ("read", "usage"),
    ("read tables 0", "table is"),
    ("read input x", "not a number"),
    ("read input 70000", "0-65535"),
    ("read input 0 -c 0", "count"),
    ("read input 0 -c 61", "count"),
    ("read input 0 -t float128", "type"),
    ("read input 0 -o XYZW", "order"),
    ("read input 0 --scale 0", "scale"),
    ("read input 0 -c", "needs a value"),
    ("write input 0 1", "only coil and holding"),
    ("write holding 0", "at least one value"),
    ("help me", "no arguments"),
    ("read input 0 'open", "quotation"),
])
def test_parse_errors(line, message):
    with pytest.raises(CommandError, match=message):
        parse(line)


def test_bad_values_are_reported_not_raised(demo):
    assert texts(demo.run("controller", "write holding 0 70000"), "error") == \
        ["70000 does not fit uint16"]
    assert texts(demo.run("meter", "write coil 0 2"), "error") == ["coil value '2' is not 0/1"]
    assert texts(demo.run("controller", "write holding 0 abc"), "error") == \
        ["'abc' is not a number"]
    assert texts(demo.run("controller", "write holding 0 inf"), "error") == \
        ["'inf' is not a finite number"]
    assert texts(demo.run("meter", "read input 0 -c 60 -t uint64"), "error") == \
        ["that read is too long"]
    with pytest.raises(ValueError):
        demo.run("pump", "read input 0")
    with pytest.raises(ValueError):
        demo.inject("meteor")


def test_control_characters_are_dropped(demo):
    result = demo.run("meter", "read\x00 input 0\x1b")
    assert texts(result, "value")


def test_state_shape(demo):
    s = demo.state()
    assert set(s) == {"meter", "controller", "inverter"}
    assert s["meter"]["frequency"] == 50.0
    assert s["controller"]["running"] and s["controller"]["setpoint"] == 0
    assert s["inverter"]["state"] == "MPPT" and 11.9 < s["inverter"]["hour"] < 12.1
