"""A full rescore must not carry diagnostics from a disabled peer model."""
import unittest

import pandas as pd

import pipeline
from test_peer_scoring import episodes, settings


class PeerRescoringTests(unittest.TestCase):
    def test_disabled_full_rescore_clears_old_peer_diagnostics(self):
        config = settings()
        first = pipeline.score_anomalies(episodes(), config)
        config["vegetation_peers"]["enabled"] = False
        second = pipeline.score_anomalies(first, config)
        target = second.set_index("event_id").loc["TARGET"]
        self.assertTrue(pd.isna(target.anomaly_score))
        self.assertNotEqual(target.get("score_method"), "vegetation_peer")
        self.assertTrue(pd.isna(target.get("peer_score")))
        self.assertNotEqual(target.get("peer_status"), "scored")


if __name__ == "__main__":
    unittest.main()
