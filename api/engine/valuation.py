"""
Full zoning valuation calculation.

Used for report generation (not the quick /assess check).
Applies per-property allowances from the relevant business-type rule file.
"""
from __future__ import annotations

import math
import re
from typing import Optional

from api.engine.csa import itza_from_geometry, itza_from_nia
from api.engine.rules import business_rules, rule_file_name
from api.models import AreasInput, FlagsInput, NurseryInput, PropertyInput


def _relativity_from_description(
    description: str | None,
    *,
    is_retail_itza: bool = False,
) -> float | None:
    desc = str(description or "").lower()
    if is_retail_itza:
        # Retail ITZA calibration:
        # - trading basement / lower-ground retail: 20% of Zone A
        # - storage / internal storage:            10% of Zone A
        if "storage" in desc or "internal store" in desc:
            return 0.10
        if "basement" in desc or "lower ground" in desc:
            return 0.20
    if "zone a" in desc:
        return 1.0
    if "zone b" in desc:
        return 0.5
    if "zone c" in desc:
        return 0.25
    if "remainder" in desc:
        return 0.125
    return None


def itza_from_voa_sv_lines(sv_lines: list[dict], *, is_retail_itza: bool = False) -> float:
    """Compute ITZA from VOA structured valuation lines."""
    if not sv_lines:
        return 0.0

    prices = [float(r["price"]) for r in sv_lines if r.get("price") is not None and float(r["price"]) > 0]
    zone_a_price = max(prices) if prices else None

    total_itza = 0.0
    for line in sv_lines:
        area = float(line.get("area") or 0.0)
        if area <= 0:
            continue
        relativity: float | None = None
        price = line.get("price")
        if zone_a_price and price is not None and float(price) > 0:
            relativity = float(price) / zone_a_price
        if relativity is None:
            relativity = _relativity_from_description(
                line.get("description"),
                is_retail_itza=is_retail_itza,
            )
        if relativity is None:
            relativity = 1.0
        total_itza += area * relativity
    return total_itza


def _resolve_valuation_method(rules: dict) -> str:
    """Normalize rule-file method labels to the report contract values."""
    raw = str(rules.get("valuation_method") or "").strip().lower()
    if raw in {"nia", "nia_only"}:
        return "nia"
    return "itza"


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

    if _resolve_valuation_method(rules) == "nia":
        return _nia_rv(
            property=property,
            tone_rate=tone_rate,
            areas=areas,
            nursery=nursery or NurseryInput(),
            flags=flags,
            rules=rules,
            business_type=btype,
        )
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
        "method": "itza",
        "itza": round(itza, 2),
        "tone_rate": tone_rate,
        "base_rv": round(base_rv, 2),
        "adjustments": adjustments,
        "adjustment_multiplier": round(adj_multiplier, 4),
        "geometry_assumed": geometry_assumed,
        "rv": rv,
    }


# ---------------------------------------------------------------------------
# NIA method (e.g. nursery / restaurant_cafe)
# ---------------------------------------------------------------------------

