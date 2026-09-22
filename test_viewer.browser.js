(async () => {
  const output = document.createElement("pre"); output.id = "browser-check"; output.hidden = true;
  document.body.append(output);
  const ok = (value, message) => {if (!value) throw new Error(message);};
  const $ = selector => document.querySelector(selector);
  const change = (selector, value, event = "change") => {$(selector).value = value; $(selector).dispatchEvent(new Event(event, {bubbles: true}));};
  const tick = () => new Promise(resolve => setTimeout(resolve, 180));
  try {
    await tick();
    if (location.protocol === 'file:') {
      ok($('#show-tiles').disabled, 'file-open pages must disable online OSM tiles: no valid HTTP referrer');
      $('#show-tiles').checked = true; $('#show-tiles').dispatchEvent(new Event('change'));
      ok(!$('#show-tiles').checked && !document.querySelector('.leaflet-tile'), 'file-open pages must never issue online tile requests');
      ok($('#tile-note').textContent.includes('localhost'), 'file-open mode must explain how to enable online maps');
    }
    ok($("#count").textContent.includes("6 events shown"), "history should show all events");
    ok($(".event") && $(".facility"), "keyboard event and facility lists missing");
    ok($("#map").dataset.visibleEvents === "6", "event layer not populated");
    const painted = [...document.querySelectorAll(".leaflet-overlay-pane canvas")].some(canvas => {
      if (!canvas.width || !canvas.height) return false;
      const pixels = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
      // Require a heat-event color, not merely the AOI outline or a gold facility.
      for (let i = 0; i < pixels.length; i += 4) {
        const [r, g, b, alpha] = pixels.slice(i, i + 4);
        if (alpha && ((r > 160 && g < 115 && b < 115) || (g > r * 1.15 && g > b * 1.15))) return true;
      }
      return false;
    });
    ok(painted, "offline map has no painted vector features");
    const initialZoom = Number($("#map").dataset.zoom);
    $(".leaflet-control-zoom-in").click(); await tick(); await tick();
    ok(Number($("#map").dataset.zoom) > initialZoom, "zoom control failed");
    change("#period", "day");
    ok($("#count").textContent.includes("0 events shown"), "old observations falsely shown as live");
    ok(!$("#empty-state").hidden, "date empty state is missing");
    ok($("#empty-state").textContent.includes("2024"), "empty state lacks last acquisition");
    $("#replay").checked = true; $("#replay").dispatchEvent(new Event("change"));
    ok($("#count").textContent.includes("1 events shown"), "explicit replay should show latest historic event");
    ok($("#freshness-state").textContent.includes("REPLAY"), "replay is not visibly labelled");
    change("#score", "0.99", "input");
    ok($("#count").textContent.includes("1 events shown"), "threshold hid unscored vegetation");
    $(".event").click();
    ok($("#detail .band").textContent === 'Insufficient data', 'missing-score badge must distinguish unavailable scoring from low priority');
    ok($("#detail").textContent.includes("<img src=x"), "facility name was not shown as literal text");
    ok(!globalThis.__injected && !document.querySelector('img[src="x"]'), "data executed as HTML");
    ok($("#detail").textContent.includes("IST") && $("#detail").textContent.includes("UTC"), "detail missing timezones");
    ok($("#detail").textContent.includes("No suitable post-event image"), "Sentinel reason hidden");
    const spectralFacts = () => Object.fromEntries([...$("#detail").querySelectorAll("dt")]
      .filter(node => /dNBR|Burn-like change fraction|SWIR anomaly fraction/.test(node.textContent))
      .map(node => [node.textContent, node.nextElementSibling.textContent]));
    ok(Object.keys(spectralFacts()).length === 0, "unavailable comparison exposed quantitative spectral claims");
    $("#reset").click();
    $('.event[data-event-id="E2"]').click();
    ok($("#detail .band").textContent === 'High priority', 'approved review-priority badge is missing');
    const measured = spectralFacts();
    for (const [label, value] of [["Mean dNBR", "0.318"], ["Median dNBR", "0.275"],
      ["Burn-like change fraction", "12.5%"], ["SWIR anomaly fraction", "25.0%"]]) {
      ok(measured[label] === value, `${label}: expected ${value}, got ${measured[label]}`);
    }
    $('.event[data-event-id="E3"]').click();
    ok(Object.keys(spectralFacts()).length === 0, "legacy radiometry exposed quantitative spectral claims");
    ok($("#detail").textContent.includes("Quantitative spectral-change claims are withheld"), "legacy radiometry warning missing");
    $("#reset").click();
    ok($("#score-availability"), "score-availability filter is missing");
    change("#filter", "industrial_associated_unscored");
    change("#score", "0.99", "input");
    ok($("#count").textContent.includes("1 events shown"), "industrial unscored context must have its own filter");
    $('.event[data-event-id="E5"]').click();
    ok($("#detail").textContent.includes("Industrial context · unscored"), "industrial context was relabelled unresolved");
    ok($("#detail").textContent.includes("Only 2 prior comparable episodes; at least 6 are required."), "missing-score reason is not displayed");
    change("#filter", "mining_associated_unscored");
    ok($("#count").textContent.includes("1 events shown"), "mining and industrial unscored episodes were conflated");
    $('.event[data-event-id="E4"]').click();
    ok($("#detail").textContent.includes("Mining/quarry context · unscored"), "quarry context was not preserved");
    change("#score-availability", "scored");
    ok($("#count").textContent.includes("0 events shown"), "scored-only mode retained unscored mining");
    change("#filter", "all"); change("#score", "0", "input");
    ok($("#count").textContent.includes("3 events shown"), "scored-only count must include both industrial and mining scores");
    change("#score-availability", "unscored");
    ok($("#count").textContent.includes("3 events shown"), "unscored-only count is wrong");
    $("#reset").click();
    ok($("#score-availability").value === "all" && $("#count").textContent.includes("6 events shown"), "reset did not restore score availability");
    const legendColors = Object.fromEntries([...document.querySelectorAll("#legend .legend-item")]
      .map(item => [item.textContent, getComputedStyle(item.querySelector(".legend-dot")).backgroundColor]));
    for (const [id, label, color] of [
      ["E1", "Vegetation", "rgb(22, 101, 52)"],
      ["E2", "Industrial review", "rgb(220, 38, 38)"],
      ["E3", "Unresolved", "rgb(107, 114, 128)"],
    ]) {
      ok(legendColors[label] === color, `${label}: legend does not show the approved context color`);
      ok(getComputedStyle($(`.event[data-event-id="${id}"]`)).borderLeftColor === color, `${id}: event card and context legend disagree`);
    }
    ok(legendColors["Industrial · unscored"] && legendColors["Mining · unscored"], "legend erased known context for unscored episodes");
    ok($("#legend-panel .band.unknown"), "legend does not explain the separate missing-score badge");
    // Pale marker colors must not become unreadable evidence text on the light panel.
    const luminance = color => {
      const rgb = color.match(/[\d.]+/g).slice(0, 3).map(value => {
        const channel = Number(value) / 255;
        return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
      });
      return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
    };
    for (const id of ["E1", "E2", "E3", "E4", "E5", "E6"]) {
      $(`.event[data-event-id="${id}"]`).click();
      const foreground = luminance(getComputedStyle($(".detail-context strong")).color);
      const background = luminance(getComputedStyle($("#detail")).backgroundColor);
      const contrast = (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
      ok(contrast >= 4.5, `${id}: evidence context text contrast is ${contrast.toFixed(2)}:1; minimum is 4.5:1`);
    }
    output.dataset.result = "pass";
    output.textContent = "PASS: offline vectors, zoom, dates, replay, unscored filter, safe detail, Sentinel producer fields, quantitative guards and context contrast";
  } catch (error) {output.dataset.result = "fail"; output.textContent = error.stack;}
})();
