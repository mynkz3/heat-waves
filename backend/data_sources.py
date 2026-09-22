"""Public FIRMS acquisition and auditable, sensor-aware local CSV ingestion."""
from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests


LIVE_FEEDS = {
    "viirs-snpp": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Global_7d.csv",
    "viirs-jpss1": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-20-viirs-c2/csv/J1_VIIRS_C2_Global_7d.csv",
}
REQUIRED = {"latitude", "longitude", "acq_date", "acq_time", "satellite"}


def dump_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False), encoding="utf-8")


def recover_complete_csv(destination, quality):
    """Recover only a response whose bytes exactly match its saved MD5-style ETag.

    ETags are generally opaque: other formats or mismatches are never accepted.
    This handles a complete body rejected later by an outdated CSV validator.
    """
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    temporary = destination.with_suffix(".csv.part")
    state = json.loads(destination.with_suffix(".partial.meta.json").read_text(encoding="utf-8"))
    md5, sha256 = hashlib.md5(usedforsecurity=False), hashlib.sha256()
    with temporary.open("rb") as handle:
        header = handle.readline().decode("utf-8-sig").strip().split(",")
        if not REQUIRED.issubset(header):
            raise ValueError("Saved response is not a FIRMS CSV")
        handle.seek(0)
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            md5.update(chunk)
            sha256.update(chunk)
        handle.seek(-1, 2)
        if handle.read(1) != b"\n":
            raise ValueError("Saved CSV has an incomplete final line")
    if state.get("validator") != '"' + md5.hexdigest() + '"':
        raise ValueError("Saved response does not exactly match an MD5-style server ETag")
    metadata = {
        "file": str(destination), "url": state["url"], "data_quality": quality,
        "status": "recovered_verified_response", "etag": state["validator"],
        "retrieved_at": pd.Timestamp(temporary.stat().st_mtime, unit="s", tz="UTC").isoformat(),
        "validated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "bytes": temporary.stat().st_size, "sha256": sha256.hexdigest(),
        "integrity_check": "Local MD5 exactly equals the saved strong response ETag; original bytes unchanged.",
    }
    temporary.replace(destination)
    dump_json(destination.with_suffix(".meta.json"), metadata)
    return metadata


def download_csv(url, destination, quality, refresh=False):
    """Keep original bytes; publish a new download only after CSV-header validation."""
    destination = Path(destination)
    metadata_path = destination.with_suffix(".meta.json")
    if destination.exists() and not refresh:
        with destination.open(encoding="utf-8-sig") as handle:
            if REQUIRED.issubset(set(handle.readline().strip().split(","))):
                return {"file": str(destination), "url": url, "status": "cached", "data_quality": quality}
        raise ValueError(f"Cached CSV has an invalid header: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".csv.part")
    partial_metadata = destination.with_suffix(".partial.meta.json")
    state = json.loads(partial_metadata.read_text(encoding="utf-8")) if partial_metadata.exists() else {}
    if not destination.exists() and temporary.exists() and state.get("url") == url:
        try:
            return recover_complete_csv(destination, quality)
        except ValueError:
            pass
    for attempt in range(4):
        offset = temporary.stat().st_size if temporary.exists() else 0
        validator = state.get("validator") if state.get("url") == url else None
        headers = {"Range": f"bytes={offset}-", "If-Range": validator} if offset and validator else {}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(60, 120)) as response:
                response.raise_for_status()
                append = bool(offset and headers and response.status_code == 206)
                content_range = response.headers.get("Content-Range", "")
                if append and not content_range.startswith(f"bytes {offset}-"):
                    raise ValueError("Server returned a different byte range than requested")
                if not append:
                    offset = 0
                etag = response.headers.get("ETag", "")
                validator = etag if etag and not etag.startswith("W/") else response.headers.get("Last-Modified")
                state = {"url": url, "validator": validator}
                dump_json(partial_metadata, state)
                with temporary.open("ab" if append else "wb") as handle:
                    for chunk in response.iter_content(65536):
                        if chunk:
                            handle.write(chunk)
                expected = int(content_range.split("/")[-1]) if "/" in content_range and not content_range.endswith("*") else None
                if expected is not None and temporary.stat().st_size != expected:
                    raise requests.ConnectionError("Incomplete ranged response; retaining verified prefix for retry")
                with temporary.open(encoding="utf-8-sig") as handle:
                    header = set(handle.readline().strip().split(","))
                if not REQUIRED.issubset(header):
                    raise ValueError("Downloaded response is not a FIRMS CSV with the required columns")
                digest = hashlib.sha256()
                with temporary.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                metadata = {
                    "file": str(destination), "url": url, "status": "downloaded",
                    "data_quality": quality, "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat(),
                    "last_modified": response.headers.get("Last-Modified"), "etag": etag,
                    "bytes": temporary.stat().st_size, "sha256": digest.hexdigest(),
                }
                temporary.replace(destination)
                dump_json(metadata_path, metadata)
                partial_metadata.unlink(missing_ok=True)
                return metadata
        except requests.HTTPError as error:
            if error.response.status_code in (401, 403, 404, 416) or attempt == 3:
                raise
        except requests.RequestException as error:
            print(f"retry {attempt + 1}/4 {destination.name}: {str(error)[:180]}", flush=True)
            if attempt == 3:
                raise


