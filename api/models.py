from __future__ import annotations

from datetime import datetime, timezone
import logging
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field
from api.reports.narrative import build_rendered_narrative

logger = logging.getLogger(__name__)


class BusinessType(str, Enum):
    restaurant_cafe = "restaurant_cafe"
    retail = "retail"
    hair_beauty = "hair_beauty"
    nursery = "nursery"
    pub = "pub"


class ContactInput(BaseModel):
    email: str
    business_name: str = ""
    phone: Optional[str] = None


class PropertyInput(BaseModel):
    address: str = ""
    postcode: str
    uprn: Optional[str] = None
    property_reference: Optional[str] = None
    business_type: BusinessType
    voa_rv: float = 0
    nia_sqm: float
    frontage_m: Optional[float] = None
    depth_m: Optional[float] = None
    floors: Optional[int] = None
    layout_notes: Optional[str] = None


class AreasInput(BaseModel):
    sales_area_sqm: float = 0
    visible_kitchen_sqm: float = 0
    non_visible_kitchen_sqm: float = 0
    storage_sqm: float = 0
    basement_sqm: float = 0
    upper_sqm: float = 0
    outdoor_seating: bool = False


class LayoutInputModel(BaseModel):
    """User-provided layout description for the overweighting layer."""
    floor_config: str = "ground_only"
    ground_floor_trading_sqm: float = 0.0
    ground_floor_storage_sqm: float = 0.0
    lower_ground_use: str = "not_applicable"
    upper_floor_use: str = "not_applicable"
    kitchen_on_ground: str = "no_kitchen"


class NurseryInput(BaseModel):
    purpose_built: bool = False
    outdoor_play: bool = False


class FlagsInput(BaseModel):
    layout_flag: bool = False
    cramped_flag: bool = False
    fitout_year: Optional[int] = None
    consent_disclaimer: bool


class AssessRequest(BaseModel):
    contact: ContactInput
    property: PropertyInput
    areas: Optional[AreasInput] = None
    nursery: Optional[NurseryInput] = None
    layout: Optional[LayoutInputModel] = None
    flags: FlagsInput
    captcha_token: Optional[str] = None


class AdjustmentItem(BaseModel):
    """A single rule from the sector YAML, with its trigger outcome."""
    name: str
    source: str          # e.g. "retail.yaml", "restaurant_cafe.yaml"
    factor: float        # signed decimal, e.g. -0.08 for -8%
    triggered: bool      # True when the rule fired against the subject inputs


class AdjustmentBreakdown(BaseModel):
    """Full adjustment-layer result for a single valuation."""
    applied: list[AdjustmentItem]
    total_adjustment_factor: float   # cumulative multiplier, e.g. 0.874


class AssessResponse(BaseModel):
    signal: str  # "High", "Medium", "Low", "Insufficient Data"
    explanation: str
    comparable_count: Optional[int] = None
    saving_estimate: Optional[str] = None
    tone_rate: Optional[float] = None
    base_estimated_rv: Optional[int] = None
    adjusted_estimated_rv: Optional[int] = None
    adjustments: Optional[AdjustmentBreakdown] = None
    adjustment_summary: Optional[str] = None
    rated_comps: list[dict] = Field(default_factory=list)
    location_signals: Optional[dict] = None
    tone_source: Optional[str] = None
    tone_source_label: Optional[str] = None


class PurchaseFormData(BaseModel):
    """Legacy purchase form — retained for reference."""
    contact: ContactInput
    property: PropertyInput
    areas: Optional[AreasInput] = None
    nursery: Optional[NurseryInput] = None
    flags: FlagsInput


