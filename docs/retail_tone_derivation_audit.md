# Retail tone derivation audit (code-path review)

Date: 2026-04-14

## Scope

This note documents the implemented execution path (not live DB outputs) used to derive retail tone and apply it in evidence-pack valuation tables.

## Core finding (current method)

For retail / hair_beauty comparables classified as `itza_retail`, CSA overrides per-comparable rate to:

- `rate = rv / itza_from_nia(nia_sqm)`

That `rate` is then passed into primary-cluster selection, primary-cluster outlier exclusion, and then a weighted median to derive `tone_rate`.

So the derived tone signal is based on reconstructed Zone A-equivalent effective rates (ITZA basis), not a plain blended `rv / nia` metric.

## End-to-end path summary

1. `api/routes/assess.py::run_assessment_pipeline` fetches comparables via `get_comparables`, builds `Comparable` objects, and calls `run_csa`.
2. `api/engine/csa.py::run_csa` computes per-comp `rate`; for retail `itza_retail` comparables it applies an effective-rate override (`rv/itza`).
3. `run_csa` selects the primary cluster, excludes outliers from that primary cluster, then derives `tone_rate` as a weighted median within that cluster.
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

- for retail/hair_beauty evidence reports on ITZA valuation basis, table rate
  display prioritises CSA `rate` (same basis used to derive tone);
- SV-line-derived ITZA rate is computed as auxiliary metadata when available;
- if missing, fallback display rate uses the CSA-equivalent fallback
  `rv / itza_from_nia(nia_sqm)` so table and tone remain on the same basis;
- table header is set to `Rate £/sqm (ITZA)` for those reports.

## Where upper-central anchoring was previously applied

- `api/engine/csa.py::_derive_primary_tone` previously used the `same_street_weighted_p60` branch (weighted p60 uplift) for same-street-primary cases, and could also use `same_street_dominant_cluster_median`.
- `api/models.py::build_evidence_payload_from_assess` previously defaulted `tone_basis` to `Evidence-weighted upper-central anchor`.
- `src/templates/reports/evidence_pack.html` previously described the adopted tone as an “evidence-weighted central anchor.”

These have now been replaced with primary-cluster weighted-median wording and behavior for the retail ITZA path.

## Caveat

This audit confirms code behavior only; it does not validate live VOA record contents in this environment.
