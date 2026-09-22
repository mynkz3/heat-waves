# Data sources and scientific limitations

This document describes the supplied Singrauli–Sonebhadra snapshot. For exact
file hashes use `MANIFEST-data.json` from the data archive; for retained
observations, model parameters and counts use `outputs/run_manifest.json`.
Neither file establishes that a detected anomaly was an accident.

## Source inventory

| Source | Local form / role | Time represented |
| --- | --- | --- |
| NASA FIRMS VIIRS Suomi-NPP and NOAA-20 | Original CSVs and available `.meta.json` sidecars in `data/raw/firms/`; primary positive thermal detections | Science-quality 2019–2024; small September 2026 NRT snapshot |
| OpenStreetMap facilities | AOI-keyed Overpass JSON in `data/cache/`; industrial, power and quarry context | Cached OSM base timestamp `2026-07-15T15:22:01Z`, not historical event-date mapping |
| OpenStreetMap roads | `data/cache/osm_roads.geojson`; offline road geometry | Saved extract; do not assume the facility-cache timestamp also dates this extract |
| ESA WorldCover | 2021 v200 10 m land-cover GeoTIFFs in `data/cache/worldcover/` | Fixed 2021 context, not annual 2019–2026 land-cover history |
| Sentinel-2 L2A | Saved event-specific metadata, pre/post raster crops and evidence previews | Individual acquisition dates retained per event; 103 analysed pairs in the snapshot |
| OSM online tiles | Browser requests to `tile.openstreetmap.org`; optional visual basemap | Provider's displayed basemap; not a refresh of the analytic facility cache |

MODIS CSVs may exist in the working directory, but the configured accepted
sensors are `VIIRS_SNPP` and `VIIRS_NOAA20`. They are the sensors used for this
snapshot. Sentinel-1, Sentinel-5P, FIRMS static thermal anomalies, Planck/PINN
inversion and Lyapunov stages are **not active components** of this release.
There is no separate complete industrial-asset registry hidden behind OSM.

## FIRMS coverage and provenance

The retained AOI spans **2019-01-01 06:50 UTC to 2026-09-07 20:27 UTC**:
112,652 science-quality detections and 18 provisional NRT detections.
The requested year **2025 is absent for both accepted platforms**. 2026 is not
a continuous annual record. NRT files were retrieved on September 8–9, 2026;
retrieval time and the latest observation *inside the AOI* are different facts.

Original country/global CSV bytes are retained for reproducibility; the
pipeline filters them to the AOI rather than assuming every downloaded row
belongs to the pilot. Available metadata sidecars carry source URLs and
retrieval times. Some older files lack those sidecars: the run audit explicitly
retains missing provenance instead of inventing it. Row-level version fields
support the quality assignment; an absent file-level retrieval record does
not itself turn an archive row into NRT.

FIRMS CSVs contain positive detections, not a full cloud-free observation
opportunity mask. No row does **not** mean no fire, and recurrence here is
detection-conditioned. NRT records are excluded from fitting/calibration
histories. The supplied release does not schedule new downloads.

