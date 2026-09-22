import hashlib
import json
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from backend import data_sources


class DownloadTests(unittest.TestCase):
    def test_official_nrt_schema_without_instrument_is_ingested(self):
        content = (
            "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight\n"
            "24,82,340,0.4,0.4,2024-01-01,0100,N20,nominal,2.0NRT,300,8,N\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nrt.csv").write_text(content, encoding="utf-8")
            config = {"aoi": {"bbox": [81, 23, 83, 25]},
                      "inputs": {"firms_globs": ["*.csv"], "expected_years": [2024],
                                 "accepted_sensors": ["VIIRS_NOAA20"]}}
            frame, audit = data_sources.ingest_firms(root, config)
            self.assertEqual(len(frame), 1)
            self.assertEqual(frame.iloc[0]["sensor"], "VIIRS_NOAA20")
            self.assertEqual(frame.iloc[0]["data_quality"], "near_real_time")
            self.assertAlmostEqual(frame.iloc[0]["confidence_score"], 0.6)
            self.assertEqual(audit["quality_counts"], {"near_real_time": 1})

    def test_complete_saved_response_recovery_preserves_source_bytes(self):
        content = b"latitude,longitude,acq_date,acq_time,satellite\n24,82,2024-01-01,0100,N\n"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source.csv"
            target.with_suffix(".csv.part").write_bytes(content)
            target.with_suffix(".partial.meta.json").write_text(json.dumps({
                "url": "https://example.invalid/no-network",
                "validator": '"' + hashlib.md5(content).hexdigest() + '"',
            }), encoding="utf-8")
            metadata = data_sources.recover_complete_csv(target, "near_real_time")
            self.assertEqual(target.read_bytes(), content)
            self.assertEqual(metadata["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(metadata["status"], "recovered_verified_response")

    def test_recovery_rejects_mismatched_or_opaque_validator(self):
        content = b"latitude,longitude,acq_date,acq_time,satellite\n24,82,2024-01-01,0100,N\n"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source.csv"
            partial = target.with_suffix(".csv.part")
            partial.write_bytes(content)
            for validator in ('"00000000000000000000000000000000"', '"opaque-etag"'):
                target.with_suffix(".partial.meta.json").write_text(json.dumps({
                    "url": "https://example.invalid/no-network", "validator": validator,
                }), encoding="utf-8")
                with self.assertRaises(ValueError):
                    data_sources.recover_complete_csv(target, "near_real_time")
                self.assertFalse(target.exists())
                self.assertEqual(partial.read_bytes(), content)

    def test_interrupted_transfer_resumes_and_preserves_exact_source_bytes(self):
        content = (b"latitude,longitude,acq_date,acq_time,satellite,instrument\n"
                   + b"24,82,2024-01-01,0100,N,VIIRS\n" * 12000)

        class Handler(BaseHTTPRequestHandler):
            first = True
            def log_message(self, *args):
                pass
            def do_GET(self):
                offset = int(self.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
                self.send_response(206 if offset else 200)
                self.send_header("ETag", '"unchanged-source"')
                self.send_header("Content-Length", str(len(content) - offset))
                if offset:
                    self.send_header("Content-Range", f"bytes {offset}-{len(content)-1}/{len(content)}")
                self.end_headers()
                if Handler.first:
                    Handler.first = False
                    self.wfile.write(content[:140000])
                    self.wfile.flush()
                    self.close_connection = True
                else:
                    self.wfile.write(content[offset:])

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "source.csv"
                data_sources.download_csv(f"http://127.0.0.1:{server.server_port}/firms.csv",
                                          target, "science_quality")
                self.assertEqual(target.read_bytes(), content)
                self.assertTrue(target.with_suffix(".meta.json").is_file())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_bad_cached_header_is_not_accepted_as_data(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "wrong.csv"
            target.write_text("<html>not a dataset</html>", encoding="utf-8")
            with self.assertRaises(ValueError):
                data_sources.download_csv("https://example.invalid/not-used", target, "science_quality")


if __name__ == "__main__":
    unittest.main()
