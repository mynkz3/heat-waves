"use strict";
// Full saved-dataset smoke: node test_release_browser.js <browser> <python> [screenshots] [project]
// Uses the shipped stdlib launcher and blocks every external browser request.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {spawn} = require("node:child_process");

async function main() {
  const [browser, python, reportArg, projectArg] = process.argv.slice(2);
  assert.ok(browser && python, "Usage: node test_release_browser.js <browser> <python> [screenshots] [project]");
  const project = path.resolve(projectArg || __dirname);
  const reports = path.resolve(reportArg || path.join(project, "reports/generated/deployment"));
  const config = JSON.parse(fs.readFileSync(path.join(project, "config.json"), "utf8"));
  const site = path.join(project, config.paths.output, "site");
  const data = JSON.parse(fs.readFileSync(path.join(site, "data/workspace.json"), "utf8"));
  const summaries = data.events.features;
  assert.equal(summaries.length, 35963, "Saved observation count changed");
  let example;
  for (const name of fs.readdirSync(path.join(site, "data/events")).filter(name => name.endsWith(".json"))) {
    const features = JSON.parse(fs.readFileSync(path.join(site, "data/events", name), "utf8")).features;
    example = features.find(feature => feature.properties.sentinel_status === "analysed" &&
      feature.properties.sentinel_radiometry_status !== "legacy_offset_unverified" && feature.properties.sentinel_preview);
    if (example) break;
  }
  assert.ok(example, "No saved analysed Sentinel event with a local preview");
  fs.mkdirSync(reports, {recursive: true});
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "thermal-release-browser-"));
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const timings = {}, requests = [], failures = [], responses = [];
  const processes = [], pending = new Map();
  let socket, serverLog = "", serverErrors = "";
  const began = Date.now();
  try {
    const server = spawn(python, ["-S", path.join(project, "app.py"), "serve", "--port", "0"],
      {cwd: os.tmpdir(), stdio: ["ignore", "pipe", "pipe"], windowsHide: true});
    processes.push(server);
    server.stdout.on("data", chunk => {serverLog += chunk;});
    server.stderr.on("data", chunk => {serverErrors += chunk;});
    for (let i = 0; i < 100 && !/http:\/\/127\.0\.0\.1:\d+/.test(serverLog); i++) await pause(50);
    const base = serverLog.match(/http:\/\/127\.0\.0\.1:\d+/)?.[0];
    assert.ok(base, "Stdlib launcher failed: " + serverErrors);
    timings.server_ready_ms = Date.now() - began;
    const child = spawn(browser, ["--headless=new", "--disable-gpu", "--no-sandbox", "--disable-background-networking",
      "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "--remote-debugging-address=127.0.0.1",
      "--remote-debugging-port=0", "--window-size=1440,1000", "--force-prefers-reduced-motion",
      "--user-data-dir=" + profile, "about:blank"], {stdio: "ignore", windowsHide: true});
    processes.push(child);
    const activePort = path.join(profile, "DevToolsActivePort");
    for (let i = 0; i < 160 && !fs.existsSync(activePort); i++) await pause(50);
    assert.ok(fs.existsSync(activePort), "Browser debugger unavailable");
    const port = fs.readFileSync(activePort, "utf8").split(/\r?\n/)[0];
    const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    socket = new WebSocket(pages.find(page => page.type === "page").webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, {once: true});
      socket.addEventListener("error", reject, {once: true});
    });
    let serial = 0;
    socket.addEventListener("message", event => {
      const message = JSON.parse(event.data), request = pending.get(message.id);
      if (message.method === "Network.requestWillBeSent") requests.push(message.params.request.url);
      if (message.method === "Runtime.exceptionThrown") failures.push(message.params.exceptionDetails);
      if (message.method === "Network.responseReceived") responses.push(message.params.response);
      if (request) {
        pending.delete(message.id); clearTimeout(request.timer);
        message.error ? request.reject(new Error(message.error.message)) : request.resolve(message.result);
      }
    });
    const command = (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++serial, timer = setTimeout(() => reject(new Error("Browser command timed out: " + method)), 15000);
      pending.set(id, {resolve, reject, timer});
      socket.send(JSON.stringify({id, method, params}));
    });
    const evaluate = async expression => {
      const result = await command("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
      if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    async function until(expression, timeout = 30000) {
      const began = Date.now();
      while (Date.now() - began < timeout) {
        const result = await evaluate(expression);
        if (result) return result;
        await pause(75);
      }
      throw new Error("Browser condition timed out: " + expression);
    }
    async function capture(name) {
      await pause(150);
      const screenshot = await command("Page.captureScreenshot", {format: "png", captureBeyondViewport: false});
      fs.writeFileSync(path.join(reports, name), Buffer.from(screenshot.data, "base64"));
    }
    const assertNoOverflow = async () => assert.equal(
      await evaluate("document.documentElement.scrollWidth > innerWidth"), false, "Horizontal page overflow");
    await command("Network.enable");
    await command("Runtime.enable");
    await command("Network.setBlockedURLs", {urls: ["https://*"]});
    const navigation = Date.now();
    await command("Page.navigate", {url: base});
    await until("document.querySelector('#count')?.textContent.includes('35,963 events shown')");
    timings.events_ready_ms = Date.now() - navigation;
    assert.equal(await evaluate("document.querySelector('#total-count').textContent"), "35,963");
    assert.equal(await evaluate("document.querySelector('#map').dataset.visibleEvents"), "35963");
    assert.equal(requests.some(url => url.includes("/data/events/")), false, "Full evidence loaded at startup");
    await until("document.querySelector('#basemap-status')?.textContent.includes('cached OSM roads')");
    timings.roads_ready_ms = Date.now() - navigation;
    assert.equal(await evaluate("document.querySelector('#show-tiles').checked"), false);
    assert.ok(requests.some(url => url.startsWith("https://tile.openstreetmap.org/")), "Online tiles were not attempted");
    assert.ok(requests.filter(url => /^https?:/.test(url) && !url.startsWith(base))
      .every(url => url.startsWith("https://tile.openstreetmap.org/")), "Unexpected external request");
    const payloadResponse = responses.find(response => response.url === base + "/data/workspace.json");
    assert.equal(payloadResponse?.headers["Content-Encoding"], "gzip", "Launcher did not serve compressed JSON");
    await assertNoOverflow();
    await capture("desktop-map.png");
    const expectedThreshold = summaries.filter(feature => feature.properties.anomaly_score == null ||
      feature.properties.anomaly_score >= 0.99).length;
    await evaluate("document.querySelector('#score').value='0.99'; document.querySelector('#score').dispatchEvent(new Event('input'))");
    assert.equal(await evaluate("document.querySelector('#count').textContent"),
      expectedThreshold.toLocaleString("en-IN") + " events shown");
    await evaluate("document.querySelector('#reset').click(); document.querySelector('#period').value='day'; document.querySelector('#period').dispatchEvent(new Event('change'))");
    assert.equal(await evaluate("document.querySelector('#count').textContent"), "0 events shown");
    assert.equal(await evaluate("document.querySelector('#empty-state').hidden"), false);
    await evaluate("document.querySelector('#reset').click()");
    const id = JSON.stringify(example.properties.event_id);
    await evaluate(`document.querySelector('#search').value=${id}; document.querySelector('#search').dispatchEvent(new Event('input'))`);
    await until("document.querySelectorAll('.event').length===1");
    const selection = Date.now();
    await evaluate("document.querySelector('.event').click()");
    await until("document.querySelector('.detail-preview')");
    const previewBeforeScroll = await evaluate(`(() => {
      const image=document.querySelector('.detail-preview'), rect=image.getBoundingClientRect();
      return {top:rect.top, viewport:innerHeight, loading:image.loading, complete:image.complete, naturalWidth:image.naturalWidth};
    })()`);
    console.log("Preview before scrolling: " + JSON.stringify(previewBeforeScroll));
    await evaluate("document.querySelector('.detail-preview').scrollIntoView({block:'center'})");
    await until("document.querySelector('.detail-preview')?.complete && document.querySelector('.detail-preview').naturalWidth > 0");
    timings.evidence_ready_ms = Date.now() - selection;
    const expectedDnbr = Number(example.properties.sentinel_dnbr_mean).toFixed(3);
    assert.ok(await evaluate(`document.querySelector('#detail-body').textContent.includes(${JSON.stringify(expectedDnbr)})`));
    assert.equal(await evaluate("new URL(document.querySelector('.detail-preview').src).pathname"),
      "/" + example.properties.sentinel_preview);
    await evaluate("document.querySelector('.detail-preview').scrollIntoView({block:'center'})");
    await capture("desktop-evidence.png");
    await command("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
    await evaluate("document.querySelector('#close-detail').click(); document.querySelector('#reset').click(); document.querySelector('#nav-map').click()");
    await pause(400);
    await evaluate("document.querySelector('#home').click()");
    await pause(150);
    await assertNoOverflow();
    await capture("mobile-map.png");
    await evaluate(`document.querySelector('#toggle-sidebar').click(); document.querySelector('#search').value=${id}; document.querySelector('#search').dispatchEvent(new Event('input'))`);
    await until("document.querySelectorAll('.event').length===1");
    await evaluate("document.querySelector('.event').click()");
    await until("document.querySelector('.detail-preview')");
    await evaluate("document.querySelector('.detail-preview').scrollIntoView({block:'center'})");
    await until("document.querySelector('.detail-preview')?.complete && document.querySelector('.detail-preview').naturalWidth > 0");
    await evaluate("document.querySelector('.detail-preview').scrollIntoView({block:'center'})");
    await assertNoOverflow();
    await capture("mobile-evidence.png");
    assert.deepEqual(failures, [], "Browser runtime errors");
    timings.total_ms = Date.now() - began;
    const report = {status: "pass", events: summaries.length, selected_event: example.properties.event_id,
      sentinel_preview: example.properties.sentinel_preview, min_score_099_count: expectedThreshold,
      gzip_workspace_bytes: fs.statSync(path.join(site, "data/workspace.json.gz")).size, timings,
      external_request_hosts: [...new Set(requests.filter(url => url.startsWith("https:")).map(url => new URL(url).host))],
      screenshots: ["desktop-map.png", "desktop-evidence.png", "mobile-map.png", "mobile-evidence.png"]};
    fs.writeFileSync(path.join(reports, "smoke.json"), JSON.stringify(report, null, 2));
    console.log("PASS: full-data localhost browser\n" + JSON.stringify(report, null, 2));
  } finally {
    for (const request of pending.values()) clearTimeout(request.timer);
    if (socket) socket.close();
    for (const child of processes.reverse()) {
      if (child.exitCode !== null) continue;
      const exited = new Promise(resolve => child.once("exit", resolve));
      child.kill(); await exited;
    }
    if (path.dirname(path.resolve(profile)) === path.resolve(os.tmpdir()) &&
        path.basename(profile).startsWith("thermal-release-browser-")) {
      fs.rmSync(profile, {recursive: true, force: true, maxRetries: 5, retryDelay: 100});
    }
  }
}

// This integration smoke is opt-in; normal `node --test test_*.js` has no side effects.
if (process.argv[2]) main().catch(error => {console.error(error); process.exitCode = 1;});
