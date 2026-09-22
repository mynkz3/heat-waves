"""Portable packages and explicit local rebuilds. Viewing uses only the stdlib."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import socket
import zipfile
from collections import Counter
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

RESULT_FILES = (
    "events.csv", "events.geojson", "sites.csv", "sites.geojson",
    "detections.csv", "detections.geojson", "facilities.csv", "facilities.geojson",
    "anomaly_rankings.csv", "run_manifest.json", "data_audit.json",
    "sentinel_summary.json", "vegetation_scoring.json", "tuning_report.json",
)


def dump_json(path, payload):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def local_path(root, value):
    """Reject portable paths that escape the distribution, including symlinks."""
    name = str(value).replace("\\", "/")
    if Path(name).is_absolute() or PureWindowsPath(name).drive or ".." in name.split("/"):
        raise ValueError(f"Path must be relative and inside the project: {value}")
    target = (Path(root) / name).resolve()
    if not target.is_relative_to(Path(root).resolve()):
        raise ValueError(f"Path escapes project: {value}")
    return target


def settings(root, config="config.json"):
    config_path = local_path(root, config)
    value = read_json(config_path)
    for key in ("cache", "output"):
        local_path(root, value["paths"][key])
    west, south, east, north = value["aoi"]["bbox"]
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("Invalid AOI bounding box")
    return value


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def make_manifest(root, files, kind):
    root = Path(root).resolve()
    records = []
    for path in sorted(set(Path(p).resolve() for p in files)):
        relative = path.relative_to(root).as_posix()
        local_path(root, relative)
        records.append({"path": relative, "bytes": path.stat().st_size, "sha256": sha256(path)})
    return {"schema_version": 1, "kind": kind,
            "created_at": datetime.now(timezone.utc).isoformat(), "files": records}


def check_manifest(root, manifest, deep=False):
    errors = []
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
        return ["Invalid package manifest schema"]
    seen = set()
    for record in manifest["files"]:
        try:
            name = record["path"]
            if name in seen:
                raise ValueError(f"Duplicate manifest path: {name}")
            seen.add(name)
            path = local_path(root, name)
            if not path.is_file():
                errors.append(f"Missing file: {name}")
            elif path.stat().st_size != record["bytes"]:
                errors.append(f"Size mismatch: {name}")
            elif deep and sha256(path) != record["sha256"]:
                errors.append(f"SHA-256 checksum mismatch: {name}")
        except (KeyError, ValueError, TypeError, OSError) as error:
            errors.append(str(error))
    return errors


@contextmanager
def no_network():
    """Fail immediately, without DNS/retry waits; used only during local analysis."""
    def blocked(*args, **kwargs):
        raise RuntimeError("Network acquisition is disabled for local rebuilds; supply the missing dataset.")
    with ExitStack() as stack:
        for attribute in ("connect", "connect_ex", "sendto"):
            stack.enter_context(patch.object(socket.socket, attribute, blocked))
        stack.enter_context(patch.object(socket, "getaddrinfo", blocked))
        # requests is optional for the stdlib-only viewing path.
        try:
            import requests
        except ImportError:
            pass
        else:
            stack.enter_context(patch.object(requests.sessions.Session, "request", blocked))
        yield


def preflight(root, config):
    """Check required sources, exact AOI OSM cache and readable WorldCover tiles."""
    from . import pipeline
    import rasterio
    root = Path(root)
    cache = local_path(root, config["paths"]["cache"])
    files = set()
    for pattern in config["inputs"]["firms_globs"]:
        local_path(root, pattern)
        files.update(root.glob(pattern))
    if not files:
        raise ValueError("No FIRMS CSV files. Extract the data package into the project folder.")
    for path in sorted(files):
        local_path(root, path.relative_to(root))
        with path.open(encoding="utf-8-sig", newline="") as stream:
            header = next(csv.reader(stream), [])
        if not {"latitude", "longitude", "acq_date", "acq_time", "satellite"}.issubset(header):
            raise ValueError(f"Invalid FIRMS header: {path}")
    with no_network():
        facilities = pipeline.fetch_osm_facilities(config, cache, refresh=False)
        if facilities.empty:
            raise ValueError("OSM cache contains no usable industrial context features.")
        west, south, east, north = config["aoi"]["bbox"]
        for lat in range(math.floor(south / 3) * 3, math.floor(north / 3) * 3 + 1, 3):
            for lon in range(math.floor(west / 3) * 3, math.floor(east / 3) * 3 + 1, 3):
                tile = pipeline.worldcover_tile(lat, lon)
                path = pipeline.cached_worldcover_tile(tile, cache)
                with rasterio.open(path) as raster:
                    if raster.crs is None or raster.count != 1:
                        raise ValueError(f"Invalid WorldCover raster: {path}")
                    expected_bounds = (lon, lat, lon + 3, lat + 3)
                    if raster.crs.to_epsg() != 4326 or any(
                        abs(actual - expected) > 1e-6
                        for actual, expected in zip(raster.bounds, expected_bounds)
                    ):
                        raise ValueError(f"WorldCover georeferencing does not match tile {tile}")
                    raster.read(1, window=((0, 1), (0, 1)))
    return {"firms_files": len(files), "osm_context_features": len(facilities)}


def csv_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream)


def validate_results(output):
    output = Path(output)
    errors = []
    for name in RESULT_FILES[:9]:
        if not (output / name).is_file():
            errors.append(f"Missing result: {name}")
    if not (output / "run_manifest.json").is_file():
        return errors + ["Missing result: run_manifest.json"]
    manifest = read_json(output / "run_manifest.json")
    if manifest.get("external_data", {}).get("worldcover_errors"):
        errors.append("WorldCover context errors: " + str(manifest["external_data"]["worldcover_errors"]))
    expected = manifest.get("outputs", {})
    event_scores = {}
    for name, key, id_field in (("events", "events", "event_id"), ("sites", "persistent_sites", "site_id"),
                                 ("detections", "detections", "detection_id")):
        path = output / f"{name}.csv"
        if not path.exists():
            continue
        ids, count = set(), 0
        for row in csv_rows(path):
            count += 1
            identifier = row.get(id_field)
            if not identifier or identifier in ids:
                errors.append(f"Missing or duplicate {id_field} in {name}.csv")
            ids.add(identifier)
            if name == "events":
                try:
                    score = float(row["anomaly_score"]) if row.get("anomaly_score") else None
                    if score is not None and (not math.isfinite(score) or not 0 <= score <= 1):
                        errors.append(f"Invalid anomaly score: {identifier}")
                    event_scores[identifier] = score
                except ValueError:
                    errors.append(f"Invalid anomaly score: {identifier}")
        if count != expected.get(key):
            errors.append(f"{name} count disagrees with run manifest")
        if name == "events" and count == 0:
            errors.append("No events produced")
    path = output / "events.geojson"
    if path.exists():
        features = read_json(path).get("features", [])
        geo_ids = [f["properties"].get("event_id") for f in features]
        if len(set(geo_ids)) != len(geo_ids) or set(geo_ids) != set(event_scores):
            errors.append("Event CSV and GeoJSON IDs disagree")
        for feature in features:
            p = feature["properties"]
            if p.get("anomaly_score") != event_scores.get(p.get("event_id")):
                errors.append("Event CSV and GeoJSON scores disagree")
                break
    return errors


def portable_previews(features, output):
    """Relocate historical absolute preview references, never read external files."""
    output = Path(output)
    for feature in features:
        p = feature.get("properties", {})
        value = p.get("sentinel_preview")
        if not isinstance(value, str):
            continue
        normalized = value.replace("\\", "/")
        if "/sentinel/" in normalized:
            relative = "sentinel/" + normalized.rsplit("/sentinel/", 1)[1]
            try:
                candidate = local_path(output, relative)
                if candidate.is_file():
                    p["sentinel_preview"] = relative
            except ValueError:
                pass


def prepare(root, config, output=None):
    from .viewer import write_web_viewer
    root = Path(root)
    output = Path(output) if output is not None else local_path(root, config["paths"]["output"])
    required = ("events.geojson", "facilities.geojson", "run_manifest.json")
    missing = [name for name in required if not (output / name).is_file()]
    if missing:
        raise ValueError("Missing saved results: " + ", ".join(missing) + ". Extract the data package first.")
    events = read_json(output / "events.geojson")
    portable_previews(events["features"], output)
    runtime = dict(config)
    runtime["paths"] = dict(config["paths"], cache=str(local_path(root, config["paths"]["cache"])))
    runtime["_runtime_metadata"] = read_json(output / "run_manifest.json")
    return write_web_viewer(output, events, read_json(output / "facilities.geojson"), runtime)


def publish(stage, output, backup):
    """Same-filesystem directory swaps; keep the previous result for rollback."""
    stage, output, backup = map(Path, (stage, output, backup))
    if backup.exists():
        raise FileExistsError(backup)
    had_output = output.exists()
    if had_output:
        output.rename(backup)
    try:
        stage.rename(output)
    except BaseException:
        if had_output:
            backup.rename(output)
        raise


def compare_results(before, after):
    fields = ("decision", "source_context", "anomaly_score", "site_id")
    old = {r["event_id"]: r for r in csv_rows(Path(before) / "events.csv")}
    new = {r["event_id"]: r for r in csv_rows(Path(after) / "events.csv")}
    common = old.keys() & new.keys()
    changes = Counter()
    for identifier in common:
        for field in fields:
            a, b = old[identifier].get(field, ""), new[identifier].get(field, "")
            if field == "anomaly_score" and a and b:
                same = math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-12)
            else:
                same = a == b
            if not same:
                changes[field] += 1
    return {"reference_events": len(old), "rebuilt_events": len(new),
            "added_event_ids": len(new.keys() - old.keys()), "removed_event_ids": len(old.keys() - new.keys()),
            "changed_fields": dict(changes), "score_absolute_tolerance": 1e-12}


def rebuild(root, config, should_publish=False):
    from . import pipeline
    root = Path(root)
    preflight(root, config)
    output = local_path(root, config["paths"]["output"])
    run = root / "runs" / ("rebuild-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    stage = run / "outputs"
    stage.mkdir(parents=True)
    # Reuse only evidence whose event identity passes pipeline.reuse_sentinel_evidence.
    if (output / "events.csv").exists():
        shutil.copy2(output / "events.csv", stage / "events.csv")
    if (output / "sentinel").is_dir():
        shutil.copytree(output / "sentinel", stage / "sentinel")
    staged = json.loads(json.dumps(config))
    staged["paths"]["output"] = stage.relative_to(root).as_posix()
    dump_json(run / "config.json", staged)
    with no_network():
        pipeline.pipeline(root, run / "config.json", refresh=False, skip_sentinel=True)
    errors = validate_results(stage)
    if errors:
        raise ValueError("Staged results rejected; existing outputs retained:\n" + "\n".join(errors[:20]))
    comparison = compare_results(output, stage) if (output / "events.csv").exists() else {"reference": "none"}
    dump_json(run / "comparison.json", comparison)
    prepare(root, config, output=stage)
    if should_publish:
        publish(stage, output, run / "previous_outputs")
    return {"run_directory": str(run), "published": should_publish, "comparison": comparison}


def write_package(root, files, destination, kind):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = make_manifest(root, files, kind)
    temporary = destination.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for entry in manifest["files"]:
                archive.write(local_path(root, entry["path"]), entry["path"])
            archive.writestr(f"MANIFEST-{kind}.json", json.dumps(manifest, indent=2))
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return manifest


def package_files(root, config, kind):
    root = Path(root)
    if kind == "source":
        names = ("app.py", "config.json", "requirements.txt", "README.md",
                 "Dockerfile", "compose.yaml", ".dockerignore", ".gitignore")
        files = [root / n for n in names]
        for directory, patterns in (
            ("backend", ("*.py",)), ("scripts", ("*.py",)),
            ("tests", ("*.py", "*.js")), ("docs", ("*.md",)),
            ("frontend", ("*.html", "*.css", "*.js", "*.txt", "*.ttf")),
        ):
            for pattern in patterns:
                files.extend(p for p in (root / directory).rglob(pattern)
                             if p.is_file() and "__pycache__" not in p.parts)
        return files
    output = local_path(root, config["paths"]["output"])
    cache = local_path(root, config["paths"]["cache"])
    files = []
    for pattern in config["inputs"]["firms_globs"]:
        local_path(root, pattern)
        for path in root.glob(pattern):
            files.append(path)
            if path.with_suffix(".meta.json").is_file():
                files.append(path.with_suffix(".meta.json"))
    for pattern in ("osm_industrial_context_*.json", "osm_roads.geojson", "source_sync.json",
                    "worldcover/*.tif", "sentinel_evidence/**/*.json"):
        files.extend(cache.glob(pattern))
    files += [output / n for n in RESULT_FILES if (output / n).is_file()]
    for directory in ("site", "sentinel"):
        files += [p for p in (output / directory).rglob("*") if p.is_file()]
    return files
