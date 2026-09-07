import { defineConfig } from "../../examples/browser-service/node_modules/@playwright/test/index.mjs";

export default defineConfig({
  testDir: ".",
  testMatch: "dashboard.spec.mjs",
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 40_000,
  expect: { timeout: 8_000 },
  outputDir: "test-results/artifacts",
  reporter: "junit",
  use: {
    baseURL: "http://127.0.0.1:4174",
    headless: true,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  webServer: {
    command: "node serve.mjs",
    url: "http://127.0.0.1:4174/",
    cwd: ".",
    reuseExistingServer: false,
    timeout: 20_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
