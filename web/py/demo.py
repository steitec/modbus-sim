"""The Python side of the browser demo: a Modbus TCP master talking to the example
bench (examples/real-devices.yaml), one request at a time.

Runs in Pyodide next to the modbus-sim wheel. The page calls ``run``, ``inject``,
``reset`` and ``state``; every value it shows comes from the simulator. No sockets:
the bench is never started. A command becomes a real Modbus TCP frame (MBAP header +
PDU), goes through the same ``dispatch`` the TCP server uses (unit IDs, transport
faults), and the answer frame is decoded like the ``modbus-sim read/write`` CLI
prints it.

Commands (a subset of the CLI, the unit comes from the selected device)::

    read  TABLE ADDRESS [-c COUNT] [-t TYPE] [-o ORDER] [--scale S]
    write TABLE ADDRESS VALUE... [-t TYPE] [-o ORDER] [--scale S]
    ident
    help
"""

from __future__ import annotations

import math
import shlex

from modbus_sim import datatypes as dt
from modbus_sim import frame
from modbus_sim.bench import Bench
from modbus_sim.server import dispatch

MAX_LINE = 200
MAX_COUNT = 60          # values per read; keeps the terminal readable
TARGETS = {"meter": 1, "controller": 2, "inverter": 3}

# button name -> (device, fault kind, parameters)
FAULTS: dict[str, tuple[str, str, dict]] = {
    "meter-no-response": ("meter", "no_response", {"count": 1}),
    "meter-phase-loss": ("meter", "phase_loss", {"phase": 2}),
    "controller-sensor-break": ("controller", "sensor_break", {}),
    "inverter-grid-loss": ("inverter", "grid_loss", {}),
}

TABLES = {"coil": "coil", "coils": "coil", "co": "coil",
          "discrete": "discrete", "di": "discrete",
          "holding": "holding", "hr": "holding",
          "input": "input", "ir": "input"}
READ_FUNCTION = {"coil": 0x01, "discrete": 0x02, "holding": 0x03, "input": 0x04}
EXCEPTIONS = {0x01: "illegal function", 0x02: "illegal data address",
              0x03: "illegal data value", 0x04: "server device failure",
              0x06: "server device busy", 0x0B: "gateway target failed to respond"}
INVERTER_STATES = {1: "off", 2: "sleeping", 3: "starting", 4: "MPPT", 5: "throttled",
                   6: "shutting down", 7: "fault", 8: "standby"}
HELP = """\
read  TABLE ADDRESS [-c COUNT] [-t TYPE] [-o ORDER] [--scale S]
write TABLE ADDRESS VALUE... [-t TYPE] [-o ORDER] [--scale S]
ident                        device identification (function 43/14)
  TABLE   coil, discrete, holding or input (co, di, hr, ir)
  ADDRESS 0-based protocol address, 0x.. ok (register 30001 = input 0)
  TYPE    uint16 int16 uint32 int32 float32 uint64 int64 float64 string
  ORDER   ABCD CDAB BADC DCBA (word order of 32/64-bit values)"""


class CommandError(ValueError):
    """A command the master cannot send; printed as a usage message."""


def _clean(line: str) -> str:
    return "".join(ch for ch in line[:MAX_LINE] if ch == "\t" or ch.isprintable())


def _hex(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data)


def _number(text: str) -> float:
    try:
        if text.lower().lstrip("-").startswith("0x"):
            return float(int(text, 0))
        value = float(text)
    except ValueError:
        raise CommandError(f"{text!r} is not a number") from None
    if not math.isfinite(value):
        raise CommandError(f"{text!r} is not a finite number")
    return value


