"""PDF report generation using Jinja2 + WeasyPrint."""
from __future__ import annotations

import os
import re
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


def _format_floor_config(raw_value: Any) -> str:
    value = str(raw_value or "").strip()
    mapping = {
        "ground_only": "Ground floor only",
        "ground_lower_ground": "Ground floor plus basement/lower ground",
        "ground_first": "Ground and upper floor",
        "ground_lower_ground_first": "Ground, basement/lower ground, and upper floor",
        "other": "Mixed/other floor arrangement",
    }
    return mapping.get(value, value.replace("_", " ").title()) if value else "Not provided"


def _normalise_comparable(comp: dict[str, Any]) -> dict[str, Any]:
    """Populate optional template fields so report rendering is resilient.

    rate_psm precedence (to avoid basis mismatch):
      1. Use pre-set rate_psm if already provided (from canonical builder).
      2. Use the engine's "rate" field (which is on the correct basis —
         ITZA for retail/restaurant, NIA for nursery).
      3. Only as a last resort, fall back to rv/nia_sqm (NIA basis).
         This case should not occur when using the canonical builder.
    """
    normalised = dict(comp)

    if normalised.get("rate_psm") is None:
        # Prefer engine "rate" field (correctly normalised by CSA)
        engine_rate = normalised.get("rate")
        if engine_rate is not None and float(engine_rate) > 0:
            normalised["rate_psm"] = round(float(engine_rate), 2)
        else:
            rv = normalised.get("rv")
            nia_sqm = normalised.get("nia_sqm")
            if rv is not None and nia_sqm and float(nia_sqm) > 0:
                normalised["rate_psm"] = round(float(rv) / float(nia_sqm), 2)

    similarity = normalised.get("layout_similarity_score")
    normalised["layout_similarity_score"] = float(similarity or 0)

    # weight_pct is intentionally left unset here; pool-level normalisation
    # in _derive_fields() computes the correct share-of-pool percentage.
    normalised.setdefault("weight_pct", "")

    normalised.setdefault("floor_config", "")
    normalised.setdefault("uarn", "")
    return normalised