class PaidIntakeData(BaseModel):
    """Second-screen paid intake data captured after the free /assess step.

    These fields enrich the final paid report beyond what was submitted to
    /assess on screen 1.  The structure mirrors AssessRequest sub-models so
    paid_intake can be cleanly merged into the stored assess_request before
    report generation.

    The frontend may also send flat top-level convenience keys (e.g.
    business_name, address, postcode, nia_sqm, voa_rv) which are re-routed
    into the correct nested position by _merge_paid_intake().
    """
    # Sub-model overrides (nested dicts matching AssessRequest structure)
    contact: Optional[dict] = None    # ContactInput overrides (business_name, email)
    property: Optional[dict] = None   # partial PropertyInput overrides (address, uprn, frontage_m, depth_m, floors)
    layout: Optional[dict] = None     # LayoutInputModel fields
    areas: Optional[dict] = None      # AreasInput fields
    nursery: Optional[dict] = None    # NurseryInput fields
    flags: Optional[dict] = None      # FlagsInput updates

    # Flat convenience keys — the frontend may send these at the top level
    # instead of nesting them under contact/property.  _merge_paid_intake()
    # re-routes them into the correct nested position.
    business_name: Optional[str] = None
    address: Optional[str] = None
    postcode: Optional[str] = None
    nia_sqm: Optional[float] = None
    voa_rv: Optional[float] = None
    has_parking: Optional[bool] = None


class PurchaseRequest(BaseModel):
    """Purchase request — persists full report draft before Stripe checkout.

    The frontend must send:
      - product: "report" or "evidence"
      - assess_request: the full AssessRequest JSON used for the free /assess call
      - assess_response: the full AssessResponse JSON returned by /assess
      - paid_intake: second-screen paid intake data (layout, areas, property enrichments)

    The frontend may also pass rated_comps as a top-level sibling field if
    it stores them separately from the assess response.  The purchase handler
    merges them into assess_response before persisting.
    """
    product: str  # "report" or "evidence"
    assess_request: dict  # Full AssessRequest JSON from screen 1
    assess_response: dict  # Full AssessResponse JSON from /assess
    paid_intake: PaidIntakeData = Field(default_factory=PaidIntakeData)
    rated_comps: Optional[list[dict]] = None  # Fallback if frontend stores comps separately


class SimplifiedReportRequest(BaseModel):
    """Report payload — all fields are required with no defaults.

    This model must be populated from actual /assess engine outputs via
    build_report_payload_from_assess().  Placeholder/synthetic data is
    rejected at the route level.
    """
    business_name: str
    property_address: str
    postcode: str
    business_type: str
    date_prepared: str
    voa_rv: float
    modelled_rv_low: float
    modelled_rv_high: float
    annual_saving_low: float
    annual_saving_high: float
    case_strength: str
    comparables: list[dict] = Field(default_factory=list)
    comp_count: int
    layout_adjustment_applied: Optional[bool] = None
    summary_text: Optional[str] = None
    # Engine-derived canonical fields
    tone_rate: Optional[float] = None
    base_estimated_rv: Optional[float] = None
    adjusted_estimated_rv: Optional[float] = None
    rate_basis: Optional[str] = None  # "ITZA" or "NIA" — from CSA
    voa_reconciliation: Optional[dict] = None


class EvidenceReportRequest(SimplifiedReportRequest):
    """Evidence pack payload — fields required by _EVIDENCE_REQUIRED_FIELDS
    are non-optional so Pydantic catches missing values before the route does.
    """
    uprn: Optional[str] = ""
    voa_description: str
    nia_sqm: float
    modelled_rv: float
    final_tone_psm: float
    tone_basis: str
    tone_source_label: Optional[str] = None
    confidence: str
    recommendation_text: str
    # Valuation detail (dynamic, business-type-aware)
    valuation_method: str = "itza"
    valuation_basis: str = "ITZA (Zoning)"
    valuation_basis_sqm: Optional[float] = None
    geometry_assumed: bool = False
    geometry_source_indicator: Optional[str] = None
    zoning_rows: list[dict] = Field(default_factory=list)
    nursery_adjustments: list[dict] = Field(default_factory=list)
    adjustment_items: list[dict] = Field(default_factory=list)
    adjustment_factor: float = 1.0
    allowances_summary: Optional[str] = None
    subtotal_pre: Optional[float] = None
    floor_config: Optional[str] = None
    ground_floor_trading_sqm: Optional[float] = None
    ground_floor_storage_sqm: Optional[float] = None
    kitchen_area_sqm: Optional[float] = None
    kitchen_on_ground: Optional[str] = None
    voa_reconciliation: Optional[dict] = None
    evidence_interpretation: Optional[str] = None
    case_assessment: Optional[str] = None
    recommended_action: Optional[str] = None
    narrative_signals: Optional[dict] = None
    location_signals: Optional[dict] = None
    crime_adjustment_indicator: Optional[str] = None
    flood_adjustment_indicator: Optional[str] = None


