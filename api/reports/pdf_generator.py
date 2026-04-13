"""PDF report generation using Jinja2 + WeasyPrint."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, TemplateNotFound
from api.engine.csa import itza_from_nia
from api.reports.narrative import build_rendered_narrative

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
    "voa_description",
    "nia_sqm",
    "modelled_rv",
    "final_tone_psm",
    "tone_basis",
    "confidence",
    "recommendation_text",
    "valuation_method",
    "valuation_basis",
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


def _normalise_comparable(
    comp: dict[str, Any],
    *,
    business_type: str | None = None,
    valuation_method: str | None = None,
    sv_lines: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Populate optional template fields so report rendering is resilient.

    rate_psm precedence (to avoid basis mismatch):
      1. Use pre-set rate_psm if already provided (from canonical builder).
      2. Use the engine's "rate" field (which is on the correct basis —
         ITZA for retail ITZA path; NIA for NIA-valued paths).
      3. For retail/hair_beauty evidence reports on ITZA basis, fall back to
         rv/itza_from_nia(nia_sqm) so display aligns to CSA retail valuation basis.
      4. Only as a final fallback, use rv/nia_sqm (NIA basis).
         This case should not occur when using the canonical builder.
    """
    normalised = dict(comp)
    btype = str(business_type or "").strip().lower()
    method = str(valuation_method or "").strip().lower()
    is_retail_itza = btype in {"retail", "hair_beauty"} and method == "itza"
    rv = normalised.get("rv")
    nia_sqm = normalised.get("nia_sqm")

    if is_retail_itza and sv_lines and rv is not None and float(rv) > 0:
        try:
            _sv_itza = _retail_itza_from_sv_lines_for_display(sv_lines)
        except Exception:
            _sv_itza = 0.0
        if _sv_itza > 0:
            normalised["sv_itza_rate_psm"] = round(float(rv) / _sv_itza, 2)

    # Retail ITZA display keeps parity with the CSA tone basis:
    # prefer engine "rate" (the value used in tone derivation) for display.
    engine_rate = normalised.get("rate")
    if engine_rate is not None and float(engine_rate) > 0:
        normalised["rate_psm"] = round(float(engine_rate), 2)
        normalised["display_rate_basis"] = "CSA-derived"
    else:
        _has_existing_rate = normalised.get("rate_psm") is not None
        _existing_basis = str(normalised.get("display_rate_basis") or "").strip().lower()
        _existing_rate_basis = str(normalised.get("rate_basis") or "").strip().upper()
        _existing_marked_itza = (
            _existing_basis in {"csa-derived", "itza-fallback", "itza-provided", "sv-lines-itza"}
            or _existing_rate_basis == "ITZA"
        )

        # Retail ITZA reports must not display stale NIA-style precomputed rate_psm.
        # If no CSA rate is present, recompute from rv/itza when possible.
        if (
            is_retail_itza
            and (normalised.get("rate_psm") is None or not _existing_marked_itza)
            and rv is not None
            and nia_sqm
            and float(nia_sqm) > 0
        ):
            # Keep fallback aligned with CSA tone basis for itza_retail branch:
            # rv / itza_from_nia(nia_sqm).
            itza = itza_from_nia(float(nia_sqm))
            if itza > 0:
                normalised["rate_psm"] = round(float(rv) / itza, 2)
                normalised["display_rate_basis"] = "ITZA-fallback"
            elif not _has_existing_rate:
                normalised["rate_psm"] = round(float(rv) / float(nia_sqm), 2)
                normalised["display_rate_basis"] = "NIA-fallback"
        elif not _has_existing_rate:
            if rv is not None and nia_sqm and float(nia_sqm) > 0:
                normalised["rate_psm"] = round(float(rv) / float(nia_sqm), 2)
                normalised["display_rate_basis"] = "NIA-fallback"
        elif _existing_marked_itza:
            normalised.setdefault("display_rate_basis", "provided")
        else:
            # Non-ITZA paths keep provided values untouched for backward compatibility.
            normalised.setdefault("display_rate_basis", "provided")

    similarity = normalised.get("layout_similarity_score")
    normalised["layout_similarity_score"] = float(similarity or 0)

    # weight_pct is intentionally left unset here; pool-level normalisation
    # in _derive_fields() computes the correct share-of-pool percentage.
    normalised.setdefault("weight_pct", "")

    normalised.setdefault("floor_config", "")
    normalised.setdefault("uarn", "")
    return normalised


