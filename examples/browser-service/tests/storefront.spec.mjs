import { expect, test } from "@playwright/test";

test("catalog API returns the bounded product contract", async ({ request }) => {
  const response = await request.get("/api/catalog");
  expect(response.ok()).toBeTruthy();
  expect(response.headers()["content-type"]).toContain("application/json");
  const catalog = await response.json();
  expect(catalog.products).toHaveLength(3);
  expect(catalog.products).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ id: "alpine-mug", price: 24, category: "Camp" }),
    ]),
  );
});

test("shopper filters products and adjusts a cart", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Pack less. Notice more." })).toBeVisible();
  await expect(page.locator("[data-products] article")).toHaveCount(3);

  await page.getByRole("button", { name: "Camp", exact: true }).click();
  await expect(page.locator("[data-products] article")).toHaveCount(1);
  await expect(page.getByRole("heading", { name: "Alpine Mug" })).toBeVisible();

  await page.getByRole("button", { name: "Add Alpine Mug to cart" }).click();
  await page.getByRole("button", { name: "Add one Alpine Mug" }).click();
  await expect(page.getByLabel("Alpine Mug quantity")).toHaveText("2");
  await expect(page.locator("[data-subtotal]")).toHaveText("$48.00");
  await expect(page.locator("[data-delivery]")).toHaveText("$0.00");
  await expect(page.locator("[data-total]")).toHaveText("$48.00");

  await page.getByRole("button", { name: "Remove one Alpine Mug" }).click();
  await expect(page.getByLabel("Alpine Mug quantity")).toHaveText("1");
  await expect(page.locator("[data-total]")).toHaveText("$29.00");
});

test("shopper validates checkout and receives an order reference", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Add Signal Tote to cart" }).click();
  await page.getByRole("button", { name: "Continue to checkout" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();

  await page.getByLabel("Email").fill("trail@example.com");
  await page.getByLabel("Postal code").fill("59601");
  await page.getByRole("button", { name: "Place order" }).click();

  const confirmation = page.locator("[data-confirmation]");
  await expect(confirmation).toBeVisible();
  await expect(confirmation).toContainText("See you beyond the pavement.");
  await expect(page.locator("[data-order-reference]")).toHaveText(/^SS-[A-Z0-9]+$/);
  await expect(page.locator("[data-cart-count]")).toHaveText("0");
  await expect(page.getByRole("button", { name: "Continue to checkout" })).toBeDisabled();
});

