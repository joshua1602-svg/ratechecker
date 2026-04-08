# Batch overassessment runner

Script: `scripts/batch_overassessment_runner.py`

## What it does
- Pulls a batch of properties from Supabase via SQL.
- Builds an `AssessRequest` per row.
- Calls `api.services.assessment.run_live_assessment()` directly (same live valuation path used by production).
- Computes fair range as ±5% around the returned modelled RV (same report logic).
- Flags overassessed cases and writes ranked CSV output.
- Continues processing when a row fails and records `status` + `error_message`.

## Required row columns
`id, uarn, address, postcode, business_type, scat_code, voa_rv, nia_sqm`

`business_type` can be null if `scat_code` maps to one of the supported sectors.

## Setup
1. Ensure `DATABASE_URL` points to your Supabase/Postgres instance.
2. (Optional) Create helper view:
   ```bash
   psql "$DATABASE_URL" -f sql/overassessment_universe_view.sql
   ```

## Run
```bash
python scripts/batch_overassessment_runner.py \
  --limit 1000 \
  --offset 0 \
  --concurrency 4 \
  --output-csv artifacts/overassessment_batch_results.csv
```

## Custom source query
```bash
python scripts/batch_overassessment_runner.py \
  --source-sql "SELECT id,uarn,address,postcode,business_type,scat_code,voa_rv,nia_sqm FROM my_universe LIMIT :limit OFFSET :offset" \
  --limit 500
```
