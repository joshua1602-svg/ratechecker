"""POST /assess — free quick-check endpoint."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from api.captcha import verify_turnstile
from api.db import (
    DATABASE_URL,
    DatabaseError,
    exclude_subject_from_comparables,
    get_comparables,
    get_subject_voa_candidates_by_address_postcode,
    get_sv_lines_batch,
    get_sv_car_parking_batch,
    get_sv_additions_batch,
    get_sv_plant_machinery_batch,
    get_sv_adjustment_totals_batch,
)
from api.engine.csa import Comparable, run_csa
from api.engine.geocoding import postcode_to_coords
from api.engine.fit_layer import apply_fit_layer
from api.engine.layout_overweight import LayoutInput, apply_layout_overweighting
from api.engine.rules import csa_rules
from api.engine.valuation import apply_adjustments, itza_from_voa_sv_lines
from api.location_signals import get_location_signals
from api.models import (
    AdjustmentBreakdown,
    AdjustmentItem,
    AssessRequest,
    AssessResponse,
    resolve_subject_voa_record,
)

log = logging.getLogger(__name__)

router = APIRouter()

# Map front-end business_type values to VOA SCAT codes (from csa.yaml)
def _scat_codes(business_type: str, rules: dict) -> list[int]:
    sc = rules["scat_codes"]
    mapping = {
        "restaurant_cafe": [sc["cafe"], sc["restaurant"]],
        "retail": [sc["retail_shop"], sc["showroom"]],
        "hair_beauty": [sc["hair_beauty"]],
        "nursery": [sc["nursery"]],
        "pub": [sc["pub"], sc["pub_with_lodge"]],
    }
    return mapping.get(business_type, [])


@router.post("/assess", response_model=AssessResponse)
async def assess(req: AssessRequest) -> AssessResponse:
    # 1. Captcha verification
    if not await verify_turnstile(req.captcha_token):
        raise HTTPException(status_code=400, detail="Captcha verification failed")

    # 2. Geocode postcode
    coords = await postcode_to_coords(req.property.postcode)
    if coords is None:
        raise HTTPException(status_code=422, detail="Could not geocode postcode — check it is a valid UK postcode")
    lat, lon = coords

    btype = req.property.business_type.value
    rules = csa_rules()
    target_scats = _scat_codes(btype, rules)

    # 3. Resolve the subject's real VOA UARN(s) using postcode + address
    #    so we can exclude the customer's own property by exact identity.
    subject_uarns: set[str] = set()
    if req.property.uprn:
        subject_uarns.add(str(req.property.uprn).strip())
    if req.property.address and req.property.postcode:
        try:
            voa_candidates = get_subject_voa_candidates_by_address_postcode(
                req.property.address, req.property.postcode,
            )
            for cand in voa_candidates:
                if cand.get("uarn"):
                    subject_uarns.add(str(cand["uarn"]).strip())
            log.info(
                "subject VOA lookup address=%r postcode=%r candidates=%d uarns=%s",
                req.property.address, req.property.postcode,
                len(voa_candidates), subject_uarns,
            )
        except DatabaseError:
            log.warning("subject VOA lookup failed — falling back to UPRN only")

    # Pick the best single UARN for the SQL-level exclusion (first resolved VOA UARN)
    _primary_exclude_uarn = next(iter(subject_uarns), None)

    # 4. Query comparables from VOA database
    if btype == "restaurant_cafe":
        _radius_m = 3000
    elif btype == "nursery":
        _radius_m = 10_000
    else:
        _radius_m = rules["filters"]["distance_m"]["fallback"]
    if btype == "restaurant_cafe":
        _size_band_pct = 75
    elif btype in ("retail", "hair_beauty"):
        _size_band_pct = 500
    else:
        _size_band_pct = rules["filters"]["size_band_pct_fallback"]

    try:
        rows = get_comparables(
            lat=lat,
            lon=lon,
            scat_codes=target_scats,
            radius_m=_radius_m,
            nia_sqm=req.property.nia_sqm,
            size_band_pct=_size_band_pct,
            exclude_uarn=_primary_exclude_uarn,
        )
    except DatabaseError as exc:
        log.error("Database failure in /assess: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Database is temporarily unavailable. Please try again shortly.",
        ) from exc

    # 5. Post-filter: remove any remaining subject UARNs the SQL missed
    #    (e.g. when the VOA lookup returned multiple candidate UARNs)
    if subject_uarns:
        before_count = len(rows)
        rows = [r for r in rows if str(r.get("uarn", "")).strip() not in subject_uarns]
        excluded_count = before_count - len(rows)
        if excluded_count:
            log.info("post-filter excluded %d subject comp(s) by UARN set %s", excluded_count, subject_uarns)

    # 6. Structured fallback for edge cases the UARN lookup missed entirely
    subject_record = (
        {"uarn": _primary_exclude_uarn} if _primary_exclude_uarn
        else None
    )
    rows, excluded_subject_rows = exclude_subject_from_comparables(
        rows,
        subject_record=subject_record,
        subject_address=req.property.address,
        subject_postcode=req.property.postcode,
    )
    if excluded_subject_rows:
        log.info(
            "structured fallback excluded %d additional comp(s)",
            len(excluded_subject_rows),
        )

    # 4. Convert DB rows → Comparable objects
    comps = [
        Comparable(
            uarn=r["uarn"],
            address=r["address"] or "",
            scat_code=int(r["scat_code"]),
            rv=float(r["rv"]),
            nia_sqm=float(r["nia_sqm"]),
            unadjusted_price_psm=r.get("unadjusted_price_psm"),
            unit_of_measurement=r.get("unit_of_measurement") or "NIA",
            has_summary=bool(r.get("has_summary")),
            lat=float(r["lat"]),
            lon=float(r["lon"]),
            description=r.get("description") or "",
        )
        for r in rows
    ]

    # 5. Run CSA
    resolved_subject_record, _ = resolve_subject_voa_record(req)
    subject_itza_override = None
    if resolved_subject_record is not None:
        _sv_lines = resolved_subject_record.get("sv_lines") or []
        if _sv_lines:
            subject_itza_override = itza_from_voa_sv_lines(_sv_lines)

    result = run_csa(
        comps=comps,
        lat=lat,
        lon=lon,
        business_type=btype,
        nia_sqm=req.property.nia_sqm,
        voa_rv=req.property.voa_rv,
        subject_itza_sqm=subject_itza_override,
    )

    # 5b. Layout overweighting layer (runs after CSA, before adjustments)
    layout_result = None
    if req.layout is not None:
        layout_in = LayoutInput(
            floor_config=req.layout.floor_config,
            ground_floor_trading_sqm=req.layout.ground_floor_trading_sqm,
            ground_floor_storage_sqm=req.layout.ground_floor_storage_sqm,
            lower_ground_use=req.layout.lower_ground_use,
            upper_floor_use=req.layout.upper_floor_use,
            kitchen_on_ground=req.layout.kitchen_on_ground,
            total_nia_sqm=req.property.nia_sqm,
        )
        # Fetch SV lines for all comps in the rated set
        rated_comps = result.get("_rated_comps", [])
        comp_uarns = [str(c["uarn"]) for c in rated_comps]
        try:
            sv_lines = get_sv_lines_batch(comp_uarns) if comp_uarns else {}
        except DatabaseError:
            sv_lines = {}
        layout_result = apply_layout_overweighting(
            csa_result=result,
            layout_input=layout_in,
            sv_lines_by_uarn=sv_lines,
            business_type=btype,
        )

    # 5c. 03-07 fit classification and pool-control layer
    _base_comps = (
        layout_result["comps"] if layout_result is not None
        else result.get("_rated_comps", [])
    )
    _comps_for_response: list[dict] = []
    if _base_comps:
        _comp_uarns = [str(c["uarn"]) for c in _base_comps]
        # Fit-layer DB queries are non-critical — degrade gracefully to empty
        # data rather than failing the entire assessment.
        try:
            _parking = get_sv_car_parking_batch(_comp_uarns)
            _additions = get_sv_additions_batch(_comp_uarns)
            _pm = get_sv_plant_machinery_batch(_comp_uarns)
            _adj_totals = get_sv_adjustment_totals_batch(_comp_uarns)
        except DatabaseError:
            log.warning("Fit-layer DB queries failed — proceeding without fit classification")
            _parking, _additions, _pm, _adj_totals = {}, {}, {}, {}
        _fit_result = apply_fit_layer(
            rated_comps=_base_comps,
            parking_by_uarn=_parking,
            additions_by_uarn=_additions,
            pm_by_uarn=_pm,
            adj_totals_by_uarn=_adj_totals,
            subject_has_parking=None,
        )
        _comps_for_response = _fit_result["comps"]

    # 6. Apply adjustment layer
    base_rv: int | None = result.get("estimated_rv")
    adj_breakdown: AdjustmentBreakdown | None = None
    adj_rv: int | None = None
    adj_summary: str | None = None

    if base_rv is not None:
        adj = apply_adjustments(
            business_type=btype,
            base_rv=int(base_rv),
            property=req.property,
            areas=req.areas,
            nursery=req.nursery,
            flags=req.flags,
        )
        adj_rv = adj["adjusted_estimated_rv"]
        adj_summary = adj["adjustment_summary"]
        adj_breakdown = AdjustmentBreakdown(
            applied=[AdjustmentItem(**item) for item in adj["adjustments"]["applied"]],
            total_adjustment_factor=adj["adjustments"]["total_adjustment_factor"],
        )

    location_signals = get_location_signals(
        postcode=req.property.postcode,
        latitude=lat,
        longitude=lon,
    )

    return AssessResponse(
        signal=result["signal"],
        explanation=result["explanation"],
        comparable_count=result.get("comparable_count"),
        saving_estimate=result.get("saving_estimate"),
        tone_rate=result.get("tone_rate"),
        base_estimated_rv=base_rv,
        adjusted_estimated_rv=adj_rv,
        adjustments=adj_breakdown,
        adjustment_summary=adj_summary,
        rated_comps=_comps_for_response,
        location_signals=location_signals,
    )
