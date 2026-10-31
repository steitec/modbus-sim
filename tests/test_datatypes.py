import math

import pytest

from modbus_sim import datatypes as dt


@pytest.mark.parametrize("order, words", [
    ("ABCD", [0x4049, 0x0FDB]),
    ("CDAB", [0x0FDB, 0x4049]),
    ("BADC", [0x4940, 0xDB0F]),
    ("DCBA", [0xDB0F, 0x4940]),
])
def test_float32_word_orders(order, words):
    assert dt.encode(math.pi, "float32", order) == words
    assert dt.decode(words, "float32", order) == pytest.approx(math.pi, rel=1e-7)


def test_uint64_cdab_reverses_all_words():
    words = dt.encode(0x0001_0002_0003_0004, "uint64", "CDAB")
    assert words == [4, 3, 2, 1]
    assert dt.decode(words, "uint64", "CDAB") == 0x0001_0002_0003_0004


def test_16_bit_types_ignore_word_order():
    assert dt.encode(0x1234, "uint16", "DCBA") == [0x1234]
    assert dt.encode(-1, "int16") == [0xFFFF]
    assert dt.decode([0xFFFF], "int16") == -1


def test_scale_rounds_and_saturates():
    assert dt.encode(21.37, "int16", scale=0.1) == [214]
    assert dt.decode([214], "int16", scale=0.1) == pytest.approx(21.4)
    assert dt.encode(1e9, "int16") == [0x7FFF]
    assert dt.encode(-5, "uint16") == [0]
    assert dt.encode(math.nan, "uint16") == [0]


def test_float32_overflow_becomes_infinity():
    assert dt.decode(dt.encode(1e40, "float32"), "float32") == math.inf


def test_string_padded_and_truncated():
    assert dt.encode("AB", "string", length=2) == [0x4142, 0]
    assert dt.decode([0x4142, 0], "string") == "AB"
    assert dt.encode("ABCDE", "string", length=2) == [0x4142, 0x4344]


def test_register_counts():
    assert dt.registers("float64") == 4
    assert dt.registers("string", 8) == 8
    with pytest.raises(ValueError):
        dt.registers("string")
    with pytest.raises(ValueError):
        dt.registers("bool")


def test_fits():
    assert dt.fits(3276.7, "int16", 0.1)
    assert not dt.fits(3276.8, "int16", 0.1)
    assert dt.fits(1e30, "float32")
