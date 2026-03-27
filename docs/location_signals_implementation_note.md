# Location Signals v1 — Implementation Note

## Architecture and cache strategy

- Added `api/location_signals.py` with a dedicated orchestration layer for directional location signals.
- Implemented a shared, database-backed cache (`location_signal_cache`) via `DatabaseLocationSignalCache`.
- Cache keys are deterministic (`location-signals:v1:{lat_3dp}:{lon_3dp}`), with TTL-based expiry and configurable timeout/TTL.
- Startup now ensures the cache table exists.

## Crime benchmark method

- Uses Police API data for the latest available month.
- Computes subject retail-relevant count at the subject coordinate.
- Computes benchmark counts at 8 deterministic offset points around the subject.
- Uses median benchmark retail-relevant count and classifies by subject/median ratio.

## Crime thresholds (conservative v1)

- `low`: ratio `< 1.15`
- `moderate`: `1.15 <= ratio < 1.5`
- `elevated`: ratio `>= 1.5`
- Missing/unreliable data returns `N/A`.

## Flood signal method

- Uses Environment Agency flood monitoring endpoint.
- Active-only signal logic:
  - `active` when active warning or alert present nearby.
  - `none` when no active signal.
  - `N/A` when unavailable/error.

## Payload attachment and compatibility

- `/assess` now attaches `location_signals` on `AssessResponse`.
- No valuation outputs are modified (`base_estimated_rv`, `adjusted_estimated_rv`, adjustments, comp weighting logic remain unchanged).
- Evidence payload builder maps report indicators:
  - `crime_adjustment_indicator`: Low/Moderate/Elevated/N/A
  - `flood_adjustment_indicator`: Active/None/N/A

## Evidence Pack rendering location

- Added concise rendering under **Valuation Estimate** in a new subsection:
  - **Allowances / Adjustments — Directional Public Signals**
- Includes explicit disclaimer that signals are directional evidence only and do not automatically create allowances or alter modelled RV.


## Reliability tightening (quality pass)

- Added benchmark reliability guards before crime classification: minimum successful benchmark sample count and minimum non-zero benchmark points; otherwise return `N/A`.
- Added a minimum local baseline floor in ratio calculation to avoid spurious escalation in very low-crime contexts.
- Flood remains active-only: being in/near flood area without active warning/alert maps to `none`, not an escalated signal.
