"""Build the portable evidence workspace. No web server or build step required."""
from __future__ import annotations

import base64
import gzip
import json
import math
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "frontend"
SUMMARY_FIELDS = (
    "event_id", "event_uid", "site_id", "start_time", "end_time", "decision",
    "anomaly_score", "detection_count", "frp_peak_mw", "nearest_facility_name",
    "nearest_facility_id", "context_label", "source_context", "score_status",
)


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return _clean(value.item())
    return str(value)


def _optional_geojson(path: Path) -> tuple[dict, str]:
    empty = {"type": "FeatureCollection", "features": []}
    if not path.is_file():
        return empty, "not_cached"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
            return empty, "invalid_cache"
        return data, "cached" if data["features"] else "empty_cache"
    except (OSError, ValueError, AttributeError):
        return empty, "unreadable_cache"


def _site_file(site: Path, relative: str) -> Path:
    target = site / relative
    if not target.resolve().is_relative_to(site.resolve()):
        raise ValueError(f"Site asset escapes its output directory: {relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _write_site_json(site: Path, relative: str, value: Any) -> None:
    payload = json.dumps(_clean(value), ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    _site_file(site, relative).write_bytes(payload)
    _site_file(site, relative + ".gz").write_bytes(gzip.compress(payload, compresslevel=6, mtime=0))


def _copy_preview(value: Any, output: Path, site: Path) -> str | None:
    """Only publish existing raster previews from this output's sentinel folder."""
    if not isinstance(value, str):
        return None
    value = unquote(value).replace("\\", "/").strip()
    if value.startswith("//") or "%" in value or re.search(r"[\x00-\x1f]", value) or ".." in value.split("/"):
        return None
    candidate = Path(value)
    if not candidate.is_absolute() and (value.startswith("/") or re.match(r"^[a-z][a-z0-9+.-]*:", value, re.I)):
        return None
    if not candidate.is_absolute():
        candidate = output / candidate
    candidate = candidate.resolve()
    sentinel = (output / "sentinel").resolve()
    if (not sentinel.is_relative_to(output.resolve()) or not candidate.is_relative_to(sentinel)
            or candidate.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} or not candidate.is_file()):
        return None
    relative = "sentinel/" + candidate.relative_to(sentinel).as_posix()
    shutil.copyfile(candidate, _site_file(site, relative))
    return relative


def write_web_viewer(output_dir: Path, events_geojson: dict, facilities_geojson: dict, config: dict) -> Path:
    """Package a localhost site without downloading or recomputing observations.

    Summaries reference bounded detail shards by index, never by an event ID.
    Scientific properties and coordinates are preserved; only preview paths are
    normalized to the copied local assets. Source exports are not changed.
    """
    output = Path(output_dir)
    site = output / "site"
    if not site.resolve().is_relative_to(output.resolve()):
        raise ValueError("Site directory escapes the output directory")
    site.mkdir(parents=True, exist_ok=True)
    cache = Path(config.get("paths", {}).get("cache", "data/cache"))
    if not cache.is_absolute():
        cache = ROOT / cache
    roads, roads_status = _optional_geojson(cache / "osm_roads.geojson")
    _write_site_json(site, "data/roads.json", roads)
    supplied = config.get("_runtime_metadata", {})
    metadata = {key: supplied[key] for key in (
        "generated_at", "corroborated_at", "interpretation", "input_audit", "live_sync", "external_data", "outputs"
    ) if key in supplied}
    metadata.setdefault("generated_at", datetime.now(timezone.utc).isoformat())
    metadata.update(page_generated_at=datetime.now(timezone.utc).isoformat(),
                    offline_roads_status=roads_status, offline_roads_count=len(roads["features"]))
    summaries = []
    features = events_geojson.get("features", [])
    # Bounded shards avoid tens of thousands of tiny files in the shareable pack.
    for start in range(0, len(features), 256):
        details = _clean(features[start:start + 256])
        relative = f"data/events/{start // 256}.json"
        for index, feature in enumerate(details):
            properties = feature.get("properties") or {}
            if "sentinel_preview" in properties:
                properties["sentinel_preview"] = _copy_preview(properties["sentinel_preview"], output, site)
            summaries.append({"type": "Feature", "geometry": feature.get("geometry"),
                "properties": {key: properties[key] for key in SUMMARY_FIELDS if key in properties},
                "detail": {"url": relative, "index": index}})
        _write_site_json(site, relative, {"type": "FeatureCollection", "features": details})
    downloads = []
    for filename in ("events.csv", "anomaly_rankings.csv", "sites.csv", "facilities.csv"):
        source = output / filename
        if source.is_file():
            if not source.resolve().is_relative_to(output.resolve()):
                raise ValueError(f"Saved export escapes the output directory: {filename}")
            relative = "downloads/" + filename
            shutil.copyfile(source, _site_file(site, relative))
            downloads.append({"name": filename, "url": relative})
    _write_site_json(site, "data/workspace.json", {
        "schema_version": 1, "events": {"type": "FeatureCollection", "features": summaries},
        "facilities": facilities_geojson, "roads_url": "data/roads.json", "aoi": config.get("aoi", {}),
        "metadata": metadata, "downloads": downloads,
    })
    for filename in ("viewer.js", "vendor/leaflet.js", "vendor/leaflet.css",
                     "vendor/Sora-Variable.ttf", "vendor/LEAFLET-LICENSE.txt", "vendor/SORA-LICENSE.txt"):
        shutil.copyfile(WEB / filename, _site_file(site, filename))
    font_style = ('@font-face{font-family:"Sora";font-style:normal;font-weight:100 900;font-display:swap;'
                  'src:url("vendor/Sora-Variable.ttf") format("truetype");}\n')
    _site_file(site, "style.css").write_text(font_style + (WEB / "style.css").read_text(encoding="utf-8"), encoding="utf-8")
    template = (WEB / "index.html").read_text(encoding="utf-8")
    replacements = {
        "<style>@@FONT_STYLE@@</style>": "",
        "<style>@@LEAFLET_STYLE@@</style>": '<link rel="stylesheet" href="vendor/leaflet.css">',
        "<style>@@APP_STYLE@@</style>": '<link rel="stylesheet" href="style.css">',
        '<script id="evidence-data" type="application/json">@@DATA@@</script>':
            '<script id="evidence-data" type="application/json" data-src="data/workspace.json"></script>',
        "<script>@@LEAFLET_SCRIPT@@</script>": '<script src="vendor/leaflet.js"></script>',
        "<script>@@APP_SCRIPT@@</script>": (
            '<script src="viewer.js" onerror="document.getElementById(\'loading-state\').textContent='
            '\'Viewer script is missing. Restore the complete site folder and reload.\'"></script>'
        ),
        "@@LEAFLET_LICENSE@@": "See vendor/LEAFLET-LICENSE.txt",
        "@@SORA_LICENSE@@": "See vendor/SORA-LICENSE.txt",
    }
    for marker, replacement in replacements.items():
        template = template.replace(marker, replacement)
    _site_file(site, "index.html").write_text(template, encoding="utf-8")
    return site


def write_offline_viewer(output_dir: Path, events_geojson: dict, facilities_geojson: dict, config: dict) -> None:
    """Write one self-contained index.html, with optional cached OSM vector roads."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache = Path(config.get("paths", {}).get("cache", "data/cache"))
    if not cache.is_absolute():
        cache = ROOT / cache
    roads, roads_status = _optional_geojson(cache / "osm_roads.geojson")
    sites, _ = _optional_geojson(output_dir / "sites.geojson")
    supplied = config.get("_runtime_metadata", {})
    metadata = {key: supplied[key] for key in (
        "generated_at", "corroborated_at", "interpretation", "input_audit", "live_sync", "external_data", "outputs"
    ) if key in supplied}
    metadata.setdefault("generated_at", datetime.now(timezone.utc).isoformat())
    metadata["page_generated_at"] = datetime.now(timezone.utc).isoformat()
    metadata["offline_roads_status"] = roads_status
    data = _clean({"events": events_geojson, "facilities": facilities_geojson,
        "roads": roads, "sites": sites, "aoi": config.get("aoi", {}), "metadata": metadata})
    output_root = output_dir.resolve()
    for feature in data["events"].get("features", []):
        properties = feature.get("properties", {})
        preview = properties.get("sentinel_preview")
        if isinstance(preview, str) and Path(preview).is_absolute():
            try:
                properties["sentinel_preview"] = Path(preview).resolve().relative_to(output_root).as_posix()
            except (OSError, ValueError):
                properties["sentinel_preview"] = None
    # A JSON script is still parsed by HTML first; escape delimiters at that boundary.
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    for raw, escaped in (("&", r"\u0026"), ("<", r"\u003c"), (">", r"\u003e"), ("\u2028", r"\u2028"), ("\u2029", r"\u2029")):
        payload = payload.replace(raw, escaped)
    font = base64.b64encode((WEB / "vendor/Sora-Variable.ttf").read_bytes()).decode("ascii")
    parts = {"DATA": payload, "FONT_STYLE": (
        '@font-face{font-family:"Sora";font-style:normal;font-weight:100 900;font-display:swap;'
        f'src:url(data:font/ttf;base64,{font}) format("truetype");}}'
    )}
    for key, filename in (("LEAFLET_STYLE", "vendor/leaflet.css"), ("APP_STYLE", "style.css"),
        ("LEAFLET_SCRIPT", "vendor/leaflet.js"), ("APP_SCRIPT", "viewer.js"),
        ("LEAFLET_LICENSE", "vendor/LEAFLET-LICENSE.txt"), ("SORA_LICENSE", "vendor/SORA-LICENSE.txt")):
        parts[key] = (WEB / filename).read_text(encoding="utf-8")
    template = (WEB / "index.html").read_text(encoding="utf-8")
    html = re.sub(r"@@([A-Z_]+)@@", lambda match: parts[match.group(1)], template)
    (output_dir / "index.html").write_text(html, encoding="utf-8")
