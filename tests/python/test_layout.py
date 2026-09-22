"""The organized source archive must deploy without the development checkout."""
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend import release

ROOT = Path(__file__).resolve().parents[2]


class LayoutTests(unittest.TestCase):
    def test_packaged_layout_prepares_a_site_without_scientific_dependencies(self):
        files = release.package_files(ROOT, {}, "source")
        names = {p.relative_to(ROOT).as_posix() for p in files}
        self.assertTrue({
            "backend/__init__.py", "backend/release.py", "backend/viewer.py",
            "frontend/index.html", "frontend/vendor/leaflet.js",
            "tests/python/test_web_viewer.py", "tests/javascript/test_web_viewer.js",
            "scripts/make_ppt_figures.py", "docs/DATA_SOURCES.md", ".gitignore",
        }.issubset(names), "The archive must include the organized application's dependencies")
        self.assertFalse(any("/__pycache__/" in name or name.endswith(".pyc") for name in names))
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            archive_path = folder / "source.zip"
            release.write_package(ROOT, files, archive_path, "source")
            installed = folder / "installed"
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(installed)
            output = installed / "outputs"
            output.mkdir()
            empty = {"type": "FeatureCollection", "features": []}
            for name in ("events.geojson", "facilities.geojson"):
                (output / name).write_text(json.dumps(empty), encoding="utf-8")
            (output / "run_manifest.json").write_text("{}", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-S", str(installed / "app.py"), "prepare"],
                cwd=folder, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            site = output / "site"
            self.assertTrue((site / "vendor/leaflet.js").is_file())
            payload = json.loads((site / "data/workspace.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["events"]["features"], [])

    def test_analysis_entrypoints_can_run_as_package_modules(self):
        for module in ("backend.pipeline", "backend.data_sources", "scripts.make_ppt_figures"):
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-m", module, "--help"],
                    cwd=ROOT, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
