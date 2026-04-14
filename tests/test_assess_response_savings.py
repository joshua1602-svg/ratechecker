from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.models import AssessRequest, BusinessType, ContactInput, FlagsInput, PropertyInput
from api.routes.assess import router
from api.routes.assess import run_assessment_pipeline
from api.models import AssessResponse
from api.services.savings import build_downside_rv_range, calculate_implied_savings


def _request() -> AssessRequest:
    return AssessRequest(
        contact=ContactInput(email="test@example.com", business_name="Demo Shop"),
        property=PropertyInput(
            address="1 High Street",
            postcode="SW1A 1AA",
            business_type=BusinessType.retail,
            voa_rv=20_000,
            nia_sqm=100,
        ),
        flags=FlagsInput(consent_disclaimer=True),
    )


def _stub_dependencies(monkeypatch):
    async def _coords(_postcode):
        return (51.5, -0.12)

    monkeypatch.setattr("api.routes.assess.postcode_to_coords", _coords)
    monkeypatch.setattr("api.routes.assess.csa_rules", lambda: {"scat_codes": {"retail_shop": 249, "showroom": 269, "cafe": 409, "hair_beauty": 226, "nursery": 210, "pub": 284, "pub_with_lodge": 281}, "filters": {"distance_m": {"fallback": 5000}, "size_band_pct_fallback": 50}})
    monkeypatch.setattr("api.routes.assess.get_subject_voa_candidates_by_address_postcode", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("api.routes.assess.get_comparables", lambda **_kwargs: [{"uarn": "u1", "address": "2 High Street", "scat_code": 249, "rv": 18000, "nia_sqm": 90, "lat": 51.5005, "lon": -0.1205, "description": "Retail", "has_summary": True, "unit_of_measurement": "NIA"}])
    monkeypatch.setattr("api.routes.assess.exclude_subject_from_comparables", lambda rows, **_kwargs: (rows, []))
    monkeypatch.setattr("api.routes.assess.get_sv_lines_batch", lambda _uarns: {})
    monkeypatch.setattr("api.routes.assess.resolve_subject_voa_record", lambda _req: (None, None))
    monkeypatch.setattr(
        "api.routes.assess.run_csa",
        lambda **_kwargs: {
            "signal": "Medium",
            "explanation": "Demo",
            "comparable_count": 1,
            "saving_estimate": "Potential",
            "tone_rate": 200.0,
            "estimated_rv": 19_000,
            "_rated_comps": [{"uarn": "u1", "rate": 200.0, "address": "2 High Street"}],
            "_primary_tone_comps": [{"uarn": "u1"}],
            "tone_source": "primary",
            "tone_source_label": "Primary cluster",
        },
    )
    monkeypatch.setattr("api.routes.assess.get_sv_car_parking_batch", lambda _uarns: {})
    monkeypatch.setattr("api.routes.assess.get_sv_additions_batch", lambda _uarns: {})
    monkeypatch.setattr("api.routes.assess.get_sv_plant_machinery_batch", lambda _uarns: {})
    monkeypatch.setattr("api.routes.assess.get_sv_adjustment_totals_batch", lambda _uarns: {})
    monkeypatch.setattr("api.routes.assess.apply_fit_layer", lambda **kwargs: {"comps": kwargs["rated_comps"]})
    monkeypatch.setattr(
        "api.routes.assess.apply_adjustments",
        lambda **_kwargs: {
            "adjusted_estimated_rv": 19_000,
            "adjustment_summary": "No adjustments",
            "adjustments": {"applied": [], "total_adjustment_factor": 1.0},
        },
    )
    monkeypatch.setattr("api.routes.assess.get_location_signals", lambda **_kwargs: {"available": True})


def test_assess_response_exposes_top_level_implied_savings_fields(monkeypatch):
    _stub_dependencies(monkeypatch)

    req = _request()
    response = asyncio.run(run_assessment_pipeline(req))

    rv_low, rv_high = build_downside_rv_range(19_000)
    expected = calculate_implied_savings(
        current_rv=req.property.voa_rv,
        modelled_rv_point=19_000,
        modelled_rv_low=rv_low,
        modelled_rv_high=rv_high,
    )

    assert response.implied_total_saving_point == expected["implied_total_saving_point"]
    assert response.implied_total_saving_low == expected["implied_total_saving_low"]
    assert response.implied_total_saving_high == expected["implied_total_saving_high"]
    assert response.years_remaining_in_cycle == expected["years_remaining_in_cycle"]
    assert response.indicative_total_saving_low == response.implied_total_saving_low
    assert response.indicative_total_saving_high == response.implied_total_saving_high


def test_assess_response_savings_use_shared_cash_savings_logic_not_raw_rv_delta(monkeypatch):
    _stub_dependencies(monkeypatch)

    response = asyncio.run(run_assessment_pipeline(_request()))

    raw_rv_delta = 20_000 - 19_000
    assert response.implied_annual_saving_point is not None
    assert response.implied_annual_saving_point != raw_rv_delta


def test_assess_endpoint_serializes_savings_fields_at_top_level(monkeypatch):
    app = FastAPI()
    app.include_router(router)

    async def _captcha_ok(_token):
        return True

    async def _stub_pipeline(_req):
        return AssessResponse(
            signal="Medium",
            explanation="Demo",
            implied_total_saving_point=1470,
            implied_total_saving_low=1470,
            implied_total_saving_high=2940,
            years_remaining_in_cycle=3,
        )

    monkeypatch.setattr("api.routes.assess.verify_turnstile", _captcha_ok)
    monkeypatch.setattr("api.routes.assess.run_assessment_pipeline", _stub_pipeline)

    client = TestClient(app)
    payload = {
        "contact": {"email": "test@example.com", "business_name": "Demo Shop"},
        "property": {
            "address": "1 High Street",
            "postcode": "SW1A 1AA",
            "business_type": "retail",
            "voa_rv": 20000,
            "nia_sqm": 100,
        },
        "flags": {"consent_disclaimer": True},
        "captcha_token": "token",
    }
    resp = client.post("/assess", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["implied_total_saving_point"] == 1470
    assert body["implied_total_saving_low"] == 1470
    assert body["implied_total_saving_high"] == 2940
    assert body["years_remaining_in_cycle"] == 3
