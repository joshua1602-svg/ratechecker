from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from statistics import quantiles
from typing import Literal, Optional


TonePosition = Literal[
    "materially_above",
    "above",
    "broadly_in_line",
    "below",
    "materially_below",
]

MatchQuality = Literal["exact", "partial", "none"]
MismatchBand = Literal["none", "minor", "moderate", "material"]
StructuralBand = Literal[
    "not_applied",
    "standard_layout",
    "basement_relevant",
    "restaurant_layout",
]
CaseStrength = Literal["strong", "moderate", "weak"]
EvidenceStrength = Literal["high", "medium", "low"]
DispersionBand = Literal["low", "moderate", "high"]


@dataclass
class NarrativeSignals:
    sector: str
    case_strength: CaseStrength

    current_rv: float
    modelled_rv: float
    rv_delta_abs: float
    rv_delta_pct: float

    evidence_count: int
    same_street_comp_count: int
    same_postcode_comp_count: int

    weighted_tone: float
    subject_implied_tone: float
    tone_gap_pct: float
    tone_position: TonePosition

    voa_match_quality: MatchQuality
    area_delta_pct: float
    floor_split_delta_pct: float
    mismatch_band: MismatchBand

    layout_weighting_applied: bool
    floor_configuration: Optional[str]
    structural_band: StructuralBand

    dispersion_band: DispersionBand
    evidence_strength: EvidenceStrength


def classify_tone_position(tone_gap_pct: float) -> TonePosition:
    if tone_gap_pct >= 12.5:
        return "materially_above"
    if tone_gap_pct >= 5.0:
        return "above"
    if tone_gap_pct <= -12.5:
        return "materially_below"
    if tone_gap_pct <= -5.0:
        return "below"
    return "broadly_in_line"


def classify_mismatch_band(
    voa_match_quality: str,
    area_delta_pct: Optional[float],
    floor_split_delta_pct: Optional[float],
) -> MismatchBand:
    if voa_match_quality == "exact":
        return "none"

    values = [v for v in [area_delta_pct, floor_split_delta_pct] if v is not None]
    worst = max(values) if values else 0.0

    if worst >= 20.0:
        return "material"
    if worst >= 10.0:
        return "moderate"
    if worst > 0:
        return "minor"
    return "minor" if voa_match_quality == "partial" else "none"


def classify_structural_band(
    layout_weighting_applied: bool,
    floor_configuration: Optional[str],
    sector: str,
) -> StructuralBand:
    if not layout_weighting_applied:
        return "not_applied"

    fc = (floor_configuration or "").lower()
    sec = (sector or "").lower()

    if sec == "restaurant_cafe":
        return "restaurant_layout"

    if (
        "basement" in fc
        or "lower ground" in fc
        or "lower_ground" in fc
        or "ground_lower_ground" in fc
    ):
        return "basement_relevant"

    return "standard_layout"


def classify_dispersion_band(dispersion_ratio: float) -> DispersionBand:
    if dispersion_ratio <= 0.20:
        return "low"
    if dispersion_ratio <= 0.40:
        return "moderate"
    return "high"


def classify_evidence_strength(
    evidence_count: int,
    same_street_comp_count: int,
    same_postcode_comp_count: int,
) -> EvidenceStrength:
    if evidence_count >= 15 and (same_street_comp_count >= 3 or same_postcode_comp_count >= 5):
        return "high"
    if evidence_count >= 8:
        return "medium"
    return "low"


def classify_case_strength(
    rv_delta_pct: float,
    evidence_strength: EvidenceStrength,
    dispersion_band: DispersionBand,
) -> CaseStrength:
    if rv_delta_pct >= 15.0 and evidence_strength == "high" and dispersion_band in {"low", "moderate"}:
        return "strong"
    if rv_delta_pct >= 7.5:
        return "moderate"
    return "weak"


POSITIONING_TEMPLATES = {
    "materially_above": "The current assessment appears materially above the central tone indicated by the comparable evidence.",
    "above": "The current assessment sits above the central body of comparable evidence.",
    "broadly_in_line": "The current assessment is broadly in line with the central body of comparable evidence.",
    "below": "The current assessment sits below the central body of comparable evidence.",
    "materially_below": "The current assessment appears materially below the central tone indicated by the comparable evidence.",
}

