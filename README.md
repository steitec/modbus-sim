<a href="https://steitec.net"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/steitec/.github/main/brand/steitec-logo-on-dark.svg">
  <img align="right" height="32" alt="steitec" src="https://raw.githubusercontent.com/steitec/.github/main/brand/steitec-logo-on-light.svg">
</picture></a>

# modbus-sim

[![CI](https://github.com/steitec/modbus-sim/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/steitec/modbus-sim/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/modbus-sim?v=0.2.0)](https://pypi.org/project/modbus-sim/)
[![Python](https://img.shields.io/pypi/pyversions/modbus-sim?v=0.2.0)](https://pypi.org/project/modbus-sim/)
[![License](https://img.shields.io/pypi/l/modbus-sim?v=0.2.0)](https://github.com/steitec/modbus-sim/blob/main/LICENSE)
[![Demo](https://img.shields.io/badge/demo-live%20in%20your%20browser-2ea44f)](https://modbus-sim.steitec.net)

Simulated Modbus devices that **behave**: an energy meter whose counters count,
a temperature controller that heats towards its setpoint, a frequency converter
that ramps up when you set the run bit and trips when you tell it to. Three
profiles follow the published register maps of real devices: an Eastron SDM630
energy meter, an Omron E5CC temperature controller and a SunSpec PV inverter.
**[Try them in your browser](https://modbus-sim.steitec.net)**, no install needed.
They speak
Modbus TCP, RTU over TCP and RTU on a serial line or a virtual serial port, so
you can develop and test SCADA links, data loggers, gateways, Node-RED flows or
your own Modbus code without the hardware, also in CI.

Walkthroughs, CI recipes and troubleshooting are in the
[wiki](https://github.com/steitec/modbus-sim/wiki); this README is the reference.

```console
$ modbus-sim serve --bench examples/plant.yaml &
modbus-sim: 3 devices (units 1, 2, 3) on modbus tcp 127.0.0.1:5020, control on :5021
  unit   1  meter  (energy-meter)
  unit   2  oven  (temperature-controller)
  unit   3  pump  (vfd)
$ modbus-sim read input 0 -c 3 -t float32 -o CDAB          # meter: L1-L3 voltage
input 0: 230.201  [336c 4366]
input 2: 229.924  [ec8c 4365]
input 4: 229.215  [3714 4365]
$ modbus-sim write holding 1 30 --scale 0.01 -u 3          # pump: 30 Hz reference
$ modbus-sim write holding 0 1 -u 3                        # control word: run
$ modbus-sim read holding 101 --scale 0.01 -u 3            # two seconds later
holding 101: 21.09  [083d]
$ modbus-sim inject pump trip code=4
$ modbus-sim read holding 105 -u 3                         # fault code
holding 105: 4  [0004]
$ modbus-sim inject oven no_response
$ modbus-sim read input 0 -u 2 --timeout 1
modbus-sim: no (complete) answer
```

## Why

Free Modbus simulators give you a table of registers. Real integrations break
on other things:

- **behaviour**: a drive needs a control word and a ramp before it reports a
  speed, a meter's energy must rise, a controller's process value must follow
  its setpoint. Code that only ever saw static registers has never been tested.
- **the classic pitfalls**: 32-bit values in **word order** ABCD, CDAB, BADC or
  DCBA; integers **scaled** by 0.1 or 0.01; **gaps** in the register map that
  answer exception 02; request **size limits** that answer exception 03;
  0-based protocol addresses against 1-based "40001" references in the manual.
- **failures**: devices that do not answer, answer late, answer busy or with
  an exception, send a bad CRC, a wrong transaction or unit ID, a truncated
  frame, or drop the connection. And device faults: a drive trip, a broken
  temperature sensor, a lost phase.

modbus-sim gives you all of that from a YAML profile, switchable at runtime.

## Install

Requires Python 3.10 or newer; the only dependency is PyYAML.

```bash
pip install modbus-sim
pip install 'modbus-sim[serial]'    # also for real serial ports (pyserial)
```

From source, with the test dependencies:

```bash
git clone https://github.com/steitec/modbus-sim
cd modbus-sim
pip install -e '.[dev,serial]'
pytest
```

## Quick start

```bash
modbus-sim profiles                     # the bundled profiles
modbus-sim map vfd                      # register map of one of them
modbus-sim serve vfd                    # one drive, unit 1, Modbus TCP on 127.0.0.1:5020
modbus-sim serve vfd --host 0.0.0.0     # reachable from the network
modbus-sim serve vfd --rtu-over-tcp     # RTU frames over TCP (as many gateways do)
modbus-sim serve vfd --pty              # RTU on a new virtual serial port (Linux/macOS)
modbus-sim serve vfd --serial /dev/ttyUSB0 --baud 9600 --parity N
modbus-sim serve --bench examples/plant.yaml
```

Port 502 needs root on Linux, so the default is 5020. With `--pty`, the server
prints the device to open, for example `/dev/pts/7`; point your Modbus RTU
master at it. On Windows, use `--serial COM5` with a virtual null-modem pair
such as com0com.

## Profiles

| profile | what it is | in particular |
|---|---|---|
| `energy-meter` | three-phase meter: voltages, currents, powers, power factor, frequency, energy import/export | float32 in **CDAB** word order, Wh counters as uint64, reset coil |
| `temperature-controller` | PI controller heating a first-order process; run/stop, auto/manual, alarm limits | int16 scaled by 0.1; sensor break reads 3276.7 (0x7FFF) |
| `vfd` | frequency converter: control word, speed reference, accel/decel ramps, status word, current, voltage, rpm, run hours | trip is latched until a fault reset (rising edge) and a new run edge |
| `eastron-sdm630` | Eastron SDM630 three-phase meter: the full input register map of protocol V1.5 (volts, amps, powers, power factor, phase angle, THD, demand, per-phase import/export kWh and kvarh) and the set-up holding registers | float32 in **ABCD**, input registers 30001 ff.; unlisted addresses read 0 |
| `omron-e5cc` | Omron E5CC temperature controller in two-byte mode: PV, status, set point, MV, manual MV, operation commands (run/stop, auto/manual, write mode) | writes answer exception 04 until **communications writing** is switched on with operation command 0x0001 |
| `sunspec-inverter` | three-phase PV inverter with the SunSpec map that SMA, Fronius, SolarEdge and others publish: model 1 (common) + model 103, on a day curve | "SunS" at 40000, integer + scale-factor registers, SunSpec states and event bits |

### Profiles after real devices

`eastron-sdm630`, `omron-e5cc` and `sunspec-inverter` are built from public
documentation, so code written against them should work against the real
device's register map:

| profile | source | simplified |
|---|---|---|
| `eastron-sdm630` | Eastron "SDM630 Modbus Protocol" V1.5 | odd register counts and requests over 80 registers are accepted; THD is constant with noise; demand is an exponential average; set-up registers are stored only (no password, restart or register-order switching); a `reset_energy` coil is added |
| `omron-e5cc` | Omron "E5@C Communications Manual" (H175), sections 3-2, 4 and 5 | two-byte mode only; status sets only the bits the model knows (input error, control output, write mode, run/stop, communications writing, auto/manual); no heater current, alarm values or status 2; commands other than communications writing, run/stop, auto/manual and write mode are accepted without effect |
| `sunspec-inverter` | SunSpec model definitions (github.com/sunspec/models) | fixed scale factors; no control models (120-127); generic manufacturer "steitec" |

Each was checked against a third-party client: `sdm_modbus` reads the
simulated SDM630, and the SunSpec Alliance's `pysunspec2` scans and decodes
the inverter. steitec is not affiliated with Eastron, Omron or the SunSpec
Alliance; product names identify the documented register maps only. Firmware
versions of real devices differ, so check your device's documentation.

```console
$ modbus-sim serve omron-e5cc &
$ modbus-sim write holding 0x2103 100 --scale 0.1     # refused: exception 04
$ modbus-sim write holding 0 0x0001                   # communications writing on
$ modbus-sim write holding 0x2103 100 --scale 0.1     # set point 100.0 degC
$ modbus-sim serve sunspec-inverter --port 5030 \
    --control-port 5031 &
$ modbus-sim read holding 40000 -c 2 -t string --port 5030
```

`modbus-sim map PROFILE` prints the complete register map, with the 0-based
protocol address and the conventional reference (`400001` = holding
register 0):

```console
$ modbus-sim map vfd
table     address  ref     point             type          scale  unit  access  limits  notes
discrete  0        100001  ready             bool                       ro
...
holding   0        400001  control_word      uint16                     rw              0: run, 1: reverse, 2: fault reset (rising edge)
holding   1        400002  speed_reference   uint16        0.01   Hz    rw      0..50
holding   100      400101  status_word       uint16                     ro              0: ready, 1: running, 2: at reference, 3: fault, 4: reverse
holding   101      400102  output_frequency  uint16        0.01   Hz    ro
...
```

`--markdown` gives the same as a Markdown table for your documentation.

### Your own profiles

A profile is a YAML file with points. Each point sits in one table at a
0-based address and gets its value from a model variable, a signal source, or
a stored value:

```yaml
name: tank
description: level sensor and two pumps
identification: {vendor: ACME, product: TANK-1, revision: "2.1"}
word_order: CDAB        # default for 32/64-bit points
gaps: error             # unmapped addresses: exception 02 ("zero" reads 0 instead)
points:
  - {name: level, table: input, address: 0, type: float32, unit: m,
     source: [{type: sine, offset: 2.0, amplitude: 0.5, period: 120},
              {type: noise, sigma: 0.01}]}
  - {name: temperature, table: input, address: 2, type: int16, scale: 0.1, unit: degC,
     source: {type: ramp, start: 15, stop: 25, period: 600}}
  - {name: pump1, table: coil, address: 0}
  - {name: pump2, table: coil, address: 1, value: true}
  - {name: max_level, table: holding, address: 0, type: uint16, scale: 0.01, unit: m,
     value: 3.5, min: 0.5, max: 4.0}
  - {name: site, table: holding, address: 10, type: string, length: 8, value: "PLANT A"}
```

- **types**: `bool` (coils, discrete inputs), `uint16`, `int16`, `uint32`,
  `int32`, `float32`, `uint64`, `int64`, `float64`, `string` (with `length` in
  registers)
- **word_order** per profile or per point: `ABCD` (big endian), `CDAB`, `BADC`, `DCBA`
- **scale**: register value × scale = engineering value; integers saturate at
  their range like real devices do
- **access**: `rw` (default for coils and holding registers) or `ro`; writing
  a read-only point is exception 02
- **min/max**: written values outside are exception 03
- **sources**: `constant` (value), `ramp` (start, stop, period), `sine`
  (offset, amplitude, period), `noise` (sigma); a list is added up
- **model**: the bundled profiles bind points to a model (`energy_meter`,
  `temperature_controller`, `vfd`, `pv_inverter`) with `model: <variable>`;
  `model_params` tune it (`time_scale` speeds any model up)
- **bits**: names for the bits of a uint16 register (shown by `map`)

A point named `exception_status` provides the byte returned by function 07.

## Bench files

Several devices behind one server, each with its own unit ID, like a gateway:

```yaml
devices:
  - {profile: energy-meter, unit: 1, name: meter}
  - {profile: temperature-controller, unit: 2, name: oven,
     model_params: {setpoint: 180.0, time_constant: 60.0}}
  - {profile: vfd, unit: 3, name: pump, model_params: {accel_time: 5, decel_time: 5}}
  - {profile: profiles/tank.yaml, unit: 10, word_order: ABCD, values: {site: "PLANT B"}}
```

An entry can override `word_order`, `gaps`, `model_params`, `identification`
and `values` (initial values of stored points). Profile paths are relative to
the bench file.

## Protocol

| function | name | notes |
|---|---|---|
| 01, 02 | read coils / discrete inputs | 1-2000 bits |
| 03, 04 | read holding / input registers | 1-125 registers |
| 05, 06 | write single coil / register | coil value 0xFF00 or 0x0000 |
| 07 | read exception status | from the `exception_status` point |
| 08 | diagnostics | sub-function 00 (return query data) only |
| 15, 16 | write multiple coils / registers | 1-1968 bits, 1-123 registers |
| 22 | mask write register | |
| 23 | read/write multiple registers | write first, then read |
| 43 / 14 | read device identification | basic and regular objects, stream and individual access |

Anything else answers exception 01. Exceptions follow the specification:
02 for unmapped or read-only addresses and for writes that would change only
part of a 32/64-bit value, 03 for bad quantities, byte counts and values
outside `min`/`max`, 04 where a device refuses a write in its present state
(`omron-e5cc` with communications writing off).

**Unit IDs.** On Modbus TCP an unknown unit answers exception 0x0B (gateway
target failed to respond); a single device also answers unit 0 and 255. On
RTU an unknown unit gets no answer, a frame with a bad CRC is ignored, and
unit 0 is a broadcast: write requests reach every device and nobody answers.

## Fault injection

Inject faults into a running bench with `modbus-sim inject DEVICE FAULT
[name=value ...]` (device by name or unit ID), clear them with `modbus-sim
clear DEVICE [FAULT]`:

| fault | parameters | effect |
|---|---|---|
| `no_response` | count=1 | no answer, the master times out |
| `delay` | seconds, count=forever | answers come late |
| `exception` | code, function, count=1 | answers with that exception code, optionally only for one function code |
| `busy` | count=1 | exception 06 |
| `drop_connection` | count=1 | the TCP connection is closed instead of an answer |
| `bad_crc` | count=1 | RTU: the answer has a wrong CRC |
| `wrong_transaction` | count=1 | TCP: the answer has another transaction ID |
| `wrong_unit` | count=1 | the answer comes from another unit ID |
| `truncate` | bytes=1, count=1 | the answer is cut short |
| `freeze` | | values stop changing |
| `drift` | point, per_second | a numeric point runs away |
| `trip` | code=1 | `vfd`: fault trip, coasts to stop, latched until reset |
| `sensor_break` | | `temperature-controller`: PV reads 3276.7, output off, alarm |
| `phase_loss` | phase | `energy-meter`, `eastron-sdm630`: that phase reads 0 V, 0 A |
| `grid_loss` | | `sunspec-inverter`: state 7 (fault), event bit 4, AC values 0 |
| `ground_fault` | | `sunspec-inverter`: state 7 (fault), event bit 0 |
| `over_temp` | | `sunspec-inverter`: state 5 (throttled) at half power, event bit 7 |

## Control channel

`modbus-sim serve` also listens on a control port (default 5021, loopback
only, no authentication). It takes one JSON object per line:

```json
{"device": "pump", "inject": "trip", "params": {"code": 7}}
{"device": "pump", "clear": true}
{"device": 2, "set": {"pv": 99.5}}
{"device": "oven", "get": ["pv", "output"]}
{"list": true}
```

`set` overrides a point's value (a model or source point keeps it until you
set `null`); the CLI has the same as `modbus-sim set`, `get` and `list`.
`--no-control` turns the channel off.

## Python client

A minimal master is included, mainly for trying things out and for tests:

```python
from modbus_sim.client import ModbusClient

with ModbusClient("127.0.0.1", 5020) as c:
    c.write_register(1, 2500, unit=3)                # 25.00 Hz
    c.write_register(0, 1, unit=3)                   # run
    status, frequency = c.read_holding_registers(100, 2, unit=3)

with ModbusClient.serial("/dev/pts/7") as c:         # RTU on a serial port or pty
    print(c.read_device_identification(unit=1))
```

Errors raise `ModbusError` (with `.code`), `TimeoutError` or `ProtocolError`.
For production code use pymodbus or similar; the test suite cross-checks the
simulator against pymodbus.

## pytest plugin

Installing modbus-sim registers two fixtures:

```python
def test_my_poller(modbus_bench):                  # one vfd at unit 1, free port
    modbus_bench.inject(1, "busy")
    result = my_poller("127.0.0.1", modbus_bench.port)
    assert result == "device busy"

def test_plant(modbus_bench_factory):
    bench = modbus_bench_factory("examples/plant.yaml")      # or a dict or a profile
    rtu = modbus_bench_factory("energy-meter", framing="rtu")  # RTU over TCP
    with bench.client() as c:
        ...
```

`--modbus-bench FILE` or the ini key `modbus_bench` sets what `modbus_bench`
starts. See [examples/test_example.py](examples/test_example.py).

## Limitations

- For development and testing; not a certified or conformance-tested Modbus
  implementation.
- `energy-meter`, `temperature-controller` and `vfd` are invented devices with
  plausible register maps. The profiles after real devices follow the
  published register maps but not every behaviour of the device (see
  [Profiles after real devices](#profiles-after-real-devices)). Models are
  plausible, not physically accurate.
- No Modbus ASCII, no Modbus/TCP Security (TLS), no function codes 11, 12, 17,
  20, 21, 24; diagnostics only sub-function 00.
- RTU framing finds the end of a request from its function code, and the
  3.5-character silence only for unknown function codes; inter-character
  timeouts (t1.5) are not enforced.
- Over a pty, baud rate and parity are not simulated.

## Licence

MIT, see [LICENSE](LICENSE).
