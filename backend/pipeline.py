from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
from sklearn.cluster import AgglomerativeClustering, DBSCAN
from sklearn.ensemble import IsolationForest
from sklearn.metrics import adjusted_rand_score
from sklearn.model_selection import ParameterGrid

from . import data_sources
from .peer_scoring import score_vegetation_peers


EARTH_SEARCH = "https://earth-search.aws.element84.com/v1/search"
OVERPASS_ENDPOINTS = (
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
)
WORLDCOVER_URL = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/"
    "v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)
WORLDCOVER_CLASSES = {
    10: "tree_cover",
    20: "shrubland",
    30: "grassland",
    40: "cropland",
    50: "built_up",
    60: "bare_sparse",
    70: "snow_ice",
    80: "water",
    90: "herbaceous_wetland",
    95: "mangroves",
    100: "moss_lichen",
}
VEGETATION_CLASSES = {10, 20, 30, 40, 90, 95, 100}
SENTINEL_COLUMNS = [
    "event_id", "sentinel_pre_item", "sentinel_post_item", "sentinel_pre_datetime",
    "sentinel_post_datetime", "sentinel_pre_scene_cloud", "sentinel_post_scene_cloud",
    "sentinel_joint_valid_fraction", "sentinel_pre_swir12_mean",
    "sentinel_post_swir12_mean", "sentinel_swir12_relative_change",
    "sentinel_normalized_swir_change", "sentinel_evidence", "sentinel_adjustment",
    "sentinel_pre_crop", "sentinel_post_crop",
]
DYNAMIC_FEATURES = (
    "frp_peak_mw",
    "frp_median_mw",
    "detection_peak",
    "duration_hours",
    "spread_km",
    "brightness_temperature_difference_max",
)


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    west, south, east, north = config["aoi"]["bbox"]
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("aoi.bbox must be [west, south, east, north]")
    models = config["models"]
    positive = (
        "event_spatial_km", "event_temporal_hours", "event_min_samples",
        "site_min_events", "site_max_diameter_km", "min_history_events",
        "isolation_min_training_events", "isolation_trees",
    )
    invalid = [name for name in positive if not isinstance(models.get(name), (int, float)) or models[name] <= 0]
    if invalid:
        raise ValueError(f"models values must be positive: {', '.join(invalid)}")
    if not 0 <= models["anomaly_percentile"] <= 1:
        raise ValueError("models.anomaly_percentile must be between 0 and 1")
    sentinel = config["sentinel"]
    if (sentinel["max_events"] is not None and sentinel["max_events"] < 0) or sentinel["crop_radius_m"] <= 0:
        raise ValueError("sentinel max_events/radius values are invalid")
    return config


def json_value(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, np.generic):
        return json_value(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def clean_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(item) for item in value]
    return json_value(value)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean_json(payload), indent=2, allow_nan=False), encoding="utf-8")


