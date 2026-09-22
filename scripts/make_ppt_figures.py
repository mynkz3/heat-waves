from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


FIGURE_NAMES = (
    "01_pipeline_counts.png",
    "02_monthly_detections.png",
    "03_decision_distribution.png",
    "04_event_map.png",
    "05_anomaly_scores.png",
    "06_clustering_stability.png",
    "07_firms_silver_agreement.png",
)
DECISION_LABELS = {
    "suspected_abnormal_industrial_associated_event": "Unusual, industrial context",
    "routine_persistent_industrial_thermal_source": "Recurring industrial heat",
    "industrial_associated_unscored": "Industrial context, unscored",
    "suspected_abnormal_mining_associated_event": "Unusual, mining/quarry context",
    "mining_associated_thermal_source": "Recurring mining/quarry heat",
    "mining_associated_unscored": "Mining/quarry context, unscored",
    "likely_vegetation_fire": "Vegetation-associated heat",
    "mixed_or_unknown": "Mixed or unknown",
}
DECISION_COLORS = {
    "suspected_abnormal_industrial_associated_event": "#C23B22",
    "routine_persistent_industrial_thermal_source": "#19735B",
    "industrial_associated_unscored": "#7F9EAA",
    "suspected_abnormal_mining_associated_event": "#B6672B",
    "mining_associated_thermal_source": "#927444",
    "mining_associated_unscored": "#B7A178",
    "likely_vegetation_fire": "#7DA33D",
    "mixed_or_unknown": "#8A8071",
}
BLUE = "#245A7A"
GOLD = "#D69E2E"


plt.rcParams.update(
    {
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "axes.edgecolor": "#333333",
        "axes.titleweight": "bold",
        "axes.titlesize": 15,
        "axes.labelsize": 11,
        "font.size": 10,
        "legend.frameon": False,
        "grid.color": "#D8D8D8",
        "grid.linewidth": 0.7,
        "savefig.facecolor": "white",
    }
)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}


def read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path, low_memory=False)
    except (FileNotFoundError, OSError, UnicodeError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame()


def figure(title: str, columns: int = 1) -> tuple[plt.Figure, Any]:
    fig, axes = plt.subplots(1, columns, figsize=(10, 5.625))
    fig.suptitle(title, x=0.06, y=0.98, ha="left", fontsize=18, fontweight="bold")
    return fig, axes


def no_data(ax: plt.Axes, message: str = "No data available") -> None:
    ax.text(0.5, 0.5, message, ha="center", va="center", color="#666666", fontsize=14)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)


def save(fig: plt.Figure, path: Path) -> None:
    fig.tight_layout(rect=(0.02, 0.02, 0.98, 0.93))
    fig.savefig(path, dpi=300, bbox_inches="tight", pad_inches=0.18)
    plt.close(fig)


