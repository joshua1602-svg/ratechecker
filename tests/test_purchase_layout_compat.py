from __future__ import annotations

from fastapi.testclient import TestClient

from api.layout_compat import normalize_paid_intake_layout
from api.models import PaidIntakeData
from api.main import app


def _base_purchase_payload() -> dict:
    return {
        "product": "report",
        "assess_request": {
            "contact": {"email": "user@example.com", "business_name": "Demo"},
            "property": {
                "address": "1 High Street",
                "postcode": "SW1A 1AA",
                "business_type": "restaurant_cafe",
                "voa_rv": 20000,
                "nia_sqm": 160,
            },
            "flags": {"consent_disclaimer": True},
        },
        "assess_response": {"signal": "High", "explanation": "ok", "rated_comps": []},
        "paid_intake": {},
    }


def test_normalize_paid_intake_layout_maps_canonical_to_legacy_fields() -> None:
    payload = {
        "layout": {
            "floor_config": "ground_lower_ground_first",
            "floors": [
                {"level": "ground", "uses": {"trading_sqm": 60, "storage_sqm": 20, "kitchen_sqm": 10, "other_sqm": 5}},
                {"level": "lower_ground", "uses": {"trading_sqm": 15, "storage_sqm": 30, "kitchen_sqm": 5, "other_sqm": 0}},
                {"level": "first", "uses": {"trading_sqm": 10, "storage_sqm": 20, "kitchen_sqm": 0, "other_sqm": 5}},
            ],
            "total_entered_sqm": 170,
        }
    }

    out = normalize_paid_intake_layout(payload)

    assert out["areas"]["sales_area_sqm"] == 85
    assert out["areas"]["storage_sqm"] == 70
    assert out["areas"]["visible_kitchen_sqm"] == 15
    assert out["areas"]["basement_sqm"] == 50
    assert out["areas"]["upper_sqm"] == 35
    assert out["layout"]["ground_floor_trading_sqm"] == 60
    assert out["layout"]["ground_floor_storage_sqm"] == 20
    assert out["layout"]["lower_ground_use"] == "storage"
    assert out["layout"]["upper_floor_use"] == "storage"
    assert out["layout"]["kitchen_on_ground"] == "yes"


def test_purchase_accepts_legacy_paid_intake_shape(monkeypatch) -> None:
    payload = _base_purchase_payload()
    payload["paid_intake"] = {
        "layout": {
            "floor_config": "ground_only",
            "ground_floor_trading_sqm": 90,
            "ground_floor_storage_sqm": 10,
            "kitchen_on_ground": "no_kitchen",
        },
        "areas": {"sales_area_sqm": 90, "storage_sqm": 10},
    }

    captured = {}

    monkeypatch.setattr("api.routes.purchase.stripe.api_key", "sk_test")
    monkeypatch.setitem(__import__("api.routes.purchase", fromlist=["_PRICES"])._PRICES, "report", "price_report")

    class _Checkout:
        url = "https://checkout.stripe.test/session"

    def _fake_create_draft(*, product, assess_request, assess_response, paid_intake):
        captured["paid_intake"] = paid_intake
        return "sid_legacy"

    monkeypatch.setattr("api.routes.purchase.create_draft", _fake_create_draft)
    monkeypatch.setattr("api.routes.purchase.stripe.checkout.Session.create", lambda **_: _Checkout())

    client = TestClient(app)
    resp = client.post("/purchase", json=payload)
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "sid_legacy"
    assert captured["paid_intake"]["areas"]["sales_area_sqm"] == 90


def test_purchase_accepts_canonical_floors_and_derives_legacy(monkeypatch) -> None:
    payload = _base_purchase_payload()
    payload["paid_intake"] = {
        "layout": {
            "floor_config": "ground_lower_ground_first",
            "floors": [
                {"level": "ground", "uses": {"trading_sqm": 30, "storage_sqm": 10, "kitchen_sqm": 5, "other_sqm": 0}},
                {"level": "lower_ground", "uses": {"trading_sqm": 0, "storage_sqm": 20, "kitchen_sqm": 0, "other_sqm": 5}},
                {"level": "first", "uses": {"trading_sqm": 10, "storage_sqm": 15, "kitchen_sqm": 0, "other_sqm": 0}},
            ],
        }
    }

    captured = {}

    monkeypatch.setattr("api.routes.purchase.stripe.api_key", "sk_test")
    monkeypatch.setitem(__import__("api.routes.purchase", fromlist=["_PRICES"])._PRICES, "report", "price_report")

    class _Checkout:
        url = "https://checkout.stripe.test/session"

    def _fake_create_draft(*, product, assess_request, assess_response, paid_intake):
        captured["paid_intake"] = paid_intake
        return "sid_canonical"

    monkeypatch.setattr("api.routes.purchase.create_draft", _fake_create_draft)
    monkeypatch.setattr("api.routes.purchase.stripe.checkout.Session.create", lambda **_: _Checkout())

    client = TestClient(app)
    resp = client.post("/purchase", json=payload)
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "sid_canonical"
    assert captured["paid_intake"]["layout"]["floors"][0]["level"] == "ground"
    assert captured["paid_intake"]["areas"]["sales_area_sqm"] == 40
    assert captured["paid_intake"]["areas"]["storage_sqm"] == 45
    assert captured["paid_intake"]["areas"]["visible_kitchen_sqm"] == 5
    assert captured["paid_intake"]["areas"]["basement_sqm"] == 25
    assert captured["paid_intake"]["areas"]["upper_sqm"] == 25


