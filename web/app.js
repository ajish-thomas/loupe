// Offline colormap samples from the bundled matplotlib palettes; no runtime import.
const COLORMAPS = {
  "viridis": [
    "#440154",
    "#481a6c",
    "#472f7d",
    "#414487",
    "#39568c",
    "#31688e",
    "#2a788e",
    "#23888e",
    "#1f988b",
    "#22a884",
    "#35b779",
    "#54c568",
    "#7ad151",
    "#a5db36",
    "#d2e21b",
    "#fde725"
  ],
  "plasma": [
    "#0d0887",
    "#330597",
    "#5002a2",
    "#6a00a8",
    "#8405a7",
    "#9c179e",
    "#b12a90",
    "#c33d80",
    "#d35171",
    "#e16462",
    "#ed7953",
    "#f68f44",
    "#fca636",
    "#fec029",
    "#f9dc24",
    "#f0f921"
  ],
  "inferno": [
    "#000004",
    "#0c0826",
    "#240c4f",
    "#420a68",
    "#5d126e",
    "#781c6d",
    "#932667",
    "#ae305c",
    "#c73e4c",
    "#dd513a",
    "#ed6925",
    "#f8850f",
    "#fca50a",
    "#fac62d",
    "#f2e661",
    "#fcffa4"
  ],
  "coolwarm": [
    "#3b4cc0",
    "#4f69d9",
    "#6485ec",
    "#7b9ff9",
    "#93b5fe",
    "#aac7fd",
    "#c0d4f5",
    "#d4dbe6",
    "#e5d8d1",
    "#f2cbb7",
    "#f7b89c",
    "#f5a081",
    "#ee8468",
    "#e0654f",
    "#cc403a",
    "#b40426"
  ],
  "turbo": [
    "#30123b",
    "#4143a7",
    "#4771e9",
    "#3e9bfe",
    "#22c5e2",
    "#1ae4b6",
    "#46f884",
    "#88ff4e",
    "#b9f635",
    "#e1dd37",
    "#faba39",
    "#fd8d27",
    "#f05b12",
    "#d63506",
    "#af1801",
    "#7a0403"
  ]
};
let heatmapCmap = "viridis";

// Minimal tier-1 client: sends execute_request over /ws, renders stream/
// error/execute_result text, and draws a "figure" message's binary
// float32 payload (PLAN.md: header immediately followed by raw bytes,
// x then y) with uPlot. See PLAN.md, "Architecture", for the protocol.

const codeEl = document.getElementById("code");
const runEl = document.getElementById("run");
const resetZoomEl = document.getElementById("reset-zoom");
const outputEl = document.getElementById("output");
const chartEl = document.getElementById("chart");
const stopEl = document.getElementById("stop");
const examplesEl = document.getElementById("examples");
const themeEl = document.getElementById("theme-toggle");
const systemTheme = matchMedia("(prefers-color-scheme: dark)");
let colors = {};
let followsSystemTheme = true;
try {
  followsSystemTheme = !["light", "dark"].includes(localStorage.getItem("loupe-theme"));
} catch { /* Storage is optional. */ }

let chart = null;
let lastFigure = null;
let labels = { title: "", x: "", y: "" };
const labelInputs = Object.fromEntries(Object.keys(labels).map(key => [key, document.getElementById(`label-${key}`)]));
const exportEl = document.getElementById("export-png");
const copyEl = document.getElementById("copy-image");
const importStatus = document.getElementById("import-status");
const dataPlotEl = document.getElementById("data-plot");
const dataKindEl = document.getElementById("data-kind");
const dataXEl = document.getElementById("data-x");
const dataYEl = document.getElementById("data-y");
let importRequest = null;
let dataPlotRequest = null;
let imported = false;
let importedSchema = null;
let directory = null;
let fileRequest = null;
let pendingFigure = null; // the JSON header waiting for its binary payload
let nextMsgId = 1;
let latestFigureMsgID = null;
const figureRequests = new Map();
let zoomTimer = null;
// Guards the setScale hook against the scale-change(s) uPlot fires while
// *constructing* a chart (including the one render() creates to show a
// zoom response) -- only a change after that, i.e. real drag/dblclick
// interaction, should trigger a new zoom_request. See render().
let suppressScaleHook = false;

const ws = new WebSocket(`ws://${location.host}/ws`);
ws.binaryType = "arraybuffer";

ws.addEventListener("open", () => setConnection(true));
ws.addEventListener("close", () => { setConnection(false); log("disconnected"); });
ws.addEventListener("error", () => log("websocket error"));
ws.addEventListener("message", (event) => {
  if (typeof event.data === "string") {
    handleControlMessage(JSON.parse(event.data));
  } else {
    handleFigurePayload(event.data);
  }
});

runEl.addEventListener("click", run);
stopEl.addEventListener("click", () => {
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: "interrupt_request"}));
});
resetZoomEl.addEventListener("click", () => requestZoom(null, null));
codeEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    run();
  }
});

