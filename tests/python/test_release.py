"""Deployment boundary tests: fail closed, keep reference results intact."""
import csv
import importlib.util
import json
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.release"), "release module is required")
        from backend import release
        self.release = release
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_manifest_detects_changed_and_missing_bytes(self):
        file = self.root / "data.csv"
        file.write_bytes(b"abc")
        manifest = self.release.make_manifest(self.root, [file], "data")
        self.assertEqual(self.release.check_manifest(self.root, manifest, deep=True), [])
        file.write_bytes(b"bad")
        self.assertTrue(any("checksum" in e for e in self.release.check_manifest(self.root, manifest, deep=True)))
        file.unlink()
        self.assertTrue(any("Missing" in e for e in self.release.check_manifest(self.root, manifest)))

    def test_manifest_rejects_outside_root_and_absolute_names(self):
        for name in ("../private.txt", "/private.txt", "C:/private.txt", "..\\private.txt"):
            with self.subTest(name=name):
                errors = self.release.check_manifest(self.root, {
                    "schema_version": 1, "files": [{"path": name, "bytes": 1, "sha256": "a" * 64}]
                })
                self.assertTrue(errors)

    def test_network_guard_blocks_and_restores_sockets(self):
        connect = socket.socket.connect
        with self.release.no_network():
            with socket.socket() as client:
                with self.assertRaisesRegex(RuntimeError, "disabled"):
                    client.connect(("127.0.0.1", 9))
        self.assertIs(socket.socket.connect, connect)

    def test_publish_failure_keeps_previous_outputs(self):
        stage, output, backup = (self.root / name for name in ("stage", "outputs", "backup"))
        stage.mkdir()
        output.mkdir()
        (stage / "events.csv").write_text("new")
        (output / "events.csv").write_text("reference")
        rename = Path.rename

        def fail_stage(path, destination):
            if path == stage:
                raise OSError("simulated publication failure")
            return rename(path, destination)

        with patch.object(Path, "rename", fail_stage):
            with self.assertRaises(OSError):
                self.release.publish(stage, output, backup)
        self.assertEqual((output / "events.csv").read_text(), "reference")

    def test_zip_contains_only_selected_relative_files_and_manifest(self):
        file = self.root / "data" / "sample.csv"
        file.parent.mkdir()
        file.write_bytes(b"abc")
        archive = self.root / "sample.zip"
        self.release.write_package(self.root, [file], archive, "data")
        with zipfile.ZipFile(archive) as package:
            self.assertEqual(set(package.namelist()), {"data/sample.csv", "MANIFEST-data.json"})
            manifest = json.loads(package.read("MANIFEST-data.json"))
            self.assertEqual(manifest["files"][0]["path"], "data/sample.csv")

    def test_source_selection_includes_browser_test_dependencies(self):
        for name in ("tests/python/test_web_viewer.py", "tests/javascript/test_web_viewer.js"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        selected = {p.name for p in self.release.package_files(self.root, {}, "source")}
        self.assertIn("test_web_viewer.js", selected)

    def test_output_validation_rejects_bad_score_and_duplicate_ids(self):
        output = self.root / "outputs"
        output.mkdir()
        self.release.dump_json(output / "run_manifest.json", {
            "outputs": {"events": 2, "persistent_sites": 0, "detections": 0},
            "external_data": {"worldcover_errors": []}
        })
        with (output / "events.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["event_id", "anomaly_score"])
            writer.writeheader()
            writer.writerows([{"event_id": "E1", "anomaly_score": "1.2"}] * 2)
        errors = self.release.validate_results(output)
        self.assertTrue(any("score" in e.lower() for e in errors))
        self.assertTrue(any("duplicate" in e.lower() for e in errors))

    def test_relocated_preview_uses_only_the_local_sentinel_copy(self):
        sentinel = self.root / "sentinel"
        sentinel.mkdir()
        (sentinel / "example.png").write_bytes(b"png")
        features = [{"properties": {"sentinel_preview": "C:/old/project/outputs/sentinel/example.png"}}]
        self.release.portable_previews(features, self.root)
        self.assertEqual(features[0]["properties"]["sentinel_preview"], "sentinel/example.png")
        outside = [{"properties": {"sentinel_preview": "C:/old/sentinel/../secret.png"}}]
        self.release.portable_previews(outside, self.root)
        self.assertEqual(outside[0]["properties"]["sentinel_preview"], "C:/old/sentinel/../secret.png")

    def test_missing_or_wrong_aoi_context_fails_preflight_without_download(self):
        config = json.loads((Path(__file__).resolve().parents[2] / "config.json").read_text())
        config["inputs"]["firms_globs"] = ["data/sample.csv"]
        source = self.root / "data/sample.csv"
        source.parent.mkdir()
        source.write_text("latitude,longitude,acq_date,acq_time,satellite\n24,82,2020-01-01,0000,N\n")
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            self.release.preflight(self.root, config)

    def test_preflight_rejects_georeferencing_from_a_different_tile(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin
        config = json.loads((Path(__file__).resolve().parents[2] / "config.json").read_text())
        config["aoi"]["bbox"] = [81.5, 23.2, 82.0, 23.8]
        config["inputs"]["firms_globs"] = ["data/sample.csv"]
        source = self.root / "data/sample.csv"
        source.parent.mkdir()
        source.write_text("latitude,longitude,acq_date,acq_time,satellite\n")
        # Use the real matching OSM cache, but a wrongly located, correctly named TIFF.
        cache = self.root / "data/cache"
        cache.mkdir()
        from backend import pipeline
        class Response:
            def raise_for_status(self): pass
            def json(self):
                return {"elements": [{"type": "node", "id": 1, "lat": 23.5, "lon": 81.6,
                                      "tags": {"power": "plant"}}]}
        with patch.object(pipeline.requests, "get", return_value=Response()):
            pipeline.fetch_osm_facilities(config, cache, refresh=True)
        world = cache / "worldcover"
        world.mkdir()
        with rasterio.open(world / "ESA_WorldCover_10m_2021_v200_N21E081_Map.tif", "w",
                           driver="GTiff", width=1100, height=1100, count=1, dtype="uint8",
                           crs="EPSG:4326", transform=from_origin(0, 3, 3/1100, 3/1100)) as dataset:
            dataset.write(np.zeros((1, 1100, 1100), dtype="uint8"))
        with self.assertRaisesRegex(ValueError, "georeferencing"):
            self.release.preflight(self.root, config)


if __name__ == "__main__":
    unittest.main()
