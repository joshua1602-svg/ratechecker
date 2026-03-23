"""
03-07 Comparable Fit Classification and Pool-Control Layer.

Sits AFTER the core CSA pipeline (and after layout overweighting when applied),
controlling which comparables participate in the displayed evidence pool.

Uses 03-07 VOA summary valuation data to:
  - Classify each comparable as fit_class: "high" | "medium" | "low"
  - Apply density-tier-aware pool control (exclusion only where pool can absorb it)
  - Attach ``fit_class`` and ``fit_notes`` to each surviving comp dict

Does NOT:
  - modify the modelled or implied RV
  - create subject-side deductions or uplifts
  - alter the existing weighting formula (weighting runs on the surviving pool)
  - use binary exclusion for car parking (parking is a soft influence signal only)

Integration contract
--------------------
Input  : list of comp dicts with at minimum ``uarn`` and ``rv``.
         Comp dicts from layout_overweight carry ``adjusted_weight``;
         raw CSA comps carry ``weight``.  Both are preserved unchanged.
Output : dict with keys:
            comps        – surviving comp dicts enriched with fit_class, fit_notes
            density_tier – "high" | "medium" | "low" | "sparse"
            fit_applied  – bool, True if any low-fit comps were excluded
            fit_summary  – stats dict
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Density-tier thresholds (based on surviving post-CSA pool size)
# ---------------------------------------------------------------------------
_DENSITY_HIGH_MIN:   int = 16  # ≥ 16 comps → "high"
_DENSITY_MEDIUM_MIN: int = 8   # 8–15 comps  → "medium"
_DENSITY_LOW_MIN:    int = 4   # 4–7 comps   → "low"
# < 4 comps → "sparse"

# Minimum pool size that must survive after any exclusion step
_MIN_POOL_AFTER_EXCLUSION: int = 5

# ---------------------------------------------------------------------------
# Signal thresholds
# ---------------------------------------------------------------------------

# Adjustment intensity: abs(total_adj) / total_before_adj (record type 07)
_ADJ_INTENSITY_HIGH:   float = 0.20  # > 20 % adjustment burden → penalty +2
_ADJ_INTENSITY_MEDIUM: float = 0.10  # > 10 % adjustment burden → penalty +1

# Additions as a fraction of the comp's rateable value (record type 03)
_ADDITIONS_FRAC_HIGH:   float = 0.25  # additions > 25 % of RV → penalty +2
_ADDITIONS_FRAC_MEDIUM: float = 0.12  # additions > 12 % of RV → penalty +1

# Car-parking contribution as a fraction of comp RV (record type 05)
# When parking value exceeds this share, the comp is structurally
# parking-dominated even without subject comparison.
_PARKING_FRAC_SIGNIFICANT: float = 0.20  # cp_total > 20 % of RV → penalty +1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _density_tier(pool_size: int) -> str:
    """Map comparable pool size to a density tier label."""
    if pool_size >= _DENSITY_HIGH_MIN:
        return "high"
    if pool_size >= _DENSITY_MEDIUM_MIN:
        return "medium"
    if pool_size >= _DENSITY_LOW_MIN:
        return "low"
    return "sparse"


def _classify_single_comp(
    comp: dict,
    parking_data: dict | None,
    additions_data: dict | None,
    pm_data: dict | None,
    adj_totals_data: dict | None,
    subject_has_parking: bool | None = None,
) -> tuple[str, str]:
    """
    Classify one comparable and return (fit_class, fit_notes).

    Parameters
    ----------
    comp            : comp dict from CSA or layout layer (must have ``rv``).
    parking_data    : row from voa_sv_car_parking, or None if absent.
    additions_data  : aggregated row from voa_sv_additions, or None.
    pm_data         : aggregated row from voa_sv_plant_machinery, or None.
    adj_totals_data : row from voa_sv_adjustment_totals, or None.
    subject_has_parking : True/False/None.  None → parking mismatch check skipped.

    Returns
    -------
    fit_class : "high" | "medium" | "low"
    fit_notes : comma-separated short signal labels for report rendering.
    """
    notes: list[str] = []
    penalty: int = 0  # 0 → high; 1–2 → medium; 3+ → low

    # --- Adjustment intensity signal (record type 07) ---
    if adj_totals_data:
        before = float(adj_totals_data.get("total_before_adj") or 0)
        adj    = float(adj_totals_data.get("total_adj")        or 0)
        if before > 0:
            intensity = abs(adj) / before
            if intensity > _ADJ_INTENSITY_HIGH:
                penalty += 2
                notes.append("high adjustments")
            elif intensity > _ADJ_INTENSITY_MEDIUM:
                penalty += 1
                notes.append("adjustments")

    # --- Additions contribution signal (record type 03) ---
    if additions_data:
        total_oa = float(additions_data.get("total_oa_value") or 0)
        comp_rv  = float(comp.get("rv") or 0)
        if comp_rv > 0 and total_oa > 0:
            frac = total_oa / comp_rv
            if frac > _ADDITIONS_FRAC_HIGH:
                penalty += 2
                notes.append("large additions")
            elif frac > _ADDITIONS_FRAC_MEDIUM:
                penalty += 1
                notes.append("additions")

    # --- Plant & machinery complexity signal (record type 04) ---
    if pm_data and float(pm_data.get("pm_value") or 0) > 0:
        penalty += 1
        notes.append("plant & machinery")

    # --- Car-parking signal (record type 05) ---
    # Parking is NEVER a binary exclusion criterion (requirement A4).
    # Two independent sub-signals:
    #   (a) Structural: comp is parking-dominated vs its RV → soft penalty
    #   (b) Mismatch: comp parking differs from subject → soft penalty
    has_comp_parking = False
    if parking_data:
        cp_total = float(parking_data.get("cp_total") or 0)
        comp_rv  = float(comp.get("rv") or 0)
        if cp_total > 0:
            has_comp_parking = True
            # (a) Significant parking contribution even without subject info
            if comp_rv > 0 and cp_total / comp_rv > _PARKING_FRAC_SIGNIFICANT:
                penalty += 1
                notes.append("significant parking")

    # (b) Mismatch vs subject (only when subject parking is known)
    if subject_has_parking is not None and has_comp_parking != subject_has_parking:
        penalty += 1
        notes.append("parking present" if has_comp_parking else "no parking")

    # --- Map penalty to fit class ---
    if penalty == 0:
        fit_class = "high"
    elif penalty <= 2:
        fit_class = "medium"
    else:
        fit_class = "low"

    return fit_class, ", ".join(notes)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def apply_fit_layer(
    rated_comps: list[dict],
    parking_by_uarn: dict[str, dict],
    additions_by_uarn: dict[str, dict],
    pm_by_uarn: dict[str, dict],
    adj_totals_by_uarn: dict[str, dict],
    subject_has_parking: bool | None = None,
) -> dict:
    """
    Apply 03-07 fit classification and density-tier-aware pool control.

    The layer preserves all existing weight keys (``weight`` from CSA,
    ``adjusted_weight`` from layout overweighting) unchanged.  It only
    adds ``fit_class`` and ``fit_notes`` to each comp dict, and may remove
    low-fit comps when the pool is dense enough to absorb the loss.

    Density-tier behaviour
    ----------------------
    sparse (< 4)  : no exclusion; fit is informational only.
    low    (4–7)  : no exclusion; fit is informational only.
    medium (8–15) : exclude low-fit comps only when ≥ 5 better comps remain.
    high   (≥ 16) : same exclusion rule as medium.

    Parameters
    ----------
    rated_comps     : comp dicts from CSA or layout layer.
    *_by_uarn       : batch-fetched 03-07 data keyed by str(uarn).
                      Empty dict for any table → that signal is skipped.
    subject_has_parking : True/False/None (None → parking mismatch skipped).

    Returns
    -------
    dict with:
        comps        – surviving comp dicts, each with fit_class, fit_notes added
        density_tier – "high" | "medium" | "low" | "sparse"
        fit_applied  – bool, True if at least one low-fit comp was excluded
        fit_summary  – input_count, output_count, excluded_count, fit_counts
    """
    n_input = len(rated_comps)
    tier    = _density_tier(n_input)

    # --- Step 1: classify every comp ---
    classified: list[dict] = []
    for comp in rated_comps:
        uarn = str(comp.get("uarn", ""))
        fit_class, fit_notes = _classify_single_comp(
            comp=comp,
            parking_data=parking_by_uarn.get(uarn),
            additions_data=additions_by_uarn.get(uarn),
            pm_data=pm_by_uarn.get(uarn),
            adj_totals_data=adj_totals_by_uarn.get(uarn),
            subject_has_parking=subject_has_parking,
        )
        classified.append({**comp, "fit_class": fit_class, "fit_notes": fit_notes})

    # --- Step 2: density-tier pool control ---
    fit_applied = False

    if tier in ("sparse", "low"):
        # Pool too thin to afford exclusion; retain all, fit is display-only
        surviving = classified

    else:
        # Medium / high: exclude low-fit comps when the pool can absorb the loss
        low_fit = [c for c in classified if c["fit_class"] == "low"]
        better  = [c for c in classified if c["fit_class"] != "low"]

        if low_fit and len(better) >= _MIN_POOL_AFTER_EXCLUSION:
            surviving  = better
            fit_applied = True
            log.warning(
                "fit_layer: excluded %d low-fit comp(s) "
                "(density_tier=%s, pool_in=%d, pool_out=%d)",
                len(low_fit), tier, n_input, len(better),
            )
        else:
            # Not enough better comps; retain all and rely on weight capping
            surviving = classified

    # --- Step 3: build summary statistics ---
    fit_counts: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    for c in surviving:
        k = c.get("fit_class", "medium")
        fit_counts[k] = fit_counts.get(k, 0) + 1

    return {
        "comps": surviving,
        "density_tier": tier,
        "fit_applied": fit_applied,
        "fit_summary": {
            "input_count":   n_input,
            "output_count":  len(surviving),
            "excluded_count": n_input - len(surviving),
            "fit_counts":    fit_counts,
        },
    }