// Starter snippets for the Input editor. Each uses only in-memory data so it
// runs anywhere; picking one fills the editor and the user runs it themselves.
const examples = [
  {
    label: "Damped wave — line",
    code: `x = np.linspace(0, 4 * np.pi, 1000)
y = np.sin(x) * np.exp(-x / 12)
plot(x, y, title="Damped wave")`,
  },
  {
    label: "IR drop — heatmap",
    code: `x, y = np.meshgrid(np.linspace(0, 100, 50), np.linspace(0, 100, 50))
irdrop = 20 + 80 * np.exp(-((x - 65)**2 + (y - 45)**2) / 500)
heatmap(x.ravel(), y.ravel(), irdrop.ravel(), cmap="inferno", bins=8, title="IR drop")`,
  },
  {
    label: "Correlated sample — scatter",
    code: `rng = np.random.default_rng(4)
x = rng.normal(size=100_000)
y = 0.5 * x + rng.normal(size=100_000)
scatter(x, y, max_points=8000, title="Correlated sample")`,
  },
  {
    label: "Normal distribution — histogram",
    code: `samples = np.random.default_rng(0).normal(size=200_000)
histogram(samples, bins=50, title="Normal distribution")`,
  },
  {
    label: "One million points — M4 line",
    code: `x = np.arange(1_000_000)
y = np.sin(x / 5000) + 0.3 * np.sin(x / 137)
plot(x, y, title="1M points, reduced with M4")`,
  },
  {
    label: "LazyFrame — data preview",
    code: `pl.LazyFrame({
    "t": range(1000),
    "value": [i ** 0.5 for i in range(1000)],
})`,
  },
];
for (const [index, example] of examples.entries()) {
  examplesEl.add(new Option(example.label, String(index)));
}
examplesEl.addEventListener("change", () => {
  const example = examples[Number(examplesEl.value)];
  examplesEl.selectedIndex = 0; // it's an action menu, not a persistent choice
  if (!example) return;
  codeEl.value = example.code;
  codeEl.focus();
});

function run() {
  if (ws.readyState !== WebSocket.OPEN) return;
  cancelZoomRequests();
  outputEl.textContent = "";
  const msg = { type: "execute_request", msg_id: String(nextMsgId++), code: codeEl.value };
  latestFigureMsgID = msg.msg_id;
  figureRequests.set(msg.msg_id, { viewport: null });
  ws.send(JSON.stringify(msg));
}

// PLAN.md's "zoom-settle re-aggregation": ask the kernel to re-run the
// *currently displayed* figure's reduction for a new viewport, rather
// than re-sending any code. min/max null means "reset to the full range"
// (api.Figure.replot(None)). The response arrives exactly like a normal
// execute's figure -- same message shapes -- so it needs no separate
// handling in handleControlMessage/handleFigurePayload.
function requestZoom(min, max) {
  if (ws.readyState !== WebSocket.OPEN || !chart) return;
  if (min != null && (!Number.isFinite(min) || !Number.isFinite(max) || min >= max)) return;
  if (zoomTimer !== null) clearTimeout(zoomTimer);
  const msg = {
    type: "zoom_request", msg_id: String(nextMsgId++),
    x_range: min == null ? null : [min, max],
  };
  // Invalidate older responses immediately, including during the debounce.
  latestFigureMsgID = msg.msg_id;
  zoomTimer = setTimeout(() => {
    zoomTimer = null;
    if (ws.readyState !== WebSocket.OPEN) return;
    figureRequests.set(msg.msg_id, { viewport: msg.x_range, zoom: true });
    ws.send(JSON.stringify(msg));
  }, min == null ? 0 : 100);
}

function cancelZoomRequests() {
  if (zoomTimer !== null) clearTimeout(zoomTimer);
  zoomTimer = null;
}

