"""
Full zoning valuation calculation.

Used for report generation (not the quick /assess check).
Applies per-property allowances from the relevant business-type rule file.
"""
from __future__ import annotations

import re
from typing import Optional

from api.engine.csa import itza_from_geometry, itza_from_nia
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
        geometry_assumed, and method-specific fields (itza / nia / tone_rate).
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

    # Use actual geometry when both dimensions are supplied; fall back to the
    # 1:3 NIA-derived assumption otherwise and record that the assumption fired.
    if property.frontage_m and property.depth_m:
        itza = itza_from_geometry(property.frontage_m, property.depth_m, zone_depth)
        geometry_assumed = False
    else:
        itza = itza_from_nia(property.nia_sqm, zone_depth)
        geometry_assumed = True

    # Adjustments are chained multiplicatively: each fires independently against
    # the running multiplier.  This matches VOA survey practice and avoids the
    # arithmetic error of summing percentages additively.
    adj_multiplier = 1.0
    adjustments: list[dict] = []

    for name, rule in rules.get("allowances", {}).items():
        trigger = rule.get("trigger", "")
        adj = float(rule.get("adjustment", 0))
        if _eval_trigger(trigger, property, areas, flags):
            adj_multiplier *= (1.0 + adj)
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
        "geometry_assumed": geometry_assumed,
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
            adj_multiplier *= (1.0 + adj)
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
        "geometry_assumed": False,  # NIA method does not use geometric assumptions
        "rv": rv,
    }


# ---------------------------------------------------------------------------
# Trigger evaluators
# ---------------------------------------------------------------------------

# Matches patterns like "frontage_m < 3.0", "layout_flag == true"
_TRIGGER_RE = re.compile(r"^(\w+)\s*(<=|>=|==|!=|<|>)\s*(.+)$")


def _resolve_field(
    field: str,
    property: PropertyInput,
    areas: Optional[AreasInput],
    flags: FlagsInput,
) -> object:
    """Return the Python value for a field name, or None if unavailable."""
    mapping = {
        "frontage_m": property.frontage_m,
        "depth_m": property.depth_m,
        "layout_flag": flags.layout_flag,
        "cramped_flag": flags.cramped_flag,
        "fitout_year": flags.fitout_year,
        "outdoor_seating": areas.outdoor_seating if areas else False,
    }
    return mapping.get(field)


def _resolve_nursery_field(
    field: str,
    nursery: NurseryInput,
    flags: FlagsInput,
) -> object:
    mapping = {
        "purpose_built": nursery.purpose_built,
        "outdoor_play": nursery.outdoor_play,
        "layout_flag": flags.layout_flag,
    }
    return mapping.get(field)


def _apply_op(op: str, actual: object, target: object) -> bool:
    """Apply a comparison operator between two values."""
    ops = {
        "<":  lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
        ">":  lambda a, b: a > b,
        ">=": lambda a, b: a >= b,
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
    }
    fn = ops.get(op)
    return fn(actual, target) if fn else False


def _parse_trigger(trigger: str, actual_value: object) -> Optional[tuple]:
    """
    Parse "field op value" and return (op, coerced_actual, coerced_target)
    or None if the trigger string is unrecognised.
    """
    m = _TRIGGER_RE.match(trigger.strip())
    if not m:
        return None
    _, op, raw_target = m.group(1), m.group(2), m.group(3).strip().lower()

    if raw_target == "true":
        return (op, bool(actual_value), True)
    if raw_target == "false":
        return (op, bool(actual_value), False)
    # Numeric comparison — coerce both sides to float
    try:
        return (op, float(actual_value), float(raw_target))
    except (TypeError, ValueError):
        return None


def _eval_trigger(
    trigger: str,
    property: PropertyInput,
    areas: Optional[AreasInput],
    flags: FlagsInput,
) -> bool:
    m = _TRIGGER_RE.match(trigger.strip())
    if not m:
        return False
    field = m.group(1)
    actual = _resolve_field(field, property, areas, flags)
    if actual is None:
        return False
    parsed = _parse_trigger(trigger, actual)
    if parsed is None:
        return False
    op, coerced_actual, target = parsed
    return _apply_op(op, coerced_actual, target)


def _eval_nursery_trigger(
    trigger: str,
    nursery: NurseryInput,
    flags: FlagsInput,
) -> bool:
    m = _TRIGGER_RE.match(trigger.strip())
    if not m:
        return False
    field = m.group(1)
    actual = _resolve_nursery_field(field, nursery, flags)
    if actual is None:
        return False
    parsed = _parse_trigger(trigger, actual)
    if parsed is None:
        return False
    op, coerced_actual, target = parsed
    return _apply_op(op, coerced_actual, target)
