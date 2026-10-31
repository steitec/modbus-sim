"""Device profiles: identification, defaults and the point map, loaded from YAML.

A profile names a set of *points*. Each point lives in one of the four Modbus
tables at a 0-based protocol address and has a data type::

    name: temperature-controller
    identification: {vendor: steitec, product: SIM-TC1, revision: "1.0"}
    word_order: ABCD          # default for 32/64-bit points
    gaps: error               # reading unmapped addresses: exception 02 (or "zero")
    model: temperature_controller
    model_params: {ambient: 22.0}
    points:
      - {name: pv, table: input, address: 0, type: int16, scale: 0.1, unit: degC,
         model: pv}
      - {name: setpoint, table: holding, address: 0, type: int16, scale: 0.1,
         min: 0, max: 400, model: setpoint}
      - {name: label, table: holding, address: 100, type: string, length: 8,
         value: "OVEN 1"}

A point gets its value from (first match): the model variable named in ``model``,
the signal ``source`` (a mapping or a list of mappings that are added up), or the
stored ``value`` (default 0). Writable points without a model keep what is written.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from . import datatypes as dt

TABLES = ("coil", "discrete", "holding", "input")
BIT_TABLES = ("coil", "discrete")
WRITABLE_TABLES = ("coil", "holding")
SOURCE_PARAMS = {
    "constant": {"value"},
    "ramp": {"start", "stop", "period"},
    "sine": {"offset", "amplitude", "period"},
    "noise": {"sigma"},
}
_POINT_KEYS = {"name", "table", "address", "type", "length", "scale", "unit", "access",
               "min", "max", "value", "source", "model", "word_order", "description", "bits"}
_PROFILE_KEYS = {"name", "description", "identification", "word_order", "gaps", "model",
                 "model_params", "points"}
_ID_KEYS = ("vendor", "product", "revision", "vendor_url", "product_name", "model_name",
            "user_application_name")
OVERRIDE_KEYS = {"word_order", "gaps", "model_params", "values", "identification"}


class ProfileError(ValueError):
    """Invalid profile; the message names the offending point or key."""


@dataclass(frozen=True)
class Source:
    type: str
    params: dict[str, float]


@dataclass
class PointSpec:
    name: str
    table: str
    address: int
    type: str
    word_order: str = "ABCD"
    length: int | None = None
    scale: float = 1.0
    unit: str = ""
    access: str = "ro"
    min: float | None = None
    max: float | None = None
    value: Any = 0
    sources: list[Source] = field(default_factory=list)
    model: str | None = None
    description: str = ""
    bits: dict[int, str] = field(default_factory=dict)

    @property
    def count(self) -> int:
        """Addresses this point occupies (registers, or 1 for a bit)."""
        if self.table in BIT_TABLES:
            return 1
        return dt.registers(self.type, self.length)

    @property
    def writable(self) -> bool:
        return self.access == "rw"


@dataclass
class Profile:
    name: str
    description: str
    identification: dict[str, str]
    word_order: str
    gaps: str
    model: str | None
    model_params: dict[str, Any]
    points: list[PointSpec]

    def point(self, name: str) -> PointSpec:
        for spec in self.points:
            if spec.name == name:
                return spec
        raise KeyError(name)


def bundled_profiles() -> list[str]:
    folder = resources.files("modbus_sim") / "profiles"
    return sorted(p.name[:-5] for p in folder.iterdir() if p.name.endswith(".yaml"))


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise ProfileError(f"{what} must be a finite number, got {value!r}")
    return float(value)


def _int(value: Any, what: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ProfileError(f"{what} must be an integer {low}-{high}, got {value!r}")
    return value


def _order(value: Any, what: str) -> str:
    if value not in dt.WORD_ORDERS:
        raise ProfileError(f"{what} must be one of {', '.join(dt.WORD_ORDERS)}, got {value!r}")
    return value


def _sources(raw: Any, where: str) -> list[Source]:
    items = raw if isinstance(raw, list) else [raw]
    result = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in SOURCE_PARAMS:
            raise ProfileError(f"{where}: source needs a type "
                               f"({', '.join(SOURCE_PARAMS)})")
        needed = SOURCE_PARAMS[item["type"]]
        params = {k: v for k, v in item.items() if k != "type"}
        missing = needed - set(params)
        extra = set(params) - needed
        if missing or extra:
            raise ProfileError(f"{where}: {item['type']} source takes "
                               f"{', '.join(sorted(needed))}")
        params = {k: _number(v, f"{where}: source {k}") for k, v in params.items()}
        if "period" in params and params["period"] <= 0:
            raise ProfileError(f"{where}: source period must be > 0")
        if "sigma" in params and params["sigma"] < 0:
            raise ProfileError(f"{where}: source sigma must be >= 0")
        result.append(Source(item["type"], params))
    return result


def _point(raw: Any, index: int, default_order: str) -> PointSpec:
    where = f"points[{index}]"
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: expected a mapping")
    unknown = set(raw) - _POINT_KEYS
    if unknown:
        raise ProfileError(f"{where}: unknown key {sorted(unknown)[0]!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ProfileError(f"{where}: missing 'name'")
    where = f"point {name!r}"
    table = raw.get("table")
    if table not in TABLES:
        raise ProfileError(f"{where}: table must be one of {', '.join(TABLES)}")
    address = _int(raw.get("address"), f"{where}: address", 0, 0xFFFF)
    bit = table in BIT_TABLES
    kind = raw.get("type", "bool" if bit else "uint16")
    if bit and kind != "bool":
        raise ProfileError(f"{where}: {table} points are type bool")
    if not bit and kind not in dt.REGISTER_TYPES:
        raise ProfileError(f"{where}: type must be one of {', '.join(dt.REGISTER_TYPES)}")
    length = None
    if kind == "string":
        length = _int(raw.get("length"), f"{where}: length", 1, 125)
    elif "length" in raw:
        raise ProfileError(f"{where}: only strings have a length")
    scale = _number(raw.get("scale", 1.0), f"{where}: scale")
    if scale == 0 or (scale != 1.0 and (bit or kind == "string")):
        raise ProfileError(f"{where}: scale must be non-zero and only on numbers")
    access = raw.get("access", "rw" if table in WRITABLE_TABLES else "ro")
    if access not in ("ro", "rw"):
        raise ProfileError(f"{where}: access must be ro or rw")
    if access == "rw" and table not in WRITABLE_TABLES:
        raise ProfileError(f"{where}: {table} points cannot be written over Modbus")
    spec = PointSpec(
        name=name, table=table, address=address, type=kind, length=length, scale=scale,
        word_order=_order(raw.get("word_order", default_order), f"{where}: word_order"),
        unit=str(raw.get("unit", "")), access=access,
        description=str(raw.get("description", "")))
    if address + spec.count > 0x10000:
        raise ProfileError(f"{where}: does not fit below address 65536")
    for key in ("min", "max"):
        if key in raw:
            setattr(spec, key, _number(raw[key], f"{where}: {key}"))
    if "source" in raw:
        if kind == "string" or bit:
            raise ProfileError(f"{where}: sources only drive numeric registers")
        spec.sources = _sources(raw["source"], where)
    if "model" in raw:
        if not isinstance(raw["model"], str):
            raise ProfileError(f"{where}: model must name a model variable")
        spec.model = raw["model"]
    if "value" in raw:
        spec.value = _initial(raw["value"], spec, where)
    elif kind == "string":
        spec.value = ""
    elif bit:
        spec.value = False
    if "bits" in raw:
        bits = raw["bits"]
        if not isinstance(bits, dict) or kind != "uint16":
            raise ProfileError(f"{where}: bits is a mapping on a uint16 register")
        spec.bits = {_int(k, f"{where}: bit", 0, 15): str(v) for k, v in bits.items()}
    return spec


def _initial(value: Any, spec: PointSpec, where: str) -> Any:
    if spec.type == "string":
        if not isinstance(value, str):
            raise ProfileError(f"{where}: value must be a string")
        return value
    if spec.type == "bool":
        if not isinstance(value, (bool, int)):
            raise ProfileError(f"{where}: value must be true or false")
        return bool(value)
    return _number(value, f"{where}: value")


def _check_overlaps(points: list[PointSpec]) -> None:
    used: dict[tuple[str, int], str] = {}
    names: set[str] = set()
    for spec in points:
        if spec.name in names:
            raise ProfileError(f"point {spec.name!r}: duplicate name")
        names.add(spec.name)
        for address in range(spec.address, spec.address + spec.count):
            key = (spec.table, address)
            if key in used:
                raise ProfileError(f"point {spec.name!r}: {spec.table} address {address} "
                                   f"is already used by {used[key]!r}")
            used[key] = spec.name


def profile_from_dict(data: Any, overrides: dict | None = None) -> Profile:
    if not isinstance(data, dict):
        raise ProfileError("a profile is a mapping")
    data = dict(data)
    overrides = dict(overrides or {})
    unknown = set(overrides) - OVERRIDE_KEYS
    if unknown:
        raise ProfileError(f"unknown override {sorted(unknown)[0]!r} "
                           f"(allowed: {', '.join(sorted(OVERRIDE_KEYS))})")
    values = overrides.pop("values", None) or {}
    if not isinstance(values, dict):
        raise ProfileError("values must map point names to values")
    if "model_params" in overrides:
        merged = dict(data.get("model_params") or {})
        if not isinstance(overrides["model_params"], dict):
            raise ProfileError("model_params must be a mapping")
        merged.update(overrides.pop("model_params"))
        data["model_params"] = merged
    if "identification" in overrides:
        merged = dict(data.get("identification") or {})
        if not isinstance(overrides["identification"], dict):
            raise ProfileError("identification must be a mapping")
        merged.update(overrides.pop("identification"))
        data["identification"] = merged
    data.update(overrides)
    unknown = set(data) - _PROFILE_KEYS
    if unknown:
        raise ProfileError(f"unknown key {sorted(unknown)[0]!r}")
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ProfileError("missing 'name'")
    order = _order(data.get("word_order", "ABCD"), "word_order")
    gaps = data.get("gaps", "error")
    if gaps not in ("error", "zero"):
        raise ProfileError("gaps must be error or zero")
    ident = data.get("identification") or {}
    if not isinstance(ident, dict) or set(ident) - set(_ID_KEYS):
        raise ProfileError(f"identification keys are {', '.join(_ID_KEYS)}")
    ident = {k: str(v) for k, v in ident.items()}
    ident.setdefault("vendor", "steitec")
    ident.setdefault("product", name)
    ident.setdefault("revision", "1.0")
    raw_points = data.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raise ProfileError("'points' must be a non-empty list")
    points = [_point(raw, i, order) for i, raw in enumerate(raw_points, 1)]
    _check_overlaps(points)
    model = data.get("model")
    if model is not None and not isinstance(model, str):
        raise ProfileError("model must be a model name")
    params = data.get("model_params") or {}
    if not isinstance(params, dict):
        raise ProfileError("model_params must be a mapping")
    profile = Profile(name=name, description=str(data.get("description", "")),
                      identification=ident, word_order=order, gaps=gaps, model=model,
                      model_params=dict(params), points=points)
    for point_name, value in values.items():
        try:
            spec = profile.point(point_name)
        except KeyError:
            raise ProfileError(f"values: unknown point {point_name!r}") from None
        if spec.model or spec.sources:
            raise ProfileError(f"values: point {point_name!r} is driven by "
                               f"{'the model' if spec.model else 'a source'}")
        spec.value = _initial(value, spec, f"values: {point_name!r}")
    if any(spec.model for spec in points) and model is None:
        raise ProfileError("points refer to a model but the profile names none")
    return profile


def load_profile(ref: str | Path, overrides: dict | None = None) -> Profile:
    """Load a bundled profile by name, or a profile file by path."""
    ref = str(ref)
    if ref in bundled_profiles():
        text = (resources.files("modbus_sim") / "profiles" / f"{ref}.yaml").read_text("utf-8")
        where = ref
    else:
        path = Path(ref)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ProfileError(f"unknown profile {ref!r} (bundled: "
                               f"{', '.join(bundled_profiles())}; or a .yaml path)") from None
        except (OSError, UnicodeDecodeError) as exc:
            raise ProfileError(f"{ref}: cannot read: {exc}") from None
        where = str(path)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(f"{where}: invalid YAML: {exc}") from None
    try:
        return profile_from_dict(data, overrides)
    except ProfileError as exc:
        raise ProfileError(f"{where}: {exc}") from None
