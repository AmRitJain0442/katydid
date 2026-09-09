# Vultron visual identity

Vultron is the product name for the autonomous testing platform and its agent
workspace. The Orders Lab demonstration application uses the same visual system.

## Design reference

The workspace draws on [DeepSeek Harness](https://www.deepseek.com/harness/en/):
compact navigation, traceable execution, a detailed record inspector, and a rounded
instruction composer. The Vultron mark, layouts, and CSS are original. There are no
copied logos, remote fonts, new UI dependencies, or decorative controls implying
unimplemented capabilities.

## Theme contract

| Role | Value |
| --- | --- |
| Canvas | `#09090b` |
| Panel | `#111114` |
| Elevated surface | `#19191e` |
| Primary text | `#f4f4f5` |
| Secondary text | `#a1a1aa` |
| Brand accent | `#ef3340` |
| Primary button background | `#ce2634` (4.83:1 contrast with primary text) |
| Accent text | `#ff7680` |
| Typography | Segoe UI Variable / Segoe UI; Cascadia Code for records |

Use red for the brand and primary action, quiet neutral separators, and small
semantic status accents. Status text accompanies color. Keep native controls,
visible keyboard focus, reduced-motion support, and independently scrollable
evidence. Both applications use local assets and CSS variables.

## Logo asset

The current workspace uses the original split-chevron V in
[`vultron-mark.svg`](../src/katydid/static/vultron-mark.svg). Two solid red shapes
are separated by a narrow diagonal cut. The only color is `#f04452`; the background
is transparent, with no gradients, shadows, outlines, or enclosing badge.

This is a native SVG, drawn directly rather than generated as a bitmap. The same
asset serves the sidebar, welcome screen, workspace avatar, footer, and SVG favicon.
Keep its 64-by-64 viewBox and built-in clear space intact when reusing it.

## Compatibility

This release changes the product identity and frontend presentation. Existing
`katydid` CLI commands, Python imports, distribution name, `.katydid` storage,
configuration files, environment variables, HTTP headers, and GitHub repository
URLs continue to work. They retain their established names to preserve installed
environments, credentials, CI integrations, and historical evidence. Recorded
acceptance documents retain the names and revisions used during those runs.

The rebrand does not rerun or reset the completed Gemini repair. Existing orders,
task history, provider configuration, and delivery policy remain intact.

## Validation

The dashboard browser flow covers dispatch, pause, steering, cancellation,
completion, expanded event persistence, and narrow-screen overflow. Existing
HTTP boundary tests validate the local assets and API controls. Orders Lab keeps
its existing quantity, total, inventory, and order-history browser contract.
Desktop and mobile screenshots are reviewed against the live local services.
