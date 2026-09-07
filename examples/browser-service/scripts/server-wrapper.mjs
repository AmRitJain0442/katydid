import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const python = process.env.BROWSER_EXAMPLE_PYTHON;
if (!python) {
  console.error("BROWSER_EXAMPLE_PYTHON is required");
  process.exit(2);
}

const server = spawn(python, [resolve(root, "server.py"), "--port", "4173"], {
  cwd: root,
  env: process.env,
  shell: false,
  stdio: "inherit",
  windowsHide: true,
});

let stopping = false;
function stop(signal) {
  if (stopping) return;
  stopping = true;
  server.kill(signal);
  const forced = setTimeout(() => server.kill("SIGKILL"), 2_000);
  forced.unref();
}

process.on("SIGINT", () => stop("SIGINT"));
process.on("SIGTERM", () => stop("SIGTERM"));
server.on("error", (error) => {
  console.error(`Cannot start the example server: ${error.message}`);
  process.exitCode = 2;
});
server.on("exit", (code, signal) => {
  if (!stopping && signal) console.error(`Example server stopped from ${signal}`);
  process.exit(code ?? (stopping ? 0 : 2));
});

