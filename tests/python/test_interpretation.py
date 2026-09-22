"""Context/score separation must not change observations or manufacture scores."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from backend import pipeline


ROOT = Path(__file__).resolve().parents[2]
SETTINGS = {"models": {"anomaly_percentile": .95, "min_history_events": 6, "min_calibration_events": 32}}


def event(**changes):
    row = dict(event_id="E1", site_id="S1", context_label="industrial_associated",
               nearest_facility_kind="power=plant", history_event_count=10,
               model_feature_count=6, model_daynight="N", calibration_event_count=40,
               history_sufficient=False, anomaly_score=float("nan"),
               sentinel_evidence="inconclusive", incident_verification="unverified",
               latitude=24., longitude=82., start_time="2024-01-01T00:00:00+00:00",
               end_time="2024-01-01T01:00:00+00:00")
    row.update(changes)
    return row


class InterpretationTests(unittest.TestCase):
    def test_each_missing_score_gets_its_actual_gate_and_keeps_industrial_context(self):
        cases = [
            ({"site_id": "", "history_event_count": 0}, "no_recurrent_site"),
            ({"history_event_count": 5, "model_feature_count": 0}, "insufficient_site_history"),
            ({"model_feature_count": 2}, "insufficient_features"),
            ({"model_daynight": "unknown"}, "unknown_daynight"),
            ({"calibration_event_count": 31}, "insufficient_calibration"),
            ({}, "diagnostics_unavailable"),
        ]
        for changes, status in cases:
            with self.subTest(status=status):
                source = pd.DataFrame([event(**changes)])
                saved = source.copy(deep=True)
                result = pipeline.consensus(source, SETTINGS).iloc[0]
                self.assertIn("score_status", result)
                self.assertEqual(result.score_status, status)
                self.assertTrue(result.score_reason)
                self.assertEqual(result.decision, "industrial_associated_unscored")
                self.assertEqual(result.context_label, "industrial_associated")
                self.assertTrue(pd.isna(result.anomaly_score))
                pd.testing.assert_frame_equal(source, saved)

    def test_reason_uses_configured_history_and_calibration_requirements(self):
        config = copy.deepcopy(SETTINGS)
        config["models"].update(min_history_events=12, min_calibration_events=50)
        for changes, status, required in [
            ({"history_event_count": 10}, "insufficient_site_history", "12"),
            ({"history_event_count": 12, "calibration_event_count": 40}, "insufficient_calibration", "50"),
        ]:
            result = pipeline.consensus(pd.DataFrame([event(**changes)]), config).iloc[0]
            self.assertIn("score_status", result)
            self.assertEqual(result.score_status, status)
            self.assertIn(required, result.score_reason)

    def test_quarry_has_separate_categories_without_changing_scores(self):
        for score, expected in [
            (.97, "suspected_abnormal_mining_associated_event"),
            (.2, "mining_associated_thermal_source"),
            (float("nan"), "mining_associated_unscored"),
        ]:
            source = pd.DataFrame([event(nearest_facility_kind="landuse=quarry", anomaly_score=score,
                                        history_sufficient=pd.notna(score))])
            result = pipeline.consensus(source, SETTINGS)
            self.assertEqual(result.loc[0, "decision"], expected)
            self.assertEqual(result.loc[0, "source_context"], "mining_quarry")
            pd.testing.assert_series_equal(result.anomaly_score, source.anomaly_score)
            self.assertEqual(result.loc[0, "sentinel_evidence"], "inconclusive")

    def test_bare_land_and_facility_names_do_not_invent_mining_context(self):
        for kind in ["power=plant", "industrial=cement", "landuse=industrial", None]:
            source = pd.DataFrame([event(nearest_facility_kind=kind, nearest_facility_name="Quarry Road Works",
                                        bare_fraction=.95, anomaly_score=.97, history_sufficient=True)])
            result = pipeline.consensus(source, SETTINGS).iloc[0]
            self.assertIn("source_context", result)
            self.assertEqual(result.source_context, "industrial_associated")
            self.assertEqual(result.decision, "suspected_abnormal_industrial_associated_event")

    def test_nearby_quarry_does_not_override_mixed_or_vegetation_context(self):
        for context, decision in [("mixed", "mixed_or_unknown"), ("unknown", "mixed_or_unknown"),
                                  ("vegetation_associated", "likely_vegetation_fire")]:
            result = pipeline.consensus(pd.DataFrame([event(context_label=context,
                                        nearest_facility_kind="landuse=quarry")]), SETTINGS).iloc[0]
            self.assertIn("source_context", result)
            self.assertEqual(result.source_context, context)
            self.assertEqual(result.decision, decision)

    def test_zero_score_is_available_and_recorded_verification_is_preserved(self):
        source = pd.DataFrame([event(anomaly_score=0., history_sufficient=True,
                                    incident_verification="independently_audited_non_incident")])
        result = pipeline.consensus(source, SETTINGS).iloc[0]
        self.assertIn("score_status", result)
        self.assertEqual(result.score_status, "scored")
        self.assertIsNone(result.score_reason)
        self.assertEqual(result.decision, "routine_persistent_industrial_thermal_source")
        self.assertEqual(result.anomaly_score, 0.)
        self.assertEqual(result.incident_verification, source.loc[0, "incident_verification"])


class InterpretationRefreshTests(unittest.TestCase):
    def fixture(self, root):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        config["paths"] = {"output": "outputs", "cache": str(root / "cache")}
        path = root / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        out = root / "outputs"
        out.mkdir()
        rows = pd.DataFrame([
            event(event_id="E1", nearest_facility_kind="landuse=quarry", anomaly_score=.9734123456789012,
                  history_sufficient=True, decision="suspected_abnormal_industrial_associated_event"),
            event(event_id="E2", history_event_count=2, model_feature_count=0, decision="mixed_or_unknown"),
        ])
        rows.to_csv(out / "events.csv", index=False)
        rows.iloc[:1].to_csv(out / "anomaly_rankings.csv", index=False)
        pipeline.write_json(out / "events.geojson", pipeline.frame_to_geojson(rows, "event_id"))
        pipeline.write_json(out / "facilities.geojson", {"type": "FeatureCollection", "features": []})
        manifest = {"generated_at": "2026-09-08T18:00:00Z", "corroborated_at": "2026-09-08T19:00:00Z",
                    "models": {"parameters": config["models"]}, "outputs": {"events": 2, "scored_events": 1}}
        pipeline.write_json(out / "run_manifest.json", manifest)
        (out / "index.html").write_text("previous valid map", encoding="utf-8")
        (out / "sentinel_summary.json").write_text('{"unchanged":true}', encoding="utf-8")
        return path, out

    def test_refresh_changes_only_interpretation_and_retains_original_core_times(self):
        self.assertTrue(hasattr(pipeline, "reinterpret_existing"), "Missing safe interpretation-only refresh")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, out = self.fixture(root)
            before_csv = pd.read_csv(out / "events.csv", float_precision="round_trip")
            before_geo = json.loads((out / "events.geojson").read_text(encoding="utf-8"))
            sentinel_before = (out / "sentinel_summary.json").read_bytes()
            with patch.object(pipeline, "score_anomalies", side_effect=AssertionError("Must not refit")), \
                 patch.object(pipeline, "acquire_sentinel", side_effect=AssertionError("Must not reacquire")):
                report = pipeline.reinterpret_existing(root, config_path)
            after_csv = pd.read_csv(out / "events.csv", float_precision="round_trip")
            pd.testing.assert_series_equal(before_csv.anomaly_score, after_csv.anomaly_score)
            self.assertEqual(after_csv.decision.tolist(), ["suspected_abnormal_mining_associated_event", "industrial_associated_unscored"])
            after_geo = json.loads((out / "events.geojson").read_text(encoding="utf-8"))
            for old, new in zip(before_geo["features"], after_geo["features"], strict=True):
                self.assertEqual(old["geometry"], new["geometry"])
                for key, value in old["properties"].items():
                    if key not in {"decision", "decision_basis"}:
                        self.assertEqual(value, new["properties"][key], key)
            self.assertEqual((out / "sentinel_summary.json").read_bytes(), sentinel_before)
            self.assertEqual(report["generated_at"], "2026-09-08T18:00:00Z")
            self.assertEqual(report["corroborated_at"], "2026-09-08T19:00:00Z")
            self.assertEqual(report["outputs"]["scored_events"], 1)
            self.assertEqual(report["interpretation"]["schema_version"], 2)
            self.assertEqual(report["interpretation"]["score_status_counts"], {"scored": 1, "insufficient_site_history": 1})
            backup = Path(report["interpretation"]["backup_directory"])
            self.assertEqual((backup / "index.html").read_text(encoding="utf-8"), "previous valid map")

    def test_failed_map_refresh_restores_every_previous_export(self):
        self.assertTrue(hasattr(pipeline, "reinterpret_existing"), "Missing safe interpretation-only refresh")
        for error in (OSError, KeyboardInterrupt):
            with self.subTest(error=error.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config_path, out = self.fixture(root)
                before = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}
                with patch.object(pipeline, "write_offline_viewer", side_effect=error("simulated export failure")):
                    with self.assertRaisesRegex(error, "simulated export failure"):
                        pipeline.reinterpret_existing(root, config_path)
                self.assertEqual(before, {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()})

    def test_refresh_rejects_changed_model_requirements_before_writing(self):
        self.assertTrue(hasattr(pipeline, "reinterpret_existing"), "Missing safe interpretation-only refresh")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, out = self.fixture(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["models"]["min_history_events"] = 12
            config_path.write_text(json.dumps(config), encoding="utf-8")
            before = (out / "events.csv").read_bytes()
            with self.assertRaisesRegex(ValueError, "model parameters"):
                pipeline.reinterpret_existing(root, config_path)
            self.assertEqual(before, (out / "events.csv").read_bytes())


if __name__ == "__main__":
    unittest.main()
