"""PDF report generation using Jinja2 + WeasyPrint."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

try:
    from weasyprint import HTML
except ImportError as e:
    raise ImportError(
        f"WeasyPrint failed to import. Ensure system "
        f"dependencies are installed. Original error: {e}"
    )

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_TEMPLATE_DIRS = [
    _REPO_ROOT / "src" / "templates" / "reports",
    _REPO_ROOT / "templates" / "reports",
]

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


def _resolve_template_dir() -> Path:
    """Resolve a valid report template dir from env or known repo locations."""
    configured_dir = os.getenv("REPORT_TEMPLATE_DIR")
    candidates: list[Path] = []

    if configured_dir:
        candidate = Path(configured_dir)
        if not candidate.is_absolute():
            candidate = (_REPO_ROOT / candidate).resolve()
        candidates.extend([candidate, candidate / "reports"])

    candidates.extend(_DEFAULT_TEMPLATE_DIRS)

    for candidate in candidates:
        if (candidate / "simplified_report.html").exists() and (candidate / "evidence_pack.html").exists():
            return candidate

    searched = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(
        "Could not locate report templates. Expected simplified_report.html and "
        f"evidence_pack.html in one of: {searched}"
    )


def _normalise_comparable(comp: dict[str, Any]) -> dict[str, Any]:
    """Populate optional template fields so report rendering is resilient."""
    normalised = dict(comp)

    rv = normalised.get("rv")
    nia_sqm = normalised.get("nia_sqm")
    if normalised.get("rate_psm") is None and rv is not None and nia_sqm:
        normalised["rate_psm"] = round(rv / nia_sqm, 2)

    similarity = normalised.get("layout_similarity_score")
    normalised["layout_similarity_score"] = float(similarity or 0)

    adjusted_weight = normalised.get("adjusted_weight")
    if normalised.get("weight_pct") is None:
        if adjusted_weight is not None:
            normalised["weight_pct"] = round(float(adjusted_weight) * 100, 1)
        else:
            normalised["weight_pct"] = ""

    normalised.setdefault("floor_config", "")
    normalised.setdefault("uarn", "")
    return normalised


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
        data["comparables"] = [
            _normalise_comparable(comp) if isinstance(comp, dict) else comp
            for comp in comps
        ]

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
    template_path = _resolve_template_dir()
    return Environment(
        loader=FileSystemLoader(str(template_path)),
        autoescape=True,
    )


def _load_template(env: Environment, template_name: str):
    """Load a template and raise a clearer error if it is missing."""
    try:
        return env.get_template(template_name)
    except TemplateNotFound as exc:
        search_paths = getattr(env.loader, "searchpath", [])
        raise FileNotFoundError(
            f"Template '{template_name}' was not found. Jinja search paths: {search_paths}"
        ) from exc


def generate_simplified_report(report_data: dict) -> bytes:
    """Render the simplified report and return PDF bytes."""
    _validate(report_data, _SIMPLIFIED_REQUIRED)
    data = _derive_fields(report_data)

    env = _get_env()
    template = _load_template(env, "simplified_report.html")
    rendered_html = template.render(**data)

    pdf_bytes: bytes = HTML(string=rendered_html).write_pdf()
    return pdf_bytes


def generate_evidence_pack(report_data: dict) -> bytes:
    """Render the full evidence pack and return PDF bytes."""
    _validate(report_data, _SIMPLIFIED_REQUIRED + _EVIDENCE_EXTRA_REQUIRED)
    _validate_evidence_conditionals(report_data)
    data = _derive_fields(report_data)

    env = _get_env()
    template = _load_template(env, "evidence_pack.html")
    rendered_html = template.render(**data)

    pdf_bytes: bytes = HTML(string=rendered_html).write_pdf()
    return pdf_bytes
