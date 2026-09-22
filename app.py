"""Localhost launcher. Saved-result viewing requires no third-party packages."""
from __future__ import annotations

import argparse
import functools
import json
import sys
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import release

ROOT = Path(__file__).resolve().parent


class SiteHandler(SimpleHTTPRequestHandler):
    def list_directory(self, path):
        self.send_error(403, "Directory listing is disabled")
        return None

    def end_headers(self):
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        relative = unquote(urlsplit(self.path).path).lstrip("/")
        try:
            path = release.local_path(self.directory, relative)
        except ValueError:
            self.send_error(403, "Path outside site")
            return None
        if path.is_dir():
            for name in ("index.html", "index.htm"):
                candidate = path / name
                if candidate.exists() and not candidate.resolve().is_relative_to(Path(self.directory).resolve()):
                    self.send_error(403, "Index outside site")
                    return None
        accepts_gzip = False
        for part in self.headers.get("Accept-Encoding", "").split(","):
            encoding, *parameters = part.strip().split(";")
            if encoding != "gzip":
                continue
            try:
                quality = next((float(p.strip()[2:]) for p in parameters if p.strip().startswith("q=")), 1)
                accepts_gzip = 0 < quality <= 1
            except ValueError:
                pass
        compressed = Path(str(path) + ".gz")
        if accepts_gzip and path.is_file() and compressed.is_file():
            if not compressed.resolve().is_relative_to(Path(self.directory).resolve()):
                self.send_error(403, "Path outside site")
                return None
            handle = compressed.open("rb")
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(str(path)))
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(compressed.stat().st_size))
            self.end_headers()
            return handle
        return super().send_head()


def create_server(site, host="127.0.0.1", port=8000):
    return ThreadingHTTPServer((host, port), functools.partial(SiteHandler, directory=str(Path(site).resolve())))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("serve", "prepare", "check", "rebuild", "package"):
        child = commands.add_parser(command)
        child.add_argument("--config", default="config.json", help="Project-relative configuration path")
        if command == "serve":
            child.add_argument("--host", default="127.0.0.1")
            child.add_argument("--port", default=8000, type=int)
            child.add_argument("--open", action="store_true", help="Open the local URL in your browser")
        elif command == "check":
            child.add_argument("--mode", choices=("view", "rebuild"), default="view")
            child.add_argument("--deep", action="store_true", help="Verify SHA-256 and result consistency")
        elif command == "rebuild":
            child.add_argument("--publish", action="store_true", help="Replace outputs only after validation")
        elif command == "package":
            child.add_argument("--kind", choices=("source", "data", "all"), default="all")
    args = parser.parse_args(argv)
    try:
        config = release.settings(ROOT, args.config)
        output = release.local_path(ROOT, config["paths"]["output"])
        if args.command == "prepare":
            print(f"Prepared {release.prepare(ROOT, config)}")
        elif args.command == "serve":
            site = output / "site"
            if not (site / "index.html").is_file() or not (site / "data/workspace.json").is_file():
                raise ValueError("Prepared site missing. Extract the data package, or run: python app.py prepare")
            with create_server(site, args.host, args.port) as server:
                url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{server.server_port}"
                print(f"Thermal Sentinel: {url}\nSaved results only; no automatic data downloads.\nPress Ctrl+C to stop.", flush=True)
                if args.open:
                    webbrowser.open(url)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
        elif args.command == "check":
            errors = []
            for kind in ("source", "data"):
                manifest = ROOT / f"MANIFEST-{kind}.json"
                if manifest.is_file():
                    errors += release.check_manifest(ROOT, release.read_json(manifest), deep=args.deep)
                else:
                    print(f"Note: no MANIFEST-{kind}.json (working tree, not an extracted package).")
            for name in ("site/index.html", "site/data/workspace.json", "site/viewer.js",
                         "site/style.css", "site/vendor/leaflet.js"):
                if not (output / name).is_file():
                    errors.append(f"Missing {output / name}")
            if args.deep:
                errors += release.validate_results(output)
            if args.mode == "rebuild":
                print(json.dumps(release.preflight(ROOT, config), indent=2))
            manifest = output / "run_manifest.json"
            if manifest.exists():
                audit = release.read_json(manifest).get("input_audit", {})
                print("Missing requested archive years:", audit.get("missing_expected_years", "unknown"))
            if errors:
                raise ValueError("\n".join(errors[:30]))
            print("Checks passed." + (" SHA-256 verified where manifests are supplied." if args.deep else " Use --deep for checksums."))
        elif args.command == "rebuild":
            print(json.dumps(release.rebuild(ROOT, config, args.publish), indent=2))
        elif args.command == "package":
            if args.kind in ("data", "all") and not (output / "site/index.html").exists():
                raise ValueError("Run python app.py prepare before packaging data.")
            for kind in (("source", "data") if args.kind == "all" else (args.kind,)):
                destination = ROOT / "dist" / f"heat-waves-{kind}.zip"
                manifest = release.write_package(ROOT, release.package_files(ROOT, config, kind), destination, kind)
                print(f"{destination}: {len(manifest['files'])} files, {destination.stat().st_size / 1048576:.1f} MiB", flush=True)
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, ImportError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
