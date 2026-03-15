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
    flags: FlagsInput
    captcha_token: Optional[str] = None


class AssessResponse(BaseModel):
    signal: str  # "High", "Medium", "Low", "Insufficient Data"
    explanation: str
    comparable_count: Optional[int] = None
    saving_estimate: Optional[str] = None


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
