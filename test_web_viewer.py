"""Local-site contracts; no scientific libraries or network acquisition required."""
import copy
import base64
import gzip
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import viewer
from test_viewer import ATTACK, fixture


class WebViewerTests(unittest.TestCase):
    def write(self, output, events=None):
        source, facilities, config = fixture(output / "cache")
        self.assertTrue(callable(getattr(viewer, "write_web_viewer", None)),
                        "The external-payload localhost site writer is missing")
        site = viewer.write_web_viewer(output, events or source, facilities, config)
        self.assertEqual(site, output / "site")
        html = (site / "index.html").read_text(encoding="utf-8")
        url = re.search(r'id="evidence-data"[^>]*data-src="([^"]+)"', html).group(1)
        return site, json.loads((site / url).read_text(encoding="utf-8")), html

    def test_lightweight_site_uses_local_assets_and_separate_readable_payloads(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            events, _, _ = fixture(output / "cache")
            events["features"][0]["properties"]["anomaly_reasons"] = ["detail-only"] * 10000
            original = copy.deepcopy(events)
            site, data, html = self.write(output, events)
            self.assertLess(len(html.encode("utf-8")), 16000)
            self.assertNotIn(ATTACK, html)
            self.assertNotIn("detail-only", json.dumps(data))
            self.assertNotIn("roads", data)
            self.assertEqual(data["metadata"]["generated_at"], "2026-09-08T10:00:00Z")
            self.assertEqual(data["events"]["features"][0]["properties"]["anomaly_score"], None)
            for url in re.findall(r'<(?:script|link)[^>]+(?:src|href)="([^"]+)"', html):
                self.assertNotRegex(url, r"^(https?:|//)")
                self.assertTrue((site / url).is_file(), url)
            self.assertEqual((site / "vendor/Sora-Variable.ttf").read_bytes(),
                             (viewer.WEB / "vendor/Sora-Variable.ttf").read_bytes())
            self.assertEqual(events, original, "Packaging must not mutate scientific inputs")
            for path in (site / "data").rglob("*.json"):
                self.assertEqual(gzip.decompress(path.with_suffix(".json.gz").read_bytes()),
                                 path.read_bytes())

    def test_lazy_details_keep_values_and_do_not_use_event_ids_as_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            events, _, _ = fixture(output / "cache")
            events["features"][0]["id"] = "../../outside"
            events["features"][0]["properties"].update(event_id="../../outside", anomaly_score=0.9876543210987654,
                anomaly_reasons=["A", {"nested": 1}], sentinel_dnbr_mean=0.318123456789)
            events["features"][1]["properties"]["event_id"] = "../../outside"
            site, data, _ = self.write(output, events)
            for expected, summary in zip(events["features"], data["events"]["features"]):
                reference = summary["detail"]
                self.assertRegex(reference["url"], r"^data/events/\d+\.json$")
                payload = json.loads((site / reference["url"]).read_text(encoding="utf-8"))
                self.assertEqual(payload["features"][reference["index"]], expected)
            self.assertFalse((output.parent / "outside").exists())

    def test_previews_are_copied_and_work_when_site_moves(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            sentinel = output / "sentinel"
            sentinel.mkdir()
            (sentinel / "known.png").write_bytes(b"saved preview")
            events, _, _ = fixture(output / "cache")
            paths = [str(sentinel / "known.png"), "sentinel/known.png", "../outside.png",
                     "sentinel/%2e%2e/private.png", "https://example.org/a.png", "sentinel/missing.png"]
            for feature, path in zip(events["features"], paths):
                feature["properties"]["sentinel_preview"] = path
            site, data, _ = self.write(output, events)
            details = []
            for summary in data["events"]["features"]:
                ref = summary["detail"]
                details.append(json.loads((site / ref["url"]).read_text())["features"][ref["index"]])
            self.assertEqual([f["properties"]["sentinel_preview"] for f in details],
                             ["sentinel/known.png", "sentinel/known.png", None, None, None, None])
            self.assertEqual((site / "sentinel/known.png").read_bytes(), b"saved preview")

    def test_cached_roads_are_external_and_saved_csv_bytes_are_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            (output / "cache").mkdir()
            roads = {"type": "FeatureCollection", "features": [{"type": "Feature",
                "properties": {"highway": "primary"},
                "geometry": {"type": "LineString", "coordinates": [[82.123456789, 24], [82.2, 24.1]]}}]}
            (output / "cache/osm_roads.geojson").write_text(json.dumps(roads))
            csv = b'event_id,anomaly_score\r\nE2,0.9876543210987654\r\n'
            (output / "events.csv").write_bytes(csv)
            site, data, _ = self.write(output)
            self.assertEqual(json.loads((site / data["roads_url"]).read_text()), roads)
            self.assertEqual(data["metadata"]["offline_roads_status"], "cached")
            self.assertEqual((site / data["downloads"][0]["url"]).read_bytes(), csv)

    def test_symlinked_site_cannot_write_outside_output(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            output = Path(folder)
            try:
                (output / "site").symlink_to(outside, target_is_directory=True)
            except OSError:
                if not shutil.which("cmd"):
                    self.skipTest("Directory symlinks unavailable")
                result = subprocess.run(["cmd", "/c", "mklink", "/J",
                    str(output / "site"), outside], capture_output=True)
                if result.returncode:
                    self.skipTest("Directory symlinks and junctions unavailable")
            self.assertTrue(callable(getattr(viewer, "write_web_viewer", None)))
            with self.assertRaises(ValueError):
                viewer.write_web_viewer(output, *fixture(output / "cache"))
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_network_and_traversing_preview_paths_are_rejected_before_resolution(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            events, _, _ = fixture(output / "cache")
            paths = ["//server/share/preview.png", r"\\server\share\preview.png", "file:///private.png",
                     "sentinel/%252e%252e/private.png", "sentinel/../private.png", "sentinel/private.png\x00"]
            for feature, path in zip(events["features"], paths):
                feature["properties"]["sentinel_preview"] = path
            resolve = Path.resolve
            def forbid_network_path(path, *args, **kwargs):
                if path.as_posix().startswith("//"):
                    raise AssertionError("Untrusted preview triggered network path resolution")
                return resolve(path, *args, **kwargs)
            with patch.object(Path, "resolve", forbid_network_path):
                site, data, _ = self.write(output, events)
            ref = data["events"]["features"][0]["detail"]
            details = json.loads((site / ref["url"]).read_text())["features"]
            self.assertEqual([f["properties"]["sentinel_preview"] for f in details], [None] * 6)

    def test_real_localhost_browser_reads_moved_site_and_handles_missing_payloads(self):
        browser = next((str(path) for path in (
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"),
        ) if path.is_file()), None)
        node = shutil.which("node")
        if not browser or not node:
            self.skipTest("Node and Chrome/Edge required")
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            sentinel = output / "sentinel"
            sentinel.mkdir()
            (sentinel / "saved.png").write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII="))
            events, _, _ = fixture(output / "cache")
            events["features"][1]["properties"]["sentinel_preview"] = str(sentinel / "saved.png")
            (output / "cache").mkdir()
            (output / "cache/osm_roads.geojson").write_text(json.dumps({"type": "FeatureCollection",
                "features": [{"type": "Feature", "properties": {"highway": "primary"},
                    "geometry": {"type": "LineString", "coordinates": [[82, 24], [82.1, 24.1]]}}]}))
            site, _, _ = self.write(output, events)
            relocated = output / "relocated"
            shutil.copytree(site, relocated)
            result = subprocess.run([node, str(viewer.ROOT / "test_web_viewer.js"), "--browser",
                                     browser, str(relocated), str(output / "profile")],
                                    capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("localhost browser", result.stdout)


if __name__ == "__main__":
    unittest.main()
