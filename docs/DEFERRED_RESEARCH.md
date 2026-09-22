# Deferred research — industrial heat versus incidents

Recorded 2026-09-09. User explicitly deferred implementation.

The current industrial anomaly score measures unusual heat, not the cause of
that heat. A legitimate production increase can still trigger industrial review.

Potential future work:

- Test recurring observed operating-pattern models against the robust site
  baseline, using only earlier observations. Recurrence is not proof of safety.
- Separate thermal, spatial, temporal, optical and operational evidence.
  Correlated FRP features are not independent confirmations.
- Improve Sentinel comparisons with appropriate dates, land-cover-matched
  surrounding controls, alignment and local quality checks. Surface change is
  not necessarily fire; missing evidence must not suppress an early warning.
- Keep anomaly priority, corroboration and independently verified incident
  status separate. Plant/maintenance/incident records are optional evidence,
  not assumed available datasets.
- Evaluate on held-out time periods and an independent audit set that includes
  lower-ranked cases. No news report is not a verified negative.
- Observation-normalised recurrence requires additional valid-observation
  information; FIRMS positive-detection CSVs alone do not supply it.

Current authorised work is vegetation peer-comparison scoring and FIRMS
coverage repair. Do not implement the proposals above as part of that work.