function handleControlMessage(msg) {
  switch (msg.type) {
    case "stream":
      log(msg.text, msg.name === "stderr" ? "stderr" : null);
      break;
    case "data_schema":
      if (msg.msg_id === importRequest) { importedSchema = msg; break; }
      renderData(msg);
      break;
    case "data_preview":
      renderData(msg);
      break;
    case "execute_result":
      log(msg.text);
      break;
    case "error":
      // traceback is absent for a Go-synthesized KernelError (a dead
      // connection has no Python traceback) -- confirmed as a real crash
      // when this went missing in a browser run, not assumed here.
      log(`${msg.ename}: ${msg.evalue}\n${(msg.traceback || []).join("")}`, "error");
      if (msg.msg_id === importRequest || msg.msg_id === dataPlotRequest) importStatus.textContent = `${msg.ename}: ${msg.evalue}`;
      break;
    case "figure":
      // Always consume the following payload, but don't display an obsolete
      // zoom response that was queued before a newer viewport settled.
      pendingFigure = msg; // its payload arrives as the next (binary) message
      break;
    case "execute_reply":
      figureRequests.delete(msg.msg_id);
      if (msg.msg_id === importRequest) {
        importRequest = null;
        if (msg.status === "ok" && importedSchema) {
          imported = true;
          for (const select of [dataXEl, dataYEl, document.getElementById("data-color")]) {
            select.replaceChildren(...importedSchema.columns.map(column => new Option(`${column.name} (${column.dtype})`, column.name)));
          }
          dataYEl.selectedIndex = Math.min(1, dataYEl.options.length - 1);
          document.getElementById("data-color").selectedIndex = Math.min(2, dataXEl.options.length - 1);
          importStatus.textContent = `Imported ${importedSchema.path} as df`;
          document.getElementById("column-controls").hidden = false;
        }
        updateDataControls();
      }
      if (msg.msg_id === dataPlotRequest) {
        dataPlotRequest = null;
        if (msg.status === "ok") importStatus.textContent = "Plot ready.";
        updateDataControls();
      }
      break;
    case "kernel_restarted":
      // internal/kernel auto-restarts a crashed subprocess (e.g. os._exit()
      // in user code) so the connection keeps working, but the new
      // interpreter has a fresh namespace -- say so, or a silently reset
      // session looks like a bug.
      log("[kernel restarted -- previous variables and the last plot are gone]", "error");
      cancelZoomRequests();
      latestFigureMsgID = null;
      figureRequests.clear();
      suppressScaleHook = true;
      if (chart) chart.destroy();
      chart = null;
      lastFigure = null;
      labels = { title: "", x: "", y: "" };
      for (const input of Object.values(labelInputs)) input.value = "";
      imported = false;
      selectedFile = null;
      for (const item of document.querySelectorAll("#file-list .tree-item")) item.setAttribute("aria-selected", "false");
      importedSchema = null;
      importRequest = dataPlotRequest = null;
      document.getElementById("column-controls").hidden = true;
      importStatus.textContent = "Kernel reset. Choose a file to import again.";
      exportEl.disabled = copyEl.disabled = true;
      updateDataControls();
      pendingFigure = null;
      resetZoomEl.disabled = true;
      document.getElementById("plot-empty").hidden = false;
      document.getElementById("plot-info").textContent = "Ready to plot";
      document.getElementById("heatmap-controls").hidden = true;
      document.getElementById("color-legend").replaceChildren();
      document.getElementById("data").replaceChildren();
      queueMicrotask(() => { suppressScaleHook = false; });
      break;
    default:
      log(`[unknown message type ${msg.type}]`, "error");
  }
}

function handleFigurePayload(buffer) {
  if (!pendingFigure) {
    log("[figure payload with no preceding header]", "error");
    return;
  }
  const fig = pendingFigure;
  pendingFigure = null;

  const [xBytes, yBytes] = fig.byte_lengths;
  const x = new Float32Array(buffer, 0, xBytes / 4);
  const y = new Float32Array(buffer, xBytes, yBytes / 4);
  const request = figureRequests.get(fig.msg_id);
  if (request && fig.msg_id === latestFigureMsgID) {
    if (fig.kind === "heatmap") {
      fig.color = new Float32Array(buffer, xBytes + yBytes, fig.byte_lengths[2] / 4);
      if (!request.zoom) heatmapCmap = fig.cmap;
    }
    render(fig, x, y, request.viewport);
  }
}

