# Sector Methodology Audit (Phase 1)

## Summary conclusion
- The issue is **pipeline-deep, not only last-mile PDF**. The `/assess` CSA path currently computes `restaurant_cafe` modelled RV using an ITZA subject conversion (`tone * itza_from_nia`), while comparable tone extraction for restaurant comps is NIA-normalised (`rv/nia`), creating a method mismatch. The PDF then faithfully renders ITZA-style valuation detail because the valuation-detail builder also used restaurant `valuation_method: zoning` from rules.

## Current valuation pipeline by sector
- **retail / hair_beauty**: tone is derived from comparables with ITZA-effective-rate override for ITZA-like retail descriptions, and modelled RV is reconstructed on ITZA for standard retail; area-retail variants reconstruct on NIA. 
- **restaurant_cafe (before fix)**: comparable rates are NIA-normalised in CSA, but modelled RV reconstruction used ITZA.
- **nursery**: comparable rates and modelled RV are NIA-based.

## Restaurant_cafe deep dive
- Restaurant comparables in CSA are not ITZA-overridden (that override is retail-like only), so tone behaves as £/sqm NIA.
- Restaurant RV reconstruction previously multiplied by subject ITZA (1:3 fallback where needed), introducing ITZA geometry into the final RV.
- Layout overweighting adjusts comparable weights and confidence signals; it is not coded as direct subject-level deduction/allowance in the PDF narrative path.

## PDF rendering path
- `/report/download/{session_id}` and `/report/evidence` build payload via `build_evidence_payload_from_assess()`.
- That calls `build_valuation_detail()`, which previously read rule `valuation_method` and returned zoning blocks for restaurant.
- Template `src/templates/reports/evidence_pack.html` rendered valuation summary + zoning schedule when `valuation_method == 'zoning'`.

## Root cause
- Primary root cause: **method mismatch in computational pipeline for restaurant_cafe** (NIA tone derivation + ITZA subject reconstruction).
- Secondary root cause: **report contract/template coupling to legacy method labels (`zoning`/`nia_only`)** and restaurant rule set declaring zoning.

## Recommended fix scope
1. Explicit method contract: `valuation_method in {itza, nia}`.
2. Switch restaurant and nursery to NIA method in rules + valuation detail builder.
3. Update CSA restaurant subject reconstruction and quality-gate implied-rate basis to NIA.
4. Make evidence template method-safe (`itza` renders zoning; `nia` renders NIA block only).
5. Add tests for method selection and rendered content by sector.

## Files/functions/components involved
- `api/engine/csa.py::run_csa`
- `api/engine/valuation.py::{calculate_rv, apply_adjustments, build_valuation_detail}`
- `rules/restaurant_cafe.yaml`, `rules/nursery.yaml`, `rules/retail.yaml`
- `api/models.py::EvidenceReportRequest`, payload builders
- `api/reports/pdf_generator.py::_derive_fields`
- `src/templates/reports/evidence_pack.html`
- Tests: `tests/test_valuation.py`, `tests/test_pdf_generator.py`
