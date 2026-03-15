"""
Full zoning valuation calculation.

Used for report generation (not the quick /assess check).
Applies per-property allowances from the relevant business-type rule file.
"""
from __future__ import annotations

from typing import Optional

from api.engine.csa import itza_from_nia
from api.engine.rules import business_rules
from api.models import AreasInput, FlagsInput, NurseryInput, PropertyInput


def calculate_rv(
    property: PropertyInput,
    tone_rate: float,
    areas: Optional[AreasInput] = None,
    nursery: Optional[NurseryInput] = None,
    flags: Optional[FlagsInput] = None,
) -> dict:
    """
    Calculate a modelled RV using the appropriate method for the business type.

    Returns a dict containing:
        method, rv, base_rv, adjustments, adjustment_multiplier,
        and method-specific fields (itza / nia / tone_rate).
    """
    btype = property.business_type.value
    rules = business_rules(btype)
    flags = flags or FlagsInput(consent_disclaimer=True)

    if rules.get("valuation_method") == "nia_only":
        return _nursery_rv(property, tone_rate, nursery or NurseryInput(), flags, rules)
    return _zoning_rv(property, tone_rate, areas, flags, rules)


# ---------------------------------------------------------------------------
# Zoning method
# ---------------------------------------------------------------------------

def _zoning_rv(
    property: PropertyInput,
    tone_rate: float,
    areas: Optional[AreasInput],
    flags: FlagsInput,
    rules: dict,
) -> dict:
    zone_depth = rules.get("zoning", {}).get("zone_depth_m", 6.1)
    itza = itza_from_nia(property.nia_sqm, zone_depth)

    adj_multiplier = 1.0
    adjustments: list[dict] = []

    for name, rule in rules.get("allowances", {}).items():
        trigger = rule.get("trigger", "")
        adj = float(rule.get("adjustment", 0))
        if _eval_trigger(trigger, property, areas, flags):
            adj_multiplier += adj
            adjustments.append({"name": name, "pct": round(adj * 100, 1)})

    base_rv = tone_rate * itza
    adjusted_rv = base_rv * adj_multiplier
    rv = round(adjusted_rv / 100) * 100

    return {
        "method": "zoning",
        "itza": round(itza, 2),
        "tone_rate": tone_rate,
        "base_rv": round(base_rv, 2),
        "adjustments": adjustments,
        "adjustment_multiplier": round(adj_multiplier, 4),
        "rv": rv,
    }


# ---------------------------------------------------------------------------
# NIA-only method (nurseries)
# ---------------------------------------------------------------------------

def _nursery_rv(
    property: PropertyInput,
    tone_rate: float,
    nursery: NurseryInput,
    flags: FlagsInput,
    rules: dict,
) -> dict:
    adj_multiplier = 1.0
    adjustments: list[dict] = []

    for name, rule in rules.get("adjustments", {}).items():
        trigger = rule.get("trigger", "")
        adj = float(rule.get("adjustment", 0))
        if _eval_nursery_trigger(trigger, nursery, flags):
            adj_multiplier += adj
            adjustments.append({"name": name, "pct": round(adj * 100, 1)})

    base_rv = tone_rate * property.nia_sqm
    rv = round(base_rv * adj_multiplier / 100) * 100

    return {
        "method": "nia_only",
        "nia": property.nia_sqm,
        "tone_rate": tone_rate,
        "base_rv": round(base_rv, 2),
        "adjustments": adjustments,
        "adjustment_multiplier": round(adj_multiplier, 4),
        "rv": rv,
    }


# ---------------------------------------------------------------------------
# Trigger evaluators
# ---------------------------------------------------------------------------

def _eval_trigger(
    trigger: str,
    property: PropertyInput,
    areas: Optional[AreasInput],
    flags: FlagsInput,
) -> bool:
    t = trigger.strip()
    if "frontage_m < 3.0" in t:
        return property.frontage_m is not None and property.frontage_m < 3.0
    if "layout_flag == true" in t:
        return flags.layout_flag
    if "depth_m > 20" in t:
        return property.depth_m is not None and property.depth_m > 20
    if "outdoor_seating == true" in t:
        return areas is not None and areas.outdoor_seating
    if "cramped_flag == true" in t:
        return flags.cramped_flag
    if "fitout_year >= 2020" in t:
        return flags.fitout_year is not None and flags.fitout_year >= 2020
    return False


def _eval_nursery_trigger(
    trigger: str,
    nursery: NurseryInput,
    flags: FlagsInput,
) -> bool:
    t = trigger.strip()
    if "purpose_built == true" in t:
        return nursery.purpose_built
    if "purpose_built == false" in t:
        return not nursery.purpose_built
    if "outdoor_play == true" in t:
        return nursery.outdoor_play
    if "layout_flag == true" in t:
        return flags.layout_flag
    return False
