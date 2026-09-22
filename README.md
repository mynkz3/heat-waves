# Thermal Sentinel — SIH 26162

A localhost GIS for reviewing satellite thermal detections in the
Singrauli–Sonebhadra pilot region. It separates mapped industrial, mining,
vegetation and unresolved contexts, then ranks unusual observation episodes.

**This is a research screening tool, not a confirmed-fire feed or an emergency
warning service. An anomaly score is not a fire probability.**

## Quick start: open the supplied results

Requirements: **Python 3.12** and a modern desktop browser. No Python packages,
database, Node.js, API key or GPU are required to view the packaged results.

1. Obtain both `heat-waves-source.zip` and `heat-waves-data.zip` from the project
   distributor. Extract **both into the same folder**. `app.py`, `config.json`,
   `data/` and `outputs/` must be siblings, not nested inside separate folders.
   A source-only checkout does not contain the datasets.
2. Open a terminal in that folder and run:

   ```console
   python app.py check --mode view --deep
   python app.py serve
   ```

3. Open **http://127.0.0.1:8000/**. Keep the terminal open; press `Ctrl+C` to stop.

On macOS/Linux, use `python3` if that is the name of your Python 3.12 executable.
The deep check verifies the delivered package hashes and can take longer than
an ordinary startup. Run `python app.py check --mode view` for a quicker check.

The data package includes a prepared `outputs/site/`. If you have the result
files but no prepared site, run:

```console
python app.py prepare
python app.py serve
```

`prepare` converts existing results into browser assets. It does **not** fit
models, download data or alter the saved event scores. Serving also never runs
the analysis.

## Online and offline operation

| Capability | Internet required? |
| --- | --- |
| View, filter and inspect packaged events, sites and evidence | No |
| Show packaged road geometry | No |
| Show detailed OpenStreetMap street-map tiles | Yes |
| Rebuild from a complete supplied dataset/cache folder | No |
| Obtain new satellite observations or updated facility context | Deferred; not automatic |

Online mode requests street tiles directly from OpenStreetMap in the browser.
If tiles fail or the browser is offline, local roads and result layers remain.
The fallback is **not** a complete offline copy of the OSM tiled basemap.