Source access and technical information:
[NASA FIRMS archive](https://firms.modaps.eosdis.nasa.gov/download/) and
[active-fire documentation](https://firms.modaps.eosdis.nasa.gov/content/active_fire/).
Acknowledge NASA FIRMS and the relevant VIIRS product/platform when presenting
derived results. Check product-specific terms and preserve citations; the
general [NASA Earthdata data-use guidance](https://www.earthdata.nasa.gov/engage/open-data-services-software/data-use-policy)
distinguishes NASA material from third-party material.

## OSM and WorldCover are context, not ground truth

The OSM facility cache contains 250 features for the configured bounding-box
query. Missing or incorrect map features can affect attribution; proximity
alone does not identify the burning asset. Quarry context is separated from
other industrial context. WorldCover's bare/sparse-vegetation class is not a
dedicated mine or industry class.

Applying present/snapshot OSM and fixed 2021 land cover to earlier or later
events can create temporal mismatches. The pipeline keeps these sources as
contextual evidence; it does not claim historically synchronized land use.

OSM data: **© OpenStreetMap contributors**, under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
Keep attribution and applicable ODbL notices with OSM extracts and derived
databases. Consult the [OSM copyright page](https://www.openstreetmap.org/copyright)
for redistribution obligations. Tile access has a separate
[usage policy](https://operations.osmfoundation.org/policies/tiles/); the release
does not prefetch or redistribute standard OSM raster tiles.

WorldCover: [ESA WorldCover 10 m 2021 v200](https://doi.org/10.5281/zenodo.7254221),
Zanaga et al. (2022), **CC BY 4.0**. Retain the acknowledgement:

> © ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium

See the [WorldCover data and citation page](https://esa-worldcover.org/en/data-access)
for the license and product manual.

## Sentinel-2 corroboration

The implemented catalog source is **Element 84 Earth Search**, not the
Copernicus Data Space STAC endpoint from the early architecture proposal.
The code queries L2A collections and reads available cloud-optimized assets.
See the [Earth Search provider documentation](https://github.com/Element84/earth-search).

The analysis compares cloud-masked pre/post NIR and SWIR crops around an
event, retaining scene IDs, acquisition dates, clear-pixel coverage and
radiometry metadata. Its dNBR is **pre-event NBR minus post-event NBR**.
Asset scale/offset metadata must be respected; legacy radiometry warnings
remain visible. Existing legacy results must not be upgraded to confirmed
burn evidence merely because their previews look convincing.

Only 103 of 35,963 episodes have analysed scene pairs in this snapshot.
The remainder were not acquired/analysed in the saved run. Missing imagery
is not contradictory evidence and is not “no fire.” Surface change can arise
from harvesting, excavation, vegetation condition or other causes; lack of
visible change cannot rule out a brief/small thermal event.

Local rebuilds preserve completed evidence only for matching event IDs,
coordinates and dates. They do not download new scenes or synthesize missing
evidence. Pre/post comparisons may use observations obtained after an event;
that is delayed corroboration, not advance prediction.

Copernicus Sentinel data are supplied under their
[data legal notice](https://cds.climate.copernicus.eu/licences/ec-sentinel).
Retain scene identifiers, source metadata and the appropriate
“Contains modified Copernicus Sentinel data” acknowledgement with the actual
acquisition year(s) when publishing derived images. The
[Copernicus Data Space terms](https://dataspace.copernicus.eu/terms-and-conditions)
distinguish Sentinel data from unrelated portal content; this package does not
grant rights to arbitrary web articles or news images.

## What can and cannot be concluded

The models fit without disaster/fire labels or hand-selected normal labels.
Site-history scoring uses eligible earlier observations; vegetation events
can use eligible spatial/seasonal peers rather than requiring recurrence at
the same site. Groups with insufficient comparison data abstain.

The saved scores are review rankings, not calibrated fire probabilities or
severity measures. A `0.95` threshold cannot be translated into “95% confidence
that this is a fire.” A doubling of recorded FRP can also reflect operational
or observation changes. This release does not establish the cause of that
change.

Historical source-site discovery uses the full available period, so the
historical run is exploratory, not a leakage-free prospective performance
estimate. The independent incident audit is incomplete; the earlier 99-alert
audit did not establish confirmed industrial asset fires. No accuracy,
precision, recall, or number of actual accidents is certified by this release.

## Redistribution and reproducibility

- Keep original CSVs, available sidecars, scene metadata, file hashes,
  attribution and radiometry warnings together.
- The data package deliberately omits unneeded catalog-download caches and
  scratch runs; the included files support the documented local rebuild and
  reuse of available evidence, not acquisition of new imagery.
- Preserve third-party notices under `frontend/vendor/` for Leaflet and the Sora
  font. Dataset licenses are separate from application-code licensing.
- Do not bundle copyrighted news text/images merely because an article was
  used as an audit lead. Link and cite the original source instead.
- Package integrity verifies delivered bytes, not scientific validity,
  complete coverage, or endorsement by NASA, ESA, Copernicus or OSM.