def number(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


def pipeline_counts(
    manifest: dict[str, Any], frames: dict[str, pd.DataFrame], destination: Path
) -> None:
    reported = manifest.get("outputs", {})
    labels = ["Detections", "Events", "Persistent sites", "Mapped facilities"]
    values = [
        number(reported.get("detections"), len(frames["detections"])),
        number(reported.get("events"), len(frames["events"])),
        number(reported.get("persistent_sites"), len(frames["sites"])),
        number(reported.get("osm_context_features"), len(frames["facilities"])),
    ]
    fig, ax = figure("Pipeline output at a glance")
    bars = ax.barh(labels[::-1], values[::-1], color=["#8A8071", GOLD, "#4C956C", BLUE])
    maximum = max(values, default=0)
    ax.set_xlim(0, max(1, maximum * 1.18))
    ax.set_xlabel("Records")
    ax.grid(axis="x")
    ax.set_axisbelow(True)
    for bar, value in zip(bars, values[::-1]):
        ax.text(bar.get_width() + max(maximum * 0.015, 0.02), bar.get_y() + bar.get_height() / 2,
                f"{value:,}", va="center", fontweight="bold")
    ax.set_title("Counts reported in the run manifest", loc="left", fontsize=11, fontweight="normal")
    save(fig, destination)


def monthly_detections(detections: pd.DataFrame, destination: Path) -> None:
    fig, ax = figure("Monthly thermal detections")
    if "acquired_at" not in detections:
        no_data(ax)
        save(fig, destination)
        return
    work = detections.copy()
    work["_date"] = pd.to_datetime(work["acquired_at"], errors="coerce", utc=True)
    work = work.dropna(subset=["_date"])
    if work.empty:
        no_data(ax)
        save(fig, destination)
        return
    work["_month"] = work["_date"].dt.strftime("%Y-%m")
    work["_sensor"] = work.get("sensor", pd.Series("All detections", index=work.index)).fillna("Unknown")
    monthly = work.groupby(["_month", "_sensor"]).size().unstack(fill_value=0).sort_index()
    palette = plt.cm.tab10(np.linspace(0, 1, max(1, len(monthly.columns))))
    for color, sensor in zip(palette, monthly.columns):
        ax.plot(monthly.index, monthly[sensor], marker="o", linewidth=2.2, label=str(sensor), color=color)
    ax.set_ylabel("Detection count")
    ax.set_xlabel("Acquisition month (UTC)")
    ax.tick_params(axis="x", rotation=45)
    ax.grid(axis="y")
    ax.set_axisbelow(True)
    if len(monthly.columns) > 1:
        ax.legend(title="Sensor", ncol=min(3, len(monthly.columns)))
    ax.set_title("Seasonality and sensor coverage", loc="left", fontsize=11, fontweight="normal")
    save(fig, destination)


def decision_distribution(events: pd.DataFrame, destination: Path) -> None:
    fig, ax = figure("Decision distribution")
    if "decision" not in events or events["decision"].dropna().empty:
        no_data(ax)
        save(fig, destination)
        return
    counts = events["decision"].fillna("mixed_or_unknown").value_counts()
    order = [key for key in DECISION_LABELS if key in counts]
    order.extend(key for key in counts.index if key not in order)
    values = [int(counts[key]) for key in order]
    labels = [DECISION_LABELS.get(key, str(key).replace("_", " ").title()) for key in order]
    colors = [DECISION_COLORS.get(key, "#607D8B") for key in order]
    bars = ax.barh(labels[::-1], values[::-1], color=colors[::-1])
    total = sum(values)
    maximum = max(values)
    ax.set_xlim(0, max(1, maximum * 1.25))
    ax.set_xlabel("Events")
    ax.grid(axis="x")
    ax.set_axisbelow(True)
    for bar, value in zip(bars, values[::-1]):
        percent = 100 * value / total if total else 0
        ax.text(bar.get_width() + max(maximum * 0.015, 0.02), bar.get_y() + bar.get_height() / 2,
                f"{value:,}  ({percent:.1f}%)", va="center", fontweight="bold")
    ax.set_title("Transparent rule-based output classes", loc="left", fontsize=11, fontweight="normal")
    save(fig, destination)


def event_map(
    manifest: dict[str, Any], events: pd.DataFrame, facilities: pd.DataFrame, destination: Path
) -> None:
    fig, ax = figure("Thermal events and mapped industrial context")
    if not {"latitude", "longitude"}.issubset(events.columns):
        no_data(ax)
        save(fig, destination)
        return
    work = events.copy()
    work["_lat"] = pd.to_numeric(work["latitude"], errors="coerce")
    work["_lon"] = pd.to_numeric(work["longitude"], errors="coerce")
    work = work.dropna(subset=["_lat", "_lon"])
    if work.empty:
        no_data(ax)
        save(fig, destination)
        return
    decisions = work.get("decision", pd.Series("mixed_or_unknown", index=work.index)).fillna("mixed_or_unknown")
    detection_count = work.get("detection_count", pd.Series(1, index=work.index))
    sizes = 35 + 24 * np.log1p(pd.to_numeric(detection_count, errors="coerce").fillna(1).clip(lower=1))
    for decision in decisions.unique():
        mask = decisions.eq(decision)
        ax.scatter(
            work.loc[mask, "_lon"], work.loc[mask, "_lat"], s=sizes.loc[mask],
            color=DECISION_COLORS.get(str(decision), "#607D8B"), alpha=0.82,
            edgecolor="white", linewidth=0.6, label=DECISION_LABELS.get(str(decision), str(decision).replace("_", " ").title()),
        )
    if {"latitude", "longitude"}.issubset(facilities.columns):
        facility_lat = pd.to_numeric(facilities["latitude"], errors="coerce")
        facility_lon = pd.to_numeric(facilities["longitude"], errors="coerce")
        valid = facility_lat.notna() & facility_lon.notna()
        if valid.any():
            ax.scatter(facility_lon[valid], facility_lat[valid], marker="X", s=85, color="#222222",
                       edgecolor="white", linewidth=0.7, label="Mapped facility", zorder=5)
    bbox = manifest.get("aoi", {}).get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4:
        try:
            west, south, east, north = map(float, bbox)
            ax.set_xlim(west, east)
            ax.set_ylim(south, north)
        except (TypeError, ValueError):
            pass
    mean_latitude = float(work["_lat"].mean())
    ax.set_aspect(1 / max(math.cos(math.radians(mean_latitude)), 0.2))
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.grid()
    ax.set_axisbelow(True)
    ax.legend(loc="best", fontsize=8)
    ax.set_title("Circle size reflects detections per event; no online basemap", loc="left", fontsize=11,
                 fontweight="normal")
    save(fig, destination)


def anomaly_scores(events: pd.DataFrame, destination: Path) -> None:
    fig, axes = figure("Anomaly score evidence", columns=2)
    if "anomaly_score" not in events:
        for ax in axes:
            no_data(ax)
        save(fig, destination)
        return
    scores = pd.to_numeric(events["anomaly_score"], errors="coerce")
    valid = scores.notna()
    if not valid.any():
        for ax in axes:
            no_data(ax, "Scores unavailable; see event reasons")
        save(fig, destination)
        return
    axes[0].hist(scores[valid].clip(0, 1), bins=np.linspace(0, 1, 11), color=BLUE, edgecolor="white")
    axes[0].set_xlim(0, 1)
    axes[0].set_xlabel("Anomaly score (rank, not probability)")
    axes[0].set_ylabel("Scored events")
    axes[0].grid(axis="y")
    axes[0].set_title("Score distribution")

    dates = pd.to_datetime(events.get("start_time", pd.Series(index=events.index, dtype=object)),
                           errors="coerce", utc=True)
    x = dates if dates.notna().any() else pd.Series(np.arange(len(events)), index=events.index)
    decisions = events.get("decision", pd.Series("mixed_or_unknown", index=events.index)).fillna("mixed_or_unknown")
    for decision in decisions[valid].unique():
        mask = valid & decisions.eq(decision)
        axes[1].scatter(x[mask], scores[mask], s=34, alpha=0.8,
                        color=DECISION_COLORS.get(str(decision), "#607D8B"),
                        label=DECISION_LABELS.get(str(decision), str(decision).replace("_", " ").title()))
    axes[1].set_ylim(-0.03, 1.03)
    axes[1].set_ylabel("Anomaly score")
    axes[1].set_xlabel("Event time" if dates.notna().any() else "Event index")
    axes[1].grid(axis="y")
    axes[1].set_title(f"{int(valid.sum()):,} scored; {int((~valid).sum()):,} unavailable")
    axes[1].legend(fontsize=7, loc="best")
    save(fig, destination)


def clustering_stability(tuning: dict[str, Any], destination: Path) -> None:
    fig, axes = figure("Label-free clustering stability", columns=2)
    event_trials = pd.DataFrame(tuning.get("event_trials", []))
    event_trials = event_trials.rename(columns={"bootstrap_ari": "subsample_ari"})
    required = {"event_spatial_km", "event_temporal_hours", "subsample_ari"}
    if required.issubset(event_trials.columns) and not event_trials.empty:
        for column in required:
            event_trials[column] = pd.to_numeric(event_trials[column], errors="coerce")
        event_trials = event_trials.dropna(subset=list(required))
    if required.issubset(event_trials.columns) and not event_trials.empty:
        grid = event_trials.pivot_table(index="event_temporal_hours", columns="event_spatial_km",
                                        values="subsample_ari", aggfunc="max").sort_index(ascending=False)
        image = axes[0].imshow(grid.to_numpy(), cmap="YlGnBu", vmin=0, vmax=1, aspect="auto")
        axes[0].set_xticks(range(len(grid.columns)), [f"{value:g}" for value in grid.columns])
        axes[0].set_yticks(range(len(grid.index)), [f"{value:g}" for value in grid.index])
        axes[0].set_xlabel("Spatial radius (km)")
        axes[0].set_ylabel("Temporal span (hours)")
        axes[0].set_title("Event 80% subsample ARI")
        for row in range(grid.shape[0]):
            for column in range(grid.shape[1]):
                value = grid.iloc[row, column]
                if pd.notna(value):
                    axes[0].text(column, row, f"{value:.2f}", ha="center", va="center",
                                 color="white" if value > 0.62 else "#222222", fontweight="bold")
        fig.colorbar(image, ax=axes[0], fraction=0.046, pad=0.04, label="Adjusted Rand index")
    else:
        no_data(axes[0], "Event tuning report unavailable")

    site_trials = pd.DataFrame(tuning.get("site_trials", []))
    site_trials = site_trials.rename(columns={"bootstrap_ari": "subsample_ari"})
    site_required = {"site_max_diameter_km", "subsample_ari"}
    if site_required.issubset(site_trials.columns) and not site_trials.empty:
        x = pd.to_numeric(site_trials["site_max_diameter_km"], errors="coerce")
        y = pd.to_numeric(site_trials["subsample_ari"], errors="coerce")
        valid = x.notna() & y.notna()
        axes[1].plot(x[valid], y[valid], marker="o", linewidth=2.3, color=BLUE, label="Bootstrap ARI")
        if "event_coverage" in site_trials:
            coverage = pd.to_numeric(site_trials["event_coverage"], errors="coerce")
            axes[1].plot(x[valid], coverage[valid], marker="s", linewidth=2, color=GOLD,
                         label="Event coverage")
        axes[1].set_ylim(0, 1.03)
        axes[1].set_xlabel("Maximum site diameter (km)")
        axes[1].set_ylabel("Fraction / score")
        axes[1].set_title("Persistent-site sensitivity")
        axes[1].grid(axis="y")
        axes[1].legend()
    else:
        no_data(axes[1], "Site tuning report unavailable")
    fig.text(0.06, 0.01, "Stability measures reproducibility under resampling, not classification correctness.",
             color="#555555", fontsize=9)
    save(fig, destination)


def silver_result(events: pd.DataFrame, detections: pd.DataFrame) -> dict[str, Any]:
    empty = {"matrix": None, "eligible": 0, "total": len(events), "agreed": 0}
    if "event_id" not in events or not {"event_id", "firms_type_reference"}.issubset(detections.columns):
        return empty
    references = detections[["event_id", "firms_type_reference"]].copy()
    references["firms_type_reference"] = pd.to_numeric(references["firms_type_reference"], errors="coerce")
    references = references[references["firms_type_reference"].isin([0, 2])]
    if references.empty:
        return empty
    grouped = references.groupby("event_id")["firms_type_reference"]
    audit = pd.DataFrame(
        {
            "n_ref": grouped.size(),
            "type0_share": grouped.apply(lambda values: float(values.eq(0).mean())),
            "type2_share": grouped.apply(lambda values: float(values.eq(2).mean())),
        }
    )
    audit["silver"] = pd.NA
    audit.loc[(audit["n_ref"] >= 2) & (audit["type2_share"] >= 0.8), "silver"] = "Static source (Type 2)"
    audit.loc[(audit["n_ref"] >= 2) & (audit["type0_share"] >= 0.8), "silver"] = "Vegetation (Type 0)"

    event_context = events.drop_duplicates("event_id").set_index("event_id")
    if "context_label" in event_context:
        context = event_context["context_label"].astype(str)
        prediction = context.map(
            {"industrial_associated": "Industrial", "vegetation_associated": "Vegetation"}
        ).fillna("Abstain / mixed")
    elif "decision" in event_context:
        prediction = event_context["decision"].astype(str).map(
            {
                "suspected_abnormal_industrial_associated_event": "Industrial",
                "routine_persistent_industrial_thermal_source": "Industrial",
                "industrial_associated_unscored": "Industrial",
                "suspected_abnormal_mining_associated_event": "Industrial",
                "mining_associated_thermal_source": "Industrial",
                "mining_associated_unscored": "Industrial",
                "likely_vegetation_fire": "Vegetation",
            }
        ).fillna("Abstain / mixed")
    else:
        return empty
    audit = audit.join(prediction.rename("prediction"), how="inner")
    eligible = audit.dropna(subset=["silver"])
    if eligible.empty:
        return empty
    rows = ["Static source (Type 2)", "Vegetation (Type 0)"]
    columns = ["Industrial", "Vegetation", "Abstain / mixed"]
    matrix = pd.crosstab(eligible["silver"], eligible["prediction"]).reindex(
        index=rows, columns=columns, fill_value=0
    )
    agreed = int(matrix.loc["Static source (Type 2)", "Industrial"] +
                 matrix.loc["Vegetation (Type 0)", "Vegetation"])
    return {"matrix": matrix, "eligible": len(eligible), "total": event_context.index.nunique(), "agreed": agreed}


def firms_silver_agreement(result: dict[str, Any], destination: Path) -> None:
    fig, ax = figure("Held-out FIRMS type agreement (context only)")
    matrix = result["matrix"]
    if matrix is None:
        no_data(ax, "No eligible type-coded events")
        fig.text(0.06, 0.02, "Requires at least two type-coded detections with 80% event-level purity.",
                 color="#555555", fontsize=9)
        save(fig, destination)
        return
    values = matrix.to_numpy(dtype=float)
    image = ax.imshow(values, cmap="Blues", vmin=0, vmax=max(1, float(values.max())), aspect="auto")
    ax.set_xticks(range(len(matrix.columns)), matrix.columns)
    ax.set_yticks(range(len(matrix.index)), matrix.index)
    ax.set_xlabel("Pipeline context")
    ax.set_ylabel("Withheld FIRMS reference")
    ax.set_title(
        f"{result['agreed']}/{result['eligible']} eligible events agree; "
        f"coverage {result['eligible']}/{result['total']}"
    )
    for row in range(values.shape[0]):
        row_total = values[row].sum()
        for column in range(values.shape[1]):
            value = int(values[row, column])
            percent = 100 * value / row_total if row_total else 0
            ax.text(column, row, f"{value}\n{percent:.0f}%", ha="center", va="center",
                    color="white" if values[row, column] > values.max() * 0.55 else "#222222",
                    fontweight="bold")
    fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04, label="Eligible events")
    fig.text(0.06, 0.02, "FIRMS type is consulted only after prediction; agreement is not incident truth.",
             color="#555555", fontsize=9)
    save(fig, destination)