def test_non_ground_breakdown_alias_fields_map_to_internal_uses_and_ancillary() -> None:
    paid_intake = PaidIntakeData.model_validate(
        {
            "property": {"business_type": "restaurant_cafe"},
            "layout": {
                "floors": [
                    {
                        "level": "lower_ground",
                        "uses": {
                            "tradingArea": 12,
                            "storageArea": 8,
                            "kitchenPrepArea": 6,
                            "otherArea": 4,
                        },
                    }
                ]
            }
        }
    ).model_dump(exclude_none=True)

    out = normalize_paid_intake_layout(paid_intake)

    floor_uses = out["layout"]["floors"][0]["uses"]
    assert floor_uses["trading_sqm"] == 12
    assert floor_uses["storage_sqm"] == 8
    assert floor_uses["kitchen_sqm"] == 6
    assert floor_uses["other_sqm"] == 4
    assert out["areas"]["sales_area_sqm"] == 12
    assert out["areas"]["storage_sqm"] == 8
    assert out["areas"]["visible_kitchen_sqm"] == 6
    assert out["areas"]["ancillary_area_sqm"] == 4
    assert out["areas"]["non_ground_ancillary_area_sqm"] == 4
    assert out["areas"]["non_ground_ancillary_sqm"] == 4
    # Ancillary ("other") is counted in non-ground total area only.
    assert out["areas"]["basement_sqm"] == 30


def test_retail_without_kitchen_field_treats_other_as_ancillary_only() -> None:
    payload = {
        "property": {"business_type": "retail"},
        "layout": {
            "floors": [
                {
                    "level": "lower_ground",
                    "uses": {"trading_sqm": 0, "storage_sqm": 10, "other_sqm": 20},
                },
            ],
        },
    }
    out = normalize_paid_intake_layout(payload)

    # Kitchen is absent and remains zero; "other" is wholly ancillary.
    assert out["areas"]["visible_kitchen_sqm"] == 0
    assert out["areas"]["ancillary_area_sqm"] == 20
    assert out["areas"]["non_ground_ancillary_area_sqm"] == 20
    assert out["areas"]["sales_area_sqm"] == 0
    assert out["areas"]["storage_sqm"] == 10
    assert out["areas"]["basement_sqm"] == 30


def test_restaurant_with_explicit_kitchen_keeps_other_ancillary_only() -> None:
    payload = {
        "property": {"business_type": "restaurant_cafe"},
        "layout": {
            "floors": [
                {
                    "level": "lower_ground",
                    "uses": {"trading_sqm": 0, "storage_sqm": 10, "kitchen_sqm": 12, "other_sqm": 8},
                },
            ],
        },
    }
    out = normalize_paid_intake_layout(payload)

    assert out["areas"]["visible_kitchen_sqm"] == 12
    assert out["areas"]["ancillary_area_sqm"] == 8
    assert out["areas"]["non_ground_ancillary_area_sqm"] == 8
    assert out["areas"]["storage_sqm"] == 10
    assert out["areas"]["basement_sqm"] == 30


def test_legacy_floor_breakdown_without_new_fields_preserves_behavior() -> None:
    payload = {
        "layout": {
            "floors": [
                {"level": "lower_ground", "uses": {"trading_sqm": 12, "storage_sqm": 8, "kitchen_sqm": 6}},
            ],
        }
    }
    out = normalize_paid_intake_layout(payload)

    assert out["areas"]["sales_area_sqm"] == 12
    assert out["areas"]["storage_sqm"] == 8
    assert out["areas"]["visible_kitchen_sqm"] == 6
    assert out["areas"]["basement_sqm"] == 26
    assert out["layout"]["lower_ground_use"] == "trading"


def test_purchase_rejects_negative_floor_sqm(monkeypatch) -> None:
    payload = _base_purchase_payload()
    payload["paid_intake"] = {
        "layout": {
            "floors": [
                {"level": "ground", "uses": {"trading_sqm": -1, "storage_sqm": 0, "kitchen_sqm": 0, "other_sqm": 0}}
            ]
        }
    }

    monkeypatch.setattr("api.routes.purchase.stripe.api_key", "sk_test")
    monkeypatch.setitem(__import__("api.routes.purchase", fromlist=["_PRICES"])._PRICES, "report", "price_report")

    client = TestClient(app)
    resp = client.post("/purchase", json=payload)
    assert resp.status_code == 422


def test_purchase_rejects_invalid_floor_level(monkeypatch) -> None:
    payload = _base_purchase_payload()
    payload["paid_intake"] = {
        "layout": {
            "floors": [
                {"level": "roof", "uses": {"trading_sqm": 1, "storage_sqm": 0, "kitchen_sqm": 0, "other_sqm": 0}}
            ]
        }
    }

    monkeypatch.setattr("api.routes.purchase.stripe.api_key", "sk_test")
    monkeypatch.setitem(__import__("api.routes.purchase", fromlist=["_PRICES"])._PRICES, "report", "price_report")

    client = TestClient(app)
    resp = client.post("/purchase", json=payload)
    assert resp.status_code == 422