class PurchaseResponse(BaseModel):
    checkout_url: str
    session_id: str  # pending_reports draft ID — use for /report/download/{session_id}


# ---------------------------------------------------------------------------
# Canonical report payload builder
# ---------------------------------------------------------------------------
# This function produces the authoritative report payload from actual
# /assess engine outputs.  The frontend must call this (or use the values
# it computes) rather than inventing report fields.

_SAVING_MARGIN = 0.05  # ±5% around the point estimate for low/high range


def _candidate_floor_presence(candidate: dict) -> tuple[bool, bool]:
    floor = candidate.get("floor_areas") or {}
    return (bool((floor.get("ground") or 0) > 0), bool((floor.get("basement") or 0) > 0))


def _resolve_subject_candidate(
    *,
    candidates: list[dict],
    business_type: str,
    user_ground: bool | None,
    user_basement: bool | None,
    user_total_area_sqm: float | None,
) -> tuple[dict | None, str]:
    """Resolve ambiguous address candidates deterministically.

    Priority: retail SCAT filter -> floor presence -> area proximity -> stable tie-break.
    Returns (candidate, resolution_signal).
    """
    if not candidates:
        return None, "no_candidates"
    if len(candidates) == 1:
        return candidates[0], "single_candidate"

    pool = list(candidates)
    if business_type == "retail":
        retail = [c for c in pool if c.get("scat_code") in {249, 251}]
        if retail:
            pool = retail
            if len(pool) == 1:
                return pool[0], "retail_scat_filter"

    scored: list[tuple[int, float, str, dict]] = []
    has_floor_signal = user_ground is not None and user_basement is not None
    has_area_signal = user_total_area_sqm is not None
    for c in pool:
        floor_score = 0
        cand_ground, cand_basement = _candidate_floor_presence(c)
        if has_floor_signal:
            floor_score += 1 if cand_ground == user_ground else 0
            floor_score += 1 if cand_basement == user_basement else 0
        total = c.get("total_area_sqm")
        if has_area_signal and total is not None:
            area_diff = abs(float(total) - float(user_total_area_sqm))
        else:
            area_diff = float("inf")
        scored.append((floor_score, area_diff, str(c.get("uarn") or ""), c))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    best = scored[0]

    # If we had no floor/area signal and still many, treat as unresolved ambiguity.
    if len(scored) > 1 and not has_floor_signal and not has_area_signal:
        return None, "ambiguous_unresolved"

    if len(scored) > 1:
        runner_up = scored[1]
        if best[0] != runner_up[0]:
            return best[3], "floor_presence"
        if best[1] != runner_up[1]:
            return best[3], "floor_area_proximity"
    return best[3], "deterministic_tie_breaker"


