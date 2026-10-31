import struct

import pytest

from modbus_sim.device import Device
from modbus_sim.profile import load_profile, profile_from_dict


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_device(clock):
    def make(profile="vfd", overrides=None, unit=1, **kwargs):
        if isinstance(profile, dict):
            prof = profile_from_dict(profile, overrides)
        else:
            prof = load_profile(profile, overrides)
        return Device(prof, unit=unit, clock=clock, seed=1, **kwargs)
    return make


def read(device, function, address, count):
    """Send a read request, return the register words (or bit list) or the exception code."""
    response = device.handle(struct.pack(">BHH", function, address, count))
    if response[0] & 0x80:
        return ("exception", response[1])
    if function in (1, 2):
        data = response[2:]
        return [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]
    return list(struct.unpack(f">{count}H", response[2:]))


def write(device, address, *words):
    if len(words) == 1:
        response = device.handle(struct.pack(">BHH", 6, address, words[0]))
    else:
        response = device.handle(struct.pack(f">BHHB{len(words)}H", 16, address, len(words),
                                             2 * len(words), *words))
    return ("exception", response[1]) if response[0] & 0x80 else "ok"
