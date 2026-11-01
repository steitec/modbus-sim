"""Profiles after real register maps: addresses and encodings as documented."""
import math
import struct

import pytest

from modbus_sim import datatypes as dt
from modbus_sim.profile import ProfileError

from conftest import read, write


def f32(words):
    return dt.decode(words, "float32", "ABCD")


def f32_words(value):
    return dt.encode(value, "float32", "ABCD")


# --- Eastron SDM630 (protocol V1.5) -------------------------------------------------

def sdm(dev, register):
    """Read one float by its documented 3xxxx/4xxxx register number."""
    if register >= 40001:
        return f32(read(dev, 3, register - 40001, 2))
    return f32(read(dev, 4, register - 30001, 2))


def test_sdm630_documented_addresses(make_device):
    dev = make_device("eastron-sdm630")
    assert sdm(dev, 30001) == pytest.approx(230, rel=0.02)          # phase 1 volts
    assert sdm(dev, 30071) == 50.0                                    # frequency, 0x46
    assert sdm(dev, 30201) == pytest.approx(230 * math.sqrt(3), rel=0.02)   # L1-L2, 0xC8
    assert sdm(dev, 30031) == pytest.approx(-0.95)       # inductive pf is negative
    assert sdm(dev, 30255) == pytest.approx(0.95)        # sign-inverted total pf
    assert sdm(dev, 30037) == pytest.approx(math.degrees(math.acos(0.95)))
    assert 1 < sdm(dev, 30235) < 3                       # voltage THD
    assert sdm(dev, 40021) == 1.0                         # node address
    assert sdm(dev, 40029) == 2.0                         # 9600 baud


def test_sdm630_block_read_across_gaps(make_device):
    dev = make_device("eastron-sdm630")
    words = read(dev, 4, 0, 80)                # 40 parameters, as the meter allows
    assert len(words) == 80
    assert words[44:46] == [0, 0]              # 30045 is not in the map: reads 0
    assert f32(words[0:2]) == pytest.approx(230, rel=0.02)


def test_sdm630_holding_writes(make_device):
    dev = make_device("eastron-sdm630")
    assert write(dev, 20, *f32_words(5.0)) == "ok"
    assert sdm(dev, 40021) == 5.0
    assert write(dev, 20, *f32_words(300.0)) == ("exception", 3)     # 1-247
    assert write(dev, 14, *f32_words(1.0)) == ("exception", 2)       # password lock: ro


def test_sdm630_values_are_consistent(make_device):
    dev = make_device("eastron-sdm630")
    currents = [sdm(dev, r) for r in (30007, 30009, 30011)]
    assert sdm(dev, 30049) == pytest.approx(sum(currents), rel=0.01)
    a, b, c = currents
    neutral = math.sqrt(a * a + b * b + c * c - a * b - b * c - c * a)
    assert sdm(dev, 30225) == pytest.approx(neutral, rel=0.05)
    p = sdm(dev, 30053)
    s = sdm(dev, 30057)
    q = sdm(dev, 30061)
    assert p == pytest.approx(0.95 * s, rel=0.01)
    assert q == pytest.approx(math.sqrt(s * s - p * p), rel=0.05)


def test_sdm630_energy_and_demand(make_device, clock):
    dev = make_device("eastron-sdm630")
    power = sdm(dev, 30053)
    clock.advance(3600)
    imported = sdm(dev, 30073)
    assert imported == pytest.approx(power / 1000, rel=0.1)
    assert sdm(dev, 30343) == pytest.approx(imported)                  # total kWh
    per_phase = sum(sdm(dev, r) for r in (30347, 30349, 30351))
    assert per_phase == pytest.approx(imported, rel=1e-4)
    assert sdm(dev, 30077) > 0                                          # import kvarh
    assert sdm(dev, 30083) == pytest.approx(sdm(dev, 30049), rel=0.1)  # Ah over 1 h
    demand = sdm(dev, 30085)                  # one demand period: 1 - 1/e of the power
    assert demand == pytest.approx(power * (1 - math.exp(-1)), rel=0.1)
    assert sdm(dev, 30087) >= demand
    dev.handle(bytes([5, 0, 0, 0xFF, 0]))     # simulator-only reset coil
    assert sdm(dev, 30073) == 0.0 and sdm(dev, 30087) == 0.0


