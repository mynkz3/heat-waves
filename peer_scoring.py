"""Detection-conditioned vegetation peer ranking, not fire probability."""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd


FEATURES = ("frp_peak_mw", "brightness_temperature_difference_max", "detection_peak")
VEGETATION = {"tree_cover", "shrubland", "grassland", "cropland",
              "herbaceous_wetland", "mangroves", "moss_lichen"}
DEFAULTS = dict(enabled=False, min_events=32, min_days=8, min_cells=3,
                radius_km=100, exclude_radius_km=2, season_window_days=45,
                history_window_days=2557, gap_days=7, cell_km=5, max_events=2000)
METHOD = "vegetation-peer-mad-v1"


def score_vegetation_peers(events: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Fill missing vegetation scores, without altering existing site scores.

    References match cover/platform/phase and a spatial/seasonal neighbourhood.
    One episode per grid-cell/day limits dense detections dominating the cohort.
    Its fitted robust-distance percentile is descriptive, not fire probability.
    """
    settings = {**DEFAULTS, **config.get("vegetation_peers", {})}
    if not settings["enabled"]:
        return events.copy()
    for key in ("min_events", "min_days", "min_cells", "max_events",
                "history_window_days", "gap_days"):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value < 1 or value != int(value):
            raise ValueError(f"vegetation_peers.{key} must be a positive integer")
        settings[key] = int(value)
    for key in ("radius_km", "exclude_radius_km", "cell_km", "season_window_days"):
        if not np.isfinite(settings[key]) or settings[key] <= 0:
            raise ValueError(f"vegetation_peers.{key} must be positive")
    if (settings["max_events"] < settings["min_events"]
            or settings["exclude_radius_km"] >= settings["radius_km"]
            or settings["season_window_days"] > 183):
        raise ValueError("Invalid vegetation peer limits")
    if not events.index.is_unique or events["event_id"].isna().any() or events["event_id"].duplicated().any():
        raise ValueError("Peer scoring requires unique event IDs and row indices")
    west, south, east, north = config["aoi"]["bbox"]
    result = events.copy()
    site_scores = result.get("site_anomaly_score", result["anomaly_score"]).copy()
    result["site_anomaly_score"] = site_scores
    result["score_method"] = np.where(site_scores.notna(), "site_history", "unavailable")
    defaults = {
        "peer_score": np.nan, "peer_status": "not_applicable", "peer_reason": None,
        "peer_reference_count": 0, "peer_reference_days": 0, "peer_reference_cells": 0,
        "peer_feature_count": 0, "peer_reference_start": None, "peer_reference_end": None,
        "peer_reference_hash": None, "peer_group": None, "peer_method": METHOD,
        "peer_radius_km": settings["radius_km"],
        "peer_season_window_days": settings["season_window_days"],
        "peer_gap_days": settings["gap_days"],
    }
    for name, value in defaults.items():
        result[name] = value
    for feature in FEATURES:
        result[f"peer_z_{feature}"] = np.nan
    if result.empty:
        return result

    def column(name, default=None):
        return result.get(name, pd.Series(default, index=result.index))

    context = column("context_label")
    target = context.eq("vegetation_associated") & site_scores.isna()
    result.loc[target, "anomaly_score"] = np.nan
    result.loc[target, "review_priority"] = "unscored"
    start = pd.to_datetime(column("start_time"), utc=True, errors="coerce")
    end = pd.to_datetime(column("end_time"), utc=True, errors="coerce")
    latitude = pd.to_numeric(column("latitude"), errors="coerce").to_numpy(dtype=float)
    longitude = pd.to_numeric(column("longitude"), errors="coerce").to_numpy(dtype=float)
    cover, sensor, phase = column("worldcover_dominant"), column("model_sensor"), column("model_daynight")
    in_aoi = np.isfinite(latitude) & np.isfinite(longitude) & (latitude >= south) & (latitude <= north) & (longitude >= west) & (longitude <= east)
    valid_context = cover.isin(VEGETATION) & pd.to_numeric(column("worldcover_coverage"), errors="coerce").ge(.8)
    valid_phase = phase.isin(["D", "N"]) & sensor.isin(["VIIRS_SNPP", "VIIRS_NOAA20", "VIIRS_NOAA21"])
    valid_time = start.notna() & end.notna() & end.ge(start)
    eligible = (context.eq("vegetation_associated") & valid_context & valid_phase
                & valid_time & in_aoi & column("data_quality").eq("science_quality"))
    values = np.column_stack([pd.to_numeric(column(f), errors="coerce").to_numpy(dtype=float) for f in FEATURES])
    values[~np.isfinite(values)] = np.nan
    values[values[:, 0] <= 0, 0] = np.nan
    values[values[:, 2] < 1, 2] = np.nan
    # Filter before deduplication and capping so incomplete vectors cannot
    # displace valid peers or inflate fitted-cohort diversity.
    eligible &= np.isfinite(values).all(axis=1)
    doy = start.dt.dayofyear.to_numpy()
    days = start.dt.floor("D")
    day_keys = days.astype("int64").to_numpy()
    # Fixed AOI grid: no dependence on sites discovered using future events.
    x = (np.where(np.isfinite(longitude), longitude, west) - west) * 111.32 * np.cos(np.radians((south + north) / 2))
    y = (np.where(np.isfinite(latitude), latitude, south) - south) * 110.57
    cells = [f"{int(a)}:{int(b)}" for a, b in zip(np.floor(x / settings["cell_km"]), np.floor(y / settings["cell_km"]))]
    ids = result["event_id"].astype(str).to_numpy()
    groups = {}
    if eligible.any():
        for key, indices in result.loc[eligible].groupby(["worldcover_dominant", "model_sensor", "model_daynight"]).groups.items():
            groups[key] = result.index.get_indexer(indices)

    def unavailable(index, status, reason):
        result.at[index, "peer_status"] = status
        result.at[index, "peer_reason"] = reason

    for pos in np.flatnonzero(target.to_numpy()):
        index = result.index[pos]
        if not in_aoi[pos] or not valid_time.iloc[pos]:
            unavailable(index, "invalid_peer_location_or_time", "Peer scoring needs a dated event inside the configured pilot region.")
            continue
        if not valid_context.iloc[pos]:
            unavailable(index, "unknown_peer_landcover", "No reliable vegetation land-cover group is available for peer comparison.")
            continue
        if not valid_phase.iloc[pos]:
            unavailable(index, "unknown_peer_sensor_or_phase", "Peer comparison needs a recognised satellite and day/night phase.")
            continue
        key = (cover.iloc[pos], sensor.iloc[pos], phase.iloc[pos])
        result.at[index, "peer_group"] = " | ".join(key)
        reference = groups.get(key, np.array([], dtype=int))
        cutoff = days.iloc[pos] - pd.Timedelta(days=settings["gap_days"])
        earliest = start.iloc[pos] - pd.Timedelta(days=settings["history_window_days"])
        delta = np.abs(doy[reference] - doy[pos])
        mask = ((end.iloc[reference] < cutoff).to_numpy()
                & (start.iloc[reference] >= earliest).to_numpy()
                & (np.minimum(delta, 366 - delta) <= settings["season_window_days"]))
        reference = reference[mask]
        a = (np.sin(np.radians(latitude[reference] - latitude[pos]) / 2) ** 2
             + np.cos(np.radians(latitude[pos])) * np.cos(np.radians(latitude[reference]))
             * np.sin(np.radians(longitude[reference] - longitude[pos]) / 2) ** 2)
        distance = 6371.0088 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
        reference = reference[(distance <= settings["radius_km"]) & (distance > settings["exclude_radius_km"])]
        ordered = sorted(reference, key=lambda j: (day_keys[j], ids[j]))
        seen, chosen = set(), []
        for j in ordered:
            cell_day = (cells[j], day_keys[j])
            if cell_day not in seen:
                seen.add(cell_day)
                chosen.append(j)
        reference = np.asarray(chosen[-settings["max_events"]:], dtype=int)
        count = len(reference)
        n_days, n_cells = len(set(day_keys[reference])), len({cells[j] for j in reference})
        result.loc[index, ["peer_reference_count", "peer_reference_days", "peer_reference_cells"]] = [count, n_days, n_cells]
        if count:
            result.at[index, "peer_reference_start"] = start.iloc[reference].min().isoformat()
            result.at[index, "peer_reference_end"] = end.iloc[reference].max().isoformat()
            digest = hashlib.sha256(("\n".join(ids[reference])).encode())
            digest.update(values[reference].tobytes())
            result.at[index, "peer_reference_hash"] = digest.hexdigest()
        if count < settings["min_events"]:
            unavailable(index, "insufficient_peers", f"Only {count} comparable earlier peer episodes; at least {settings['min_events']} required. No same-site recurrence is required.")
            continue
        if n_days < settings["min_days"]:
            unavailable(index, "insufficient_peer_days", f"Peer episodes cover only {n_days} dates; at least {settings['min_days']} required.")
            continue
        if n_cells < settings["min_cells"]:
            unavailable(index, "insufficient_peer_cells", f"Peer episodes cover only {n_cells} spatial cells; at least {settings['min_cells']} required.")
            continue
        matrix = values[reference]
        usable = np.isfinite(values[pos]) & (np.isfinite(matrix).sum(axis=0) >= settings["min_events"])
        n_features = int(usable.sum())
        result.at[index, "peer_feature_count"] = n_features
        if n_features < 3:
            unavailable(index, "insufficient_peer_features", f"Only {n_features} usable peer feature comparisons; at least 3 required.")
            continue
        matrix = matrix[:, usable]
        matrix = matrix[np.isfinite(matrix).all(axis=1)]
        if len(matrix) < settings["min_events"]:
            unavailable(index, "insufficient_peer_features", "Too few complete peer feature vectors for a comparable multivariate rank.")
            continue
        median = np.median(matrix, axis=0)
        scale = 1.4826 * np.median(np.abs(matrix - median), axis=0)
        fallback = np.maximum(np.maximum(np.std(matrix, axis=0), np.abs(median) * .05), 1e-6)
        scale = np.where(scale < 1e-9, fallback, scale)
        z = (values[pos, usable] - median) / scale
        reference_raw = np.sqrt(np.mean(np.maximum((matrix - median) / scale, 0) ** 2, axis=1))
        raw = float(np.sqrt(np.mean(np.maximum(z, 0) ** 2)))
        score = float(((reference_raw < raw).sum() + (reference_raw <= raw).sum() + 1) / (2 * (len(matrix) + 1)))
        for feature, value in zip(np.asarray(FEATURES)[usable], z):
            result.at[index, f"peer_z_{feature}"] = float(value)
        result.at[index, "peer_score"] = score
        result.at[index, "anomaly_score"] = score
        result.at[index, "score_method"] = "vegetation_peer"
        result.at[index, "peer_status"] = "scored"
        result.at[index, "peer_reason"] = "Relative to earlier, season/land-cover/platform/phase-matched vegetation detections; not fire probability or severity."
        result.at[index, "review_priority"] = "very_high" if score >= .99 else "high" if score >= .95 else "moderate" if score >= .8 else "lower"
    return result
