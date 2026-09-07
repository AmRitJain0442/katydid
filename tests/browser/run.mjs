import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const [python, reportArgument] = process.argv.slice(2);
const cli = resolve(root, "../../examples/browser-service/node_modules/@playwright/test/cli.js");

if (!python || !reportArgument) {
  console.error("Usage: node run.mjs PYTHON JUNIT_REPORT");
  process.exit(2);
}
if (!existsSync(cli)) {
  console.error("Pinned Playwright is unavailable in examples/browser-service/node_modules");
  process.exit(2);
}

const child = spawn(process.execPath, [cli, "test", "--project=chromium"], {
  cwd: root,
  env: {
    ...process.env,
    KATYDID_BROWSER_PYTHON: python,
    PLAYWRIGHT_JUNIT_OUTPUT_FILE: resolve(reportArgument),
    PLAYWRIGHT_JUNIT_STRIP_ANSI: "1",
  },
  shell: false,
  stdio: "inherit",
  windowsHide: true,
});

let stopping = false;
function stop(signal) {
  if (stopping) return;
  stopping = true;
  child.kill(signal);
  const forced = setTimeout(() => child.kill("SIGKILL"), 2_000);
  forced.unref();
}

process.on("SIGINT", () => stop("SIGINT"));
process.on("SIGTERM", () => stop("SIGTERM"));
child.on("error", (error) => {
  console.error(`Cannot start Playwright: ${error.message}`);
  process.exitCode = 2;
});
child.on("exit", (code, signal) => {
  if (!stopping && signal) console.error(`Playwright stopped from ${signal}`);
  process.exit(code ?? (stopping ? 0 : 2));
});
