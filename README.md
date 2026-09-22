# Thermal Sentinel — SIH 26162

A map dashboard for reviewing satellite heat detections, industrial and
vegetation context, anomaly rankings, and available Sentinel-2 evidence in
**Singrauli–Sonebhadra**.

> Research screening only. An anomaly score is not a fire probability or a
> confirmed accident.

## Run locally in 3 steps

You need **Python 3.12** and a modern browser. Viewing the supplied results
does **not** require `pip install`, Node.js, a database, API keys, or a GPU.

### 1. Get the project

Extract the supplied `heat-waves-source.zip`, or clone the deployable branch:

```console
git clone --branch results/sih26162-pilot https://github.com/mynkz3/heat-waves.git
cd heat-waves
```

If you used the ZIP, open a terminal in the extracted folder containing
`app.py`. Run the remaining commands from that folder.

### 2. Add the data package

Get **`heat-waves-data.zip` from the project maintainer**. It is supplied
separately and is **not included in the Git repository**.

Extract its contents into the project folder, alongside `app.py`:

```text
heat-waves/
├── app.py
├── config.json
├── backend/
├── frontend/
├── data/
└── outputs/
    └── site/
        └── index.html
```

Do not leave `data/` and `outputs/` nested inside a `heat-waves-data/` folder.
Use the source and data packages distributed together.

### 3. Start the dashboard

```console
python app.py check --mode view
python app.py serve --open
```

The second command starts the server and opens your browser. If it does not
open automatically, visit **http://127.0.0.1:8000/**.

Keep the terminal running. Press **Ctrl+C** to stop.
On macOS/Linux, use `python3` instead of `python` if needed.

**Do not double-click `index.html`.** Always use the localhost address.
Starting the dashboard does not train models or download new datasets.

## Do I need internet?

- **Online:** the map can display detailed OpenStreetMap street tiles.
- **Offline:** supplied results, filters, cached roads, and saved evidence
  remain available. Cached roads are not a complete offline street basemap.
- **No automatic updates:** connecting to the internet does not refresh
  thermal detections or retrain models.

## Which dates can I view?

The supplied snapshot has **35,963 observation episodes**, not 35,963
confirmed fires. It covers **2019–2024** and a short September 2026 snapshot.
**2025 is missing**; the latest observation is **7 September 2026, 20:27 UTC**.

Use **All time** or historical dates for the demo. Latest/week/month filters
may be empty because this is a saved dataset, not a live feed.

## Quick fixes

| Problem | What to do |
| --- | --- |
| Missing datasets or results | Extract the matching data ZIP beside `app.py`. A source-only clone is not enough. |
| “Prepared site missing” | If saved results are present, run `python app.py prepare`, then start the server again. This does not retrain models. |
| Port 8000 is busy | Run `python app.py serve --port 8080 --open`. |
| Blank page or blocked OSM tiles | Use the localhost URL, reload, and check your internet/privacy settings. Cached roads still work offline. |
| Need to verify downloaded files | Run `python app.py check --mode view --deep` to check supplied manifests and result consistency. |

## Optional: rebuilding, Docker, and development

You do not need these to open the supplied dashboard.

- [Deployment and developer guide](docs/DEPLOYMENT.md): rebuild from local
  datasets, Docker viewer, packaging, folder layout, and tests.
- [Data sources and limitations](docs/DATA_SOURCES.md): provenance, coverage,
  score interpretation, and attribution requirements.

For command options, run `python app.py --help`.
This server is intended for localhost use, not unprotected public hosting.