def _nia_rv(
    property: PropertyInput,
    tone_rate: float,
    areas: Optional[AreasInput],
    nursery: NurseryInput,
    flags: FlagsInput,
    rules: dict,
    business_type: str,
) -> dict:
    adj_multiplier = 1.0
    adjustments: list[dict] = []

    for name, rule in rules.get("adjustments", {}).items():
        trigger = rule.get("trigger", "")
        adj = float(rule.get("adjustment", 0))
        if business_type == "nursery":
            triggered = _eval_nursery_trigger(trigger, nursery, flags)
        else:
            triggered = _eval_trigger(trigger, property, areas, flags)
        if triggered:
            adj_multiplier *= (1.0 + adj)
            adjustments.append({"name": name, "pct": round(adj * 100, 1)})

    base_rv = tone_rate * property.nia_sqm
    rv = round(base_rv * adj_multiplier / 100) * 100

    return {
        "method": "nia",
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


# ---------------------------------------------------------------------------
# Adjustment layer (post-CSA)
# ---------------------------------------------------------------------------

def apply_adjustments(
    business_type: str,
    base_rv: int,
    property: PropertyInput,
    areas: Optional[AreasInput] = None,
    nursery: Optional[NurseryInput] = None,
    flags: Optional[FlagsInput] = None,
) -> dict:
    """
    Apply the sector-specific adjustment layer to a CSA-derived base RV.

    Loads the relevant YAML rule file, evaluates every applicable rule trigger
    against the supplied inputs, and chains triggered adjustments multiplicatively.

    Parameters
    ----------
    business_type : str
        One of "retail", "hair_beauty", "restaurant_cafe", "nursery", "pub".
    base_rv : int
        The CSA-derived base rateable value (already rounded to nearest £100).
    property, areas, nursery, flags : input models
        Form inputs used to evaluate rule triggers.  Any that are None are
        replaced with safe empty defaults so that missing fields skip (not fail)
        their associated adjustment triggers.

    Returns
    -------
    dict with keys:
        base_estimated_rv       – same as base_rv (passed through unchanged)
        adjusted_estimated_rv   – base_rv × total_factor, rounded to £100
        adjustments             – breakdown dict:
            applied             – list of all applicable rules with triggered flag
            total_adjustment_factor – cumulative multiplicative factor
        adjustment_summary      – human-readable one-line summary
    """
    rules = business_rules(business_type)
    source = rule_file_name(business_type)

    _flags = flags or FlagsInput(consent_disclaimer=True)
    _areas = areas or AreasInput()
    _nursery = nursery or NurseryInput()

    # NIA-method segments declare adjustments under "adjustments"; ITZA segments
    # use "allowances".  The trigger evaluators differ accordingly.
    is_nia = _resolve_valuation_method(rules) == "nia"
    is_nursery = business_type == "nursery"
    rule_section = rules.get("adjustments" if is_nia else "allowances", {})

    adj_multiplier = 1.0
    applied: list[dict] = []

    for name, rule in rule_section.items():
        adj = float(rule.get("adjustment", 0))
        if is_nia and is_nursery:
            triggered = _eval_nursery_trigger(rule.get("trigger", ""), _nursery, _flags)
        else:
            triggered = _eval_trigger(rule.get("trigger", ""), property, _areas, _flags)

        if triggered:
            adj_multiplier *= (1.0 + adj)

        applied.append({
            "name": name,
            "source": source,
            "factor": round(adj, 4),
            "triggered": triggered,
        })

    adjusted_rv = round(base_rv * adj_multiplier / 100) * 100
    total_factor = round(adj_multiplier, 4)

    n_triggered = sum(1 for item in applied if item["triggered"])
    if n_triggered == 0:
        summary = "No adjustments applied — base RV unchanged."
    else:
        pct = round((total_factor - 1) * 100, 1)
        direction = "upward" if pct > 0 else "downward"
        names = ", ".join(item["name"] for item in applied if item["triggered"])
        summary = (
            f"{n_triggered} adjustment(s) applied ({names}): "
            f"{pct:+.1f}% {direction} (factor {total_factor:.4f}). "
            f"Adjusted RV £{adjusted_rv:,}."
        )

    return {
        "base_estimated_rv": base_rv,
        "adjusted_estimated_rv": adjusted_rv,
        "adjustments": {
            "applied": applied,
            "total_adjustment_factor": total_factor,
        },
        "adjustment_summary": summary,
    }


# ---------------------------------------------------------------------------
# Evidence-pack valuation detail builder
# ---------------------------------------------------------------------------

def build_valuation_detail(
    property: PropertyInput,
    tone_rate: float,
    adjusted_rv: int,
    adjustments_applied: list[dict],
    adjustment_factor: float,
    business_type: str,
    voa_subject_record: dict | None = None,
) -> dict:
    """Build the valuation calculation detail for the evidence pack.

    Reconstructs the per-zone breakdown (for zoning types) or NIA calculation
    (for nia-method sectors) using the same maths the engine applied, so the evidence
    pack can display truthful intermediate steps.

    Parameters
    ----------
    property : PropertyInput
        Subject property (NIA, optional frontage/depth).
    tone_rate : float
        Derived market tone (£/m²) on the basis used by the sector method.
    adjusted_rv : int
        Final modelled RV after adjustments.
    adjustments_applied : list[dict]
        Each dict has keys: name, source, factor, triggered.
    adjustment_factor : float
        Cumulative multiplicative factor (e.g. 0.874).
    business_type : str
        One of the supported business types.

    Returns a dict with keys needed by the evidence pack template.
    """
    rules = business_rules(business_type)
    method = _resolve_valuation_method(rules)

    if method == "nia":
        return _build_nia_detail(
            property, tone_rate, adjusted_rv,
            adjustments_applied, adjustment_factor, rules,
        )
    return _build_zoning_detail(
        property, tone_rate, adjusted_rv,
        adjustments_applied, adjustment_factor, rules, voa_subject_record=voa_subject_record,
    )


def _build_zoning_detail(
    property: PropertyInput,
    tone_rate: float,
    adjusted_rv: int,
    adjustments_applied: list[dict],
    adjustment_factor: float,
    rules: dict,
    *,
    voa_subject_record: dict | None = None,
) -> dict:
    zone_depth = rules.get("zoning", {}).get("zone_depth_m", 6.1)
    relativities = rules.get("zoning", {}).get("relativities", {})

    sv_lines = (voa_subject_record or {}).get("sv_lines") or []
    is_retail_itza = property.business_type.value in {"retail", "hair_beauty"}
    has_voa_geometry = bool(sv_lines)

    # Determine geometry — mirrors _zoning_rv() logic exactly for fallback cases.
    if has_voa_geometry:
        width = None
        depth = None
        geometry_assumed = False
    elif property.frontage_m and property.depth_m:
        width = property.frontage_m
        depth = property.depth_m
        geometry_assumed = False
    else:
        aspect_ratio = 3.0
        width = math.sqrt(property.nia_sqm / aspect_ratio)
        depth = property.nia_sqm / width
        geometry_assumed = True

    zoning_rows: list[dict] = []
    itza_total = 0.0
    if has_voa_geometry:
        prices = [float(r["price"]) for r in sv_lines if r.get("price") is not None and float(r["price"]) > 0]
        zone_a_price = max(prices) if prices else None
        for idx, line in enumerate(sv_lines, start=1):
            area = float(line.get("area") or 0.0)
            if area <= 0:
                continue
            line_price = line.get("price")
            line_value = line.get("value")
            relativity: float | None = None
            if zone_a_price and line_price is not None and float(line_price) > 0:
                relativity = float(line_price) / zone_a_price
            if relativity is None:
                relativity = _relativity_from_description(
                    line.get("description"),
                    is_retail_itza=is_retail_itza,
                )
            if relativity is None:
                relativity = 1.0
            itza_contrib = area * relativity
            itza_total += itza_contrib

            if line_value is None and line_price is not None:
                line_value = area * float(line_price)
            elif line_value is None:
                line_value = itza_contrib * tone_rate

            zoning_rows.append({
                "zone": str(line.get("description") or f"Line {idx}"),
                "area_sqm": round(area, 1),
                "depth_m": None,
                "relativity": round(relativity, 4),
                "tone": round(float(line_price), 2) if line_price is not None else round(tone_rate, 2),
                "value": round(float(line_value), 2),
                "floor": line.get("floor"),
                "description": line.get("description"),
            })
    else:
        # Build zone rows (same maths as itza_from_geometry)
        zone_defs = [
            ("Zone A", zone_depth, relativities.get("zone_a", 1.0)),
            ("Zone B", zone_depth, relativities.get("zone_b", 0.5)),
            ("Zone C", zone_depth, relativities.get("zone_c", 0.25)),
            ("Remainder", float("inf"), relativities.get("remainder", 0.125)),
        ]
        remaining = depth
        for zone_name, z_depth, relativity in zone_defs:
            if remaining <= 0:
                break
            used = min(remaining, z_depth)
            area = width * used
            itza_contrib = area * relativity
            value = itza_contrib * tone_rate
            itza_total += itza_contrib
            zoning_rows.append({
                "zone": zone_name,
                "area_sqm": round(area, 1),
                "depth_m": round(used, 1),
                "relativity": relativity,
                "tone": round(tone_rate, 2),
                "value": round(value, 2),
            })
            remaining -= used

    subtotal_pre = round(itza_total * tone_rate, 2)

    # Build allowances summary from triggered adjustments
    triggered = [a for a in adjustments_applied if a.get("triggered")]
    if triggered:
        parts = [
            f"{a['name'].replace('_', ' ').title()} ({a['factor'] * 100:+.0f}%)"
            for a in triggered
        ]
        allowances_summary = (
            "; ".join(parts)
            + f" — cumulative factor {adjustment_factor:.4f}"
        )
    else:
        allowances_summary = "No allowances applied — base RV unchanged."

    return {
        "valuation_method": "itza",
        "valuation_basis": "ITZA (Zoning)",
        "valuation_basis_sqm": round(itza_total, 2),
        "geometry_assumed": geometry_assumed,
        "geometry_source_indicator": (
            "VOA structured valuation record"
            if has_voa_geometry else
            "Assumed 1:3 geometry fallback"
        ),
        "zoning_rows": zoning_rows,
        "subtotal_pre": subtotal_pre,
        "adjustment_items": adjustments_applied,
        "adjustment_factor": adjustment_factor,
        "allowances_summary": allowances_summary,
    }


def _build_nia_detail(
    property: PropertyInput,
    tone_rate: float,
    adjusted_rv: int,
    adjustments_applied: list[dict],
    adjustment_factor: float,
    rules: dict,
) -> dict:
    nia = property.nia_sqm
    subtotal_pre = round(tone_rate * nia, 2)

    triggered = [a for a in adjustments_applied if a.get("triggered")]
    if triggered:
        adj_parts = [
            {
                "name": a["name"].replace("_", " ").title(),
                "pct": f"{a['factor'] * 100:+.0f}%",
                "triggered": True,
            }
            for a in triggered
        ]
        allowances_summary = (
            "; ".join(f"{p['name']} ({p['pct']})" for p in adj_parts)
            + f" — cumulative factor {adjustment_factor:.4f}"
        )
    else:
        adj_parts = []
        allowances_summary = "No adjustments applied — base RV unchanged."

    return {
        "valuation_method": "nia",
        "valuation_basis": "Comparable Tone (£/sqm NIA)",
        "valuation_basis_sqm": round(nia, 2),
        "geometry_assumed": False,
        "zoning_rows": [],
        "subtotal_pre": subtotal_pre,
        "nursery_adjustments": adj_parts,
        "adjustment_items": adjustments_applied,
        "adjustment_factor": adjustment_factor,
        "allowances_summary": allowances_summary,
    }
