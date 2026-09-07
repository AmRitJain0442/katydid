import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 20_000,
  expect: { timeout: 5_000 },
  outputDir: "test-results/artifacts",
  reporter: "junit",
  use: {
    baseURL: "http://127.0.0.1:4173",
    headless: true,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  webServer: {
    command: "node scripts/server-wrapper.mjs",
    url: "http://127.0.0.1:4173/health",
    cwd: ".",
    reuseExistingServer: false,
    timeout: 15_000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