def _retail_itza_from_sv_lines_for_display(sv_lines: list[dict[str, Any]]) -> float:
    """Estimate ITZA from SV lines for retail comparable display-rate fallback.

    Description-led relativities are applied first so retail storage/kitchen rows
    map to standard low relativities instead of inheriting raw matrix price
    quirks. Price-ratio fallback is used only when description cannot be mapped.
    """
    if not sv_lines:
        return 0.0
    prices = [
        float(r["price"])
        for r in sv_lines
        if r.get("price") is not None and float(r["price"]) > 0
    ]
    zone_a_price = max(prices) if prices else None
    total = 0.0
    for line in sv_lines:
        area = float(line.get("area") or 0.0)
        if area <= 0:
            continue
        desc = str(line.get("description") or "").lower()
        rel: float | None = None
        if "zone a" in desc:
            rel = 1.0
        elif "zone b" in desc:
            rel = 0.5
        elif "zone c" in desc:
            rel = 0.25
        elif "remainder" in desc:
            rel = 0.125
        elif "storage" in desc or "internal store" in desc or "kitchen" in desc:
            rel = 0.10
        elif "basement" in desc or "lower ground" in desc:
            rel = 0.20
        if rel is None and zone_a_price and line.get("price") is not None and float(line["price"]) > 0:
            rel = float(line["price"]) / zone_a_price
        if rel is None:
            rel = 1.0
        total += area * rel
    return total


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




def _format_reconciliation_status(value: Any) -> str:
    mapping = {
        "yes": "Yes",
        "no": "No",
        "unknown": "Unknown",
        "partially": "Partial",
        "strong": "Strong",
        "broad": "Broad",
        "partial": "Partial",
        "unresolved": "Unavailable",
    }
    return mapping.get(str(value or "").lower(), "Unknown")


def _build_reconciliation_detail_rows(reconciliation: dict[str, Any] | None) -> list[dict[str, str]]:
    if not reconciliation:
        return []
    checks = reconciliation.get("checks") or {}
    rows: list[dict[str, str]] = []
    area = checks.get("total_area_alignment") or checks.get("gross_floor_space") or {}
    if area:
        rows.append({
            "label": "Entered vs VOA Area",
            "value": area.get("detail_text") or area.get("summary_text") or "Area comparison unavailable.",
        })

    layout = checks.get("layout_categorisation_alignment") or {}
    if layout and layout.get("status") == "no":
        rows.append({
            "label": "Layout / Categorisation Note",
            "value": layout.get("detail_text") or layout.get("summary_text") or "Internal categorisation differs.",
        })

    inconsistency_flag = bool(reconciliation.get("voa_structured_inconsistency_flag"))
    inconsistency_notes = reconciliation.get("voa_structured_inconsistency_notes") or []
    if inconsistency_flag and inconsistency_notes:
        rows.append({
            "label": "VOA Structured Record Note",
            "value": " ".join(str(n) for n in inconsistency_notes),
        })
    return rows

def _to_title_case(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).title()


_log = logging.getLogger(__name__)


def _resolve_subject_uarns(property_address: str | None, postcode: str | None) -> set[str]:
    """Look up the subject's VOA UARN(s) by address + postcode.

    Returns an empty set on any failure so report rendering is never blocked.
    """
    if not property_address or not postcode:
        return set()
    try:
        from api.db import get_subject_voa_candidates_by_address_postcode
        candidates = get_subject_voa_candidates_by_address_postcode(property_address, postcode)
        uarns = {str(c["uarn"]).strip() for c in candidates if c.get("uarn")}
        if uarns:
            _log.info("report subject UARN lookup: address=%r postcode=%r → %s", property_address, postcode, uarns)
        return uarns
    except Exception:
        _log.warning("report subject UARN lookup failed — skipping exclusion", exc_info=True)
        return set()


