"""Offline UI checks: stdlib plus an installed Chrome/Edge, no npm dependencies."""
import importlib
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JAVASCRIPT = ROOT / "tests" / "javascript"
ATTACK = '<img src=x onerror="globalThis.__injected=true">Plant</script><script>globalThis.__injected=true</script>'

def fixture(cache):
    events = {"type": "FeatureCollection", "features": []}
    for event_id, when, score, decision, status, radiometry in (
        ("E1", "2024-12-31T10:00:00Z", None, "likely_vegetation_fire", "waiting_for_post_image", None),
        ("E2", "2023-12-31T10:00:00Z", 0.97, "suspected_abnormal_industrial_associated_event", "analysed", "asset_metadata"),
        ("E3", "2023-12-30T10:00:00Z", 0.91, "mixed_or_unknown", "analysed", "legacy_offset_unverified"),
        ("E4", "2022-01-01T10:00:00Z", None, "mining_associated_unscored", "queued", None),
        ("E5", "2022-01-02T10:00:00Z", None, "industrial_associated_unscored", "queued", None),
        ("E6", "2022-01-03T10:00:00Z", 0.98, "suspected_abnormal_mining_associated_event", "queued", None),
    ):
        events["features"].append({"type": "Feature", "id": event_id,
            "geometry": {"type": "Point", "coordinates": [{"E3": 82.2, "E4": 82.08, "E5": 82.15, "E6": 82.3}.get(event_id, 82.0), 24.0]},
            "properties": {"event_id": event_id, "start_time": when, "end_time": when,
                "decision": decision, "anomaly_score": score, "detection_count": 3,
                "source_context": "mining_quarry" if event_id in {"E4", "E6"} else "industrial_associated" if event_id in {"E2", "E5"} else "vegetation_associated" if event_id == "E1" else "mixed",
                "score_status": "insufficient_site_history" if event_id in {"E4", "E5"} else "scored" if score is not None else "no_recurrent_site",
                "score_reason": "Only 2 prior comparable episodes; at least 6 are required." if event_id in {"E4", "E5"} else None,
                "nearest_facility_name": ATTACK, "sentinel_status": status,
                "sentinel_radiometry_status": radiometry, "sentinel_dnbr_mean": 0.318,
                "sentinel_dnbr_median": 0.275, "sentinel_affected_fraction": 0.125,
                "sentinel_swir_affected_fraction": 0.25,
                "sentinel_reason": "No suitable post-event image is available.",
                "product_status": "standard", "observed_sensors": "VIIRS", "frp_peak_mw": 17.4}})
    facilities = {"type": "FeatureCollection", "features": [{"type": "Feature",
        "geometry": {"type": "Point", "coordinates": [82.1, 24.1]},
        "properties": {"name": ATTACK, "facility_id": "F1", "kind": "power"}}]}
    config = {"aoi": {"name": "Test pilot", "bbox": [81.5, 23.2, 83.5, 25.2]},
        "paths": {"cache": str(cache)},
        "_runtime_metadata": {"generated_at": "2026-09-08T10:00:00Z", "input_audit": {
            "date_min": "2023-12-31T10:00:00Z", "date_max": "2024-12-31T10:00:00Z",
            "available_years": [2023, 2024], "expected_years": [2022, 2023, 2024], "missing_expected_years": [2022]}}}
    return events, facilities, config