def test_sdm630_phase_loss(make_device):
    dev = make_device("eastron-sdm630")
    dev.faults.inject("phase_loss", phase=3)
    assert sdm(dev, 30005) == 0.0 and sdm(dev, 30011) == 0.0
    assert sdm(dev, 30001) > 200


def test_pf_sign_must_be_one(make_device):
    with pytest.raises(ProfileError, match="pf_sign"):
        make_device("eastron-sdm630", {"model_params": {"pf_sign": 2}})


# --- Omron E5CC (H175, two-byte mode) -------------------------------------------------
# H175 4-4-3: operation command H'0101 = stop; with communications writing off, writes
# answer error code 04 (operation error); a wrong command code answers 03.

def command(dev, value):
    response = dev.handle(struct.pack(">BHH", 6, 0x0000, value))
    return ("exception", response[1]) if response[0] & 0x80 else "ok"


def test_e5cc_writes_need_communications_writing(make_device):
    dev = make_device("omron-e5cc")
    assert write(dev, 0x2103, 1000) == ("exception", 4)    # operation error
    assert command(dev, 0x0101) == ("exception", 4)        # stop refused as well
    assert command(dev, 0x0001) == "ok"                     # communications writing on
    assert write(dev, 0x2103, 1000) == "ok"
    assert read(dev, 3, 0x2002, 1) == [1000]                # internal SP = 100.0 degC
    assert command(dev, 0x0000) == "ok"                     # off again
    assert write(dev, 0x2103, 500) == ("exception", 4)
    assert command(dev, 0x0001) == "ok"
    assert write(dev, 0x2103, 9999) == ("exception", 3)    # outside the SP limits


def test_e5cc_monitor_block_and_constants(make_device):
    dev = make_device("omron-e5cc")
    words = read(dev, 3, 0x2000, 6)
    assert words[0] == pytest.approx(220, abs=2)            # PV 22.0 degC
    assert words[1] == 0                                    # status: nothing set
    assert words[3] == 0 and words[5] == 0                  # heater current, MV cooling
    assert read(dev, 3, 0x2410, 1) == [1]                   # one decimal
    assert read(dev, 3, 0x2404, 1) == [0]


def test_e5cc_heats_stops_and_manual(make_device, clock):
    dev = make_device("omron-e5cc")
    command(dev, 0x0001)
    write(dev, 0x2103, 1000)
    clock.advance(600)
    pv = dt.decode(read(dev, 3, 0x2000, 1), "int16") / 10
    assert 60 < pv < 110
    assert read(dev, 3, 0x2402, 1)[0] == pytest.approx(pv * 10, abs=2)
    assert command(dev, 0x0101) == "ok"                      # stop
    clock.advance(1)
    assert read(dev, 3, 0x2004, 1) == [0]
    assert command(dev, 0x0100) == "ok"                      # run
    assert command(dev, 0x0901) == "ok"                      # manual
    assert write(dev, 0x2600, 500) == "ok"                   # 50.0 %
    clock.advance(1)
    assert read(dev, 3, 0x2004, 1) == [500]
    assert command(dev, 0x0A00) == ("exception", 3)          # no such command
    assert command(dev, 0x0105) == ("exception", 3)          # bad related information


def test_e5cc_sensor_break(make_device):
    dev = make_device("omron-e5cc")
    dev.faults.inject("sensor_break")
    assert read(dev, 3, 0x2000, 1) == [0x7FFF]
    assert read(dev, 3, 0x2001, 1)[0] & 1 << 6             # input error


def test_e5cc_status_bits(make_device, clock):
    dev = make_device("omron-e5cc")
    assert read(dev, 3, 0x2407, 1) == [0]                   # run, writing off, auto
    command(dev, 0x0001)
    assert read(dev, 3, 0x2407, 1) == [1 << 9]              # bit 25: writing on
    write(dev, 0x2103, 1000)
    clock.advance(5)
    low = read(dev, 3, 0x2001, 1)[0]
    assert low & 1 << 8                                      # control output (heating)
    assert read(dev, 3, 0x2406, 1) == [low]
    command(dev, 0x0101)                                     # stop
    command(dev, 0x0901)                                     # manual
    command(dev, 0x0401)                                     # RAM write mode
    assert read(dev, 3, 0x2407, 1) == [1 << 4 | 1 << 8 | 1 << 9 | 1 << 10]