def _resolve_subject_cover_uarn(
    *,
    property_address: str | None,
    postcode: str | None,
    business_type: str | None,
    nia_sqm: float | None,
    floor_config: str | None,
    basement_sqm: float | None,
) -> str | None:
    """Resolve a canonical subject UARN for cover-page display.

    This follows the same disambiguation methodology as VOA subject matching.
    """
    if not property_address or not postcode:
        return None
    try:
        from api.db import get_subject_voa_candidates_by_address_postcode
        from api.models import _resolve_subject_candidate

        candidates = get_subject_voa_candidates_by_address_postcode(property_address, postcode)
        if not candidates:
            return None

        user_basement = bool((basement_sqm or 0) > 0)
        if floor_config in {"ground_lower_ground", "ground_lower_ground_first", "ground_and_basement", "ground_basement_first"}:
            user_basement = True
        user_ground = True if floor_config is not None else None

        resolved, signal = _resolve_subject_candidate(
            candidates=candidates,
            business_type=str(business_type or "").strip().lower(),
            user_ground=user_ground,
            user_basement=user_basement if user_ground is not None else None,
            user_total_area_sqm=nia_sqm,
        )
        uarn = str((resolved or {}).get("uarn") or "").strip()
        if uarn:
            _log.info(
                "report cover UARN lookup: address=%r postcode=%r candidates=%d signal=%s selected=%s",
                property_address, postcode, len(candidates), signal, uarn,
            )
            return uarn
    except Exception:
        _log.warning("report cover UARN lookup failed — leaving uprn unchanged", exc_info=True)
    return None


