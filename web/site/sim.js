// Loads Pyodide, the modbus-sim wheel and web/py/demo.py, and wraps the Demo object.
// Everything comes from this site: ./pyodide/<version>/ and ./py/ (see web/build_site.py).

const base = new URL(".", import.meta.url);

async function fetchOk(path, options) {
  const response = await fetch(new URL(path, base), options);
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response;
}

const LOAD_LIMIT_MS = 120_000; // generous: about 10 MB on a slow mobile connection

// Pyodide reports a failed WebAssembly download as an unhandled rejection and its
// loadPyodide() never settles; turn that (or a load that never ends) into an error.
export async function loadSim(onStatus) {
  let onRejection, timer;
  const failure = new Promise((_, reject) => {
    onRejection = (event) => reject(event.reason instanceof Error ? event.reason : new Error(String(event.reason)));
    window.addEventListener("unhandledrejection", onRejection);
    timer = setTimeout(() => reject(new Error("loading took too long")), LOAD_LIMIT_MS);
  });
  try {
    return await Promise.race([load(onStatus), failure]);
  } finally {
    window.removeEventListener("unhandledrejection", onRejection);
    clearTimeout(timer);
  }
}

async function load(onStatus) {
  if (typeof WebAssembly !== "object") throw new Error("this browser has no WebAssembly");

  onStatus("loading the Python runtime…");
  const manifest = await (await fetchOk("py/manifest.json")).json();
  const runtime = `pyodide/${manifest.pyodide}/`;
  // Pyodide only logs a warning when the .wasm answers 404 and then never settles
  await fetchOk(`${runtime}pyodide.asm.wasm`, { method: "HEAD" });
  const { loadPyodide } = await import(`./${runtime}pyodide.mjs`);
  const pyodide = await loadPyodide({ indexURL: new URL(runtime, base).href });

  onStatus("loading modbus-sim…");
  await pyodide.loadPackage("pyyaml", { messageCallback: () => {} });
  await pyodide.loadPackage(new URL(`py/${manifest.wheel}`, base).href, { messageCallback: () => {} });
  for (const name of ["demo.py", "real-devices.yaml"]) {
    const data = new Uint8Array(await (await fetchOk(`py/${name}`)).arrayBuffer());
    pyodide.FS.writeFile(name, data);
  }

  onStatus("powering the devices…");
  const demo = pyodide.runPython('import sys; sys.path.insert(0, "."); from demo import Demo; Demo("real-devices.yaml")');

  // Python dict -> plain JS object; the proxy is freed right away
  const js = (proxy) => {
    const value = proxy.toJs({ dict_converter: Object.fromEntries });
    proxy.destroy();
    return value;
  };
  return {
    version: manifest.version,
    run: async (target, line) => js(demo.run(target, line)),
    inject: async (fault) => js(demo.inject(fault)),
    reset: async () => js(demo.reset()),
    state: () => js(demo.state()),
  };
}