def summary_text(
    manifest: dict[str, Any], frames: dict[str, pd.DataFrame], tuning: dict[str, Any],
    silver: dict[str, Any], figure_dir: Path
) -> str:
    reported = manifest.get("outputs", {})
    counts = {
        "Detections": number(reported.get("detections"), len(frames["detections"])),
        "Events": number(reported.get("events"), len(frames["events"])),
        "Persistent sites": number(reported.get("persistent_sites"), len(frames["sites"])),
        "Mapped facilities": number(reported.get("osm_context_features"), len(frames["facilities"])),
    }
    detections = frames["detections"]
    dates = pd.to_datetime(detections.get("acquired_at", pd.Series(dtype=object)), errors="coerce", utc=True)
    date_line = "unavailable"
    if dates.notna().any():
        date_line = f"{dates.min().date().isoformat()} to {dates.max().date().isoformat()} (UTC)"

    events = frames["events"]
    decisions = events.get("decision", pd.Series(dtype=object)).dropna().value_counts()
    decision_lines = [
        f"  - {DECISION_LABELS.get(str(key), str(key).replace('_', ' ').title())}: {int(value):,}"
        for key, value in decisions.items()
    ] or ["  - Unavailable"]
    scores = pd.to_numeric(events.get("anomaly_score", pd.Series(dtype=float)), errors="coerce")
    score_line = "unavailable"
    if scores.notna().any():
        score_line = f"{int(scores.notna().sum()):,}/{len(events):,} events scored; median {scores.median():.3f}"

    event_trials = pd.DataFrame(tuning.get("event_trials", []))
    event_trials = event_trials.rename(columns={"bootstrap_ari": "subsample_ari"})
    stability_line = "Clustering stability: unavailable (tuning_report.json missing or empty)."
    if {"subsample_ari", "event_spatial_km", "event_temporal_hours"}.issubset(event_trials.columns):
        ari = pd.to_numeric(event_trials["subsample_ari"], errors="coerce")
        if ari.notna().any():
            best = event_trials.loc[ari.idxmax()]
            stability_line = (
                f"Clustering stability: highest tested 80% subsample ARI {float(ari.max()):.3f} at "
                f"{best['event_spatial_km']} km / {best['event_temporal_hours']} hours."
            )

    if silver["eligible"]:
        percent = 100 * silver["agreed"] / silver["eligible"]
        silver_line = (
            f"FIRMS silver agreement: {silver['agreed']}/{silver['eligible']} eligible events agreed "
            f"({percent:.1f}%); {silver['eligible']}/{silver['total']} events eligible."
        )
    else:
        silver_line = "FIRMS silver agreement: unavailable (no eligible post-hoc type-coded events)."

    figures = "\n".join(f"- `{figure_dir.name}/{name}`" for name in FIGURE_NAMES)
    return f"""# SIH26162 results summary

## Run coverage

{chr(10).join(f'- {label}: {value:,}' for label, value in counts.items())}
- Detection date range: {date_line}
- Anomaly-score coverage: {score_line}

## Decisions

{chr(10).join(decision_lines)}

## Evaluation evidence

- {stability_line}
- {silver_line}

## Interpretation limits

- These are thermal observation episodes, not confirmed fires. Anomaly ranks are not fire probabilities, and a low or missing score cannot rule out fire.
- Mining/quarry context is separated using explicit mapped quarry features, not inferred from bare land. Context and score availability are separate attributes.
- FIRMS type is excluded from model features and decisions, then used post-hoc. The reported quantity is agreement, not an accuracy estimate.
- Type 0/2 is a coarse silver reference for vegetation versus static heat; it does not establish an industrial incident or distinguish abnormal from routine activity.
- OSM, land-cover and satellite corroboration are evidence inputs, not ground truth.
- Missing fields and absent tuning results are reported as unavailable rather than imputed.

## Generated figures

{figures}
"""


