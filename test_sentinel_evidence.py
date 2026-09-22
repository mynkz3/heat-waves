"""Offline, synthetic-image checks for the optional Sentinel evidence stage."""

import importlib.util
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_origin

if importlib.util.find_spec("sentinel_evidence"):
    import sentinel_evidence as se
else:
    se = None


def event(event_id="E1", **changes):
    row = dict(event_id=event_id, longitude=82.0, latitude=24.0,
               start_time="2025-04-10T12:00:00Z", end_time="2025-04-10T13:00:00Z",
               anomaly_score=0.99, context_label="industrial_associated",
               frp_peak_mw=40.0, site_id="S1")
    row.update(changes)
    return pd.Series(row)


def config(**changes):
    settings = dict(enabled=True, max_events=5, days_before=30, days_after=30,
                    max_scene_cloud=25, crop_radius_m=100, analysis_radius_m=60,
                    min_local_valid_fraction=0.4, workers=1,
                    collections=["sentinel-2-c1-l2a"], as_of="2025-05-20T00:00:00Z")
    settings.update(changes)
    return {"sentinel": settings, "models": {"anomaly_percentile": 0.95}}


def item(name, when, assets=None, tile="44QKL", bbox=None, collection=None):
    assets = assets or {key: {"href": f"https://example.invalid/{key}.tif",
                              "raster:bands": [{"scale": 0.0001, "offset": 0, "nodata": 0}]}
                        for key in ("nir08", "swir16", "swir22", "scl")}
    return {"type": "Feature", "id": name,
            "collection": collection or "sentinel-2-c1-l2a",
            "bbox": bbox or [81.0, 23.0, 83.0, 25.0],
            "geometry": {"type": "Polygon", "coordinates": [[[81, 23], [83, 23],
                          [83, 25], [81, 25], [81, 23]]]},
            "properties": {"datetime": when, "eo:cloud_cover": 5, "s2:mgrs_tile": tile,
                           "earthsearch:boffset_applied": True}, "assets": assets}


def scene(nir=0.6, swir16=0.2, swir22=0.1, scl=4, shape=(4, 4)):
    return {key: np.full(shape, value, dtype=np.float32)
            for key, value in dict(nir=nir, swir16=swir16, swir22=swir22, scl=scl).items()}