def parse(line: str) -> dict:
    """Split a command line into its parts; raises CommandError with a short reason."""
    try:
        words = shlex.split(line)
    except ValueError as exc:
        raise CommandError(str(exc)) from None
    if not words:
        raise CommandError("empty command")
    verb, args = words[0].lower(), words[1:]
    if verb in ("help", "ident"):
        if args:
            raise CommandError(f"{verb} takes no arguments")
        return {"verb": verb}
    if verb not in ("read", "write"):
        raise CommandError(f"unknown command {verb!r} (try: read, write, ident, help)")
    options = {"count": 1, "type": "uint16", "order": "ABCD", "scale": 1.0}
    positional: list[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        flag = {"-c": "count", "--count": "count", "-t": "type", "--type": "type",
                "-o": "order", "--order": "order", "--scale": "scale"}.get(word)
        if flag is None:
            positional.append(word)
            i += 1
            continue
        if i + 1 >= len(args):
            raise CommandError(f"{word} needs a value")
        options[flag] = args[i + 1]
        i += 2
    if len(positional) < 2:
        raise CommandError(f"usage: {verb} TABLE ADDRESS{' VALUE...' if verb == 'write' else ''}")
    table = TABLES.get(positional[0].lower())
    if table is None:
        raise CommandError("table is coil, discrete, holding or input (or co, di, hr, ir)")
    try:
        address = int(positional[1], 0)
    except ValueError:
        raise CommandError(f"address {positional[1]!r} is not a number") from None
    if not 0 <= address <= 0xFFFF:
        raise CommandError("address must be 0-65535")
    if options["type"] not in dt.REGISTER_TYPES:
        raise CommandError(f"type must be one of {', '.join(dt.REGISTER_TYPES)}")
    if options["order"].upper() not in dt.WORD_ORDERS:
        raise CommandError(f"order must be one of {', '.join(dt.WORD_ORDERS)}")
    options["order"] = options["order"].upper()
    try:
        options["count"] = int(options["count"], 0) if isinstance(options["count"], str) \
            else options["count"]
    except ValueError:
        raise CommandError("count must be a number") from None
    if not 1 <= options["count"] <= MAX_COUNT:
        raise CommandError(f"count must be 1-{MAX_COUNT} in this demo")
    options["scale"] = _number(str(options["scale"]))
    if options["scale"] == 0:
        raise CommandError("scale must not be 0")
    command = {"verb": verb, "table": table, "address": address, **options}
    if verb == "read":
        if len(positional) > 2:
            raise CommandError("read takes TABLE ADDRESS and options")
        return command
    if table not in ("coil", "holding"):
        raise CommandError("only coil and holding can be written")
    if len(positional) < 3:
        raise CommandError("write needs at least one value")
    command["values"] = positional[2:]
    return command


class Demo:
    def __init__(self, bench_path: str = "real-devices.yaml"):
        self.bench_path = bench_path
        self._load()

    def _load(self) -> None:
        self.bench = Bench.from_yaml(self.bench_path)
        self.devices = {d.unit: d for d in self.bench.devices.values()}
        self.transaction = 0

    # --- page API ---

    def run(self, target: str, line: str) -> dict:
        """Run one command against a device; returns the lines to print and the state."""
        if target not in TARGETS:
            raise ValueError(f"unknown device {target!r} (allowed: {', '.join(TARGETS)})")
        unit = TARGETS[target]
        line = _clean(line).strip()
        try:
            command = parse(line)
            if command["verb"] == "help":
                lines = [{"kind": "quiet", "text": t} for t in HELP.splitlines()]
            elif command["verb"] == "ident":
                lines = self._ident(unit)
            elif command["verb"] == "read":
                lines = self._read(unit, command)
            else:
                lines = self._write(unit, command)
        except CommandError as exc:
            lines = [{"kind": "error", "text": str(exc)}]
        return {"lines": lines, "state": self.state()}

    def inject(self, fault: str) -> dict:
        if fault not in FAULTS:
            raise ValueError(f"unknown fault {fault!r} (allowed: {', '.join(FAULTS)})")
        name, kind, params = FAULTS[fault]
        self.bench.devices[name].faults.inject(kind, **params)
        return self.state()

    def reset(self) -> dict:
        """Back to power-on: fresh devices, no faults, communications writing off."""
        self._load()
        return self.state()

    # --- Modbus ---

    def _transfer(self, unit: int, pdu: bytes) -> tuple[list[dict], bytes | None]:
        """Send one request frame; returns the frame lines and the answer PDU (None:
        timeout)."""
        self.transaction = (self.transaction + 1) & 0xFFFF
        request = frame.mbap(self.transaction, unit, pdu)
        lines = [{"kind": "tx", "text": f"tx  {_hex(request)}"}]
        outcome = dispatch(self.devices, unit, pdu, "tcp")
        if outcome.pdu is None:
            lines.append({"kind": "timeout",
                          "text": "timeout - no response (the master gives up after 2 s)"})
            return lines, None
        answer = frame.mbap(self.transaction, outcome.unit, outcome.pdu)
        lines.append({"kind": "rx", "text": f"rx  {_hex(answer)}"})
        if outcome.pdu[0] & 0x80:
            code = outcome.pdu[1]
            lines.append({"kind": "error", "text": f"exception {code:#04x} "
                                                   f"({EXCEPTIONS.get(code, 'unknown')})"})
            return lines, None
        return lines, outcome.pdu

    def _read(self, unit: int, c: dict) -> list[dict]:
        table, address = c["table"], c["address"]
        if table in ("coil", "discrete"):
            count = c["count"]
            pdu = bytes([READ_FUNCTION[table]]) + address.to_bytes(2, "big") \
                + count.to_bytes(2, "big")
            lines, answer = self._transfer(unit, pdu)
            if answer is not None:
                data = answer[2:]
                for i in range(count):
                    bit = data[i // 8] >> (i % 8) & 1
                    lines.append({"kind": "value", "text": f"{table} {address + i}: {bit}"})
            return lines
        kind = c["type"]
        size = dt.registers(kind, 1 if kind == "string" else None)
        total = c["count"] if kind == "string" else c["count"] * size
        if address + total > 0x10000 or total > 125:
            raise CommandError("that read is too long")
        pdu = bytes([READ_FUNCTION[table]]) + address.to_bytes(2, "big") \
            + total.to_bytes(2, "big")
        lines, answer = self._transfer(unit, pdu)
        if answer is None:
            return lines
        words = [int.from_bytes(answer[2 + 2 * i:4 + 2 * i], "big") for i in range(total)]
        if kind == "string":
            text = dt.decode(words, "string")
            lines.append({"kind": "value", "text": f"{table} {address}: {text!r}"})
            return lines
        for i in range(c["count"]):
            part = words[i * size:(i + 1) * size]
            value = dt.decode(part, kind, c["order"], c["scale"])
            shown = f"{value:g}" if isinstance(value, float) else str(value)
            raw = " ".join(f"{w:04x}" for w in part)
            lines.append({"kind": "value",
                          "text": f"{table} {address + i * size}: {shown}  [{raw}]"})
        return lines

    def _write(self, unit: int, c: dict) -> list[dict]:
        address = c["address"]
        if c["table"] == "coil":
            bits = []
            for v in c["values"]:
                if v.lower() not in ("0", "1", "true", "false", "on", "off"):
                    raise CommandError(f"coil value {v!r} is not 0/1")
                bits.append(v.lower() in ("1", "true", "on"))
            if len(bits) == 1:
                pdu = bytes([0x05]) + address.to_bytes(2, "big") \
                    + (b"\xff\x00" if bits[0] else b"\x00\x00")
            else:
                packed = bytearray((len(bits) + 7) // 8)
                for i, bit in enumerate(bits):
                    packed[i // 8] |= bit << (i % 8)
                pdu = bytes([0x0F]) + address.to_bytes(2, "big") \
                    + len(bits).to_bytes(2, "big") + bytes([len(packed)]) + bytes(packed)
        else:
            words: list[int] = []
            for v in c["values"]:
                if c["type"] == "string":
                    words += dt.encode(v, "string", length=(len(v.encode("latin-1", "replace"))
                                                            + 1) // 2)
                    continue
                number = _number(v)
                if dt.is_integer(c["type"]) and not dt.fits(number, c["type"], c["scale"]):
                    raise CommandError(f"{v} does not fit {c['type']}")
                words += dt.encode(number, c["type"], c["order"], c["scale"])
            if len(words) > 123:
                raise CommandError("that write is too long")
            if len(words) == 1:
                pdu = bytes([0x06]) + address.to_bytes(2, "big") + words[0].to_bytes(2, "big")
            else:
                pdu = bytes([0x10]) + address.to_bytes(2, "big") \
                    + len(words).to_bytes(2, "big") + bytes([2 * len(words)]) \
                    + b"".join(w.to_bytes(2, "big") for w in words)
        lines, answer = self._transfer(unit, pdu)
        if answer is not None:
            lines.append({"kind": "quiet", "text": "(ok)"})
        return lines

    def _ident(self, unit: int) -> list[dict]:
        lines, answer = self._transfer(unit, bytes([0x2B, 0x0E, 0x01, 0x00]))
        if answer is None:
            return lines
        names = ["vendor", "product code", "revision"]
        count, pos = answer[6], 7
        for _ in range(count):
            oid, length = answer[pos], answer[pos + 1]
            text = answer[pos + 2:pos + 2 + length].decode("latin-1")
            lines.append({"kind": "value", "text": f"{names[oid] if oid < 3 else oid}: {text}"})
            pos += 2 + length
        return lines

    # --- state for the device fronts ---

    def _value(self, device: str, point: str):
        return self.bench.devices[device].points[point].value()

    def _faults(self, device: str) -> list[str]:
        return sorted(self.bench.devices[device].faults.snapshot())

    def state(self) -> dict:
        v = self._value
        meter = {f"{q}_l{i}": v("meter", f"{q}_l{i}")
                 for q in ("voltage", "current", "power") for i in (1, 2, 3)}
        meter.update(power_total=v("meter", "power_total"),
                     energy_import=v("meter", "energy_import"),
                     frequency=v("meter", "frequency"),
                     power_factor=v("meter", "power_factor_total"),
                     faults=self._faults("meter"))
        model = self.bench.devices["controller"].model
        pv = v("controller", "pv")
        controller = {"pv": pv if math.isfinite(pv) else None,
                      "setpoint": v("controller", "set_point"),
                      "mv": v("controller", "mv_heating"),
                      "running": model.running, "manual": model.mode == 1,
                      "comms_write": model.comms_write, "faults": self._faults("controller")}
        state = v("inverter", "operating_state")
        inverter = {"state": INVERTER_STATES.get(state, str(state)),
                    "power": v("inverter", "ac_power"),
                    "energy": v("inverter", "energy"),
                    "dc_voltage": v("inverter", "dc_voltage"),
                    "heatsink": v("inverter", "heatsink_temperature"),
                    "hour": self.bench.devices["inverter"].model.get("hour"),
                    "events": v("inverter", "events1"),
                    "faults": self._faults("inverter")}
        return {"meter": meter, "controller": controller, "inverter": inverter}
