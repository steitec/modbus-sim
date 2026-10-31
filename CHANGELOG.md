# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## 0.1.0 - 2026-10-31

- Modbus TCP, RTU over TCP, and RTU on a serial port (pyserial) or a virtual
  serial port (pty, Linux/macOS); several devices by unit ID behind one server,
  RTU broadcast
- Function codes 01-08 (08: sub-function 00), 15, 16, 22, 23 and 43/14 (device
  identification) with the exceptions of the specification
- YAML profiles: points with type (16/32/64-bit integers, floats, strings, bits),
  word order (ABCD/CDAB/BADC/DCBA), scale, access, limits, signal sources
- Bundled devices with behaviour: `energy-meter`, `temperature-controller`, `vfd`
- Bench files for several devices with per-device overrides
- Fault injection (`no_response`, `delay`, `exception`, `busy`, `drop_connection`,
  `bad_crc`, `wrong_transaction`, `wrong_unit`, `truncate`, `freeze`, `drift`, `trip`,
  `sensor_break`, `phase_loss`) through a JSON control channel and the CLI
- Command line (`serve`, `profiles`, `map`, `read`, `write`, `ident`, `inject`, `clear`,
  `get`, `set`, `list`) and a small Python client
- pytest plugin with the `modbus_bench` and `modbus_bench_factory` fixtures
- Test suite cross-checks the simulator against pymodbus