def sync_firms(root, config, history=True, live=True):
    root = Path(root)
    requested_at = pd.Timestamp.now(tz="UTC").isoformat()
    products = config.get("acquisition", {}).get("archive_products", ["viirs-snpp", "viirs-jpss1"])
    jobs = []
    if history:
        for year in config["inputs"]["expected_years"]:
            for product in products:
                filename = f"{product}_{year}_India.csv"
                url = f"https://firms.modaps.eosdis.nasa.gov/data/country/{product}/{year}/{filename}"
                jobs.append((url, root / "data/raw/firms" / product / str(year) / filename, "science_quality", False))
    if live:
        now = pd.Timestamp.now(tz="UTC")
        date, stamp = now.strftime("%Y-%m-%d"), now.strftime("%Y%m%dT%H%M%S%fZ")
        for product, url in LIVE_FEEDS.items():
            jobs.append((url, root / "data/raw/firms/nrt" / date / f"{product}_7d_{stamp}.csv", "near_real_time", False))

    def fetch(job):
        try:
            result = download_csv(*job)
        except (requests.RequestException, OSError, ValueError) as error:
            result = {"url": job[0], "file": str(job[1]), "data_quality": job[2],
                      "status": "unavailable", "error": str(error)}
        print(f"{result['status']}: {Path(result['file']).name} {result.get('error', '')[:200]}", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=config.get("acquisition", {}).get("workers", 2)) as workers:
        files = list(workers.map(fetch, jobs))
    report = {"requested_at": requested_at, "completed_at": pd.Timestamp.now(tz="UTC").isoformat(), "files": files,
              "requested_history_years": config["inputs"]["expected_years"],
              "live_window_days": 7 if live else 0,
              "note": "An unavailable annual file is a coverage gap, not an empty fire year."}
    dump_json(root / config["paths"]["cache"] / "source_sync.json", report)
    return report


