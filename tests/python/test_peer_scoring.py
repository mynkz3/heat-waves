"""Past-only peer ranking retains detections and is never a fire label."""
import unittest

import numpy as np
import pandas as pd

from backend import pipeline


def settings():
    return {
        "aoi": {"bbox": [81.5, 23.2, 83.5, 25.2]},
        "models": {"min_history_events": 6, "min_calibration_events": 32,
                   "isolation_min_training_events": 32, "isolation_trees": 10,
                   "random_state": 26162, "anomaly_percentile": .95},
        "vegetation_peers": {"enabled": True, "min_events": 32, "min_days": 8,
                             "min_cells": 3, "radius_km": 100, "exclude_radius_km": 2,
                             "season_window_days": 90, "history_window_days": 2557,
                             "gap_days": 7, "cell_km": 5, "max_events": 2000},
    }


def episodes():
    rows = []
    for i in range(36):
        start = pd.Timestamp("2023-01-01", tz="UTC") + pd.Timedelta(days=i)
        rows.append(dict(
            event_id=f"P{i:03}", site_id="", start_time=start, end_time=start,
            latitude=24.3 + (i % 6) * .06, longitude=82.,
            context_label="vegetation_associated", worldcover_dominant="tree_cover",
            worldcover_coverage=1., data_quality="science_quality",
            model_sensor="VIIRS_SNPP", model_daynight="D",
            frp_peak_mw=10., frp_median_mw=10., detection_peak=2.,
            duration_hours=0., spread_km=0., brightness_temperature_difference_max=20.,
            anomaly_score=np.nan, history_sufficient=False, history_event_count=0,
            model_feature_count=0, calibration_event_count=0,
            baseline_mode="insufficient_history", review_priority="unscored",
        ))
    rows.append({**rows[-1], "event_id": "TARGET", "latitude": 24.,
                 "start_time": pd.Timestamp("2023-03-01", tz="UTC"),
                 "end_time": pd.Timestamp("2023-03-01", tz="UTC")})
    return pd.DataFrame(rows)


