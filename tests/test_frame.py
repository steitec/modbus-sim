from modbus_sim import frame


def test_crc_of_spec_example():
    # Modbus over Serial Line V1.02, appendix B: "02 07" -> CRC 0x1241, sent 41 12
    assert frame.add_crc(bytes([0x02, 0x07])) == bytes([0x02, 0x07, 0x41, 0x12])


def test_crc_of_well_known_read_request():
    # unit 1, read holding registers 0, count 10 -> C5 CD
    assert frame.add_crc(bytes.fromhex("01030000000A")).hex() == "01030000000ac5cd"


def test_check_crc():
    good = frame.add_crc(bytes.fromhex("010300000001"))
    assert frame.check_crc(good)
    assert not frame.check_crc(good[:-1] + bytes([good[-1] ^ 1]))
    assert not frame.check_crc(b"\x01\x03")


def test_mbap_round_trip():
    packet = frame.mbap(0x1234, 7, b"\x03\x00\x00\x00\x02")
    assert packet.hex() == "123400000006070300000002"
    assert frame.parse_mbap(packet[:7]) == (0x1234, 0, 6, 7)


def test_request_lengths():
    assert frame.request_length(b"\x01") is None
    assert frame.request_length(bytes.fromhex("0103")) == 8
    assert frame.request_length(bytes.fromhex("011000000002")) is None
    assert frame.request_length(bytes.fromhex("01100000000204")) == 13
    assert frame.request_length(bytes.fromhex("012b")) == 7
    assert frame.request_length(bytes.fromhex("0141")) is None


def test_response_lengths():
    assert frame.response_length(bytes.fromhex("0183")) == 5
    assert frame.response_length(bytes.fromhex("010304")) == 9
    assert frame.response_length(bytes.fromhex("0106")) == 8


def test_t35():
    assert frame.t35(115200) == 0.00175
    assert abs(frame.t35(9600) - 0.00401) < 1e-5
