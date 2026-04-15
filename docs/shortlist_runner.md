# Shortlist runner (`scripts/run_shortlist.py`)

## Confirmed from repository evidence

### Canonical assessment entrypoint reused
- The runner calls `api.routes.assess.run_assessment_pipeline()` directly for every subject. This is the same internal path used by `POST /assess` after captcha verification. No valuation logic is duplicated or replaced.

### Single-subject assessment flow currently used
- `POST /assess` verifies captcha, then delegates to `run_assessment_pipeline()`.
- `run_assessment_pipeline()` performs postcode geocoding, comparable lookup, subject exclusion, CSA scoring, optional layout overweighting, fit layer enrichment, adjustment layer, and savings projection before returning `AssessResponse`.

### Supabase/Postgres access patterns present in code
- The codebase uses SQLAlchemy engines driven by `DATABASE_URL` (`api/db.py` for API runtime pooling, `data/db.py` for script/batch NullPool usage).
- Query style is parameterized SQL via `sqlalchemy.text(...)` with `Session`/connection scopes.
- Runtime DDL pattern exists in app code (e.g., `pending_reports` auto-create in `api/pending_reports.py`) using `CREATE TABLE IF NOT EXISTS`.

### VOA tables/fields inferred from code + SQL
- `voa_list_entries`: includes `uarn`, `full_property_identifier`, `postcode`, `street`, `scat_code`, `rateable_value`, `primary_description_text`, `composite_indicator`.
- `voa_sv_header`: includes `uarn`, `total_area_or_units`, `unit_of_measurement`, `unadjusted_price_psm`.
- `voa_sv_lines`: used for structured valuation lines (`floor`, `description`, `area`, `price`, `value`, `line_number`).
- `postcode_coords`: includes `postcode`, `latitude`, `longitude`.
- Existing helper view in repo: `overassessment_universe` (`sql/overassessment_universe_view.sql`).

### Existing batch tooling and reusable scoring artifacts
- Existing script `scripts/batch_overassessment_runner.py` already reuses `run_assessment_pipeline()` in batch mode.
- Existing savings and RV-range helpers reused by this runner (`build_downside_rv_range`, implied saving fields on `AssessResponse`).

## Assumptions (because no live Supabase schema/browser access)

1. The runner assumes tables `voa_list_entries` and `voa_sv_header` exist and are queryable by the batch DB role in the same shape used by current API code.
2. `shortlist_runs` and `shortlist_candidates` may not exist in target environments; runner creates them on demand when `--write-db` is used.
3. JSON-like payload columns are stored as `TEXT` for broad compatibility (SQLite/Postgres) and serialized with `json.dumps`.
4. `--min-comp-density` is exposed as CLI input but currently reserved (not applied DB-side) because no confirmed low-cost density pre-aggregation object exists in repository evidence.
5. `confidence_score` is derived from existing output signals (`signal`, comparable counts, same-street count) because `AssessResponse` does not expose raw CSA confidence directly.

## Shortlist score formula

Implemented in `_shortlist_score(...)` as:

```text
score = saving_mid
      + (max(rv_delta_pct, 0) * 100)
      + (confidence_score * 1000)
      + (min(comp_count, 15) * 120)
      + (min(same_street_comp_count, 10) * 100)
```

Where `saving_mid = (annual_saving_low + annual_saving_high) / 2`.

## Output structure (CSV/JSON)

Primary export columns:
- `uarn`
- `property_name`
- `address`
- `postcode`
- `sector`
- `scat_code`
- `current_rv`
- `fair_rv_low`
- `fair_rv_high`
- `fair_rv_mid`
- `rv_delta`
- `rv_delta_pct`
- `estimated_annual_saving_low`
- `estimated_annual_saving_high`
- `confidence_score`
- `comp_count`
- `same_street_comp_count`
- `shortlist_score`
- `primary_reason`
- `reason_summary`
- `status`
- `error_message`

Sorted by pass-status then `shortlist_score` descending by default.

## Example commands

```bash
python scripts/run_shortlist.py --postcode SW19 --limit 500 --dry-run
python scripts/run_shortlist.py --sector retail --min-saving 2500 --dry-run
python scripts/run_shortlist.py --postcode-prefix SW --write-db --limit 1000
python scripts/run_shortlist.py --supported-sectors-only --batch-size 100 --dry-run
python scripts/run_shortlist.py --sector retail --postcode-prefix SW --min-comp-density 5 --export-csv out.csv --dry-run
```
