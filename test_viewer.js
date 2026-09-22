"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const script = path.join(__dirname, "web", "viewer.js");
let ui;
test("offline frontend exports date and evidence filters", () => {
  assert.ok(fs.existsSync(script), "Offline viewer implementation is missing");
  ui = require(script);
  assert.equal(typeof ui.dateWindow, "function");
  assert.equal(typeof ui.matchesEvent, "function");
});

const now = Date.parse("2026-09-08T12:00:00Z");
const latest = Date.parse("2024-12-31T22:00:00Z");
const event = (extra = {}) => ({properties: {event_id: "E1", decision: "likely_vegetation_fire", start_time: "2024-12-31T10:00:00Z", end_time: "2024-12-31T11:00:00Z", anomaly_score: null, ...extra}, geometry: {type: "Point", coordinates: [82, 24]}});
const filters = (extra = {}) => ({decision: "all", minScore: 0, query: "", time: {start: -Infinity, end: Infinity}, ...extra});

test("latest 24h uses current UTC unless replay is explicit", () => {
  const current = ui.dateWindow("day", {now, latest});
  assert.equal(current.start, Date.parse("2026-09-07T12:00:00Z"));
  assert.equal(current.end, now);
  assert.equal(ui.matchesEvent(event(), filters({time: current})), false);
  const replay = ui.dateWindow("day", {now, latest, replay: true});
  assert.equal(replay.start, Date.parse("2024-12-30T22:00:00Z"));
  assert.equal(replay.end, latest);
  assert.equal(ui.matchesEvent(event(), filters({time: replay})), true);
});

test("missing scores are not coerced to zero by the threshold", () => {
  assert.equal(ui.matchesEvent(event(), filters({minScore: 0.99})), true);
  assert.equal(ui.matchesEvent(event({anomaly_score: ""}), filters({minScore: 0.99})), true);
  assert.equal(ui.matchesEvent(event({anomaly_score: "   "}), filters({minScore: 0.99})), true);
  assert.equal(ui.matchesEvent(event({anomaly_score: 0.2}), filters({minScore: 0.99})), false);
  assert.equal(ui.anomalyBand(null).label, "Insufficient data");
  assert.equal(ui.anomalyBand(0).label, "Lower priority");
});

test("weekly monthly yearly windows use explicit rolling durations", () => {
  for (const [preset, days] of [["week", 7], ["month", 30], ["year", 365]]) {
    assert.equal(ui.dateWindow(preset, {now}).start, now - days * 86400000);
  }
});

test("custom dates cover full IST days independent of browser timezone", () => {
  const span = ui.dateWindow("custom", {from: "2024-12-31", to: "2024-12-31", now});
  assert.equal(span.start, Date.parse("2024-12-30T18:30:00Z"));
  assert.equal(span.end, Date.parse("2024-12-31T18:29:59.999Z"));
  for (const [from, to] of [["", ""], ["2024-02-30", "2024-03-01"], ["2025-01-02", "2025-01-01"]]) {
    assert.equal(ui.dateWindow("custom", {from, to, now}).valid, false);
  }
});

test("date filter includes overlapping intervals but excludes undated latest events", () => {
  const time = {start: Date.parse("2025-01-01T00:00:00Z"), end: Date.parse("2025-01-02T00:00:00Z")};
  assert.equal(ui.matchesEvent(event({end_time: "2025-01-01T04:00:00Z"}), filters({time})), true);
  assert.equal(ui.matchesEvent(event(), filters({time})), false);
  const undated = event({start_time: null, end_time: null});
  assert.equal(ui.matchesEvent(undated, filters()), true);
  assert.equal(ui.matchesEvent(undated, filters({time})), false);
});

test("facility search is literal case insensitive and decision filter is independent", () => {
  const item = event({nearest_facility_name: "Anpara Power Station"});
  assert.equal(ui.matchesEvent(item, filters({query: "ANPARA"})), true);
  assert.equal(ui.matchesEvent(item, filters({query: "[.*]"})), false);
  assert.equal(ui.matchesEvent(item, filters({decision: "mixed_or_unknown"})), false);
});

