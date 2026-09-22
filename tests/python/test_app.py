import gzip
import importlib.util
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


class AppTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("app"), "app module is required")

    def test_help_runs_without_site_packages_from_other_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, "-S", str(Path(__file__).resolve().parents[2] / "app.py"), "--help"],
                                    cwd=temp, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("serve", result.stdout)

    def test_http_serves_gzip_but_not_private_files_or_directories(self):
        import app
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            site = root / "site"
            site.mkdir()
            (root / "secret.txt").write_text("private")
            (site / "index.html").write_text("dashboard")
            (site / "data.json").write_bytes(b'{"events":1}')
            (site / "data.json.gz").write_bytes(gzip.compress(b'{"events":1}'))
            (site / "empty").mkdir()
            server = app.create_server(site, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            try:
                request = urllib.request.Request(url + "/data.json", headers={"Accept-Encoding": "gzip"})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.headers["Content-Encoding"], "gzip")
                    self.assertEqual(gzip.decompress(response.read()), b'{"events":1}')
                    self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
                for encoding in ("gzip;q=0", "gzip;q=invalid"):
                    request = urllib.request.Request(url + "/data.json", headers={"Accept-Encoding": encoding})
                    with urllib.request.urlopen(request) as response:
                        self.assertIsNone(response.headers["Content-Encoding"])
                        self.assertEqual(response.read(), b'{"events":1}')
                for path in ("/../secret.txt", "/empty/", "/%2e%2e/secret.txt"):
                    with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError):
                        urllib.request.urlopen(url + path)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_directory_index_cannot_resolve_outside_site(self):
        import app
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            site = root / "site"
            site.mkdir()
            private = root / "secret.html"
            private.write_text("private")
            try:
                (site / "index.html").symlink_to(private)
            except OSError:
                self.skipTest("File symlinks are unavailable on this platform")
            server = app.create_server(site, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
