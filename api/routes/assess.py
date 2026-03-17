"""POST /assess — free quick-check endpoint."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.captcha import verify_turnstile
from api.db import get_comparables
from api.engine.csa import Comparable, run_csa
from api.engine.geocoding import postcode_to_coords
from api.engine.rules import csa_rules
from api.models import AssessRequest, AssessResponse

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
    rows = get_comparables(
        lat=lat,
        lon=lon,
        scat_codes=target_scats,
        radius_m=rules["filters"]["distance_m"]["fallback"],
        nia_sqm=req.property.nia_sqm,
        size_band_pct=rules["filters"]["size_band_pct_fallback"],
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

    return AssessResponse(
        signal=result["signal"],
        explanation=result["explanation"],
        comparable_count=result.get("comparable_count"),
        saving_estimate=result.get("saving_estimate"),
    )
