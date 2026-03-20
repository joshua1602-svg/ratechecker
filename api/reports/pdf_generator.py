"""PDF report generation using Jinja2 + WeasyPrint."""
from __future__ import annotations

import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

REPORT_TEMPLATE_DIR = os.getenv(
    "REPORT_TEMPLATE_DIR", "src/templates/reports"
)

_SIMPLIFIED_REQUIRED = [
    "business_name",
    "property_address",
    "postcode",
    "business_type",
    "date_prepared",
    "voa_rv",
    "modelled_rv_low",
    "modelled_rv_high",
    "annual_saving_low",
    "annual_saving_high",
    "case_strength",
    "comparables",
    "comp_count",
]

_EVIDENCE_EXTRA_REQUIRED = [
    "uprn",
    "voa_description",
    "nia_sqm",
    "modelled_rv",
    "final_tone_psm",
    "tone_basis",
    "confidence",
    "recommendation_text",
]


def _validate(report_data: dict, required_fields: list[str]) -> None:
    """Raise ValueError if any required field is missing or None."""
    for field in required_fields:
        if field not in report_data or report_data[field] is None:
            raise ValueError(f"Missing required field: {field}")


def _validate_evidence_conditionals(report_data: dict) -> None:
    """Evidence pack needs zoning_rows or nursery_adjustments depending on type."""
    bt = report_data.get("business_type", "")
    if bt == "nursery":
        if not report_data.get("nursery_adjustments"):
            raise ValueError(
                "Missing required field: nursery_adjustments "
                "(required for nursery business_type)"
            )
    else:
        if not report_data.get("zoning_rows"):
            raise ValueError(
                "Missing required field: zoning_rows "
                "(required for non-nursery business_type)"
            )


def _derive_fields(report_data: dict) -> dict:
    """Compute derived fields and return an augmented copy."""
    data = dict(report_data)

    voa_rv = data.get("voa_rv")
    nia_sqm = data.get("nia_sqm")

    # Property rate per sqm
    if voa_rv and nia_sqm:
        data.setdefault("rate_psm", round(voa_rv / nia_sqm, 2))

    # RV delta (evidence pack)
    modelled_rv = data.get("modelled_rv")
    if voa_rv and modelled_rv:
        rv_delta = voa_rv - modelled_rv
        data.setdefault("rv_delta", rv_delta)
        if voa_rv != 0:
            data.setdefault("rv_delta_pct", round((rv_delta / voa_rv) * 100, 1))

    # Per-comparable rate_psm
    comps = data.get("comparables")
    if comps:
        for comp in comps:
            if isinstance(comp, dict):
                c_rv = comp.get("rv")
                c_nia = comp.get("nia_sqm")
                if c_rv and c_nia:
                    comp.setdefault("rate_psm", round(c_rv / c_nia, 2))

    # Submission narrative (evidence pack)
    if data.get("modelled_rv") is not None:
        business_name = data.get("business_name", "")
        property_address = data.get("property_address", "")
        postcode = data.get("postcode", "")
        business_type = data.get("business_type", "")
        data.setdefault(
            "submission_narrative",
            (
                f"This evidence pack has been prepared for {business_name} "
                f"at {property_address}, {postcode}. "
                f"The property is classified as {business_type} "
                f"with a current VOA rateable value of £{voa_rv:,.0f}. "
                f"Based on analysis of {data.get('comp_count', 0)} comparable "
                f"properties, the modelled rateable value is £{modelled_rv:,.0f}, "
                f"representing a difference of £{data.get('rv_delta', 0):,.0f} "
                f"({data.get('rv_delta_pct', 0)}%)."
            ),
        )

    return data


def _get_env() -> Environment:
    """Build a Jinja2 environment pointing at the configured template dir."""
    template_path = Path(REPORT_TEMPLATE_DIR)
    return Environment(
        loader=FileSystemLoader(str(template_path)),
        autoescape=True,
    )


def generate_simplified_report(report_data: dict) -> bytes:
    """Render the simplified report and return PDF bytes."""
    _validate(report_data, _SIMPLIFIED_REQUIRED)
    data = _derive_fields(report_data)

    env = _get_env()
    template = env.get_template("simplified_report.html")
    rendered_html = template.render(**data)

    pdf_bytes: bytes = HTML(string=rendered_html).write_pdf()
    return pdf_bytes


def generate_evidence_pack(report_data: dict) -> bytes:
    """Render the full evidence pack and return PDF bytes."""
    _validate(report_data, _SIMPLIFIED_REQUIRED + _EVIDENCE_EXTRA_REQUIRED)
    _validate_evidence_conditionals(report_data)
    data = _derive_fields(report_data)

    env = _get_env()
    template = env.get_template("evidence_pack.html")
    rendered_html = template.render(**data)

    pdf_bytes: bytes = HTML(string=rendered_html).write_pdf()
    return pdf_bytes
