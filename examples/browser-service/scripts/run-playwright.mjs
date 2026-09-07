import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const [python, reportArgument] = process.argv.slice(2);

if (!python || !reportArgument) {
  console.error("Usage: node scripts/run-playwright.mjs PYTHON JUNIT_REPORT");
  process.exit(2);
}

const report = resolve(reportArgument);
const cli = resolve(root, "node_modules", "@playwright", "test", "cli.js");
if (!existsSync(cli)) {
  console.error("Playwright is not installed; run npm ci in examples/browser-service");
  process.exit(2);
}

const result = spawnSync(process.execPath, [cli, "test", "--project=chromium"], {
  cwd: root,
  env: {
    ...process.env,
    BROWSER_EXAMPLE_PYTHON: python,
    PLAYWRIGHT_JUNIT_OUTPUT_FILE: report,
    PLAYWRIGHT_JUNIT_STRIP_ANSI: "1",
  },
  shell: false,
  stdio: "inherit",
  windowsHide: true,
});

if (result.error) {
  console.error(`Cannot start Playwright: ${result.error.message}`);
  process.exit(2);
}
process.exit(result.status ?? 2);

