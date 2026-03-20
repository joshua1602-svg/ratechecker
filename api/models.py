from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


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
    floor_config: str = "ground_only"  # ground_only / ground_lower_ground / ground_first / ground_lower_ground_first / other
    ground_floor_trading_sqm: float = 0.0
    ground_floor_storage_sqm: float = 0.0
    lower_ground_use: str = "not_applicable"  # trading / storage / kitchen / office / not_applicable
    upper_floor_use: str = "not_applicable"   # trading / storage / office / not_applicable
    kitchen_on_ground: str = "no_kitchen"     # yes / no / no_kitchen (restaurants only)


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


class PurchaseFormData(BaseModel):
    contact: ContactInput
    property: PropertyInput
    areas: Optional[AreasInput] = None
    nursery: Optional[NurseryInput] = None
    flags: FlagsInput


class PurchaseRequest(BaseModel):
    product: str  # "report" or "evidence"
    form_data: PurchaseFormData


class PurchaseResponse(BaseModel):
    checkout_url: str
