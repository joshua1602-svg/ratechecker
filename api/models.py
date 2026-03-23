from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


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


class PurchaseFormData(BaseModel):
    contact: ContactInput
    property: PropertyInput
    areas: Optional[AreasInput] = None
    nursery: Optional[NurseryInput] = None
    flags: FlagsInput


class PurchaseRequest(BaseModel):
    product: str  # "report" or "evidence"
    form_data: PurchaseFormData


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


class EvidenceReportRequest(SimplifiedReportRequest):
    uprn: Optional[str] = None
    voa_description: Optional[str] = None
    nia_sqm: Optional[float] = None
    modelled_rv: Optional[float] = None
    final_tone_psm: Optional[float] = None
    tone_basis: Optional[str] = None
    confidence: Optional[str] = None
    recommendation_text: Optional[str] = None
    zoning_rows: list[dict] = Field(default_factory=list)
    nursery_adjustments: list[dict] = Field(default_factory=list)
    allowances_summary: Optional[str] = None
    subtotal_pre: Optional[float] = None
    floor_config: Optional[str] = None
    ground_floor_trading_sqm: Optional[float] = None
    ground_floor_storage_sqm: Optional[float] = None
    kitchen_area_sqm: Optional[float] = None
    kitchen_on_ground: Optional[str] = None


class PurchaseResponse(BaseModel):
    checkout_url: str


# ---------------------------------------------------------------------------
# Canonical report payload builder
# ---------------------------------------------------------------------------
# This function produces the authoritative report payload from actual
# /assess engine outputs.  The frontend must call this (or use the values
# it computes) rather than inventing report fields.

_SAVING_MARGIN = 0.05  # ±5% around the point estimate for low/high range


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
        "comp_count": assess_response.comparable_count or 0,
        "tone_rate": tone_rate,
        "base_estimated_rv": base_rv,
        "adjusted_estimated_rv": adj_rv,
        "rate_basis": None,  # set by caller from CSA result if available
    }
