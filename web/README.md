Embedded UI assets, served on `127.0.0.1` and compiled into every build via
`assets.go` (`//go:embed index.html app.js vendor`).

- `index.html`, `app.js` — the workbench UI (no external URLs, no `fetch`; the
  only connection is the same-origin WebSocket).
- `vendor/` — uPlot (`uPlot.iife.min.js`, `uPlot.min.css`) and its license.
- `browser.test.mjs` — a real headless-Chromium smoke test driven over CDP with
  no npm packages: offline load, bounded ingest preview, plot/zoom/hover, and
  Stop-and-reset recovery. Run with `node --test web/browser.test.mjs`.
