# Retail tone derivation audit (code-path review)

Date: 2026-04-12

## Scope

This note documents the implemented execution path (not live DB outputs) used to derive retail tone and apply it in evidence-pack valuation tables.

## Core finding

For retail / hair_beauty comparables classified as `itza_retail`, CSA overrides per-comparable rate to:

- `rate = rv / itza_from_nia(nia_sqm)`

That `rate` is then passed into weighted median / same-street cluster logic to derive `tone_rate`.

So the derived tone signal is based on reconstructed Zone A-equivalent effective rates (ITZA basis), not a plain blended `rv / nia` metric.

## End-to-end path summary

1. `api/routes/assess.py::run_assessment_pipeline` fetches comparables via `get_comparables`, builds `Comparable` objects, and calls `run_csa`.
2. `api/engine/csa.py::run_csa` computes per-comp `rate`; for retail `itza_retail` comparables it applies an effective-rate override (`rv/itza`).
3. `run_csa` derives `tone_rate` from weighted central tendency of those `rate` values.
4. `api/models.py::build_evidence_payload_from_assess` copies `tone_rate` into `final_tone_psm` and calls `build_valuation_detail`.
5. `api/engine/valuation.py::_build_zoning_detail` builds subject ITZA rows where row value is `itza_contribution * tone_rate`.
6. `src/templates/reports/evidence_pack.html` labels valuation basis as ITZA and prints row-level displayed tones/values.

## Subject table application

In zoning detail generation:

- `displayed_tone = tone_rate * relativity`
- `row_value = itza_contrib * tone_rate`

This is ITZA-consistent arithmetic for the subject table.

## Comparable-table alignment

Report rendering now applies a retail ITZA guard for comparable display rates:

- for retail/hair_beauty evidence reports on ITZA valuation basis, if SV lines
  are available the table shows `rv / comparable_itza_from_sv_lines`;
- if SV lines are absent and comparable carries CSA `rate`, that value is shown;
- if missing, fallback display rate is `rv / comparable_itza`, where comparable
  ITZA is derived from SV lines when available (description-led retail zoning
  relativities, with price-ratio fallback), then falls back to `itza_from_nia`;
- table header is set to `Rate £/sqm (ITZA)` for those reports.

## Caveat

This audit confirms code behavior only; it does not validate live VOA record contents in this environment.
