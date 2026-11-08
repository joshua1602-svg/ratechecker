"""RateCheck read-only MCP discovery pilot.

This standalone pilot intentionally does NOT call the /assess API, which
requires a user contact, appropriate user interaction and captcha. It also
does not grant access to paid reports or raw VOA database records.

Run locally with Python 3.11+ and MCP Python SDK 2.x. Review before any
HTTP deployment or connection to a third-party AI client.
"""

from __future__ import annotations

import re
from typing import Literal

from mcp.server import MCPServer

mcp = MCPServer("RateCheck public information pilot")

SUPPORTED_PROPERTY_TYPES = {
    "retail": "Shops and showrooms",
    "restaurant_cafe": "Restaurants and cafes",
    "hair_beauty": "Hair and beauty premises",
    "nursery": "Nurseries",
    "pub": "Public houses",
}

UK_POSTCODE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$", re.IGNORECASE)

OFFICIAL_GUIDANCE = {
    "find_rateable_value": {
        "title": "Find a business rates valuation",
        "url": "https://www.gov.uk/find-business-rates",
        "summary": "Find the published rateable value for a property in England or Wales.",
    },
    "rates_calculation": {
        "title": "How business rates are calculated",
        "url": "https://www.gov.uk/introduction-to-business-rates/how-your-rates-are-calculated",
        "summary": "Rateable value is not the same as rates payable; local council calculations and reliefs apply.",
    },
    "check_challenge": {
        "title": "Check and Challenge business rates",
        "url": "https://www.gov.uk/government/collections/how-to-challenge-your-business-rates-valuation",
        "summary": "Review the official Check, Challenge and Appeal stages and relevant deadlines.",
    },
}


@mcp.tool()
def describe_ratecheck() -> dict:
    """Describe RateCheck's free indicative property valuation comparison."""
    return {
        "name": "RateCheck",
        "website": "https://ratecheck.uk/",
        "what_it_does": (
            "Offers a free, indicative comparison of a property's 2026 "
            "rateable value with published valuations of similar properties."
        ),
        "where": "Selected commercial property types in England and Wales",
        "supported_property_types": SUPPORTED_PROPERTY_TYPES,
        "what_it_does_not_do": (
            "It does not determine the official valuation, calculate a "
            "guaranteed business rates saving, submit a challenge or replace "
            "the Valuation Office or a qualified adviser."
        ),
        "how_to_proceed": "Open https://ratecheck.uk/ and complete the on-site free check.",
    }


@mcp.tool()
def prepare_ratecheck_check(
    property_type: Literal["retail", "restaurant_cafe", "hair_beauty", "nursery", "pub"],
    postcode: str,
    floor_area_sqm: float,
    rateable_value_gbp: float | None = None,
) -> dict:
    """Validate basic inputs and explain how to start an on-site RateCheck check.

    No information is saved, transmitted or submitted to the assessment engine.
    rateable_value_gbp is the published rateable value, never the annual rates bill.
    """
    errors: list[str] = []
    normalized_postcode = " ".join(postcode.strip().upper().split())
    if not UK_POSTCODE.fullmatch(normalized_postcode):
        errors.append("Enter a valid UK postcode.")
    if floor_area_sqm <= 0 or floor_area_sqm > 1_000_000:
        errors.append("Floor area must be positive and plausible in square metres.")
    if rateable_value_gbp is not None and (
        rateable_value_gbp < 0 or rateable_value_gbp > 1_000_000_000
    ):
        errors.append("The rateable value must be non-negative and plausible.")
    if errors:
        return {"ready": False, "issues": errors, "submitted": False}

    return {
        "ready": True,
        "submitted": False,
        "next_step_url": "https://ratecheck.uk/",
        "property_type": property_type,
        "postcode": normalized_postcode,
        "floor_area_sqm": floor_area_sqm,
        "rateable_value_gbp": rateable_value_gbp,
        "message": (
            "The inputs look suitable for the on-site free check. "
            "The user must open RateCheck and complete the form. "
            "No valuation has been calculated, and no check has been submitted."
        ),
        "official_rateable_value_lookup": "https://www.gov.uk/find-business-rates",
    }


@mcp.tool()
def get_official_business_rates_guidance(
    topic: Literal["find_rateable_value", "rates_calculation", "check_challenge"],
) -> dict:
    """Provide an official link for understanding business rates, not legal advice."""
    return {
        **OFFICIAL_GUIDANCE[topic],
        "independent_service_notice": (
            "RateCheck is an independent screening service. "
            "Follow current official guidance for applicable deadlines and requirements."
        ),
    }


if __name__ == "__main__":
    # Local stdio only: this pilot intentionally does not bind a network socket.
    mcp.run(transport="stdio")
