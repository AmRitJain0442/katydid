# Browser example

`examples/browser-service` is a self-contained Chromium test target for Katydid. It serves a
small storefront and JSON catalog on loopback, exercises the page through a real browser, and
writes the browser results into Katydid's fresh JUnit evidence path.

The example deliberately has no framework build, database, external API, secrets, or deployment.
Its product catalog lives in the Python server process and checkout is a browser-only demonstration
that collects no payment.

## Pinned setup

Install a current Node.js release supported by Playwright (22, 24, or 26) and use the committed npm
lockfile. From the example directory:

```text
npm ci
node node_modules/@playwright/test/cli.js install chromium
```

On a fresh Ubuntu CI host, install Chromium and its operating-system packages with:

```text
node node_modules/@playwright/test/cli.js install --with-deps chromium
```

The dependency and lockfile pin `@playwright/test` 1.63.0 exactly. Playwright associates each
release with particular browser binaries, so the explicit install must be repeated when that pin
changes. These commands follow Playwright's [browser installation guidance](https://playwright.dev/docs/browsers)
and [supported Node.js versions](https://playwright.dev/docs/intro).

`node_modules`, browser reports, test results, and Katydid run evidence are ignored locally. The
browser itself is held in Playwright's operating-system cache by default; it is not committed to
the repository.

## Run through Katydid

From the repository root:

```text
python scripts/dev.py cli validate examples/browser-service/quality.yaml
python scripts/dev.py cli run examples/browser-service/quality.yaml
```

The profile's parent is its default repository root, so no `--root` argument is needed. Its argv is
an explicit cross-platform array:

```text
node scripts/run-playwright.mjs {python} {report}
```

The wrapper launches Playwright through Node rather than relying on `npx`, `npm.cmd`, or shell
resolution. It passes Katydid's interpreter to the server wrapper and sets the official
`PLAYWRIGHT_JUNIT_OUTPUT_FILE` environment variable to the expanded `{report}` path. The JUnit
reporter behavior is documented in Playwright's [reporter reference](https://playwright.dev/docs/test-reporters#junit-reporter).

Playwright's configured `webServer` starts `server.py` before the test and stops its process tree
when the run ends. The server binds only to `127.0.0.1:4173`; `reuseExistingServer` is disabled so
the suite will not accidentally test a different process already using that port. This follows
Playwright's [managed web server contract](https://playwright.dev/docs/test-webserver).

## Coverage and evidence

The suite runs serially in headless Chromium and checks three user-facing contracts:

- the catalog API returns the expected bounded JSON shape;
- a shopper can filter the collection, add and remove quantities, and see delivery and totals;
- a shopper can submit the checkout form, receive an order reference, and see the cart clear.

Locators use accessible roles and names where possible, while assertions wait on visible browser
state. On failure, Playwright retains a screenshot and trace under ignored `test-results`; the
JUnit document remains in the Katydid run directory with stdout, stderr, invocation metadata, and
the aggregate gate decision.