def generate(root: Path) -> Path:
    root = root.resolve()
    outputs = root / "outputs"
    generated = root / "reports" / "generated"
    figure_dir = generated / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)

    manifest = read_json(outputs / "run_manifest.json")
    tuning = read_json(outputs / "tuning_report.json")
    frames = {
        "events": read_csv(outputs / "events.csv"),
        "detections": read_csv(outputs / "detections.csv"),
        "sites": read_csv(outputs / "sites.csv"),
        "facilities": read_csv(outputs / "facilities.csv"),
    }
    destinations = [figure_dir / name for name in FIGURE_NAMES]
    pipeline_counts(manifest, frames, destinations[0])
    monthly_detections(frames["detections"], destinations[1])
    decision_distribution(frames["events"], destinations[2])
    event_map(manifest, frames["events"], frames["facilities"], destinations[3])
    anomaly_scores(frames["events"], destinations[4])
    clustering_stability(tuning, destinations[5])
    silver = silver_result(frames["events"], frames["detections"])
    firms_silver_agreement(silver, destinations[6])

    summary = generated / "results_summary.md"
    summary.write_text(summary_text(manifest, frames, tuning, silver, figure_dir), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate SIH26162 PPT-ready result figures")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    summary = generate(args.root)
    print(f"Generated figures and summary: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