class VegetationPeerTests(unittest.TestCase):
    def score(self, frame, config=None):
        self.assertTrue(hasattr(pipeline, "score_vegetation_peers"),
                        "Vegetation peer scoring is not implemented")
        return pipeline.score_vegetation_peers(frame, config or settings())

    def test_isolated_vegetation_gets_peer_score_without_claiming_site_history(self):
        source = episodes()
        saved = source.copy(deep=True)
        result = self.score(source)
        row = result.iloc[-1]
        self.assertEqual(row.score_method, "vegetation_peer")
        self.assertEqual(row.peer_reference_count, 36)
        self.assertEqual(row.peer_score, .5)
        self.assertEqual(row.anomaly_score, .5)
        self.assertFalse(row.history_sufficient)
        self.assertEqual(row.history_event_count, 0)
        self.assertEqual(len(result), len(source))
        pd.testing.assert_frame_equal(source, saved)

    def test_heat_growth_ranks_above_tied_peers_without_relabelling_context(self):
        source = episodes()
        source.loc[source.index[-1], "frp_peak_mw"] = 100.
        result = pipeline.consensus(self.score(source), settings()).iloc[-1]
        self.assertGreater(result.anomaly_score, .95)
        self.assertLess(result.anomaly_score, 1.)
        self.assertEqual(result.decision, "likely_vegetation_fire")
        self.assertEqual(result.incident_verification, "unverified")
        self.assertEqual(result.score_status, "scored")

    def test_industrial_mining_unknown_and_existing_site_scores_are_preserved(self):
        source = episodes()
        additions = []
        for i, (context, score) in enumerate([
            ("industrial_associated", .98), ("industrial_associated", np.nan),
            ("mixed", np.nan), ("unknown", np.nan), ("vegetation_associated", .3),
        ]):
            additions.append({**source.iloc[-1].to_dict(), "event_id": f"KEEP{i}",
                              "context_label": context, "anomaly_score": score,
                              "review_priority": "high" if score == .98 else "lower"})
        source = pd.concat([source, pd.DataFrame(additions)], ignore_index=True)
        result = self.score(source)
        pd.testing.assert_frame_equal(source.iloc[-5:], result[source.columns].iloc[-5:])

    def test_future_current_day_overlapping_and_recent_events_cannot_be_peers(self):
        source = episodes()
        target = source.iloc[-1].to_dict()
        extras = [
            {**target, "event_id": "FUTURE", "start_time": pd.Timestamp("2024-03-01", tz="UTC"),
             "end_time": pd.Timestamp("2024-03-01", tz="UTC")},
            {**target, "event_id": "SAME_DAY", "latitude": 24.4},
            {**source.iloc[0].to_dict(), "event_id": "OVERLAP",
             "end_time": pd.Timestamp("2023-03-02", tz="UTC")},
            {**target, "event_id": "RECENT", "latitude": 24.4,
             "start_time": pd.Timestamp("2023-02-28", tz="UTC"),
             "end_time": pd.Timestamp("2023-02-28", tz="UTC")},
        ]
        short = self.score(source).set_index("event_id").loc["TARGET"]
        long = self.score(pd.concat([source, pd.DataFrame(extras)], ignore_index=True))
        row = long.set_index("event_id").loc["TARGET"]
        self.assertEqual(row.peer_reference_count, 36)
        self.assertEqual(row.peer_score, short.peer_score)
        self.assertEqual(row.peer_reference_hash, short.peer_reference_hash)

    def test_peer_matching_does_not_borrow_other_sensor_phase_cover_or_nrt(self):
        for column, value in [("model_sensor", "VIIRS_NOAA20"), ("model_daynight", "N"),
                              ("worldcover_dominant", "cropland"),
                              ("data_quality", "near_real_time"),
                              ("context_label", "industrial_associated")]:
            with self.subTest(column=column):
                source = episodes()
                source.loc[source.index[:-1], column] = value
                row = self.score(source).iloc[-1]
                self.assertTrue(pd.isna(row.anomaly_score))
                self.assertEqual(row.peer_status, "insufficient_peers")

    def test_near_real_time_candidate_can_use_science_peers(self):
        source = episodes()
        source.loc[source.index[-1], "data_quality"] = "near_real_time"
        row = self.score(source).iloc[-1]
        self.assertEqual(row.peer_score, .5)
        self.assertEqual(row.data_quality, "near_real_time")

    def test_time_season_and_distance_limits_are_enforced(self):
        for config_change, column, value in [
            ({}, "latitude", 23.21),
            ({}, "latitude", 24.),
            ({"season_window_days": 5}, None, None),
            ({"history_window_days": 10}, None, None),
        ]:
            with self.subTest(config_change=config_change, value=value):
                source, config = episodes(), settings()
                config["vegetation_peers"].update(config_change)
                if column:
                    source.loc[source.index[:-1], column] = value
                    if value == 23.21:
                        source.loc[source.index[:-1], "longitude"] = 81.51
                row = self.score(source, config).iloc[-1]
                self.assertTrue(pd.isna(row.peer_score))

    def test_early_history_and_missing_features_remain_unscored_with_peer_reason(self):
        source = episodes()
        source.loc[source.index[-1], ["frp_peak_mw", "detection_peak", "spread_km"]] = np.nan
        result = pipeline.consensus(self.score(source), settings())
        self.assertEqual(result.iloc[0].score_status, "insufficient_peers")
        self.assertTrue(pd.isna(result.iloc[0].anomaly_score))
        self.assertEqual(result.iloc[-1].score_status, "insufficient_peer_features")
        self.assertTrue(pd.isna(result.iloc[-1].anomaly_score))
        self.assertIn("peer", result.iloc[-1].score_reason.lower())

    def test_unknown_phase_landcover_and_outside_aoi_abstain(self):
        for column, value in [("model_daynight", "unknown"), ("worldcover_dominant", "water"),
                              ("worldcover_coverage", .2), ("latitude", 40.)]:
            with self.subTest(column=column):
                source = episodes()
                source.loc[source.index[-1], column] = value
                row = self.score(source).iloc[-1]
                self.assertTrue(pd.isna(row.peer_score))
                self.assertTrue(row.peer_reason)

    def test_many_observations_at_one_location_do_not_make_diverse_peers(self):
        source = episodes()
        source.loc[source.index[:-1], "latitude"] = 24.4
        row = self.score(source).iloc[-1]
        self.assertTrue(pd.isna(row.peer_score))
        self.assertEqual(row.peer_status, "insufficient_peer_cells")

    def test_same_cell_day_is_not_counted_repeatedly_and_order_does_not_matter(self):
        source = episodes()
        duplicates = source.iloc[:-1].copy()
        duplicates["event_id"] = "ZZ" + duplicates["event_id"]
        duplicates["frp_peak_mw"] = 500.
        combined = pd.concat([source, duplicates], ignore_index=True)
        row = self.score(combined).set_index("event_id").loc["TARGET"]
        shuffled = self.score(combined.sample(frac=1, random_state=3)).set_index("event_id").loc["TARGET"]
        self.assertEqual(row.peer_reference_count, 36)
        self.assertEqual(row.peer_score, .5)
        self.assertEqual(row.peer_reference_hash, shuffled.peer_reference_hash)

    def test_scoring_is_idempotent_and_preserves_non_default_index(self):
        source = episodes()
        source.index = np.arange(len(source)) * 3 + 7
        once = self.score(source)
        pd.testing.assert_frame_equal(once, self.score(once))
        self.assertEqual(once.index.tolist(), source.index.tolist())

    def test_disabled_peer_scoring_leaves_existing_data_untouched(self):
        source, config = episodes(), settings()
        config["vegetation_peers"]["enabled"] = False
        pd.testing.assert_frame_equal(self.score(source, config), source)

    def test_regular_pipeline_scoring_reaches_peer_fallback(self):
        result = pipeline.score_anomalies(episodes(), settings())
        self.assertTrue(pd.notna(result.iloc[-1].anomaly_score),
                        "The production scorer must invoke peer fallback for vegetation")

    def test_invalid_peer_configuration_fails_before_scoring(self):
        config = settings()
        config["vegetation_peers"]["min_events"] = 0
        with self.assertRaises(ValueError):
            self.score(episodes(), config)

    def test_secondary_observation_duration_and_spread_cannot_inflate_peer_score(self):
        source = episodes()
        source.loc[source.index[-1], ["duration_hours", "spread_km"]] = [12., .55]
        self.assertEqual(self.score(source).iloc[-1].peer_score, .5)

    def test_complete_same_cell_day_alternative_survives_incomplete_low_id(self):
        source = episodes()
        alternatives = source.iloc[:-1].copy()
        alternatives["event_id"] = "ZZ" + alternatives["event_id"]
        source.loc[source.index[:-1], "frp_peak_mw"] = np.nan
        row = self.score(pd.concat([source, alternatives], ignore_index=True)).set_index("event_id").loc["TARGET"]
        self.assertEqual(row.peer_reference_count, 36)
        self.assertEqual(row.peer_score, .5)

    def test_diversity_counts_only_complete_reference_vectors(self):
        source = episodes()
        source.loc[source.index[:32], "latitude"] = 24.4
        source.loc[source.index[32:36], "frp_peak_mw"] = np.nan
        row = self.score(source).iloc[-1]
        self.assertEqual(row.peer_reference_count, 32)
        self.assertEqual(row.peer_status, "insufficient_peer_cells")

    def test_missing_cover_column_abstains_instead_of_crashing(self):
        row = self.score(episodes().drop(columns="worldcover_dominant")).iloc[-1]
        self.assertEqual(row.peer_status, "unknown_peer_landcover")

    def test_rescoring_does_not_reuse_stale_site_provenance(self):
        source = episodes().iloc[-1:].copy()
        source["context_label"] = "industrial_associated"
        source["site_id"] = "S1"
        source["site_anomaly_score"] = .5
        source["anomaly_score"] = .5
        row = pipeline.score_anomalies(source, settings()).iloc[0]
        self.assertTrue(pd.isna(row.site_anomaly_score))
        self.assertEqual(row.score_method, "unavailable")

    def test_fire_labels_and_arbitrary_site_ids_do_not_change_peer_scores(self):
        source = episodes()
        before = self.score(source)
        source["site_id"] = [f"invented_{i}" for i in range(len(source))]
        source["firms_type_2_reference_fraction"] = 1
        source["incident_verification"] = "invented_label_not_for_training"
        after = self.score(source)
        pd.testing.assert_series_equal(before.peer_score, after.peer_score)
        pd.testing.assert_series_equal(before.peer_reference_hash, after.peer_reference_hash)


if __name__ == "__main__":
    unittest.main()
