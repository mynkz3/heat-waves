import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from PIL import Image

import make_ppt_figures as figures


SCRIPT = Path(__file__).with_name("make_ppt_figures.py")
FIGURE_NAMES = [
    "01_pipeline_counts.png",
    "02_monthly_detections.png",
    "03_decision_distribution.png",
    "04_event_map.png",
    "05_anomaly_scores.png",
    "06_clustering_stability.png",
    "07_firms_silver_agreement.png",
]


def run_generator(root: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["MPLBACKEND"] = "Agg"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )


class PptFigureTests(unittest.TestCase):
    def test_new_industrial_and_mining_categories_retain_broad_silver_context(self):
        decisions = [
            "industrial_associated_unscored",
            "suspected_abnormal_mining_associated_event",
            "mining_associated_thermal_source",
            "mining_associated_unscored",
        ]
        events = pd.DataFrame({"event_id": ["E1", "E2", "E3", "E4"], "decision": decisions})
        detections = pd.DataFrame([
            {"event_id": event_id, "firms_type_reference": 2}
            for event_id in events.event_id for _ in range(2)
        ])
        result = figures.silver_result(events, detections)
        self.assertEqual(result["eligible"], 4)
        self.assertEqual(result["agreed"], 4)
        self.assertEqual(result["matrix"].loc["Static source (Type 2)", "Abstain / mixed"], 0)
        # An explicit mixed context must still take precedence over legacy decision fallback.
        events["context_label"] = "mixed"
        result = figures.silver_result(events, detections)
        self.assertEqual(result["agreed"], 0)
        self.assertEqual(result["matrix"].loc["Static source (Type 2)", "Abstain / mixed"], 4)

    def test_summary_explains_new_categories_without_claiming_incidents(self):
        events = pd.DataFrame([
            {"decision": "industrial_associated_unscored", "anomaly_score": None},
            {"decision": "mining_associated_unscored", "anomaly_score": None},
            {"decision": "suspected_abnormal_mining_associated_event", "anomaly_score": .97},
            {"decision": "mining_associated_thermal_source", "anomaly_score": .2},
        ])
        frames = {name: pd.DataFrame() for name in ("detections", "sites", "facilities")}
        frames["events"] = events
        text = figures.summary_text({}, frames, {}, {"eligible": 0}, Path("figures"))
        self.assertIn("Industrial context, unscored: 1", text)
        self.assertIn("Mining/quarry context, unscored: 1", text)
        self.assertIn("Unusual, mining/quarry context: 1", text)
        self.assertIn("Recurring mining/quarry heat: 1", text)
        self.assertIn("2/4 events scored", text)
        self.assertIn("not confirmed fires", text)

    def test_generates_all_300_dpi_figures_and_an_honest_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outputs = root / "outputs"
            outputs.mkdir()
            (outputs / "run_manifest.json").write_text(
                json.dumps(
                    {
                        "aoi": {"name": "Pilot", "bbox": [81.5, 23.2, 83.5, 25.2]},
                        "input_audit": {
                            "aoi_rows": 6,
                            "date_min": "2024-01-01T00:00:00Z",
                            "date_max": "2024-03-01T00:00:00Z",
                            "available_years": [2024],
                            "missing_expected_years": [],
                        },
                        "outputs": {
                            "detections": 6,
                            "events": 3,
                            "persistent_sites": 1,
                            "osm_context_features": 1,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (outputs / "tuning_report.json").write_text(
                json.dumps(
                    {
                        "event_trials": [
                            {
                                "event_spatial_km": 0.75,
                                "event_temporal_hours": 12,
                                "subsample_ari": 0.72,
                                "events": 4,
                            },
                            {
                                "event_spatial_km": 1.25,
                                "event_temporal_hours": 24,
                                "subsample_ari": 0.91,
                                "events": 3,
                            },
                        ],
                        "site_trials": [
                            {
                                "site_max_diameter_km": 1.0,
                                "subsample_ari": 0.80,
                                "event_coverage": 0.50,
                            },
                            {
                                "site_max_diameter_km": 1.5,
                                "subsample_ari": 0.88,
                                "event_coverage": 0.67,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            pd.DataFrame(
                [
                    {
                        "event_id": "E1",
                        "start_time": "2024-01-01T00:00:00Z",
                        "latitude": 24.0,
                        "longitude": 82.0,
                        "detection_count": 2,
                        "context_label": "industrial_associated",
                        "decision": "routine_persistent_industrial_thermal_source",
                        "anomaly_score": 0.40,
                        "history_sufficient": True,
                    },
                    {
                        "event_id": "E2",
                        "start_time": "2024-02-01T00:00:00Z",
                        "latitude": 24.2,
                        "longitude": 82.2,
                        "detection_count": 2,
                        "context_label": "vegetation_associated",
                        "decision": "likely_vegetation_fire",
                        "anomaly_score": 0.80,
                        "history_sufficient": True,
                    },
                    {
                        "event_id": "E3",
                        "start_time": "2024-03-01T00:00:00Z",
                        "latitude": 24.4,
                        "longitude": 82.4,
                        "detection_count": 2,
                        "context_label": "mixed",
                        "decision": "mixed_or_unknown",
                        "anomaly_score": None,
                        "history_sufficient": False,
                    },
                ]
            ).to_csv(outputs / "events.csv", index=False)
            pd.DataFrame(
                [
                    {
                        "detection_id": f"D{index}",
                        "event_id": event_id,
                        "acquired_at": date,
                        "sensor": "MODIS_TERRA",
                        "firms_type_reference": reference,
                    }
                    for index, (event_id, date, reference) in enumerate(
                        [
                            ("E1", "2024-01-01T00:00:00Z", 2),
                            ("E1", "2024-01-02T00:00:00Z", 2),
                            ("E2", "2024-02-01T00:00:00Z", 0),
                            ("E2", "2024-02-02T00:00:00Z", 0),
                            ("E3", "2024-03-01T00:00:00Z", None),
                            ("E3", "2024-03-02T00:00:00Z", None),
                        ],
                        start=1,
                    )
                ]
            ).to_csv(outputs / "detections.csv", index=False)
            pd.DataFrame(
                [{"site_id": "S1", "latitude": 24.0, "longitude": 82.0, "event_count": 2}]
            ).to_csv(outputs / "sites.csv", index=False)
            pd.DataFrame(
                [
                    {
                        "facility_id": "F1",
                        "latitude": 24.05,
                        "longitude": 82.05,
                        "name": "Plant",
                        "kind": "power=plant",
                    }
                ]
            ).to_csv(outputs / "facilities.csv", index=False)

            result = run_generator(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            figure_dir = root / "reports" / "generated" / "figures"
            self.assertEqual(sorted(path.name for path in figure_dir.glob("*.png")), FIGURE_NAMES)
            for name in FIGURE_NAMES:
                with Image.open(figure_dir / name) as image:
                    self.assertGreaterEqual(image.width, 2000)
                    self.assertGreaterEqual(image.height, 1000)
                    self.assertAlmostEqual(image.info["dpi"][0], 300, delta=1)
                    self.assertAlmostEqual(image.info["dpi"][1], 300, delta=1)
            summary = (root / "reports" / "generated" / "results_summary.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("FIRMS silver agreement", summary)
            self.assertIn("2/3 events eligible", summary)
            self.assertIn("agreement, not an accuracy estimate", summary)

    def test_missing_tuning_and_empty_csvs_still_produce_the_full_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outputs = root / "outputs"
            outputs.mkdir()
            (outputs / "run_manifest.json").write_text("{}", encoding="utf-8")
            for name in ("events.csv", "detections.csv", "sites.csv", "facilities.csv"):
                (outputs / name).write_text("", encoding="utf-8")

            result = run_generator(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            figure_dir = root / "reports" / "generated" / "figures"
            self.assertEqual(sorted(path.name for path in figure_dir.glob("*.png")), FIGURE_NAMES)
            summary = (root / "reports" / "generated" / "results_summary.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Clustering stability: unavailable", summary)
            self.assertIn("FIRMS silver agreement: unavailable", summary)

    def test_partial_nonempty_csvs_do_not_require_optional_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outputs = root / "outputs"
            outputs.mkdir()
            (outputs / "run_manifest.json").write_text("{}", encoding="utf-8")
            pd.DataFrame(
                [{"event_id": "E1", "latitude": 24.0, "longitude": 82.0}]
            ).to_csv(outputs / "events.csv", index=False)
            pd.DataFrame(
                [{"detection_id": "D1", "acquired_at": "2024-01-01T00:00:00Z"}]
            ).to_csv(outputs / "detections.csv", index=False)
            pd.DataFrame([{"site_id": "S1"}]).to_csv(outputs / "sites.csv", index=False)
            pd.DataFrame([{"facility_id": "F1"}]).to_csv(outputs / "facilities.csv", index=False)

            result = run_generator(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            figure_dir = root / "reports" / "generated" / "figures"
            self.assertEqual(sorted(path.name for path in figure_dir.glob("*.png")), FIGURE_NAMES)


if __name__ == "__main__":
    unittest.main()
