"""Modbus framing: CRC-16, MBAP header (Modbus TCP) and RTU frame lengths.

References: Modbus Application Protocol Specification V1.1b3 (PDU, function codes,
exception codes) and Modbus over Serial Line V1.02 (RTU framing, CRC-16).
"""
from __future__ import annotations

import struct

MAX_PDU = 253  # bytes (function code + data)
MBAP_LEN = 7   # transaction id, protocol id, length, unit id

# Exception codes (Application Protocol spec, section 7)
ILLEGAL_FUNCTION = 0x01
ILLEGAL_DATA_ADDRESS = 0x02
ILLEGAL_DATA_VALUE = 0x03
DEVICE_FAILURE = 0x04
ACKNOWLEDGE = 0x05
DEVICE_BUSY = 0x06
GATEWAY_PATH_UNAVAILABLE = 0x0A
GATEWAY_TARGET_FAILED = 0x0B


def _crc_table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_TABLE = _crc_table()


def crc16(data: bytes) -> int:
    """Modbus CRC-16 (polynomial 0xA001 reflected, initial value 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc = (crc >> 8) ^ _TABLE[(crc ^ byte) & 0xFF]
    return crc


def add_crc(frame: bytes) -> bytes:
    """Append the CRC, low byte first as RTU sends it."""
    return frame + struct.pack("<H", crc16(frame))


def check_crc(frame: bytes) -> bool:
    return len(frame) >= 4 and crc16(frame[:-2]) == struct.unpack("<H", frame[-2:])[0]


def mbap(transaction: int, unit: int, pdu: bytes, protocol: int = 0) -> bytes:
    return struct.pack(">HHHB", transaction & 0xFFFF, protocol, len(pdu) + 1, unit) + pdu


def parse_mbap(header: bytes) -> tuple[int, int, int, int]:
    """Return (transaction, protocol, length, unit) of a 7-byte MBAP header."""
    return struct.unpack(">HHHB", header)


def request_length(buffer: bytes) -> int | None:
    """Total length of the RTU *request* at the start of `buffer` (unit .. CRC).

    Returns None when the length cannot be known yet (too few bytes) or at all
    (unknown function code; the caller then falls back to the 3.5-character gap).
    """
    if len(buffer) < 2:
        return None
    function = buffer[1]
    if function in (1, 2, 3, 4, 5, 6):
        return 8
    if function == 7:
        return 4
    if function == 8:
        return 8  # sub-function 00 (echo) with two data bytes; others have the same size
    if function == 11:
        return 4
    if function in (15, 16):
        return 9 + buffer[6] if len(buffer) >= 7 else None
    if function == 22:
        return 10
    if function == 23:
        return 13 + buffer[10] if len(buffer) >= 11 else None
    if function == 43:
        return 7
    return None


def response_length(buffer: bytes) -> int | None:
    """Total length of the RTU *response* at the start of `buffer` (for clients)."""
    if len(buffer) < 2:
        return None
    function = buffer[1]
    if function & 0x80:
        return 5
    if function in (1, 2, 3, 4, 12, 17, 23):
        return 5 + buffer[2] if len(buffer) >= 3 else None
    if function in (5, 6, 8, 11, 15, 16):
        return 8
    if function == 7:
        return 5
    if function == 22:
        return 10
    return None


def t35(baudrate: int) -> float:
    """Silent interval that ends an RTU frame: 3.5 characters, 1.75 ms above 19200 Bd."""
    if baudrate > 19200:
        return 0.00175
    return 3.5 * 11 / baudrate