def _build_voa_reconciliation(request: AssessRequest) -> dict | None:
    """Build supplementary VOA reconciliation diagnostics for report rendering."""
    from api.services.voa_reconciliation import reconcile_subject_against_voa

    normalized_facts, user_payload, user_total = _build_voa_reconciliation_inputs(request)
    voa_record, lookup_path = resolve_subject_voa_record(request, normalized_facts, user_total)

    logger.info(
        "VOA reconciliation subject lookup path=%s found=%s subject_id=%s scat_code=%s total_nia=%s ground_sqm=%s basement_sqm=%s",
        lookup_path,
        bool(voa_record),
        (voa_record or {}).get("uarn"),
        (voa_record or {}).get("scat_code"),
        (voa_record or {}).get("total_area_sqm"),
        ((voa_record or {}).get("floor_areas") or {}).get("ground"),
        ((voa_record or {}).get("floor_areas") or {}).get("basement"),
    )

    if voa_record is None:
        voa_record = {
            "total_area_sqm": None,
            "floor_areas": {"ground": None, "basement": None},
            "scat_code": None,
            "description": None,
        }

    return reconcile_subject_against_voa(
        user_payload=user_payload,
        voa_subject_record=voa_record,
        normalized_facts=normalized_facts,
    )["voa_reconciliation"]


def _build_voa_reconciliation_inputs(request: AssessRequest) -> tuple[dict, dict, float | None]:
    user_total = request.property.nia_sqm
    floor_config = request.layout.floor_config if request.layout is not None else None
    user_basement = bool(request.areas and (request.areas.basement_sqm or 0) > 0)
    if floor_config in {"ground_lower_ground", "ground_lower_ground_first", "ground_and_basement", "ground_basement_first"}:
        user_basement = True
    user_ground = True if floor_config is not None else None

    normalized_facts = {
        "ground_present": user_ground,
        "basement_present": user_basement if user_ground is not None else None,
        "floor_areas": {
            "ground": (request.areas.sales_area_sqm if request.areas is not None else None),
            "basement": (request.areas.basement_sqm if request.areas is not None else None),
        },
    }
    user_payload = {
        "business_type": request.property.business_type.value,
        "total_area_sqm": user_total,
    }
    return normalized_facts, user_payload, user_total


def resolve_subject_voa_record(
    request: AssessRequest,
    normalized_facts: dict | None = None,
    user_total_area_sqm: float | None = None,
) -> tuple[dict | None, str]:
    """Resolve the matched VOA subject record using the structured lookup path."""
    from api.db import (
        DatabaseError,
        get_subject_voa_candidates_by_address_postcode,
        get_subject_voa_record,
        get_subject_voa_record_by_reference,
    )

    if normalized_facts is None:
        normalized_facts, _, _ = _build_voa_reconciliation_inputs(request)
    if user_total_area_sqm is None:
        user_total_area_sqm = request.property.nia_sqm

    voa_record = None
    lookup_path = "none"
    optional_reference = request.property.property_reference or None

    try:
        logger.info("VOA reconciliation lookup optional_reference_supplied=%s", bool(optional_reference))

        if optional_reference:
            voa_record = get_subject_voa_record_by_reference(optional_reference)
            if voa_record is not None:
                lookup_path = "reference_override"
            else:
                logger.info("VOA reconciliation reference override did not resolve; falling back to address/postcode")

        if voa_record is None:
            candidates = get_subject_voa_candidates_by_address_postcode(
                address=request.property.address,
                postcode=request.property.postcode,
            )
            logger.info(
                "VOA reconciliation address/postcode candidate_count=%s disambiguation_needed=%s",
                len(candidates),
                len(candidates) > 1,
            )
            voa_record, signal = _resolve_subject_candidate(
                candidates=candidates,
                business_type=request.property.business_type.value,
                user_ground=normalized_facts.get("ground_present"),
                user_basement=normalized_facts.get("basement_present"),
                user_total_area_sqm=user_total_area_sqm,
            )
            if voa_record is not None:
                lookup_path = f"address_postcode_{signal}"

        if voa_record is None and request.property.uprn:
            voa_record = get_subject_voa_record(request.property.uprn)
            if voa_record is not None:
                lookup_path = "uprn_internal_fallback"
    except DatabaseError:
        voa_record = None

    return voa_record, lookup_path


