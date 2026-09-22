# Dataset-based localhost release

Approved scope: supplied datasets and saved results, online OpenStreetMap tiles,
offline roads and evidence, explicit local rebuild, no automatic data updates.

## Interfaces and ownership

- `app.py`: standard-library CLI: `serve`, `prepare`, `check`, `rebuild`, `package`.
  Default configuration is `config.json` beside the launcher, independent of cwd.
  `serve` hosts only `outputs/site`, binds to `127.0.0.1`, and never runs models.
- `release.py`: validate portable data manifests, build source/data ZIP packages,
  check required local inputs, and stage/publish a local rebuild with rollback.
- `viewer.write_web_viewer(output_dir, events_geojson, facilities_geojson, config)`:
  returns `output_dir/site`, containing the localhost site and its local assets.
  Existing single-file viewer export remains available.
- `config.json`: default local-only analysis; explicit archive coverage retained.
- `README.md`, `DATA_SOURCES.md`, `Dockerfile`, `compose.yaml`: installation,
  source/data distribution, data provenance, and optional containerized serving.

## Work and verification

1. Write behavioral tests for missing/corrupt datasets, wrong AOI cache,
   outside-root paths, server containment, saved-result startup, and publication
   rollback. Run `python -m unittest test_release test_app` before implementation.
2. Implement local preflight and an explicit outbound-network guard for rebuild.
   Rebuild under `runs/`, retaining the previous results on any failure.
   Validate event IDs, score bounds, counts, required outputs and context errors
   before publication. Reuse saved Sentinel evidence only through the existing
   event-identity checks.
3. Implement `prepare` and `serve` without importing scientific libraries.
   Prepare a lightweight localhost site from supplied result files. Serve local
   static files, gzip siblings when accepted, and no filesystem directories.
4. Implement separate source and data packages with portable SHA-256 manifests.
   Include exact source CSVs/metadata needed by configured sensors, required OSM
   and WorldCover files, saved results and available Sentinel evidence. Omit
   irrelevant catalog caches, scratch runs, git metadata and personal documents.
   Keep originals untouched.
5. Pin the tested scientific dependencies and document Python 3.12 setup.
   Serving supplied results needs only Python's standard library. Docker uses
   this same serving workflow and a mounted results directory.
6. Build actual ZIP artifacts, extract into a fresh location, run integrity
   checks and start the site from that location with no scientific dependencies.
   Exercise HTTP, gzip, relative image paths, online tile attempt and offline
   fallback in a real browser.
7. Run existing Python and JavaScript regression suites. Compare a full staged
   offline rebuild against the reference event IDs, classifications and scores;
   report any differences instead of replacing the reference silently.

## Acceptance criteria

- README instructions are sufficient to open supplied results on localhost.
- Offline analysis attempts no external acquisition.
- Online OSM tiles remain available; offline data and roads remain usable.
- Startup does not retrain, recompute, or silently download datasets.
- Dataset dates, known gaps and anomaly-score interpretation stay visible.
- Current files and reference scores remain preserved through verification.
- Source and data archives are ready to share; publication is a separate action.
