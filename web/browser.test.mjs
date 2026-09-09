// Real Chromium smoke test, using Node's test runner and built-in CDP WebSocket.
// No npm packages or downloaded browser are required.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { once } from "node:events";
import { setTimeout as delay } from "node:timers/promises";
import test from "node:test";

function endpoint(process, stream, pattern) {
  return new Promise((resolve, reject) => {
    let output = "";
    process[stream].on("data", data => {
      output += data;
      const match = output.match(pattern);
      if (match) resolve(match[1]);
    });
    process.once("error", reject);
    process.once("exit", code => reject(new Error(`process exited ${code}: ${output}`)));
  });
}

async function waitFor(check, description) {
  const end = Date.now() + 10_000;
  while (Date.now() < end) {
    if (await check()) return;
    await delay(50);
  }
  throw new Error(`Timed out: ${description}`);
}

test("offline UI, bounded ingest preview, plot, and infinite-loop Stop", { timeout: 60_000 }, async t => {
  const directory = await mkdtemp(join(tmpdir(), "loupe-browser-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const binary = process.env.LOUPE_TEST_BINARY ? resolve(process.env.LOUPE_TEST_BINARY) : join(directory, "loupe");
  if (!process.env.LOUPE_TEST_BINARY) {
    const build = spawn("go", ["build", "-o", binary, "."], { stdio: "inherit" });
    assert.equal((await once(build, "exit"))[0], 0);
  }
  const host = spawn(binary, process.env.LOUPE_TEST_BINARY ? [] : ["-python", resolve("python/.venv/bin/python3")], {
    cwd: directory, env: { ...process.env, XDG_CACHE_HOME: join(directory, "cache") },
  });
  t.after(async () => { if (host.exitCode === null) { host.kill("SIGTERM"); await once(host, "exit"); } });
  const url = await endpoint(host, "stdout", /serving on (http:\/\/[^\s]+)/);
  const browser = spawn(process.env.CHROMIUM || "chromium", [
    "--headless", "--no-sandbox", "--disable-gpu", "--disable-background-networking",
    "--no-first-run", "--remote-debugging-port=0", `--user-data-dir=${join(directory, "browser")}`,
    "about:blank",
  ]);
  t.after(async () => { if (browser.exitCode === null) { browser.kill("SIGTERM"); await once(browser, "exit"); } });
  const debuggerURL = await endpoint(browser, "stderr", /DevTools listening on (ws:\/\/[^\s]+)/);
  const socket = new WebSocket(debuggerURL);
  await once(socket, "open");
  t.after(() => socket.close());
  let id = 0;
  const pending = new Map();
  const exceptions = [];
  const requests = [];
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const handler = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) handler.reject(new Error(JSON.stringify(message.error)));
      else handler.resolve(message.result);
    }
    if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params);
    if (message.method === "Network.requestWillBeSent") requests.push(message.params.request.url);
  });
  function send(method, params = {}, sessionId) {
    return new Promise((resolve, reject) => {
      const requestID = ++id;
      pending.set(requestID, { resolve, reject });
      socket.send(JSON.stringify({ id: requestID, method, params, sessionId }));
    });
  }
  const { targetId } = await send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
  const command = (method, params) => send(method, params, sessionId);
  const evaluate = async expression => {
    const result = await command("Runtime.evaluate", { expression, returnByValue: true });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  await command("Runtime.enable");
  await command("Network.enable");
  await command("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
  await command("Emulation.setDeviceMetricsOverride", { width: 1280, height: 1000, deviceScaleFactor: 1, mobile: false });
  await command("Page.navigate", { url });
  await waitFor(() => evaluate("typeof run === 'function' && ws.readyState === WebSocket.OPEN"), "UI connection");
  assert.equal(await evaluate("document.documentElement.dataset.theme"), "dark");
  // Picking an example fills the editor and resets the picker to its placeholder.
  assert.ok(await evaluate("examplesEl.options.length > 1"), "examples not populated");
  await evaluate("codeEl.value = ''; examplesEl.selectedIndex = 1; examplesEl.dispatchEvent(new Event('change'))");
  assert.ok(await evaluate("codeEl.value.includes('plot(') && examplesEl.selectedIndex === 0"), "example did not fill the editor");
  assert.ok(await evaluate("document.getElementById('plot-panel').getBoundingClientRect().bottom < document.getElementById('repl-panel').getBoundingClientRect().top"));
  await evaluate("codeEl.value = 'pl.LazyFrame({\"x\": range(1000)})'; runEl.click()");
  await waitFor(() => evaluate("document.querySelectorAll('#data td').length === 20"), "capped preview");
  assert.match(await evaluate("document.getElementById('data').textContent"), /limited to 20/);
  await evaluate("codeEl.value = 'plot(np.arange(1000), np.sin(np.arange(1000) / 70), title=\"Signal\")'; runEl.click()");
  await waitFor(() => evaluate("chart !== null"), "tier 1 plot");
  const sample = 'x = np.random.default_rng(4).normal(size=100_000)\ny = x * 0.5 + np.random.default_rng(5).normal(size=100_000)\nscatter(x, y, max_points=8000, title="Scatter sample")';
  await evaluate(`codeEl.value = ${JSON.stringify(sample)}; runEl.click()`);
  await waitFor(() => evaluate("chart?.series[1].label === 'Scatter sample'"), "exact user scatter sample");
  assert.ok(await evaluate("chart.data[0].every((x, i, xs) => i === 0 || x >= xs[i-1])"), "scatter wire data must be sorted, including in release builds");

  async function checkScatter() {
    assert.ok(await evaluate("chart.data[0].length > 0"), "scatter unexpectedly empty");
    const counts = await evaluate(`(() => {
      const n = chart.data[0].filter((x,i) => x >= chart.scales.x.min && x <= chart.scales.x.max && chart.data[1][i] >= chart.scales.y.min && chart.data[1][i] <= chart.scales.y.max).length;
      return { expected: n.toLocaleString() + ' in view · ' + chart.data[0].length.toLocaleString() + ' sampled points', actual: document.getElementById('plot-info').textContent };
    })()`);
    assert.equal(counts.actual, counts.expected);
    const target = await evaluate(`(() => {
      const r = chart.over.getBoundingClientRect();
      const i = chart.data[0].findIndex((x,i) => {
        const px = chart.valToPos(x,'x'), py = chart.valToPos(chart.data[1][i],'y');
        return px > r.width*0.4 && px < r.width*0.6 && py > r.height*0.3 && py < r.height*0.7;
      });
      if (i < 0) throw new Error('No central sample point');
      const x = chart.valToPos(chart.data[0][i], 'x'), y = chart.valToPos(chart.data[1][i], 'y');
      // Verify the actual canvas has a plotted point, not only data in memory.
      const cx = chart.valToPos(chart.data[0][i], 'x', true), cy = chart.valToPos(chart.data[1][i], 'y', true);
      const pixels = chart.ctx.getImageData(Math.round(cx)-3, Math.round(cy)-3, 7, 7).data;
      let painted = false;
      for (let p=0; p<pixels.length; p+=4) if (pixels[p+1] > pixels[p]+25 && pixels[p+1] > pixels[p+2]+5) painted = true;
      return {x:r.x+x, y:r.y+y, i, left:x, top:y, painted};
    })()`);
    assert.ok(target.painted, "sample point missing from canvas");
    await command("Input.dispatchMouseEvent", { type: "mouseMoved", x: target.x, y: target.y });
    await waitFor(() => evaluate("document.querySelector('.u-tooltip')?.style.display === 'block'"), "scatter point tooltip");
    const hover = await evaluate(`(() => {
      const tip = document.querySelector('.u-tooltip');
      const index = Number(tip.dataset.index);
      const marker = chart.over.querySelector('.u-cursor-pt').getBoundingClientRect();
      const area = chart.over.getBoundingClientRect();
      return {index, left: parseFloat(tip.style.left), top: parseFloat(tip.style.top),
        dataLeft: chart.valToPos(chart.data[0][index], 'x'), dataTop: chart.valToPos(chart.data[1][index], 'y'),
        markerX: marker.x + marker.width/2 - area.x, markerY: marker.y + marker.height/2 - area.y};
    })()`);
    // Browser mouse coordinates are rounded; dense samples may have another
    // point closer to that pixel. It must still be within two pixels in 2D.
    assert.ok(Math.hypot(hover.dataLeft-target.left, hover.dataTop-target.top) <= 2, "tooltip did not select the hovered point in both dimensions");
    assert.ok(Math.abs(hover.left-hover.dataLeft) <= 1 && Math.abs(hover.top-hover.dataTop) <= 1, "tooltip detached from data point");
    assert.ok(Math.abs(hover.markerX-hover.dataLeft) <= 1.5 && Math.abs(hover.markerY-hover.dataTop) <= 1.5, "highlight detached from tooltip point");
    if (process.env.LOUPE_SCREENSHOT_DIR) {
      const shot = await command("Page.captureScreenshot", { format: "png" });
      await writeFile(join(process.env.LOUPE_SCREENSHOT_DIR, "loupe-scatter.png"), Buffer.from(shot.data, "base64"));
    }
    await command("Input.dispatchMouseEvent", { type: "mouseMoved", x: 5, y: 5 });
    await waitFor(() => evaluate("document.querySelector('.u-tooltip').style.display === 'none'"), "tooltip hidden outside plot");
  }
  await checkScatter();
  for (let attempt = 0; attempt < 3; attempt++) {
    const box = await evaluate("(() => { const r = chart.over.getBoundingClientRect(); return {x:r.x, y:r.y, width:r.width, height:r.height}; })()");
    const expected = await evaluate("[chart.posToVal(chart.over.clientWidth*0.3, 'x'), chart.posToVal(chart.over.clientWidth*0.7, 'x')]");
    const start = box.x + box.width*0.3, end = box.x + box.width*0.7, y = box.y + box.height*0.5;
    await command("Input.dispatchMouseEvent", {type:"mouseMoved", x:start, y});
    await command("Input.dispatchMouseEvent", {type:"mousePressed", x:start, y, button:"left", buttons:1, clickCount:1});
    for (let step=1; step<=5; step++) await command("Input.dispatchMouseEvent", {type:"mouseMoved", x:start+(end-start)*step/5, y, buttons:1});
    await command("Input.dispatchMouseEvent", {type:"mouseReleased", x:end, y, button:"left", clickCount:1});
    await waitFor(() => evaluate("zoomTimer === null && figureRequests.size === 0"), "scatter drag settled");
    const actual = await evaluate("[chart.scales.x.min, chart.scales.x.max]");
    assert.ok(Math.abs(actual[0]-expected[0]) < (expected[1]-expected[0])*0.01 && Math.abs(actual[1]-expected[1]) < (expected[1]-expected[0])*0.01, "zoom response changed the selected viewport");
    await checkScatter();
  }
  // Hold the kernel busy so an old zoom reply arrives after a newer zoom was
  // requested. This tests real queued responses, not just coalesced timers.
  await evaluate(`codeEl.value = "import time; time.sleep(0.5)"; runEl.click(); requestZoom(-2, -1)`);
  await waitFor(() => evaluate("zoomTimer === null && figureRequests.size >= 2"), "old zoom queued behind execution");
  await evaluate("requestZoom(1, 2)");
  await waitFor(() => evaluate("zoomTimer === null && figureRequests.size === 0 && chart.scales.x.min === 1 && chart.scales.x.max === 2"), "latest queued zoom wins");
  assert.ok(await evaluate("chart.data[0].length > 0 && chart.data[0].every(x => x >= 1 && x <= 2)"));
  await evaluate("codeEl.value = 'plot(np.arange(1000), np.sin(np.arange(1000) / 70), title=\"Signal\")'; runEl.click()");
  await waitFor(() => evaluate("chart.data[0][0] === 0 && chart.data[0].at(-1) === 999"), "line plot restored");
  await evaluate("requestZoom(100, 200)");
  await waitFor(() => evaluate("chart.data[0][0] === 100 && chart.data[0].at(-1) === 200"), "zoom reduction");
  const zoom = await evaluate("[chart.scales.x.min, chart.scales.x.max]");
  await evaluate("themeEl.click()");
  assert.equal(await evaluate("document.documentElement.dataset.theme"), "light");
  assert.deepEqual(await evaluate("[chart.scales.x.min, chart.scales.x.max]"), zoom);
  assert.equal(await evaluate("chart.axes[0].stroke(chart, 0)"), "#61717f");
  // Exercise actual double-click input on the reduced plot, not the Reset button.
  const position = await evaluate("(() => { const r = chart.over.getBoundingClientRect(); return {x:r.x+r.width/2, y:r.y+r.height/2}; })()");
  await command("Input.dispatchMouseEvent", { type: "mousePressed", ...position, button: "left", clickCount: 2 });
  await command("Input.dispatchMouseEvent", { type: "mouseReleased", ...position, button: "left", clickCount: 2 });
  await waitFor(() => evaluate("chart.data[0][0] === 0 && chart.data[0].at(-1) === 999"), "double-click fits full source");
  if (process.env.LOUPE_SCREENSHOT_DIR) {
    const shot = await command("Page.captureScreenshot", { format: "png" });
    await writeFile(join(process.env.LOUPE_SCREENSHOT_DIR, "loupe-light.png"), Buffer.from(shot.data, "base64"));
  }
  await evaluate("themeEl.click()");
  assert.equal(await evaluate("chart.axes[0].stroke(chart, 0)"), "#98a9b9");
  if (process.env.LOUPE_SCREENSHOT_DIR) {
    const shot = await command("Page.captureScreenshot", { format: "png" });
    await writeFile(join(process.env.LOUPE_SCREENSHOT_DIR, "loupe-dark.png"), Buffer.from(shot.data, "base64"));
  }
  await evaluate("codeEl.value = 'print(\"loop ready\", flush=True)\\nwhile True: pass'; runEl.click()");
  await waitFor(() => evaluate("outputEl.textContent.includes('loop ready')"), "infinite loop started");
  await evaluate("document.getElementById('stop').click()");
  await waitFor(() => evaluate("outputEl.textContent.includes('kernel restarted')"), "Stop recovery");
  await evaluate("codeEl.value = '42'; runEl.click()");
  await waitFor(() => evaluate("outputEl.textContent === '42'"), "execution after Stop");
  await command("Page.reload");
  await waitFor(() => evaluate("typeof run === 'function' && ws.readyState === WebSocket.OPEN"), "reloaded connection");
  assert.equal(await evaluate("document.documentElement.dataset.theme"), "dark");
  await command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await waitFor(() => evaluate("innerWidth === 390"), "mobile layout");
  assert.ok(await evaluate("document.documentElement.scrollWidth <= innerWidth"), "mobile layout overflows");
  assert.deepEqual(exceptions, []);
  assert.ok(requests.length >= 4);
  assert.ok(requests.every(request => request.startsWith(url)), `non-local request: ${requests}`);
});