def build_report_payload_from_assess(
    assess_response: AssessResponse,
    request: AssessRequest,
) -> dict:
    """Build a canonical simplified report payload from actual engine outputs.

    This is the single authoritative mapping from engine → report.  Every
    value in the returned dict is derived from the engine's actual outputs.
    The frontend should pass this dict (or its JSON form) to /report/simplified.

    Returns a dict suitable for SimplifiedReportRequest.model_validate().
    """
    voa_rv = request.property.voa_rv
    tone_rate = assess_response.tone_rate
    base_rv = assess_response.base_estimated_rv
    adj_rv = assess_response.adjusted_estimated_rv

    # Use adjusted RV if available, otherwise base RV
    best_rv = adj_rv if adj_rv is not None else base_rv

    # Compute low/high range as ±5% of best estimate (conservative)
    if best_rv is not None:
        rv_low = round(best_rv * (1 - _SAVING_MARGIN) / 100) * 100
        rv_high = round(best_rv * (1 + _SAVING_MARGIN) / 100) * 100
    else:
        rv_low = None
        rv_high = None

    # Savings: difference between VOA RV and our modelled range
    if rv_low is not None and voa_rv > 0:
        saving_low = max(0, round(voa_rv - rv_high))
        saving_high = max(0, round(voa_rv - rv_low))
    else:
        saving_low = None
        saving_high = None

    # Case strength maps directly from signal
    signal = assess_response.signal
    case_strength_map = {
        "High": "Strong",
        "Medium": "Moderate",
        "Low": "Weak",
        "Insufficient Data": "Insufficient Data",
    }

    # Per-comparable rate_psm: use the engine's rate field (which is on the
    # correct basis — ITZA for retail, NIA for nursery) rather than
    # re-deriving rv/nia_sqm which would produce a wrong-basis figure.
    comps_for_report = []
    for c in assess_response.rated_comps:
        comp = dict(c)
        # The engine's "rate" field is the correctly-normalised rate
        if "rate" in comp and comp.get("rate_psm") is None:
            comp["rate_psm"] = round(comp["rate"], 2)
        comps_for_report.append(comp)

    # comp_count must always match the actual comparables list to prevent
    # the banner showing a non-zero count while the table renders no rows.
    comp_count = len(comps_for_report)

    return {
        "business_name": request.contact.business_name or request.property.address,
        "property_address": request.property.address,
        "postcode": request.property.postcode,
        "business_type": request.property.business_type.value,
        "date_prepared": datetime.now(timezone.utc).strftime("%d %B %Y"),
        "voa_rv": voa_rv,
        "modelled_rv_low": rv_low,
        "modelled_rv_high": rv_high,
        "annual_saving_low": saving_low,
        "annual_saving_high": saving_high,
        "case_strength": case_strength_map.get(signal, signal),
        "comparables": comps_for_report,
        "comp_count": comp_count,
        "tone_rate": tone_rate,
        "base_estimated_rv": base_rv,
        "adjusted_estimated_rv": adj_rv,
        "rate_basis": None,  # set by caller from CSA result if available
        "voa_reconciliation": _build_voa_reconciliation(request),
    }


