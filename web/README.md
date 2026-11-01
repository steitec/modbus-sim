# Browser demo

The interactive demo at <https://modbus-sim.steitec.net>: the example bench from
`examples/real-devices.yaml` (Eastron SDM630 meter, Omron E5CC controller,
SunSpec inverter) running in the browser. The real modbus-sim code runs in
[Pyodide](https://pyodide.org); every command becomes a Modbus TCP frame that
goes through the same dispatch as the TCP server. The site is static, with no
backend and no requests to other hosts.

This directory is not part of the PyPI package.

| Path | What it is |
|---|---|
| `py/demo.py` | the Python layer: parses `read`/`write`/`ident`, sends the frames, injects faults, returns the device state |
| `site/` | the page (HTML, CSS, JavaScript; no framework, no bundler) |
| `build_site.py` | builds `web/dist`: page, `demo.py`, bench, a wheel of this checkout, Pyodide (pinned by SHA-256) |
| `nginx.conf`, `Dockerfile` | the container that serves `web/dist` |
| `tests/` | pytest for `demo.py` and the build, Playwright for the page |

## Build and run

```bash
pip install -e ".[dev]"
python web/build_site.py                 # -> web/dist
python -m http.server -d web/dist 8000   # quick look, without the nginx headers

docker build -f web/Dockerfile -t modbus-sim-web .   # from the repository root
docker run --rm -p 8080:80 modbus-sim-web            # http://localhost:8080
```

## Test

```bash
pytest web/tests                       # Python layer and build
cd web && npm ci && npx playwright install chromium firefox
npx playwright test                    # against http://localhost:8080 (DEMO_URL to change it)
node tests/screenshot.mjs              # refresh site/img/bench.png from the running demo
```
