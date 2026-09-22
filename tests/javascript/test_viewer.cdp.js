"use strict";
// Real-clock browser verification with Node's built-in WebSocket; no automation dependency.
const fs = require("node:fs");
const path = require("node:path");
const {spawn} = require("node:child_process");
const {pathToFileURL} = require("node:url");
const [browser, html, profile, screenshots, mode] = process.argv.slice(2);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const ok = (value, message) => {if (!value) throw new Error(message);};

(async () => {
  const launchStarted = Date.now();
  const child = spawn(browser, ["--headless=new", "--disable-gpu", "--no-sandbox", "--disable-background-networking", "--host-resolver-rules=MAP * ~NOTFOUND", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", "--window-size=1440,1000", "--user-data-dir=" + profile, pathToFileURL(html).href], {stdio: "ignore", windowsHide: true});
  let socket, serial = 0, command;
  try {
    const activePort = path.join(profile, "DevToolsActivePort");
    const started = Date.now();
    while (!fs.existsSync(activePort) && Date.now() - started < 12000) await pause(60);
    ok(fs.existsSync(activePort), "Chrome did not expose its local debugging port");
    const port = fs.readFileSync(activePort, "utf8").split(/\r?\n/)[0];
    const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const page = pages.find(entry => entry.type === "page");
    ok(page, "Chrome did not open the fixture");
    socket = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {socket.addEventListener("open", resolve, {once: true}); socket.addEventListener("error", reject, {once: true});});
    const pending = new Map();
    socket.addEventListener("message", event => {
      const result = JSON.parse(event.data), request = pending.get(result.id);
      if (request) {pending.delete(result.id); clearTimeout(request.timer); result.error ? request.reject(new Error(result.error.message)) : request.resolve(result.result);}
    });
    command = (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++serial;
      const timer = setTimeout(() => {pending.delete(id); reject(new Error("CDP timed out: " + method));}, 5000);
      pending.set(id, {resolve, reject, timer}); socket.send(JSON.stringify({id, method, params}));
    });
    const evaluate = async expression => {
      const result = await command("Runtime.evaluate", {expression, returnByValue: true});
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
      return result.result.value;
    };
    async function until(expression, timeout = 5000) {
      const start = Date.now();
      while (Date.now() - start < timeout) {const value = await evaluate(expression); if (value) return value; await pause(80);}
      throw new Error("Browser condition timed out: " + expression);
    }
    async function capture(name) {
      if (!screenshots) return;
      fs.mkdirSync(screenshots, {recursive: true});
      const shot = await command("Page.captureScreenshot", {format: "png", captureBeyondViewport: false});
      fs.writeFileSync(path.join(screenshots, name), Buffer.from(shot.data, "base64"));
    }
    async function checkHover() {
      // Hover real painted vegetation markers, not a synthetic tooltip detached from the map.
      // Isolate this context: fixture E1 and E2 share a location and otherwise occlude each other.
      await evaluate("document.querySelector('#filter').value='likely_vegetation_fire'; document.querySelector('#filter').dispatchEvent(new Event('change')); document.querySelector('#fit').click()");
      await pause(400);
      const points = await evaluate(`(() => {
        const area=document.querySelector('#map').getBoundingClientRect(), found=[];
        for(const c of document.querySelectorAll('.leaflet-overlay-pane canvas')) {
          if(!c.width || !c.height) continue;
          const r=c.getBoundingClientRect(), pixels=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
          for(let y=3;y<c.height;y+=3) for(let x=3;x<c.width;x+=3) {
            const i=(y*c.width+x)*4, red=pixels[i], green=pixels[i+1], blue=pixels[i+2];
            const px=r.x+x*r.width/c.width, py=r.y+y*r.height/c.height;
            if(pixels[i+3]>150 && green>red*1.15 && green>blue*1.15 &&
                px>area.x+40 && px<area.right-40 && py>area.y+40 && py<area.bottom-40) found.push({x:px,y:py});
            if(found.length>=20) return found;
          }
        }
        return found;
      })()`);
      for (const point of points) {
        await command("Input.dispatchMouseEvent", {type:"mouseMoved", x:point.x, y:point.y});
        await pause(80);
        const tooltip = await evaluate(`(() => {
          const e=document.querySelector('.leaflet-tooltip'); if(!e) return null;
          const r=e.getBoundingClientRect();
          return {text:e.textContent,width:r.width,height:r.height};
        })()`);
        if (tooltip) {
          ok(tooltip.width>=120 && tooltip.height<tooltip.width, "Hover text collapsed into a vertical column: "+JSON.stringify(tooltip));
          return tooltip;
        }
      }
      throw new Error("Could not hover a visible event marker");
    }
    async function checkTooltipEdges() {
      const example = await evaluate(`(() => {
        const feature=JSON.parse(document.querySelector('#evidence-data').textContent).events.features.find(f=>f.properties.decision==='likely_vegetation_fire');
        const search=document.querySelector('#search'); search.value=feature.properties.event_id; search.dispatchEvent(new Event('input'));
        document.querySelector('#show-facilities').checked=false; document.querySelector('#show-facilities').dispatchEvent(new Event('change'));
        return {id:feature.properties.event_id, coordinates:feature.geometry.coordinates};
      })()`);
      await until(`document.querySelectorAll('.event').length===1 && document.querySelector('.event').dataset.eventId===${JSON.stringify(example.id)}`);
      // Capture the real map only in the harness; no test hooks in production.
      await evaluate(`(() => {
        const original=L.Map.prototype.panTo;
        try {
          L.Map.prototype.panTo=function(...args){window.__hoverMap=this;return original.apply(this,args);};
          document.querySelector('.event').click();
        } finally {L.Map.prototype.panTo=original;}
        document.querySelector('#close-detail').click();
      })()`);
      const checks = [];
      for (const [width,height] of [[390,844],[320,640],[1440,900]]) {
        await command("Emulation.setDeviceMetricsOverride", {width,height,deviceScaleFactor:1,mobile:width<=800});
        await evaluate("document.querySelector('#nav-map').click(); document.querySelector('#fit').click()");
        await pause(350);
        for (const edge of ["side","top"]) {
          const target = await evaluate(`(() => {
            const m=window.__hoverMap, size=m.getSize(), [lon,lat]=${JSON.stringify(example.coordinates)};
            const target=L.point(size.x/2-5, ${JSON.stringify(edge)}==='top'?7:size.y/2);
            m.panBy(m.latLngToContainerPoint([lat,lon]).subtract(target),{animate:false});
            const r=m.getContainer().getBoundingClientRect(); return {x:r.x+target.x,y:r.y+target.y};
          })()`);
          await command("Input.dispatchMouseEvent", {type:"mouseMoved", x:10,y:10});
          // Leaflet retains closed tooltips for a 200 ms fade; do not measure the stale node.
          await until("!document.querySelector('.leaflet-tooltip')");
          await command("Input.dispatchMouseEvent", {type:"mouseMoved", ...target});
          await until("document.querySelector('.leaflet-tooltip')");
          const bounds = await evaluate(`(() => {
            const t=document.querySelector('.leaflet-tooltip').getBoundingClientRect(), m=document.querySelector('#map').getBoundingClientRect();
            return {width:t.width,height:t.height,left:t.left-m.left,right:m.right-t.right,top:t.top-m.top,bottom:m.bottom-t.bottom};
          })()`);
          ok(bounds.width>=120 && ["left","right","top","bottom"].every(key=>bounds[key]>=-1),
            `${width}x${height} ${edge}: hover tooltip is clipped by the map: ${JSON.stringify(bounds)}`);
          checks.push({viewportWidth:width,viewportHeight:height,edge,...bounds});
          if (edge==="side") await capture(`tooltip-edge-${width}.png`);
        }
      }
      await evaluate("document.querySelector('#show-facilities').checked=true; document.querySelector('#show-facilities').dispatchEvent(new Event('change'))");
      const facility = await evaluate(`(() => {
        const m=window.__hoverMap; let layer;
        m.eachLayer(candidate=>{if(!layer && candidate.feature && candidate.getTooltip?.()) layer=candidate;});
        const ll=layer.getLatLng ? layer.getLatLng() : layer.getBounds().getCenter();
        m.setView(ll,13,{animate:false});
        const target=L.point(m.getSize().x/2-5,7);
        m.panBy(m.latLngToContainerPoint(ll).subtract(target),{animate:false});
        const r=m.getContainer().getBoundingClientRect();return {x:r.x+target.x,y:r.y+target.y,zoom:m.getZoom()};
      })()`);
      await command("Input.dispatchMouseEvent", {type:"mouseMoved", x:10,y:10});
      await until("!document.querySelector('.leaflet-tooltip')");
      await command("Input.dispatchMouseEvent", {type:"mouseMoved", x:facility.x,y:facility.y});
      await until("document.querySelector('.leaflet-tooltip')");
      await command("Input.dispatchMouseEvent", {type:"mouseWheel",x:facility.x,y:facility.y,deltaX:0,deltaY:-120});
      await until(`Number(document.querySelector('#map').dataset.zoom) > ${facility.zoom}`);
      ok(await evaluate("[...document.querySelectorAll('.leaflet-tooltip')].every(e=>e.style.opacity==='0')"),
        "Facility tooltip remains open after zoom and can be repositioned outside the map");
      await evaluate("delete window.__hoverMap");
      return checks;
    }
    const report = {html, clock: new Date().toISOString(), windows: []};
    if (mode === "--production") {
      await until("document.querySelector('#map')?.dataset.visibleEvents && document.querySelector('#fatal-error')?.hidden", 30000);
      report.load = await evaluate("({navigation:performance.getEntriesByType('navigation')[0]?.toJSON(), heapBytes:performance.memory?.usedJSHeapSize, ready:document.readyState})");
      report.load.coldProcessToReadyMs = Date.now() - launchStarted;
      report.load.htmlBytes = fs.statSync(html).size;
      await evaluate("document.querySelector('#show-tiles').checked=false; document.querySelector('#show-tiles').dispatchEvent(new Event('change'))");
      for (const period of ["day", "week", "all"]) {
        const sample = await evaluate(`(() => {
          const now=Date.now(), period=${JSON.stringify(period)};
          const filterStart=performance.now();
          const select=document.querySelector('#period'); select.value=period; select.dispatchEvent(new Event('change'));
          const filterMs=performance.now()-filterStart;
          const payload=JSON.parse(document.querySelector('#evidence-data').textContent), events=payload.events.features;
          const cutoff=now-(period==='day'?1:7)*86400000;
          const expected=period==='all'?events.length:events.filter(event=>{
            const p=event.properties, start=Date.parse(p.start_time), end=Date.parse(p.end_time);
            return Number.isFinite(start) && end>=cutoff && start<=now;
          }).length;
          return {period, now, expected, filterMs, roads:payload.roads.features.length, shown:Number(document.querySelector('#count').textContent.replace(/[^0-9]/g,'')),
            replay:document.querySelector('#replay').checked, state:document.querySelector('#freshness-state').textContent,
            visible:Number(document.querySelector('#map').dataset.visibleEvents), groups:Number(document.querySelector('#map').dataset.renderedGroups),
            empty:!document.querySelector('#empty-state').hidden};
        })()`);
        ok(Math.abs(sample.now-Date.now()) < 10000, "Browser clock is not current wall-clock time");
        ok(!sample.replay, "Production time filter silently enabled historical replay");
        ok(sample.shown === sample.expected, `${period}: expected ${sample.expected}, displayed ${sample.shown}`);
        ok(sample.empty === (sample.expected === 0), `${period}: empty-state visibility is wrong`);
        report.windows.push(sample);
        await pause(250); await capture(period + "-window.png");
      }
      report.interpretation = await evaluate(`(() => {
        const payload=JSON.parse(document.querySelector('#evidence-data').textContent);
        const rows=payload.events.features.map(f=>f.properties), unscored=rows.filter(p=>p.anomaly_score==null);
        return {schemaVersion:payload.metadata.interpretation?.schema_version, total:rows.length,
          scored:rows.length-unscored.length, unscored:unscored.length,
          missingReasons:unscored.filter(p=>!p.score_status || !p.score_reason).length,
          lostIndustrialContext:unscored.filter(p=>p.context_label==='industrial_associated' &&
            !['industrial_associated_unscored','mining_associated_unscored'].includes(p.decision)).length,
          expectedReview:rows.filter(p=>['suspected_abnormal_industrial_associated_event','suspected_abnormal_mining_associated_event'].includes(p.decision)).length,
          displayedReview:Number(document.querySelector('#review-count').textContent.replace(/[^0-9]/g,''))};
      })()`);
      ok(report.interpretation.schemaVersion===2, 'Production map lacks interpretation schema v2');
      ok(report.interpretation.missingReasons===0, 'Unscored production events lack explicit reasons');
      ok(report.interpretation.lostIndustrialContext===0, 'Unscored production events lost industrial context');
      ok(report.interpretation.expectedReview===report.interpretation.displayedReview, 'Top review count omitted a context group');
      report.availability = {};
      for (const availability of ['scored','unscored','all']) {
        const shown = await evaluate(`(() => {
          const select=document.querySelector('#score-availability'); select.value=${JSON.stringify(availability)};
          select.dispatchEvent(new Event('change'));
          return Number(document.querySelector('#count').textContent.replace(/[^0-9]/g,''));
        })()`);
        const expected=availability==='all'?report.interpretation.total:report.interpretation[availability];
        ok(shown===expected, availability+': score-availability filter has incorrect count');
        report.availability[availability]=shown;
      }
      report.contextFilters = [];
      for (const decision of ['industrial_associated_unscored','mining_associated_unscored','suspected_abnormal_mining_associated_event','mining_associated_thermal_source']) {
        const sample = await evaluate(`(() => {
          const decision=${JSON.stringify(decision)}, select=document.querySelector('#filter');
          select.value=decision; select.dispatchEvent(new Event('change'));
          const rows=JSON.parse(document.querySelector('#evidence-data').textContent).events.features;
          return {decision,selected:select.value,expected:rows.filter(f=>f.properties.decision===decision).length,
            shown:Number(document.querySelector('#count').textContent.replace(/[^0-9]/g,''))};
        })()`);
        ok(sample.selected===decision && sample.shown===sample.expected, decision+': category filter is missing or incorrect');
        report.contextFilters.push(sample);
      }
      await evaluate("document.querySelector('#reset').click()");
      if (report.windows[0].roads) {
        const roadPixels = "(() => {const c=document.querySelector('.leaflet-overlay-pane canvas'),p=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let n=0;for(let i=3;i<p.length;i+=4) if(p[i]) n++;return n;})()";
        const visiblePixels = await evaluate(roadPixels);
        await evaluate("document.querySelector('#show-roads').checked=false; document.querySelector('#show-roads').dispatchEvent(new Event('change'))");
        await pause(250);
        const hiddenPixels = await evaluate(roadPixels);
        await evaluate("document.querySelector('#show-roads').checked=true; document.querySelector('#show-roads').dispatchEvent(new Event('change'))");
        await pause(250);
        const restoredPixels = await evaluate(roadPixels);
        ok(visiblePixels > hiddenPixels && restoredPixels > hiddenPixels, "Real offline road geometry did not hide and restore on the canvas");
        report.roads = {features:report.windows[0].roads, visiblePixels, hiddenPixels, restoredPixels};
      }
      report.cardOpenMs = await evaluate("(() => {const start=performance.now();document.querySelector('.event').click();return performance.now()-start;})()");
      report.event = await evaluate("({id:document.querySelector('#detail-title').textContent, visible:!document.querySelector('#detail').hidden, text:document.querySelector('#detail-body').textContent, sentinel:document.querySelector('.sentinel-status').textContent})");
      ok(report.event.visible && report.event.text.includes('IST') && report.event.text.includes('UTC'), "Real event card is missing acquisition timezones");
      ok(report.event.text.includes('Satellite observations') && report.event.text.includes('Anomaly evidence'), "Real event card is missing evidence sections");
      delete report.event.text;
      await pause(350); await capture("event-card.png");
      report.unscoredCards = [];
      for (const context of ['industrial_associated','mining_quarry','vegetation_associated']) {
        const example = await evaluate(`(() => {
          const feature=JSON.parse(document.querySelector('#evidence-data').textContent).events.features.find(f=>
            f.properties.anomaly_score==null && f.properties.source_context===${JSON.stringify(context)});
          if (!feature) return null;
          const p=feature.properties, search=document.querySelector('#search');
          search.value=p.event_id; search.dispatchEvent(new Event('input'));
          return {id:p.event_id,context:p.source_context,status:p.score_status,reason:p.score_reason};
        })()`);
        ok(example, 'Missing production unscored example for '+context);
        await until(`document.querySelector('.event[data-event-id="${example.id}"]') !== null`);
        await evaluate(`document.querySelector('.event[data-event-id="${example.id}"]').click()`);
        ok(await evaluate("document.querySelector('.score-explanation')?.textContent")===example.reason, 'Unscored card does not display its exported reason');
        report.unscoredCards.push(example);
        if (context==='mining_quarry') {await pause(250); await capture('unscored-mining-card.png');}
        await evaluate("document.querySelector('#reset').click()");
      }
      const peer = await evaluate(`(() => {
        const feature=JSON.parse(document.querySelector('#evidence-data').textContent).events.features.find(f=>f.properties.score_method==='vegetation_peer');
        if(!feature) return null;
        const p=feature.properties, search=document.querySelector('#search');
        search.value=p.event_id; search.dispatchEvent(new Event('input'));
        return {id:p.event_id,references:p.peer_reference_count,features:p.peer_feature_count};
      })()`);
      ok(peer, 'Production output has no vegetation peer scores');
      await until(`document.querySelector('.event[data-event-id="${peer.id}"]') !== null`);
      await evaluate(`document.querySelector('.event[data-event-id="${peer.id}"]').click()`);
      const peerFacts = await evaluate("Object.fromEntries([...document.querySelector('#detail').querySelectorAll('dt')].map(n=>[n.textContent,n.nextElementSibling.textContent]))");
      ok(peerFacts['Comparison method']==='Vegetation peers', 'Peer card is labelled as a same-site comparison');
      ok(Number(peerFacts['Earlier peer episodes'])===peer.references && Number(peerFacts['Model features used'])===peer.features, 'Peer card counts differ from exported provenance');
      ok(peerFacts['Historical comparison events']===undefined && peerFacts['Isolation anomaly percentile']===undefined, 'Peer card exposes an inactive site baseline as model evidence');
      ok(await evaluate("document.querySelector('#freshness-message').textContent.includes('not evidence of no fires')"), 'Production map lacks the coverage warning');
      report.vegetationPeer = peer;
      await evaluate("[...document.querySelectorAll('#detail h3')].find(e=>e.textContent.includes('Anomaly evidence'))?.scrollIntoView({block:'start'})");
      await pause(250); await capture('vegetation-peer-card.png');
      await evaluate("document.querySelector('#reset').click()");
      report.sentinelPreviews = [];
      for (const kind of ["industrial", "mining", "vegetation"]) {
        const example = await evaluate(`(() => {
          const features=JSON.parse(document.querySelector('#evidence-data').textContent).events.features;
          const feature=features.find(f=>f.properties.sentinel_status==='analysed' &&
            f.properties.sentinel_radiometry_status==='asset_metadata' &&
            f.properties.decision.includes(${JSON.stringify(kind)}) &&
            String(f.properties.sentinel_evidence).startsWith('observed_'));
          if (!feature) return null;
          const p=feature.properties;
          const search=document.querySelector('#search'); search.value=p.event_id; search.dispatchEvent(new Event('input'));
          return {id:p.event_id, evidence:p.sentinel_evidence, meanDnbr:p.sentinel_dnbr_mean};
        })()`);
        if (!example) continue;
        await until(`document.querySelector('.event[data-event-id="${example.id}"]') !== null`);
        await evaluate(`document.querySelector('.event[data-event-id="${example.id}"]').click()`);
        await evaluate("document.querySelector('.sentinel-status').scrollIntoView({block:'start'})");
        await until("document.querySelector('#detail .detail-preview')?.complete && document.querySelector('#detail .detail-preview').naturalWidth > 0");
        const shown = await evaluate("Object.fromEntries([...document.querySelector('#detail').querySelectorAll('dt')].map(n=>[n.textContent,n.nextElementSibling.textContent]))");
        ok(shown['Mean dNBR'] === Number(example.meanDnbr).toFixed(3), 'Real Sentinel dNBR did not match exported measurements');
        example.imageLoaded = true;
        report.sentinelPreviews.push(example);
        await pause(250); await capture('sentinel-' + kind + '.png');
        await evaluate("document.querySelector('#reset').click()");
      }
    } else {
      const result = await until("document.querySelector('#browser-check')?.dataset.result", 20000);
      ok(result === "pass", await evaluate("document.querySelector('#browser-check')?.textContent"));
    }
    await evaluate("if (!document.querySelector('#detail').hidden) document.querySelector('#close-detail').click()");
    await pause(350);
    report.hover = await checkHover();
    await capture("horizontal-tooltip.png");
    report.tooltipEdges = await checkTooltipEdges();
    await command("Input.dispatchMouseEvent", {type:"mouseMoved", x:10, y:10});
    await evaluate("document.querySelector('#reset').click(); document.querySelector('#home').click()");
    await pause(350);
    const box = await evaluate("(() => {const r=document.querySelector('#map').getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};})()");
    const before = await evaluate("Number(document.querySelector('#map').dataset.zoom)");
    const buttonStarted = Date.now();
    await evaluate("document.querySelector('.leaflet-control-zoom-in').click()");
    await until(`Number(document.querySelector('#map').dataset.zoom) > ${before}`);
    const buttonSettledMs = Date.now() - buttonStarted;
    await pause(350);
    const afterButton = await evaluate("Number(document.querySelector('#map').dataset.zoom)");
    const wheelStarted = Date.now();
    await command("Input.dispatchMouseEvent", {type: "mouseWheel", x: box.x, y: box.y, deltaX: 0, deltaY: -200});
    await until(`Number(document.querySelector('#map').dataset.zoom) > ${afterButton}`);
    const wheelSettledMs = Date.now() - wheelStarted;
    const beforePan = await evaluate("document.querySelector('.leaflet-map-pane').style.transform");
    await command("Input.dispatchMouseEvent", {type: "mousePressed", x: box.x, y: box.y, button: "left", clickCount: 1});
    for (let step = 1; step <= 5; step++) {await command("Input.dispatchMouseEvent", {type: "mouseMoved", x: box.x + step * 20, y: box.y + step * 5, buttons: 1}); await pause(35);}
    await command("Input.dispatchMouseEvent", {type: "mouseReleased", x: box.x + 100, y: box.y + 25, button: "left", clickCount: 1});
    await pause(350);
    ok(await evaluate("document.querySelector('.leaflet-map-pane').style.transform") !== beforePan, "Real mouse drag did not pan the map");
    report.zoom = {before, afterButton, afterWheel:await evaluate("Number(document.querySelector('#map').dataset.zoom)"), buttonSettledMs, wheelSettledMs};
    report.pan = true;
    await capture("desktop.png");
    await command("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
    await pause(300);
    ok(await evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile layout overflows horizontally");
    await evaluate("document.querySelector('#toggle-sidebar').click()");
    ok(await evaluate("getComputedStyle(document.querySelector('#sidebar')).display !== 'none'"), "Mobile filter panel did not open");
    await capture("mobile.png");
    if (mode === "--production") {
      const cardOpenMs = await evaluate("(() => {const start=performance.now();document.querySelector('.event').click();return performance.now()-start;})()");
      ok(await evaluate("!document.querySelector('#detail').hidden && getComputedStyle(document.querySelector('#sidebar')).display === 'none' && document.documentElement.scrollWidth <= innerWidth"), "Mobile event card or filter dismissal failed");
      await capture("mobile-event-card.png");
      report.mobile = {width:390, height:844, overflow:false, filters:true, eventCard:true, cardOpenMs};
    }
    report.layouts = [];
    for (const [width, height] of [[1920,1080],[1440,900],[1366,768],[1280,720],[1024,768],[800,900],[768,900],[390,844],[320,640],[844,390]]) {
      await command("Emulation.setDeviceMetricsOverride", {width, height, deviceScaleFactor:1, mobile:width<=800});
      await evaluate("document.querySelector('#reset').click(); if (!document.querySelector('#detail').hidden) document.querySelector('#close-detail').click()");
      await pause(180);
      for (const state of ["map", "evidence"]) {
        if (state === "evidence") await evaluate("document.querySelector('.event').click()");
        else await evaluate("document.body.classList.remove('sidebar-open')");
        await pause(180);
        const geometry = await evaluate(`(() => {
          const visible=e=>e && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
          const rect=s=>{const e=document.querySelector(s);if(!visible(e)) return null;const r=e.getBoundingClientRect();return {selector:s,x:r.x,y:r.y,right:r.right,bottom:r.bottom,width:r.width,height:r.height};};
          const regions=['.topbar','.navigation','#sidebar','#map','#detail'].map(rect).filter(Boolean);
          const overlaps=[];
          for(let i=0;i<regions.length;i++) for(let j=i+1;j<regions.length;j++) {
            const a=regions[i],b=regions[j];
            if(Math.min(a.right,b.right)-Math.max(a.x,b.x)>1 && Math.min(a.bottom,b.bottom)-Math.max(a.y,b.y)>1) overlaps.push([a.selector,b.selector]);
          }
          const wideContent=[...document.querySelectorAll('body *')].filter(e=>visible(e) && e.scrollWidth>e.clientWidth+2 && getComputedStyle(e).overflowX==='visible').slice(0,8).map(e=>({tag:e.tagName,id:e.id,class:e.className,width:e.clientWidth,scroll:e.scrollWidth}));
          return {regions,overlaps,layoutWidth:innerWidth,wideContent,overflow:document.documentElement.scrollWidth>innerWidth,
            escaped:regions.filter(r=>r.x<-.5 || r.y<-.5 || r.right>innerWidth+.5 || r.bottom>innerHeight+.5)};
        })()`);
        ok(geometry.layoutWidth===width && !geometry.overflow && !geometry.escaped.length, `${width}x${height} ${state}: a workspace panel exceeds the viewport: ${JSON.stringify(geometry)}`);
        ok(!geometry.overlaps.length, `${width}x${height} ${state}: workspace panels overlap: ${JSON.stringify(geometry.overlaps)}`);
        if (state === "map") {
          ok(geometry.regions.some(r=>r.selector==="#map" && r.height>=80), `${width}x${height}: map is unusably small`);
          ok(await evaluate("document.querySelector('#legend').getBoundingClientRect().height > 0 && [...document.querySelectorAll('#legend .legend-item')].every(e=>e.checkVisibility())"),
            `${width}x${height}: context legend is hidden or collapsed`);
        }
        if (state === "evidence") ok(geometry.regions.some(r=>r.selector==="#detail"), "Evidence view disappeared");
        report.layouts.push({width,height,state,overlap:false,overflow:false});
        if (screenshots && [[1440,900],[390,844],[320,640],[844,390]].some(([w,h])=>w===width && h===height)) await capture(`layout-${width}-${height}-${state}.png`);
      }
      await evaluate("document.querySelector('#close-detail').click()");
      ok(await evaluate("document.querySelector('#toggle-sidebar').getAttribute('aria-expanded') === String(getComputedStyle(document.querySelector('#sidebar')).display !== 'none')"),
        `${width}x${height}: Events accessibility state does not match sidebar visibility after closing evidence`);
    }
    ok(await evaluate("document.querySelector('#profile')?.disabled && document.querySelector('#profile').textContent.includes('Not enabled')"), "Profile must be clearly unavailable, not a fake active account");
    ok(await evaluate("[...document.fonts].some(f=>f.family==='Sora' && f.status==='loaded')"), "Local design font did not load offline");
    if (mode === "--production") console.log(JSON.stringify(report, null, 2));
    console.log("PASS: real-clock browser, animated zoom, wheel zoom, drag pan, mobile layout");
  } finally {
    if (command && socket?.readyState === 1) await command("Browser.close").catch(() => {});
    socket?.close();
    if (child.exitCode === null) {await Promise.race([new Promise(resolve => child.once("exit", resolve)), pause(2000)]); if (child.exitCode === null) child.kill();}
  }
})().catch(error => {console.error(error.message); process.exitCode = 1;});
