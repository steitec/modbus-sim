// The demo page in a real browser: Pyodide, the device fronts, the terminal queue,
// faults, load failure, no third-party requests, narrow screens.
import { test, expect } from "@playwright/test";

const id = (page, testId) => page.getByTestId(testId);

async function ready(page) {
  await page.goto("/");
  await expect(id(page, "status")).toContainText("ready", { timeout: 60_000 });
}

// terminal lines without notes (#) and without the raw frames
async function lines(page) {
  return (await id(page, "terminal-output").locator(".line").allInnerTexts())
    .filter((l) => !l.startsWith("#") && !l.startsWith("tx ") && !l.startsWith("rx "));
}

test("tour", async ({ page }) => {
  await ready(page);
  await id(page, "chip-1").click();
  await expect(id(page, "terminal-output")).toContainText(/input 0: 2[23]\d\.\d+ {2}\[436[56] [0-9a-f]{4}\]/);
  await expect(id(page, "terminal-output")).toContainText("tx  00 01 00 00 00 06 01 04 00 00 00 02");
  await id(page, "chip-3").click();
  await expect(id(page, "terminal-output")).toContainText("input 70: 50  [4248 0000]");

  await id(page, "chip-4").click();
  await expect(id(page, "terminal-output")).toContainText("exception 0x04 (server device failure)");
  await expect(id(page, "controller-comms")).not.toHaveClass(/on/);
  await id(page, "chip-5").click();
  await expect(id(page, "controller-comms")).toHaveClass(/on/);
  await expect(id(page, "controller-sp")).toHaveText("150.0 °C");
  await expect(id(page, "controller-mv")).not.toHaveText("0.0 %");

  await id(page, "chip-7").click();
  await expect(id(page, "terminal-output")).toContainText("holding 40000: 'SunS'");
  await expect(id(page, "terminal-output")).toContainText("holding 40070: 103  [0067]");
  await id(page, "chip-8").click();
  await expect(id(page, "terminal-output")).toContainText("holding 40076: -2  [fffe]");
});

test("the devices live on", async ({ page }) => {
  await ready(page);
  const clock = await id(page, "inverter-clock").innerText();
  await expect(id(page, "meter-l1-v")).toContainText(/2[23]\d\.\d V/);
  await expect(id(page, "inverter-state")).toHaveText("MPPT");
  await expect.poll(() => id(page, "inverter-clock").innerText(), { timeout: 10_000 }).not.toBe(clock);
});

test("typing a command", async ({ page }) => {
  await ready(page);
  await id(page, "target-inverter").click();
  await id(page, "terminal-input").fill("ident");
  await id(page, "terminal-input").press("Enter");
  await expect(id(page, "terminal-output")).toContainText("inverter> ident");
  await expect(id(page, "terminal-output")).toContainText("product code: SIM-PV10");
  await id(page, "terminal-input").fill("read flux 0");
  await id(page, "terminal-input").press("Enter");
  await expect(id(page, "terminal-output")).toContainText("table is coil, discrete, holding or input");
  await id(page, "terminal-input").press("ArrowUp");
  await expect(id(page, "terminal-input")).toHaveValue("read flux 0");
});

test("faults and reset", async ({ page }) => {
  await ready(page);
  await id(page, "fault-meter-phase-loss").click();
  await expect(id(page, "meter-l2-v")).toHaveText("0.0 V");
  await expect(id(page, "fault-badge-meter")).toBeVisible();
  await id(page, "fault-controller-sensor-break").click();
  await expect(id(page, "controller-pv")).toHaveText("S.Err");
  await id(page, "fault-inverter-grid-loss").click();
  await expect(id(page, "inverter-state")).toHaveText("fault");
  await expect(id(page, "inverter-power")).toHaveText("0.00 kW");

  await id(page, "fault-meter-no-response").click();
  const started = Date.now();
  await id(page, "chip-1").click();
  await expect(id(page, "terminal-output")).toContainText("timeout - no response", { timeout: 10_000 });
  expect(Date.now() - started).toBeGreaterThanOrEqual(1900);

  await id(page, "reset").click();
  await expect(id(page, "fault-badge-meter")).toBeHidden();
  await expect(id(page, "fault-badge-controller")).toBeHidden();
  await expect(id(page, "fault-badge-inverter")).toBeHidden();
  await expect(id(page, "inverter-state")).toHaveText("MPPT");
  await expect(id(page, "controller-pv")).toContainText("°C");
});