function render(fig, x, y, viewport = null) {
  lastFigure = { fig, x, y, viewport };
  document.getElementById("plot-empty").hidden = true;
  resetZoomEl.disabled = ws.readyState !== WebSocket.OPEN;
  exportEl.disabled = copyEl.disabled = false;
  document.getElementById("heatmap-controls").hidden = fig.kind !== "heatmap";
  document.getElementById("plot-cmap").value = heatmapCmap;
  renderColorLegend(fig);
  const isScatter = fig.kind === "scatter" || fig.kind === "heatmap";
  let xData = x, yData = y;
  const series = [{}, { label: fig.title || fig.kind, stroke: () => colors.plot, width: 2 }];

  if (isScatter) {
    series[1].paths = () => null; // points only, no connecting line
    series[1].points = { show: fig.kind !== "heatmap", size: 4 };
  } else if (fig.kind === "histogram") {
    // x holds bin edges (len(y)+1); uPlot needs equal-length series, so
    // use bin centers and a stepped line as a first approximation of bars.
    xData = new Float32Array(y.length);
    for (let i = 0; i < y.length; i++) xData[i] = (x[i] + x[i + 1]) / 2;
    series[1].paths = uPlot.paths.stepped({ align: 1 });
  }

  let tooltip = null; // created in the ready hook, scoped to this chart instance
  let xMin = Infinity, xMax = -Infinity;
  for (const value of xData) {
    if (Number.isFinite(value)) { xMin = Math.min(xMin, value); xMax = Math.max(xMax, value); }
  }
  const xBounds = viewport || paddedRange(xMin, xMax);

  const opts = {
    width: chartEl.clientWidth || 640,
    height: chartHeight(),
    axes: [0, 1].map(index => ({
      label: index === 0 ? labels.x : labels.y,
      stroke: () => colors.muted,
      grid: { stroke: () => colors.grid, width: 1 },
      ticks: { stroke: () => colors.grid },
      font: "11px system-ui",
    })),
    title: labels.title || fig.title || undefined,
    // uPlot defaults its x scale to time-series mode (x treated as Unix
    // seconds, axis labels formatted as clock times) -- our x is plain
    // numeric data, not necessarily time, so this must be turned off.
    scales: {
      x: { time: false, min: xBounds[0], max: xBounds[1] },
      y: { range: (_u, min, max) => paddedRange(min, max) },
    },
    cursor: isScatter ? {
      dataIdx: (u, seriesIdx) => seriesIdx === 1 ? nearestScatterPoint(u, xData, yData) : null,
    } : {},
    series,
    hooks: {
      // Following uPlot's own tooltip recipe (demos/tooltips.html):
      // create the tooltip in `over` (the cursor/interaction layer, which
      // uPlot's own CSS already positions absolutely, making it a valid
      // containing block for an absolutely-positioned child), then
      // reposition it in setCursor using valToPos so it tracks the
      // hovered data point rather than the raw mouse position.
      ready: [
        (u) => {
          tooltip = document.createElement("div");
          tooltip.className = "u-tooltip";
          u.over.appendChild(tooltip);
          // uPlot's built-in reset fits only the currently reduced data. After
          // a zoom round trip, ask the kernel for the full source range instead.
          u.over.addEventListener("dblclick", event => {
            event.preventDefault();
            event.stopImmediatePropagation();
            requestZoom(null, null);
          }, true);
        },
      ],
      setCursor: [
        (u) => {
          // uPlot fires setCursor at least once during construction --
          // before the ready hook below has created the tooltip -- so
          // this must tolerate running before tooltip exists.
          if (!tooltip) return;
          const idx = u.cursor.idxs[1];
          const xv = idx == null ? null : xData[idx];
          const yv = idx == null ? null : yData[idx];
          if (!u.series[1].show || u.cursor.left < 0 || u.cursor.top < 0 || idx == null || idx < 0 || idx >= xData.length || xv == null || yv == null || !Number.isFinite(xv) || !Number.isFinite(yv)) {
            tooltip.style.display = "none";
            return;
          }
          const left = u.valToPos(xv, "x");
          const top = u.valToPos(yv, "y");
          if (left < 0 || top < 0 || left > u.over.clientWidth || top > u.over.clientHeight) {
            tooltip.style.display = "none";
            return;
          }
          tooltip.textContent = `(${fmtNum(xv)}, ${fmtNum(yv)})` + (fig.kind === "heatmap" ? ` · ${fig.color_label}: ${fmtNum(fig.color[idx])}` : "");
          tooltip.dataset.index = String(idx);
          tooltip.style.display = "block";
          tooltip.style.left = Math.round(left) + "px";
          tooltip.style.top = Math.round(top) + "px";
        },
      ],
      // uPlot's default cursor.drag already does client-side drag-to-zoom
      // and dblclick-to-reset by rescaling the x axis; this hook reacts to
      // that rescale by asking the kernel to re-aggregate for the new
      // range, so zooming in reveals real detail from the source instead
      // of just stretching the already-reduced points on screen.
      setScale: [
        (u, key) => {
          if (suppressScaleHook || key !== "x") return;
          requestZoom(u.scales.x.min, u.scales.x.max);
        },
      ],
      draw: [(u) => {
        if (fig.kind === "heatmap" && u.series[1].show) drawHeatmap(u, fig, xData, yData);
        // Coordinates change on zoom/resize/theme redraw; wait for a fresh
        // cursor event before showing a tooltip anchored to the previous draw.
        if (tooltip) tooltip.style.display = "none";
        let visible = 0;
        if (u.series[1].show) {
          for (let i = 0; i < xData.length; i++) {
            if (xData[i] >= u.scales.x.min && xData[i] <= u.scales.x.max &&
                yData[i] >= u.scales.y.min && yData[i] <= u.scales.y.max) visible++;
          }
        }
        document.getElementById("plot-info").textContent = isScatter
          ? `${visible.toLocaleString()} in view · ${yData.length.toLocaleString()} sampled points`
          : `${visible.toLocaleString()} in view · ${yData.length.toLocaleString()} displayed ${fig.kind === "histogram" ? "bins" : "points"}`;
      }],
    },
  };

  // uPlot fires setScale synchronously both when constructing the new
  // chart's initial x/y scales AND, it turns out, when destroy()ing the
  // old one -- discovered as an infinite loop in a real browser: an
  // unsuppressed destroy-time setScale re-requested the old chart's own
  // range, whose response then got destroyed the same way, forever. So
  // the suppression must span both calls, not just construction. No user
  // interaction can happen inside this block -- JS is single-threaded and
  // nothing here awaits -- so this is race-free regardless.
  suppressScaleHook = true;
  if (chart) chart.destroy();
  chart = new uPlot(opts, [xData, yData], chartEl);
  const renderedChart = chart;
  // uPlot defers its own initial x/y scale commit to a microtask (it uses
  // queueMicrotask internally -- confirmed by tracing a real run, not
  // assumed), so resetting the flag synchronously here re-enables the
  // hook before that deferred setScale actually fires, causing an
  // infinite zoom_request loop. Queueing the reset as a microtask too
  // puts it behind uPlot's already-queued one in FIFO order, so it only
  // takes effect once uPlot's deferred setup has actually run.
  queueMicrotask(() => {
    if (chart === renderedChart) suppressScaleHook = false;
  });
}