def ingest_firms(root, config):
    root = Path(root)
    paths = sorted({path for pattern in config["inputs"]["firms_globs"] for path in root.glob(pattern)})
    if not paths:
        raise FileNotFoundError("No FIRMS files match inputs.firms_globs")
    west, south, east, north = config["aoi"]["bbox"]
    now = pd.Timestamp.now(tz="UTC")
    accepted = config["inputs"].get("accepted_sensors")
    frames, file_audits = [], []
    for path in paths:
        metadata_path = path.with_suffix(".meta.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        audit = {"file": str(path.relative_to(root)), "raw_rows": 0, "aoi_valid_rows": 0,
                 "invalid_coordinate_rows": 0, "invalid_or_future_time_rows": 0,
                 "unsupported_sensor_rows": 0, "excluded_sensor_rows": 0,
                 "source_url": metadata.get("url"), "retrieved_at": metadata.get("retrieved_at"),
                 "source_latest_acquisition_utc": None, "source_earliest_acquisition_utc": None,
                 "data_quality": metadata.get("data_quality", "unknown")}
        for frame in pd.read_csv(path, chunksize=200000, low_memory=False,
                                 dtype={"acq_time": str, "acq_date": str}):
            missing = REQUIRED - set(frame.columns)
            if missing:
                raise ValueError(f"{path.name}: required fields missing: {sorted(missing)}")
            audit["raw_rows"] += len(frame)
            latitude = pd.to_numeric(frame["latitude"], errors="coerce")
            longitude = pd.to_numeric(frame["longitude"], errors="coerce")
            valid_coordinates = latitude.between(-90, 90) & longitude.between(-180, 180)
            audit["invalid_coordinate_rows"] += int((~valid_coordinates).sum())
            time_text = frame["acq_time"].str.replace(r"\.0$", "", regex=True).str.zfill(4)
            acquired = pd.to_datetime(frame["acq_date"] + "T" + time_text.str[:2] + ":" + time_text.str[2:],
                                      utc=True, errors="coerce")
            valid_time = acquired.notna() & acquired.le(now)
            audit["invalid_or_future_time_rows"] += int((~valid_time).sum())
            latest = acquired[valid_time].max()
            if pd.notna(latest):
                prior = audit["source_latest_acquisition_utc"]
                audit["source_latest_acquisition_utc"] = max(latest.isoformat(), prior or "")
                earliest = acquired[valid_time].min().isoformat()
                audit["source_earliest_acquisition_utc"] = min(earliest, audit["source_earliest_acquisition_utc"] or earliest)
            mask = valid_coordinates & longitude.between(west, east) & latitude.between(south, north) & valid_time
            frame = frame.loc[mask].copy()
            if frame.empty:
                continue
            frame["latitude"], frame["longitude"] = latitude.loc[mask], longitude.loc[mask]
            frame["acquired_at"] = acquired.loc[mask]
            inferred_instrument = (
                "VIIRS" if {"bright_ti4", "bright_ti5"}.issubset(frame.columns)
                else "MODIS" if {"brightness", "bright_t31"}.issubset(frame.columns)
                else "UNSUPPORTED"
            )
            instrument = frame.get("instrument", pd.Series(inferred_instrument, index=frame.index)).astype(str).str.upper()
            audit["instrument_inferred_from_band_columns"] = "instrument" not in frame.columns
            platform = frame["satellite"].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
            viirs, modis = instrument.eq("VIIRS"), instrument.eq("MODIS")
            frame["sensor"] = np.select([
                viirs & platform.isin(["N", "SNPP", "SUOMINPP"]),
                viirs & platform.isin(["N20", "NOAA20", "J1", "JPSS1"]),
                viirs & platform.isin(["N21", "NOAA21", "J2", "JPSS2"]),
                modis & platform.eq("TERRA"), modis & platform.eq("AQUA")],
                ["VIIRS_SNPP", "VIIRS_NOAA20", "VIIRS_NOAA21", "MODIS_TERRA", "MODIS_AQUA"],
                default="UNSUPPORTED")
            audit["unsupported_sensor_rows"] += int(frame["sensor"].eq("UNSUPPORTED").sum())
            keep_sensor = frame["sensor"].ne("UNSUPPORTED")
            if accepted:
                audit["excluded_sensor_rows"] += int((keep_sensor & ~frame["sensor"].isin(accepted)).sum())
                keep_sensor &= frame["sensor"].isin(accepted)
            frame = frame.loc[keep_sensor].copy()
            viirs = frame["sensor"].str.startswith("VIIRS")
            if frame.empty:
                continue
            def numeric(column):
                return pd.to_numeric(frame.get(column, pd.Series(np.nan, index=frame.index)), errors="coerce")
            frame["brightness"] = numeric("bright_ti4").where(viirs, numeric("brightness"))
            frame["longwave_brightness"] = numeric("bright_ti5").where(viirs, numeric("bright_t31"))
            frame["brightness_temperature_difference"] = frame["brightness"] - frame["longwave_brightness"]
            for column in ("frp", "scan", "track"):
                values = numeric(column)
                # VIIRS FRP=0 can mean a failed retrieval, not absence of heat.
                # NASA Collection-2 375m Active Fire User Guide, FRP limitation.
                frame[column] = values.where(np.isfinite(values) & values.gt(0))
            confidence = frame.get("confidence", pd.Series("", index=frame.index)).astype(str)
            frame["confidence_score"] = confidence.str.strip().str.lower().map(
                {"l": .2, "low": .2, "n": .6, "nominal": .6, "h": 1, "high": 1}).fillna(
                pd.to_numeric(confidence, errors="coerce").clip(0, 100) / 100)
            frame["firms_type_reference"] = numeric("type")
            frame["source_version"] = frame.get("version", pd.Series("unknown", index=frame.index)).astype(str)
            nrt = frame["source_version"].str.contains("NRT", case=False)
            known_science = pd.to_numeric(frame["source_version"], errors="coerce").notna()
            frame["data_quality"] = np.select([nrt, known_science], ["near_real_time", "science_quality"],
                                               default=metadata.get("data_quality", "unknown"))
            frame["daynight"] = frame.get("daynight", pd.Series("unknown", index=frame.index)).astype(str)
            frame["source_file"] = str(path.relative_to(root))
            frame["source_retrieved_at"] = metadata.get("retrieved_at")
            frame["source_url"] = metadata.get("url")
            keep = ["latitude", "longitude", "acquired_at", "sensor", "brightness", "longwave_brightness",
                    "brightness_temperature_difference", "frp", "scan", "track", "confidence_score",
                    "firms_type_reference", "daynight", "source_file", "source_version", "data_quality",
                    "source_retrieved_at", "source_url"]
            frames.append(frame[keep])
            audit["aoi_valid_rows"] += len(frame)
        file_audits.append(audit)
    if not frames:
        raise ValueError("No valid accepted-sensor observations in the AOI")
    detections = pd.concat(frames, ignore_index=True)
    before = len(detections)
    detections["_quality"] = detections["data_quality"].map({"science_quality": 2, "near_real_time": 1}).fillna(0)
    detections = detections.sort_values(["_quality", "source_retrieved_at"], na_position="first", kind="stable")
    keys = ["sensor", "latitude", "longitude", "acquired_at"]
    detections = detections.drop_duplicates(keys, keep="last").drop(columns="_quality")
    detections = detections.sort_values(["acquired_at", "sensor", "latitude", "longitude"]).reset_index(drop=True)
    detections.insert(0, "detection_id", ["D" + hashlib.sha1(
        f"{r.sensor}|{r.latitude!r}|{r.longitude!r}|{r.acquired_at.isoformat()}".encode()).hexdigest()[:16]
        for r in detections.itertuples()])
    years = sorted(int(x) for x in detections["acquired_at"].dt.year.unique())
    science = detections[detections["data_quality"].eq("science_quality")]
    science_years = sorted(int(x) for x in science["acquired_at"].dt.year.unique())
    expected = config["inputs"].get("expected_years", [])
    yearly = detections.assign(year=detections["acquired_at"].dt.year).groupby(["year", "sensor", "data_quality"]).size()
    present = {(int(year), sensor) for (year, sensor, quality), _ in yearly.items() if quality == "science_quality"}
    missing_sensor_years = [{"year": year, "sensor": sensor} for year in expected
                            for sensor in (accepted or []) if (year, sensor) not in present]
    audit = {
        "input_files": file_audits, "aoi_rows": len(detections), "duplicates_removed": before - len(detections),
        "date_min": detections["acquired_at"].min().isoformat(), "date_max": detections["acquired_at"].max().isoformat(),
        "available_years": years, "science_quality_years": science_years, "expected_years": expected,
        "missing_expected_years": sorted(set(expected) - set(science_years)),
        "missing_sensor_years": missing_sensor_years,
        "sensor_counts": {str(k): int(v) for k, v in detections["sensor"].value_counts().items()},
        "quality_counts": {str(k): int(v) for k, v in detections["data_quality"].value_counts().items()},
        "year_sensor_quality_counts": [{"year": int(y), "sensor": s, "quality": q, "detections": int(n)}
                                       for (y, s, q), n in yearly.items()],
        "model_leakage_guard": "FIRMS type is audit-only; models never use it or disaster/normal labels.",
        "coverage_note": "Positive detections are not an observation-opportunity mask; gaps are not no-fire evidence.",
    }
    return detections, audit


