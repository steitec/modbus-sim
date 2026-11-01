// Browser tests for the demo page. They run against a served site, by default the
// nginx container: docker run --rm -p 8080:80 modbus-sim-web (DEMO_URL to change it).
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "tests",
  testMatch: "*.spec.mjs",
  timeout: 90_000,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never", outputFolder: "test-results/html" }]] : "list",
  outputDir: "test-results/artifacts",
  use: { baseURL: process.env.DEMO_URL ?? "http://localhost:8080", trace: "retain-on-failure" },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
  ],
});
