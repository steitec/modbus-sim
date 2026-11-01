// The page: draws the device fronts from the simulator's state and runs the Modbus terminal.
// Every command, fault and reset goes through one queue, so output keeps the click order.

import { loadSim } from "./sim.js";

const TIMEOUT_MS = 2000;
const POLL_MS = 1000;
const CHIPS = [
  { label: "Meter: L1 voltage as float32", steps: [["meter", "read input 0 -t float32"]] },
  { label: "Same registers, wrong word order", steps: [["meter", "read input 0 -t float32 -o CDAB"]] },
  { label: "Three phases and the frequency",
    steps: [["meter", "read input 0 -c 3 -t float32"], ["meter", "read input 0x46 -t float32"]] },
  { label: "Controller: set 150 °C", steps: [["controller", "write holding 0x2103 150 --scale 0.1"]] },
  { label: "Enable writing, then set 150 °C",
    steps: [["controller", "write holding 0 0x0001"], ["controller", "write holding 0x2103 150 --scale 0.1"]] },
  { label: "Read the process value", steps: [["controller", "read holding 0x2000 -t int16 --scale 0.1"]] },
  { label: "Inverter: find the SunSpec models",
    steps: [["inverter", "read holding 40000 -c 2 -t string"], ["inverter", "read holding 40002 -c 2"],
            ["inverter", "read holding 40070 -c 2"]] },
  { label: "AC current and its scale factor",
    steps: [["inverter", "read holding 40072"], ["inverter", "read holding 40076 -t int16"]] },
];
const FAULT_TEXT = {
  "meter-no-response": "the meter will not answer the next request",
  "meter-phase-loss": "the meter lost phase L2",
  "controller-sensor-break": "the controller's sensor is broken",
  "inverter-grid-loss": "the inverter lost the grid",
};
const FAULT_BADGE = { no_response: "no response", phase_loss: "phase loss", sensor_break: "sensor break",
                      grid_loss: "grid loss" };

const $ = (selector) => document.querySelector(selector);
const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`);
const status = byTestId("status");
const output = byTestId("terminal-output");
const input = byTestId("terminal-input");
const controls = () => document.querySelectorAll(".console button, .console input");

let sim = null;
let target = "meter";
let queue = Promise.resolve();
const history = [];
let historyAt = 0;

// --- drawing ------------------------------------------------------------------

function el(tag, attrs = {}, text = "") {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  node.textContent = text;
  return node;
}

const fixed = (value, digits, unit) => `${value.toFixed(digits)} ${unit}`;
const setText = (testId, text) => { byTestId(testId).textContent = text; };

function showFaults(name, faults) {
  const badge = byTestId(`fault-badge-${name}`);
  badge.hidden = faults.length === 0;
  badge.textContent = faults.map((kind) => `fault: ${FAULT_BADGE[kind] ?? kind}`).join(" · ");
}

function lamp(testId, on, text) {
  const badge = byTestId(testId);
  badge.classList.toggle("on", on);
  if (text !== undefined) badge.textContent = text;
}

function render(state) {
  const m = state.meter;
  for (const n of [1, 2, 3]) {
    setText(`meter-l${n}-v`, fixed(m[`voltage_l${n}`], 1, "V"));
    setText(`meter-l${n}-a`, fixed(m[`current_l${n}`], 2, "A"));
    setText(`meter-l${n}-w`, fixed(m[`power_l${n}`], 0, "W"));
    $(`#meter-l${n}`).classList.toggle("dead", m[`voltage_l${n}`] < 1);
  }
  setText("meter-power", fixed(m.power_total / 1000, 2, "kW"));
  setText("meter-energy", fixed(m.energy_import, 4, "kWh"));
  setText("meter-frequency", fixed(m.frequency, 2, "Hz"));
  setText("meter-pf", m.power_factor.toFixed(3));
  showFaults("meter", m.faults);

  const c = state.controller;
  setText("controller-pv", c.pv == null ? "S.Err" : fixed(c.pv, 1, "°C"));
  setText("controller-sp", fixed(c.setpoint, 1, "°C"));
  setText("controller-mv", fixed(c.mv, 1, "%"));
  $("#controller-bar").style.width = `${Math.max(0, Math.min(100, c.mv))}%`;
  lamp("controller-run", c.running, c.running ? "RUN" : "STOP");
  lamp("controller-manual", c.manual);
  lamp("controller-comms", c.comms_write);
  showFaults("controller", c.faults);

  const inv = state.inverter;
  lamp("inverter-state", inv.state === "MPPT", inv.state);
  byTestId("inverter-state").classList.toggle("alarm", inv.state === "fault");
  setText("inverter-power", fixed(inv.power / 1000, 2, "kW"));
  setText("inverter-energy", fixed(inv.energy / 1000, 2, "kWh"));
  setText("inverter-dc", fixed(inv.dc_voltage, 0, "V"));
  setText("inverter-heatsink", fixed(inv.heatsink, 1, "°C"));
  const minutes = Math.floor(inv.hour * 60) % 1440;
  setText("inverter-clock", `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`);
  // the sun's position over the day bar: 0 % at 00:00, 100 % at 24:00
  $("#inverter-sun").style.left = `${(inv.hour / 24) * 100}%`;
  $("#inverter-sun").classList.toggle("night", inv.hour < 6 || inv.hour > 20);
  showFaults("inverter", inv.faults);
}

