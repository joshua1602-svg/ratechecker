"""POST /assess — free quick-check endpoint."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.captcha import verify_turnstile
from api.db import get_comparables
from api.engine.csa import Comparable, run_csa
from api.engine.geocoding import postcode_to_coords
from api.engine.rules import csa_rules
from api.engine.valuation import apply_adjustments
from api.models import AdjustmentBreakdown, AdjustmentItem, AssessRequest, AssessResponse

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

    # 3. Query comparables from VOA database
    _radius_m = 3000 if btype == "restaurant_cafe" else rules["filters"]["distance_m"]["fallback"]
    if btype == "restaurant_cafe":
        _size_band_pct = 75
    elif btype in ("retail", "hair_beauty"):
        # ITZA normalization converts all shops to a Zone A equivalent rate, so
        # size differences are handled analytically — not by excluding comparables.
        # Use a very wide DB pre-filter and let the CSA do fine-grained selection.
        _size_band_pct = 500
    else:
        _size_band_pct = rules["filters"]["size_band_pct_fallback"]
    rows = get_comparables(
        lat=lat,
        lon=lon,
        scat_codes=target_scats,
        radius_m=_radius_m,
        nia_sqm=req.property.nia_sqm,
        size_band_pct=_size_band_pct,
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
    # subject_description and subject_sv_line_descs are not available from the
    # web form request — they would require a UARN lookup.  The defaults
    # ("" and ()) resolve to itza_retail, which is correct for standard
    # high-street retail and restaurant subjects.
    result = run_csa(
        comps=comps,
        lat=lat,
        lon=lon,
        business_type=btype,
        nia_sqm=req.property.nia_sqm,
        voa_rv=req.property.voa_rv,
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