// Scatter hover follows distance in both screen dimensions. The same index
// drives uPlot's highlight/legend and our tooltip, including duplicate x values.
function nearestScatterPoint(u, x, y) {
  const { left, top } = u.cursor;
  if (!u.series[1].show || left < 0 || top < 0 || left > u.over.clientWidth || top > u.over.clientHeight) return null;
  let nearest = null;
  let distance = 12 * 12;
  for (let i = 0; i < x.length; i++) {
    if (x[i] < u.scales.x.min || x[i] > u.scales.x.max || y[i] < u.scales.y.min || y[i] > u.scales.y.max) continue;
    const dx = u.valToPos(x[i], "x") - left;
    if (Math.abs(dx) > 12) continue;
    const dy = u.valToPos(y[i], "y") - top;
    const d = dx * dx + dy * dy;
    if (d < distance) { distance = d; nearest = i; }
  }
  return nearest;
}

function paddedRange(min, max) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1];
  // Constant series still need room on both sides, including a point at zero.
  const padding = 0.05 * (max > min ? max - min : Math.abs(min) || 1);
  return [min - padding, max + padding];
}

function fmtNum(n) {
  // float32 round-trips as things like 4.000000476837158; round to 6
  // significant digits and drop trailing zeros for a readable label.
  return Number(n.toPrecision(6)).toString();
}

function log(text, cls) {
  if (cls) {
    const span = document.createElement("span");
    span.className = cls;
    span.textContent = text;
    outputEl.appendChild(span);
  } else {
    outputEl.appendChild(document.createTextNode(text));
  }
}

// Values are text nodes, including column names and file paths from user data.
function renderData(msg) {
  const container = document.getElementById("data");
  container.replaceChildren();
  const caption = document.createElement("p");
  caption.textContent = msg.type === "data_schema"
    ? `Schema: ${msg.path}`
    : `Preview: ${(msg.rows || []).length} rows${msg.has_more ? ` (limited to ${msg.limit}; more rows available)` : ""}`;
  container.appendChild(caption);
  const table = document.createElement("table");
  const header = document.createElement("tr");
  for (const column of msg.columns || []) {
    const cell = document.createElement("th");
    cell.textContent = `${column.name} (${column.dtype})`;
    header.appendChild(cell);
  }
  table.appendChild(header);
  for (const row of msg.rows || []) {
    const tr = document.createElement("tr");
    for (const value of row) {
      const cell = document.createElement("td");
      cell.textContent = value;
      tr.appendChild(cell);
    }
    table.appendChild(tr);
  }
  container.appendChild(table);
}

function setConnection(connected) {
  const status = document.getElementById("connection");
  status.textContent = connected ? "Kernel connected" : "Disconnected";
  status.dataset.connected = String(connected);
  runEl.disabled = !connected;
  stopEl.disabled = !connected;
  document.getElementById("data-stop").disabled = !connected;
  resetZoomEl.disabled = !connected || !chart;
  updateDataControls();
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const style = getComputedStyle(document.documentElement);
  colors = Object.fromEntries(["plot", "muted", "grid"].map(key => [key, style.getPropertyValue(`--${key}`).trim()]));
  themeEl.textContent = theme === "dark" ? "☼ Light mode" : "☾ Dark mode";
  themeEl.setAttribute("aria-pressed", String(theme === "dark"));
  // Redraw the existing canvas so data, scale, and viewport stay intact.
  if (chart) chart.redraw(true, true);
}

themeEl.addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  followsSystemTheme = false;
  try { localStorage.setItem("loupe-theme", theme); } catch { /* Still switch without persistence. */ }
  applyTheme(theme);
});
systemTheme.addEventListener("change", event => {
  if (followsSystemTheme) applyTheme(event.matches ? "dark" : "light");
});
applyTheme(document.documentElement.dataset.theme);

new ResizeObserver(() => {
  if (!chart || (chart.width === chartEl.clientWidth && chart.height === chartHeight())) return;
  suppressScaleHook = true;
  chart.setSize({ width: chartEl.clientWidth, height: chartHeight() });
  queueMicrotask(() => { suppressScaleHook = false; });
}).observe(chartEl);

function chartHeight() {
  // uPlot's requested height excludes its title and legend. Reserve room for
  // both inside the fixed plot panel so they cannot overlap the footer.
  return Math.max(180, chartEl.clientHeight - 64);
}

for (const [key, input] of Object.entries(labelInputs)) {
  input.addEventListener("input", () => {
    labels[key] = input.value;
    if (chart && lastFigure) render(lastFigure.fig, lastFigure.x, lastFigure.y, [chart.scales.x.min, chart.scales.x.max]);
  });
}