MISMATCH_TEMPLATES = {
    "none": "The property details used in this analysis align closely with the matched VOA record.",
    "minor": "The property details broadly align with the matched VOA record, with only limited differences noted.",
    "moderate": "Some differences exist between the entered property details and the matched VOA record. These do not prevent analysis, but they reduce certainty and should be checked before formal submission.",
    "material": "There is a material difference between the entered property details and the matched VOA record. This should be verified at Check stage, as floor area or configuration differences may affect the assessment.",
}

STRUCTURAL_TEMPLATES = {
    "not_applied": "No additional structural weighting was applied beyond the core comparable selection criteria.",
    "standard_layout": "Additional weight has been given to hereditaments showing closer structural similarity in floor configuration and trading/ancillary balance.",
    "basement_relevant": "Additional weight has been given to hereditaments with a more similar floor configuration, particularly where basement or lower-ground accommodation forms part of the unit.",
    "restaurant_layout": "Additional weight has been given to hereditaments with closer operational layout characteristics, including trading/storage balance and kitchen positioning where available.",
}

CASE_STRENGTH_TEMPLATES = {
    "strong": "This case is assessed as Strong because the modelled value is materially below the current assessment and is supported by a sufficiently strong and locally relevant body of comparable evidence.",
    "moderate": "This case is assessed as Moderate because the comparable evidence supports a possible reduction, but either the value gap, factual alignment, or consistency of evidence is not strong enough to treat the case as clear-cut.",
    "weak": "This case is assessed as Weak because the comparable evidence does not currently demonstrate a sufficiently clear overassessment relative to the existing VOA rateable value.",
}


_STREET_SUFFIXES: frozenset[str] = frozenset({
    "STREET", "ROAD", "AVENUE", "LANE", "WAY", "CLOSE", "GROVE",
    "PLACE", "GARDENS", "COURT", "DRIVE", "ROW", "TERRACE", "WALK",
    "PARADE", "GATE", "BRIDGE", "HILL", "SQUARE", "MEWS", "YARD",
    "QUAY", "WHARF", "BROADWAY", "CRESCENT", "APPROACH", "PRECINCT",
})
_STREET_SUFFIX_ALIASES: dict[str, str] = {
    "ST": "STREET", "RD": "ROAD", "AVE": "AVENUE", "LN": "LANE",
    "CL": "CLOSE", "DR": "DRIVE", "TER": "TERRACE", "SQ": "SQUARE",
}
_STREET_SUFFIX_PATTERN: str = "|".join(
    sorted(set(_STREET_SUFFIXES) | set(_STREET_SUFFIX_ALIASES.keys()), key=len, reverse=True),
)
_STREET_KEY_RE = re.compile(rf"\b([A-Z][A-Z0-9]*)\s*({_STREET_SUFFIX_PATTERN})\b")


