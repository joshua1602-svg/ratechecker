# Shortlist runner (`scripts/run_shortlist.py`)

## Confirmed from repository evidence

### Canonical assessment entrypoint reused
- The runner calls `api.routes.assess.run_assessment_pipeline()` directly for each subject property.
- This is the same internal path used by `POST /assess` after captcha validation.

### Candidate-universe source used
- The candidate universe is sourced from the existing SQL helper view `overassessment_universe`.
- The runner joins `overassessment_universe` to `voa_list_entries` to support `description_type` prefiltering using `primary_description_text`.
- Runtime safety guard: the runner refuses to execute unless at least one explicit scope flag is provided (`--postcode`, `--postcode-prefix`, `--sector`, `--supported-sectors-only`, or explicit `--limit`).

### Supabase/Postgres access patterns present in code
- Uses SQLAlchemy engines driven by `DATABASE_URL` (`api/db.py` for API runtime pooling, `data/db.py` for script/batch NullPool usage).
- Uses parameterized SQL via `sqlalchemy.text(...)` and connection scopes.

### VOA tables/fields inferred from code + SQL
- `overassessment_universe` view: `id`, `uarn`, `address`, `postcode`, `business_type`, `scat_code`, `voa_rv`, `nia_sqm`.
- `voa_list_entries`: `uarn`, `primary_description_text` (used for description prefilter).
- Other confirmed valuation-side tables from existing code paths: `voa_sv_lines`, `postcode_coords`.

## Assumptions (because no live Supabase schema/browser access)

1. `overassessment_universe` is present in each target environment (the repo includes SQL to create it).
2. `shortlist_runs` and `shortlist_candidates` may not exist and are created on demand when `--write-db` is used.
3. JSON payload columns are stored as `TEXT` and serialized with `json.dumps`.

## Normalized shortlist scoring (0-100 components)

Sub-scores are normalized/capped before weighting so large outliers do not dominate:

- `saving_score`: `annual_saving_high` capped at £10,000 → 0..100
- `delta_score`: `rv_delta_pct` capped at 30% → 0..100
- `confidence_score_component`: derived confidence (0..1) × 100
- `comp_score`: comparable count capped at 10 → 0..100
- `same_street_score`: same-street comparable count capped at 6 → 0..100

Final score:

```text
shortlist_score =
  0.35 * saving_score +
  0.20 * delta_score +
  0.20 * confidence_score_component +
  0.15 * comp_score +
  0.10 * same_street_score
```

## Derived confidence logic

Derived confidence is explicit and inspectable:

- Base mapping from assessment `signal`:
  - High=0.90, Medium=0.70, Low=0.45, Insufficient Data=0.20
- Adjustments:
  - `comp_count >= 8`: +0.05
  - `comp_count <= 2`: -0.10
  - `same_street_comp_count >= 4`: +0.03
  - `comp_count == 0`: -0.15 (ambiguity)
  - `signal == "Insufficient Data"`: -0.10
  - `signal == "Low" and comp_count < 3`: -0.05
- Clamped to 0.0..1.0.

Exports include:
- raw confidence inputs (`evidence_summary.confidence_raw_inputs`)
- `derived_confidence_score`
- `derived_confidence_reason`

## `--min-comp-density` status

- Removed from CLI because it was previously non-functional.
- This avoids exposing a user-facing no-op control.

## Early pre-assessment filtering

Before running valuation, each candidate is checked for obvious invalidity:
- missing postcode
- non-positive RV
- non-positive NIA
- unsupported business type

Skipped rows are exported with:
- `status=skipped_pre_assessment`
- `early_filter_reason`
- `pass_fail_reason`

## Score transparency outputs

Each assessed row includes:
- `score_breakdown_json`
- `score_breakdown_summary`

These include component values, weights, and dominant driver.

## Example passing row shape

```json
{
  "uarn": "123456789",
  "status": "pass",
  "shortlist_score": 74.3,
  "saving_score": 82.0,
  "delta_score": 61.4,
  "confidence_score_component": 78.0,
  "comp_score": 70.0,
  "same_street_score": 33.3,
  "derived_confidence_score": 0.78,
  "derived_confidence_reason": "signal_base=0.70 | comp_count>=8:+0.05"
}
```

## Example filtered/skipped row shape

```json
{
  "uarn": "987654321",
  "status": "skipped_pre_assessment",
  "early_filter_reason": "unsupported_business_type",
  "pass_fail_reason": "skipped:unsupported_business_type",
  "shortlist_score": 0.0
}
```

## Example commands

```bash
python scripts/run_shortlist.py --postcode SW19 --limit 500 --dry-run
python scripts/run_shortlist.py --sector retail --min-saving 2500 --dry-run
python scripts/run_shortlist.py --postcode-prefix SW --write-db --limit 1000
python scripts/run_shortlist.py --supported-sectors-only --batch-size 100 --dry-run
python scripts/run_shortlist.py --sector retail --postcode-prefix SW --export-csv out.csv --dry-run
python scripts/run_shortlist.py --sector retail --postcode-prefix SW --export-review-csv review.csv --max-runtime-minutes 20 --max-subjects 1000 --dry-run
```