// uPlot draws axes on its canvas, but its title is a DOM element. Compose
// that title explicitly so downloads include it without a DOM rasterizer.
function exportCanvas() {
  if (!chart) return Promise.reject(new Error("Create a plot first."));
  const source = chart.ctx.canvas;
  const ratio = source.width / chart.width;
  const title = labels.title || lastFigure.fig.title || "";
  const heading = title ? 36 * ratio : 0;
  const legend = lastFigure.fig.kind === "heatmap" ? colorLegendEntries(lastFigure.fig) : [];
  const legendColumns = Math.max(1, Math.floor(chart.width / 180));
  const legendHeight = lastFigure.fig.kind === "heatmap" ? (32 + 24 * Math.ceil(legend.length / legendColumns)) * ratio : 0;
  const canvas = document.createElement("canvas");
  canvas.width = source.width;
  canvas.height = source.height + heading + legendHeight;
  const ctx = canvas.getContext("2d");
  const style = getComputedStyle(document.documentElement);
  ctx.fillStyle = style.getPropertyValue("--surface").trim();
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(source, 0, heading);
  if (title) {
    ctx.fillStyle = style.getPropertyValue("--text").trim();
    ctx.font = `600 ${14 * ratio}px system-ui`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(title, canvas.width / 2, heading / 2, canvas.width - 24 * ratio);
  }
  if (legendHeight) {
    ctx.fillStyle = style.getPropertyValue("--text").trim();
    ctx.font = `${11 * ratio}px system-ui`;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    const top = heading + source.height;
    ctx.fillText(`${lastFigure.fig.color_label} · ${heatmapCmap}`, 12*ratio, top+14*ratio, canvas.width-24*ratio);
    legend.forEach((entry, i) => {
      const left = (12 + (i % legendColumns) * chart.width / legendColumns) * ratio;
      const y = top + (38 + Math.floor(i / legendColumns) * 24) * ratio;
      ctx.fillStyle = entry.color;
      ctx.fillRect(left, y-5*ratio, 14*ratio, 10*ratio);
      ctx.fillStyle = style.getPropertyValue("--text").trim();
      ctx.fillText(entry.text, left+20*ratio, y, (chart.width/legendColumns-36)*ratio);
    });
  }
  return new Promise((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error("PNG export failed.")), "image/png"));
}

exportEl.addEventListener("click", async () => {
  const status = document.getElementById("export-status");
  try {
    const blob = await exportCanvas();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "loupe-plot.png";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status.textContent = "PNG downloaded.";
  } catch (error) { status.textContent = error.message; }
});
copyEl.addEventListener("click", async () => {
  const status = document.getElementById("export-status");
  try {
    if (!navigator.clipboard?.write || typeof ClipboardItem === "undefined") throw new Error("Image copy is unavailable in this browser. Use Export PNG.");
    await navigator.clipboard.write([new ClipboardItem({ "image/png": exportCanvas() })]);
    status.textContent = "Image copied.";
  } catch (error) { status.textContent = `Could not copy image: ${error.message}`; }
});

function selectTab(name) {
  for (const tab of ["repl", "data"]) {
    const selected = tab === name;
    const button = document.getElementById(`${tab}-tab`);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    document.getElementById(`${tab}-panel`).hidden = !selected;
  }
  if (name === "data" && !directory && !fileRequest) browse("");
}
for (const name of ["repl", "data"]) {
  const tab = document.getElementById(`${name}-tab`);
  tab.addEventListener("click", () => selectTab(name));
  tab.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? "repl" : event.key === "End" ? "data" : name === "repl" ? "data" : "repl";
    selectTab(next);
    document.getElementById(`${next}-tab`).focus();
  });
}

const treeRequests = new Set();
let selectedFile = null;

function fileIcon(kind) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.6");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add(kind === "chevron" ? "tree-chevron" : "tree-icon");
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d", kind === "chevron" ? "m9 5 7 7-7 7" : kind === "folder"
    ? "M3 7V5h6l2 2h10v13H3Z M3 10h18"
    : "M5 3h9l5 5v13H5Z M14 3v6h5 M8 13h8 M8 17h8 M11 11v8");
  svg.appendChild(path);
  return svg;
}

function focusTreeItem(item) {
  if (!item) return;
  for (const node of document.querySelectorAll("#file-list .tree-item")) node.tabIndex = -1;
  item.tabIndex = 0;
  item.focus();
}

function treeMessage(group, text) {
  const item = document.createElement("li");
  item.setAttribute("role", "none");
  item.className = "tree-message";
  item.textContent = text;
  group.replaceChildren(item);
}

async function directoryEntries(path, signal) {
  const response = await fetch(`/fs?path=${encodeURIComponent(path)}`, { signal });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Cannot list directory.");
  return result;
}

