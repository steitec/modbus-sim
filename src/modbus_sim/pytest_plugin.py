"""pytest plugin: a fresh simulated Modbus bench for every test.

Fixtures:

- ``modbus_bench`` -- the bench from ``--modbus-bench FILE`` or the ini key
  ``modbus_bench`` (relative to the rootdir); without either, one ``vfd`` at unit 1.
  Runs Modbus TCP on a free port; ``modbus_bench.client()`` returns a connected
  :class:`~modbus_sim.client.ModbusClient`, ``modbus_bench.port`` is the port.
- ``modbus_bench_factory`` -- ``factory(source, **start_options)`` for more benches:
  a bench file, a bench dict, or a bundled profile name (one device at unit 1).
  Start options are those of :meth:`Bench.start`, e.g. ``framing="rtu"`` or
  ``pty=True``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .bench import Bench, BenchError
from .profile import ProfileError, bundled_profiles

DEFAULT_PROFILE = "vfd"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("modbus-sim")
    group.addoption("--modbus-bench", metavar="FILE",
                    help="bench file (YAML) for the modbus_bench fixture")
    parser.addini("modbus_bench", "bench file (YAML) for the modbus_bench fixture, "
                  "relative to the rootdir")


class BenchPool:
    def __init__(self, root: Path | None):
        self.root = root
        self.benches: list[Bench] = []

    def start(self, source: str | Path | dict, **options: Any) -> Bench:
        message = None
        try:
            if isinstance(source, dict):
                bench = Bench.from_dict(source)
            elif isinstance(source, str) and source in bundled_profiles():
                bench = Bench.single(source)
            else:
                path = Path(source)
                if self.root is not None and not path.is_absolute():
                    path = self.root / path
                bench = Bench.from_yaml(path)
            options.setdefault("port", 0)
            options.setdefault("control_port", 0)
            bench.start(**options)
        except (BenchError, ProfileError, OSError) as error:
            message = f"modbus-sim bench: {error}"
        if message is not None:
            pytest.fail(message, pytrace=False)
        self.benches.append(bench)
        return bench

    def stop_all(self) -> None:
        failure = None
        for bench in self.benches:
            try:
                bench.stop()
            except Exception as error:  # keep stopping the others
                failure = failure or error
        self.benches.clear()
        if failure is not None:
            raise failure


def _configured(config: pytest.Config) -> Path | str:
    option = config.getoption("--modbus-bench")
    if option:
        return config.invocation_params.dir / option
    ini = config.getini("modbus_bench")
    if ini:
        return config.rootpath / ini
    return DEFAULT_PROFILE


@pytest.fixture
def modbus_bench_factory(request: pytest.FixtureRequest):
    pool = BenchPool(request.config.rootpath)
    yield pool.start
    pool.stop_all()


@pytest.fixture
def modbus_bench(request: pytest.FixtureRequest):
    pool = BenchPool(None)
    yield pool.start(_configured(request.config))
    pool.stop_all()