def _extract_street_key(address: str) -> str:
    """Return a normalised street identifier (e.g. 'HIGH STREET') from an address.

    Mirrors the logic in api/engine/csa.py so that same-street matching in the
    narrative layer is consistent with the CSA engine's street detection.
    """
    clean = re.sub(r"['\-]", "", (address or "").upper())
    clean = re.sub(r"[^A-Z0-9 ]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    m = _STREET_KEY_RE.search(clean)
    if m:
        stem, suffix = m.group(1), m.group(2)
        canonical = _STREET_SUFFIX_ALIASES.get(suffix, suffix)
        if canonical in _STREET_SUFFIXES:
            return f"{stem} {canonical}"

    tokens = clean.split()
    for i, token in enumerate(tokens):
        canonical = _STREET_SUFFIX_ALIASES.get(token, token)
        if canonical in _STREET_SUFFIXES and i > 0:
            return f"{tokens[i - 1]} {canonical}"
    return ""


def _infer_voa_match_quality(reconciliation: dict | None) -> MatchQuality:
    if not reconciliation:
        return "none"
    overall = str(reconciliation.get("overall_status") or "").lower()
    if overall == "yes":
        return "exact"
    if overall in {"partially", "no"}:
        return "partial"
    return "none"


def _extract_deltas(reconciliation: dict | None) -> tuple[float | None, float | None]:
    if not reconciliation:
        return None, None
    checks = reconciliation.get("checks") or {}

    gross = checks.get("gross_floor_space") or {}
    area_delta_pct = gross.get("percentage_difference")

    split = checks.get("floor_split") or {}
    per_floor = split.get("per_floor_comparison") or {}
    split_values: list[float] = []
    for floor_data in per_floor.values():
        pct = floor_data.get("percentage_difference") if isinstance(floor_data, dict) else None
        if pct is not None:
            split_values.append(abs(float(pct)))
    floor_split_delta_pct = max(split_values) if split_values else None
    return area_delta_pct, floor_split_delta_pct


def _compute_dispersion_ratio(comparables: list[dict], weighted_tone: float) -> float:
    rates = [float(c.get("rate") or c.get("rate_psm") or 0) for c in comparables if isinstance(c, dict)]
    rates = [r for r in rates if r > 0]
    if not rates or weighted_tone <= 0:
        return 0.0
    if len(rates) == 1:
        return 0.0
    q1, _, q3 = quantiles(sorted(rates), n=4, method="inclusive")
    return max(0.0, (q3 - q1) / weighted_tone)


def _canonical_case_strength(case_strength_value: str | None) -> CaseStrength | None:
    raw = str(case_strength_value or "").strip().lower()
    mapping = {
        "strong": "strong",
        "high": "strong",
        "moderate": "moderate",
        "medium": "moderate",
        "weak": "weak",
        "low": "weak",
    }
    return mapping.get(raw)


def build_narrative_signals(case_result: dict) -> NarrativeSignals:
    comparables = case_result.get("comparables") or []
    current_rv = float(case_result.get("voa_rv") or 0.0)
    modelled_rv = float(case_result.get("modelled_rv") or case_result.get("adjusted_estimated_rv") or case_result.get("base_estimated_rv") or 0.0)
    rv_delta_abs = current_rv - modelled_rv
    rv_delta_pct = 0.0 if current_rv <= 0 else (rv_delta_abs / current_rv) * 100.0

    valuation_basis = str(case_result.get("valuation_basis") or "").upper()
    valuation_basis_sqm = float(case_result.get("valuation_basis_sqm") or 0.0)
    subject_nia = float(case_result.get("nia_sqm") or 0.0)
    if valuation_basis.startswith("ITZA") and valuation_basis_sqm > 0:
        subject_area_basis = valuation_basis_sqm
    else:
        subject_area_basis = subject_nia

    weighted_tone = float(case_result.get("final_tone_psm") or case_result.get("tone_rate") or 0.0)
    subject_implied_tone = current_rv / subject_area_basis if subject_area_basis > 0 else 0.0
    tone_gap_pct = 0.0 if weighted_tone <= 0 else ((subject_implied_tone - weighted_tone) / weighted_tone) * 100.0
    tone_position = classify_tone_position(tone_gap_pct)

    evidence_count = int(case_result.get("comp_count") or len(comparables) or 0)

    subject_street = _extract_street_key(case_result.get("property_address") or "")
    same_street_comp_count = sum(
        1
        for c in comparables
        if _extract_street_key(c.get("address") or "") == subject_street and subject_street
    )

    subject_postcode = str(case_result.get("postcode") or "").strip().upper()
    same_postcode_comp_count = sum(
        1
        for c in comparables
        if str(c.get("postcode") or "").strip().upper() == subject_postcode and subject_postcode
    )

    reconciliation = case_result.get("voa_reconciliation") or {}
    voa_match_quality = _infer_voa_match_quality(reconciliation)
    area_delta_pct, floor_split_delta_pct = _extract_deltas(reconciliation)
    mismatch_band = classify_mismatch_band(voa_match_quality, area_delta_pct, floor_split_delta_pct)

    layout_weighting_applied = bool(case_result.get("layout_adjustment_applied"))
    floor_configuration = case_result.get("floor_config")
    sector = str(case_result.get("business_type") or "")
    structural_band = classify_structural_band(layout_weighting_applied, floor_configuration, sector)

    dispersion_ratio = _compute_dispersion_ratio(comparables, weighted_tone)
    dispersion_band = classify_dispersion_band(dispersion_ratio)
    evidence_strength = classify_evidence_strength(
        evidence_count=evidence_count,
        same_street_comp_count=same_street_comp_count,
        same_postcode_comp_count=same_postcode_comp_count,
    )

    canonical_strength = _canonical_case_strength(case_result.get("case_strength"))
    derived_strength = classify_case_strength(rv_delta_pct, evidence_strength, dispersion_band)

    return NarrativeSignals(
        sector=sector,
        case_strength=canonical_strength or derived_strength,
        current_rv=current_rv,
        modelled_rv=modelled_rv,
        rv_delta_abs=rv_delta_abs,
        rv_delta_pct=rv_delta_pct,
        evidence_count=evidence_count,
        same_street_comp_count=same_street_comp_count,
        same_postcode_comp_count=same_postcode_comp_count,
        weighted_tone=weighted_tone,
        subject_implied_tone=subject_implied_tone,
        tone_gap_pct=tone_gap_pct,
        tone_position=tone_position,
        voa_match_quality=voa_match_quality,
        area_delta_pct=float(area_delta_pct or 0.0),
        floor_split_delta_pct=float(floor_split_delta_pct or 0.0),
        mismatch_band=mismatch_band,
        layout_weighting_applied=layout_weighting_applied,
        floor_configuration=floor_configuration,
        structural_band=structural_band,
        dispersion_band=dispersion_band,
        evidence_strength=evidence_strength,
    )


def render_positioning_support(signals: NarrativeSignals) -> str:
    if signals.same_street_comp_count >= 3:
        return "This conclusion is supported by a meaningful body of nearby same-street evidence."
    if signals.same_postcode_comp_count >= 5:
        return "This conclusion is supported by a meaningful body of nearby comparable evidence within the immediate locality."
    if signals.evidence_count >= 15:
        return "This conclusion is supported by a sufficiently sized local comparable set."
    if signals.evidence_count >= 8:
        return "This conclusion is based on a moderate body of local comparable evidence."
    return "This conclusion is based on a limited body of comparable evidence and should be treated with additional caution."


def render_case_strength_support(signals: NarrativeSignals) -> Optional[str]:
    if signals.mismatch_band in {"moderate", "material"}:
        return "Case strength should be considered alongside the differences noted between the entered property details and the matched VOA record."
    if signals.dispersion_band == "high":
        return "The spread of comparable evidence is relatively wide, which reduces certainty in the inferred tone."
    return None


def render_recommended_action(signals: NarrativeSignals) -> str:
    if signals.case_strength == "strong":
        if signals.mismatch_band in {"moderate", "material"}:
            return "Recommended next step: submit a Check focused first on factual verification, then proceed to Challenge if the valuation remains unchanged."
        return "Recommended next step: proceed to Check and prepare to advance to Challenge if the assessment is not corrected."

    if signals.case_strength == "moderate":
        return "Recommended next step: submit a Check to verify property facts and test the basis of assessment before deciding whether to proceed further."

    return "Recommended next step: only proceed if factual errors can be established or if stronger comparable evidence becomes available."


def render_narrative_blocks(signals: NarrativeSignals) -> dict:
    evidence_lines = [
        POSITIONING_TEMPLATES[signals.tone_position],
        render_positioning_support(signals),
    ]

    if signals.mismatch_band != "none" or signals.voa_match_quality != "exact":
        evidence_lines.append(MISMATCH_TEMPLATES[signals.mismatch_band])

    if signals.layout_weighting_applied:
        evidence_lines.append(STRUCTURAL_TEMPLATES[signals.structural_band])

    case_lines = [CASE_STRENGTH_TEMPLATES[signals.case_strength]]
    extra_case_line = render_case_strength_support(signals)
    if extra_case_line:
        case_lines.append(extra_case_line)

    return {
        "evidence_interpretation": " ".join(evidence_lines),
        "case_assessment": " ".join(case_lines),
        "recommended_action": render_recommended_action(signals),
    }


def build_rendered_narrative(case_result: dict) -> dict:
    signals = build_narrative_signals(case_result)
    rendered = render_narrative_blocks(signals)
    rendered["narrative_signals"] = asdict(signals)
    return rendered