function populateTree(group, result) {
  group.replaceChildren();
  for (const entry of result.entries) {
    const item = document.createElement("li");
    item.className = "tree-item";
    item.setAttribute("role", "treeitem");
    item.setAttribute("aria-label", entry.name);
    item.tabIndex = -1;
    item.dataset.file = String(!entry.is_dir);
    item.dataset.path = `${result.path.replace(/\/$/, "")}/${entry.name}`;
    item.setAttribute("aria-selected", String(item.dataset.path === selectedFile));
    const row = document.createElement("div");
    row.className = "tree-row";
    row.title = item.dataset.path;
    const chevron = fileIcon("chevron");
    if (!entry.is_dir) chevron.style.visibility = "hidden";
    row.append(chevron, fileIcon(entry.is_dir ? "folder" : "file"));
    const name = document.createElement("span");
    name.className = "tree-name";
    name.textContent = entry.name;
    row.appendChild(name);
    if (!entry.is_dir) {
      const meta = document.createElement("span");
      meta.className = "tree-meta";
      const size = entry.size < 1024 ? `${entry.size} B` : entry.size < 1024 ** 2
        ? `${(entry.size / 1024).toFixed(1)} KB` : `${(entry.size / 1024 ** 2).toFixed(1)} MB`;
      meta.textContent = `${entry.name.split(".").at(-1).toUpperCase()} · ${size}`;
      row.appendChild(meta);
    }
    item.appendChild(row);
    const children = document.createElement("ul");
    children.setAttribute("role", "group");
    children.hidden = true;
    let loaded = false;
    let loading = false;
    if (entry.is_dir) {
      item.setAttribute("aria-expanded", "false");
      item.appendChild(children);
    }
    const toggle = async () => {
      const expanded = item.getAttribute("aria-expanded") !== "true";
      item.setAttribute("aria-expanded", String(expanded));
      children.hidden = !expanded;
      if (!expanded || loaded || loading) return;
      loading = true;
      item.setAttribute("aria-busy", "true");
      treeMessage(children, "Loading…");
      const request = new AbortController();
      treeRequests.add(request);
      try {
        const listing = await directoryEntries(item.dataset.path, request.signal);
        if (!item.isConnected || request.signal.aborted) return;
        populateTree(children, listing);
        loaded = true;
      } catch (error) {
        if (error.name !== "AbortError" && item.isConnected) treeMessage(children, `${error.message}. Collapse and expand to retry.`);
      } finally {
        treeRequests.delete(request);
        loading = false;
        item.removeAttribute("aria-busy");
      }
    };
    const activate = () => {
      focusTreeItem(item);
      if (entry.is_dir) { toggle(); return; }
      if (item.getAttribute("aria-disabled") === "true") return;
      selectedFile = item.dataset.path;
      for (const node of document.querySelectorAll("#file-list .tree-item")) node.setAttribute("aria-selected", String(node.dataset.path === selectedFile));
      importFile(selectedFile);
    };
    row.addEventListener("click", activate);
    item.addEventListener("keydown", event => {
      if (event.target !== item) return;
      const visible = [...document.querySelectorAll("#file-list .tree-item")].filter(node => node.getClientRects().length);
      const index = visible.indexOf(item);
      switch (event.key) {
        case "ArrowDown": focusTreeItem(visible[index + 1]); break;
        case "ArrowUp": focusTreeItem(visible[index - 1]); break;
        case "Home": focusTreeItem(visible[0]); break;
        case "End": focusTreeItem(visible.at(-1)); break;
        case "ArrowRight":
          if (entry.is_dir) {
            if (children.hidden) toggle(); else focusTreeItem(children.querySelector(".tree-item"));
          }
          break;
        case "ArrowLeft":
          if (entry.is_dir && !children.hidden) toggle(); else focusTreeItem(item.parentElement.closest(".tree-item"));
          break;
        case "Enter": case " ": activate(); break;
        default: return;
      }
      event.preventDefault();
      event.stopPropagation();
    });
    group.appendChild(item);
  }
  if (!result.entries.length) treeMessage(group, "No supported files or folders.");
  updateDataControls();
}

async function browse(path) {
  fileRequest?.abort();
  for (const request of treeRequests) request.abort();
  treeRequests.clear();
  const request = new AbortController();
  fileRequest = request;
  const status = document.getElementById("file-status");
  const list = document.getElementById("file-list");
  status.textContent = "Loading directory…";
  list.setAttribute("aria-busy", "true");
  list.replaceChildren();
  try {
    const result = await directoryEntries(path, request.signal);
    if (fileRequest !== request) return;
    directory = result;
    document.getElementById("file-path").value = result.path;
    document.getElementById("file-up").disabled = result.parent === result.path;
    populateTree(list, result);
    const first = list.querySelector(".tree-item");
    if (first) first.tabIndex = 0;
    status.textContent = `${result.entries.length} items in ${result.path}`;
  } catch (error) {
    if (fileRequest === request && error.name !== "AbortError") status.textContent = error.message;
  } finally {
    if (fileRequest === request) {
      fileRequest = null;
      list.removeAttribute("aria-busy");
    }
  }
}

document.getElementById("file-navigation").addEventListener("submit", event => {
  event.preventDefault();
  browse(document.getElementById("file-path").value);
});
document.getElementById("file-refresh").addEventListener("click", () => browse(directory?.path || ""));
document.getElementById("file-up").addEventListener("click", () => { if (directory) browse(directory.parent); });
document.getElementById("data-stop").addEventListener("click", () => stopEl.click());
dataKindEl.addEventListener("change", () => {
  document.getElementById("data-y-label").hidden = dataKindEl.value === "histogram";
  for (const element of document.querySelectorAll(".data-heatmap")) element.hidden = dataKindEl.value !== "heatmap";
});

function updateDataControls() {
  const busy = importRequest !== null || dataPlotRequest !== null;
  const connected = ws.readyState === WebSocket.OPEN;
  dataPlotEl.disabled = !connected || busy || !imported || !dataXEl.options.length;
  for (const item of document.querySelectorAll('#file-list .tree-item[data-file="true"]')) item.setAttribute("aria-disabled", String(!connected || busy));
}

