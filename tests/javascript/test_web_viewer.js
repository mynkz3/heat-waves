"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const ui = require("../../frontend/viewer.js");

test("lazy event loading keeps original precision, caches a shard, and uses its index not its ID", async () => {
  assert.equal(typeof ui.createDetailLoader, "function");
  let requests = 0;
  const features = [0.9876543210987654, 0.1234567890123456].map(anomaly_score => ({
    type: "Feature", geometry: {type: "Point", coordinates: [82, 24]},
    properties: {event_id: "../../same", anomaly_score, sentinel_preview: "sentinel/saved.png"}
  }));
  const load = ui.createDetailLoader(async url => {
    assert.equal(url, "data/events/0.json"); requests++;
    return {ok: true, json: async () => ({type: "FeatureCollection", features})};
  });
  const summary = index => ({properties: {event_id: "../../same"}, detail: {url: "data/events/0.json", index}});
  assert.equal(requests, 0);
  assert.deepEqual(await load(summary(0)), features[0]);
  assert.deepEqual(await load(summary(1)), features[1]);
  assert.equal(requests, 1);
  assert.deepEqual(await load(features[0]), features[0], "Portable embedded data should not fetch");
});

test("lazy payload errors can be retried and reject remote or traversing references", async () => {
  assert.equal(typeof ui.createDetailLoader, "function");
  let attempts = 0;
  const feature = {type: "Feature", geometry: {type: "Point", coordinates: [82, 24]}, properties: {event_id: "E1"}};
  const load = ui.createDetailLoader(async () => {
    if (++attempts === 1) return {ok: false, status: 404};
    return {ok: true, json: async () => ({type: "FeatureCollection", features: [feature]})};
  });
  const summary = {properties: {event_id: "E1"}, detail: {url: "data/events/0.json", index: 0}};
  await assert.rejects(load(summary), /404/);
  assert.deepEqual(await load(summary), feature);
  for (const url of ["https://example.org/e.json", "../e.json", "data/../e.json", "data/%2e%2e/e.json", "//elsewhere/e.json"]) {
    await assert.rejects(load({...summary, detail: {url, index: 0}}), /local|path/i);
  }
  await assert.rejects(load({...summary, detail: {url: "data/events/0.json", index: 5}}), /detail|event/i);
  await assert.rejects(load({...summary, properties: {event_id: "wrong"}}), /detail|event/i);
});

