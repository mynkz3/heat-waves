import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import pipeline


class StabilityTests(unittest.TestCase):
    def test_all_candidates_report_every_requested_resample(self):
        rows = []
        for site in range(4):
            for episode in range(4):
                for point in range(3):
                    rows.append({
                        'detection_id': f'D{site}-{episode}-{point}',
                        'latitude': 24 + site * .06, 'longitude': 82 + point * .003,
                        'acquired_at': pd.Timestamp('2024-01-01', tz='UTC') + pd.Timedelta(days=episode * 3, hours=point),
                        'sensor': 'VIIRS_SNPP', 'frp': 5 + point, 'brightness': 330.,
                        'brightness_temperature_difference': 20., 'daynight': 'N',
                        'confidence_score': .6, 'firms_type_reference': float('nan'),
                    })
        detections = pd.DataFrame(rows)
        config = pipeline.load_config(Path(__file__).with_name('config.json'))
        config['models']['tuning_repeats'] = 3
        config['paths']['output'] = 'out'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / 'config.json'
            config_path.write_text(json.dumps(config), encoding='utf-8')
            with patch.object(pipeline, 'ingest_firms', return_value=(detections, {'missing_expected_years': [2020]})):
                report = pipeline.tune_hyperparameters(root, config_path)
            for trial in report['event_trials'] + report['site_trials']:
                self.assertEqual(len(trial['subsample_ari_runs']), 3)
                self.assertAlmostEqual(trial['subsample_ari'], sum(trial['subsample_ari_runs']) / 3)
                self.assertLessEqual(trial['subsample_ari_min'], trial['subsample_ari_max'])
            self.assertEqual(report['input_detections'], 48)
            self.assertEqual(report['resampling_repeats'], 3)


if __name__ == '__main__':
    unittest.main()
