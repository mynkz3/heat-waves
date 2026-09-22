"""Optional, asynchronous Sentinel-2 surface-change evidence, never a fire verdict.

Radiometry follows ITEM asset raster:bands, not a date-derived correction:
https://github.com/Element84/earth-search#gainoffset-in-items-after-jan-25-2022
Collections are listed by https://earth-search.aws.element84.com/v1/collections.
SCL classes: https://custom-scripts.sentinel-hub.com/custom-scripts/sentinel-2/scene-classification/

The dNBR/SWIR thresholds below are unvalidated screening heuristics. Clear images
without lasting surface change cannot exclude a short, small, or industrial fire.
Only small raster windows are read; this module does not download whole scenes.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np
import pandas as pd
import requests
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject, transform
from rasterio.windows import Window
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


EARTH_SEARCH = "https://earth-search.aws.element84.com/v1/search"
METHOD = "sentinel-local-nbr-swir-v2"
COLLECTIONS = ["sentinel-2-c1-l2a", "sentinel-2-l2a", "sentinel-2-pre-c1-l2a"]
CLEAR_SCL = (4, 5, 6)  # Vegetation, bare/not-vegetated, water; unknown pixels excluded.
CLOUD_SCL = (3, 8, 9, 10)  # Cloud shadow, medium/high cloud, cirrus.
DEFAULTS = dict(
    enabled=True, max_events=150, days_before=30, days_after=30,
    max_scene_cloud=25, fallback_scene_cloud=90, crop_radius_m=2000,
    analysis_radius_m=500, min_local_valid_fraction=0.4, workers=3,
    collections=COLLECTIONS, max_catalog_pages=2, catalog_page_size=100,
    max_scenes_per_side=12, max_pair_attempts=16, request_timeout_seconds=30,
    dnbr_threshold=0.1, min_affected_fraction=0.1,
    swir_relative_threshold=0.2, normalized_swir_threshold=0.02,
    retry_after_hours=12, candidate_policy="industrial_highs_stratified_vegetation",
    vegetation_reserved_fraction=0.2, recent_reserved_fraction=0.2, recent_days=90,
)
SENTINEL_COLUMNS = [
    "event_id", "sentinel_status", "sentinel_reason", "sentinel_evidence",
    "sentinel_selected", "sentinel_priority_group", "sentinel_method",
    "sentinel_cache_key", "sentinel_cache_hit", "sentinel_catalog_truncated",
    "sentinel_candidate_pairs", "sentinel_pairs_attempted", "sentinel_search_as_of",
    "sentinel_pre_item", "sentinel_post_item", "sentinel_pre_collection",
    "sentinel_post_collection", "sentinel_mgrs_tile", "sentinel_nir_band",
    "sentinel_radiometry_status", "sentinel_radiometry_note",
    "sentinel_pre_boa_offset_applied", "sentinel_post_boa_offset_applied",
    "sentinel_pre_datetime", "sentinel_post_datetime", "sentinel_pre_lag_days",
    "sentinel_post_lag_days", "sentinel_pre_scene_cloud", "sentinel_post_scene_cloud",
    "sentinel_pre_valid_fraction", "sentinel_post_valid_fraction",
    "sentinel_joint_valid_fraction", "sentinel_pre_cloud_fraction",
    "sentinel_post_cloud_fraction", "sentinel_pre_saturated_fraction",
    "sentinel_post_saturated_fraction", "sentinel_pre_nodata_fraction",
    "sentinel_post_nodata_fraction", "sentinel_analysis_radius_m",
    "sentinel_grid_resolution_m", "sentinel_valid_pixels", "sentinel_pre_nbr_mean",
    "sentinel_post_nbr_mean", "sentinel_dnbr_mean", "sentinel_dnbr_median",
    "sentinel_affected_fraction", "sentinel_affected_area_m2",
    "sentinel_pre_swir12_mean", "sentinel_post_swir12_mean",
    "sentinel_swir12_relative_change", "sentinel_normalized_swir_change",
    "sentinel_pre_swir_ratio", "sentinel_post_swir_ratio", "sentinel_swir_ratio_change",
    "sentinel_swir_affected_fraction", "sentinel_adjustment", "sentinel_pre_crop",
    "sentinel_post_crop", "sentinel_preview",
]
# ponytail: serialize matplotlib rendering only; imagery I/O still uses three workers.
_PLOT_LOCK = threading.Lock()
# Single CLI process, multiple imagery threads: serialize cache publication/read.
_CACHE_IO_LOCK = threading.Lock()


def _settings(config: dict) -> dict:
    return {**DEFAULTS, **config.get("sentinel", {}),
            "catalogue_bbox": config.get("aoi", {}).get("bbox")}


def _utc(value: Any) -> pd.Timestamp:
    result = pd.to_datetime(value, utc=True)
    if pd.isna(result):
        raise ValueError("Missing event/acquisition time")
    return result


def _now(settings: dict) -> pd.Timestamp:
    now = pd.Timestamp.now(tz="UTC")
    return min(now, _utc(settings["as_of"])) if settings.get("as_of") else now


def _coordinates(event: pd.Series) -> tuple[float, float]:
    lon, lat = float(event["longitude"]), float(event["latitude"])
    if not np.isfinite([lon, lat]).all() or not (-180 <= lon <= 180 and -80 <= lat <= 84):
        raise ValueError("Event coordinates must be finite and inside Sentinel UTM coverage")
    if _utc(event["end_time"]) < _utc(event["start_time"]):
        raise ValueError("Event end precedes event start")
    return lon, lat


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _write_json(path: Path, value: dict) -> None:
    with _CACHE_IO_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)


def evidence_cache_key(event: pd.Series, config: dict) -> str:
    """Stable under event renumbering, selection limits and worker-count changes."""
    settings = _settings(config)
    ignored = {"enabled", "max_events", "workers", "selected_event_ids", "event_ids",
               "candidate_policy", "as_of", "retry_after_hours", "request_timeout_seconds",
               "vegetation_reserved_fraction", "recent_reserved_fraction", "recent_days"}
    lon, lat = _coordinates(event)
    return _digest({"method": METHOD, "longitude": lon, "latitude": lat,
                    "start": _utc(event["start_time"]).isoformat(),
                    "end": _utc(event["end_time"]).isoformat(),
                    "settings": {key: value for key, value in settings.items() if key not in ignored}})


def select_candidates(events: pd.DataFrame, config: dict) -> dict[str, str]:
    """Reserve vegetation/latest coverage, then prioritize industrial/mixed highs.

    selected_event_ids explicitly requests individual events. candidate_policy=all
    additionally queues every remaining context. A null max_events removes the cap.
    all_priority_groups removes the cap only when max_events is explicitly null.
    By default floor(20% of cap) slots are reserved for vegetation and another
    floor(20%) for the latest 90 days. Unfilled reservations return to priority use.
    """
    if events.empty:
        return {}
    settings = _settings(config)
    ranked = events.copy()
    ranked["_score"] = pd.to_numeric(ranked.get("anomaly_score", pd.Series(index=ranked.index, dtype=float)),
                                      errors="coerce").fillna(-1)
    ranked["_frp"] = pd.to_numeric(ranked.get("frp_peak_mw", pd.Series(index=ranked.index, dtype=float)),
                                    errors="coerce").fillna(0)
    ranked = ranked.sort_values(["_score", "_frp", "start_time", "event_id"],
                                ascending=[False, False, False, True], kind="stable")
    chosen_ids = settings.get("selected_event_ids", settings.get("event_ids"))
    cap = settings["max_events"]
    cap = len(events) if cap is None else max(0, int(cap))
    if chosen_ids is not None:
        wanted = {str(value) for value in chosen_ids}
        return {str(row["event_id"]): "explicit_selection" for _, row in
                ranked[ranked["event_id"].astype(str).isin(wanted)].head(cap).iterrows()}
    threshold = float(config.get("models", {}).get("anomaly_percentile", 0.95))
    groups: dict[str, list] = defaultdict(list)
    for _, row in ranked.iterrows():
        label = str(row.get("context_label", "")).lower()
        if "industrial" in label and row["_score"] >= threshold:
            groups["industrial_high"].append(row)
        elif "mixed" in label and row["_score"] >= threshold:
            groups["mixed_unusual"].append(row)
        elif "vegetation" in label or "natural" in label:
            groups["vegetation_sample"].append(row)
        else:
            groups["other_context"].append(row)
    ordered = [(row, key) for key in ("industrial_high", "mixed_unusual") for row in groups[key]]
    strata: dict[tuple, deque] = defaultdict(deque)
    for row in groups["vegetation_sample"]:
        site = row.get("site_id")
        if pd.isna(site) or not str(site).strip():
            site = (round(float(row["longitude"]), 2), round(float(row["latitude"]), 2))
        strata[(_utc(row["start_time"]).year, str(site))].append(row)
    # Rotate years AND sites so many high-scoring sites in one year cannot win
    # every vegetation slot before an older year receives its first observation.
    years: dict[int, deque] = defaultdict(deque)
    for (year, _), bucket in strata.items():
        years[year].append(bucket)
    vegetation = []
    while any(years.values()):
        for year in sorted(years):
            if years[year]:
                bucket = years[year].popleft()
                vegetation.append((bucket.popleft(), "vegetation_sample"))
                if bucket:
                    years[year].append(bucket)
    ordered.extend(vegetation)
    if settings["candidate_policy"] == "all":
        ordered.extend((row, "other_context") for row in groups["other_context"])
    chosen = {}
    vegetation_slots = int(cap * min(0.5, max(0, float(settings["vegetation_reserved_fraction"]))))
    for row, group in vegetation[:vegetation_slots]:
        chosen[str(row["event_id"])] = group
    recent_slots = int(cap * min(0.5, max(0, float(settings["recent_reserved_fraction"]))))
    cutoff = _now(settings) - pd.Timedelta(days=float(settings["recent_days"]))
    recent = [(row, "recent_event") for _, row in ranked.iterrows()
              if _utc(row["start_time"]) >= cutoff and str(row["event_id"]) not in chosen]
    recent.sort(key=lambda entry: _utc(entry[0]["start_time"]), reverse=True)
    for row, group in recent[:recent_slots]:
        chosen[str(row["event_id"])] = group
    for row, group in ordered:
        if len(chosen) >= cap:
            break
        chosen.setdefault(str(row["event_id"]), group)
    return chosen


def _request_json(method: str, url: str, body: dict | None, settings: dict) -> dict:
    """Bounded retries; no credential/key is needed for this public catalogue."""
    retries = Retry(total=2, backoff_factor=0.4, status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=frozenset(("GET", "POST")), respect_retry_after_header=False)
    with requests.Session() as session:
        session.mount("https://", HTTPAdapter(max_retries=retries))
        response = session.request(method, url, json=body if method == "POST" else None,
                                   timeout=(10, min(60, max(1, float(settings["request_timeout_seconds"])))) )
        response.raise_for_status()
        return response.json()


def _search(payload: dict, settings: dict, cache_dir: Path, refresh: bool) -> tuple[list, bool]:
    now = _now(settings)
    cache_path = cache_dir / "sentinel_catalog_v2" / f"{_digest(payload)}.json"
    if cache_path.exists() and not refresh:
        try:
            with _CACHE_IO_LOCK:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            age = (now - _utc(cached["created_at"])).total_seconds() / 3600
            if 0 <= age < float(settings["retry_after_hours"]):
                return cached["features"], cached["truncated"]
        except (OSError, ValueError, KeyError):
            pass
    features, next_link = [], None
    method, url, body = "POST", EARTH_SEARCH, payload
    for _ in range(max(1, min(10, int(settings["max_catalog_pages"])))):
        page = _request_json(method, url, body, settings)
        features.extend(page.get("features", []))
        next_link = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
        if not next_link:
            break
        url = next_link["href"]
        if urlparse(url).hostname != urlparse(EARTH_SEARCH).hostname:
            raise ValueError("Refusing a catalogue next-page link outside the provider")
        method = next_link.get("method", "GET").upper()
        body = {**payload, **next_link.get("body", {})} if method == "POST" else None
    truncated = bool(next_link)
    _write_json(cache_path, {"created_at": now.isoformat(), "features": features, "truncated": truncated})
    return features, truncated


def _tile(item: dict) -> str | None:
    properties = item.get("properties", {})
    tile = properties.get("s2:mgrs_tile") or properties.get("grid:code")
    if tile:
        return str(tile).removeprefix("MGRS-")
    zone, band, square = (properties.get(key) for key in
                           ("mgrs:utm_zone", "mgrs:latitude_band", "mgrs:grid_square"))
    return f"{int(zone):02d}{band}{square}" if zone and band and square else None


def _usable_item(item: dict, lon: float, lat: float) -> bool:
    bbox, assets = item.get("bbox", []), item.get("assets", {})
    covers = len(bbox) >= 4 and bbox[0] <= lon <= bbox[-2] and bbox[1] <= lat <= bbox[-1]
    return bool(covers and _tile(item) and {"swir16", "swir22", "scl"} <= assets.keys()
                and ("nir08" in assets or "nir" in assets))


def _common_nir(pre: dict, post: dict) -> str | None:
    return next((band for band in ("nir08", "nir")
                 if band in pre["assets"] and band in post["assets"]), None)


def sentinel_query(event: pd.Series, config: dict, cache_dir: Path, refresh: bool) -> dict:
    """Find bounded, same-tile/same-collection alternatives, never future images.

    pre/post keys retain convenient access to the first pair. pairs contains the
    alternatives, which still need LOCAL quality checks; catalogue cloud is not
    a claim that the event location was clear.
    """
    settings, (lon, lat) = _settings(config), _coordinates(event)
    now = _now(settings)
    start, end = _utc(event["start_time"]), _utc(event["end_time"])
    before_start = start - pd.Timedelta(days=float(settings["days_before"]))
    requested_end = end + pd.Timedelta(days=float(settings["days_after"]))
    after_end = min(requested_end, now)
    result = {"pairs": [], "status": "no_catalogue_match", "reason": "", "errors": [],
              "truncated": False, "search_as_of": now.isoformat()}
    if end >= now:
        result.update(status="waiting_for_post_image", reason="The event has not ended as of the image search; no future post-event image is used.")
        return result
    grouped: dict[tuple, dict] = defaultdict(lambda: {"pre": [], "post": []})
    cloud_limit = min(100, max(float(settings["max_scene_cloud"]), float(settings["fallback_scene_cloud"])))
    collections = list(dict.fromkeys(settings["collections"]))
    for collection in collections:
        for phase, lo, hi in (("pre", before_start, min(start, now)), ("post", end, after_end)):
            if lo >= hi:
                continue
            payload = {"collections": [collection], "intersects": {"type": "Point", "coordinates": [lon, lat]},
                       "datetime": f"{lo.isoformat()}/{hi.isoformat()}",
                       "limit": min(250, max(1, int(settings["catalog_page_size"]))),
                       "query": {"eo:cloud_cover": {"lte": cloud_limit}},
                       "sortby": [{"field": "properties.datetime", "direction": "desc" if phase == "pre" else "asc"}]}
            if settings.get("catalogue_bbox"):
                # Share catalogue searches across events without transmitting
                # event-specific coordinates/times. Exact matching stays local.
                month_start = lo.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                month_end = hi.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + pd.offsets.MonthBegin(1)
                payload.pop("intersects")
                payload["bbox"] = settings["catalogue_bbox"]
                payload["datetime"] = f"{month_start.isoformat()}/{month_end.isoformat()}"
            try:
                features, truncated = _search(payload, settings, Path(cache_dir), refresh)
                result["truncated"] |= truncated
            except Exception as exc:
                result["errors"].append(f"{collection} {phase}: {exc}")
                continue
            seen = set()
            for item in features:
                if not _usable_item(item, lon, lat) or item.get("collection", collection) != collection:
                    continue
                stamp = _utc(item.get("properties", {}).get("datetime"))
                if not lo <= stamp <= hi or stamp > now or (phase == "pre" and stamp >= start) or (phase == "post" and stamp <= end):
                    continue
                tile = _tile(item)
                identity = (tile, stamp.isoformat())
                if identity not in seen:
                    seen.add(identity)
                    grouped[(collection, tile)][phase].append(item)
    ranked = []
    for (collection, tile), sides in grouped.items():
        for phase, anchor in (("pre", start), ("post", end)):
            sides[phase].sort(key=lambda it: (abs((_utc(it["properties"]["datetime"]) - anchor).total_seconds()),
                                              it["properties"].get("eo:cloud_cover", 100)))
            sides[phase] = sides[phase][:max(1, int(settings["max_scenes_per_side"]))]
        for pre in sides["pre"]:
            for post in sides["post"]:
                nir_band = _common_nir(pre, post)
                if not nir_band:
                    continue
                lag = ((start - _utc(pre["properties"]["datetime"])) +
                       (_utc(post["properties"]["datetime"]) - end)).total_seconds()
                clouds = pre["properties"].get("eo:cloud_cover", 100) + post["properties"].get("eo:cloud_cover", 100)
                # Prefer offset-consistent collections before the legacy fallback;
                # within each quality tier, acquisition proximity comes first.
                ranked.append(((collection == "sentinel-2-l2a", lag, clouds, collections.index(collection)),
                               {"pre": pre, "post": post, "tile": tile, "nir_band": nir_band}))
    ranked.sort(key=lambda pair: pair[0])
    result["pairs"] = [pair for _, pair in ranked]
    if result["pairs"]:
        result.update(status="queued", reason="Catalogue pairs found; local cloud and surface-change checks pending.",
                      pre=result["pairs"][0]["pre"], post=result["pairs"][0]["post"])
    elif result["errors"]:
        result.update(status="download_failed", reason="Catalogue acquisition failed: " + "; ".join(result["errors"]))
    elif requested_end > now:
        result.update(status="waiting_for_post_image", reason="No matching pre/post pair is available yet; the post-event observation window is still open.")
    else:
        result["reason"] = "No same-tile, same-collection pre/post pair covering this point was found in the searched dates. This is data unavailability, not evidence that no fire occurred."
    if result["truncated"]:
        result["reason"] += " Catalogue pagination cap reached; additional scenes may exist."
    return result


def read_remote_crop(asset: dict, lon: float, lat: float, radius_m: float) -> tuple:
    """Return a small native-grid masked window (legacy five-value signature)."""
    if not 0 < radius_m <= 20000:
        raise ValueError("Sentinel crop radius must be within 0..20,000 metres")
    href = str(asset["href"])
    if href.startswith(("http://", "https://")) and not urlparse(href).path.lower().endswith((".tif", ".tiff")):
        raise ValueError("Only range-readable GeoTIFF imagery is supported, not full-scene JPEG2000")
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_TIMEOUT="30",
                       GDAL_HTTP_CONNECTTIMEOUT="10", GDAL_HTTP_MAX_RETRY="1", GDAL_HTTP_RETRY_DELAY="1"):
        with rasterio.open(href) as dataset:
            if not dataset.crs or not dataset.crs.is_projected:
                raise ValueError("Sentinel asset must have a projected metre grid")
            xs, ys = transform("EPSG:4326", dataset.crs, [lon], [lat])
            x, y = xs[0], ys[0]
            if not dataset.bounds.left <= x <= dataset.bounds.right or not dataset.bounds.bottom <= y <= dataset.bounds.top:
                raise ValueError("Event centroid falls outside the Sentinel raster")
            row, column = dataset.index(x, y)
            rx, ry = (max(1, math.ceil(radius_m / abs(res))) for res in dataset.res)
            if rx * ry > 4_000_000:
                raise ValueError("Requested Sentinel window exceeds the bounded read size")
            window = Window(column - rx, row - ry, 2 * rx, 2 * ry)
            array = dataset.read(1, window=window, boundless=True, masked=True)
            band = (asset.get("raster:bands") or [{}])[0]
            return array, dataset.window_transform(window), dataset.crs, float(band.get("scale", 1)), float(band.get("offset", 0))


def scale_reflectance(raw: np.ndarray, asset: dict, properties: dict | None = None) -> np.ndarray:
    """Apply the asset's correction once; boa_offset_applied is not a second offset.

    Provider metadata can have a true boa_offset_applied flag AND a nonzero asset
    offset. The provider explicitly directs readers to the asset raster metadata.
    Missing scale/offset fails visibly instead of guessing a processing baseline.
    """
    band = (asset.get("raster:bands") or [{}])[0]
    if "scale" not in band or "offset" not in band:
        raise ValueError("Sentinel reflectance asset lacks explicit raster:bands scale/offset")
    source = np.asarray(np.ma.getdata(raw), dtype=np.float32)
    valid = ~np.ma.getmaskarray(raw) & np.isfinite(source) & (source != 0)
    if band.get("nodata") is not None:
        valid &= source != float(band["nodata"])
    scaled = source * float(band["scale"]) + float(band["offset"])
    return np.where(valid, scaled, np.nan).astype(np.float32)


def make_grid(lon: float, lat: float, radius_m: float, resolution: float = 20) -> dict:
    if not 0 < radius_m <= 10000 or resolution != 20:
        raise ValueError("Use a 20 m grid and a crop radius within 0..10,000 m")
    zone = max(1, min(60, int((lon + 180) // 6) + 1))
    crs = rasterio.crs.CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)
    xs, ys = transform("EPSG:4326", crs, [lon], [lat])
    x, y = xs[0], ys[0]
    left = math.floor((x - radius_m) / resolution) * resolution
    top = math.ceil((y + radius_m) / resolution) * resolution
    shape = (math.ceil((top - y + radius_m) / resolution), math.ceil((x + radius_m - left) / resolution))
    return {"transform": from_origin(left, top, resolution, resolution), "crs": crs,
            "shape": shape, "center": (x, y), "resolution": resolution}


def reproject_array(array: np.ndarray, source_transform: Any, source_crs: Any, grid: dict,
                    categorical: bool = False) -> np.ndarray:
    source = np.asarray(np.ma.filled(np.ma.asarray(array, dtype=np.float32), np.nan))
    target = np.full(grid["shape"], np.nan, dtype=np.float32)
    reproject(source, target, src_transform=source_transform, src_crs=source_crs, src_nodata=np.nan,
              dst_transform=grid["transform"], dst_crs=grid["crs"], dst_nodata=np.nan,
              resampling=Resampling.nearest if categorical else Resampling.bilinear,
              num_threads=1)
    return target


def _circle(grid: dict, radius_m: float) -> np.ndarray:
    yy, xx = np.indices(grid["shape"])
    affine = grid["transform"]
    x = affine.c + (xx + 0.5) * affine.a
    y = affine.f + (yy + 0.5) * affine.e
    return (x - grid["center"][0]) ** 2 + (y - grid["center"][1]) ** 2 <= radius_m ** 2


def _read_scene(item: dict, event: pd.Series, settings: dict, grid: dict, roi: np.ndarray,
                nir_band: str | None = None) -> dict:
    lon, lat = _coordinates(event)
    radius = float(settings["crop_radius_m"]) * 1.1 + 40
    scl, scl_transform, scl_crs, _, _ = read_remote_crop(item["assets"]["scl"], lon, lat, radius)
    aligned_scl = reproject_array(scl, scl_transform, scl_crs, grid, categorical=True)
    scene = {"scl": aligned_scl, "nir_band": nir_band or ("nir08" if "nir08" in item["assets"] else "nir")}
    # A cloudy SCL window is enough to reject a scene, avoiding three band reads.
    clear_fraction = float(np.isin(aligned_scl[roi], CLEAR_SCL).mean())
    for key in ("nir", "swir16", "swir22"):
        if clear_fraction < float(settings["min_local_valid_fraction"]):
            scene[key] = np.full(grid["shape"], np.nan, dtype=np.float32)
            continue
        asset = item["assets"][scene["nir_band"] if key == "nir" else key]
        raw, affine, crs, _, _ = read_remote_crop(asset, lon, lat, radius)
        values = scale_reflectance(raw, asset, item.get("properties"))
        native_grid = {"shape": values.shape, "transform": affine, "crs": crs}
        native_scl = reproject_array(scl, scl_transform, scl_crs, native_grid, categorical=True)
        # Mask BEFORE bilinear interpolation so bright cloud/nodata cannot bleed in.
        values[~np.isin(native_scl, CLEAR_SCL) | (values <= 0)] = np.nan
        scene[key] = reproject_array(values, affine, crs, grid)
        scene[key][~np.isin(aligned_scl, CLEAR_SCL)] = np.nan
    return scene


def _maps(pre: dict, post: dict) -> dict:
    maps = {}
    for phase, scene in (("pre", pre), ("post", post)):
        valid = np.isin(scene["scl"], CLEAR_SCL)
        for band in ("nir", "swir16", "swir22"):
            valid &= np.isfinite(scene[band]) & (scene[band] > 0)
        maps[f"{phase}_valid"] = valid
        denominator = scene["nir"] + scene["swir22"]
        maps[f"{phase}_nbr"] = np.divide(scene["nir"] - scene["swir22"], denominator,
                                          out=np.full(denominator.shape, np.nan, dtype=np.float32), where=valid)
    maps["joint"] = maps["pre_valid"] & maps["post_valid"]
    maps["dnbr"] = maps["pre_nbr"] - maps["post_nbr"]
    return maps


def compute_evidence(pre: dict, post: dict, roi: np.ndarray, settings: dict) -> dict:
    """Joint-clear, event-circle statistics; dNBR = pre NBR minus post NBR."""
    settings = {**DEFAULTS, **settings}
    shape = roi.shape
    if not roi.any() or any(scene[key].shape != shape for scene in (pre, post)
                            for key in ("nir", "swir16", "swir22", "scl")):
        raise ValueError("Evidence arrays must share one grid and a nonempty analysis circle")
    maps = _maps(pre, post)
    joint = maps["joint"] & roi
    count = int(joint.sum())
    metrics = {key: None for key in (
        "sentinel_pre_nbr_mean", "sentinel_post_nbr_mean", "sentinel_dnbr_mean", "sentinel_dnbr_median",
        "sentinel_affected_fraction", "sentinel_affected_area_m2", "sentinel_pre_swir12_mean",
        "sentinel_post_swir12_mean", "sentinel_swir12_relative_change", "sentinel_normalized_swir_change",
        "sentinel_pre_swir_ratio", "sentinel_post_swir_ratio", "sentinel_swir_ratio_change",
        "sentinel_swir_affected_fraction")}
    metrics.update(sentinel_joint_valid_fraction=count / int(roi.sum()), sentinel_valid_pixels=count,
                   sentinel_evidence="inconclusive", sentinel_status="cloud_obscured")
    for phase, scene in (("pre", pre), ("post", post)):
        scl = scene["scl"][roi]
        metrics.update({f"sentinel_{phase}_valid_fraction": float(maps[f"{phase}_valid"][roi].mean()),
                        f"sentinel_{phase}_cloud_fraction": float(np.isin(scl, CLOUD_SCL).mean()),
                        f"sentinel_{phase}_saturated_fraction": float((scl == 1).mean()),
                        f"sentinel_{phase}_nodata_fraction": float(((scl == 0) | ~np.isfinite(scl)).mean())})
    if not count or metrics["sentinel_joint_valid_fraction"] < float(settings["min_local_valid_fraction"]):
        metrics["sentinel_reason"] = "Too little jointly clear local area after cloud, shadow, snow, saturation and no-data masking; surface change is inconclusive, not absent."
        return metrics
    dnbr = maps["dnbr"][joint]
    affected = dnbr >= float(settings["dnbr_threshold"])
    b12_pre, b12_post = pre["swir22"][joint], post["swir22"][joint]
    pre_mean, post_mean = float(b12_pre.mean()), float(b12_post.mean())
    ratios, normalized = {}, {}
    for phase, scene in (("pre", pre), ("post", post)):
        b11, b12 = scene["swir16"][joint], scene["swir22"][joint]
        ratios[phase] = float((b12 / b11).mean())
        normalized[phase] = float(((b12 - b11) / (b12 + b11)).mean())
    relative_change = (post_mean - pre_mean) / max(abs(pre_mean), 1e-6)
    normalized_change = normalized["post"] - normalized["pre"]
    swir_affected = np.abs((b12_post - b12_pre) / np.maximum(b12_pre, 0.02)) >= float(settings["swir_relative_threshold"])
    metrics.update(sentinel_status="analysed", sentinel_pre_nbr_mean=float(maps["pre_nbr"][joint].mean()),
                   sentinel_post_nbr_mean=float(maps["post_nbr"][joint].mean()),
                   sentinel_dnbr_mean=float(dnbr.mean()), sentinel_dnbr_median=float(np.median(dnbr)),
                   sentinel_affected_fraction=float(affected.mean()), sentinel_affected_area_m2=int(affected.sum()) * 400,
                   sentinel_pre_swir12_mean=pre_mean, sentinel_post_swir12_mean=post_mean,
                   sentinel_swir12_relative_change=relative_change, sentinel_normalized_swir_change=normalized_change,
                   sentinel_pre_swir_ratio=ratios["pre"], sentinel_post_swir_ratio=ratios["post"],
                   sentinel_swir_ratio_change=ratios["post"] - ratios["pre"],
                   sentinel_swir_affected_fraction=float(swir_affected.mean()))
    if float(dnbr.mean()) >= float(settings["dnbr_threshold"]) and float(affected.mean()) >= float(settings["min_affected_fraction"]):
        metrics.update(sentinel_evidence="observed_burn_like_change",
                       sentinel_reason="A positive pre-minus-post NBR change affects a substantial share of the jointly clear area. This is an unvalidated burn-like surface-change heuristic, not fire confirmation.")
    elif (abs(relative_change) >= float(settings["swir_relative_threshold"])
          and abs(normalized_change) >= float(settings["normalized_swir_threshold"])
          and float(swir_affected.mean()) >= float(settings["min_affected_fraction"])):
        metrics.update(sentinel_evidence="observed_swir_change",
                       sentinel_reason="SWIR reflectance and spectral ratio changed between acquisitions. This unvalidated heuristic is not specific to fire or industrial escalation.")
    else:
        metrics.update(sentinel_evidence="no_clear_change",
                       sentinel_reason="No threshold-exceeding lasting surface change was observed in these dated images. This does not rule out a short, small, or industrial fire between overpasses.")
    return metrics


def _pair_fields(pair: dict, event: pd.Series) -> dict:
    fields = {"sentinel_mgrs_tile": pair.get("tile") or _tile(pair["pre"])}
    legacy = any(pair[phase].get("collection") == "sentinel-2-l2a" for phase in ("pre", "post"))
    fields["sentinel_radiometry_status"] = "legacy_offset_unverified" if legacy else "asset_metadata"
    fields["sentinel_radiometry_note"] = (
        "Legacy COG offset inconsistencies are reported in Element84/earth-search issue66. "
        "Asset scaling is used for provisional imagery only; quantitative change claims are withheld."
        if legacy else "Each item asset's scale and offset applied exactly once; no date/BOA-flag correction added.")
    for phase, anchor in (("pre", _utc(event["start_time"])), ("post", _utc(event["end_time"]))):
        item = pair[phase]
        stamp = _utc(item["properties"]["datetime"])
        fields.update({f"sentinel_{phase}_item": item["id"], f"sentinel_{phase}_collection": item.get("collection"),
                       f"sentinel_{phase}_datetime": stamp.isoformat(),
                       f"sentinel_{phase}_lag_days": abs((stamp - anchor).total_seconds()) / 86400,
                       f"sentinel_{phase}_scene_cloud": item["properties"].get("eo:cloud_cover")})
        fields[f"sentinel_{phase}_boa_offset_applied"] = item["properties"].get(
            "earthsearch:boa_offset_applied", item["properties"].get("earthsearch:boffset_applied"))
    return fields


def _save_products(folder: Path, pre: dict, post: dict, grid: dict, roi: np.ndarray, row: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for phase, scene in (("pre", pre), ("post", post)):
        with rasterio.open(folder / f"{phase}.tif", "w", driver="GTiff", height=grid["shape"][0],
                           width=grid["shape"][1], count=4, dtype="float32", crs=grid["crs"],
                           transform=grid["transform"], nodata=np.nan, compress="deflate") as dst:
            for index, band in enumerate(("nir", "swir16", "swir22", "scl"), 1):
                dst.write(scene[band].astype(np.float32), index)
                dst.set_band_description(index, f"{band}: " + ("categorical SCL" if band == "scl" else "scaled reflectance, cloud masked"))
            dst.update_tags(scene_id=row[f"sentinel_{phase}_item"], datetime=row[f"sentinel_{phase}_datetime"],
                            method=METHOD, scale_offset="Applied exactly once from item asset metadata",
                            radiometry_status=row["sentinel_radiometry_status"])
    with _PLOT_LOCK:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        maps = _maps(pre, post)
        figure = Figure(figsize=(10, 3.6), constrained_layout=True)
        FigureCanvasAgg(figure)
        for axis, phase, label in zip(figure.subplots(1, 3), ("pre_nbr", "post_nbr", "dnbr"),
                                      ("Pre-event NBR", "Post-event NBR", "dNBR (pre minus post)")):
            axis.set_facecolor("#dddddd")
            image = axis.imshow(np.ma.masked_invalid(maps[phase]), cmap="RdYlGn" if phase != "dnbr" else "RdBu_r",
                                vmin=-1 if phase != "dnbr" else -0.5, vmax=1 if phase != "dnbr" else 0.5)
            axis.contour(roi.astype(int), levels=[0.5], colors=["black"], linewidths=0.7)
            axis.set_title(label, fontsize=10)
            axis.set_xticks([])
            axis.set_yticks([])
            figure.colorbar(image, ax=axis, shrink=0.75)
        qualifier = "LEGACY RADIOMETRY UNVERIFIED; PROVISIONAL INDICES" if row["sentinel_radiometry_status"] == "legacy_offset_unverified" else "unvalidated surface-change evidence"
        figure.suptitle(f"{row['sentinel_pre_datetime'][:10]} / {row['sentinel_post_datetime'][:10]} UTC | "
                       f"{row['sentinel_evidence']}\nCircle: {row['sentinel_analysis_radius_m']:g} m; gray: excluded pixels; unvalidated surface-change evidence",
                       fontsize=9)
        if row["sentinel_radiometry_status"] == "legacy_offset_unverified":
            figure.suptitle(qualifier + "\n" + f"{row['sentinel_pre_datetime'][:10]} / {row['sentinel_post_datetime'][:10]} UTC; gray: excluded pixels; circle: analysis area", fontsize=9)
        figure.savefig(folder / "evidence.png", dpi=120)


def _publish_products(folder: Path, output_dir: Path, key: str, row: dict) -> None:
    if row.get("sentinel_status") != "analysed":
        return
    destination = output_dir / "sentinel"
    for name, column in (("pre.tif", "sentinel_pre_crop"), ("post.tif", "sentinel_post_crop"),
                         ("evidence.png", "sentinel_preview")):
        source = folder / name
        if source.exists():
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / f"{key}_{name}"
            shutil.copy2(source, target)
            row[column] = str(target)


def _base_row(event: pd.Series, settings: dict) -> dict:
    row = {key: None for key in SENTINEL_COLUMNS}
    row.update(event_id=str(event["event_id"]), sentinel_status="queued", sentinel_evidence="inconclusive",
               sentinel_reason="Not selected by this run's priority policy or processing cap.",
               sentinel_selected=False, sentinel_method=METHOD, sentinel_cache_hit=False,
               sentinel_adjustment=0.0, sentinel_catalog_truncated=False, sentinel_pairs_attempted=0,
               sentinel_candidate_pairs=0, sentinel_analysis_radius_m=float(settings["analysis_radius_m"]),
               sentinel_grid_resolution_m=20)
    return row


def _process_event(event: pd.Series, config: dict, cache_dir: Path, output_dir: Path, refresh: bool) -> tuple[dict, list]:
    settings, errors = _settings(config), []
    row = _base_row(event, settings)
    row["sentinel_selected"] = True
    key = evidence_cache_key(event, config)
    row["sentinel_cache_key"] = key
    folder = cache_dir / "sentinel_evidence" / key
    metrics_path = folder / "metrics.json"
    now = _now(settings)
    if metrics_path.exists() and not refresh:
        try:
            cached = json.loads(metrics_path.read_text(encoding="utf-8"))
            saved = cached["result"]
            post_date = saved.get("sentinel_post_datetime")
            valid_date = not post_date or _utc(post_date) <= now
            age = (now - _utc(cached["created_at"])).total_seconds() / 3600
            final = saved["sentinel_status"] == "analysed" and all((folder / f).exists() for f in ("pre.tif", "post.tif", "evidence.png"))
            if valid_date and (final or 0 <= age < float(settings["retry_after_hours"])):
                row.update(saved)
                row.update(event_id=str(event["event_id"]), sentinel_selected=True, sentinel_cache_hit=True)
                _publish_products(folder, output_dir, key, row)
                return row, cached.get("errors", [])
        except (OSError, ValueError, KeyError):
            pass
    catalogue = sentinel_query(event, config, cache_dir, refresh)
    row.update(sentinel_search_as_of=catalogue["search_as_of"], sentinel_catalog_truncated=catalogue["truncated"],
               sentinel_candidate_pairs=len(catalogue["pairs"]), sentinel_status=catalogue["status"],
               sentinel_reason=catalogue["reason"])
    errors.extend(catalogue["errors"])
    if catalogue["pairs"]:
        radius, analysis = float(settings["crop_radius_m"]), float(settings["analysis_radius_m"])
        if not 0 < analysis <= radius or not 0 < float(settings["min_local_valid_fraction"]) <= 1:
            raise ValueError("Analysis radius must fit inside crop, and minimum clear fraction must be within (0, 1]")
        lon, lat = _coordinates(event)
        grid = make_grid(lon, lat, radius)
        roi = _circle(grid, analysis)
        scenes: dict[tuple, Any] = {}
        best_quality = -1.0
        row.update(sentinel_status="download_failed", sentinel_reason="No candidate imagery could be read.")
        for pair in catalogue["pairs"][:max(1, min(100, int(settings["max_pair_attempts"])) )]:
            row["sentinel_pairs_attempted"] += 1
            try:
                data = {}
                for phase in ("pre", "post"):
                    item = pair[phase]
                    identity = (item.get("collection"), item["id"], pair["nir_band"])
                    if identity not in scenes:
                        try:
                            scenes[identity] = _read_scene(item, event, settings, grid, roi, pair["nir_band"])
                        except Exception as exc:
                            scenes[identity] = exc
                    if isinstance(scenes[identity], Exception):
                        raise scenes[identity]
                    data[phase] = scenes[identity]
                metrics = compute_evidence(data["pre"], data["post"], roi, settings)
                pair_fields = _pair_fields(pair, event)
                if pair_fields["sentinel_radiometry_status"] == "legacy_offset_unverified" and metrics["sentinel_status"] == "analysed":
                    for field in tuple(metrics):
                        if any(part in field for part in ("nbr", "swir", "affected")):
                            metrics[field] = None
                    metrics.update(sentinel_evidence="inconclusive", sentinel_reason=pair_fields["sentinel_radiometry_note"])
                quality = metrics["sentinel_joint_valid_fraction"]
                comparison = {**row, **metrics, **pair_fields}
                comparison["sentinel_nir_band"] = f"pre:{data['pre']['nir_band']};post:{data['post']['nir_band']}"
                if metrics["sentinel_status"] == "analysed":
                    # Publish a successful result only after this exact pair's products exist.
                    _save_products(folder, data["pre"], data["post"], grid, roi, comparison)
                    row.update(comparison)
                    break
                if quality > best_quality:
                    best_quality = quality
                    row.update(comparison)
            except Exception as exc:
                message = f"Pair {pair['pre']['id']} / {pair['post']['id']}: {exc}"
                if message not in errors:
                    errors.append(message)
        if row["sentinel_status"] != "analysed":
            row["sentinel_reason"] += f" Tried {row['sentinel_pairs_attempted']} of {row['sentinel_candidate_pairs']} bounded candidate pairs."
        if row["sentinel_status"] == "download_failed" and errors:
            row["sentinel_reason"] += " " + errors[-1]
    if catalogue["truncated"] and "pagination" not in row["sentinel_reason"]:
        row["sentinel_reason"] += " Catalogue pagination cap reached; more scenes may exist."
    try:
        _write_json(metrics_path, {"method": METHOD, "created_at": now.isoformat(), "result": row, "errors": errors})
        _publish_products(folder, output_dir, key, row)
    except OSError as exc:
        errors.append(f"Evidence cache/output write: {exc}")
    return row, errors


def acquire_sentinel(events: pd.DataFrame, config: dict, cache_dir: Path, output_dir: Path,
                     refresh: bool = False) -> tuple[pd.DataFrame, list[str]]:
    """Return one explicit-status row per event, preserving all primary results."""
    settings = _settings(config)
    if events.empty:
        return pd.DataFrame(columns=SENTINEL_COLUMNS), []
    if events["event_id"].astype(str).duplicated().any():
        raise ValueError("Sentinel evidence requires unique event_id values")
    rows = {str(event["event_id"]): _base_row(event, settings) for _, event in events.iterrows()}
    if not settings["enabled"]:
        for row in rows.values():
            row.update(sentinel_status="disabled", sentinel_reason="Sentinel imagery acquisition is disabled in this run's configuration.")
        return pd.DataFrame(rows.values(), columns=SENTINEL_COLUMNS), []
    selected = select_candidates(events, config)
    errors = []
    work: dict[str, list] = defaultdict(list)
    for _, event in events.iterrows():
        identifier = str(event["event_id"])
        if identifier in selected:
            try:
                work[evidence_cache_key(event, config)].append(event)
            except Exception as exc:
                rows[identifier].update(sentinel_status="download_failed", sentinel_selected=True, sentinel_reason=str(exc))
                errors.append(f"{identifier}: {exc}")
        else:
            rows[identifier]["sentinel_reason"] = (f"Queued: this run selected {len(selected)} of {len(events)} events under "
                                                    f"policy '{settings['candidate_policy']}' and cap {settings['max_events']}; no image claim is made.")
    with ThreadPoolExecutor(max_workers=max(1, min(3, int(settings["workers"])))) as executor:
        futures = {executor.submit(_process_event, group[0], config, Path(cache_dir), Path(output_dir), refresh): group
                   for group in work.values()}
        for completed, future in enumerate(as_completed(futures), 1):
            group = futures[future]
            try:
                evidence, event_errors = future.result()
            except Exception as exc:  # Optional imaging must never discard the core event table.
                evidence = {"sentinel_status": "download_failed", "sentinel_reason": str(exc), "sentinel_evidence": "inconclusive"}
                event_errors = [str(exc)]
            for event in group:
                identifier = str(event["event_id"])
                rows[identifier].update(evidence)
                rows[identifier].update(event_id=identifier, sentinel_selected=True, sentinel_priority_group=selected[identifier])
                errors.extend(f"{identifier}: {message}" for message in event_errors)
            print(f"Sentinel {completed}/{len(futures)}: {evidence.get('sentinel_status', 'unknown')}", flush=True)
    return pd.DataFrame(rows.values(), columns=SENTINEL_COLUMNS), errors