Use the localhost URL, not a double-clicked `index.html`. OSM requires a valid
web referrer and visible attribution. This release does not bulk-download OSM
tiles. Public tile servers are best-effort; larger public deployments need an
appropriate tile provider. See the [OSM tile policy](https://operations.osmfoundation.org/policies/tiles/).

There is no background synchronization, scheduler or automatic retraining.
Connecting to the internet changes the basemap availability, **not the age of
the thermal dataset**.

## What the supplied snapshot contains

These figures describe the saved `outputs/run_manifest.json`, generated on
**2026-09-09**, not a live evaluation:

| Item | Saved snapshot |
| --- | ---: |
| Retained thermal detections | 112,670 |
| Observation episodes | 35,963 |
| Persistent source sites | 4,985 |
| Episodes with an anomaly score | 29,330 |
| Episodes without a score | 6,633 |
| OSM contextual features | 250 |
| Episodes with analysed Sentinel-2 pairs | 103 |

The pilot uses bounding box `[81.5, 23.2, 83.5, 25.2]` in
`[west, south, east, north]` order, not exact district boundaries.
Science-quality FIRMS records cover **2019–2024**. **2025 is missing**.
2026 contains only a short September NRT snapshot; it is not a complete year.
The latest retained detection inside the pilot is **2026-09-07 20:27 UTC**.
Recent-date filters can therefore correctly be empty. Use the all-time view
or historical dates when demonstrating this snapshot.

Read [DATA_SOURCES.md](DATA_SOURCES.md) for provenance, timestamps and caveats.

## How to interpret the map

- A detection means FIRMS reported a thermal hotspot, not that a particular
  industrial asset burned.
- Context describes nearby mapped land cover/facilities. Mining and quarry
  context is kept separate from other industrial context.
- Scores rank unusual observations against eligible historical comparisons.
  The configured `0.95` cutoff is a **review threshold**, not “95% chance of
  fire.” Low scores do not establish safety.
- A missing score means the model abstained: for example, insufficient site
  history or too few comparable vegetation peers. The detected heat and its
  known context remain visible.
- Sentinel pre/post change is supporting evidence, with acquisition dates,
  cloud limitations and radiometry warnings; it does not confirm the cause.

The snapshot has 19 suspected non-mining industrial episodes and 166 suspected
mining-associated episodes. These are **not counts of confirmed accidents**.
The independent incident audit is incomplete; no validated precision, recall
or accuracy is claimed.

## Rebuild the analysis from local datasets

Viewing is the default workflow. Rebuilding is an explicit, slower operation
that requires the scientific dependencies and the full local data package.
Install these in a virtual environment.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py check --mode rebuild --deep
.\.venv\Scripts\python.exe app.py rebuild
```

macOS/Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py check --mode rebuild --deep
.venv/bin/python app.py rebuild
```

Installing packages initially requires internet unless you supply a compatible
local wheel cache. The rebuild itself uses local inputs and rejects outbound
acquisition. It requires matching OSM context and WorldCover caches. It reuses
saved Sentinel evidence only when event identity, coordinates and observation
dates match; it does not acquire new Sentinel scenes.

By default, the rebuild writes to `runs/rebuild-.../outputs/` and reports a
comparison with the existing results. **It does not replace the dashboard.**
Review the comparison and run manifest first. To perform a rebuild and publish
its validated outputs explicitly, use the virtual environment's Python:

```console
python app.py rebuild --publish
```

Publication preserves the previous output directory under the rebuild run.
A failed rebuild must not replace the last successful results. Keep enough
free disk space for the input package, staged outputs and previous results.
Runtime depends on CPU, storage and input size; no fixed completion time is
guaranteed.

The core workflow is:

```text
local FIRMS → validation/deduplication → event/site discovery
           → OSM + WorldCover context
           → historical site or vegetation-peer anomaly scoring
           → saved Sentinel evidence → GIS exports
```

Fitting uses no disaster/fire labels or hand-selected “normal” examples.
Historical source-site discovery is retrospective, so this release is not a
fully prospective validation of future-fire detection.

## Commands and files

Run `python app.py --help` or `python app.py <command> --help` for options.
Options follow the command; for example:

```console
python app.py serve --port 8080
python app.py check --mode rebuild --config config.json
python app.py package --kind all
```

| Command | Purpose |
| --- | --- |
| `serve` | Host only the prepared site, default `127.0.0.1:8000` |
| `prepare` | Prepare browser assets from saved result files |
| `check --mode view` | Check the saved-result deployment |
| `check --mode rebuild` | Check required local analysis inputs |
| `rebuild` | Run local analysis into a new staging directory |
| `rebuild --publish` | Rebuild and explicitly publish validated results |
| `package --kind source` | Create the source-code ZIP |
| `package --kind data` | Create the local dataset/result ZIP |
| `package --kind all` | Create both ZIPs |

```text
app.py, release.py           Local launcher, checks and packaging
config.json                 Pilot, input paths and model parameters
pipeline.py                 Analysis and export stages
data_sources.py             FIRMS ingestion and source metadata
peer_scoring.py              Vegetation-peer scoring
sentinel_evidence.py         Sentinel-2 corroboration implementation
viewer.py, web/              Viewer generation and local frontend assets
data/raw/firms/              Original CSVs and available metadata sidecars
data/cache/                 Required contextual data and saved evidence
outputs/                    Saved analysis tables, GIS layers and metadata
outputs/site/               Only directory exposed by the localhost server
runs/                       Staged rebuilds and retained previous outputs
dist/                       Shareable source and data archives
```

Packages are `dist/heat-waves-source.zip` and `dist/heat-waves-data.zip`.
They include `MANIFEST-source.json` and `MANIFEST-data.json`, respectively,
with file hashes. Distribute both archives together. The data archive excludes
unneeded download-catalog caches and the old large single-file HTML export.
Packaging does not upload or publish anything.

Manifests describe the delivered snapshot. After intentionally modifying or
rebuilding its files, old hashes may no longer match: review the changes and
create a new package instead of treating an old manifest as a new certificate.

## Optional Docker viewer

Docker is optional and serves **already prepared results only**. It does not
install the scientific stack or rebuild models. These container files were
not runtime-tested in the development environment because Docker was absent.

With Docker and Compose installed, extract the packages and ensure
`outputs/site/index.html` exists, then run:

```console
docker compose up --build
```

Open http://127.0.0.1:8000/. Stop with `Ctrl+C`, then `docker compose down`.
The first build needs internet for the Python image. The site is mounted
read-only; the host port binds to localhost. Run preparation/rebuilding on the
host before restarting the container to serve changed outputs.

This is a local demonstration server, not a hardened public hosting service.
Do not expose it to untrusted networks without adding appropriate hosting,
authentication and operational controls.

## Troubleshooting

- **Missing files:** extract the data ZIP beside `app.py`, not into an extra
  nested directory. Run `check` again; do not run a rebuild merely to view.
- **Hash mismatch:** re-extract the matching release into a fresh folder.
  Do not mix datasets and source archives from unrelated releases.
- **Blank or stale map:** use the localhost URL, confirm the server is running,
  and reload the page. Run `prepare` if site assets are absent or intentionally
  stale after changing saved results.
- **OSM “referer required” or blocked tiles:** use localhost rather than
  `file://`; check browser extensions/privacy rules. Keep the local-road
  fallback available rather than bypassing the provider's policy.
- **Empty latest/last-year view:** inspect dataset dates and coverage gaps.
  An empty view does not establish that no fire occurred.
- **Port already used:** `python app.py serve --port 8080`.
- **Missing local context during rebuild:** restore the matching dataset/cache
  package. The offline rebuild does not silently download replacements.

For development, Python tests use `python -m unittest discover`; JavaScript
helper tests use Node.js via `node --test test_viewer.js`. Browser integration
checks additionally require an installed supported browser.

Data and bundled third-party assets retain their own attribution/license
requirements; see [DATA_SOURCES.md](DATA_SOURCES.md) and the license files in
`web/vendor/`.
