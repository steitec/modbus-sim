"""Build the browser demo into a directory of static files (default: web/dist).

    python web/build_site.py [--out DIR]

Puts together the page (web/site), the demo layer (web/py/demo.py), the example
bench (examples/real-devices.yaml), a modbus-sim wheel built from this checkout, and the Pyodide runtime. Pyodide
is downloaded once into web/.cache and only used when its SHA-256 matches the
pinned value, so a changed or tampered download stops the build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

WEB = Path(__file__).resolve().parent
ROOT = WEB.parent

PYODIDE_VERSION = "0.27.7"
PYODIDE_CORE = (
    f"https://github.com/pyodide/pyodide/releases/download/{PYODIDE_VERSION}/pyodide-core-{PYODIDE_VERSION}.tar.bz2",
    "9bc8f127db6c590b191b9aee754022cb41b1a36c7bac233776c11c5ecb541be8",
)
# the same checksum as in the core's pyodide-lock.json
PYYAML = (
    f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/pyyaml-6.0.2-cp312-cp312-pyodide_2024_0_wasm32.whl",
    "444d034ede13ca0a2961a1215838fb7d4cf6fa98ba7fe5e2a09e5063180af2bf",
)
# what the page needs from the core (the type declarations are left out)
CORE_FILES = ("pyodide.mjs", "pyodide.asm.js", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch(url: str, sha256: str, cache: Path) -> Path:
    """Return the cached download of ``url``; exit if its checksum is not ``sha256``."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / url.rsplit("/", 1)[1]
    if not path.exists():
        print(f"downloading {url}")
        with urllib.request.urlopen(url) as response, open(path.with_suffix(".part"), "wb") as out:
            shutil.copyfileobj(response, out)
        path.with_suffix(".part").replace(path)
    actual = _sha256(path)
    if actual != sha256:
        path.unlink()
        raise SystemExit(f"checksum mismatch for {url}: expected {sha256}, got {actual}")
    return path


def build_wheel(out: Path) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", tmp, str(ROOT)],
                       check=True, cwd=ROOT, stdout=subprocess.DEVNULL)
        (wheel,) = Path(tmp).glob("modbus_sim-*.whl")
        return Path(shutil.copy2(wheel, out))


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(WEB / "site", out)

    py = out / "py"
    py.mkdir()
    shutil.copy2(WEB / "py" / "demo.py", py)
    shutil.copy2(ROOT / "examples" / "real-devices.yaml", py)
    wheel = build_wheel(py)
    version = wheel.name.split("-")[1]
    manifest = {"wheel": wheel.name, "version": version, "pyodide": PYODIDE_VERSION}
    (py / "manifest.json").write_text(json.dumps(manifest) + "\n")

    # versioned directory: nginx caches it for good, an upgrade gets new URLs
    cache = WEB / ".cache"
    pyodide = out / "pyodide" / PYODIDE_VERSION
    pyodide.mkdir(parents=True)
    with tarfile.open(fetch(*PYODIDE_CORE, cache)) as tar:
        for name in CORE_FILES:
            member = tar.getmember(f"pyodide/{name}")
            with tar.extractfile(member) as source, open(pyodide / name, "wb") as target:
                shutil.copyfileobj(source, target)
    shutil.copy2(fetch(*PYYAML, cache), pyodide)
    print(f"built {out} (modbus-sim {version}, Pyodide {PYODIDE_VERSION})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=WEB / "dist")
    build(parser.parse_args().out.resolve())


if __name__ == "__main__":
    main()