class SentinelTests(unittest.TestCase):
    def test_same_size_refreshed_products_replace_old_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'cache'
            source.mkdir()
            (source / 'pre.tif').write_bytes(b'old')
            row = {'sentinel_status': 'analysed'}
            se._publish_products(source, root / 'out', 'key', row)
            (source / 'pre.tif').write_bytes(b'new')
            se._publish_products(source, root / 'out', 'key', row)
            self.assertEqual(Path(row['sentinel_pre_crop']).read_bytes(), b'new')

    def test_fallback_products_use_the_successful_pairs_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = item('pre1', '2025-04-09T00:00:00Z', self.make_assets(root, 'pre1', 6000, 2000, 1000))
            fallback = item('pre2', '2025-04-08T00:00:00Z', self.make_assets(root, 'pre2', 4500, 2000, 1500))
            post = item('post', '2025-04-12T00:00:00Z', self.make_assets(root, 'post', 3000, 2500, 3000))
            save = se._save_products
            calls = []
            def fail_first(*args):
                calls.append(1)
                if len(calls) == 1:
                    raise OSError('simulated first-pair write failure')
                return save(*args)
            with patch.object(se, '_request_json', return_value={'features': [first, fallback, post], 'links': []}), patch.object(se, '_save_products', side_effect=fail_first):
                rows, errors = se.acquire_sentinel(pd.DataFrame([event()]), config(), root / 'cache', root / 'out')
            row = rows.iloc[0]
            self.assertEqual(row['sentinel_status'], 'analysed')
            self.assertEqual(row['sentinel_pre_item'], 'pre2')
            self.assertAlmostEqual(row['sentinel_dnbr_mean'], 0.5, places=5)
            self.assertTrue(any('simulated first-pair' in error for error in errors))
            with rasterio.open(row['sentinel_pre_crop']) as crop:
                self.assertEqual(crop.tags()['scene_id'], 'pre2')
                self.assertAlmostEqual(float(np.nanmean(crop.read(1))), 0.45, places=5)

    def test_failed_product_writes_cannot_return_analysed_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pre = item('pre', '2025-04-09T00:00:00Z', self.make_assets(root, 'pre', 6000, 2000, 1000))
            post = item('post', '2025-04-12T00:00:00Z', self.make_assets(root, 'post', 3000, 2500, 3000))
            with patch.object(se, '_request_json', return_value={'features': [pre, post], 'links': []}), patch.object(se, '_save_products', side_effect=OSError('disk unavailable')):
                rows, errors = se.acquire_sentinel(pd.DataFrame([event()]), config(), root / 'cache', root / 'out')
            self.assertEqual(rows.iloc[0]['sentinel_status'], 'download_failed')
            self.assertTrue(pd.isna(rows.iloc[0]['sentinel_preview']))
            self.assertTrue(errors)

    def test_parallel_catalogue_writers_preserve_one_complete_json(self):
        barrier = threading.Barrier(8)
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / 'shared.json'
            def write(index):
                barrier.wait(timeout=10)
                se._write_json(destination, {'index': index, 'payload': str(index) * 200000})
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(write, range(8)))
            saved = json.loads(destination.read_text())
            self.assertIn(saved['index'], range(8))
            self.assertEqual(saved['payload'], str(saved['index']) * 200000)

    def test_public_aoi_queries_hide_event_precision_and_filter_dates_locally(self):
        settings = config()
        settings['aoi'] = {'bbox': [81.5, 23.2, 83.5, 25.2]}
        payloads = []
        def search(payload, *args):
            payloads.append(payload)
            return [item('pre', '2025-04-09T00:00:00Z'),
                    item('post', '2025-04-11T00:00:00Z'),
                    item('too-early', '2025-03-01T00:00:00Z')], False
        with tempfile.TemporaryDirectory() as folder, patch.object(se, '_search', side_effect=search):
            result = se.sentinel_query(event(), settings, Path(folder), False)
        self.assertEqual(result['pre']['id'], 'pre')
        self.assertEqual(result['post']['id'], 'post')
        for payload in payloads:
            self.assertNotIn('intersects', payload)
            self.assertEqual(payload['bbox'], [81.5, 23.2, 83.5, 25.2])
            for bound in payload['datetime'].split('/'):
                stamp = pd.Timestamp(bound)
                self.assertEqual((stamp.day, stamp.hour, stamp.minute, stamp.second), (1, 0, 0, 0))

    def setUp(self):
        self.assertIsNotNone(se, "The Sentinel evidence implementation is missing")

    def test_radiometry_masks_raw_nodata_before_offset(self):
        asset = {"raster:bands": [{"scale": 0.0001, "offset": -0.1, "nodata": 0}]}
        actual = se.scale_reflectance(np.array([0, 1000, 3000]), asset)
        self.assertTrue(np.isnan(actual[0]))
        np.testing.assert_allclose(actual[1:], [0.0, 0.2], atol=1e-7)

    def test_asset_offset_is_applied_once_even_with_boffset_flag(self):
        props = {"earthsearch:boffset_applied": True}
        for offset, expected in [(0, 0.3), (-0.1, 0.2)]:
            asset = {"raster:bands": [{"scale": 0.0001, "offset": offset, "nodata": 0}]}
            self.assertAlmostEqual(float(se.scale_reflectance(np.array([3000]), asset, props)[0]),
                                   expected, places=6)

    def test_missing_radiometric_metadata_is_not_guessed(self):
        with self.assertRaises(ValueError):
            se.scale_reflectance(np.array([3000]), {})

    def test_dnbr_is_pre_minus_post_and_cloud_pixels_never_contribute(self):
        pre, post = scene(), scene(nir=0.3, swir16=0.25, swir22=0.3)
        pre["scl"][0, 0] = 9
        pre["swir22"][0, 0] = 30
        metrics = se.compute_evidence(pre, post, np.ones((4, 4), bool), config()["sentinel"])
        self.assertAlmostEqual(metrics["sentinel_dnbr_mean"], 5 / 7, places=6)
        self.assertAlmostEqual(metrics["sentinel_joint_valid_fraction"], 15 / 16)
        self.assertEqual(metrics["sentinel_evidence"], "observed_burn_like_change")
        self.assertEqual(metrics["sentinel_affected_fraction"], 1.0)

    def test_quality_fraction_uses_event_circle_not_whole_crop(self):
        pre, post = scene(scl=9), scene(scl=9)
        pre["scl"][:2, :2] = post["scl"][:2, :2] = 4
        roi = np.zeros((4, 4), bool)
        roi[:2, :2] = True
        metrics = se.compute_evidence(pre, post, roi, config()["sentinel"])
        self.assertEqual(metrics["sentinel_joint_valid_fraction"], 1.0)
        self.assertEqual(metrics["sentinel_pre_cloud_fraction"], 0.0)
        self.assertEqual(metrics["sentinel_evidence"], "no_clear_change")

    def test_all_obscured_has_no_fabricated_change_value(self):
        metrics = se.compute_evidence(scene(scl=9), scene(), np.ones((4, 4), bool),
                                      config()["sentinel"])
        self.assertEqual(metrics["sentinel_evidence"], "inconclusive")
        self.assertEqual(metrics["sentinel_joint_valid_fraction"], 0.0)
        self.assertIsNone(metrics["sentinel_dnbr_mean"])

    def test_nodata_zero_and_saturation_are_not_clear_observations(self):
        pre, post = scene(), scene()
        pre["nir"][0, 0] = 0
        pre["scl"][0, 1] = 1
        post["scl"][0, 2] = 3
        metrics = se.compute_evidence(pre, post, np.ones((4, 4), bool), config()["sentinel"])
        self.assertAlmostEqual(metrics["sentinel_joint_valid_fraction"], 13 / 16)
        self.assertAlmostEqual(metrics["sentinel_pre_saturated_fraction"], 1 / 16)

    def test_reprojection_corrects_shift_and_keeps_scl_categorical(self):
        source = np.array([[1, 3], [1, 3]], dtype=np.float32)
        grid = {"transform": from_origin(10, 40, 20, 20), "crs": "EPSG:32644",
                "shape": (2, 1)}
        continuous = se.reproject_array(source, from_origin(0, 40, 20, 20),
                                         "EPSG:32644", grid)
        category = se.reproject_array(source, from_origin(0, 40, 20, 20),
                                       "EPSG:32644", grid, categorical=True)
        np.testing.assert_allclose(continuous, [[2], [2]])
        np.testing.assert_array_equal(category, [[3], [3]])

    def test_cache_identity_ignores_event_number_but_not_science_settings(self):
        key = se.evidence_cache_key(event(), config())
        self.assertEqual(key, se.evidence_cache_key(event("RENUMBERED"), config(workers=3)))
        self.assertNotEqual(key, se.evidence_cache_key(event(longitude=82.01), config()))
        self.assertNotEqual(key, se.evidence_cache_key(event(), config(analysis_radius_m=80)))

    def test_disabled_and_cap_excluded_events_have_explicit_rows(self):
        events = pd.DataFrame([event(), event("E2")])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            disabled, errors = se.acquire_sentinel(events, config(enabled=False), root, root, False)
            queued, _ = se.acquire_sentinel(events, config(max_events=0), root, root, False)
        self.assertEqual(disabled["sentinel_status"].tolist(), ["disabled", "disabled"])
        self.assertEqual(queued["sentinel_status"].tolist(), ["queued", "queued"])
        self.assertTrue(queued["sentinel_reason"].str.contains("cap", case=False).all())
        self.assertTrue(disabled["sentinel_reason"].str.len().gt(0).all())
        self.assertEqual(errors, [])

    def test_industrial_highs_precede_high_frp_vegetation(self):
        events = pd.DataFrame([event("VEG", context_label="vegetation_associated", frp_peak_mw=10000),
                               event("IND1"), event("IND2", anomaly_score=0.96)])
        selected = se.select_candidates(events, config(max_events=2))
        self.assertEqual(set(selected), {"IND1", "IND2"})

    def test_vegetation_sampling_spans_years_and_sites(self):
        events = pd.DataFrame([event("A", context_label="vegetation_associated", site_id="A"),
                               event("B", context_label="vegetation_associated", site_id="A",
                                     anomaly_score=0.98),
                               event("C", context_label="vegetation_associated", site_id="C",
                                     start_time="2024-04-10", end_time="2024-04-11",
                                     anomaly_score=0.97)])
        selected = se.select_candidates(events, config(max_events=2))
        self.assertEqual(set(selected), {"A", "C"})

    def test_selected_event_ids_can_request_a_nonpriority_event(self):
        events = pd.DataFrame([event("HIGH"), event("LOW", anomaly_score=0.1)])
        selected = se.select_candidates(events, config(selected_event_ids=["LOW"]))
        self.assertEqual(set(selected), {"LOW"})

    def test_processing_cap_reserves_vegetation_and_latest_events(self):
        rows = [event(f"I{i}", start_time="2019-04-10", end_time="2019-04-11") for i in range(10)]
        rows += [event("VEG", context_label="vegetation_associated", anomaly_score=0.1,
                       start_time="2020-04-10", end_time="2020-04-11"),
                 event("RECENT", anomaly_score=0.1, start_time="2025-05-18", end_time="2025-05-19")]
        selected = se.select_candidates(pd.DataFrame(rows), config(max_events=5))
        self.assertEqual(len(selected), 5)
        self.assertIn("VEG", selected)
        self.assertIn("RECENT", selected)

    def test_vegetation_uses_year_strata_before_extra_high_scoring_sites(self):
        rows = [event(f"NEW{i}", context_label="vegetation_associated", site_id=f"NEW{i}") for i in range(8)]
        rows += [event("OLD", context_label="vegetation_associated", anomaly_score=0.1,
                       start_time="2019-04-10", end_time="2019-04-11", site_id="OLD")]
        selected = se.select_candidates(pd.DataFrame(rows), config(max_events=2))
        self.assertIn("OLD", selected)

    def test_catalogue_excludes_future_noncovering_and_cross_tile_pairs(self):
        rows = [item("pre", "2025-04-08T00:00:00Z"),
                item("post", "2025-04-12T00:00:00Z"),
                item("future", "2025-04-20T00:00:00Z"),
                item("outside", "2025-04-11T00:00:00Z", bbox=[0, 0, 1, 1]),
                item("other-tile", "2025-04-11T00:00:00Z", tile="45ABC")]
        as_of = pd.Timestamp("2025-04-15T00:00:00Z")

        def catalogue(method, url, body, settings):
            self.assertLessEqual(pd.Timestamp(body["datetime"].split("/")[1]), as_of)
            return {"features": rows, "links": []}

        with tempfile.TemporaryDirectory() as folder, patch.object(se, "_request_json", catalogue):
            result = se.sentinel_query(event(), config(as_of=as_of.isoformat()), Path(folder), False)
        self.assertTrue(result["pairs"])
        self.assertEqual({(p["pre"]["id"], p["post"]["id"]) for p in result["pairs"]},
                         {("pre", "post")})

    def test_catalogue_follows_bounded_pagination(self):
        def catalogue(method, url, body, settings):
            before = pd.Timestamp(body["datetime"].split("/")[1]) <= pd.Timestamp(event()["start_time"])
            if "token" not in body:
                return {"features": [], "links": [{"rel": "next", "href": se.EARTH_SEARCH,
                         "method": "POST", "body": {**body, "token": "page2"}}]}
            return {"features": [item("pre", "2025-04-08T00:00:00Z") if before
                                  else item("post", "2025-04-12T00:00:00Z")], "links": []}

        with tempfile.TemporaryDirectory() as folder, patch.object(se, "_request_json", catalogue):
            result = se.sentinel_query(event(), config(max_catalog_pages=2), Path(folder), False)
        self.assertEqual(result["pairs"][0]["pre"]["id"], "pre")
        self.assertFalse(result["truncated"])

    def test_pair_requires_the_same_nir_band_on_both_dates(self):
        pre = item("pre", "2025-04-08T00:00:00Z")
        post = item("post", "2025-04-12T00:00:00Z")
        post["assets"]["nir"] = post["assets"].pop("nir08")
        with tempfile.TemporaryDirectory() as folder, patch.object(se, "_request_json",
                return_value={"features": [pre, post], "links": []}):
            result = se.sentinel_query(event(), config(), Path(folder), False)
        self.assertEqual(result["pairs"], [])

    def test_recent_missing_post_is_waiting_not_observation_absence(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(se, "_request_json",
                                                                  return_value={"features": [], "links": []}):
            recent = se.sentinel_query(event(), config(as_of="2025-04-11T00:00:00Z"), Path(folder), False)
            historical = se.sentinel_query(event(), config(), Path(folder), False)
        self.assertEqual(recent["status"], "waiting_for_post_image")
        self.assertEqual(historical["status"], "no_catalogue_match")

    def make_assets(self, root, name, nir, b11, b12, scl=4):
        grid = se.make_grid(82, 24, 300)
        result = {}
        for key, value in dict(nir08=nir, swir16=b11, swir22=b12, scl=scl).items():
            path = root / f"{name}_{key}.tif"
            with rasterio.open(path, "w", driver="GTiff", width=grid["shape"][1],
                               height=grid["shape"][0], count=1, dtype="uint16",
                               crs=grid["crs"], transform=grid["transform"], nodata=0) as dst:
                dst.write(np.full(grid["shape"], value, dtype=np.uint16), 1)
            result[key] = {"href": str(path), "raster:bands": [{"scale": 1 if key == "scl" else 0.0001,
                            "offset": 0, "nodata": 0}]}
        return result

    def test_real_crops_preview_and_stable_cache_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pre = item("pre", "2025-04-08T00:00:00Z", self.make_assets(root, "pre", 6000, 2000, 1000))
            post = item("post", "2025-04-12T00:00:00Z", self.make_assets(root, "post", 3000, 2500, 3000))
            with patch.object(se, "_request_json", return_value={"features": [pre, post], "links": []}):
                first, errors = se.acquire_sentinel(pd.DataFrame([event()]), config(), root / "cache", root / "out", False)
            self.assertEqual(errors, [])
            self.assertEqual(first.iloc[0]["sentinel_status"], "analysed")
            self.assertTrue(Path(first.iloc[0]["sentinel_preview"]).is_file())
            with rasterio.open(first.iloc[0]["sentinel_pre_crop"]) as a, rasterio.open(first.iloc[0]["sentinel_post_crop"]) as b:
                self.assertEqual(a.transform, b.transform)
                self.assertEqual(a.crs, b.crs)
                self.assertEqual(a.count, 4)
            with patch.object(se, "_request_json", side_effect=AssertionError("cache must avoid network")):
                second, errors = se.acquire_sentinel(pd.DataFrame([event("NEW-ID")]), config(), root / "cache", root / "out", False)
            self.assertEqual(errors, [])
            self.assertTrue(second.iloc[0]["sentinel_cache_hit"])
            self.assertEqual(second.iloc[0]["event_id"], "NEW-ID")
            self.assertEqual(second.iloc[0]["sentinel_preview"], first.iloc[0]["sentinel_preview"])
            self.assertEqual(second.iloc[0]["sentinel_adjustment"], 0)

    def test_locally_cloudy_nearest_scene_tries_an_alternative(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cloudy = item("cloudy", "2025-04-09T00:00:00Z", self.make_assets(root, "cloudy", 6000, 2000, 1000, scl=9))
            clear = item("clear", "2025-04-07T00:00:00Z", self.make_assets(root, "clear", 6000, 2000, 1000))
            post = item("post", "2025-04-12T00:00:00Z", self.make_assets(root, "post", 3000, 2500, 3000))
            with patch.object(se, "_request_json", return_value={"features": [cloudy, clear, post], "links": []}):
                rows, errors = se.acquire_sentinel(pd.DataFrame([event()]), config(), root / "cache", root / "out", False)
        self.assertEqual(errors, [])
        self.assertEqual(rows.iloc[0]["sentinel_status"], "analysed")
        self.assertEqual(rows.iloc[0]["sentinel_pre_item"], "clear")
        self.assertGreaterEqual(rows.iloc[0]["sentinel_pairs_attempted"], 2)

    def test_legacy_offset_uncertainty_preserves_coverage_but_withholds_change_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pre = item("pre", "2025-04-08T00:00:00Z", self.make_assets(root, "pre", 6000, 2000, 1000),
                       collection="sentinel-2-l2a")
            post = item("post", "2025-04-12T00:00:00Z", self.make_assets(root, "post", 3000, 2500, 3000),
                        collection="sentinel-2-l2a")
            with patch.object(se, "_request_json", return_value={"features": [pre, post], "links": []}):
                rows, errors = se.acquire_sentinel(pd.DataFrame([event()]), config(collections=["sentinel-2-l2a"]),
                                                   root / "cache", root / "out", False)
            self.assertEqual(errors, [])
            self.assertEqual(rows.iloc[0]["sentinel_status"], "analysed")
            self.assertEqual(rows.iloc[0]["sentinel_evidence"], "inconclusive")
            self.assertEqual(rows.iloc[0]["sentinel_radiometry_status"], "legacy_offset_unverified")
            self.assertTrue(pd.isna(rows.iloc[0]["sentinel_dnbr_mean"]))
            self.assertTrue(Path(rows.iloc[0]["sentinel_preview"]).is_file())

    def test_download_failure_keeps_event_with_reason_and_error(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(se, "_request_json", side_effect=OSError("network unavailable")):
            rows, errors = se.acquire_sentinel(pd.DataFrame([event()]), config(), Path(folder), Path(folder), False)
        self.assertEqual(rows.iloc[0]["sentinel_status"], "download_failed")
        self.assertIn("network", rows.iloc[0]["sentinel_reason"].lower())
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