test("review priority labels preserve the approved score boundaries", () => {
  for (const [score, label] of [[0.799, "Lower priority"], [0.8, "Moderate priority"], [0.949, "Moderate priority"], [0.95, "High priority"], [0.989, "High priority"], [0.99, "Highest priority"], [1, "Highest priority"], [null, "Insufficient data"]]) {
    assert.equal(ui.anomalyBand(score).label, label);
  }
});

test("score availability is separate from context and keeps zero as scored", () => {
  const unscored = event({decision: "industrial_associated_unscored", source_context: "industrial_associated"});
  assert.equal(ui.matchesEvent(unscored, filters({availability: "scored"})), false);
  assert.equal(ui.matchesEvent(unscored, filters({availability: "unscored", minScore: .99})), true);
  assert.equal(ui.matchesEvent(event({anomaly_score: 0}), filters({availability: "scored"})), true);
  assert.equal(ui.matchesEvent(event({anomaly_score: 0}), filters({availability: "unscored"})), false);
  assert.equal(ui.matchesEvent(unscored, filters({availability: "unscored", decision: "mixed_or_unknown"})), false);
});

test("source subtype and scoring reason codes are searchable without changing context", () => {
  const mining = event({decision: "mining_associated_unscored", context_label: "industrial_associated",
    source_context: "mining_quarry", score_status: "insufficient_site_history"});
  assert.equal(ui.matchesEvent(mining, filters({query: "mining_quarry"})), true);
  assert.equal(ui.matchesEvent(mining, filters({query: "insufficient_site_history"})), true);
  assert.equal(ui.matchesEvent(mining, filters({decision: "industrial_associated_unscored"})), false);
});

test("unverified radiometry and unavailable comparisons never produce quantitative Sentinel claims", () => {
  assert.equal(ui.sentinelQuantitativeAllowed({sentinel_status: "analysed", sentinel_radiometry_status: "legacy_offset_unverified"}), false);
  assert.equal(ui.sentinelQuantitativeAllowed({sentinel_status: "waiting_for_post_image"}), false);
  assert.equal(ui.sentinelQuantitativeAllowed({sentinel_status: "analysed", sentinel_radiometry_status: "verified"}), true);
});

test("preview paths stay within the output folder and cannot fetch remote images", () => {
  assert.equal(ui.safePreviewPath("sentinel_previews/E1.png"), "sentinel_previews/E1.png");
  for (const path of ["../private.png", "%2e%2e/private.png", "..%5cprivate.png", "https://example.com/image.png", " https://example.com/image.png", "//example.com/image.png", "file:///private.png"]) {
    assert.equal(ui.safePreviewPath(path), null);
  }
});

test("peer comparison details never claim zero same-site history as the scoring baseline", () => {
  assert.equal(typeof ui.comparisonDetails, "function");
  const peer = ui.comparisonDetails({score_method: "vegetation_peer", anomaly_score: .96,
    history_event_count: 0, model_feature_count: 0, peer_reference_count: 120,
    peer_feature_count: 5, peer_reference_start: "2022-02-01", peer_reference_end: "2024-02-01"});
  assert.equal(peer.name, "Vegetation peers");
  assert.equal(peer.references, 120);
  assert.equal(peer.features, 5);
  assert.equal(peer.end, "2024-02-01");
  const site = ui.comparisonDetails({anomaly_score: .9, history_event_count: 16, model_feature_count: 6});
  assert.equal(site.name, "Same-site history");
  assert.equal(site.references, 16);
});

test("coverage gaps are explicit without claiming no fires or complete observations", () => {
  assert.equal(typeof ui.coverageWarning, "function");
  const message = ui.coverageWarning({missing_expected_years: [2021, 2025]});
  assert.ok(message.includes("2021, 2025"));
  assert.ok(message.includes("not evidence of no fires"));
  assert.ok(ui.coverageWarning({missing_expected_years: []}).includes("not continuous"));
});
