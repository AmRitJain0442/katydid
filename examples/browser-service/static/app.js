const productsElement = document.querySelector("[data-products]");
const cartLinesElement = document.querySelector("[data-cart-lines]");
const emptyCartElement = document.querySelector("[data-empty-cart]");
const checkoutButton = document.querySelector("[data-checkout]");
const dialog = document.querySelector("[data-checkout-dialog]");
const form = document.querySelector("[data-checkout-form]");
const confirmation = document.querySelector("[data-confirmation]");
const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

let products = [];
let filter = "All";
const cart = new Map();

function productCard(product, index) {
  const article = document.createElement("article");
  article.className = "product-card";
  article.style.setProperty("--delay", `${index * 80}ms`);

  const visual = document.createElement("div");
  visual.className = `product-visual product-${product.id}`;
  visual.setAttribute("aria-hidden", "true");
  const mark = document.createElement("span");
  mark.textContent = product.mark;
  visual.append(mark);

  const category = document.createElement("p");
  category.className = "product-category";
  category.textContent = product.category;

  const name = document.createElement("h3");
  name.textContent = product.name;

  const description = document.createElement("p");
  description.className = "product-description";
  description.textContent = product.description;

  const footer = document.createElement("div");
  footer.className = "product-footer";
  const price = document.createElement("span");
  price.textContent = money.format(product.price);
  const add = document.createElement("button");
  add.type = "button";
  add.dataset.add = product.id;
  add.textContent = "Add to pack +";
  add.setAttribute("aria-label", `Add ${product.name} to cart`);
  footer.append(price, add);
  article.append(visual, category, name, description, footer);
  return article;
}

function renderProducts() {
  productsElement.replaceChildren();
  const visible = products.filter((product) => filter === "All" || product.category === filter);
  for (const [index, product] of visible.entries()) {
    productsElement.append(productCard(product, index));
  }
}

function changeQuantity(id, amount) {
  const next = (cart.get(id) ?? 0) + amount;
  if (next <= 0) cart.delete(id);
  else cart.set(id, next);
  renderCart();
}

function cartLine(product, quantity) {
  const row = document.createElement("div");
  row.className = "cart-line";
  row.dataset.cartItem = product.id;
  const details = document.createElement("div");
  const name = document.createElement("h3");
  name.textContent = product.name;
  const price = document.createElement("p");
  price.textContent = `${money.format(product.price)} each`;
  details.append(name, price);

  const controls = document.createElement("div");
  controls.className = "quantity";
  const decrease = document.createElement("button");
  decrease.type = "button";
  decrease.dataset.decrease = product.id;
  decrease.textContent = "−";
  decrease.setAttribute("aria-label", `Remove one ${product.name}`);
  const count = document.createElement("output");
  count.textContent = String(quantity);
  count.setAttribute("aria-label", `${product.name} quantity`);
  const increase = document.createElement("button");
  increase.type = "button";
  increase.dataset.increase = product.id;
  increase.textContent = "+";
  increase.setAttribute("aria-label", `Add one ${product.name}`);
  controls.append(decrease, count, increase);

  const total = document.createElement("strong");
  total.textContent = money.format(product.price * quantity);
  row.append(details, controls, total);
  return row;
}

function renderCart() {
  cartLinesElement.replaceChildren();
  let itemCount = 0;
  let subtotal = 0;
  for (const [id, quantity] of cart) {
    const product = products.find((candidate) => candidate.id === id);
    if (!product) continue;
    itemCount += quantity;
    subtotal += product.price * quantity;
    cartLinesElement.append(cartLine(product, quantity));
  }
  const delivery = subtotal === 0 || subtotal >= 40 ? 0 : 5;
  document.querySelector("[data-cart-count]").textContent = String(itemCount);
  document.querySelector("[data-subtotal]").textContent = money.format(subtotal);
  document.querySelector("[data-delivery]").textContent = subtotal === 0 ? "—" : money.format(delivery);
  document.querySelector("[data-total]").textContent = money.format(subtotal + delivery);
  emptyCartElement.hidden = itemCount > 0;
  checkoutButton.disabled = itemCount === 0;
}

productsElement.addEventListener("click", (event) => {
  const button = event.target.closest("[data-add]");
  if (button) changeQuantity(button.dataset.add, 1);
});

cartLinesElement.addEventListener("click", (event) => {
  const increase = event.target.closest("[data-increase]");
  const decrease = event.target.closest("[data-decrease]");
  if (increase) changeQuantity(increase.dataset.increase, 1);
  if (decrease) changeQuantity(decrease.dataset.decrease, -1);
});

document.querySelector("[data-filters]").addEventListener("click", (event) => {
  const button = event.target.closest("[data-filter]");
  if (!button) return;
  filter = button.dataset.filter;
  for (const option of document.querySelectorAll("[data-filter]")) {
    const active = option === button;
    option.classList.toggle("active", active);
    option.setAttribute("aria-pressed", String(active));
  }
  renderProducts();
});

document.querySelector("[data-cart-jump]").addEventListener("click", () => {
  document.querySelector("[data-cart-panel]").scrollIntoView({ behavior: "smooth" });
});
checkoutButton.addEventListener("click", () => dialog.showModal());
form.addEventListener("submit", (event) => {
  if (event.submitter?.value !== "place-order") return;
  event.preventDefault();
  if (!form.reportValidity()) return;
  dialog.close();
  document.querySelector("[data-order-reference]").textContent = `SS-${Date.now()
    .toString(36)
    .toUpperCase()}`;
  confirmation.hidden = false;
  form.reset();
  cart.clear();
  renderCart();
  confirmation.focus();
});

try {
  const response = await fetch("/api/catalog");
  if (!response.ok) throw new Error(`Catalog returned ${response.status}`);
  ({ products } = await response.json());
  renderProducts();
  renderCart();
} catch (error) {
  productsElement.textContent = "The field collection is temporarily unavailable.";
  console.error(error);
}
