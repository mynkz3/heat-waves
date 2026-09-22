import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import pipeline


class UpgradeScientificTests(unittest.TestCase):
    def test_vegetated_industrial_neighbourhood_is_conflicting_context(self):
        frame = pd.DataFrame([dict(facility_distance_km=0.4, worldcover_coverage=1,
                                   vegetation_fraction=0.75, built_fraction=0.1, bare_fraction=0.15)])
        config = {"context": {"industrial_near_km": 2, "industrial_context_km": 5,
                              "vegetation_fraction": 0.6}}
        result = pipeline.assign_context(frame, config)
        self.assertEqual(result.loc[0, "context_label"], "mixed")

    def events(self, count=10):
        return pd.DataFrame([dict(event_id=f"E{i}", site_id="S1",
                                 start_time=pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=i),
                                 end_time=pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=i, hours=1),
                                 model_sensor="VIIRS_SNPP", model_daynight="N",
                                 frp_peak_mw=10+i, frp_median_mw=8+i, detection_peak=2+i,
                                 duration_hours=1+i, spread_km=.1+i/10,
                                 brightness_temperature_difference_max=20+i) for i in range(count)])

    def constant_events(self):
        events = self.events(48)
        for feature in pipeline.DYNAMIC_FEATURES:
            events[feature] = events.at[0, feature]
        events["data_quality"] = "science_quality"
        return events

    def config(self):
        return {"models": {"min_history_events": 6, "isolation_min_training_events": 32,
                           "isolation_trees": 10, "random_state": 26162}}

    def test_new_sensor_cannot_borrow_a_different_platform_baseline(self):
        events = self.events()
        events.loc[9, "model_sensor"] = "VIIRS_NOAA20"
        result = pipeline.score_anomalies(events, self.config())
        self.assertEqual(result.loc[9, "history_event_count"], 0)
        self.assertTrue(pd.isna(result.loc[9, "anomaly_score"]))

    def test_unfinished_earlier_events_are_not_training_history(self):
        events = self.events()
        events.loc[:8, "end_time"] = pd.Timestamp("2024-02-01", tz="UTC")
        result = pipeline.score_anomalies(events, self.config())
        self.assertEqual(result.loc[9, "history_event_count"], 0)

    def test_appending_future_events_does_not_change_existing_scores(self):
        for events in (self.events(), self.constant_events()):
            events.loc[events.index[-1], list(pipeline.DYNAMIC_FEATURES)] *= 100
            short = pipeline.score_anomalies(events.iloc[:-1], self.config())
            long = pipeline.score_anomalies(events, self.config())
            for column in ("robust_anomaly_percentile", "isolation_anomaly_percentile", "anomaly_score"):
                np.testing.assert_allclose(short[column], long.iloc[:-1][column], equal_nan=True)

    def test_constant_baseline_ranks_at_midpoint_without_extreme_alerts(self):
        result = pipeline.score_anomalies(self.constant_events(), self.config())
        for column in ("robust_anomaly_percentile", "isolation_anomaly_percentile", "anomaly_score"):
            with self.subTest(column=column):
                scores = result[column].dropna()
                self.assertFalse(scores.empty)
                np.testing.assert_allclose(scores, 0.5)
        self.assertTrue(result.loc[result["history_sufficient"], "review_priority"].eq("lower").all())

    def test_extreme_growth_ranks_above_an_unchanged_baseline(self):
        events = self.constant_events()
        unchanged = pipeline.score_anomalies(events, self.config()).iloc[-1]
        events.loc[events.index[-1], list(pipeline.DYNAMIC_FEATURES)] *= 100
        growth = pipeline.score_anomalies(events, self.config()).iloc[-1]
        for column in ("robust_anomaly_percentile", "anomaly_score"):
            with self.subTest(column=column):
                self.assertGreater(growth[column], unchanged[column])
                self.assertLess(growth[column], 1)

    def test_minimum_calibration_population_is_enforced(self):
        config = self.config()
        config["models"]["min_calibration_events"] = 32
        result = pipeline.score_anomalies(self.events(), config)
        self.assertTrue(result["anomaly_score"].isna().all())

    def test_provisional_observations_do_not_enter_training_history(self):
        events = self.events()
        events["data_quality"] = "near_real_time"
        result = pipeline.score_anomalies(events, self.config())
        self.assertTrue(result["history_event_count"].eq(0).all())
        self.assertTrue(result["anomaly_score"].isna().all())

    def test_provisional_event_can_be_scored_against_science_history(self):
        events = self.events()
        events["data_quality"] = "science_quality"
        events.loc[9, "data_quality"] = "near_real_time"
        result = pipeline.score_anomalies(events, self.config())
        self.assertTrue(pd.notna(result.loc[9, "anomaly_score"]))

    def test_ingestion_prefers_science_record_over_revised_nrt_frp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = dict(latitude=24, longitude=82, acq_date="2024-01-01", acq_time="0120",
                          satellite="N", instrument="VIIRS", bright_ti4=340, bright_ti5=300,
                          scan=.4, track=.4, confidence="n", daynight="N", type=0)
            pd.DataFrame([{**common, "version": "2", "frp": 10}]).to_csv(root / "science.csv", index=False)
            pd.DataFrame([{**common, "version": "2.0NRT", "frp": 11}]).to_csv(root / "nrt.csv", index=False)
            config = {"aoi": {"bbox": [81.5, 23.2, 83.5, 25.2]},
                      "inputs": {"firms_globs": ["*.csv"], "expected_years": [2024]}}
            detections, audit = pipeline.ingest_firms(root, config)
            self.assertEqual(len(detections), 1)
            self.assertEqual(detections.iloc[0]["frp"], 10)
            self.assertEqual(detections.iloc[0]["data_quality"], "science_quality")

    def test_unknown_instrument_and_future_observations_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = dict(latitude=24, longitude=82, acq_date="2024-01-01", acq_time="0120",
                          satellite="N", instrument="VIIRS", bright_ti4=340, bright_ti5=300,
                          frp=10, scan=.4, track=.4, confidence="n", daynight="N", type=0, version="2")
            rows = [common, {**common, "instrument": "unknown", "satellite": "unknown"},
                    {**common, "acq_date": "2099-01-01"}]
            pd.DataFrame(rows).to_csv(root / "sample.csv", index=False)
            config = {"aoi": {"bbox": [81.5, 23.2, 83.5, 25.2]},
                      "inputs": {"firms_globs": ["*.csv"], "expected_years": [2024]}}
            detections, _ = pipeline.ingest_firms(root, config)
            self.assertEqual(len(detections), 1)

    def test_event_model_features_do_not_mix_platform_measurements(self):
        detections = pd.DataFrame([dict(detection_id=f"D{i}", latitude=24, longitude=82,
                                       acquired_at=pd.Timestamp("2024-01-01", tz="UTC"),
                                       sensor=sensor, frp=frp, brightness=330,
                                       brightness_temperature_difference=20, daynight="N",
                                       confidence_score=.6, firms_type_reference=0)
                                   for i, (sensor, frp) in enumerate([
                                       ("VIIRS_SNPP", 5), ("VIIRS_SNPP", 5), ("VIIRS_NOAA20", 100)])])
        config = {"models": {"event_spatial_km": 2, "event_temporal_hours": 24, "event_min_samples": 2}}
        _, events = pipeline.cluster_events(detections, config)
        self.assertEqual(events.loc[0, "frp_peak_mw"], 10)
        self.assertEqual(events.loc[0, "model_sensor"], "VIIRS_SNPP")

        # Daytime radiation must not be pooled into the preferred nighttime row.
        detections.loc[2, "sensor"] = "VIIRS_SNPP"
        detections.loc[2, "daynight"] = "D"
        _, events = pipeline.cluster_events(detections, config)
        self.assertEqual(events.loc[0, "frp_peak_mw"], 10)
        self.assertEqual(events.loc[0, "model_daynight"], "N")

    def test_missing_frp_does_not_turn_into_zero_heat(self):
        data = pd.DataFrame([dict(detection_id="D0", latitude=24, longitude=82,
                                 acquired_at=pd.Timestamp("2024-01-01", tz="UTC"),
                                 sensor="VIIRS_SNPP", frp=np.nan, brightness=330,
                                 brightness_temperature_difference=20, daynight="N",
                                 confidence_score=.6, firms_type_reference=0)])
        config = {"models": {"event_spatial_km": 2, "event_temporal_hours": 24, "event_min_samples": 2}}
        _, events = pipeline.cluster_events(data, config)
        self.assertTrue(pd.isna(events.loc[0, "frp_peak_mw"]))


if __name__ == "__main__":
    unittest.main()