class OfflineViewerTests(unittest.TestCase):
    def renderer(self):
        self.assertTrue((ROOT / "backend" / "viewer.py").exists(), "The standalone offline viewer renderer is missing")
        return importlib.import_module("backend.viewer")

    def test_safe_self_contained_payload(self):
        renderer = self.renderer()
        with tempfile.TemporaryDirectory() as directory:
            renderer.write_offline_viewer(Path(directory), *fixture(Path(directory) / "cache"))
            html = (Path(directory) / "index.html").read_text(encoding="utf-8")
            payload = re.search(r'<script id="evidence-data" type="application/json">(.*?)</script>', html, re.S)
            self.assertIsNotNone(payload)
            self.assertEqual(json.loads(payload.group(1))["facilities"]["features"][0]["properties"]["name"], ATTACK)
            self.assertEqual(len(json.loads(payload.group(1))["roads"]["features"]), 0, "Fixture leaked the real project road cache")
            self.assertNotIn(ATTACK, html)
            self.assertNotRegex(html, r'<script[^>]+src="https?://')
            self.assertNotRegex(html, r'<link[^>]+href="https?://')

    def test_page_timestamp_is_separate_from_core_and_corroboration(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            events, facilities, config = fixture(output / 'cache')
            config['_runtime_metadata']['corroborated_at'] = '2026-09-08T11:00:00Z'
            config['_runtime_metadata']['interpretation'] = {'schema_version': 2, 'updated_at': '2026-09-08T12:00:00Z'}
            before = datetime.now(timezone.utc)
            self.renderer().write_offline_viewer(output, events, facilities, config)
            after = datetime.now(timezone.utc)
            html = (output / 'index.html').read_text(encoding='utf-8')
            payload = re.search(r'<script id="evidence-data" type="application/json">(.*?)</script>', html, re.S)
            metadata = json.loads(payload.group(1))['metadata']
            self.assertEqual(metadata.get('corroborated_at'), '2026-09-08T11:00:00Z')
            self.assertEqual(metadata['generated_at'], '2026-09-08T10:00:00Z')
            self.assertEqual(metadata.get('interpretation', {}).get('updated_at'), '2026-09-08T12:00:00Z')
            self.assertIn('page_generated_at', metadata)
            self.assertLessEqual(before, datetime.fromisoformat(metadata['page_generated_at']))
            self.assertLessEqual(datetime.fromisoformat(metadata['page_generated_at']), after)

    def test_local_preview_paths_are_portable_and_external_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            events, facilities, config = fixture(output / 'cache')
            events['features'][0]['properties']['sentinel_preview'] = str(output / 'sentinel' / 'known.png')
            events['features'][1]['properties']['sentinel_preview'] = str(output.parent / 'outside.png')
            self.renderer().write_offline_viewer(output, events, facilities, config)
            html = (output / 'index.html').read_text(encoding='utf-8')
            payload = re.search(r'<script id="evidence-data" type="application/json">(.*?)</script>', html, re.S)
            features = json.loads(payload.group(1))['events']['features']
            self.assertEqual(features[0]['properties']['sentinel_preview'], 'sentinel/known.png')
            self.assertIsNone(features[1]['properties']['sentinel_preview'])

    def test_actual_browser(self):
        renderer = self.renderer()
        browser = next((path for path in (
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"),
        ) if path.exists()), None)
        if browser is None:
            self.skipTest("Chrome/Edge is unavailable")
        harness = "<script>" + (JAVASCRIPT / "test_viewer.browser.js").read_text(encoding="utf-8") + "</script>"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            renderer.write_offline_viewer(output, *fixture(output / "cache"))
            html_file = output / "index.html"
            html_file.write_text(html_file.read_text(encoding="utf-8").replace("</body>", harness + "</body>"), encoding="utf-8")
            rendered = subprocess.run([str(browser), "--headless=new", "--disable-gpu", "--no-sandbox",
                "--disable-background-networking", "--host-resolver-rules=MAP * ~NOTFOUND",
                # Virtual time does not advance CSS transitions; test the supported reduced-motion path.
                "--force-prefers-reduced-motion", "--virtual-time-budget=4000", "--window-size=1440,1000", "--dump-dom",
                "--user-data-dir=" + str(output / "profile"), html_file.as_uri()],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
            self.assertEqual(rendered.returncode, 0, rendered.stderr[-1500:])
            result = re.search(r'<pre id="browser-check"[^>]*>(.*?)</pre>', rendered.stdout, re.S)
            self.assertIsNotNone(result, "Browser test harness did not execute")
            self.assertIn('data-result="pass"', result.group(0), result.group(1))

    def test_real_clock_browser(self):
        renderer = self.renderer()
        node = shutil.which("node")
        browser = next((path for path in (
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"),
        ) if path.exists()), None)
        if node is None or browser is None:
            self.skipTest("Node with built-in WebSocket and Chrome/Edge are required")
        harness = "<script>" + (JAVASCRIPT / "test_viewer.browser.js").read_text(encoding="utf-8") + "</script>"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            renderer.write_offline_viewer(output, *fixture(output / "cache"))
            html_file = output / "index.html"
            html_file.write_text(html_file.read_text(encoding="utf-8").replace("</body>", harness + "</body>"), encoding="utf-8")
            result = subprocess.run([node, str(JAVASCRIPT / "test_viewer.cdp.js"), str(browser), str(html_file), str(output / "profile")], capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS: real-clock browser", result.stdout)

if __name__ == "__main__":
    unittest.main()