test("queued clicks keep their order", async ({ page }) => {
  await ready(page);
  await id(page, "fault-meter-no-response").click();
  await id(page, "chip-1").click();
  await id(page, "chip-4").click();
  await expect.poll(async () => (await lines(page)).length, { timeout: 10_000 }).toBe(4);
  const out = await lines(page);
  expect(out[0]).toBe("meter> read input 0 -t float32");
  expect(out[1]).toContain("timeout - no response");
  expect(out[2]).toBe("controller> write holding 0x2103 150 --scale 0.1");
  expect(out[3]).toBe("exception 0x04 (server device failure)");
});

test("long input is cut and harmless", async ({ page }) => {
  await ready(page);
  await id(page, "terminal-input").fill("read input " + "9".repeat(10_000));
  expect((await id(page, "terminal-input").inputValue()).length).toBeLessThanOrEqual(200);
  await id(page, "terminal-input").press("Enter");
  await id(page, "chip-1").click();
  await expect(id(page, "terminal-output")).toContainText("input 0: 2");
  await expect(id(page, "status")).toContainText("ready");
});

test("a failed load shows the fallback", async ({ page }) => {
  await page.route("**/*.wasm", (route) => route.abort());
  await page.goto("/");
  await expect(id(page, "fallback")).toBeVisible({ timeout: 30_000 });
  await expect(id(page, "retry")).toBeVisible();
  await expect(id(page, "chip-1")).toBeDisabled();
  await expect(id(page, "fallback").locator("img")).toHaveJSProperty("complete", true);
  expect(await id(page, "fallback").locator("img").evaluate((img) => img.naturalWidth)).toBeGreaterThan(0);
});

test("no requests to other hosts", async ({ page, baseURL }) => {
  const foreign = [];
  page.on("request", (request) => {
    if (!request.url().startsWith(new URL(baseURL).origin) && !request.url().startsWith("data:")) foreign.push(request.url());
  });
  await ready(page);
  await id(page, "chip-1").click();
  await expect(id(page, "terminal-output")).toContainText("input 0: 2");
  expect(foreign).toEqual([]);
});

for (const colorScheme of ["light", "dark"]) {
  test(`narrow screen, ${colorScheme}`, async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 800 });
    await page.emulateMedia({ colorScheme });
    await ready(page);
    await id(page, "chip-5").click();
    await expect(id(page, "controller-sp")).toHaveText("150.0 °C");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(360);
  });
}

test("a .wasm 404 shows the fallback quickly", async ({ page }) => {
  await page.route("**/*.wasm", (route) => route.fulfill({ status: 404, body: "not found" }));
  await page.goto("/");
  await expect(id(page, "fallback")).toBeVisible({ timeout: 15_000 });
});

test("only versioned files are cached for good", async ({ request }) => {
  const manifest = await (await request.get("/py/manifest.json")).json();
  const runtime = await request.get(`/pyodide/${manifest.pyodide}/pyodide.asm.wasm`);
  expect(runtime.status()).toBe(200);
  expect(runtime.headers()["cache-control"]).toContain("immutable");
  for (const path of ["/", "/py/manifest.json", "/app.js", "/py/demo.py"]) {
    expect((await request.get(path)).headers()["cache-control"]).toBe("no-cache");
  }
});

test("security headers", async ({ request }) => {
  const headers = (await request.get("/")).headers();
  expect(headers["content-security-policy"]).toContain("default-src 'self'");
  expect(headers["x-content-type-options"]).toBe("nosniff");
});

test("robots.txt allows indexing", async ({ request }) => {
  const robots = await request.get("/robots.txt");
  expect(robots.status()).toBe(200);
  expect(await robots.text()).toMatch(/User-agent: \*\s+Allow: \//);
});

test("filled buttons are readable in dark mode", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await ready(page);
  const ratio = await page.evaluate(() => {
    const rgb = (c) => c.match(/[\d.]+/g).slice(0, 3).map(Number);
    const lum = ([r, g, b]) => {
      const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const style = getComputedStyle(document.querySelector(".prompt button"));
    const [a, b] = [lum(rgb(style.color)), lum(rgb(style.backgroundColor))].sort((x, y) => y - x);
    return (a + 0.05) / (b + 0.05);
  });
  expect(ratio).toBeGreaterThanOrEqual(4.5);
});
