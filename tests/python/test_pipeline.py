import tempfile
import time
import unittest
import subprocess
from pathlib import Path

import pandas as pd

from backend import pipeline


class PipelineScientificContractTests(unittest.TestCase):
    def test_offline_viewer_renders_events_in_a_real_browser(self):
        browser = next(
            (
                path
                for path in (
                    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                    Path("/usr/bin/google-chrome"),
                    Path("/usr/bin/chromium"),
                )
                if path.exists()
            ),
            None,
        )
        if browser is None:
            self.skipTest("Chrome/Edge is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            event = {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "E1",
                        "geometry": {"type": "Point", "coordinates": [82.0, 24.0]},
                        "properties": {
                            "event_id": "E1",
                            "decision": "mixed_or_unknown",
                            "detection_count": 1,
                            "anomaly_score": None,
                        },
                    }
                ],
            }
            facility = {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "F1",
                        "geometry": {"type": "Point", "coordinates": [82.1, 24.1]},
                        "properties": {"name": "Test facility"},
                    }
                ],
            }
            pipeline.write_offline_viewer(
                output,
                event,
                facility,
                {"aoi": {"bbox": [81.5, 23.2, 83.5, 25.2]},
                 "paths": {"cache": str(output / "cache")}},
            )

            rendered = subprocess.run(
                [
                    str(browser),
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--dump-dom",
                    (output / "index.html").as_uri(),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            self.assertIn('class="facility"', rendered.stdout)
            self.assertIn('class="event"', rendered.stdout)
            self.assertIn("1 events shown", rendered.stdout)

    def test_ingest_preserves_platform_identity_and_honest_temperature_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for satellite, instrument in (
                ("N", "VIIRS"),
                ("N20", "VIIRS"),
                ("N21", "VIIRS"),
                ("Terra", "MODIS"),
                ("Aqua", "MODIS"),
            ):
                rows.append(
                    {
                        "latitude": 24.0,
                        "longitude": 82.0,
                        "acq_date": "2024-01-01",
                        "acq_time": 100 + len(rows),
                        "satellite": satellite,
                        "instrument": instrument,
                        "bright_ti4": 340.0,
                        "bright_ti5": 300.0,
                        "brightness": 340.0,
                        "bright_t31": 300.0,
                        "frp": 5.0 + len(rows),
                        "scan": 0.4,
                        "track": 0.4,
                        "confidence": "n" if instrument == "VIIRS" else 80,
                        "daynight": "N",
                        "type": 0,
                    }
                )
            pd.DataFrame(rows).to_csv(root / "firms.csv", index=False)
            config = {
                "aoi": {"bbox": [81.5, 23.2, 83.5, 25.2]},
                "inputs": {"firms_globs": ["firms.csv"], "expected_years": [2024]},
            }

            detections, _ = pipeline.ingest_firms(root, config)

            self.assertEqual(
                set(detections["sensor"]),
                {
                    "VIIRS_SNPP",
                    "VIIRS_NOAA20",
                    "VIIRS_NOAA21",
                    "MODIS_TERRA",
                    "MODIS_AQUA",
                },
            )
            self.assertIn("longwave_brightness", detections)
            self.assertIn("brightness_temperature_difference", detections)
            self.assertNotIn("background_brightness", detections)
            self.assertNotIn("thermal_contrast", detections)

    def test_sentinel_corroboration_does_not_modify_primary_anomaly_score(self):
        events = pd.DataFrame(
            [
                {
                    "event_id": "E1",
                    "context_label": "industrial_associated",
                    "history_sufficient": True,
                    "history_event_count": 10,
                    "anomaly_score": 0.94,
                    "sentinel_adjustment": 0.03,
                    "sentinel_evidence": "supports_escalation",
                }
            ]
        )
        result = pipeline.consensus(events, {"models": {"anomaly_percentile": 0.95}})
        self.assertEqual(result.loc[0, "evidence_score"], 0.94)
        self.assertEqual(result.loc[0, "decision"], "routine_persistent_industrial_thermal_source")

    def test_consensus_does_not_misdescribe_an_unavailable_percentile_as_missing_history(self):
        events = pd.DataFrame([
            {
                "event_id": "E1", "context_label": "industrial_associated",
                "history_sufficient": False, "history_event_count": 6,
                "model_feature_count": 6, "anomaly_score": float("nan"),
            }
        ])

        result = pipeline.consensus(events, {"models": {"anomaly_percentile": 0.95}})

        self.assertIn("anomaly score unavailable", result.loc[0, "decision_basis"])
        self.assertNotIn("insufficient source history", result.loc[0, "decision_basis"])

    def test_self_check_still_passes(self):
        pipeline.self_check()

    def test_event_construction_handles_many_noise_points_without_group_loop(self):
        count = 1000
        start = pd.Timestamp("2024-01-01T00:00:00Z")
        detections = pd.DataFrame(
            {
                "detection_id": [f"D{i}" for i in range(count)],
                "latitude": [24.0] * count,
                "longitude": [82.0] * count,
                "acquired_at": [start + pd.Timedelta(hours=48 * i) for i in range(count)],
                "sensor": ["VIIRS_SNPP"] * count,
                "frp": [5.0] * count,
                "brightness": [330.0] * count,
                "brightness_temperature_difference": [20.0] * count,
                "daynight": ["N"] * count,
                "confidence_score": [0.6] * count,
                "firms_type_reference": [0.0] * count,
            }
        )
        config = {
            "models": {
                "event_spatial_km": 1.25,
                "event_temporal_hours": 36,
                "event_min_samples": 2,
            }
        }
        began = time.perf_counter()
        _, events = pipeline.cluster_events(detections, config)
        elapsed = time.perf_counter() - began
        self.assertEqual(len(events), count)
        self.assertLess(elapsed, 5.0)

    def test_nearest_facility_prunes_distant_complex_geometries(self):
        event_count = 250
        facility_count = 250
        vertices_per_facility = 40
        events = pd.DataFrame(
            {
                "latitude": [23.2 + 2.0 * index / event_count for index in range(event_count)],
                "longitude": [81.5 + 2.0 * index / event_count for index in range(event_count)],
            }
        )
        facilities = pd.DataFrame(
            [
                {
                    "facility_id": f"F{index}",
                    "latitude": 24.0,
                    "longitude": 82.0 + index / 1000,
                    "name": f"Facility {index}",
                    "kind": "test",
                    "_geometry_parts": [
                        [
                            (24.0 + vertex * 1e-5, 82.0 + index / 1000 + vertex * 1e-5)
                            for vertex in range(vertices_per_facility)
                        ]
                    ],
                }
                for index in range(facility_count)
            ]
        )

        began = time.perf_counter()
        result = pipeline.nearest_facility(events, facilities)
        elapsed = time.perf_counter() - began

        self.assertEqual(len(result), event_count)
        self.assertTrue(result["nearest_facility_id"].str.startswith("F").all())
        self.assertLess(elapsed, 4.0)

    def test_nearest_facility_uses_full_geometry_instead_of_centroid(self):
        events = pd.DataFrame([{"latitude": 24.0, "longitude": 82.01}])
        facilities = pd.DataFrame(
            [
                {
                    "facility_id": "long-footprint",
                    "latitude": 24.0,
                    "longitude": 82.5,
                    "name": "Long footprint",
                    "kind": "test",
                    "_geometry_parts": [[(24.0, 82.0), (24.0, 83.0)]],
                },
                {
                    "facility_id": "near-centroid",
                    "latitude": 24.0,
                    "longitude": 82.02,
                    "name": "Near centroid",
                    "kind": "test",
                    "_geometry_parts": [[(24.0, 82.02)]],
                },
            ]
        )

        result = pipeline.nearest_facility(events, facilities)

        self.assertEqual(result.loc[0, "nearest_facility_id"], "long-footprint")
        self.assertAlmostEqual(result.loc[0, "facility_distance_km"], 0.0)

    def test_history_is_not_sufficient_until_anomaly_score_is_available(self):
        events = pd.DataFrame([
            {
                "event_id": f"E{index}", "site_id": "S1",
                "start_time": pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=index),
                "frp_peak_mw": 10.0 + index, "frp_median_mw": 8.0 + index,
                "detection_peak": 2.0 + index, "duration_hours": 1.0 + index,
                "spread_km": 0.1 + index / 10,
                "brightness_temperature_difference_max": 20.0 + index,
            }
            for index in range(7)
        ])
        config = {"models": {
            "min_history_events": 6, "isolation_min_training_events": 32,
            "isolation_trees": 10, "random_state": 26162,
        }}

        result = pipeline.score_anomalies(events, config)

        self.assertTrue(pd.isna(result.loc[6, "anomaly_score"]))
        self.assertFalse(result.loc[6, "history_sufficient"])


if __name__ == "__main__":
    unittest.main()
