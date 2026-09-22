"""Skipping acquisition must not erase trustworthy saved evidence."""
import unittest

import pandas as pd

import pipeline


class SentinelReuseTests(unittest.TestCase):
    def test_reuses_only_identical_event_identity_and_observation_bounds(self):
        self.assertTrue(hasattr(pipeline, "reuse_sentinel_evidence"))
        events = pd.DataFrame([
            dict(event_id=f"E{i}", latitude=24., longitude=82.,
                 start_time="2024-01-01T00:00:00Z", end_time="2024-01-01T01:00:00Z")
            for i in range(3)
        ])
        previous = events.iloc[:2].copy()
        previous["sentinel_status"] = "analysed"
        previous["sentinel_evidence"] = "burn_like_change"
        previous["sentinel_dnbr_median"] = .2
        previous["sentinel_radiometry_status"] = "legacy_offset_unverified"
        previous.loc[1, "latitude"] = 24.1
        evidence = pd.DataFrame(dict(event_id=events.event_id, sentinel_status="disabled",
                                     sentinel_evidence="inconclusive", sentinel_dnbr_median=None))
        saved = evidence.copy(deep=True)
        result = pipeline.reuse_sentinel_evidence(events, evidence, previous)
        self.assertEqual(result.iloc[0].sentinel_status, "analysed")
        self.assertEqual(result.iloc[0].sentinel_dnbr_median, .2)
        self.assertEqual(result.iloc[0].sentinel_radiometry_status, "legacy_offset_unverified")
        self.assertTrue(result.iloc[0].sentinel_reused)
        self.assertEqual(result.iloc[1].sentinel_status, "disabled")
        self.assertEqual(result.iloc[2].sentinel_status, "disabled")
        pd.testing.assert_frame_equal(saved, evidence)


if __name__ == "__main__":
    unittest.main()
