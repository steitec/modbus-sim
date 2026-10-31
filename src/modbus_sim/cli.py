"""Command line: serve devices, talk to them, inject faults."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from typing import Any

from . import __version__
from . import datatypes as dt
from .bench import Bench, BenchError
from .client import ModbusClient, ModbusError, ProtocolError
from .control import CONTROL_PORT, send_control
from .profile import ProfileError, bundled_profiles, load_profile
from .server import TCP_PORT

_TABLE_REF = {"coil": 0, "discrete": 100000, "input": 300000, "holding": 400000}
_TABLE_ALIASES = {"coil": "coil", "coils": "coil", "co": "coil",
                  "discrete": "discrete", "di": "discrete", "discrete-inputs": "discrete",
                  "holding": "holding", "hr": "holding", "holding-registers": "holding",
                  "input": "input", "ir": "input", "input-registers": "input"}


class CliError(Exception):
    """A usage or input error; printed without a traceback, exit code 2."""


def _int(text: str) -> int:
    return int(text, 0)


def _table(text: str) -> str:
    try:
        return _TABLE_ALIASES[text.lower()]
    except KeyError:
        raise argparse.ArgumentTypeError(
            "table is coil, discrete, holding or input (or co, di, hr, ir)") from None


def _value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _pairs(items: list[str]) -> dict[str, Any]:
    result = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise CliError(f"modbus-sim: expected name=value, got {item!r}")
        result[key] = _value(value)
    return result


def _add_connection(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=TCP_PORT)
    p.add_argument("--rtu-over-tcp", action="store_true", help="RTU framing over TCP")
    p.add_argument("--serial", metavar="DEVICE", help="RTU on a serial port or pty instead")
    p.add_argument("--baud", type=int, default=19200)
    p.add_argument("--parity", choices=["N", "E", "O"], default="E")
    p.add_argument("--unit", "-u", type=int, default=1, help="unit ID (default 1)")
    p.add_argument("--timeout", type=float, default=2.0)


def _add_value_format(p: argparse.ArgumentParser) -> None:
    p.add_argument("--type", "-t", default="uint16", choices=dt.REGISTER_TYPES,
                   help="value type for registers (default uint16)")
    p.add_argument("--order", "-o", default="ABCD", choices=dt.WORD_ORDERS,
                   help="word order of 32/64-bit values (default ABCD)")
    p.add_argument("--scale", type=float, default=1.0, help="engineering = raw * scale")


def _add_control(p: argparse.ArgumentParser) -> None:
    p.add_argument("--control-host", default="127.0.0.1")
    p.add_argument("--control-port", type=int, default=CONTROL_PORT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modbus-sim", description="Simulated Modbus devices with behaviour and faults.")
    parser.add_argument("--version", action="version", version=f"modbus-sim {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run simulated devices")
    p.add_argument("profile", nargs="?", help="bundled profile or profile file (one device)")
    p.add_argument("--bench", metavar="FILE", help="bench file with several devices")
    p.add_argument("--unit", "-u", type=int, default=1, help="unit ID for a single profile")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (0.0.0.0 for the network)")
    p.add_argument("--port", type=int, default=TCP_PORT, help=f"TCP port (default {TCP_PORT})")
    p.add_argument("--rtu-over-tcp", action="store_true", help="RTU framing over TCP")
    p.add_argument("--pty", action="store_true", help="RTU on a new virtual serial port")
    p.add_argument("--serial", metavar="DEVICE", help="RTU on a serial port (needs pyserial)")
    p.add_argument("--baud", type=int, default=19200)
    p.add_argument("--parity", choices=["N", "E", "O"], default="E")
    p.add_argument("--stopbits", type=int, choices=[1, 2], default=1)
    p.add_argument("--control-port", type=int, default=CONTROL_PORT)
    p.add_argument("--control-host", default="127.0.0.1",
                   help="the control channel has no authentication; keep it on loopback")
    p.add_argument("--no-control", action="store_true", help="no control channel")

    sub.add_parser("profiles", help="list the bundled profiles")

    p = sub.add_parser("map", help="print the register map of a profile")
    p.add_argument("profile")
    p.add_argument("--markdown", action="store_true", help="as a Markdown table")

    p = sub.add_parser("read", help="read from a device")
    p.add_argument("table", type=_table, help="coil, discrete, holding or input")
    p.add_argument("address", type=_int, help="0-based protocol address (0x.. ok)")
    p.add_argument("--count", "-c", type=int, default=1, help="number of values")
    _add_connection(p)
    _add_value_format(p)

    p = sub.add_parser("write", help="write to a device (coil or holding)")
    p.add_argument("table", type=_table)
    p.add_argument("address", type=_int)
    p.add_argument("values", nargs="+", help="values (numbers; 0/1 for coils)")
    _add_connection(p)
    _add_value_format(p)

    p = sub.add_parser("ident", help="read the device identification (function 43/14)")
    _add_connection(p)

    p = sub.add_parser("inject", help="inject a fault into a running bench")
    p.add_argument("device", help="device name or unit ID")
    p.add_argument("fault")
    p.add_argument("params", nargs="*", help="name=value, e.g. code=4 count=3")
    _add_control(p)

    p = sub.add_parser("clear", help="clear faults (all, or one kind)")
    p.add_argument("device")
    p.add_argument("fault", nargs="?")
    _add_control(p)

    p = sub.add_parser("get", help="show point values of a running device")
    p.add_argument("device")
    p.add_argument("points", nargs="*", help="point names (default: all)")
    _add_control(p)

    p = sub.add_parser("set", help="override point values (name=value, null = release)")
    p.add_argument("device")
    p.add_argument("values", nargs="+")
    _add_control(p)

    p = sub.add_parser("list", help="devices and active faults of a running bench")
    _add_control(p)
    return parser


# --- commands -----------------------------------------------------------------------

def _serve(args: argparse.Namespace) -> int:
    if bool(args.profile) == bool(args.bench):
        raise CliError("modbus-sim serve: give a profile or --bench FILE")
    if sum(bool(x) for x in (args.pty, args.serial, args.rtu_over_tcp)) > 1:
        raise CliError("modbus-sim serve: choose one of --pty, --serial, --rtu-over-tcp")
    bench = Bench.from_yaml(args.bench) if args.bench else Bench.single(args.profile, args.unit)
    bench.start(host=args.host, port=args.port,
                framing="rtu" if args.rtu_over_tcp else "tcp", serial=args.serial,
                pty=args.pty, baudrate=args.baud, parity=args.parity, stopbits=args.stopbits,
                control_port=None if args.no_control else args.control_port,
                control_host=args.control_host)
    print(f"modbus-sim: {bench.describe()}", flush=True)
    for name, device in bench.devices.items():
        print(f"  unit {device.unit:3}  {name}  ({device.profile.name})", flush=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):  # not in the main thread / not on this platform
            pass
    try:
        while not stop.wait(0.5):
            pass
    finally:
        bench.stop()
    return 0


def _profiles(_args) -> int:
    for name in bundled_profiles():
        print(f"{name:24} {load_profile(name).description}")
    return 0


def _map(args) -> int:
    profile = load_profile(args.profile)
    rows = []
    for s in sorted(profile.points, key=lambda s: (list(_TABLE_REF).index(s.table), s.address)):
        kind = s.type + (f"[{s.length}]" if s.length else "")
        if s.count > 1 and s.type != "string":
            kind += f" {s.word_order}"
        limits = ""
        if s.min is not None or s.max is not None:
            limits = f"{'' if s.min is None else f'{s.min:g}'}..{'' if s.max is None else f'{s.max:g}'}"
        note = s.description
        if s.bits:
            note = ", ".join(f"{bit}: {text}" for bit, text in sorted(s.bits.items()))
        rows.append([s.table, str(s.address), f"{_TABLE_REF[s.table] + s.address + 1:06d}",
                     s.name, kind, "" if s.scale == 1 else f"{s.scale:g}", s.unit, s.access,
                     limits, note])
    header = ["table", "address", "ref", "point", "type", "scale", "unit", "access",
              "limits", "notes"]
    print(f"{profile.name}: {profile.description}")
    print(f"identification: {profile.identification.get('vendor')} "
          f"{profile.identification.get('product')} {profile.identification.get('revision')}"
          f"; unmapped addresses: {'exception 02' if profile.gaps == 'error' else 'read 0'}")
    print()
    if args.markdown:
        print("| " + " | ".join(header) + " |")
        print("|" + "---|" * len(header))
        for row in rows:
            print("| " + " | ".join(c.replace("|", "\\|") for c in row) + " |")
    else:
        widths = [max(len(r[i]) for r in rows + [header]) for i in range(len(header))]
        for row in [header] + rows:
            print("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())
    return 0


def _client(args) -> ModbusClient:
    if args.serial:
        return ModbusClient.serial(args.serial, args.baud, args.parity, timeout=args.timeout)
    return ModbusClient(args.host, args.port, timeout=args.timeout,
                        framing="rtu" if args.rtu_over_tcp else "tcp")


def _read(args) -> int:
    with _client(args) as c:
        if args.table in ("coil", "discrete"):
            read = c.read_coils if args.table == "coil" else c.read_discrete_inputs
            for i, bit in enumerate(read(args.address, args.count, args.unit)):
                print(f"{args.table} {args.address + i}: {int(bit)}")
            return 0
        size = dt.registers(args.type, 1 if args.type == "string" else None)
        total = args.count if args.type == "string" else args.count * size
        read = c.read_holding_registers if args.table == "holding" else c.read_input_registers
        words = read(args.address, total, args.unit)
    if args.type == "string":
        print(f"{args.table} {args.address}: {dt.decode(words, 'string')!r}")
        return 0
    for i in range(args.count):
        part = words[i * size:(i + 1) * size]
        value = dt.decode(part, args.type, args.order, args.scale)
        raw = " ".join(f"{w:04x}" for w in part)
        shown = f"{value:g}" if isinstance(value, float) else str(value)
        print(f"{args.table} {args.address + i * size}: {shown}  [{raw}]")
    return 0


def _write(args) -> int:
    if args.table not in ("coil", "holding"):
        raise CliError("modbus-sim write: only coil and holding can be written")
    with _client(args) as c:
        if args.table == "coil":
            bits = []
            for v in args.values:
                if v not in ("0", "1", "true", "false", "on", "off"):
                    raise CliError(f"modbus-sim write: coil value {v!r} is not 0/1")
                bits.append(v in ("1", "true", "on"))
            if len(bits) == 1:
                c.write_coil(args.address, bits[0], args.unit)
            else:
                c.write_coils(args.address, bits, args.unit)
            return 0
        words: list[int] = []
        for v in args.values:
            if args.type == "string":
                words += dt.encode(v, "string", length=(len(v.encode("latin-1", "replace"))
                                                        + 1) // 2)
                continue
            try:
                number = float(int(v, 0)) if v.lower().startswith(("0x", "-0x")) else float(v)
            except ValueError:
                raise CliError(f"modbus-sim write: {v!r} is not a number") from None
            if dt.is_integer(args.type) and not dt.fits(number, args.type, args.scale):
                raise CliError(f"modbus-sim write: {v} does not fit {args.type}")
            words += dt.encode(number, args.type, args.order, args.scale)
        if len(words) == 1:
            c.write_register(args.address, words[0], args.unit)
        else:
            c.write_registers(args.address, words, args.unit)
    return 0


_ID_NAMES = ["vendor", "product code", "revision", "vendor URL", "product name", "model name",
             "user application name"]


def _ident(args) -> int:
    with _client(args) as c:
        for oid, text in sorted(c.read_device_identification(args.unit).items()):
            name = _ID_NAMES[oid] if oid < len(_ID_NAMES) else f"object {oid:#04x}"
            print(f"{name}: {text}")
    return 0


def _control(args, request: dict) -> dict:
    answer = send_control(request, args.control_host, args.control_port)
    if not answer.get("ok"):
        raise CliError(f"modbus-sim: {answer.get('error')}")
    return answer


def _device_ref(text: str) -> Any:
    return int(text) if text.isdigit() else text


def _inject(args) -> int:
    _control(args, {"device": _device_ref(args.device), "inject": args.fault,
                    "params": _pairs(args.params)})
    return 0


def _clear(args) -> int:
    _control(args, {"device": _device_ref(args.device), "clear": args.fault or True})
    return 0


def _get(args) -> int:
    answer = _control(args, {"device": _device_ref(args.device), "get": args.points or True})
    for name, value in answer["values"].items():
        print(f"{name}: {value:g}" if isinstance(value, float) else f"{name}: {value}")
    return 0


def _set(args) -> int:
    _control(args, {"device": _device_ref(args.device), "set": _pairs(args.values)})
    return 0


def _list(args) -> int:
    answer = _control(args, {"list": True})
    for name, info in answer["devices"].items():
        faults = ", ".join(f"{k}{'' if not v else ' ' + json.dumps(v)}"
                           for k, v in info["faults"].items()) or "no faults"
        print(f"unit {info['unit']:3}  {name}  ({info['profile']})  "
              f"{info['requests']} requests  {faults}")
    return 0


_COMMANDS = {"serve": _serve, "profiles": _profiles, "map": _map, "read": _read,
             "write": _write, "ident": _ident, "inject": _inject, "clear": _clear,
             "get": _get, "set": _set, "list": _list}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except CliError as exc:
        message = str(exc)
        print(message if message.startswith("modbus-sim") else f"modbus-sim: {message}",
              file=sys.stderr)
        return 2
    except (BenchError, ProfileError) as exc:
        print(f"modbus-sim: {exc}", file=sys.stderr)
        return 2
    except ModbusError as exc:
        print(f"modbus-sim: {exc}", file=sys.stderr)
        return 1
    except (ProtocolError, TimeoutError) as exc:
        print(f"modbus-sim: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"modbus-sim: {exc}", file=sys.stderr)
        return 1
