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
    figureRequests.set(msg.msg_id, { viewport: msg.x_range });
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
      break;
    case "figure":
      // Always consume the following payload, but don't display an obsolete
      // zoom response that was queued before a newer viewport settled.
      pendingFigure = msg; // its payload arrives as the next (binary) message
      break;
    case "execute_reply":
      figureRequests.delete(msg.msg_id);
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
      pendingFigure = null;
      resetZoomEl.disabled = true;
      document.getElementById("plot-empty").hidden = false;
      document.getElementById("plot-info").textContent = "Ready to plot";
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
  if (request && fig.msg_id === latestFigureMsgID) render(fig, x, y, request.viewport);
}

function render(fig, x, y, viewport = null) {
  document.getElementById("plot-empty").hidden = true;
  resetZoomEl.disabled = ws.readyState !== WebSocket.OPEN;
  let xData = x, yData = y;
  const series = [{}, { label: fig.title || fig.kind, stroke: () => colors.plot, width: 2 }];

  if (fig.kind === "scatter") {
    series[1].paths = () => null; // points only, no connecting line
    series[1].points = { show: true, size: 4 };
  } else if (fig.kind === "histogram") {
    // x holds bin edges (len(y)+1); uPlot needs equal-length series, so
    // use bin centers and a stepped line as a first approximation of bars.
    xData = new Float32Array(y.length);
    for (let i = 0; i < y.length; i++) xData[i] = (x[i] + x[i + 1]) / 2;
    series[1].paths = uPlot.paths.stepped({ align: 1 });
  }

  let tooltip = null; // created in the ready hook, scoped to this chart instance

  const opts = {
    width: chartEl.clientWidth || 640,
    height: chartHeight(),
    axes: [0, 1].map(() => ({
      stroke: () => colors.muted,
      grid: { stroke: () => colors.grid, width: 1 },
      ticks: { stroke: () => colors.grid },
      font: "11px system-ui",
    })),
    title: fig.title || undefined,
    // uPlot defaults its x scale to time-series mode (x treated as Unix
    // seconds, axis labels formatted as clock times) -- our x is plain
    // numeric data, not necessarily time, so this must be turned off.
    scales: { x: { time: false, ...(viewport ? { min: viewport[0], max: viewport[1] } : {}) } },
    cursor: fig.kind === "scatter" ? {
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
          tooltip.textContent = `(${fmtNum(xv)}, ${fmtNum(yv)})`;
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
        document.getElementById("plot-info").textContent = fig.kind === "scatter"
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
  // uPlot defers its own initial x/y scale commit to a microtask (it uses
  // queueMicrotask internally -- confirmed by tracing a real run, not
  // assumed), so resetting the flag synchronously here re-enables the
  // hook before that deferred setScale actually fires, causing an
  // infinite zoom_request loop. Queueing the reset as a microtask too
  // puts it behind uPlot's already-queued one in FIFO order, so it only
  // takes effect once uPlot's deferred setup has actually run.
  queueMicrotask(() => {
    suppressScaleHook = false;
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
  resetZoomEl.disabled = !connected || !chart;
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
