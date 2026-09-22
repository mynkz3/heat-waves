# Vegetation peer scoring

Approved scope: retain detected vegetation heat, fill accessible history gaps,
and rank isolated episodes without requiring same-site recurrence. Industrial
production-change/accident discrimination remains deferred.

## Two comparison methods

- Existing same-site scores retain precedence. Industrial fitting is unchanged.
- Vegetation episodes without a site score use comparable earlier detected
  vegetation episodes. A peer score does not make `history_sufficient` true:
  that field still describes the separate same-site baseline.
- Missing peers or features remain unscored with a specific reason. Context is
  retained; no missing score is replaced with zero.

## Reference eligibility

References must be science-quality observations inside the configured AOI,
with matching dominant vegetation land cover, VIIRS platform and day/night
phase. Each reference ends before the candidate's UTC day minus seven days.

Defaults: history within 2,557 days, seasonal day-of-year difference within
45 days, distance greater than 2 km and at most 100 km. Require at least
32 complete reference vectors, eight distinct dates and three 5-km cells.
Keep one deterministic episode per cell/day, then at most 2,000 references.
These are initial transparent guardrails, not incident-validated optima.

Use three primary-platform/phase measurements: peak FRP, maximum brightness
contrast and peak detection count. Duration and spread remain evidence but
are excluded from peer fitting because they aggregate secondary observations.
No incident labels, FIRMS type labels or hand-picked normal episodes are used.

## Score and provenance

Positive deviations from each reference median are scaled by MAD. When MAD
vanishes, use the larger of reference standard deviation, 5% of the median
magnitude, or a small numerical floor. Combine deviations with RMS, then
calculate a smoothed empirical midrank against reference distances.
Identical tied observations receive 0.5.

Exports record comparison method, reference counts/dates/cells, group,
feature deviations, and a reference hash. The map displays the active
comparison instead of an inactive same-site baseline.

## Limits

This is unusualness among detected heat episodes, not fire probability,
severity, calibrated confidence or confirmation. Equal scores from different
reference populations do not imply equal risk. Early years and sparse groups
can remain unscored. FIRMS CSVs do not supply observed non-fire opportunities.
WorldCover is fixed 2021 context; OSM is not a historical land-use survey.
Site discovery remains retrospective, so the complete system is not a
prospectively validated forecast.

The current acquisition includes both VIIRS platforms for 2019–2024.
Attempted public 2025 annual downloads returned HTTP 404; the 2026 rolling
feeds do not fill intervening months. Missing records are not no-fire evidence.

Skipping new Sentinel acquisition preserves previously analysed evidence
only for identical event IDs, coordinates and observation dates. Original
radiometry warnings remain; reused evidence does not alter the anomaly score.

## Verification

Run `python -m unittest discover -q` and `node --test test_viewer.js`.
The production browser check additionally exercises actual peer cards,
date/context filters, saved Sentinel previews, tooltip width, zoom/pan and
mobile layouts.