function importFile(path) {
  if (ws.readyState !== WebSocket.OPEN || importRequest || dataPlotRequest) return;
  imported = false;
  importedSchema = null;
  document.getElementById("column-controls").hidden = true;
  importStatus.textContent = `Importing ${path}…`;
  importRequest = String(nextMsgId++);
  updateDataControls();
  // JSON string literals are also Python string literals; paths and column
  // names never become executable Python syntax.
  ws.send(JSON.stringify({ type: "execute_request", msg_id: importRequest, code: `df = scan(${JSON.stringify(path)})` }));
}

dataPlotEl.addEventListener("click", () => {
  if (dataPlotEl.disabled) return;
  const kind = dataKindEl.value;
  const x = dataXEl.value, y = dataYEl.value;
  const histogram = kind === "histogram";
  labels.x = x;
  labels.y = histogram ? "Count" : y;
  labelInputs.x.value = labels.x;
  labelInputs.y.value = labels.y;
  const title = histogram ? x : `${y} vs ${x}`;
  let args = histogram ? JSON.stringify(x) : `${JSON.stringify(x)}, ${JSON.stringify(y)}`;
  if (kind === "heatmap") {
    const bins = Number(document.getElementById("data-bins").value);
    if (!Number.isInteger(bins) || bins < 2 || bins > 16) { importStatus.textContent = "Buckets must be between 2 and 16."; return; }
    args += `, ${JSON.stringify(document.getElementById("data-color").value)}, cmap=${JSON.stringify(document.getElementById("data-cmap").value)}, bins=${bins}`;
  }
  cancelZoomRequests();
  dataPlotRequest = String(nextMsgId++);
  latestFigureMsgID = dataPlotRequest;
  figureRequests.set(dataPlotRequest, { viewport: null });
  updateDataControls();
  importStatus.textContent = "Plotting…";
  ws.send(JSON.stringify({ type: "execute_request", msg_id: dataPlotRequest, code: `${kind}(${args}, data=df, title=${JSON.stringify(title)})` }));
});

function bucketColors(fig) {
  const palette = COLORMAPS[heatmapCmap] || COLORMAPS.viridis;
  const count = fig.color_range?.[0] === fig.color_range?.[1] ? 1 : fig.color_bins;
  return Array.from({length:count}, (_, i) => palette[Math.round(count === 1 ? 7 : i * 15 / (count - 1))]);
}

function colorBucket(fig, value) {
  const [lo, hi] = fig.color_range;
  return hi === lo ? 0 : Math.max(0, Math.min(fig.color_bins - 1, Math.floor((value - lo) / (hi - lo) * fig.color_bins)));
}

function colorLegendEntries(fig) {
  if (!fig.color_range) return [];
  const [lo, hi] = fig.color_range;
  return bucketColors(fig).map((color, i, colors) => {
    const start = lo + (hi-lo) * i / colors.length;
    const end = lo + (hi-lo) * (i+1) / colors.length;
    return {color, text:lo === hi ? lo.toExponential(3) : `${start.toExponential(3)}–${end.toExponential(3)}`};
  });
}

function renderColorLegend(fig) {
  const legend = document.getElementById("color-legend");
  legend.replaceChildren();
  if (fig.kind !== "heatmap") return;
  const title = document.createElement("strong");
  title.textContent = `${fig.color_label} · ${heatmapCmap}${fig.color_range ? "" : " · No finite data"}`;
  legend.appendChild(title);
  for (const entry of colorLegendEntries(fig)) {
    const bucket = document.createElement("span");
    bucket.className = "color-bucket";
    const swatch = document.createElement("span");
    swatch.className = "color-swatch";
    swatch.style.backgroundColor = entry.color;
    bucket.append(swatch, document.createTextNode(entry.text));
    legend.appendChild(bucket);
  }
  legend.title = "Equal-width value buckets. Upper endpoints belong to the next bucket, except the maximum. Scale stays fixed on zoom.";
}

function drawHeatmap(u, fig, x, y) {
  if (!fig.color_range) return;
  const ctx = u.ctx;
  const palette = bucketColors(fig);
  const radius = 2.5 * u.ctx.canvas.width / u.width;
  ctx.save();
  ctx.beginPath();
  ctx.rect(u.bbox.left, u.bbox.top, u.bbox.width, u.bbox.height);
  ctx.clip();
  // Keep source order for overlapping points, matching scatter semantics.
  for (let i=0; i<x.length; i++) {
    if (x[i]<u.scales.x.min || x[i]>u.scales.x.max || y[i]<u.scales.y.min || y[i]>u.scales.y.max) continue;
    ctx.fillStyle = palette[colorBucket(fig, fig.color[i])];
    ctx.beginPath();
    ctx.arc(u.valToPos(x[i], "x", true), u.valToPos(y[i], "y", true), radius, 0, 2*Math.PI);
    ctx.fill();
  }
  ctx.restore();
}

for (const id of ["plot-cmap", "data-cmap"]) {
  const select = document.getElementById(id);
  for (const name of Object.keys(COLORMAPS)) select.add(new Option(name, name));
}
document.getElementById("plot-cmap").addEventListener("change", event => {
  heatmapCmap = event.target.value;
  if (chart && lastFigure?.fig.kind === "heatmap") {
    renderColorLegend(lastFigure.fig);
    chart.redraw(true, true);
  }
});