def sync_roads(root, config, refresh=False):
    destination = Path(root) / config["paths"]["cache"] / "osm_roads.geojson"
    if destination.exists() and not refresh:
        return {"status": "cached", "file": str(destination)}
    w, s, e, n = config["aoi"]["bbox"]
    query = (f'[out:json][timeout:60];way["highway"~"^(motorway|trunk|primary|secondary|tertiary|residential|unclassified)$"]'
             f"({s},{w},{n},{e});out geom;")
    errors = []
    for endpoint in ("https://overpass.kumi.systems/api/interpreter", "https://overpass-api.de/api/interpreter"):
        try:
            response = requests.post(endpoint, data={"data": query}, timeout=(20, 100))
            response.raise_for_status()
            result = response.json()
            if result.get("remark"):
                raise ValueError(result["remark"])
            features = []
            for way in result.get("elements", []):
                coordinates = [[p["lon"], p["lat"]] for p in way.get("geometry", []) if "lon" in p and "lat" in p]
                if len(coordinates) >= 2:
                    tags = way.get("tags", {})
                    features.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coordinates},
                                     "properties": {"osm_id": way["id"], "name": tags.get("name", ""),
                                                    "highway": tags.get("highway", "")}})
            dump_json(destination, {"type": "FeatureCollection", "features": features,
                                    "attribution": "OpenStreetMap contributors (ODbL)",
                                    "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat()})
            return {"status": "downloaded", "features": len(features), "file": str(destination)}
        except (requests.RequestException, ValueError) as error:
            errors.append(str(error))
    return {"status": "unavailable", "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--live-only", action="store_true")
    parser.add_argument("--no-live", action="store_true")
    parser.add_argument("--roads", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / args.config).read_text(encoding="utf-8"))
    sync_firms(root, config, history=not args.live_only, live=not args.no_live)
    if args.roads:
        print(json.dumps(sync_roads(root, config)), flush=True)


if __name__ == "__main__":
    main()
