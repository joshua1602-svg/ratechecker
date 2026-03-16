"""Load and cache YAML rule files from the rules/ directory."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

RULES_DIR = Path(__file__).parent.parent.parent / "rules"

# Business types that share the retail rule file
_RULE_FILE: dict[str, str] = {
    "restaurant_cafe": "restaurant_cafe",
    "retail": "retail",
    "hair_beauty": "retail",  # same zoning method as retail
    "nursery": "nursery",
    "pub": "retail",  # Phase 2; falls back to retail rules for now
}


@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    with open(RULES_DIR / f"{name}.yaml") as f:
        return yaml.safe_load(f)


def csa_rules() -> dict:
    return _load("csa")["csa"]


def general_rules() -> dict:
    """
    Load the shared general rules (rules/general.yaml).

    NOTE: general_rules() is intentionally NOT called by the valuation engine.
    Each sector YAML file (retail.yaml, restaurant_cafe.yaml, nursery.yaml)
    explicitly declares its own allowances and zoning parameters, which fully
    cover the definitions in general.yaml for Phase 1 property types.  The
    sector-specific values are the authoritative source; general.yaml serves
    as reference documentation and a fallback for future sectors.

    If a new sector is added that does not have its own allowances block, the
    engine should merge general_rules()["allowances"] as a base before
    applying sector overrides.
    """
    return _load("general")["general_rules"]


def business_rules(business_type: str) -> dict:
    rule_file = _RULE_FILE.get(business_type, "retail")
    return _load(rule_file)
