"""The peer-only export must preserve measurements, site scores and Sentinel."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import pipeline
from test_peer_scoring import episodes, settings


class PeerRefreshTests(unittest.TestCase):
    def fixture(self, root):
        config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        overrides = settings()
        config["models"].update(overrides.pop("models"))
        config.update(overrides)
        config["paths"] = {"output": "outputs", "cache": str(root / "cache")}
        path = root / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        out = root / "outputs"
        out.mkdir()
        events = episodes()
        industry = {**events.iloc[-1].to_dict(), "event_id": "INDUSTRY",
                    "site_id": "S1", "context_label": "industrial_associated",
                    "anomaly_score": .9734123456789012, "history_sufficient": True,
                    "review_priority": "high"}
        events = pipeline.consensus(pd.concat([events, pd.DataFrame([industry])], ignore_index=True), config)
        events["sentinel_status"] = "analysed"
        events["sentinel_evidence"] = "no_clear_change"
        events.to_csv(out / "events.csv", index=False)
        pipeline.write_json(out / "events.geojson", pipeline.frame_to_geojson(events, "event_id"))
        pipeline.write_json(out / "facilities.geojson", {"type": "FeatureCollection", "features": []})
        pipeline.write_json(out / "run_manifest.json", {
            "generated_at": "2026-09-08T00:00:00Z", "outputs": {"events": len(events)},
            "input_audit": {"missing_expected_years": [2025]},
        })
        (out / "index.html").write_text("old map", encoding="utf-8")
        return path, out

    def test_refresh_updates_peer_scores_but_preserves_all_original_measurements(self):
        self.assertTrue(hasattr(pipeline, "refresh_vegetation_peers"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, out = self.fixture(root)
            before = pd.read_csv(out / "events.csv", float_precision="round_trip")
            report = pipeline.refresh_vegetation_peers(root, config)
            after = pd.read_csv(out / "events.csv", float_precision="round_trip")
            self.assertEqual(after.set_index("event_id").loc["TARGET", "score_method"], "vegetation_peer")
            self.assertEqual(after.iloc[-1].anomaly_score, .9734123456789012)
            for column in ("event_id", "start_time", "end_time", "site_id", "decision",
                           "frp_peak_mw", "latitude", "sentinel_status", "sentinel_evidence",
                           "history_event_count", "history_sufficient"):
                pd.testing.assert_series_equal(before[column], after[column])
            payload = json.loads((out / "events.geojson").read_text())
            target = next(f["properties"] for f in payload["features"] if f["properties"]["event_id"] == "TARGET")
            self.assertEqual(target["anomaly_score"], .5)
            self.assertEqual(report["outputs"]["events"], len(before))
            self.assertEqual(report["generated_at"], "2026-09-08T00:00:00Z")
            self.assertTrue((out / "vegetation_scoring.json").exists())
            self.assertIn("Vegetation peers", (out / "index.html").read_text(encoding="utf-8"))

    def test_failed_map_publication_restores_previous_outputs(self):
        self.assertTrue(hasattr(pipeline, "refresh_vegetation_peers"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, out = self.fixture(root)
            originals = {p.name: p.read_bytes() for p in out.iterdir()}
            with patch.object(pipeline, "write_offline_viewer", side_effect=OSError("publication failed")):
                with self.assertRaises(OSError):
                    pipeline.refresh_vegetation_peers(root, config)
            self.assertEqual({p.name: p.read_bytes() for p in out.iterdir()}, originals)

    def test_conflicting_geojson_measurements_are_rejected_before_publication(self):
        for field in ("latitude", "start_time", "frp_peak_mw", "model_sensor"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config, out = self.fixture(root)
                path = out / "events.geojson"
                payload = json.loads(path.read_text())
                feature = payload["features"][0]
                if field == "latitude":
                    feature["geometry"]["coordinates"][1] += .1
                else:
                    feature["properties"][field] = {"start_time": "2025-01-01T00:00:00Z", "frp_peak_mw": 999., "model_sensor": "VIIRS_NOAA20"}[field]
                pipeline.write_json(path, payload)
                originals = {p.name: p.read_bytes() for p in out.iterdir()}
                with self.assertRaises(ValueError):
                    pipeline.refresh_vegetation_peers(root, config)
                self.assertEqual({p.name: p.read_bytes() for p in out.iterdir()}, originals)


if __name__ == "__main__":
    unittest.main()