def build_evidence_payload_from_assess(
    assess_response: AssessResponse,
    request: AssessRequest,
    *,
    tone_basis: str = "Weighted median",
    recommendation_text: str | None = None,
) -> dict:
    """Build a canonical evidence pack payload from actual engine outputs.

    Extends the simplified payload with the extra fields required by
    EvidenceReportRequest and validated by _EVIDENCE_REQUIRED_FIELDS in
    the /report/evidence route.

    The valuation calculation detail (zoning rows, NIA summary, adjustments)
    is built dynamically from real engine data via build_valuation_detail().

    Returns a dict suitable for EvidenceReportRequest.model_validate().
    """
    from api.engine.valuation import build_valuation_detail

    # Start from the simplified payload (all shared fields)
    payload = build_report_payload_from_assess(assess_response, request)

    voa_rv = request.property.voa_rv
    best_rv = (
        assess_response.adjusted_estimated_rv
        if assess_response.adjusted_estimated_rv is not None
        else assess_response.base_estimated_rv
    )

    # Confidence maps from signal
    confidence_map = {
        "High": "High",
        "Medium": "Medium",
        "Low": "Low",
        "Insufficient Data": "Insufficient Data",
    }

    # Default recommendation based on signal
    if recommendation_text is None:
        signal = assess_response.signal
        if signal == "High":
            recommendation_text = (
                "The evidence supports a strong case for reduction. "
                "Proceed to Check and submit this pack as supporting "
                "evidence at Challenge stage if not resolved."
            )
        elif signal == "Medium":
            recommendation_text = (
                "There is a moderate case for reduction based on comparable evidence. "
                "Consider submitting a Check to explore further."
            )
        else:
            recommendation_text = (
                "The comparable evidence does not currently support a strong case. "
                "Monitor for future revaluations or additional comparable data."
            )

    # Build valuation detail from real engine data
    normalized_facts, _, user_total = _build_voa_reconciliation_inputs(request)
    voa_subject_record, _ = resolve_subject_voa_record(request, normalized_facts, user_total)

    tone_rate = assess_response.tone_rate or 0.0
    adj_breakdown = assess_response.adjustments
    if adj_breakdown is not None:
        adj_applied = [item.model_dump() for item in adj_breakdown.applied]
        adj_factor = adj_breakdown.total_adjustment_factor
    else:
        adj_applied = []
        adj_factor = 1.0

    valuation_detail = build_valuation_detail(
        property=request.property,
        tone_rate=tone_rate,
        adjusted_rv=best_rv or 0,
        adjustments_applied=adj_applied,
        adjustment_factor=adj_factor,
        business_type=request.property.business_type.value,
        voa_subject_record=voa_subject_record,
    )

    # Re-derive tone_source_label from rated_comps using address-based
    # street-key extraction.  This is robust against frontends that strip the
    # is_same_street flag or pass stale tone_source_label values.
    # Thresholds mirror _RETAIL_PRIMARY_TONE_SAME_STREET_* in csa.py.
    _tone_source = getattr(assess_response, "tone_source", None)
    _tone_source_label = getattr(assess_response, "tone_source_label", None)
    _rated = assess_response.rated_comps or []
    if _rated and request.property.business_type.value in ("retail", "hair_beauty"):
        from api.engine.csa import _extract_street_key
        _subj_street = _extract_street_key(request.property.address or "")
        if _subj_street:
            _ss_count = sum(
                1 for c in _rated
                if _extract_street_key(c.get("address") or "") == _subj_street
            )
            _total = len(_rated)
            _ss_share = _ss_count / _total if _total > 0 else 0.0
            if _ss_count >= 6 or _ss_share >= 0.50:
                _tone_source = "same_street_evidence"
                _tone_source_label = (
                    "Primary tone source: Same street evidence "
                    "(sufficiently strong same-street set)"
                )
            else:
                _tone_source = "wider_local"
                _tone_source_label = "Primary tone source: Wider local comparable set"

    # Legacy fallback only when CSA fields are absent.
    if (
        _rated
        and request.property.business_type.value in ("retail", "hair_beauty")
        and (_tone_source is None or _tone_source_label is None)
    ):
        from api.engine.csa import _extract_street_key

        _subject_key = _extract_street_key(request.property.address or "")
        _ss_count = 0
        for c in _rated:
            if c.get("is_same_street") is True:
                _ss_count += 1
                continue
            if c.get("is_same_street") is False:
                continue
            if _subject_key and _extract_street_key(c.get("address") or "") == _subject_key:
                _ss_count += 1
        _total = len(_rated)
        _ss_share = _ss_count / _total if _total > 0 else 0.0
        _fallback_same_street = (_ss_count >= 6 or _ss_share >= 0.50)
        if _tone_source is None:
            _tone_source = "same_street_evidence" if _fallback_same_street else "wider_local"
        if _tone_source_label is None:
            if _fallback_same_street:
                _tone_source_label = (
                    "Primary tone source: Same street evidence "
                    "(sufficiently strong same-street set)"
                )
            else:
                _tone_source_label = "Primary tone source: Wider local comparable set"

    # Evidence-specific required fields
    payload.update({
        "uprn": request.property.uprn or "",
        "voa_description": f"{request.property.business_type.value.replace('_', ' ').title()} and Premises",
        "nia_sqm": request.property.nia_sqm,
        "modelled_rv": best_rv,
        "final_tone_psm": tone_rate,
        "tone_basis": tone_basis,
        "tone_source": _tone_source,
        "tone_source_label": _tone_source_label,
        "confidence": confidence_map.get(assess_response.signal, assess_response.signal),
        "recommendation_text": recommendation_text,
        # Valuation detail — all from engine truth
        "valuation_method": valuation_detail["valuation_method"],
        "valuation_basis": valuation_detail["valuation_basis"],
        "valuation_basis_sqm": valuation_detail["valuation_basis_sqm"],
        "geometry_assumed": valuation_detail["geometry_assumed"],
        "geometry_source_indicator": valuation_detail.get("geometry_source_indicator"),
        "zoning_rows": valuation_detail["zoning_rows"],
        "nursery_adjustments": valuation_detail.get("nursery_adjustments", []),
        "adjustment_items": valuation_detail["adjustment_items"],
        "adjustment_factor": valuation_detail["adjustment_factor"],
        "allowances_summary": valuation_detail["allowances_summary"],
        "subtotal_pre": valuation_detail["subtotal_pre"],
    })

    # Layout fields from the request if provided
    if request.layout is not None:
        payload.update({
            "layout_adjustment_applied": True,
            "floor_config": request.layout.floor_config,
            "ground_floor_trading_sqm": request.layout.ground_floor_trading_sqm,
            "ground_floor_storage_sqm": request.layout.ground_floor_storage_sqm,
            "kitchen_on_ground": request.layout.kitchen_on_ground,
        })

    # Area breakdown from second-screen intake
    if request.areas is not None:
        payload.update({
            "sales_area_sqm": request.areas.sales_area_sqm or None,
            "visible_kitchen_sqm": request.areas.visible_kitchen_sqm or None,
            "storage_sqm": request.areas.storage_sqm or None,
            "basement_sqm": request.areas.basement_sqm or None,
            "upper_sqm": request.areas.upper_sqm or None,
            "outdoor_seating": request.areas.outdoor_seating,
        })

    # Nursery-specific fields
    if request.nursery is not None:
        payload.update({
            "nursery_purpose_built": request.nursery.purpose_built,
            "nursery_outdoor_play": request.nursery.outdoor_play,
        })

    # Flags from intake
    if request.flags is not None:
        payload["layout_flag"] = request.flags.layout_flag
        payload["cramped_flag"] = request.flags.cramped_flag
        if request.flags.fitout_year:
            payload["fitout_year"] = request.flags.fitout_year

    location_signals = assess_response.location_signals or {}
    crime_level = str((location_signals.get("crime") or {}).get("signal_level") or "N/A")
    flood_level = str((location_signals.get("flood") or {}).get("signal_level") or "N/A")

    payload["location_signals"] = location_signals or {
        "crime": {"signal_level": "N/A", "included_in_report": False, "narrative": "N/A"},
        "flood": {"signal_level": "N/A", "included_in_report": False, "narrative": "N/A"},
    }
    payload["crime_adjustment_indicator"] = {
        "low": "Low",
        "moderate": "Moderate",
        "elevated": "Elevated",
    }.get(crime_level.lower(), "N/A")
    payload["flood_adjustment_indicator"] = {
        "active": "Active",
        "none": "None",
    }.get(flood_level.lower(), "N/A")

    payload.update(build_rendered_narrative(payload))

    return payload
