"""POST /assess — free quick-check endpoint."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from api.captcha import verify_turnstile
from api.db import DATABASE_URL, get_comparables, get_sv_lines_batch
from api.engine.csa import Comparable, run_csa
from api.engine.geocoding import postcode_to_coords
from api.engine.layout_overweight import LayoutInput, apply_layout_overweighting
from api.engine.rules import csa_rules
from api.engine.valuation import apply_adjustments
from api.models import AdjustmentBreakdown, AdjustmentItem, AssessRequest, AssessResponse

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
    log.warning("ASSESS_DEBUG postcode=%s geocoded_lat=%s geocoded_lon=%s", req.property.postcode, round(lat, 4), round(lon, 4))

    btype = req.property.business_type.value
    rules = csa_rules()
    target_scats = _scat_codes(btype, rules)

    # DEBUG — redact password from URL for safe logging
    _db_url_safe = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    _db_type = "sqlite" if "sqlite" in DATABASE_URL else "postgres/supabase"
    log.warning(
        "ASSESS_DEBUG db_type=%s db_host=%s business_type=%s postcode=%s nia_sqm=%s voa_rv=%s scat_codes=%s",
        _db_type, _db_url_safe, btype, req.property.postcode, req.property.nia_sqm,
        req.property.voa_rv, target_scats,
    )

    # 3. Query comparables from VOA database
    if btype == "restaurant_cafe":
        _radius_m = 3000
    elif btype == "nursery":
        _radius_m = 10_000
    else:
        _radius_m = rules["filters"]["distance_m"]["fallback"]
    if btype == "restaurant_cafe":
        _size_band_pct = 75
    elif btype in ("retail", "hair_beauty"):
        # ITZA normalization converts all shops to a Zone A equivalent rate, so
        # size differences are handled analytically — not by excluding comparables.
        # Use a very wide DB pre-filter and let the CSA do fine-grained selection.
        _size_band_pct = 500
    else:
        _size_band_pct = rules["filters"]["size_band_pct_fallback"]
    log.warning(
        "ASSESS_DEBUG radius_m=%s size_band_pct=%s lat=%s lon=%s",
        _radius_m, _size_band_pct, round(lat, 4), round(lon, 4),
    )
    rows = get_comparables(
        lat=lat,
        lon=lon,
        scat_codes=target_scats,
        radius_m=_radius_m,
        nia_sqm=req.property.nia_sqm,
        size_band_pct=_size_band_pct,
    )
    log.warning("ASSESS_DEBUG rows_from_db=%s", len(rows))

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
    # subject_description and subject_sv_line_descs are not available from the
    # web form request — they would require a UARN lookup.  The defaults
    # ("" and ()) resolve to itza_retail, which is correct for standard
    # high-street retail and restaurant subjects.
    log.warning("ASSESS_DEBUG comps_passed_to_csa=%s", len(comps))
    result = run_csa(
        comps=comps,
        lat=lat,
        lon=lon,
        business_type=btype,
        nia_sqm=req.property.nia_sqm,
        voa_rv=req.property.voa_rv,
    )
    log.warning(
        "ASSESS_DEBUG csa_signal=%s comparable_count=%s insufficiency_reason=%s restaurant_rejection=%s",
        result.get("signal"), result.get("comparable_count"),
        result.get("insufficiency_reason"), result.get("restaurant_rejection_reason"),
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
        sv_lines = get_sv_lines_batch(comp_uarns) if comp_uarns else {}
        layout_result = apply_layout_overweighting(
            csa_result=result,
            layout_input=layout_in,
            sv_lines_by_uarn=sv_lines,
            business_type=btype,
        )
        log.warning(
            "ASSESS_DEBUG layout_adjustment_applied=%s",
            layout_result.get("layout_adjustment_applied"),
        )

    # 6. Apply adjustment layer
    # Runs only when the CSA produced a valid estimate (base_rv is not None).
    # Missing optional fields (areas, nursery) are handled inside apply_adjustments
    # by substituting safe empty defaults, so absent triggers simply don't fire.
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
    )