def test_generic_controller_still_writable(make_device):
    dev = make_device("temperature-controller")
    assert write(dev, 0, 900) == "ok"


# --- SunSpec inverter (models 1 + 103) --------------------------------------------

def sunspec_models(dev):
    """Walk the model chain like a SunSpec client does."""
    assert dt.decode(read(dev, 3, 40000, 2), "string") == "SunS"
    address, found = 40002, []
    while True:
        model_id, length = read(dev, 3, address, 2)
        found.append((model_id, address, length))
        if model_id == 0xFFFF:
            return found
        address += 2 + length


def test_sunspec_model_chain(make_device):
    dev = make_device("sunspec-inverter")
    assert sunspec_models(dev) == [(1, 40002, 66), (103, 40070, 50), (0xFFFF, 40122, 0)]
    words = read(dev, 3, 40002, 68)          # whole common model in one read
    assert dt.decode(words[2:18], "string") == "steitec"
    assert dt.decode(words[50:66], "string") == "SIM0000001"


def sf(dev, address):
    return dt.decode(read(dev, 3, address, 1), "int16")


def scaled(dev, address, sf_address, kind="uint16"):
    return dt.decode(read(dev, 3, address, 1), kind) * 10 ** sf(dev, sf_address)


def test_sunspec_values_with_scale_factors(make_device):
    dev = make_device("sunspec-inverter")
    block = read(dev, 3, 40070, 52)           # model 103 in one read
    assert block[:2] == [103, 50]
    assert read(dev, 3, 40108, 1) == [4]                         # MPPT at noon
    power = scaled(dev, 40084, 40085, "int16")
    expected = 10000 * math.sin(math.pi * 6 / 14)                 # 12:00, sun 6-20
    assert power == pytest.approx(expected, rel=0.1)
    assert scaled(dev, 40086, 40087) == 50.0
    assert scaled(dev, 40080, 40083) == pytest.approx(230, rel=0.02)
    assert scaled(dev, 40072, 40076) == pytest.approx(power / 230, rel=0.1)
    assert scaled(dev, 40092, 40093, "int16") == pytest.approx(100)    # PF in percent
    dc = scaled(dev, 40101, 40102, "int16")
    assert dc > power
    assert read(dev, 3, 40105, 1) == [0x8000]                    # not implemented
    assert read(dev, 3, 40114, 2) == [0xFFFF, 0xFFFF]


def test_sunspec_energy_and_night(make_device, clock):
    dev = make_device("sunspec-inverter")
    clock.advance(3600)
    wh = dt.decode(read(dev, 3, 40094, 2), "uint32")
    assert 8000 < wh < 10000
    clock.advance(9 * 3600)                                       # 22:00
    assert read(dev, 3, 40108, 1) == [2]                          # sleeping
    assert read(dev, 3, 40084, 1) == [0]
    assert dt.decode(read(dev, 3, 40094, 2), "uint32") > wh


def test_sunspec_faults(make_device):
    dev = make_device("sunspec-inverter")
    dev.faults.inject("grid_loss")
    assert read(dev, 3, 40108, 1) == [7]                          # fault
    assert dt.decode(read(dev, 3, 40110, 2), "uint32") == 1 << 4  # GRID_DISCONNECT
    assert read(dev, 3, 40086, 1) == [0]
    dev.faults.clear("grid_loss")
    dev.faults.inject("over_temp")
    assert read(dev, 3, 40108, 1) == [5]                          # throttled
    assert dt.decode(read(dev, 3, 40084, 1), "int16") <= 5000
    assert dt.decode(read(dev, 3, 40110, 2), "uint32") == 1 << 7
    dev.faults.clear()
    dev.faults.inject("ground_fault")
    assert dt.decode(read(dev, 3, 40110, 2), "uint32") == 1
    dev.faults.clear()
    assert read(dev, 3, 40108, 1) == [4]


def test_sunspec_read_only_and_device_address(make_device):
    dev = make_device("sunspec-inverter")
    assert write(dev, 40084, 1) == ("exception", 2)
    assert write(dev, 40068, 7) == "ok"
    assert read(dev, 3, 40068, 1) == [7]


def test_inverter_params(make_device):
    with pytest.raises(ProfileError, match="sunset"):
        make_device("sunspec-inverter", {"model_params": {"sunrise": 20, "sunset": 6}})