def _derive_fields(report_data: dict) -> dict:
    """Compute derived fields and return an augmented copy."""
    data = dict(report_data)
    data.setdefault("business_type_title", _to_title_case(data.get("business_type")))
    method_raw = str(data.get("valuation_method") or "").strip().lower()
    if method_raw in {"nia", "nia_only"}:
        data["valuation_method"] = "nia"
    elif method_raw in {"itza", "zoning"}:
        data["valuation_method"] = "itza"
    if data.get("valuation_method") == "nia":
        data.setdefault("valuation_basis", "Comparable Tone (£/sqm NIA)")
    elif data.get("valuation_method") == "itza":
        data.setdefault("valuation_basis", "ITZA (Zoning)")
        data.setdefault(
            "geometry_source_indicator",
            "Assumed 1:3 geometry fallback" if data.get("geometry_assumed") else "Provided frontage/depth geometry",
        )

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

    # ── Exclude the subject property from comparables by UARN ──
    # This is the authoritative filter: it catches ALL report paths (direct
    # POST from the frontend, paid download flow, etc.) regardless of whether
    # the /assess endpoint already stripped the subject.
    comps = data.get("comparables")
    if comps:
        subject_uarns = _resolve_subject_uarns(
            data.get("property_address"), data.get("postcode"),
        )
        if subject_uarns:
            before = len(comps)
            comps = [
                c for c in comps
                if not (isinstance(c, dict) and str(c.get("uarn", "")).strip() in subject_uarns)
            ]
            removed = before - len(comps)
            if removed:
                _log.info("report: excluded %d subject comp(s) by UARN %s", removed, subject_uarns)
                data["comparables"] = comps
                data["comp_count"] = len(comps)

    # Populate cover UPRN/UARN when frontend payload omitted it.
    if not str(data.get("uprn") or "").strip():
        resolved_cover_uarn = _resolve_subject_cover_uarn(
            property_address=data.get("property_address"),
            postcode=data.get("postcode"),
            business_type=data.get("business_type"),
            nia_sqm=data.get("nia_sqm"),
            floor_config=data.get("floor_config"),
            basement_sqm=data.get("basement_sqm"),
        )
        if resolved_cover_uarn:
            data["uprn"] = resolved_cover_uarn

    # Per-comparable normalisation (rate_psm, layout_similarity_score defaults)
    comps = data.get("comparables")
    _sv_lines_by_uarn: dict[str, list[dict[str, Any]]] = {}
    if comps:
        _btype = str(data.get("business_type") or "").strip().lower()
        _method = str(data.get("valuation_method") or "").strip().lower()
        if _btype in {"retail", "hair_beauty"} and _method == "itza":
            _uarns = [
                str(c.get("uarn")).strip()
                for c in comps
                if isinstance(c, dict) and c.get("uarn") is not None
            ]
            if _uarns:
                try:
                    from api.db import DatabaseError, get_sv_lines_batch
                    _sv_lines_by_uarn = get_sv_lines_batch(_uarns)
                except (DatabaseError, Exception):
                    _sv_lines_by_uarn = {}
        data["comparables"] = [
            (
                _normalise_comparable(
                    comp,
                    business_type=_btype,
                    valuation_method=_method,
                    sv_lines=(
                        _sv_lines_by_uarn.get(str(comp.get("uarn")).strip(), [])
                        if isinstance(comp, dict) and comp.get("uarn") is not None
                        else []
                    ),
                )
                if isinstance(comp, dict) else comp
            )
            for comp in comps
        ]

    _btype = str(data.get("business_type") or "").strip().lower()
    _method = str(data.get("valuation_method") or "").strip().lower()
    if _btype in {"retail", "hair_beauty"} and _method == "itza":
        data.setdefault("comparable_rate_header", "Rate £/sqm (ITZA)")
    else:
        data.setdefault("comparable_rate_header", "Rate £/sqm")

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

    # Sort comparables by descending weight for presentation clarity
    if _comps_list:
        def _weight_sort_value(comp: dict) -> float:
            raw = comp.get("weight_pct")
            if isinstance(raw, str):
                raw = raw.strip().rstrip("%")
            try:
                return float(raw or 0)
            except (TypeError, ValueError):
                return 0.0

        data["comparables"] = sorted(
            _comps_list,
            key=lambda c: _weight_sort_value(c) if isinstance(c, dict) else 0.0,
            reverse=True,
        )
        _comps_list = data["comparables"]

    data.setdefault("weighting_rows", _build_weighting_rows(data))

    reconciliation = data.get("voa_reconciliation") or {}
    data.setdefault("voa_record_match", _format_reconciliation_status(reconciliation.get("overall_status")))
    data.setdefault("voa_reconciliation_no_rows", _build_reconciliation_detail_rows(reconciliation))
    loc = data.get("location_signals") or {}
    crime = loc.get("crime") or {}
    flood = loc.get("flood") or {}
    crime_level = str(crime.get("signal_level") or "N/A").lower()
    flood_level = str(flood.get("signal_level") or "N/A").lower()
    data.setdefault("crime_adjustment_indicator", {"low": "Low", "moderate": "Moderate", "elevated": "Elevated"}.get(crime_level, "N/A"))
    data.setdefault("flood_adjustment_indicator", {"active": "Active", "none": "None"}.get(flood_level, "N/A"))

    # Re-derive tone_source_label from comparable addresses so it is always
    # consistent with the actual comp pool, regardless of how the payload was
    # constructed (paid download, direct POST, stale draft, etc.).
    _btype = str(data.get("business_type") or "").lower()
    if _btype in ("retail", "hair_beauty") and _comps_list:
        from api.engine.csa import _extract_street_key as _csa_street_key
        _subj_street = _csa_street_key(data.get("property_address") or "")
        if _subj_street:
            _ss_n = sum(
                1 for _c in _comps_list
                if isinstance(_c, dict) and _csa_street_key(_c.get("address") or "") == _subj_street
            )
            _total_n = len(_comps_list)
            _ss_sh = _ss_n / _total_n if _total_n > 0 else 0.0
            if _ss_n >= 6 or _ss_sh >= 0.50:
                data["tone_source_label"] = (
                    "Primary tone source: Same street evidence "
                    "(sufficiently strong same-street set)"
                )
            else:
                data.setdefault(
                    "tone_source_label",
                    "Primary tone source: Wider local comparable set",
                )

    if not data.get("evidence_interpretation") or not data.get("case_assessment") or not data.get("recommended_action"):
        data.update(build_rendered_narrative(data))

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
    env.tests["subject_comp"] = _is_subject_comp_jinja
    return env


def _normalise_addr_tokens(value: str | None) -> set[str]:
    """Reduce an address to a set of uppercase alphanumeric tokens."""
    text = str(value or "").upper()
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)
    return set(cleaned.split())


def _is_subject_comp_jinja(comp_address: str, property_address: str) -> bool:
    """Jinja2 test: True when a comparable's address appears to be the subject.

    Checks whether all significant tokens from the comparable's VOA
    ``full_property_identifier`` (e.g. "22" or "GND FLR, 22") appear
    inside the user-supplied property address (e.g. "22 High Street, London").
    """
    comp_tokens = _normalise_addr_tokens(comp_address)
    prop_tokens = _normalise_addr_tokens(property_address)
    # Ignore common VOA noise tokens that would cause false positives
    noise = {"GND", "FLR", "FLOOR", "1ST", "2ND", "3RD", "PT", "PART",
             "UNIT", "SHOP", "OFFICE", "SUITE", "REAR", "UPPER", "LOWER",
             "AND", "AT", "OF", "THE"}
    significant = comp_tokens - noise
    if not significant:
        return False
    return significant <= prop_tokens


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
