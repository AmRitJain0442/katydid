import { expect, test } from "../../examples/browser-service/node_modules/@playwright/test/index.mjs";

async function taskRecord(page) {
  return JSON.parse(await page.locator("#record").textContent());
}

async function enqueueCatalog(page, previousId = null) {
  await page.getByRole("button", { name: "New investigation", exact: true }).click();
  await page.locator("#repository").selectOption("catalog");
  await page.getByRole("button", { name: "Enqueue task" }).click();
  await expect(page.locator("#detail-content")).toBeVisible();
  await expect(page.locator("#detail-empty")).toBeHidden();
  if (previousId !== null) {
    await expect.poll(async () => (await taskRecord(page)).id).not.toBe(previousId);
  }
  return taskRecord(page);
}

test("dashboard controls durable tasks and renders completed evidence", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Vultron", exact: true })).toBeVisible();
  await expect(page.locator("#repository")).toBeEnabled();

  const first = await enqueueCatalog(page);
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  await expect(page.locator("#detail-state")).toHaveText("paused");
  await expect(page.getByRole("button", { name: "Resume", exact: true })).toBeEnabled();

  const instruction = "Retest after checking the operator-provided constraint.";
  await page.locator("#instruction").fill(instruction);
  await page.getByRole("button", { name: "Send note" }).click();
  await expect
    .poll(async () => (await taskRecord(page)).instructions)
    .toContain(instruction);

  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.locator("#detail-state")).toHaveText("cancelled");
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Resume", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Cancel", exact: true })).toBeDisabled();
  await expect(page.locator("#instruction")).toBeDisabled();
  await expect(page.getByRole("button", { name: "Send note" })).toBeDisabled();

  const firstResponse = await page.request.get(`/api/tasks/${first.id}`);
  expect(firstResponse.ok()).toBeTruthy();
  const firstTask = (await firstResponse.json()).task;
  expect(firstTask.state).toBe("cancelled");
  expect(firstTask.instructions).toContain(instruction);
  const firstEvents = await page.request.get(`/api/tasks/${first.id}/events`);
  const firstKinds = (await firstEvents.json()).events.map((event) => event.kind);
  expect(firstKinds).toEqual(expect.arrayContaining(["pause", "steer", "cancel"]));

  const second = await enqueueCatalog(page, first.id);
  expect(second.id).not.toBe(first.id);
  await expect
    .poll(
      async () => {
        const response = await page.request.get(`/api/tasks/${second.id}`);
        return (await response.json()).task.state;
      },
      { timeout: 30_000 },
    )
    .toBe("completed");

  await page.locator("#refresh").click();
  await expect(page.locator("#detail-state")).toHaveText("completed");
  const completedResponse = await page.request.get(`/api/tasks/${second.id}`);
  expect(completedResponse.ok()).toBeTruthy();
  const completed = (await completedResponse.json()).task;
  const baseline = completed.result.baseline;
  const evidence = baseline.results[0].evidence;
  expect(completed.result.outcome).toBe("healthy");
  expect(completed.result.ai_calls).toBe(0);
  expect(baseline.gate.passed).toBe(true);
  expect(baseline.results[0].status).toBe("passed");
  expect(evidence.status).toBe("passed");
  expect(evidence.tests).toBe(5);
  expect(evidence.failures).toBe(0);
  expect(evidence.errors).toBe(0);
  expect(evidence.skipped).toBe(0);
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Resume", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Cancel", exact: true })).toBeDisabled();
  await expect(page.locator("#instruction")).toBeDisabled();
  await expect(page.getByRole("button", { name: "Send note" })).toBeDisabled();
  const eventDetails = page.locator("#timeline details").first();
  await eventDetails.locator("summary").click();
  await expect(eventDetails).toHaveAttribute("open", "");
  await page.locator("#refresh").click();
  await expect(eventDetails).toHaveAttribute("open", "");
  await page.getByRole("tab", { name: "Checks" }).click();
  await expect(page.locator(".check-row")).toHaveCount(1);
  await page.locator(".check-row summary").click();
  await expect(page.locator(".check-row pre").first()).toContainText('"tests": 5');
  await expect(page.locator("#outcome-title")).toHaveText("All required checks passed");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "New investigation", exact: true }).click();
  await expect(page.getByRole("button", { name: "Enqueue task" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(pageErrors).toEqual([]);
});

test("investigation navigation keeps evidence truthful and supports keyboard and export", async ({ page }) => {
  const check = (status, failures) => ({ id: "unit", status, duration_seconds: 1.25, evidence: { tests: 3, failures, errors: 0, skipped: 0 } });
  const rows = [
    { id: "old-failure", repository: "catalog", state: "failed", created_at: 100, payload: { stage: "nightly" }, result: { baseline: { gate: { passed: false }, results: [check("failed", 1)] } } },
    { id: "new-repair", repository: "payments <script>bad()</script>", state: "completed", created_at: 200, payload: { stage: "merge" }, result: { outcome: "repaired", baseline: { gate: { passed: false }, results: [check("failed", 1)] }, verification: { gate: { passed: true }, results: [check("passed", 0)] } } },
  ];
  const events = [
    { id: 1, kind: "transition", state: "testing", created_at: 200 },
    { id: 2, kind: "lease_renewed", state: "testing", created_at: 201 },
    { id: 3, kind: "transition", state: "completed", created_at: 202 },
  ];
  await page.route("**/api/tasks**", async route => {
    const path = new URL(route.request().url()).pathname;
    const id = path.split("/")[3];
    await route.fulfill({ json: path.endsWith("/events") ? { events } : id ? { task: rows.find(task => task.id === id) } : { tasks: rows } });
  });
  await page.goto("/");
  await expect(page.locator(".task-card").first()).toHaveAttribute("data-id", "new-repair");
  await page.getByRole("button", { name: "Attention", exact: true }).click();
  await expect(page.locator(".task-card")).toHaveCount(1);
  await expect(page.locator(".task-card")).toHaveAttribute("data-id", "old-failure");
  await page.getByRole("button", { name: "All", exact: true }).click();
  await page.getByRole("searchbox").fill("new-repair");
  await expect(page.locator(".task-card")).toHaveCount(1);
  await page.locator(".task-card").click();
  await expect(page.locator("#detail-repository")).toHaveText(rows[1].repository);
  await expect(page.locator("#detail-repository script")).toHaveCount(0);
  await expect(page.locator("#timeline .event")).toHaveCount(2);
  await page.getByLabel("Show system events").check();
  await expect(page.locator("#timeline .event")).toHaveCount(3);
  await page.getByRole("tab", { name: "Activity" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Checks" })).toBeFocused();
  await expect(page.locator("#checks-panel")).toBeVisible();
  await expect(page.locator(".check-group").first().locator(".state-chip")).toHaveText("failed");
  await expect(page.locator(".check-group").last().locator(".state-chip")).toHaveText("passed");
  await page.locator(".check-row").first().locator("summary").click();
  await page.locator("#refresh").click();
  await expect(page.locator(".check-row").first()).toHaveAttribute("open", "");
  await expect(page.getByRole("searchbox")).toHaveValue("new-repair");
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download task record" }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("vultron-new-repair.json");
  const stream = await download.createReadStream();
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk);
  const exported = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  expect(exported.task.id).toBe("new-repair");
  expect(exported.events).toEqual(events);
  await page.getByRole("button", { name: "Toggle runtime and evidence" }).click();
  await expect(page.locator("#context-panel")).toBeHidden();
  await page.getByRole("button", { name: "Toggle runtime and evidence" }).click();
  await expect(page.locator("#context-panel")).toBeVisible();
});

test("failed steering preserves the operator note and runtime failure is visible", async ({ page }) => {
  const task = { id: "paused-task", repository: "catalog", state: "paused", created_at: 200 };
  await page.route("**/api/tasks**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/control")) return route.fulfill({ status: 409, json: { error: "Control boundary changed" } });
    await route.fulfill({ json: path.endsWith("/events") ? { events: [] } : path.endsWith(task.id) ? { task } : { tasks: [task] } });
  });
  await page.route("**/api/runtime", route => route.fulfill({ status: 503, json: { error: "Unavailable" } }));
  await page.goto("/");
  await page.locator(".task-card").click();
  await page.getByLabel("Steering instruction").fill("Keep this instruction until accepted.");
  await page.getByRole("button", { name: "Send note" }).click();
  await expect(page.locator("#notice")).toContainText("Control rejected");
  await expect(page.getByLabel("Steering instruction")).toHaveValue("Keep this instruction until accepted.");
  await expect(page.getByRole("button", { name: "Resume", exact: true })).toBeEnabled();
  await expect(page.locator("#runtime-info")).toContainText("Runtime status unavailable");
  await expect(page.locator(".context-badge")).toHaveText("OFFLINE");
  for (const width of [320, 390, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
});

test("live workflow displays real subprocess output before the worker finishes", async ({ page }) => {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  const task = await enqueueCatalog(page);
  await page.getByRole("tab", { name: "Live workflow" }).click();
  await expect(page.locator(".live-step[data-state=running]")).toHaveCount(1);
  const running = page.locator(".live-step[data-state=running]");
  await expect(running).toHaveAttribute("open", "");
  await expect(running.locator('[data-stream="stdout"]')).toContainText("Starting catalog contract checks");
  const response = await page.request.get(`/api/tasks/${task.id}`);
  expect((await response.json()).task.state).toBe("testing");
  await expect(running.locator('[data-stream="stdout"]')).toContainText("Contract progress");
  await page.getByLabel("Follow active tool").uncheck();
  await page.getByRole("button", { name: "Refresh tasks" }).click();
  await expect(page.getByRole("tab", { name: "Live workflow" })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".live-step")).toHaveAttribute("open", "");
  await expect.poll(async () => (await (await page.request.get(`/api/tasks/${task.id}`)).json()).task.state, { timeout: 30000 }).toBe("completed");
  await expect(page.locator(".live-step .state-dot")).toHaveText("passed");
  await expect(page.locator("#live-sync")).toHaveText("RECORDED");
  await expect(page.locator('[data-stream="stdout"]')).toContainText("Contract progress 8/8");
  for (const width of [320, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  expect(errors).toEqual([]);
});
