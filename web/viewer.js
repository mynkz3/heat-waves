"use strict";

const Evidence = (() => {
  const DAY = 86400000;
  const classes = {
    suspected_abnormal_industrial_associated_event: {label: "Unusual · industrial context", short: "Industrial review", color: "#dc2626"},
    routine_persistent_industrial_thermal_source: {label: "Recurring industrial heat", short: "Recurring heat", color: "#0f766e"},
    industrial_associated_unscored: {label: "Industrial context · unscored", short: "Industrial · unscored", color: "#5b8c89"},
    suspected_abnormal_mining_associated_event: {label: "Unusual · mining/quarry context", short: "Mining review", color: "#d97706"},
    mining_associated_thermal_source: {label: "Recurring mining/quarry heat", short: "Recurring mining", color: "#80603c"},
    mining_associated_unscored: {label: "Mining/quarry context · unscored", short: "Mining · unscored", color: "#b7a178"},
    likely_vegetation_fire: {label: "Vegetation-associated heat", short: "Vegetation", color: "#166534"},
    mixed_or_unknown: {label: "Unresolved context", short: "Unresolved", color: "#6b7280"},
  };
  function number(value) {
    if (value == null || (typeof value === "string" && !value.trim()) || typeof value === "boolean") return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  function timestamp(value) {
    if (!value) return NaN;
    let text = String(value).trim();
    if (/^\d{4}-\d\d-\d\d[T ]\d\d:\d\d/.test(text) && !/(Z|[+-]\d\d:?\d\d)$/i.test(text)) text += "Z";
    return Date.parse(text);
  }
  function dateWindow(preset, options = {}) {
    const now = options.now ?? Date.now();
    const end = options.replay && Number.isFinite(options.latest) ? options.latest : now;
    if (preset === "all") return {start: -Infinity, end: Infinity, valid: true};
    if (preset === "custom") {
      const validDay = value => /^\d{4}-\d{2}-\d{2}$/.test(value || "") && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
      if (!validDay(options.from) || !validDay(options.to)) return {valid: false};
      const start = Date.parse(options.from + "T00:00:00+05:30");
      const finish = Date.parse(options.to + "T00:00:00+05:30") + DAY - 1;
      return {start, end: finish, valid: start <= finish};
    }
    const days = {day: 1, week: 7, month: 30, year: 365}[preset] ?? 1;
    return {start: end - days * DAY, end, valid: true};
  }
  function anomalyBand(value) {
    const score = number(value);
    if (score === null) return {label: "Insufficient data", key: "unknown"};
    if (score >= 0.99) return {label: "Highest priority", key: "critical"};
    if (score >= 0.95) return {label: "High priority", key: "high"};
    if (score >= 0.8) return {label: "Moderate priority", key: "elevated"};
    return {label: "Lower priority", key: "typical"};
  }
  function matchesEvent(feature, filters) {
    const p = feature.properties || {};
    if (filters.decision !== "all" && p.decision !== filters.decision) return false;
    const score = number(p.anomaly_score);
    if (filters.availability === "scored" && score === null) return false;
    if (filters.availability === "unscored" && score !== null) return false;
    if (score !== null && score < filters.minScore) return false;
    const query = (filters.query || "").trim().toLowerCase();
    if (query && ![p.event_id, p.event_uid, p.site_id, p.nearest_facility_name, p.nearest_facility_id, p.context_label, p.source_context, p.score_status, p.decision].filter(Boolean).join(" ").toLowerCase().includes(query)) return false;
    const span = filters.time;
    if (span.valid === false) return false;
    if (span.start === -Infinity && span.end === Infinity) return true;
    let start = timestamp(p.start_time), end = timestamp(p.end_time);
    if (!Number.isFinite(start)) start = end;
    if (!Number.isFinite(end)) end = start;
    return Number.isFinite(start) && end >= span.start && start <= span.end;
  }
  const sentinelQuantitativeAllowed = p => p.sentinel_status === "analysed" && p.sentinel_radiometry_status !== "legacy_offset_unverified";
  function safePreviewPath(value) {
    if (typeof value !== "string") return null;
    let relative;
    try {relative = decodeURIComponent(value).replace(/\\/g, "/").trim();} catch {return null;}
    if (/^(?:\/|[a-z][a-z0-9+.-]*:)/i.test(relative) || /[\u0000-\u001f%]/.test(relative) || relative.split("/").includes("..") || !/\.(?:png|jpe?g|webp)$/i.test(relative)) return null;
    return relative;
  }
  function comparisonDetails(p) {
    const peer = p.score_method === "vegetation_peer" || (p.peer_status && p.peer_status !== "not_applicable" && number(p.anomaly_score) === null);
    return peer ? {name: "Vegetation peers", references: p.peer_reference_count, features: p.peer_feature_count,
      start: p.peer_reference_start, end: p.peer_reference_end} :
      {name: "Same-site history", references: p.history_event_count, features: p.model_feature_count,
        start: p.baseline_start, end: p.baseline_end};
  }
  function coverageWarning(audit) {
    const missing = audit.missing_expected_years || [];
    return (missing.length ? `Missing requested archive years: ${missing.join(", ")}. ` : "") +
      "Coverage is not continuous; missing observations are not evidence of no fires.";
  }
  async function loadLocalJSON(path, fetcher = fetch) {
    if (typeof path !== "string" || !/^data\/(?:[a-z0-9_-]+\/)*[a-z0-9_-]+\.json$/i.test(path)) {
      throw new Error("Invalid local data path");
    }
    const response = await fetcher(path, {cache: "no-store", credentials: "same-origin", redirect: "error", signal: AbortSignal.timeout(20000)});
    if (!response.ok) throw new Error(`Local data could not be read (${response.status}): ${path}`);
    return response.json();
  }
  function createDetailLoader(fetcher = fetch) {
    const shards = new Map();
    return async feature => {
      if (!feature.detail) return feature;
      const {url, index} = feature.detail;
      let pending = shards.get(url);
      if (!pending) {
        pending = loadLocalJSON(url, fetcher).catch(error => {shards.delete(url); throw error;});
        shards.set(url, pending);
        // Keep repeated selections cheap without retaining the whole archive.
        if (shards.size > 8) shards.delete(shards.keys().next().value);
      }
      const payload = await pending;
      const detail = Number.isInteger(index) && index >= 0 ? payload.features?.[index] : null;
      if (detail?.type !== "Feature" || !detail.geometry || detail.properties?.event_id !== feature.properties?.event_id) {
        throw new Error("Saved event details do not match this snapshot");
      }
      return detail;
    };
  }
  return {classes, number, timestamp, dateWindow, anomalyBand, matchesEvent, sentinelQuantitativeAllowed, safePreviewPath, comparisonDetails, coverageWarning, loadLocalJSON, createDetailLoader};
})();

if (typeof module !== "undefined") module.exports = Evidence;

function startWorkspace(data) {
  const {classes, number, timestamp, dateWindow, anomalyBand, matchesEvent} = Evidence;
  const $ = selector => document.querySelector(selector);
  if (data.events?.type !== "FeatureCollection" || !Array.isArray(data.events.features) ||
      data.facilities?.type !== "FeatureCollection" || !Array.isArray(data.facilities.features)) {
    throw new Error("The saved workspace payload is missing its event or facility collection");
  }
  const metadata = data.metadata || {}, audit = metadata.input_audit || {};
  const rawEvents = data.events?.features || [];
  const validPoint = feature => feature.geometry?.type === "Point" && feature.geometry.coordinates?.length >= 2 && feature.geometry.coordinates.slice(0, 2).every(value => typeof value === "number" && Number.isFinite(value)) && Math.abs(feature.geometry.coordinates[0]) <= 180 && Math.abs(feature.geometry.coordinates[1]) <= 90;
  const events = rawEvents.filter(validPoint), facilities = data.facilities?.features || [];
  const palette = decision => classes[decision] || classes.mixed_or_unknown;
  const human = value => String(value ?? "").replace(/_/g, " ");
  const count = value => Number(value).toLocaleString("en-IN");
  const finite = (value, digits = 1) => number(value) === null ? null : Number(value).toFixed(digits);
  const percent = value => number(value) === null ? null : `${(Number(value) * 100).toFixed(1)}%`;
  const formatTime = (value, zone = "Asia/Kolkata") => {
    const time = typeof value === "number" ? value : timestamp(value);
    if (!Number.isFinite(time)) return "Not recorded";
    return new Intl.DateTimeFormat("en-GB", {timeZone: zone, day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).format(time) + (zone === "UTC" ? " UTC" : " IST");
  };
  function make(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function facts(container, values) {
    const dl = make("dl", null, "facts");
    for (const [label, value] of values) {
      if (value == null || value === "" || value === "Not recorded") continue;
      dl.append(make("dt", label), make("dd", Array.isArray(value) ? value.join(", ") : value));
    }
    if (dl.children.length) container.append(dl);
  }
  function section(container, title, description) {
    const block = make("section", null, "detail-section");
    block.append(make("h3", title));
    if (description) block.append(make("p", description));
    container.append(block);
    return block;
  }
  const acquisitionTimes = events.flatMap(f => [timestamp(f.properties?.start_time), timestamp(f.properties?.end_time)]).filter(Number.isFinite);
  // Reductions avoid exceeding JavaScript's argument limit with a large archive.
  const latest = Number.isFinite(timestamp(audit.date_max)) ? timestamp(audit.date_max) : acquisitionTimes.reduce((a, b) => Math.max(a, b), -Infinity);
  const earliest = Number.isFinite(timestamp(audit.date_min)) ? timestamp(audit.date_min) : acquisitionTimes.reduce((a, b) => Math.min(a, b), Infinity);
  const bbox = data.aoi?.bbox || [81.5, 23.2, 83.5, 25.2];
  const homeBounds = [[bbox[1], bbox[0]], [bbox[3], bbox[2]]];
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const map = L.map("map", {preferCanvas: true, zoomControl: false, minZoom: 4, maxZoom: 19, zoomSnap: 0.5, zoomAnimation: !reducedMotion, fadeAnimation: !reducedMotion});
  map.fitBounds(homeBounds, {padding: [28, 28]});
  L.control.zoom({position: "bottomright"}).addTo(map);
  L.control.scale({position: "bottomleft", imperial: false, maxWidth: 100}).addTo(map);
  map.on("tooltipopen", ({tooltip}) => {
    const element = tooltip.getElement(), bounds = map.getContainer().getBoundingClientRect();
    element.classList.remove("tooltip-clamped");
    element.style.maxWidth = `${Math.min(280, bounds.width - 16)}px`;
    tooltip.update();
    const rect = element.getBoundingClientRect();
    const dx = Math.max(bounds.left + 8 - rect.left, Math.min(0, bounds.right - 8 - rect.right));
    const dy = Math.max(bounds.top + 8 - rect.top, Math.min(0, bounds.bottom - 8 - rect.bottom));
    if (dx || dy) {
      L.DomUtil.setPosition(element, L.DomUtil.getPosition(element).add([dx, dy]));
      element.classList.add("tooltip-clamped");
    }
  });
  map.on("movestart zoomstart resize", () => {
    map.eachLayer(layer => {if (layer instanceof L.Tooltip) layer.close();});
  });
  map.attributionControl.addAttribution('© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a>');
  const roadRenderer = L.canvas({padding: 0.3}), eventRenderer = L.canvas({padding: 0.3});
  L.rectangle(homeBounds, {color: "#79978a", weight: 1, opacity: 0.55, fill: false, dashArray: "5 6", interactive: false, renderer: roadRenderer}).addTo(map);
  const roads = L.geoJSON(data.roads, {renderer: roadRenderer, interactive: false, style: feature => ({color: /motorway|trunk|primary/.test(feature.properties?.highway || "") ? "#b3ad91" : "#c5c6b6", weight: /motorway|trunk|primary/.test(feature.properties?.highway || "") ? 2.1 : 1.1, opacity: 0.8})}).addTo(map);
  const eventLayer = L.layerGroup().addTo(map), selectionLayer = L.layerGroup().addTo(map);
  const facilityLayer = L.geoJSON(data.facilities, {
    renderer: eventRenderer,
    style: {color: "#ae8a45", fillColor: "#c09a51", fillOpacity: 0.12, weight: 1.2},
    pointToLayer: (feature, latlng) => L.circleMarker(latlng, {renderer: eventRenderer, radius: 4, color: "#a07e3b", fillColor: "#fffefa", fillOpacity: 0.9, weight: 1.5}),
    onEachFeature: (feature, layer) => {
      layer.bindTooltip(make("span", feature.properties?.name || "Mapped facility"));
      layer.on("click", () => openFacility(feature));
    },
  }).addTo(map);
  const tileLayer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom: 19, updateWhenIdle: true, keepBuffer: 1});
  const tilesAllowed = location.protocol === "http:" || location.protocol === "https:";
  let tileErrors = 0;
  let cachedRoads = !!data.roads?.features?.length, roadsPending = null;
  function basemapLabel(text) {$("#basemap-status").textContent = text;}
  function offlineLabel(prefix = "Offline") {
    basemapLabel(`${prefix} · ${cachedRoads ? "cached OSM roads" : roadsPending ? "loading cached roads…" : "no road cache available"}`);
  }
  function loadRoads() {
    if (!data.roads_url || roadsPending || data.roads) return;
    roadsPending = Evidence.loadLocalJSON(data.roads_url).then(payload => {
      if (payload.type !== "FeatureCollection" || !Array.isArray(payload.features)) throw new Error("Invalid road collection");
      data.roads = payload;
      roads.addData(payload);
      cachedRoads = !!payload.features.length;
      if (!$("#show-roads").checked) roads.remove();
      roadsPending = null;
      if (!map.hasLayer(tileLayer)) offlineLabel();
    }).catch(error => {
      roadsPending = null;
      $("#tile-note").textContent = `Cached roads could not be loaded: ${error.message}. Events remain available. Toggle Cached roads to retry.`;
      if (!map.hasLayer(tileLayer)) basemapLabel("Offline · cached roads unavailable; events remain");
    });
  }
  function setTiles() {
    tileErrors = 0;
    if (!tilesAllowed) $("#show-tiles").checked = false;
    if ($("#show-tiles").checked) {tileLayer.addTo(map); basemapLabel("Connecting to online OSM tiles…");}
    else {tileLayer.remove(); offlineLabel();}
  }
  tileLayer.on("tileload", () => {if (map.hasLayer(tileLayer)) basemapLabel("OSM road map · online tiles");});
  tileLayer.on("tileerror", () => {
    if (++tileErrors >= 3) {
      tileLayer.remove(); $("#show-tiles").checked = false;
      offlineLabel("Tiles unavailable");
    }
  });
  $("#show-tiles").checked = tilesAllowed && navigator.onLine;
  $("#show-tiles").disabled = !tilesAllowed;
  if (!tilesAllowed) $("#tile-note").textContent = "Online OSM tiles require localhost or a hosted web page, not a file opened directly. Cached roads and events work offline.";
  $("#show-tiles").addEventListener("change", setTiles);
  setTiles();
  $("#show-roads").addEventListener("change", () => {
    if ($("#show-roads").checked) {roads.addTo(map); loadRoads();}
    else roads.remove();
  });
  $("#show-facilities").addEventListener("change", () => $("#show-facilities").checked ? facilityLayer.addTo(map) : facilityLayer.remove());
  $("#aoi-name").textContent = data.aoi?.name || "Pilot region";
  $("#snapshot-date").textContent = `Saved ${formatTime(metadata.generated_at, "UTC")} · not a live feed`;
  $("#total-count").textContent = count(events.length);
  $("#vegetation-count").textContent = count(events.filter(f => f.properties?.decision === "likely_vegetation_fire").length);
  $("#review-count").textContent = count(events.filter(f => ["suspected_abnormal_industrial_associated_event", "suspected_abnormal_mining_associated_event"].includes(f.properties?.decision)).length);
  for (const [key, value] of Object.entries(classes)) {
    const option = make("option", value.label); option.value = key; $("#filter").append(option);
    const item = make("span", null, "legend-item"), dot = make("span", null, "legend-dot");
    dot.style.background = value.color; item.append(dot, document.createTextNode(value.short)); $("#legend").append(item);
  }
  for (const [id, value] of [["#date-from", earliest], ["#date-to", latest]]) {
    if (Number.isFinite(value)) $(id).value = new Date(value + 19800000).toISOString().slice(0, 10);
  }
  let filtered = [], pageSize = 60, selected = null, detailRequest = 0;
  const loadDetail = Evidence.createDetailLoader();
  function markSelection() {
    selectionLayer.clearLayers();
    if (selected) L.circleMarker([selected.geometry.coordinates[1], selected.geometry.coordinates[0]], {renderer: eventRenderer, radius: 11, color: "#263f47", weight: 2, fill: false, interactive: false}).addTo(selectionLayer);
  }
  function drawMap() {
    eventLayer.clearLayers();
    const zoom = map.getZoom(), view = map.getBounds().pad(0.15), buckets = new Map();
    const cluster = zoom < 11 && filtered.length > 1200;
    let visible = 0;
    for (const [index, feature] of filtered.entries()) {
      const [lon, lat] = feature.geometry.coordinates, ll = L.latLng(lat, lon);
      if (!view.contains(ll)) continue;
      visible++;
      const pixel = cluster ? map.project(ll, zoom) : null;
      const key = cluster ? `${Math.floor(pixel.x / 25)}:${Math.floor(pixel.y / 25)}:${feature.properties?.decision}` : index;
      let group = buckets.get(key);
      if (!group) {group = {feature, count: 0, lat: 0, lon: 0}; buckets.set(key, group);}
      group.count++; group.lat += lat; group.lon += lon;
    }
    for (const group of buckets.values()) {
      const p = group.feature.properties || {}, style = palette(p.decision);
      const center = [group.lat / group.count, group.lon / group.count];
      const marker = L.circleMarker(center, {renderer: eventRenderer, radius: group.count > 1 ? Math.min(13, 5 + Math.log2(group.count)) : Math.min(6, 3.4 + Math.log2(1 + (number(p.detection_count) || 1)) * 0.35), color: "#fffefa", weight: 0.7, fillColor: style.color, fillOpacity: group.count > 1 ? 0.8 : 0.76, bubblingMouseEvents: false});
      marker.bindTooltip(make("span", group.count > 1 ? `${count(group.count)} ${style.short.toLowerCase()} observations · click to zoom` : `${p.event_id || "Event"} · ${style.label}`));
      marker.on("click", () => group.count > 1 ? map.setView(center, Math.min(13, zoom + 2)) : openEvent(group.feature));
      marker.addTo(eventLayer);
    }
    $("#map").dataset.visibleEvents = String(visible);
    $("#map").dataset.renderedGroups = String(buckets.size);
    $("#map").dataset.zoom = String(zoom);
    $("#grouping-note").textContent = `${count(visible)} in view · ${cluster && visible > buckets.size ? "grouped at this zoom; click to explore" : "event locations, not fire perimeters"}`;
    markSelection();
  }
  function renderList() {
    const fragment = document.createDocumentFragment();
    for (const feature of filtered.slice(0, pageSize)) {
      const p = feature.properties || {}, context = palette(p.decision), band = anomalyBand(p.anomaly_score);
      const button = make("button", null, "event");
      button.style.setProperty("--event-color", context.color);
      button.dataset.eventId = p.event_id || "";
      button.setAttribute("aria-pressed", String(selected === feature));
      const top = make("div", null, "card-top");
      top.append(make("span", context.short, "card-context"), make("span", band.label, "band " + band.key));
      const name = p.nearest_facility_name || (p.site_id ? `Persistent site ${p.site_id}` : context.label);
      const meta = make("div", null, "card-meta");
      meta.append(make("span", formatTime(p.start_time || p.end_time)));
      const bottom = make("div", null, "card-bottom");
      bottom.append(make("span", p.event_id || "Unidentified event"), make("span", `${count(number(p.detection_count) || 1)} detections${p.frp_peak_mw == null ? "" : " · " + finite(p.frp_peak_mw) + " MW"}`));
      button.append(top, make("h3", name), meta, bottom);
      button.addEventListener("click", () => openEvent(feature));
      fragment.append(button);
    }
    if (!filtered.length) fragment.append(make("p", "No matching events. Adjust the time window or reset the filters.", "list-note"));
    $("#events-list").replaceChildren(fragment);
    $("#load-more").hidden = filtered.length <= pageSize;
    $("#load-more").textContent = `Show next ${Math.min(60, Math.max(0, filtered.length - pageSize))} events`;
  }
  function renderFacilities() {
    const query = $("#search").value.trim().toLowerCase();
    const matching = facilities.filter(f => !query || [f.properties?.name, f.properties?.facility_id, f.properties?.kind].join(" ").toLowerCase().includes(query));
    $("#facility-count").textContent = `(${count(matching.length)})`;
    const fragment = document.createDocumentFragment();
    for (const feature of matching.slice(0, 40)) {
      const button = make("button", feature.properties?.name || feature.properties?.facility_id || "Mapped facility", "facility");
      button.addEventListener("click", () => openFacility(feature)); fragment.append(button);
    }
    if (matching.length > 40) fragment.append(make("p", "Showing the first 40 facilities. Search above to narrow the list.", "micro"));
    $("#facilities-list").replaceChildren(fragment);
  }
  function freshness(span) {
    const preset = $("#period").value, replay = $("#replay").checked && preset !== "all" && preset !== "custom";
    const validLatest = Number.isFinite(latest), age = Date.now() - latest, recent = validLatest && age >= 0 && age <= 86400000;
    const state = $("#freshness-state");
    state.textContent = replay ? "HISTORICAL REPLAY" : preset === "all" ? "HISTORY" : preset === "custom" ? "CUSTOM RANGE" : recent ? "RECENT DATA · SNAPSHOT" : "HISTORICAL DATA";
    state.className = "state-badge" + (replay ? " replay" : recent && preset !== "all" ? " recent" : "");
    const labels = {all: "All available history", day: "Latest 24 hours", week: "Last 7 days", month: "Last 30 days", year: "Last 365 days", custom: "Custom date range"};
    $("#window-title").textContent = labels[preset] + (replay ? " · replay" : "");
    const dateOnly = value => Number.isFinite(value) ? new Intl.DateTimeFormat("en-GB", {timeZone: "Asia/Kolkata", day: "2-digit", month: "short", year: "numeric"}).format(value) : "Unknown date";
    $("#window-caption").textContent = preset === "all" ? `${dateOnly(earliest)} – ${dateOnly(latest)} · acquisition range` : span.valid === false ? "Choose a valid date range · IST" : `${dateOnly(span.start)} – ${dateOnly(span.end)} · IST${replay ? " · not current time" : ""}`;
    let note;
    if (!validLatest) note = "No valid acquisition timestamp is recorded. Freshness cannot be established.";
    else if (age < -300000) note = `Latest acquisition is future-dated: ${formatTime(latest)}. Check source clocks; this is not a live alert.`;
    else {
      const lag = age >= 172800000 ? `${Math.floor(age / 86400000)} days old` : age >= 3600000 ? `${Math.floor(age / 3600000)} hours old` : `${Math.max(0, Math.floor(age / 60000))} minutes old`;
      note = `Latest acquisition: ${formatTime(latest)} · ${lag}. Saved snapshot, not a live feed.`;
    }
    note += " " + Evidence.coverageWarning(audit);
    $("#freshness-message").textContent = note;
    $("#freshness-message").classList.toggle("recent", recent && !replay);
    $("#empty-state").hidden = filtered.length > 0;
    $("#empty-reason").textContent = span.valid === false ? "Choose valid start and end dates. Custom ranges include the whole end day in India Standard Time." : `${validLatest ? "Last available acquisition: " + formatTime(latest) + ". " : "No dated observations are loaded. "}There are no records matching this time window and these filters. ${Evidence.coverageWarning(audit)}`;
  }
  function render() {
    const preset = $("#period").value;
    $("#custom-dates").hidden = preset !== "custom";
    const span = dateWindow(preset, {now: Date.now(), latest, replay: $("#replay").checked, from: $("#date-from").value, to: $("#date-to").value});
    const filters = {decision: $("#filter").value, availability: $("#score-availability").value, minScore: Number($("#score").value), query: $("#search").value, time: span};
    filtered = events.filter(feature => matchesEvent(feature, filters));
    const sort = $("#sort").value;
    filtered.sort((a, b) => {
      const ap = a.properties || {}, bp = b.properties || {};
      if (sort === "score") return (number(bp.anomaly_score) ?? -1) - (number(ap.anomaly_score) ?? -1);
      if (sort === "detections") return (number(bp.detection_count) ?? 0) - (number(ap.detection_count) ?? 0);
      return (timestamp(bp.start_time) || 0) - (timestamp(ap.start_time) || 0);
    });
    if (selected && !filtered.includes(selected)) closeDetail(false);
    $("#count").textContent = `${count(filtered.length)} events shown`;
    const scoredCount = filtered.filter(f => number(f.properties?.anomaly_score) !== null).length;
    $("#score-summary").textContent = `${count(scoredCount)} scored · ${count(filtered.length - scoredCount)} insufficient data`;
    $("#fit").disabled = !filtered.length;
    $("#score-value").textContent = filters.minScore ? filters.minScore.toFixed(2) + " · " + anomalyBand(filters.minScore).label : "Any";
    $("#score").setAttribute("aria-valuetext", $("#score-value").textContent);
    freshness(span); renderList(); renderFacilities(); drawMap();
  }
  function showInspector(title) {
    $("#detail-title").textContent = title;
    $("#detail-title").tabIndex = -1;
    $("#detail-body").replaceChildren();
    $("#detail").hidden = false;
    $(".map-stage").classList.add("has-detail");
    $(".workspace").classList.add("has-detail");
    document.body.classList.remove("sidebar-open");
    $("#toggle-sidebar").setAttribute("aria-expanded", "false");
    selectNavigation(false);
    $("#detail-title").focus({preventScroll: true});
    return $("#detail-body");
  }
  function closeDetail(restoreFocus = true) {
    const id = selected?.properties?.event_id;
    selected = null; $("#detail").hidden = true;
    $(".map-stage").classList.remove("has-detail");
    $(".workspace").classList.remove("has-detail");
    selectNavigation(false);
    markSelection(); renderList();
    if (restoreFocus) ([...document.querySelectorAll(".event")].find(button => button.dataset.eventId === id) || $("#map")).focus({preventScroll: true});
  }
  const unit = (value, suffix, digits = 1) => number(value) === null ? null : `${finite(value, digits)} ${suffix}`;
  function openFacility(feature) {
    selected = null; markSelection(); renderList();
    const p = feature.properties || {}, body = showInspector("Mapped facility");
    body.append(make("p", p.name || "Unnamed facility", "detail-location"));
    const context = section(body, "Mapped context, not an incident");
    facts(context, [["Facility ID", p.facility_id], ["Kind", human(p.kind)], ["Source", p.source || "OpenStreetMap"], ["OSM ID", p.osm_id]]);
    context.append(make("p", "Facility mapping may be incomplete or outdated. A feature on this map does not mean a fire has occurred there.", "detail-note"));
    const bounds = L.geoJSON(feature).getBounds();
    if (bounds.isValid()) map.panTo(bounds.getCenter(), {animate: !reducedMotion, duration: 0.2});
  }
  function openEvent(feature) {
    selected = feature;
    const request = ++detailRequest;
    if (!feature.detail) return renderEvent(feature);
    const body = showInspector(feature.properties?.event_id || "Thermal event");
    const loading = make("p", "Loading saved event evidence…", "detail-loading");
    loading.setAttribute("role", "status");
    body.append(loading);
    markSelection(); renderList();
    loadDetail(feature).then(detail => {
      if (selected === feature && detailRequest === request) renderEvent(detail);
    }).catch(error => {
      if (selected !== feature || detailRequest !== request) return;
      const message = make("p", `Event evidence could not be loaded: ${error.message}. Keep the complete site data folder beside this page.`, "detail-note");
      message.setAttribute("role", "alert");
      const retry = make("button", "Retry event evidence");
      retry.addEventListener("click", () => openEvent(feature));
      body.replaceChildren(message, retry);
    });
  }
  function renderEvent(feature) {
    const p = feature.properties || {}, context = palette(p.decision), band = anomalyBand(p.anomaly_score);
    const body = showInspector(p.event_id || "Thermal event");
    const heading = make("div", null, "detail-context"), category = make("strong", context.label);
    heading.append(category, make("span", band.label, "band " + band.key)); body.append(heading);
    if (p.nearest_facility_name) body.append(make("p", p.nearest_facility_name, "detail-location"));
    const [lon, lat] = feature.geometry.coordinates;
    body.append(make("p", `${lat.toFixed(5)}° N, ${lon.toFixed(5)}° E · event location`, "detail-coords"));
    const observations = section(body, "Satellite observations"), metrics = make("div", null, "metric-grid");
    for (const [value, label] of [[number(p.detection_count), "detections grouped"], [unit(p.frp_peak_mw, "MW"), "peak radiative power"]]) {
      if (value === null) continue;
      const metric = make("div", null, "metric"); metric.append(make("strong", value), make("span", label)); metrics.append(metric);
    }
    observations.append(metrics);
    const times = section(body, "Acquisition & source");
    facts(times, [["First detection · IST", formatTime(p.start_time)], ["First detection · UTC", formatTime(p.start_time, "UTC")],
      ["Last detection · IST", p.end_time !== p.start_time ? formatTime(p.end_time) : null],
      ["Last detection · UTC", p.end_time !== p.start_time ? formatTime(p.end_time, "UTC") : null],
      ["Observed sensors", p.observed_sensors || p.detected_sensors || p.sensor_group], ["VIIRS detections", p.viirs_count], ["MODIS detections", p.modis_count],
      ["Incident verification", human(p.incident_verification || "unverified")],
      ["Data quality", human(p.data_quality)], ["Largest scan footprint", unit(p.max_scan_km, "km", 2)], ["Largest track footprint", unit(p.max_track_km, "km", 2)],
      ["Processing", {standard: "Standard-processing archive", nrt: "Near-real-time product", mixed: "Mixed archive / near-real-time"}[p.product_status] || human(p.product_status)],
      ["Observation span", unit(p.duration_hours, "hours")], ["Spatial spread", unit(p.spread_km, "km", 2)], ["Night-time detections", percent(p.night_fraction)]]);
    times.append(make("p", "Times describe satellite detections, not verified ignition or continuous burning. FRP is radiative power, not surface temperature.", "detail-note"));
    const surroundings = section(body, "Independent context");
    facts(surroundings, [["Source group", p.source_context === "mining_quarry" ? "Mining / quarry (mapped context)" : human(p.source_context)], ["Context evidence", human(p.context_label)], ["Nearest mapped facility", p.nearest_facility_name],
      ["Facility kind", human(p.nearest_facility_kind)], ["Mapped-facility distance", unit(p.facility_distance_km, "km", 2)],
      ["Land cover", p.worldcover_dominant], ["Land-cover vintage", p.worldcover_year || p.landcover_reference_year],
      ["Vegetation cover", percent(p.vegetation_fraction)], ["Built-up cover", percent(p.built_fraction)], ["Bare-ground cover", percent(p.bare_fraction)],
      ["Persistent site", p.site_id], ["Context conflict", typeof p.context_conflict === "boolean" ? (p.context_conflict ? "Conflicting context evidence" : null) : human(p.context_conflict)], ["Decision basis", human(p.decision_basis)]]);
    surroundings.append(make("p", "Context and anomaly are separate. A nearby facility does not establish industrial causation; vegetation context is not a confirmed forest fire." + (p.worldcover_year ? ` Land cover is from ${p.worldcover_year}, not current conditions.` : ""), "detail-note"));
    if (p.source_context === "mining_quarry") surroundings.append(make("p", "The mapped feature is a quarry. This supports mining context, not confirmation of a mining accident or an industrial asset fire.", "detail-note"));
    const model = section(body, "Anomaly evidence · triage only");
    if (number(p.anomaly_score) === null) {
      model.append(make("p", "Insufficient data for anomaly scoring. No model score is available for this event; it has not been assigned a zero."));
      model.append(make("p", p.score_reason || "The saved output does not record why scoring is unavailable.", "score-explanation"));
    }
    else {
      const score = make("div", null, "metric"); score.append(make("strong", finite(p.anomaly_score, 3) + " / 1"), make("span", band.label + " relative to the comparison history")); model.append(score);
    }
    const comparison = Evidence.comparisonDetails(p), peerComparison = comparison.name === "Vegetation peers";
    facts(model, [["Scoring status", human(p.score_status)], ["Comparison method", comparison.name],
      [peerComparison ? "Earlier peer episodes" : "Historical comparison events", comparison.references], ["Model features used", comparison.features],
      ["Baseline starts · UTC", comparison.start ? formatTime(comparison.start, "UTC") : null],
      ["Baseline ends · UTC", comparison.end ? formatTime(comparison.end, "UTC") : null],
      ["Model sensor group", p.model_sensor], ["Model day/night group", human(p.model_daynight)]]);
    if (peerComparison) {
      facts(model, [["Peer group", human(p.peer_group)], ["Distinct peer dates", p.peer_reference_days],
        ["Distinct peer cells", p.peer_reference_cells], ["Comparison radius", unit(p.peer_radius_km, "km", 0)],
        ["Season match", unit(p.peer_season_window_days, "days either side", 0)], ["Historical exclusion gap", unit(p.peer_gap_days, "days", 0)]]);
      model.append(make("p", "Compared with earlier detected vegetation episodes matching land cover, season, satellite and day/night phase. Uses peak FRP, brightness contrast and peak detection count. Same-site recurrence is not required. This ranks detected heat episodes, not the likelihood of a vegetation fire.", "detail-note"));
    } else facts(model, [["Calibration events", p.calibration_event_count], ["Baseline mode", human(p.baseline_mode)],
      ["Robust anomaly percentile", percent(p.robust_anomaly_percentile)], ["Isolation anomaly percentile", percent(p.isolation_anomaly_percentile)]]);
    if (p.anomaly_reasons) {
      if (Array.isArray(p.anomaly_reasons)) {
        const list = make("ul"); p.anomaly_reasons.forEach(reason => list.append(make("li", reason))); model.append(list);
      } else model.append(make("p", p.anomaly_reasons, "detail-note"));
    }
    model.append(make("p", "The score ranks unusual behaviour in the available history. It is not the probability of a fire, its cause, or its severity.", "detail-note"));
    addSentinel(body, p);
    map.panTo([lat, lon], {animate: !reducedMotion, duration: 0.2}); markSelection(); renderList();
  }
  function addSentinel(body, p) {
    const quantitative = Evidence.sentinelQuantitativeAllowed(p);
    const unverified = p.sentinel_radiometry_status === "legacy_offset_unverified";
    const labels = {analysed: "Image comparison available", cloud_obscured: "Cloud-obscured comparison", waiting_for_post_image: "Waiting for post-event imagery", no_catalogue_match: "No matching catalogue scene", download_failed: "Image download failed", queued: "Queued for comparison", disabled: "Image analysis disabled"};
    const sentinel = section(body, "Sentinel-2 corroboration");
    sentinel.append(make("strong", unverified ? "Qualitative image context only" : labels[p.sentinel_status] || "Not assessed", "sentinel-status"));
    if (unverified) sentinel.append(make("p", "Reflectance offsets were not verified for these scenes. Quantitative spectral-change claims are withheld.", "detail-note"));
    if (p.sentinel_reason) sentinel.append(make("p", p.sentinel_reason));
    if (!unverified && p.sentinel_evidence && p.sentinel_evidence !== p.sentinel_reason) sentinel.append(make("p", human(p.sentinel_evidence), "detail-note"));
    facts(sentinel, [["Before image · UTC", p.sentinel_pre_datetime ? formatTime(p.sentinel_pre_datetime, "UTC") : null],
      ["After image · UTC", p.sentinel_post_datetime ? formatTime(p.sentinel_post_datetime, "UTC") : null],
      ["Radiometry", human(p.sentinel_radiometry_status)], ["Joint valid pixels", percent(p.sentinel_joint_valid_fraction)], ["Median dNBR", quantitative ? finite(p.sentinel_dnbr_median, 3) : null],
      ["Mean dNBR", quantitative ? finite(p.sentinel_dnbr_mean, 3) : null], ["Burn-like change fraction", quantitative ? percent(p.sentinel_affected_fraction) : null],
      ["SWIR anomaly fraction", quantitative ? percent(p.sentinel_swir_affected_fraction) : null], ["Before scene cloud", unit(p.sentinel_pre_scene_cloud, "%")],
      ["After scene cloud", unit(p.sentinel_post_scene_cloud, "%")], ["Before catalogue item", p.sentinel_pre_item], ["After catalogue item", p.sentinel_post_item]]);
    if (p.sentinel_preview) {
      const relative = Evidence.safePreviewPath(p.sentinel_preview);
      if (relative) {
        const image = make("img", null, "detail-preview"); image.src = relative; image.alt = "Sentinel-2 comparison preview; inspect source dates and valid-pixel coverage"; image.loading = "lazy";
        image.addEventListener("error", () => {image.hidden = true; sentinel.append(make("p", "The preview image is not available in this output folder.", "detail-note"));}); sentinel.append(image);
      }
    }
    sentinel.append(make("p", "Image changes can corroborate an event, but do not establish fire causation on their own. Clouds, scene timing, and valid-pixel coverage limit interpretation.", "detail-note"));
  }
  function provenance() {
    const body = $("#provenance-body"); body.replaceChildren();
    const snapshot = section(body, "Saved snapshot · not a live service", "This HTML displays the observations saved by the pipeline. It does not poll satellite feeds. Regenerate the outputs and reopen the page to see newly acquired data.");
    facts(snapshot, [["Page generated · UTC", formatTime(metadata.page_generated_at || metadata.generated_at, "UTC")],
      ["Core run · UTC", formatTime(metadata.generated_at, "UTC")],
      ["Interpretation updated · UTC", metadata.interpretation?.updated_at ? formatTime(metadata.interpretation.updated_at, "UTC") : null],
      ["Sentinel enriched · UTC", metadata.corroborated_at ? formatTime(metadata.corroborated_at, "UTC") : null], ["Earliest acquisition · UTC", formatTime(earliest, "UTC")],
      ["Latest acquisition · UTC", formatTime(latest, "UTC")], ["Events displayed", count(events.length)], ["Invalid map coordinates omitted", rawEvents.length !== events.length ? count(rawEvents.length - events.length) : null],
      ["Acquisition years present", audit.available_years], ["Science-quality archive years", audit.science_quality_years],
      ["Requested archive years", audit.expected_years], ["Missing requested years", audit.missing_expected_years?.length ? audit.missing_expected_years.join(", ") : null],
      ["Cached OSM road features", count(metadata.offline_roads_count ?? data.roads?.features?.length ?? 0)], ["Offline road cache", human(metadata.offline_roads_status)]]);
    if (data.downloads?.length) {
      const downloads = section(body, "Saved CSV exports", "Original exported values, not rounded map labels. No new analysis is run.");
      for (const file of data.downloads) {
        if (!/^downloads\/[a-z_]+\.csv$/.test(file.url)) continue;
        const link = make("a", file.name, "download-link");
        link.href = file.url; link.download = file.name;
        downloads.append(link);
      }
    }
    snapshot.append(make("p", "A date span does not imply continuous coverage. Cloud, satellite overpasses, source outages, and archive gaps can leave periods unobserved.", "detail-note"));
    section(body, "How to interpret an event", "An event groups thermal detections in space and time. Nearby mapped industry, land cover, and recurring sites provide context independently from anomaly scores. A model classification is not independent incident verification; inspect each event's verification field.");
    section(body, "Mining context and unavailable scores", "Explicit mapped quarry context is separated from other industrial context; bare land alone does not identify a mine. Known context remains visible when a score is unavailable. The event card explains the first blocking gate: recurrent site, comparable history, usable features, day/night phase, or calibration. Scored-only and unscored-only filters do not change classifications.");
    const ranking = section(body, "Review priority · triage, not probability", "Scores compare behaviour against the available baseline. Lower priority does not establish safety. Insufficient data means no anomaly score, not zero; score filters deliberately retain these observations.");
    section(body, "Vegetation peers", "Vegetation episodes without a same-site score use earlier science-quality vegetation detections matched by land cover, season, satellite, day/night and location. Sparse groups still abstain, with the reason shown. Peer and same-site scores use different reference populations; equal scores do not establish equal risk. The reference data contain detected heat only, not observed non-fire conditions.");
    facts(ranking, [["Below 0.80", "Lower priority"], ["0.80 to below 0.95", "Moderate priority"], ["0.95 to below 0.99", "High priority"], ["0.99 and above", "Highest priority"], ["No score available", "Insufficient data · remains visible"]]);
    section(body, "Sentinel-2 and land cover", "Sentinel-2 is supporting evidence only. Inspect image dates, scene cloud and joint valid-pixel coverage. Burn-like spectral change is not independent proof of fire causation. The WorldCover context layer is historical (2021), not current vegetation or weather.");
    for (const [title, value] of [["Input coverage audit", metadata.input_audit], ["Feed synchronization audit", metadata.live_sync], ["External data provenance", metadata.external_data]]) {
      if (!value) continue;
      const block = section(body, title), details = make("details"), summary = make("summary", "Inspect recorded source status");
      details.append(summary, make("pre", JSON.stringify(value, null, 2))); block.append(details);
    }
  }
  function resetFilters() {
    $("#period").value = "all"; $("#filter").value = "all"; $("#score").value = "0"; $("#search").value = ""; $("#sort").value = "latest"; $("#replay").checked = false;
    $("#score-availability").value = "all";
    pageSize = 60; render();
  }
  for (const id of ["#period", "#filter", "#score-availability", "#date-from", "#date-to", "#replay", "#sort"]) $(id).addEventListener("change", () => {pageSize = 60; render();});
  $("#score").addEventListener("input", () => {pageSize = 60; render();});
  let searchTimer;
  $("#search").addEventListener("input", () => {clearTimeout(searchTimer); searchTimer = setTimeout(() => {pageSize = 60; render();}, 120);});
  $("#reset").addEventListener("click", resetFilters);
  $("#show-history").addEventListener("click", resetFilters);
  $("#load-more").addEventListener("click", () => {pageSize += 60; renderList();});
  $("#close-detail").addEventListener("click", () => closeDetail());
  $("#home").addEventListener("click", () => map.fitBounds(homeBounds, {padding: [28, 28], animate: !reducedMotion}));
  $("#fit").addEventListener("click", () => {
    if (filtered.length) map.fitBounds(L.latLngBounds(filtered.map(f => [f.geometry.coordinates[1], f.geometry.coordinates[0]])), {padding: [55, 55], maxZoom: 13, animate: !reducedMotion});
  });
  $("#sources").addEventListener("click", () => {provenance(); $("#provenance").showModal();});
  $("#close-sources").addEventListener("click", () => $("#provenance").close());
  const compactLayout = matchMedia("(max-width: 800px), (max-height: 540px)");
  function selectNavigation(eventsOpen) {
    for (const [id, active] of [["#nav-map", !eventsOpen], ["#toggle-sidebar", eventsOpen]]) {
      if (active) $(id).setAttribute("aria-current", "page");
      else $(id).removeAttribute("aria-current");
    }
    $("#toggle-sidebar").setAttribute("aria-expanded", String(getComputedStyle($("#sidebar")).display !== "none"));
  }
  function returnToMap() {
    document.body.classList.remove("sidebar-open");
    if (!$("#detail").hidden) closeDetail(false);
    selectNavigation(false);
    $("#map").focus({preventScroll: true});
  }
  $("#nav-map").addEventListener("click", returnToMap);
  $("#toggle-sidebar").addEventListener("click", () => {
    if (compactLayout.matches && document.body.classList.contains("sidebar-open")) return returnToMap();
    if (!$("#detail").hidden && innerWidth < 1200) closeDetail(false);
    document.body.classList.add("sidebar-open");
    selectNavigation(true);
    $("#sidebar").scrollTop = 0;
    $("#search").focus({preventScroll: true});
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape" || $("#provenance").open) return;
    if (!$("#detail").hidden) closeDetail();
    else if (document.body.classList.contains("sidebar-open")) returnToMap();
  });
  function updateLayout() {
    selectNavigation(compactLayout.matches && document.body.classList.contains("sidebar-open"));
  }
  compactLayout.addEventListener("change", updateLayout);
  window.addEventListener("resize", updateLayout);
  // Docking/responsive views change the real map size without a browser resize.
  const mapResize = new ResizeObserver(() => requestAnimationFrame(() => {
    if ($("#map").clientWidth && $("#map").clientHeight) {
      map.invalidateSize({animate: false, pan: false});
      drawMap();
    }
  }));
  mapResize.observe($("#map"));
  updateLayout();
  map.on("moveend zoomend", drawMap);
  map.on("mousemove", event => {$("#coordinate").textContent = `${event.latlng.lat.toFixed(4)}°, ${event.latlng.lng.toFixed(4)}° · WGS84`;});
  window.addEventListener("offline", () => {$("#show-tiles").checked = false; setTiles();});
  setInterval(() => {if (document.visibilityState === "visible") render();}, 60000);
  render();
  $("#loading-state").hidden = true;
  // Paint the interactive event map before parsing the optional vector context.
  if (data.roads_url) setTimeout(loadRoads, 0);
}

if (typeof document !== "undefined") {
  const fail = error => {
    const notice = document.querySelector("#fatal-error");
    const loading = document.querySelector("#loading-state");
    if (loading) loading.hidden = true;
    if (notice) {
      notice.hidden = false;
      notice.textContent = "The workspace could not initialize: " + error.message + ". Open this site through the localhost launcher and keep its local data and assets together. Restore missing files, then reload.";
      const retry = document.createElement("button"); retry.textContent = "Reload workspace";
      retry.addEventListener("click", () => location.reload()); notice.append(document.createElement("p"), retry);
    }
    console.error(error);
  };
  try {
    const source = document.querySelector("#evidence-data");
    if (source?.dataset.src) Evidence.loadLocalJSON(source.dataset.src).then(startWorkspace).catch(fail);
    else startWorkspace(JSON.parse(source.textContent));
  } catch (error) {fail(error);}
}
