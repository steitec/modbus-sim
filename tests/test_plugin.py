pytest_plugins = ["pytester"]


def test_fixtures(pytester):
    pytester.makepyfile("""
        def test_default(modbus_bench):
            with modbus_bench.client() as c:
                assert c.read_holding_registers(100, 1) == [1]

        def test_factory(modbus_bench_factory):
            bench = modbus_bench_factory("temperature-controller", framing="rtu")
            with bench.client() as c:
                assert c.read_holding_registers(0, 1) == [600]
            bench.inject(1, "busy")
            with bench.client() as c:
                try:
                    c.read_holding_registers(0, 1)
                except Exception as exc:
                    assert exc.code == 6

        def test_bad(modbus_bench_factory):
            modbus_bench_factory({"devices": []})
    """)
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=2, failed=1)
    result.stdout.fnmatch_lines(["*modbus-sim bench: a bench is a mapping*"])


def test_bench_option(pytester):
    pytester.makefile(".yaml", plant="devices:\n  - {profile: energy-meter, unit: 7}\n")
    pytester.makepyfile("""
        def test_unit(modbus_bench):
            assert [d.unit for d in modbus_bench.devices.values()] == [7]
    """)
    pytester.runpytest("--modbus-bench", "plant.yaml").assert_outcomes(passed=1)
