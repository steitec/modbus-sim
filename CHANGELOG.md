# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## 0.2.1 - 2026-11-05

- Docker image on `ghcr.io/steitec/modbus-sim`: runs the profiles after real
  devices on Modbus TCP, with the control channel for fault injection

## 0.2.0 - 2026-11-01

- Profiles after the published register maps of real devices:
  `eastron-sdm630` (Eastron SDM630 protocol V1.5, float32 ABCD, input and set-up
  registers), `omron-e5cc` (Omron E5CC two-byte mode with status words, operation
  commands and the communications-writing enable) and `sunspec-inverter` (SunSpec models 1 and
  103 with scale factors)
- New model `pv_inverter`: day curve, SunSpec operating states and event bits,
  faults `grid_loss`, `ground_fault`, `over_temp`
- `energy_meter` model: apparent and reactive power per phase, phase angles,
  averages, neutral current, per-phase import/export/total kWh and kvarh, kVAh,
  Ah, demand values with maxima; parameters `pf_sign` and `demand_period`
- `temperature_controller` model: operation command register, an optional
  write enable (`comms_write`) and the Omron E5@C status words
- A model can refuse a write with exception 04 instead of 03 (the E5CC answers
  04, operation error, while communications writing is off)
- Browser demo at https://modbus-sim.steitec.net (`web/`): the three real-device
  profiles in Pyodide with a Modbus terminal that shows the TCP frames, live device
  fronts and fault buttons; example bench `examples/real-devices.yaml`
- Fixed: the word order table named Eastron among CDAB devices; Eastron meters
  send the most significant register first (ABCD)

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