def haversine_km(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> np.ndarray:
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * np.arcsin(np.sqrt(a))


def local_xy_km(lat: pd.Series, lon: pd.Series, latitude_origin: float) -> tuple[np.ndarray, np.ndarray]:
    x = (lon.to_numpy() - lon.mean()) * 111.32 * math.cos(math.radians(latitude_origin))
    y = (lat.to_numpy() - lat.mean()) * 110.57
    return x, y


def confidence_score(series: pd.Series) -> pd.Series:
    labels = series.astype(str).str.lower().map({"l": 0.2, "n": 0.6, "h": 1.0})
    numeric = pd.to_numeric(series, errors="coerce").clip(0, 100) / 100
    return labels.fillna(numeric).fillna(0.0)


def ingest_firms(root: Path, config: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    return data_sources.ingest_firms(root, config)


def cluster_events(
    detections: pd.DataFrame, config: dict[str, Any]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    model_config = config["models"]
    latitude_origin = float(detections["latitude"].mean())
    x, y = local_xy_km(detections["latitude"], detections["longitude"], latitude_origin)
    hours = (
        (detections["acquired_at"] - detections["acquired_at"].min()).dt.total_seconds().to_numpy()
        / 3600
    )
    features = np.column_stack((
        x / model_config["event_spatial_km"],
        y / model_config["event_spatial_km"],
        hours / model_config["event_temporal_hours"],
    ))
    labels = DBSCAN(eps=1.0, min_samples=model_config["event_min_samples"]).fit_predict(features)
    next_label = labels.max(initial=-1) + 1
    for index in np.flatnonzero(labels < 0):
        labels[index] = next_label
        next_label += 1
    # DBSCAN connectivity can chain forever; split each connected component into
    # episodes with a hard maximum span so recurrence remains observable.
    episode_labels = np.empty_like(labels)
    episode_label = 0
    maximum_span = float(model_config["event_temporal_hours"])
    for cluster in np.unique(labels):
        indices = np.flatnonzero(labels == cluster)
        indices = indices[np.argsort(hours[indices])]
        episode_start = hours[indices[0]]
        for index in indices:
            if hours[index] - episode_start > maximum_span:
                episode_label += 1
                episode_start = hours[index]
            episode_labels[index] = episode_label
        episode_label += 1
    detections = detections.copy()
    detections["_event_cluster"] = episode_labels

    # Build all event summaries in vectorized group-bys. A Python loop over each
    # singleton noise event made full-region runs scale with the number of groups.
    detections["_is_viirs"] = detections["sensor"].str.startswith("VIIRS").astype(int)
    detections["_is_modis"] = detections["sensor"].str.startswith("MODIS").astype(int)
    phase = detections["daynight"].astype(str).str.upper()
    detections["_phase"] = phase.where(phase.isin(["D", "N"]), "unknown")
    detections["_is_night"] = detections["_phase"].map({"D": 0.0, "N": 1.0})
    detections["_type2_reference"] = np.where(
        detections["firms_type_reference"].notna(),
        detections["firms_type_reference"].eq(2).astype(float),
        np.nan,
    )
    grouped = detections.groupby("_event_cluster", sort=False)
    events = grouped.agg(
        start_time=("acquired_at", "min"),
        end_time=("acquired_at", "max"),
        latitude=("latitude", "mean"),
        longitude=("longitude", "mean"),
        detection_count=("detection_id", "size"),
        viirs_count=("_is_viirs", "sum"),
        modis_count=("_is_modis", "sum"),
        brightness_max=("brightness", "max"),
        brightness_temperature_difference_max=("brightness_temperature_difference", "max"),
        night_fraction=("_is_night", "mean"),
        confidence_mean=("confidence_score", "mean"),
        firms_type_2_reference_fraction=("_type2_reference", "mean"),
    )
    snapshots = (
        detections.groupby(["_event_cluster", "sensor", "_phase", "acquired_at"], as_index=False)
        .agg(frp_snapshot=("frp", "sum"), frp_valid=("frp", "count"), detection_snapshot=("detection_id", "size"))
    )
    snapshots.loc[snapshots["frp_valid"].eq(0), "frp_snapshot"] = np.nan
    snapshot_summary = snapshots.groupby("_event_cluster").agg(
        observation_count=("sensor", "size"),
        frp_peak_mw=("frp_snapshot", "max"),
        frp_median_mw=("frp_snapshot", "median"),
        detection_peak=("detection_snapshot", "max"),
    )
    snapshots["_family"] = np.where(
        snapshots["sensor"].str.startswith("VIIRS"), "viirs", "modis"
    )
    family_peaks = snapshots.pivot_table(
        index="_event_cluster", columns="_family", values="frp_snapshot", aggfunc="max"
    )
    events = events.join(snapshot_summary)
    events["frp_peak_all_sensors_mw"] = events["frp_peak_mw"]
    # Fixed platform preference keeps added satellites from silently changing
    # the radiometric baseline. Other platforms remain visible as observations.
    platform_priority = {"VIIRS_SNPP": 0, "VIIRS_NOAA20": 1, "VIIRS_NOAA21": 2,
                         "MODIS_TERRA": 3, "MODIS_AQUA": 4}
    representatives = snapshots.assign(
        _priority=snapshots["sensor"].map(platform_priority).fillna(9),
        _phase_priority=snapshots["_phase"].map({"N": 0, "D": 1}).fillna(2),
        _invalid_phase=snapshots["_phase"].eq("unknown").astype(int),
    ).sort_values(["_event_cluster", "_invalid_phase", "_priority", "_phase_priority"]).drop_duplicates("_event_cluster")
    representative_sensor = representatives.set_index("_event_cluster")["sensor"]
    representative_phase = representatives.set_index("_event_cluster")["_phase"]
    events["model_sensor"] = representative_sensor
    selected = detections[
        detections["sensor"].eq(detections["_event_cluster"].map(representative_sensor))
        & detections["_phase"].eq(detections["_event_cluster"].map(representative_phase))
    ]
    selected_snapshots = snapshots[
        snapshots["sensor"].eq(snapshots["_event_cluster"].map(representative_sensor))
        & snapshots["_phase"].eq(snapshots["_event_cluster"].map(representative_phase))
    ]
    primary_summary = selected_snapshots.groupby("_event_cluster").agg(
        frp_peak_mw=("frp_snapshot", "max"), frp_median_mw=("frp_snapshot", "median"),
        detection_peak=("detection_snapshot", "max"),
    )
    for column in primary_summary:
        events[column] = primary_summary[column]
    primary_radiometry = selected.groupby("_event_cluster").agg(
        brightness_max=("brightness", "max"),
        brightness_temperature_difference_max=("brightness_temperature_difference", "max"),
        model_night_fraction=("_is_night", "mean"),
    )
    for column in primary_radiometry:
        events[column] = primary_radiometry[column]
    events["model_daynight"] = representative_phase
    events["detected_daynight"] = grouped["_phase"].agg(lambda values: "|".join(sorted(set(values))))
    events["detected_sensors"] = grouped["sensor"].agg(lambda values: "|".join(sorted(set(values))))
    if "data_quality" in detections:
        events["data_quality"] = grouped["data_quality"].agg(lambda values: "|".join(sorted(set(values))))
    if "scan" in detections:
        events["max_scan_km"] = grouped["scan"].max()
        events["max_track_km"] = grouped["track"].max()
    events["viirs_frp_peak_mw"] = family_peaks.get("viirs", np.nan)
    events["modis_frp_peak_mw"] = family_peaks.get("modis", np.nan)
    events["duration_hours"] = (
        events["end_time"] - events["start_time"]
    ).dt.total_seconds() / 3600
    centroid_lat = detections["_event_cluster"].map(events["latitude"])
    centroid_lon = detections["_event_cluster"].map(events["longitude"])
    detections["_distance_km"] = haversine_km(
        detections["latitude"], detections["longitude"], centroid_lat, centroid_lon
    )
    events["spread_km"] = detections.groupby("_event_cluster")["_distance_km"].max()
    events = (
        events.reset_index()
        .sort_values(["start_time", "latitude", "longitude"])
        .reset_index(drop=True)
    )
    event_keys = grouped["detection_id"].agg(
        lambda values: "E" + hashlib.sha1("|".join(sorted(values)).encode()).hexdigest()[:16]
    )
    events.insert(0, "event_id", events["_event_cluster"].map(event_keys))
    cluster_to_event = events.set_index("_event_cluster")["event_id"]
    detections["event_id"] = detections["_event_cluster"].map(cluster_to_event)
    helper_columns = [
        "_event_cluster", "_is_viirs", "_is_modis", "_is_night", "_phase",
        "_type2_reference", "_distance_km",
    ]
    return detections.drop(columns=helper_columns), events.drop(columns="_event_cluster")


def discover_sites(events: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    model_config = config["models"]
    latitude_origin = float(events["latitude"].mean())
    x, y = local_xy_km(events["latitude"], events["longitude"], latitude_origin)
    xy = np.column_stack((x, y))
    labels = np.full(len(events), -1)
    if len(events) >= model_config["site_min_events"]:
        maximum_diameter = float(model_config["site_max_diameter_km"])
        # Connected components are a lossless pre-partition for a complete-link
        # cut at the same distance: no valid site can span two components.
        components = DBSCAN(
            eps=maximum_diameter, min_samples=1, n_jobs=-1
        ).fit_predict(xy)
        next_label = 0
        for component in np.unique(components):
            indices = np.flatnonzero(components == component)
            if len(indices) < model_config["site_min_events"]:
                continue
            if len(indices) == 1:
                sublabels = np.zeros(1, dtype=int)
            else:
                sublabels = AgglomerativeClustering(
                    n_clusters=None,
                    distance_threshold=maximum_diameter,
                    linkage="complete",
                    metric="euclidean",
                    compute_full_tree=True,
                ).fit_predict(xy[indices])
            for sublabel in np.unique(sublabels):
                members = indices[sublabels == sublabel]
                if len(members) >= model_config["site_min_events"]:
                    labels[members] = next_label
                    next_label += 1
    events = events.copy()
    events["_site_cluster"] = labels
    valid_labels = sorted(label for label in np.unique(labels) if label >= 0)
    site_ids = {label: f"S{i:04d}" for i, label in enumerate(valid_labels, start=1)}
    events["site_id"] = events["_site_cluster"].map(site_ids).fillna("")

    site_records: list[dict[str, Any]] = []
    for cluster, group in events[events["_site_cluster"] >= 0].groupby("_site_cluster"):
        diameter = 0.0
        for point in group.itertuples():
            diameter = max(
                diameter,
                float(np.max(haversine_km(
                    point.latitude, point.longitude,
                    group["latitude"].to_numpy(), group["longitude"].to_numpy(),
                ))),
            )
        site_records.append({
            "site_id": site_ids[cluster],
            "latitude": float(np.average(group["latitude"], weights=group["detection_count"])),
            "longitude": float(np.average(group["longitude"], weights=group["detection_count"])),
            "event_count": len(group),
            "active_days": int(group["start_time"].dt.date.nunique()),
            "first_seen": group["start_time"].min(),
            "last_seen": group["end_time"].max(),
            "total_detections": int(group["detection_count"].sum()),
            "frp_peak_mw_max": float(group["frp_peak_mw"].max()),
            "frp_peak_mw_median": float(group["frp_peak_mw"].median()),
            "diameter_km": diameter,
        })
    sites = pd.DataFrame(site_records)
    if sites.empty:
        sites = pd.DataFrame(columns=[
            "site_id", "latitude", "longitude", "event_count", "active_days",
            "first_seen", "last_seen", "total_detections", "frp_peak_mw_max",
            "frp_peak_mw_median", "diameter_km",
        ])
    return events.drop(columns="_site_cluster"), sites


def fetch_osm_facilities(config: dict[str, Any], cache_dir: Path, refresh: bool) -> pd.DataFrame:
    west, south, east, north = config["aoi"]["bbox"]
    bbox = f"({south},{west},{north},{east})"
    query = (
        "[out:json][timeout:120];("
        f'nwr["power"="plant"]{bbox};'
        f'nwr["landuse"="industrial"]{bbox};'
        f'nwr["landuse"="quarry"]{bbox};'
        f'nwr["man_made"="works"]{bbox};'
        f'nwr["industrial"]{bbox};'
        ");out geom;"
    )
    request_metadata = {"bbox": config["aoi"]["bbox"], "query": query}
    cache_key = hashlib.sha256(
        json.dumps(request_metadata, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    cache_path = cache_dir / f"osm_industrial_context_{cache_key}.json"
    if cache_path.exists() and not refresh:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("request") != request_metadata:
            raise RuntimeError("OSM cache metadata does not match its cache key")
        payload = cached["response"]
    else:
        errors: list[str] = []
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                response = requests.get(
                    endpoint,
                    params={"data": query},
                    headers={"User-Agent": "SIH26162-Thermal-Sentinel/0.1"},
                    timeout=150,
                )
                response.raise_for_status()
                payload = response.json()
                write_json(cache_path, {"request": request_metadata, "response": payload})
                break
            except (requests.RequestException, ValueError) as exc:
                errors.append(f"{endpoint}: {exc}")
        else:
            raise RuntimeError("OSM acquisition failed: " + " | ".join(errors))

    records: list[dict[str, Any]] = []
    for element in payload.get("elements", []):
        parts: list[list[tuple[float, float]]] = []
        if element.get("geometry"):
            parts.append([(point["lat"], point["lon"]) for point in element["geometry"]])
        for member in element.get("members", []):
            if member.get("geometry"):
                parts.append([(point["lat"], point["lon"]) for point in member["geometry"]])
        if "lat" in element and "lon" in element:
            parts = [[(float(element["lat"]), float(element["lon"]))]]
        points = [point for part in parts for point in part]
        if not points:
            continue
        tags = element.get("tags", {})
        kind = next(
            (f"{key}={tags[key]}" for key in ("power", "industrial", "landuse", "man_made") if key in tags),
            "industrial_context",
        )
        records.append({
            "facility_id": f"osm:{element.get('type')}:{element.get('id')}",
            "latitude": float(np.mean([point[0] for point in points])),
            "longitude": float(np.mean([point[1] for point in points])),
            "name": tags.get("name", tags.get("operator", "Unnamed mapped feature")),
            "kind": kind,
            "osm_type": element.get("type"),
            "geometry_basis": "node" if element.get("type") == "node" else "OSM footprint",
            "_geometry_parts": parts,
        })
    return pd.DataFrame(records)


def distance_to_parts_km(latitude: float, longitude: float, parts: list[list[tuple[float, float]]]) -> float:
    minimum = math.inf
    cos_lat = math.cos(math.radians(latitude))
    for part in parts:
        if not part:
            continue
        coordinates = np.array([
            ((lon - longitude) * 111.32 * cos_lat, (lat - latitude) * 110.57)
            for lat, lon in part
        ])
        ring_closed = len(coordinates) >= 4 and np.linalg.norm(coordinates[0] - coordinates[-1]) < 0.02
        if ring_closed:
            x, y = coordinates[:, 0], coordinates[:, 1]
            # Standard ray crossing at the origin, including implicitly closed OSM rings.
            crossings = 0
            for first, second in zip(coordinates, np.vstack((coordinates[1:], coordinates[:1]))):
                if (first[1] > 0) != (second[1] > 0):
                    cross_x = first[0] + (second[0] - first[0]) * (-first[1]) / (second[1] - first[1])
                    if cross_x > 0:
                        crossings += 1
            inside = crossings % 2 == 1
            if inside:
                return 0.0
        if len(coordinates) == 1:
            minimum = min(minimum, float(np.linalg.norm(coordinates[0])))
            continue
        for first, second in zip(coordinates[:-1], coordinates[1:]):
            segment = second - first
            denominator = float(np.dot(segment, segment))
            fraction = 0.0 if denominator == 0 else float(np.clip(-np.dot(first, segment) / denominator, 0, 1))
            minimum = min(minimum, float(np.linalg.norm(first + fraction * segment)))
    return minimum


def nearest_facility(frame: pd.DataFrame, facilities: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if facilities.empty:
        result["facility_distance_km"] = np.nan
        result["nearest_facility_id"] = ""
        result["nearest_facility_name"] = ""
        result["nearest_facility_kind"] = ""
        return result
    bounds = np.array([
        (
            min(point[0] for part in parts for point in part),
            max(point[0] for part in parts for point in part),
            min(point[1] for part in parts for point in part),
            max(point[1] for part in parts for point in part),
        )
        for parts in facilities["_geometry_parts"]
    ])
    distances_out, ids, names, kinds = [], [], [], []
    for row in result.itertuples():
        latitude_gap = np.maximum(
            np.maximum(bounds[:, 0] - row.latitude, row.latitude - bounds[:, 1]),
            0.0,
        )
        longitude_gap = np.maximum(
            np.maximum(bounds[:, 2] - row.longitude, row.longitude - bounds[:, 3]),
            0.0,
        )
        lower_bounds = np.hypot(
            longitude_gap * 111.32 * math.cos(math.radians(row.latitude)),
            latitude_gap * 110.57,
        )
        index = int(np.argmin(lower_bounds))
        best_distance = distance_to_parts_km(
            row.latitude, row.longitude, facilities.iloc[index]["_geometry_parts"]
        )
        for candidate in np.flatnonzero(lower_bounds <= best_distance + 1e-12):
            if candidate == index:
                continue
            distance = distance_to_parts_km(
                row.latitude, row.longitude, facilities.iloc[candidate]["_geometry_parts"]
            )
            if distance < best_distance or (distance == best_distance and candidate < index):
                index = int(candidate)
                best_distance = distance
        facility = facilities.iloc[index]
        distances_out.append(float(best_distance))
        ids.append(facility["facility_id"])
        names.append(facility["name"])
        kinds.append(facility["kind"])
    result["facility_distance_km"] = distances_out
    result["nearest_facility_id"] = ids
    result["nearest_facility_name"] = names
    result["nearest_facility_kind"] = kinds
    return result


def worldcover_tile(lat: float, lon: float) -> str:
    lat_base = math.floor(lat / 3) * 3
    lon_base = math.floor(lon / 3) * 3
    return f"{'N' if lat_base >= 0 else 'S'}{abs(lat_base):02d}{'E' if lon_base >= 0 else 'W'}{abs(lon_base):03d}"


def cached_worldcover_tile(tile: str, cache_dir: Path) -> Path:
    directory = cache_dir / "worldcover"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
    if destination.exists() and destination.stat().st_size > 1_000_000:
        return destination
    url = WORLDCOVER_URL.format(tile=tile)
    temporary = destination.with_suffix(".tif.part")
    with requests.get(url, stream=True, timeout=(30, 300)) as response:
        response.raise_for_status()
        expected = int(response.headers.get("content-length", 0))
        with temporary.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
    if expected and temporary.stat().st_size != expected:
        temporary.unlink(missing_ok=True)
        raise IOError(f"Incomplete WorldCover download for {tile}")
    temporary.replace(destination)
    return destination


def add_worldcover(
    frame: pd.DataFrame, config: dict[str, Any], cache_dir: Path
) -> tuple[pd.DataFrame, list[str]]:
    try:
        import rasterio
    except ImportError:
        result = frame.copy()
        for column in (
            "worldcover_dominant", "worldcover_coverage", "vegetation_fraction",
            "built_fraction", "bare_fraction",
        ):
            result[column] = np.nan
        return result, ["Rasterio unavailable; WorldCover context skipped"]

    radius_m = config["context"]["worldcover_radius_m"]
    samples: dict[int, list[int]] = defaultdict(list)
    coverage_by_index: dict[int, float] = {}
    errors: list[str] = []
    rows_by_tile: dict[str, list[tuple[int, pd.Series]]] = defaultdict(list)
    for index, row in frame.iterrows():
        rows_by_tile[worldcover_tile(row["latitude"], row["longitude"])].append((index, row))
    for tile, tile_rows in rows_by_tile.items():
        try:
            tile_path = cached_worldcover_tile(tile, cache_dir)
            with rasterio.open(tile_path) as dataset:
                from rasterio.windows import Window

                for index, row in tile_rows:
                    raster_row, raster_column = dataset.index(row["longitude"], row["latitude"])
                    x_m_per_pixel = abs(dataset.res[0]) * 111_320 * math.cos(math.radians(row["latitude"]))
                    y_m_per_pixel = abs(dataset.res[1]) * 110_570
                    radius_x = max(1, math.ceil(radius_m / x_m_per_pixel))
                    radius_y = max(1, math.ceil(radius_m / y_m_per_pixel))
                    row_start = max(0, raster_row - radius_y)
                    column_start = max(0, raster_column - radius_x)
                    window = Window(
                        column_start,
                        row_start,
                        min(2 * radius_x + 1, dataset.width - column_start),
                        min(2 * radius_y + 1, dataset.height - row_start),
                    )
                    values = dataset.read(1, window=window)
                    yy, xx = np.indices(values.shape)
                    center_y = raster_row - row_start
                    center_x = raster_column - column_start
                    circle = (
                        ((xx - center_x) * x_m_per_pixel) ** 2
                        + ((yy - center_y) * y_m_per_pixel) ** 2
                    ) <= radius_m**2
                    valid = circle & np.isin(values, list(WORLDCOVER_CLASSES))
                    expected_pixels = int(circle.sum())
                    coverage_by_index[index] = float(valid.sum() / expected_pixels) if expected_pixels else 0.0
                    samples[index] = values[valid].astype(int).tolist()
        except Exception as exc:  # Raster/network errors differ by GDAL build.
            errors.append(f"{tile}: {exc}")

    result = frame.copy()
    dominant, coverage, vegetation, built, bare = [], [], [], [], []
    for index in result.index:
        codes = samples.get(index, [])
        if not codes:
            dominant.append(None)
            coverage.append(0.0)
            vegetation.append(np.nan)
            built.append(np.nan)
            bare.append(np.nan)
            continue
        counts = Counter(codes)
        dominant.append(WORLDCOVER_CLASSES[counts.most_common(1)[0][0]])
        coverage.append(coverage_by_index.get(index, 0.0))
        vegetation.append(sum(code in VEGETATION_CLASSES for code in codes) / len(codes))
        built.append(codes.count(50) / len(codes))
        bare.append(codes.count(60) / len(codes))
    result["worldcover_dominant"] = dominant
    result["worldcover_coverage"] = coverage
    result["vegetation_fraction"] = vegetation
    result["built_fraction"] = built
    result["bare_fraction"] = bare
    return result, errors


def assign_context(frame: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    near = config["context"]["industrial_near_km"]
    contextual = config["context"]["industrial_context_km"]
    vegetation_threshold = config["context"]["vegetation_fraction"]

    def classify(row: pd.Series) -> str:
        distance = row.get("facility_distance_km", np.nan)
        raster_valid = row.get("worldcover_coverage", 0) >= 0.8
        vegetation = row.get("vegetation_fraction", np.nan) if raster_valid else np.nan
        developed = (row.get("built_fraction", 0) or 0) + (row.get("bare_fraction", 0) or 0)
        if not raster_valid:
            developed = 0
        if (pd.notna(distance) and distance <= contextual
                and pd.notna(vegetation) and vegetation >= vegetation_threshold):
            return "mixed"
        if pd.notna(distance) and distance <= near and developed >= 0.1:
            return "industrial_associated"
        if pd.notna(vegetation) and vegetation >= vegetation_threshold and (pd.isna(distance) or distance > contextual):
            return "vegetation_associated"
        if (pd.notna(distance) and distance <= contextual) or (pd.notna(vegetation) and vegetation >= 0.4):
            return "mixed"
        return "unknown"

    result = frame.copy()
    result["context_label"] = result.apply(classify, axis=1)
    result["context_conflict"] = (
        result["context_label"].eq("mixed")
        & result["facility_distance_km"].le(contextual)
        & result["vegetation_fraction"].ge(vegetation_threshold)
    )
    result["landcover_reference_year"] = 2021
    result["context_time_warning"] = "Current OSM + fixed 2021 land cover; not a contemporaneous land-use survey."
    return result


def robust_scale(history: pd.Series) -> tuple[float, float]:
    if history.empty:
        return math.nan, math.nan
    median = float(history.median())
    mad = float((history - median).abs().median())
    scale = 1.4826 * mad
    if not math.isfinite(scale) or scale < 1e-9:
        scale = max(float(history.std(ddof=0)), abs(median) * 0.05, 1e-6)
    return median, scale


def score_anomalies(events: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    settings = config["models"]
    minimum_history = settings["min_history_events"]
    isolation_minimum = settings["isolation_min_training_events"]
    minimum_calibration = settings.get("min_calibration_events", 1)
    result = events.copy().sort_values("start_time").reset_index(drop=True)
    # A full rescore owns its diagnostics, including when peer fallback is off.
    result = result.drop(columns=[c for c in result if c.startswith("peer_") or c == "score_method"])
    for column in ("model_sensor", "model_daynight"):
        if column not in result:
            result[column] = "unspecified"
    if "end_time" not in result:
        result["end_time"] = result["start_time"]
    fit_eligible = result["data_quality"].eq("science_quality") if "data_quality" in result else pd.Series(True, index=result.index)
    result["fit_eligible"] = fit_eligible
    z_columns = [f"past_z_{feature}" for feature in DYNAMIC_FEATURES]
    for column in z_columns:
        result[column] = np.nan
    result["days_since_previous"] = np.nan
    result["history_event_count"] = 0
    result["model_feature_count"] = 0
    result["robust_anomaly_raw"] = np.nan
    result["baseline_mode"] = "insufficient_history"
    result["baseline_start"] = None
    result["baseline_end"] = None
    strata = ["model_sensor", "model_daynight"]
    for _, indices in result[result["site_id"] != ""].groupby(["site_id"] + strata).groups.items():
        ordered = sorted(indices, key=lambda index: result.at[index, "start_time"])
        for position, index in enumerate(ordered):
            current_time = result.at[index, "start_time"]
            history = result.loc[ordered[:position]]
            history = history[(history["end_time"] < current_time) & history["fit_eligible"]]
            if settings.get("history_window_days"):
                history = history[history["start_time"] >= current_time - pd.Timedelta(days=settings["history_window_days"])]
            result.at[index, "history_event_count"] = len(history)
            if len(history):
                previous = history["start_time"].max()
                result.at[index, "days_since_previous"] = (
                    current_time - previous
                ).total_seconds() / 86400
            if len(history) < minimum_history:
                continue
            mode = "rolling_same_sensor_and_daynight"
            seasonal_window = settings.get("seasonal_window_days", 0)
            if seasonal_window:
                doy_delta = (history["start_time"].dt.dayofyear - current_time.dayofyear).abs()
                seasonal = history[np.minimum(doy_delta, 366 - doy_delta) <= seasonal_window]
                if len(seasonal) >= settings.get("seasonal_min_events", 12):
                    history = seasonal
                    mode = "seasonal_same_sensor_and_daynight"
            result.at[index, "history_event_count"] = len(history)
            result.at[index, "baseline_mode"] = mode
            result.at[index, "baseline_start"] = history["start_time"].min().isoformat()
            result.at[index, "baseline_end"] = history["end_time"].max().isoformat()
            positive_z: list[float] = []
            for feature, column in zip(DYNAMIC_FEATURES, z_columns):
                history_values = history[feature].dropna()
                current = result.at[index, feature]
                if len(history_values) < max(3, minimum_history // 2) or pd.isna(current):
                    continue
                median, scale = robust_scale(history_values)
                z = (current - median) / scale
                result.at[index, column] = z
                positive_z.append(max(float(z), 0.0))
            result.at[index, "model_feature_count"] = len(positive_z)
            if len(positive_z) >= 3:
                result.at[index, "robust_anomaly_raw"] = float(np.sqrt(np.mean(np.square(positive_z))))

    valid = result["robust_anomaly_raw"].notna() & result["model_daynight"].ne("unknown")
    result["robust_anomaly_percentile"] = np.nan
    result["score_day"] = result["start_time"].dt.floor("D")
    result["calibration_event_count"] = 0
    result["isolation_training_events"] = 0
    result["isolation_training_end"] = None
    result["isolation_anomaly_raw"] = np.nan
    result["isolation_anomaly_percentile"] = np.nan
    model_features = result[z_columns].clip(-20, 20)
    model_features = pd.concat(
        [model_features.fillna(0.0), model_features.isna().astype(float).add_suffix("_missing")],
        axis=1,
    )
    # Monthly expanding/rolling refits avoid thousands of almost-identical fits.
    # Every baseline, fit and percentile reference still precedes the scored day.
    for _, group_indices in result[valid].groupby(strata).groups.items():
        member = result.index.isin(group_indices)
        model, fitted_at, reference_if, training_indices = None, None, None, None
        for day in sorted(result.loc[group_indices, "score_day"].unique()):
            train = valid & member & fit_eligible & (result["end_time"] < day)
            if settings.get("history_window_days"):
                train &= result["start_time"] >= day - pd.Timedelta(days=settings["history_window_days"])
            test = valid & member & (result["score_day"] == day)
            reference = result.loc[train, "robust_anomaly_raw"].to_numpy()
            result.loc[test, "calibration_event_count"] = len(reference)
            if len(reference) < minimum_calibration:
                continue
            sorted_reference = np.sort(reference)
            # Midranks keep an unchanged, fully tied baseline at 0.5.
            result.loc[test, "robust_anomaly_percentile"] = (
                np.searchsorted(sorted_reference, result.loc[test, "robust_anomaly_raw"], side="left")
                + np.searchsorted(sorted_reference, result.loc[test, "robust_anomaly_raw"], side="right") + 1
            ) / (2 * (len(reference) + 1))
            if train.sum() < isolation_minimum:
                continue
            if model is None or (day - fitted_at).days >= settings.get("isolation_refit_days", 30):
                training_indices = result.index[train][-settings.get("isolation_max_training_events", 20000):]
                model = IsolationForest(n_estimators=settings["isolation_trees"], contamination="auto",
                                        random_state=settings["random_state"], n_jobs=-1)
                model.fit(model_features.loc[training_indices])
                reference_if = np.sort(-model.score_samples(model_features.loc[training_indices]))
                fitted_at = day
            raw = -model.score_samples(model_features.loc[test])
            result.loc[test, "isolation_anomaly_raw"] = raw
            result.loc[test, "isolation_anomaly_percentile"] = (
                np.searchsorted(reference_if, raw, side="left")
                + np.searchsorted(reference_if, raw, side="right") + 1
            ) / (2 * (len(reference_if) + 1))
            result.loc[test, "isolation_training_events"] = len(training_indices)
            result.loc[test, "isolation_training_end"] = result.loc[training_indices, "end_time"].max().isoformat()
    result["anomaly_score"] = result[
        ["robust_anomaly_percentile", "isolation_anomaly_percentile"]
    ].mean(axis=1, skipna=True)
    result.loc[~valid, "anomaly_score"] = np.nan
    result["history_sufficient"] = result["anomaly_score"].notna()
    result["review_priority"] = np.select(
        [result["anomaly_score"].isna(), result["anomaly_score"].ge(.99),
         result["anomaly_score"].ge(.95), result["anomaly_score"].ge(.8)],
        ["unscored", "very_high", "high", "moderate"], default="lower"
    )
    # Full rescoring replaces site provenance; never reuse a previous peer run.
    result["site_anomaly_score"] = result["anomaly_score"]
    return score_vegetation_peers(result.drop(columns="score_day"), config)


def acquire_sentinel(events, config, cache_dir, output_dir, refresh):
    from .sentinel_evidence import acquire_sentinel as acquire
    return acquire(events, config, cache_dir, output_dir, refresh)


def reuse_sentinel_evidence(events, evidence, previous):
    """Retain completed evidence only for unchanged observations; never relabel it."""
    result = evidence.copy().set_index("event_id")
    result["sentinel_reused"] = False
    if previous.empty or "sentinel_status" not in previous:
        return result.reset_index()
    old = previous.set_index("event_id")
    current = events.set_index("event_id")
    shared = current.index.intersection(old.index).intersection(result.index)
    valid = old.loc[shared, "sentinel_status"].eq("analysed")
    for column in ("start_time", "end_time"):
        valid &= pd.to_datetime(current.loc[shared, column], utc=True).eq(pd.to_datetime(old.loc[shared, column], utc=True))
    for column in ("latitude", "longitude"):
        valid &= (current.loc[shared, column] - old.loc[shared, column]).abs().lt(1e-9)
    matched = shared[valid]
    if len(matched):
        for column in old:
            if column.startswith("sentinel_") and column != "sentinel_reused":
                if column not in result:
                    result[column] = None
                result[column] = result[column].astype(object)
                result.loc[matched, column] = old.loc[matched, column]
        result.loc[matched, "sentinel_reused"] = True
    return result.reset_index()




def score_availability(row: pd.Series, config: dict[str, Any]) -> tuple[str, str | None]:
    """Explain the first blocking scoring gate without inventing a score."""
    score = row.get("anomaly_score", np.nan)
    if pd.notna(score):
        if math.isfinite(float(score)) and 0 <= float(score) <= 1:
            return "scored", None
        return "invalid_score", "The stored score is outside its valid range; it must be checked, not interpreted as fire risk."
    peer_status = row.get("peer_status")
    if row.get("context_label") == "vegetation_associated" and pd.notna(peer_status) and peer_status not in ("not_applicable", "scored"):
        return str(peer_status), row.get("peer_reason") or "A suitable historical vegetation peer comparison is unavailable."
    if "site_id" in row and (pd.isna(row["site_id"]) or not str(row["site_id"]).strip()):
        return "no_recurrent_site", "No recurrent thermal-source site is linked to this episode, so a same-site anomaly baseline is unavailable."
    settings = config["models"]
    history_min = settings.get("min_history_events", 6)
    calibration_min = settings.get("min_calibration_events", 1)
    history = row.get("history_event_count", np.nan)
    if pd.notna(history) and history < history_min:
        return "insufficient_site_history", (
            f"Only {int(history)} prior comparable episodes; at least {history_min} are required "
            "at this site with the same sensor and day/night phase. Only earlier completed science-quality episodes count."
        )
    features = row.get("model_feature_count", np.nan)
    if pd.notna(features) and features < 3:
        return "insufficient_features", f"Only {int(features)} usable feature comparisons; at least 3 are required."
    if row.get("model_daynight") == "unknown":
        return "unknown_daynight", "The detection's day/night phase is unknown, so a comparable calibration group cannot be selected."
    calibration = row.get("calibration_event_count", np.nan)
    if pd.notna(calibration) and calibration < calibration_min:
        return "insufficient_calibration", (
            f"Only {int(calibration)} earlier calibration episodes; at least {calibration_min} are required "
            "in the same sensor and day/night group. A site baseline alone is not a calibrated anomaly ranking."
        )
    return "diagnostics_unavailable", "No anomaly score was recorded and the available diagnostics do not establish why. This is not evidence of safety."


def consensus(events: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    threshold = config["models"]["anomaly_percentile"]
    result = events.copy()
    # Sentinel-2 is asynchronous corroborating evidence. It must not alter the
    # primary score until an independent audit validates a fusion rule.
    result["evidence_score"] = result["anomaly_score"].clip(0, 1)
    availability = [score_availability(row, config) for _, row in result.iterrows()]
    result[["score_status", "score_reason"]] = pd.DataFrame(
        availability, index=result.index, columns=["score_status", "score_reason"]
    )
    mapped_kind = result.get("nearest_facility_kind", pd.Series("", index=result.index))
    quarry = mapped_kind.fillna("").astype(str).str.strip().str.lower().eq("landuse=quarry")
    # Preserve the original broad attribution. Subtype only explicit mapped
    # quarry evidence; bare land and facility-name keywords are not sufficient.
    result["source_context"] = np.where(
        result["context_label"].eq("industrial_associated") & quarry,
        "mining_quarry", result["context_label"],
    )

    def decide(row: pd.Series) -> str:
        if row["context_label"] == "industrial_associated":
            mining = row["source_context"] == "mining_quarry"
            if row["score_status"] != "scored":
                return "mining_associated_unscored" if mining else "industrial_associated_unscored"
            if row["evidence_score"] >= threshold:
                return ("suspected_abnormal_mining_associated_event" if mining
                        else "suspected_abnormal_industrial_associated_event")
            if mining:
                return "mining_associated_thermal_source"
            return "routine_persistent_industrial_thermal_source"
        if row["context_label"] == "vegetation_associated":
            return "likely_vegetation_fire"
        return "mixed_or_unknown"

    result["decision"] = result.apply(decide, axis=1)
    if "incident_verification" not in result:
        result["incident_verification"] = "unverified"
    result["score_interpretation"] = "Relative review priority, not a probability of fire or industrial damage."
    result["decision_basis"] = result.apply(
        lambda row: (
            f"context={row['source_context']}; "
            + (f"comparison=vegetation_peers; references={row.get('peer_reference_count', 'unrecorded')}; "
               if row.get("score_method") == "vegetation_peer" else f"history_events={row.get('history_event_count', 'unrecorded')}; ")
            +
            f"evidence_score={row['evidence_score']:.3f}; sentinel={row.get('sentinel_evidence', 'unavailable')}"
            if row["score_status"] == "scored"
            else (
                f"context={row['source_context']}; anomaly score unavailable; "
                f"reason={row['score_status']}; {row['score_reason']}"
            )
        ),
        axis=1,
    )
    return result


def interpretation_summary(events: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    cutoff = config["models"]["anomaly_percentile"]
    review = events["score_status"].eq("scored") & events["anomaly_score"].ge(cutoff)
    return {
        "schema_version": 2,
        "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "score_status_counts": events["score_status"].value_counts().to_dict(),
        "score_method_counts": events.get("score_method", pd.Series(dtype=str)).value_counts().to_dict(),
        "source_context_counts": events["source_context"].value_counts().to_dict(),
        "review_cutoff": cutoff,
        "review_context_counts": events.loc[review, "source_context"].value_counts().to_dict(),
        "note": "Mapped quarry context is separated from other industrial context. Missing scores do not erase context; no fire confirmation is implied.",
    }


def frame_to_geojson(frame: pd.DataFrame, id_column: str) -> dict[str, Any]:
    features = []
    for _, row in frame.iterrows():
        properties = {
            key: json_value(value)
            for key, value in row.items()
            if key not in {"latitude", "longitude"} and not key.startswith("_")
        }
        features.append({
            "type": "Feature",
            "id": str(row[id_column]),
            "geometry": {"type": "Point", "coordinates": [row["longitude"], row["latitude"]]},
            "properties": properties,
        })
    return {"type": "FeatureCollection", "features": features}


def write_offline_viewer(output_dir, events_geojson, facilities_geojson, config):
    from .viewer import write_offline_viewer as render
    render(output_dir, events_geojson, facilities_geojson, config)




def write_outputs(
    output_dir: Path,
    detections: pd.DataFrame,
    events: pd.DataFrame,
    sites: pd.DataFrame,
    facilities: pd.DataFrame,
    config: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in (("detections", detections), ("events", events), ("sites", sites), ("facilities", facilities)):
        frame.to_csv(output_dir / f"{name}.csv", index=False)
    detections_geojson = frame_to_geojson(detections, "detection_id")
    events_geojson = frame_to_geojson(events, "event_id")
    sites_geojson = frame_to_geojson(sites, "site_id")
    facilities_geojson = frame_to_geojson(facilities, "facility_id")
    for name, payload in (
        ("detections", detections_geojson), ("events", events_geojson),
        ("sites", sites_geojson), ("facilities", facilities_geojson),
    ):
        write_json(output_dir / f"{name}.geojson", payload)
    write_offline_viewer(output_dir, events_geojson, facilities_geojson, config)


def tune_hyperparameters(root: Path, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    detections, audit = ingest_firms(root, config)
    if "data_quality" in detections:
        detections = detections[detections["data_quality"].eq("science_quality")].copy()
    rng = np.random.default_rng(config["models"]["random_state"])
    repeats = max(1, int(config["models"].get("tuning_repeats", 3)))
    subsample_ids = [set(
        rng.choice(detections["detection_id"], size=min(len(detections), max(2, int(len(detections) * 0.8))), replace=False)
    ) for _ in range(repeats)]
    event_grid = ParameterGrid({
        "event_spatial_km": [0.75, 1.25, 2.0],
        "event_temporal_hours": [12, 24, 36],
    })
    event_trials = []
    for parameters in event_grid:
        candidate = json.loads(json.dumps(config))
        candidate["models"].update(parameters)
        full_detections, full_events = cluster_events(detections, candidate)
        ari_runs = []
        for sample_ids in subsample_ids:
            subset_input = detections[detections["detection_id"].isin(sample_ids)].copy()
            subset_detections, _ = cluster_events(subset_input, candidate)
            full_labels = full_detections.set_index("detection_id").loc[subset_detections["detection_id"], "event_id"]
            ari_runs.append(float(adjusted_rand_score(full_labels, subset_detections["event_id"])))
        stability = float(np.mean(ari_runs))
        singleton_fraction = float(full_events["detection_count"].eq(1).mean())
        spatial_compliance = float(
            full_events["spread_km"].le(parameters["event_spatial_km"] * 2).mean()
        )
        temporal_compliance = float(
            full_events["duration_hours"].le(parameters["event_temporal_hours"] + 1e-9).mean()
        )
        objective = (
            0.60 * stability
            + 0.20 * spatial_compliance
            + 0.20 * (1 - singleton_fraction)
        )
        event_trials.append({
            **parameters,
            "events": len(full_events),
            "subsample_ari": stability,
            "subsample_ari_runs": ari_runs,
            "subsample_ari_min": min(ari_runs), "subsample_ari_max": max(ari_runs),
            "subsample_ari_std": float(np.std(ari_runs)),
            "singleton_event_fraction": singleton_fraction,
            "spatial_compliance": spatial_compliance,
            "temporal_compliance": temporal_compliance,
            "selection_objective": objective,
        })
    viable_events = [trial for trial in event_trials if trial["temporal_compliance"] == 1.0]
    selected_event = max(viable_events, key=lambda trial: trial["selection_objective"])
    selected_config = json.loads(json.dumps(config))
    selected_config["models"].update({
        "event_spatial_km": selected_event["event_spatial_km"],
        "event_temporal_hours": selected_event["event_temporal_hours"],
    })
    _, selected_events = cluster_events(detections, selected_config)

    subsample_event_ids = [set(
        rng.choice(selected_events["event_id"], size=min(len(selected_events), max(2, int(len(selected_events) * 0.8))), replace=False)
    ) for _ in range(repeats)]
    site_trials = []
    for diameter in (0.75, 1.0, 1.5, 2.0):
        candidate = json.loads(json.dumps(selected_config))
        candidate["models"]["site_max_diameter_km"] = diameter
        full_events, full_sites = discover_sites(selected_events, candidate)
        ari_runs = []
        for sample_ids in subsample_event_ids:
            subset_input = selected_events[selected_events["event_id"].isin(sample_ids)].copy()
            subset_events, _ = discover_sites(subset_input, candidate)
            full_site_labels = full_events.set_index("event_id").loc[subset_events["event_id"], "site_id"]
            full_site_labels = [label or event_id for label, event_id in zip(full_site_labels, subset_events["event_id"])]
            subset_site_labels = [label or event_id for label, event_id in zip(subset_events["site_id"], subset_events["event_id"])]
            ari_runs.append(float(adjusted_rand_score(full_site_labels, subset_site_labels)))
        stability = float(np.mean(ari_runs))
        coverage = float(full_events["site_id"].ne("").mean())
        maximum_diameter = float(full_sites["diameter_km"].max()) if len(full_sites) else 0.0
        objective = 0.75 * stability + 0.25 * coverage - 0.03 * (diameter / 2.0)
        site_trials.append({
            "site_max_diameter_km": diameter,
            "sites": len(full_sites),
            "subsample_ari": stability,
            "subsample_ari_runs": ari_runs,
            "subsample_ari_min": min(ari_runs), "subsample_ari_max": max(ari_runs),
            "subsample_ari_std": float(np.std(ari_runs)),
            "event_coverage": coverage,
            "observed_max_diameter_km": maximum_diameter,
            "selection_objective": objective,
        })
    selected_site = max(site_trials, key=lambda trial: trial["selection_objective"])

    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "input_detections": len(detections), "resampling_repeats": repeats,
        "input_id_sha256": hashlib.sha256("|".join(sorted(detections["detection_id"])).encode()).hexdigest(),
        "production_parameters": {key: config["models"][key] for key in
                                  ("event_spatial_km", "event_temporal_hours", "site_max_diameter_km")},
        "status": "provisional" if audit["missing_expected_years"] else "eligible_for_temporal_validation",
        "reason": (
            f"Missing expected years {audit['missing_expected_years']}; do not call this globally optimal."
            if audit["missing_expected_years"]
            else "All expected years are present; still validate on forward-held-out time blocks."
        ),
        "initialization": {
            "DBSCAN": "deterministic; no centroid initialization",
            "complete_link_sites": "deterministic; no centroid initialization",
            "IsolationForest": "fixed production seed; no multi-seed sensitivity claim",
        },
        "event_trials": event_trials,
        "site_trials": site_trials,
        "provisional_parameters": {
            "event_spatial_km": selected_event["event_spatial_km"],
            "event_temporal_hours": selected_event["event_temporal_hours"],
            "site_max_diameter_km": selected_site["site_max_diameter_km"],
        },
        "selection_policy": (
            "Mean ARI across the requested independent 80% subsamples is combined with physical-compliance and coverage terms. "
            "Only science-quality inputs are used when quality metadata is present. No FIRMS type, event label, or final audit label is used. "
            "This report recommends parameters; it does not overwrite the production configuration."
        ),
    }
    output_dir = root / config["paths"]["output"]
    write_json(output_dir / "tuning_report.json", report)
    return report


def pipeline(root: Path, config_path: Path, refresh: bool, skip_sentinel: bool) -> dict[str, Any]:
    config = load_config(config_path)
    cache_dir, output_dir = root / config["paths"]["cache"], root / config["paths"]["output"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    began, previous = time.perf_counter(), time.perf_counter()
    timings = {}

    def checkpoint(name):
        nonlocal previous
        now = time.perf_counter()
        timings[name] = round(now - previous, 3)
        previous = now
        print(f"{name}: {timings[name]:.1f}s", flush=True)

    print("Reading and auditing local FIRMS sources...", flush=True)
    detections, audit = ingest_firms(root, config)
    write_json(output_dir / "data_audit.json", audit)
    checkpoint(f"ingestion ({len(detections)} detections)")
    detections, events = cluster_events(detections, config)
    checkpoint(f"event clustering ({len(events)} events)")
    events, sites = discover_sites(events, config)
    checkpoint(f"site discovery ({len(sites)} sites)")
    facilities = fetch_osm_facilities(config, cache_dir, refresh)
    events, sites = nearest_facility(events, facilities), nearest_facility(sites, facilities)
    events, event_worldcover_errors = add_worldcover(events, config, cache_dir)
    sites, site_worldcover_errors = add_worldcover(sites, config, cache_dir)
    events, sites = assign_context(events, config), assign_context(sites, config)
    checkpoint("facility and land-cover context")
    events = score_anomalies(events, config)
    checkpoint("past-only science-quality model scoring")

    evidence_config = json.loads(json.dumps(config))
    if skip_sentinel:
        evidence_config["sentinel"]["enabled"] = False
    evidence, sentinel_errors = acquire_sentinel(events, evidence_config, cache_dir, output_dir, refresh)
    if skip_sentinel and (output_dir / "events.csv").exists():
        previous_events = pd.read_csv(output_dir / "events.csv", low_memory=False, float_precision="round_trip")
        evidence = reuse_sentinel_evidence(events, evidence, previous_events)
    events = consensus(events.merge(evidence, how="left", on="event_id"), config)
    checkpoint("Sentinel corroboration" if not skip_sentinel else "explicit deferred Sentinel statuses")
    now = pd.Timestamp.now(tz="UTC")
    events["provisional_episode"] = events["end_time"] > now - pd.Timedelta(hours=config["models"]["event_temporal_hours"])
    detections = detections.merge(events[["event_id", "site_id", "decision"]], on="event_id", how="left")
    nrt_files = [item for item in audit["input_files"] if "/nrt/" in item["file"].replace("\\", "/")]
    live_latest = max((item["source_latest_acquisition_utc"] for item in nrt_files
                       if item.get("source_latest_acquisition_utc")), default=None)
    live_earliest = min((item["source_earliest_acquisition_utc"] for item in nrt_files
                         if item.get("source_earliest_acquisition_utc")), default=None)
    sync_path = cache_dir / "source_sync.json"
    sync = json.loads(sync_path.read_text(encoding="utf-8")) if sync_path.exists() else {}
    missing = audit["missing_expected_years"]
    manifest = {
        "project": config["project"], "aoi": config["aoi"], "generated_at": now.isoformat(),
        "input_audit": audit,
        "outputs": {
            "detections": len(detections), "events": len(events), "persistent_sites": len(sites),
            "osm_context_features": len(facilities),
            "scored_events": int(events["anomaly_score"].notna().sum()),
            "decision_counts": events["decision"].value_counts().to_dict(),
            "sentinel_event_pairs": int(events["sentinel_post_item"].notna().sum()),
            "sentinel_status_counts": events["sentinel_status"].value_counts().to_dict(),
        },
        "live_sync": {
            "status": "fresh" if live_latest and (now - pd.Timestamp(live_latest)).total_seconds() < 86400 else "stale_or_unavailable",
            "latest_acquisition": live_latest, "earliest_acquisition": live_earliest,
            "downloaded_nrt_files": len(nrt_files), "requested_rolling_days": 7,
            "source_failures": [item for item in sync.get("files", []) if item["status"] == "unavailable"],
            "note": "These are downloaded satellite observations, not continuous real-time surveillance. Latest filters use the actual current clock.",
        },
        "models": {
            "events": "DBSCAN on scaled local x/y/time; singleton detections retained; hard episode-duration split",
            "sites": "retrospective complete-link groups with maximum planar diameter; discovered from all available observations",
            "radiometry": "one deterministic platform and day/night phase per event; other observations retained as context",
            "temporal": "completed-past, science-quality, same-platform/phase site median/MAD; seasonal subset when sufficient",
            "multivariate": "past-only science-quality Isolation Forest, refit every configured 30 days; no fire or normal labels",
            "features": list(DYNAMIC_FEATURES), "parameters": config["models"],
            "semantic_attribution": "OSM + WorldCover evidence rules; conflicting industrial/vegetation evidence remains mixed",
        },
        "external_data": {
            "osm_cache_files": [str(path) for path in sorted(cache_dir.glob("osm_industrial_context_*.json"))],
            "worldcover": "ESA WorldCover 2021 v200, fixed 10m context raster; not seven annual land-cover maps",
            "sentinel": "Sentinel-2 L2A local cloud-masked pre/post NIR/SWIR comparisons; exact acquisition dates retained",
            "worldcover_errors": event_worldcover_errors + site_worldcover_errors,
            "sentinel_errors": sentinel_errors,
            "sentinel_reused_events": int(events.get("sentinel_reused", pd.Series(dtype=bool)).fillna(False).sum()),
            "sentinel_reuse_note": "When acquisition is skipped, completed evidence is preserved only for identical event IDs, coordinates and observation dates. Original radiometry warnings remain.",
        },
        "limitations": ([f"Missing requested science-quality years: {missing}."] if missing else []) + [
            "A bounding-box pilot is used, not administrative district polygons.",
            "Historical site discovery uses the entire available period; historical scores are exploratory, not a fully prospective validation.",
            "Current OSM and fixed 2021 WorldCover can differ from land use at the event date. OSM coverage is incomplete.",
            "FIRMS positive-only CSVs do not contain cloud-free non-detection opportunities; recurrence is detection-conditioned.",
            "A high anomaly ranking is not a calibrated fire probability; a low or missing score cannot rule out fire.",
            "NRT records are provisional and excluded from all fitting/calibration histories; archival replacement can revise detections.",
            "Sentinel surface changes can have non-fire causes; no detected surface change does not rule out a short or small fire.",
            "The original 99-alert audit is incomplete (zero confirmed industrial asset fires); no precision, recall or accuracy is claimed.",
            "Annual science archives and a rolling live feed do not form a continuous current-year record when intervening months are absent.",
        ],
        "stage_seconds": timings,
    }
    manifest["interpretation"] = interpretation_summary(events, config)
    if config.get("vegetation_peers", {}).get("enabled"):
        manifest["vegetation_scoring"] = vegetation_summary(events, config)
        write_json(output_dir / "vegetation_scoring.json", manifest["vegetation_scoring"])
    config["_runtime_metadata"] = manifest
    write_outputs(output_dir, detections, events, sites, facilities, config)
    events[events["anomaly_score"].notna()].sort_values("anomaly_score", ascending=False).to_csv(
        output_dir / "anomaly_rankings.csv", index=False)
    write_json(output_dir / "sentinel_summary.json", {"status_counts": manifest["outputs"]["sentinel_status_counts"],
                                                      "errors": sentinel_errors})
    checkpoint("exports and interactive offline map")
    manifest["total_seconds"] = round(time.perf_counter() - began, 3)
    write_json(output_dir / "run_manifest.json", manifest)
    return manifest


def corroborate_existing(root: Path, config_path: Path, refresh=False):
    """Enrich exported events without recomputing clustering and model fits."""
    config = load_config(config_path)
    output, cache = root / config["paths"]["output"], root / config["paths"]["cache"]
    events = pd.read_csv(output / "events.csv", low_memory=False)
    events["start_time"] = pd.to_datetime(events["start_time"], utc=True)
    events["end_time"] = pd.to_datetime(events["end_time"], utc=True)
    events = events.drop(columns=[column for column in events if column.startswith("sentinel_")])
    evidence, errors = acquire_sentinel(events, config, cache, output, refresh)
    events = consensus(events.merge(evidence, on="event_id", how="left"), config)
    events.to_csv(output / "events.csv", index=False)
    payload = frame_to_geojson(events, "event_id")
    write_json(output / "events.geojson", payload)
    events[events["anomaly_score"].notna()].sort_values("anomaly_score", ascending=False).to_csv(
        output / "anomaly_rankings.csv", index=False)
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    manifest["corroborated_at"] = pd.Timestamp.now(tz="UTC").isoformat()
    manifest["outputs"]["sentinel_event_pairs"] = int(events["sentinel_post_item"].notna().sum())
    manifest["outputs"]["sentinel_status_counts"] = events["sentinel_status"].value_counts().to_dict()
    manifest["outputs"]["decision_counts"] = events["decision"].value_counts().to_dict()
    manifest["interpretation"] = interpretation_summary(events, config)
    manifest["external_data"]["sentinel_errors"] = errors
    config["_runtime_metadata"] = manifest
    write_offline_viewer(output, payload, json.loads((output / "facilities.geojson").read_text(encoding="utf-8")), config)
    write_json(output / "run_manifest.json", manifest)
    write_json(output / "sentinel_summary.json", {"status_counts": manifest["outputs"]["sentinel_status_counts"],
                                                  "errors": errors})
    return manifest


def reinterpret_existing(root: Path, config_path: Path) -> dict[str, Any]:
    """Refresh only interpretation fields, with a backup and rollback on failure."""
    config = load_config(config_path)
    output = root / config["paths"]["output"]
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("models", {}).get("parameters") != config["models"]:
        raise ValueError("Saved model parameters differ or are missing; cannot reinterpret scoring gates with changed requirements.")
    events = pd.read_csv(output / "events.csv", low_memory=False, float_precision="round_trip")
    payload = json.loads((output / "events.geojson").read_text(encoding="utf-8"))
    geo_ids = [feature["properties"]["event_id"] for feature in payload["features"]]
    if events["event_id"].duplicated().any() or len(set(geo_ids)) != len(geo_ids) or set(geo_ids) != set(events["event_id"]):
        raise ValueError("CSV and GeoJSON must have identical unique event IDs before interpretation refresh.")
    revised = consensus(events, config)
    fields = ["decision", "decision_basis", "source_context", "score_status", "score_reason"]
    updated = events.copy()
    updated[fields] = revised[fields]
    by_id = updated.set_index("event_id")
    for feature in payload["features"]:
        properties = feature["properties"]
        row = by_id.loc[properties["event_id"]]
        if properties.get("anomaly_score") != json_value(row["anomaly_score"]):
            raise ValueError("CSV and GeoJSON anomaly scores disagree; refusing to overwrite either source.")
        properties.update({key: json_value(row[key]) for key in fields})
    manifest["outputs"]["decision_counts"] = updated["decision"].value_counts().to_dict()
    manifest["interpretation"] = interpretation_summary(updated, config)
    backup = root / "runs" / ("before-interpretation-" + pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%S%fZ"))
    backup.mkdir(parents=True, exist_ok=False)
    filenames = ("events.csv", "events.geojson", "anomaly_rankings.csv", "run_manifest.json", "index.html")
    for name in filenames:
        if (output / name).exists():
            shutil.copy2(output / name, backup / name)
    manifest["interpretation"]["backup_directory"] = str(backup.resolve())
    config["_runtime_metadata"] = manifest
    try:
        updated.to_csv(output / "events.csv", index=False)
        updated[updated["anomaly_score"].notna()].sort_values("anomaly_score", ascending=False).to_csv(
            output / "anomaly_rankings.csv", index=False)
        write_json(output / "events.geojson", payload)
        write_json(output / "run_manifest.json", manifest)
        write_offline_viewer(output, payload,
                            json.loads((output / "facilities.geojson").read_text(encoding="utf-8")), config)
        # Serialization must preserve all scientific measurements and metadata.
        reread = pd.read_csv(output / "events.csv", low_memory=False, float_precision="round_trip")
        unchanged = [column for column in events if column not in fields]
        pd.testing.assert_frame_equal(events[unchanged], reread[unchanged])
    except BaseException:
        for name in filenames:
            if (backup / name).exists():
                shutil.copy2(backup / name, output / name)
            else:
                (output / name).unlink(missing_ok=True)
        raise
    return manifest




def vegetation_summary(events: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    vegetation = events.loc[events["context_label"].eq("vegetation_associated")]
    return {
        "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "vegetation_episodes": len(vegetation),
        "scored_vegetation_episodes": int(vegetation["anomaly_score"].notna().sum()),
        "score_method_counts": vegetation["score_method"].value_counts().to_dict(),
        "peer_status_counts": vegetation["peer_status"].value_counts().to_dict(),
        "parameters": config.get("vegetation_peers", {}),
        "note": "Detection-conditioned descriptive peer ranks, not fire probability, severity or independent validation. Sparse groups abstain. No industrial algorithm change.",
    }


def refresh_vegetation_peers(root: Path, config_path: Path) -> dict[str, Any]:
    """Refresh vegetation fallback only, preserving site scores and observations."""
    config = load_config(config_path)
    output = root / config["paths"]["output"]
    events = pd.read_csv(output / "events.csv", low_memory=False, float_precision="round_trip")
    payload = json.loads((output / "events.geojson").read_text(encoding="utf-8"))
    geo_ids = [f["properties"]["event_id"] for f in payload["features"]]
    if len(set(geo_ids)) != len(geo_ids) or set(geo_ids) != set(events["event_id"]):
        raise ValueError("CSV and GeoJSON must have identical unique event IDs.")
    # Scores must be published against the same observations used to fit them.
    original = events.set_index("event_id")
    mapped = pd.DataFrame([
        {**f["properties"], "longitude": f["geometry"]["coordinates"][0],
         "latitude": f["geometry"]["coordinates"][1]} for f in payload["features"]
    ]).set_index("event_id").reindex(original.index)
    inputs = ("latitude", "longitude", "start_time", "end_time", "context_label",
              "worldcover_dominant", "worldcover_coverage", "data_quality",
              "model_sensor", "model_daynight", "frp_peak_mw",
              "brightness_temperature_difference_max", "detection_peak")
    for column in inputs:
        if column not in original:
            continue
        if column not in mapped:
            raise ValueError(f"GeoJSON is missing scoring input {column}.")
        left, right = original[column], mapped[column]
        if column in ("start_time", "end_time"):
            left, right = pd.to_datetime(left, utc=True), pd.to_datetime(right, utc=True)
        try:
            pd.testing.assert_series_equal(left, right, check_dtype=False, check_exact=True)
        except AssertionError as exc:
            raise ValueError(f"CSV and GeoJSON disagree on scoring input {column}; refusing refresh.") from exc
    updated = score_vegetation_peers(events, config)
    revised = consensus(updated, config)
    fields = ["evidence_score", "score_status", "score_reason", "score_interpretation", "decision_basis"]
    updated[fields] = revised[fields]
    by_id, before = updated.set_index("event_id"), events.set_index("event_id")
    changed = set(updated.columns) - set(events.columns)
    changed.update(fields + ["anomaly_score", "review_priority", "score_method", "site_anomaly_score"])
    changed.update(c for c in updated if c.startswith("peer_"))
    for feature in payload["features"]:
        properties = feature["properties"]
        identifier = properties["event_id"]
        if properties.get("anomaly_score") != json_value(before.loc[identifier, "anomaly_score"]):
            raise ValueError("CSV and GeoJSON scores disagree; refusing refresh.")
        properties.update({key: json_value(by_id.loc[identifier, key]) for key in changed})
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    summary = vegetation_summary(updated, config)
    manifest["vegetation_scoring"] = summary
    manifest["interpretation"] = interpretation_summary(updated, config)
    manifest["outputs"]["scored_events"] = int(updated["anomaly_score"].notna().sum())
    backup = root / "runs" / ("before-peer-refresh-" + pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%S%fZ"))
    backup.mkdir(parents=True, exist_ok=False)
    filenames = ("events.csv", "events.geojson", "anomaly_rankings.csv", "run_manifest.json", "vegetation_scoring.json", "index.html")
    for name in filenames:
        if (output / name).exists():
            shutil.copy2(output / name, backup / name)
    config["_runtime_metadata"] = manifest
    try:
        updated.to_csv(output / "events.csv", index=False)
        updated[updated["anomaly_score"].notna()].sort_values("anomaly_score", ascending=False).to_csv(output / "anomaly_rankings.csv", index=False)
        write_json(output / "events.geojson", payload)
        write_json(output / "run_manifest.json", manifest)
        write_json(output / "vegetation_scoring.json", summary)
        write_offline_viewer(output, payload, json.loads((output / "facilities.geojson").read_text(encoding="utf-8")), config)
        reread = pd.read_csv(output / "events.csv", low_memory=False, float_precision="round_trip")
        unchanged = [column for column in events if column not in changed]
        pd.testing.assert_frame_equal(events[unchanged], reread[unchanged])
    except BaseException:
        for name in filenames:
            if (backup / name).exists():
                shutil.copy2(backup / name, output / name)
            else:
                (output / name).unlink(missing_ok=True)
        raise
    return manifest


def self_check() -> None:
    rows = []
    start = pd.Timestamp("2024-01-01T00:00:00Z")
    for index in range(8):
        rows.append({
            "event_id": f"E{index}", "site_id": "S1", "start_time": start + timedelta(days=index),
            "end_time": start + timedelta(days=index, hours=1), "latitude": 24.0,
            "longitude": 82.0, "frp_peak_mw": 10.0, "frp_median_mw": 5.0,
            "detection_peak": 2, "duration_hours": 1.0, "spread_km": 0.2,
            "brightness_temperature_difference_max": np.nan,
        })
    rows[-1].update(frp_peak_mw=100.0, frp_median_mw=50.0, detection_peak=20)
    config = {"models": {
        "min_history_events": 6, "isolation_min_training_events": 32,
        "isolation_trees": 10, "random_state": 26162,
    }}
    scored = score_anomalies(pd.DataFrame(rows), config)
    assert not scored.loc[5, "history_sufficient"]
    assert scored.loc[7, "history_sufficient"]
    assert scored.loc[7, "robust_anomaly_raw"] > scored.loc[6, "robust_anomaly_raw"]
    detections = pd.DataFrame({
        "detection_id": [f"D{i}" for i in range(10)],
        "acquired_at": [start + timedelta(hours=24 * i) for i in range(10)],
        "latitude": [24.0] * 10, "longitude": [82.0] * 10,
        "sensor": ["VIIRS_SNPP"] * 10, "frp": [5.0] * 10,
        "brightness": [330.0] * 10,
        "brightness_temperature_difference": [20.0] * 10,
        "daynight": ["D"] * 10, "confidence_score": [0.6] * 10,
        "firms_type_reference": [0] * 10,
    })
    event_config = {"models": {
        "event_spatial_km": 1.25, "event_temporal_hours": 36,
        "event_min_samples": 2,
    }}
    _, episodes = cluster_events(detections, event_config)
    assert episodes["duration_hours"].max() <= 36
    assert worldcover_tile(24.1, 82.1) == "N24E081"
    print("self-check passed")


def main() -> int:
    parser = argparse.ArgumentParser(description="SIH26162 label-free thermal-event pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run acquisition, modeling, and GIS export")
    run.add_argument("--config", type=Path, default=Path("config.json"))
    run.add_argument("--refresh", action="store_true", help="refresh cached external catalogue/context data")
    run.add_argument("--skip-sentinel", action="store_true", help="run core pipeline without Sentinel crops")
    for command, help_text in (
        ("sync", "download the current seven-day feed and rebuild the offline product"),
        ("corroborate", "add Sentinel evidence to existing exported events without rerunning models"),
        ("interpret", "refresh context categories and missing-score reasons without retraining or downloads"),
        ("vegetation", "refresh vegetation peer scores without changing site models or Sentinel evidence"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("--config", type=Path, default=Path("config.json"))
        command_parser.add_argument("--refresh", action="store_true")
        command_parser.add_argument("--skip-sentinel", action="store_true")
    tune = subparsers.add_parser("tune", help="run label-free clustering stability search")
    tune.add_argument("--config", type=Path, default=Path("config.json"))
    subparsers.add_parser("self-check", help="run the small deterministic algorithm check")
    args = parser.parse_args()
    if args.command == "self-check":
        self_check()
        return 0
    root = Path(__file__).resolve().parents[1]
    config_path = args.config if args.config.is_absolute() else root / args.config
    if args.command == "tune":
        report = tune_hyperparameters(root, config_path)
        print(json.dumps(report["provisional_parameters"], indent=2))
        return 0
    if args.command == "corroborate":
        manifest = corroborate_existing(root, config_path, args.refresh)
        print(json.dumps(manifest["outputs"], indent=2))
        return 0
    if args.command == "interpret":
        manifest = reinterpret_existing(root, config_path)
        print(json.dumps(manifest["interpretation"], indent=2))
        return 0
    if args.command == "vegetation":
        manifest = refresh_vegetation_peers(root, config_path)
        print(json.dumps(manifest["vegetation_scoring"], indent=2))
        return 0
    if args.command == "sync":
        data_sources.sync_firms(root, load_config(config_path), history=False, live=True)
    manifest = pipeline(root, config_path, args.refresh, args.skip_sentinel)
    print(json.dumps(manifest["outputs"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
