"""
Layout-based comparable overweighting layer.

Sits AFTER the core CSA pipeline and adjusts comparable weights based on
how closely each comparable's physical layout matches the subject property.
Does not modify comp selection, filtering, clustering, or distance gating.

The layer is callable as a standalone function:
    apply_layout_overweighting(csa_result, layout_input, uarns, sv_lines_by_uarn)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning constant — weight boost for a perfect layout match
# ---------------------------------------------------------------------------
LAYOUT_WEIGHT_FACTOR: float = 0.5

# ---------------------------------------------------------------------------
# VOA description → use-type classification
# ---------------------------------------------------------------------------
# Maintainable dictionary mapping.  Keys are matched case-insensitively
# against the start of (or substring within) the raw VOA description string.
# Order matters: first match wins, so more specific patterns come first.

_USE_TYPE_TRADING = "TRADING"
_USE_TYPE_STORAGE = "STORAGE"
_USE_TYPE_KITCHEN = "KITCHEN"
_USE_TYPE_OFFICE = "OFFICE"
_USE_TYPE_ANCILLARY = "ANCILLARY"
_USE_TYPE_OTHER = "OTHER"

DESCRIPTION_CLASSIFICATION: dict[str, str] = {
    # Kitchen / prep
    "kitchen":          _USE_TYPE_KITCHEN,
    "prep area":        _USE_TYPE_KITCHEN,
    "prep room":        _USE_TYPE_KITCHEN,
    "preparation":      _USE_TYPE_KITCHEN,
    # Storage
    "store":            _USE_TYPE_STORAGE,
    "storage":          _USE_TYPE_STORAGE,
    "cellar":           _USE_TYPE_STORAGE,
    "cold room":        _USE_TYPE_STORAGE,
    "cold store":       _USE_TYPE_STORAGE,
    "freezer":          _USE_TYPE_STORAGE,
    # Office
    "office":           _USE_TYPE_OFFICE,
    # Ancillary
    "wc":               _USE_TYPE_ANCILLARY,
    "toilet":           _USE_TYPE_ANCILLARY,
    "staff":            _USE_TYPE_ANCILLARY,
    "changing":         _USE_TYPE_ANCILLARY,
    "lobby":            _USE_TYPE_ANCILLARY,
    "entrance":         _USE_TYPE_ANCILLARY,
    "corridor":         _USE_TYPE_ANCILLARY,
    "passage":          _USE_TYPE_ANCILLARY,
    "staircase":        _USE_TYPE_ANCILLARY,
    "stairs":           _USE_TYPE_ANCILLARY,
    "lift":             _USE_TYPE_ANCILLARY,
    "plant room":       _USE_TYPE_ANCILLARY,
    "boiler":           _USE_TYPE_ANCILLARY,
    "bin":              _USE_TYPE_ANCILLARY,
    "yard":             _USE_TYPE_ANCILLARY,
    "loading":          _USE_TYPE_ANCILLARY,
    # Trading / commercial space
    "retail zone":      _USE_TYPE_TRADING,
    "zone a":           _USE_TYPE_TRADING,
    "zone b":           _USE_TYPE_TRADING,
    "zone c":           _USE_TYPE_TRADING,
    "remainder":        _USE_TYPE_TRADING,
    "shop":             _USE_TYPE_TRADING,
    "sales":            _USE_TYPE_TRADING,
    "showroom":         _USE_TYPE_TRADING,
    "trading":          _USE_TYPE_TRADING,
    "restaurant":       _USE_TYPE_TRADING,
    "cafe":             _USE_TYPE_TRADING,
    "bar":              _USE_TYPE_TRADING,
    "dining":           _USE_TYPE_TRADING,
    "covers":           _USE_TYPE_TRADING,
    "seating":          _USE_TYPE_TRADING,
    "salon":            _USE_TYPE_TRADING,
    "treatment":        _USE_TYPE_TRADING,
    "workshop":         _USE_TYPE_TRADING,
    "display":          _USE_TYPE_TRADING,
    "ground floor":     _USE_TYPE_TRADING,
}


def classify_description(raw_desc: str) -> str:
    """Map a raw VOA SV line description to a use-type category."""
    lower = raw_desc.strip().lower()
    for pattern, use_type in DESCRIPTION_CLASSIFICATION.items():
        if pattern in lower:
            return use_type
    return _USE_TYPE_OTHER


# ---------------------------------------------------------------------------
# Floor classification helpers
# ---------------------------------------------------------------------------
_GROUND_FLOOR_PATTERNS = {"g", "gf", "ground", "ground floor", "0"}
_LOWER_GROUND_PATTERNS = {"lg", "lgf", "lower ground", "lower ground floor",
                          "basement", "bsmt", "b", "-1"}
_UPPER_FLOOR_PATTERNS = {"1", "1st", "first", "first floor",
                         "2", "2nd", "second", "second floor",
                         "3", "3rd", "third", "mezzanine", "mezz",
                         "f", "ff", "upper", "upper floor"}


def _classify_floor(raw_floor: str) -> str:
    """Classify a raw VOA floor string as 'ground', 'lower_ground', or 'upper'."""
    lower = raw_floor.strip().lower()
    if lower in _GROUND_FLOOR_PATTERNS:
        return "ground"
    if lower in _LOWER_GROUND_PATTERNS:
        return "lower_ground"
    if lower in _UPPER_FLOOR_PATTERNS:
        return "upper"
    # Heuristic fallback: numeric floors
    try:
        n = int(lower)
        if n == 0:
            return "ground"
        if n < 0:
            return "lower_ground"
        return "upper"
    except ValueError:
        pass
    return "ground"  # default assumption


# ---------------------------------------------------------------------------
# Layout fingerprint
# ---------------------------------------------------------------------------
@dataclass
class LayoutFingerprint:
    """Layout characteristics of a single property."""
    storage_ratio: float = 0.0   # total storage sqm / total NIA
    trading_ratio: float = 0.0   # total trading sqm / total NIA
    has_lower_ground: bool = False
    has_upper_floor: bool = False
    kitchen_on_ground: bool = False
    total_nia: float = 0.0


# ---------------------------------------------------------------------------
# Subject layout input
# ---------------------------------------------------------------------------
@dataclass
class LayoutInput:
    """User-provided layout description of their own property.

    floor_config options:
        "ground_only", "ground_lower_ground", "ground_first",
        "ground_lower_ground_first", "other"
    lower_ground_use / upper_floor_use options:
        "trading", "storage", "kitchen", "office", "not_applicable"
    kitchen_on_ground options (restaurants only):
        "yes", "no", "no_kitchen"
    """
    floor_config: str = "ground_only"
    ground_floor_trading_sqm: float = 0.0
    ground_floor_storage_sqm: float = 0.0
    lower_ground_use: str = "not_applicable"
    upper_floor_use: str = "not_applicable"
    kitchen_on_ground: str = "no_kitchen"  # "yes" / "no" / "no_kitchen"
    total_nia_sqm: float = 0.0


def fingerprint_from_subject(layout: LayoutInput) -> LayoutFingerprint:
    """Derive a LayoutFingerprint from user-provided layout inputs."""
    total_nia = layout.total_nia_sqm if layout.total_nia_sqm > 0 else 1.0

    storage_sqm = layout.ground_floor_storage_sqm
    trading_sqm = layout.ground_floor_trading_sqm

    has_lower_ground = layout.floor_config in (
        "ground_lower_ground", "ground_lower_ground_first",
    )
    has_upper_floor = layout.floor_config in (
        "ground_first", "ground_lower_ground_first",
    )

    # Count lower ground / upper floor contribution to storage/trading
    if has_lower_ground and layout.lower_ground_use == "storage":
        # Approximate: the remaining NIA not on ground floor, split
        non_ground = max(0, total_nia - trading_sqm - storage_sqm)
        storage_sqm += non_ground * 0.5 if has_upper_floor else non_ground
    if has_lower_ground and layout.lower_ground_use == "trading":
        non_ground = max(0, total_nia - trading_sqm - storage_sqm)
        trading_sqm += non_ground * 0.5 if has_upper_floor else non_ground
    if has_upper_floor and layout.upper_floor_use == "storage":
        non_ground = max(0, total_nia - trading_sqm - storage_sqm)
        storage_sqm += non_ground
    if has_upper_floor and layout.upper_floor_use == "trading":
        non_ground = max(0, total_nia - trading_sqm - storage_sqm)
        trading_sqm += non_ground

    return LayoutFingerprint(
        storage_ratio=min(storage_sqm / total_nia, 1.0),
        trading_ratio=min(trading_sqm / total_nia, 1.0),
        has_lower_ground=has_lower_ground,
        has_upper_floor=has_upper_floor,
        kitchen_on_ground=layout.kitchen_on_ground == "yes",
        total_nia=total_nia,
    )


def fingerprint_from_sv_lines(
    sv_lines: list[dict],
    total_nia: float,
) -> LayoutFingerprint:
    """Derive a LayoutFingerprint from voa_sv_lines rows for one UARN.

    Each row is expected to have keys: floor, description, area.
    """
    if not sv_lines or total_nia <= 0:
        return LayoutFingerprint(total_nia=total_nia)

    storage_sqm = 0.0
    trading_sqm = 0.0
    has_lower_ground = False
    has_upper_floor = False
    kitchen_on_ground = False

    for line in sv_lines:
        floor_raw = str(line.get("floor") or "")
        desc_raw = str(line.get("description") or "")
        area = float(line.get("area") or 0)

        floor_type = _classify_floor(floor_raw)
        use_type = classify_description(desc_raw)

        if floor_type == "lower_ground":
            has_lower_ground = True
        elif floor_type == "upper":
            has_upper_floor = True

        if use_type == _USE_TYPE_STORAGE:
            storage_sqm += area
        elif use_type == _USE_TYPE_TRADING:
            trading_sqm += area
        elif use_type == _USE_TYPE_KITCHEN and floor_type == "ground":
            kitchen_on_ground = True

    denom = total_nia if total_nia > 0 else 1.0
    return LayoutFingerprint(
        storage_ratio=min(storage_sqm / denom, 1.0),
        trading_ratio=min(trading_sqm / denom, 1.0),
        has_lower_ground=has_lower_ground,
        has_upper_floor=has_upper_floor,
        kitchen_on_ground=kitchen_on_ground,
        total_nia=total_nia,
    )


# ---------------------------------------------------------------------------
# Similarity scoring
# ---------------------------------------------------------------------------
_WEIGHT_STORAGE = 0.35
_WEIGHT_TRADING = 0.35
_WEIGHT_FLOOR_CONFIG = 0.30  # split equally between lower_ground and upper_floor


def _ratio_score(subject_ratio: float, comp_ratio: float) -> float:
    """Score a ratio match: 1.0 if within 0.15, 0.5 if within 0.30, else 0.0."""
    diff = abs(comp_ratio - subject_ratio)
    if diff <= 0.15:
        return 1.0
    if diff <= 0.30:
        return 0.5
    return 0.0


def layout_similarity_score(
    subject: LayoutFingerprint,
    comp: LayoutFingerprint,
    is_restaurant: bool = False,
) -> float:
    """Compute layout similarity between subject and comparable (0–1)."""
    storage_score = _ratio_score(subject.storage_ratio, comp.storage_ratio)
    trading_score = _ratio_score(subject.trading_ratio, comp.trading_ratio)

    floor_score = 0.0
    if subject.has_lower_ground == comp.has_lower_ground:
        floor_score += 0.5  # half of floor_config weight
    if subject.has_upper_floor == comp.has_upper_floor:
        floor_score += 0.5  # other half

    raw = (
        storage_score * _WEIGHT_STORAGE
        + trading_score * _WEIGHT_TRADING
        + floor_score * _WEIGHT_FLOOR_CONFIG
    )

    # Restaurant kitchen bonus
    if is_restaurant and subject.kitchen_on_ground == comp.kitchen_on_ground:
        raw *= 1.15

    return min(raw, 1.0)


# ---------------------------------------------------------------------------
# Main entry point — apply layout overweighting
# ---------------------------------------------------------------------------
def apply_layout_overweighting(
    csa_result: dict,
    layout_input: Optional[LayoutInput],
    sv_lines_by_uarn: dict[str, list[dict]],
    business_type: str = "",
) -> dict:
    """Apply layout-based weight adjustment to the CSA-rated comparables.

    Parameters
    ----------
    csa_result : dict
        The full return dict from run_csa, which must include a '_rated_comps'
        key containing a list of dicts with keys:
            uarn, address, rv, nia_sqm, rate, weight
    layout_input : LayoutInput or None
        User-provided layout description.  If None, layout adjustment is skipped.
    sv_lines_by_uarn : dict
        Mapping of UARN → list of voa_sv_lines row dicts (floor, description, area).
    business_type : str
        The business type string (e.g. "restaurant_cafe").

    Returns
    -------
    dict with keys:
        comps          — list of comp dicts, each with original_weight,
                         layout_similarity_score, adjusted_weight
        layout_summary — summary statistics
        layout_adjustment_applied — boolean
    """
    rated_comps: list[dict] = csa_result.get("_rated_comps", [])
    is_restaurant = business_type == "restaurant_cafe"

    # --- Fallback: no layout input ---
    if layout_input is None:
        return _passthrough(rated_comps, reason="no_layout_input")

    subject_fp = fingerprint_from_subject(layout_input)

    # --- Build fingerprints for each comparable ---
    uarns_with_sv = 0
    comp_results: list[dict] = []

    for comp in rated_comps:
        uarn = comp["uarn"]
        nia = comp.get("nia_sqm", 0)
        sv_lines = sv_lines_by_uarn.get(str(uarn), [])

        if sv_lines:
            uarns_with_sv += 1

        comp_fp = fingerprint_from_sv_lines(sv_lines, nia)
        sim_score = layout_similarity_score(subject_fp, comp_fp, is_restaurant)

        comp_results.append({
            **comp,
            "original_weight": comp["weight"],
            "layout_similarity_score": round(sim_score, 4),
            "_comp_fingerprint": comp_fp,
        })

    # --- Fallback: insufficient SV line coverage ---
    if len(rated_comps) > 0:
        coverage = uarns_with_sv / len(rated_comps)
    else:
        coverage = 0.0

    if coverage < 0.50:
        log.warning(
            "layout_overweight: sv_lines coverage %.0f%% < 50%% — skipping adjustment "
            "(uarns_with_sv=%d, total_comps=%d)",
            coverage * 100, uarns_with_sv, len(rated_comps),
        )
        return _passthrough(rated_comps, reason="insufficient_sv_coverage")

    # --- Apply weight adjustment ---
    original_weight_sum = sum(c["original_weight"] for c in comp_results)

    for c in comp_results:
        sim = c["layout_similarity_score"]
        c["adjusted_weight"] = c["original_weight"] * (1 + sim * LAYOUT_WEIGHT_FACTOR)

    # --- Normalise so adjusted weights sum to same total as originals ---
    adjusted_sum = sum(c["adjusted_weight"] for c in comp_results)
    if adjusted_sum > 0 and original_weight_sum > 0:
        scale = original_weight_sum / adjusted_sum
        for c in comp_results:
            c["adjusted_weight"] = round(c["adjusted_weight"] * scale, 6)

    # --- Determine materiality ---
    material_change = False
    for c in comp_results:
        if c["original_weight"] > 0:
            shift = abs(c["adjusted_weight"] - c["original_weight"]) / c["original_weight"]
            if shift > 0.20:
                material_change = True
                break

    # --- Build summary ---
    high_sim = sum(1 for c in comp_results if c["layout_similarity_score"] >= 0.7)
    mod_sim = sum(1 for c in comp_results
                  if 0.4 <= c["layout_similarity_score"] < 0.7)
    low_sim = sum(1 for c in comp_results if c["layout_similarity_score"] < 0.4)

    layout_summary = {
        "subject_fingerprint": {
            "storage_ratio": round(subject_fp.storage_ratio, 4),
            "trading_ratio": round(subject_fp.trading_ratio, 4),
            "has_lower_ground": subject_fp.has_lower_ground,
            "has_upper_floor": subject_fp.has_upper_floor,
            "kitchen_on_ground": subject_fp.kitchen_on_ground,
        },
        "high_similarity_count": high_sim,
        "moderate_similarity_count": mod_sim,
        "low_similarity_count": low_sim,
        "material_weight_change": material_change,
        "sv_line_coverage_pct": round(coverage * 100, 1),
    }

    # Clean up internal fields before returning
    for c in comp_results:
        c.pop("_comp_fingerprint", None)

    return {
        "comps": comp_results,
        "layout_summary": layout_summary,
        "layout_adjustment_applied": True,
    }


def _passthrough(
    rated_comps: list[dict],
    reason: str = "",
) -> dict:
    """Return comps unchanged with layout_adjustment_applied=False."""
    if reason:
        log.warning("layout_overweight: passthrough — %s", reason)

    comps_out = []
    for c in rated_comps:
        comps_out.append({
            **c,
            "original_weight": c.get("weight", 0),
            "layout_similarity_score": 0.0,
            "adjusted_weight": c.get("weight", 0),
        })

    return {
        "comps": comps_out,
        "layout_summary": None,
        "layout_adjustment_applied": False,
    }