function print(text, kind = "") {
  output.append(el("div", { class: `line ${kind}` }, text));
  output.scrollTop = output.scrollHeight;
}

// --- running ------------------------------------------------------------------

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function enqueue(job) {
  queue = queue.then(job).catch((error) => print(`internal error: ${error.message}`, "error"));
  return queue;
}

function setTarget(name) {
  target = name;
  for (const button of document.querySelectorAll("[data-target]")) {
    button.setAttribute("aria-pressed", String(button.dataset.target === name));
  }
  $("#prompt-label").textContent = `${name}>`;
}

function send(to, line) {
  enqueue(async () => {
    print(`${to}> ${line}`, "command");
    const result = await sim.run(to, line);
    for (const out of result.lines) {
      if (out.kind === "timeout") await sleep(TIMEOUT_MS);
      print(out.text, out.kind);
    }
    render(result.state);
  });
}

function inject(fault) {
  enqueue(async () => {
    render(await sim.inject(fault));
    print(`# fault: ${FAULT_TEXT[fault]}`, "note");
  });
}

function reset() {
  enqueue(async () => {
    render(await sim.reset());
    print("# bench reset: devices back to power-on, faults cleared, controller writing off", "note");
  });
}

function wire() {
  const chips = $("#chips");
  CHIPS.forEach((chip, i) => {
    const button = el("button", { type: "button", "data-testid": `chip-${i + 1}`, disabled: "" });
    button.append(el("span", { class: "step" }, String(i + 1)), el("span", {}, chip.label),
                  el("code", {}, chip.steps.map(([to, line]) => `${to}> ${line}`).join("  ·  ")));
    button.addEventListener("click", () => {
      for (const [to, line] of chip.steps) {
        setTarget(to);
        send(to, line);
      }
    });
    chips.append(button);
  });

  for (const button of document.querySelectorAll("[data-target]")) {
    button.addEventListener("click", () => setTarget(button.dataset.target));
  }
  $("#prompt").addEventListener("submit", (event) => {
    event.preventDefault();
    const line = input.value.trim();
    if (!line) return;
    history.push(line);
    historyAt = history.length;
    input.value = "";
    send(target, line);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    historyAt = Math.max(0, Math.min(history.length, historyAt + (event.key === "ArrowUp" ? -1 : 1)));
    input.value = history[historyAt] ?? "";
  });
  for (const button of document.querySelectorAll("[data-fault]")) {
    button.addEventListener("click", () => inject(button.dataset.fault));
  }
  byTestId("reset").addEventListener("click", reset);
  byTestId("retry").addEventListener("click", () => location.reload());
}

async function start() {
  wire();
  try {
    sim = await loadSim((text) => { status.textContent = text; });
  } catch (error) {
    document.body.classList.replace("loading", "failed");
    status.textContent = "could not start";
    $("#fallback-reason").textContent = `(${error.message})`;
    byTestId("fallback").hidden = false;
    return;
  }
  render(sim.state());
  // the devices live on: values change with time, so redraw them every second
  setInterval(() => { if (!document.hidden) render(sim.state()); }, POLL_MS);
  $("#version").textContent = sim.version;
  for (const control of controls()) control.disabled = false;
  document.body.classList.remove("loading");
  status.textContent = "ready – try the buttons below";
  print("# modbus-sim is running in your browser. Pick a device and type a command (help), or use the buttons.", "note");
}

start();