if (process.argv[2] === "--browser") test("localhost browser: loading, retry, lazy evidence, moved previews and offline roads", async () => {
  const fs = require("node:fs"), http = require("node:http"), path = require("node:path");
  const {spawn} = require("node:child_process");
  const [browser, site, profile] = process.argv.slice(3);
  const requested = [], external = [];
  let blocked = "/data/workspace.json", socket, child;
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    requested.push(pathname);
    const filename = path.join(site, pathname === "/" ? "index.html" : pathname);
    if (!filename.startsWith(site + path.sep) || pathname === blocked || !fs.existsSync(filename)) {
      response.writeHead(404); response.end("Missing fixture payload"); return;
    }
    const types = {".html": "text/html", ".js": "application/javascript", ".json": "application/json",
      ".css": "text/css", ".ttf": "font/ttf", ".png": "image/png"};
    response.setHeader("Content-Type", types[path.extname(filename)] || "application/octet-stream");
    if (fs.existsSync(filename + ".gz") && /gzip/.test(request.headers["accept-encoding"] || "")) {
      response.setHeader("Content-Encoding", "gzip"); fs.createReadStream(filename + ".gz").pipe(response);
    } else fs.createReadStream(filename).pipe(response);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const pending = new Map();
  try {
    child = spawn(browser, ["--headless=new", "--disable-gpu", "--no-sandbox", "--disable-background-networking",
      "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "--remote-debugging-address=127.0.0.1",
      "--remote-debugging-port=0", "--window-size=1440,1000", "--user-data-dir=" + profile, "about:blank"],
    {stdio: "ignore", windowsHide: true});
    const activePort = path.join(profile, "DevToolsActivePort");
    for (let i = 0; i < 100 && !fs.existsSync(activePort); i++) await pause(60);
    assert.ok(fs.existsSync(activePort), "Browser debugger unavailable");
    const port = fs.readFileSync(activePort, "utf8").split(/\r?\n/)[0];
    const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    socket = new WebSocket(pages.find(page => page.type === "page").webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {socket.addEventListener("open", resolve, {once: true}); socket.addEventListener("error", reject, {once: true});});
    let serial = 0;
    socket.addEventListener("message", event => {
      const message = JSON.parse(event.data), request = pending.get(message.id);
      if (message.method === "Network.requestWillBeSent" && /^https?:/.test(message.params.request.url) &&
          !message.params.request.url.startsWith(base)) external.push(message.params.request.url);
      if (request) {
        pending.delete(message.id); clearTimeout(request.timer);
        message.error ? request.reject(new Error(message.error.message)) : request.resolve(message.result);
      }
    });
    const command = (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++serial, timer = setTimeout(() => reject(new Error("Browser command timed out: " + method)), 5000);
      pending.set(id, {resolve, reject, timer}); socket.send(JSON.stringify({id, method, params}));
    });
    const evaluate = async expression => {
      const result = await command("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
      if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    async function until(expression) {
      for (let i = 0; i < 100; i++) {
        const result = await evaluate(expression);
        if (result) return result;
        await pause(50);
      }
      throw new Error("Browser condition timed out: " + expression);
    }
    await command("Network.enable");
    await command("Network.setBlockedURLs", {urls: ["https://*"]});
    await command("Page.navigate", {url: base});
    await until("document.querySelector('#fatal-error') && !document.querySelector('#fatal-error').hidden");
    assert.match(await evaluate("document.querySelector('#fatal-error').textContent"), /404.*localhost/);
    assert.equal(await evaluate("document.querySelector('#loading-state').hidden"), true);
    blocked = null;
    await command("Page.reload");
    await until("document.querySelector('#count')?.textContent.includes('6 events shown')");
    assert.equal(requested.some(url => url.startsWith("/data/events/")), false, "Startup eagerly loaded full evidence");
    assert.ok(requested.includes("/vendor/leaflet.js") && requested.includes("/vendor/Sora-Variable.ttf"));
    assert.match(await evaluate("document.querySelector('#snapshot-date').textContent"), /08 Sept? 2026|08 Sep 2026/);
    await until("document.querySelector('#basemap-status')?.textContent.includes('cached OSM roads')");
    assert.equal(await evaluate("document.querySelector('#show-tiles').checked"), false, "Failed online tiles did not fall back");
    assert.ok(external.some(url => url.startsWith("https://tile.openstreetmap.org/")), "Online tiles were not attempted");
    assert.ok(external.every(url => url.startsWith("https://tile.openstreetmap.org/")), "Unexpected external data request");
    blocked = "/data/events/0.json";
    await evaluate("document.querySelector('[data-event-id=\"E2\"]').click()");
    await until("document.querySelector('#detail-body [role=\"alert\"]')");
    blocked = null;
    await evaluate("document.querySelector('#detail-body button').click()");
    await until("document.querySelector('#detail-body')?.textContent.includes('0.318')");
    assert.match(await evaluate("document.querySelector('#detail-body').textContent"), /12\.5%/);
    await until("document.querySelector('.detail-preview')?.complete && document.querySelector('.detail-preview').naturalWidth > 0");
    assert.ok(requested.includes("/sentinel/saved.png"), "Moved site did not resolve local preview");
    assert.equal(await evaluate("!!globalThis.__injected"), false);
    await evaluate("document.querySelector('#close-detail').click(); document.querySelector('#score').value='0.99'; document.querySelector('#score').dispatchEvent(new Event('input'))");
    assert.match(await evaluate("document.querySelector('#count').textContent"), /3 events/);
    await evaluate("document.querySelector('#reset').click(); document.querySelector('#show-tiles').checked=true; document.querySelector('#show-tiles').dispatchEvent(new Event('change'))");
    await until("!document.querySelector('#show-tiles').checked");
    assert.match(await evaluate("document.querySelector('#count').textContent"), /6 events/);
    assert.equal(await evaluate("document.querySelector('#map').dataset.visibleEvents"), "6");
  } finally {
    for (const request of pending.values()) clearTimeout(request.timer);
    if (socket) socket.close();
    if (child) {
      const exited = new Promise(resolve => child.once("exit", resolve));
      child.kill(); await exited;
    }
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});
