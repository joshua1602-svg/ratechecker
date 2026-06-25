# Batch overassessment runner

Script: `scripts/batch_overassessment_runner.py`

## What it does
- Pulls a batch of properties from Supabase via SQL.
- Builds an `AssessRequest` per row.
- Resolves the reusable live callable (prefers service-layer callable if present; otherwise uses `api.routes.assess.run_assessment_pipeline()` when exposed).
- Falls back to calling a live HTTP assess endpoint only when no reusable callable exists (`--assess-url`).
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

## HTTP fallback mode (only if importable callable is unavailable)
```bash
python scripts/batch_overassessment_runner.py \
  --assess-url "https://your-api-host/assess" \
  --limit 200
```