def _build_weighting_rows(data: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    layout_used = bool(data.get("layout_adjustment_applied"))

    floor_config = _format_floor_config(data.get("floor_config"))
    rows.append({
        "factor": "Floor configuration",
        "subject_profile": floor_config,
        "effect": (
            "Compared against properties with a similar floor split when assigning comparative weight."
            if layout_used else
            "Comparable weighting based on live /assess layout matching was not applied in this report payload."
        ),
    })

    floor_value = str(data.get("floor_config") or "")
    if floor_value in {"ground_lower_ground", "ground_lower_ground_first"}:
        basement_profile = "Basement/lower ground space present"
    else:
        basement_profile = "No basement/lower ground detail provided"
    rows.append({
        "factor": "Basement presence",
        "subject_profile": basement_profile,
        "effect": "Used only to prioritise comparables with a similar vertical layout; not a direct deduction or allowance.",
    })

    kitchen_pos = data.get("kitchen_on_ground")
    kitchen_profile = {
        "yes": "Kitchen on the ground floor",
        "no": "Kitchen away from the ground floor",
        "no_kitchen": "No kitchen declared",
    }.get(kitchen_pos, "Kitchen position not provided")
    rows.append({
        "factor": "Kitchen position",
        "subject_profile": kitchen_profile,
        "effect": "Restaurant/cafe layout matching only — used to prioritise structurally similar comparables, not to change the subject RV directly.",
    })

    trading = float(data.get("ground_floor_trading_sqm") or 0)
    storage = float(data.get("ground_floor_storage_sqm") or 0)
    total = trading + storage
    if total > 0:
        storage_pct = round(storage / total * 100)
        balance = f"Approx. {storage_pct}% storage / {100 - storage_pct}% trading on the declared ground-floor split"
    else:
        balance = "Ground-floor storage/trading split not provided"
    rows.append({
        "factor": "Storage balance",
        "subject_profile": balance,
        "effect": "Used to compare storage-to-trading balance across the comparable set; not treated as an explicit subject-level adjustment.",
    })

    comps = data.get("comparables") or []
    if layout_used and comps:
        scores = [float(c.get("layout_similarity_score") or 0) for c in comps if isinstance(c, dict)]
        if scores:
            avg_score = sum(scores) / len(scores)
            if avg_score >= 0.7:
                alignment = "High average structural similarity across the weighted comparable set"
            elif avg_score >= 0.4:
                alignment = "Moderate average structural similarity across the weighted comparable set"
            else:
                alignment = "Limited structural similarity across the weighted comparable set"
        else:
            alignment = "Layout weighting indicated, but similarity detail was not supplied"
    elif layout_used:
        alignment = "Layout weighting indicated, but comparable similarity detail was not supplied"
    else:
        alignment = "Comparable set weighted without live layout-overweight detail"
    rows.append({
        "factor": "Overall layout alignment",
        "subject_profile": alignment,
        "effect": "Higher-alignment comparables carry more weight when deriving the market tone. Layout overweighting adjusts comparable weights only — it does not change the modelled RV directly and is not a subject-level deduction or allowance.",
    })

    return rows


def _to_title_case(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).title()


def _derive_fields(report_data: dict) -> dict:
    """Compute derived fields and return an augmented copy."""
    data = dict(report_data)
    data.setdefault("business_type_title", _to_title_case(data.get("business_type")))

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

    # Per-comparable normalisation (rate_psm, layout_similarity_score defaults)
    comps = data.get("comparables")
    if comps:
        data["comparables"] = [
            _normalise_comparable(comp) if isinstance(comp, dict) else comp
            for comp in comps
        ]

    # Pool-level weight normalisation: convert raw weight floats to share-of-pool %.
    # Handles both 'adjusted_weight' (layout path) and 'weight' (CSA-only path).
    _comps_list = data.get("comparables") or []
    if _comps_list:
        _weight_sum = sum(
            float(c.get("adjusted_weight") or c.get("weight") or 0)
            for c in _comps_list if isinstance(c, dict)
        )
        if _weight_sum > 0:
            for _c in _comps_list:
                if isinstance(_c, dict):
                    _raw = _c.get("adjusted_weight") or _c.get("weight")
                    if _raw is not None:
                        _c["weight_pct"] = round(float(_raw) / _weight_sum * 100, 1)

    data.setdefault("weighting_rows", _build_weighting_rows(data))

    # Submission narrative (evidence pack)
    if data.get("modelled_rv") is not None:
        business_name = data.get("business_name", "")
        property_address = data.get("property_address", "")
        postcode = data.get("postcode", "")
        business_type = data.get("business_type", "")
        layout_sentence = (
            " Layout details were used to prioritise and weight more structurally similar comparable properties."
            if data.get("layout_adjustment_applied")
            else ""
        )
        data.setdefault(
            "submission_narrative",
            (
                f"This evidence pack has been prepared for {business_name} "
                f"at {property_address}, {postcode}. "
                f"The property is classified as {business_type} "
                f"with a current VOA rateable value of £{voa_rv:,.0f}. "
                f"Based on analysis of {data.get('comp_count', 0)} comparable "
                f"properties, the estimated fair rateable value inferred from the weighted comparable set is £{modelled_rv:,.0f}, "
                f"representing a difference of £{data.get('rv_delta', 0):,.0f} "
                f"({data.get('rv_delta_pct', 0)}%)."
                f"{layout_sentence} These property-specific nuances influenced comparable weighting only; they did not create separate subject-level deductions or allowances in this report."
            ),
        )

    return data


def _get_env() -> Environment:
    """Build a Jinja2 environment pointing at the configured template dir."""
    template_path = _resolve_template_dir()
    env = Environment(
        loader=FileSystemLoader(str(template_path)),
        autoescape=True,
    )
    env.filters["format_currency"] = lambda v: f"{int(float(str(v).replace(',', ''))):,}"
    return env


def _load_template(env: Environment, template_name: str):
    """Load a template and raise a clearer error if it is missing."""
    try:
        return env.get_template(template_name)
    except TemplateNotFound as exc:
        search_paths = getattr(env.loader, "searchpath", [])
        raise FileNotFoundError(
            f"Template '{template_name}' was not found. Jinja search paths: {search_paths}"
        ) from exc



def generate_report_pdf(
    template_name: str,
    report_data: dict,
    required_fields: list[str],
    conditional_validator=None,
) -> bytes:
    """Render a PDF using the shared report pipeline and return PDF bytes."""
    _validate(report_data, required_fields)
    if conditional_validator is not None:
        conditional_validator(report_data)
    data = _derive_fields(report_data)

    env = _get_env()
    template = _load_template(env, template_name)
    rendered_html = template.render(**data)

    pdf_bytes: bytes = HTML(string=rendered_html).write_pdf()
    return pdf_bytes

def generate_simplified_report(report_data: dict) -> bytes:
    """Render the simplified report and return PDF bytes."""
    return generate_report_pdf("simplified_report.html", report_data, _SIMPLIFIED_REQUIRED)


def generate_evidence_pack(report_data: dict) -> bytes:
    """Render the full evidence pack and return PDF bytes."""
    return generate_report_pdf(
        "evidence_pack.html",
        report_data,
        _SIMPLIFIED_REQUIRED + _EVIDENCE_EXTRA_REQUIRED,
    )
