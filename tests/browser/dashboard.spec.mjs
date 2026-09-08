import { expect, test } from "../../examples/browser-service/node_modules/@playwright/test/index.mjs";

async function taskRecord(page) {
  return JSON.parse(await page.locator("#record").textContent());
}

async function enqueueCatalog(page, previousId = null) {
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
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("button", { name: "Enqueue task" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(pageErrors).toEqual([]);
});
