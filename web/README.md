Embedded UI assets, served on `127.0.0.1` and compiled into every build via
`assets.go` (`//go:embed index.html app.js vendor`).

- `index.html`, `app.js` — the workbench UI. Communication uses the same-origin
  WebSocket and `/fs` directory listings; all assets remain local. Data imports
  reuse the REPL protocol, labels stay in the browser, and PNG export composes
  uPlot's canvas and DOM title onto an opaque themed canvas.
  The file tree loads folders on expansion and provides local SVG icons,
  file type/size hints, keyboard navigation, selection, and refresh/retry.
- `vendor/` — uPlot (`uPlot.iife.min.js`, `uPlot.min.css`) and its license.
- `browser.test.mjs` — a real headless-Chromium smoke test driven over CDP with
  no npm packages: offline load, bounded ingest preview, plot/zoom/hover, and
  Stop-and-reset recovery, file import and all three chart types, label
  persistence, PNG download, and real clipboard image copy. Run with
  `node --test web/browser.test.mjs`.
  Tree checks cover nested keyboard imports, cached expansion, empty folders,
  disappearing folders and retry, refresh, and mobile overflow.
  Heatmap checks cover three-column import, actual bucket-colored pixels,
  colormap switching, stable zoom scales, hover values, and exported legends.
  Colormap samples are shipped in `app.js`; heatmaps never import matplotlib
  or download color assets at runtime.
