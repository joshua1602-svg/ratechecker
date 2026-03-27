from __future__ import annotations

from api.location_signals import get_crime_signal, get_flood_signal, get_location_signals
from api.models import (
    AssessRequest,
    AssessResponse,
    BusinessType,
    ContactInput,
    FlagsInput,
    PropertyInput,
    build_evidence_payload_from_assess,
)


class DummyCache:
    def __init__(self):
        self.data = {}

    def get(self, key: str):
        return self.data.get(key)

    def set(self, key: str, payload: dict, ttl_seconds: int):
        self.data[key] = payload


class DummyResponse:
    def __init__(self, payload: dict | list, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")


class DummyClient:
    def __init__(self, routes: dict[str, dict | list]):
        self.routes = routes

    def get(self, url: str, params: dict | None = None):
        if "crimes-street-dates" in url:
            return DummyResponse(self.routes["dates"])
        if "all-crime" in url:
            lat = round(float((params or {}).get("lat", 0)), 3)
            lon = round(float((params or {}).get("lng", 0)), 3)
            key = f"crime:{lat}:{lon}"
            if key not in self.routes:
                raise RuntimeError("missing benchmark point")
            return DummyResponse(self.routes[key])
        if "/floods" in url:
            return DummyResponse(self.routes["flood"])
        raise AssertionError(f"Unexpected url: {url}")


def _crime_rows(n: int, *, category: str = "shoplifting") -> list[dict]:
    return [{"category": category}] * n


def test_missing_coordinates_returns_unavailable_payload():
    payload = get_location_signals(postcode="SW1A 1AA", latitude=None, longitude=None)
    assert payload["available"] is False
    assert payload["crime"]["signal_level"] == "N/A"
    assert payload["flood"]["signal_level"] == "N/A"


def test_cache_hit_path_skips_fetches(monkeypatch):
    cache = DummyCache()
    cached = {"available": True, "crime": {"signal_level": "low"}, "flood": {"signal_level": "none"}}
    cache.data["location-signals:v1:51.500:-0.120"] = cached

    def _boom(*args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr("api.location_signals.get_crime_signal", _boom)
    monkeypatch.setattr("api.location_signals.get_flood_signal", _boom)

    payload = get_location_signals("SW1A 1AA", 51.5, -0.12, cache=cache)
    assert payload == cached


def test_crime_classification_normal_case_moderate():
    routes: dict[str, dict | list] = {
        "dates": [{"date": "2026-01"}],
        "crime:51.5:-0.12": _crime_rows(5),
        "crime:51.505:-0.12": _crime_rows(4),
        "crime:51.495:-0.12": _crime_rows(4),
        "crime:51.5:-0.113": _crime_rows(3),
        "crime:51.5:-0.127": _crime_rows(4),
        "crime:51.505:-0.113": _crime_rows(4),
        "crime:51.505:-0.127": _crime_rows(4),
        "crime:51.495:-0.113": _crime_rows(4),
        "crime:51.495:-0.127": _crime_rows(3),
    }
    signal = get_crime_signal(51.5, -0.12, client=DummyClient(routes))
    assert signal["status"] == "ok"
    assert signal["signal_level"] == "moderate"
    assert signal["retail_relevant_count"] == 5


def test_crime_very_low_benchmark_uses_floor_not_spurious_elevated():
    routes: dict[str, dict | list] = {
        "dates": [{"date": "2026-01"}],
        "crime:51.5:-0.12": _crime_rows(2),
        "crime:51.505:-0.12": _crime_rows(1),
        "crime:51.495:-0.12": _crime_rows(1),
        "crime:51.5:-0.113": _crime_rows(0),
        "crime:51.5:-0.127": _crime_rows(1),
        "crime:51.505:-0.113": _crime_rows(1),
        "crime:51.505:-0.127": _crime_rows(0),
        "crime:51.495:-0.113": _crime_rows(1),
        "crime:51.495:-0.127": _crime_rows(1),
    }
    signal = get_crime_signal(51.5, -0.12, client=DummyClient(routes))
    assert signal["status"] == "ok"
    assert signal["signal_level"] == "low"


def test_crime_thin_benchmark_returns_na():
    routes: dict[str, dict | list] = {
        "dates": [{"date": "2026-01"}],
        "crime:51.5:-0.12": _crime_rows(6),
        # only two benchmark points populated -> thin/unreliable
        "crime:51.505:-0.12": _crime_rows(2),
        "crime:51.495:-0.12": _crime_rows(2),
    }
    signal = get_crime_signal(51.5, -0.12, client=DummyClient(routes))
    assert signal["status"] == "unavailable"
    assert signal["signal_level"] == "N/A"


def test_flood_area_present_without_active_signal_is_none():
    routes: dict[str, dict | list] = {
        "flood": {
            "items": [
                {
                    "severityLevel": 4,
                    "description": "No longer in force",
                    "floodArea": {"description": "River Test"},
                }
            ]
        }
    }
    signal = get_flood_signal(51.5, -0.12, client=DummyClient(routes))
    assert signal["status"] == "ok"
    assert signal["in_flood_area"] is True
    assert signal["signal_level"] == "none"


def test_flood_active_signal_classification():
    routes: dict[str, dict | list] = {
        "flood": {
            "items": [
                {
                    "severityLevel": 2,
                    "description": "Flood warning",
                    "floodArea": {"description": "River Test"},
                }
            ]
        }
    }
    signal = get_flood_signal(51.5, -0.12, client=DummyClient(routes))
    assert signal["status"] == "ok"
    assert signal["signal_level"] == "active"
    assert signal["included_in_report"] is True


def test_upstream_error_handling_returns_na():
    class BadClient:
        def get(self, *args, **kwargs):
            raise RuntimeError("timeout")

    crime = get_crime_signal(51.5, -0.12, client=BadClient())
    flood = get_flood_signal(51.5, -0.12, client=BadClient())
    assert crime["signal_level"] == "N/A"
    assert flood["signal_level"] == "N/A"


def test_integration_payload_keeps_valuation_outputs_unchanged():
    req = AssessRequest(
        contact=ContactInput(email="x@test.com", business_name="Shop"),
        property=PropertyInput(
            address="1 High Street",
            postcode="SW1A 1AA",
            business_type=BusinessType.retail,
            voa_rv=20000,
            nia_sqm=100,
        ),
        flags=FlagsInput(consent_disclaimer=True),
    )
    resp = AssessResponse(
        signal="High",
        explanation="x",
        comparable_count=3,
        tone_rate=200.0,
        base_estimated_rv=18000,
        adjusted_estimated_rv=17000,
        rated_comps=[],
        location_signals={
            "crime": {"signal_level": "moderate", "included_in_report": True, "narrative": "x"},
            "flood": {"signal_level": "none", "included_in_report": False, "narrative": "y"},
        },
    )

    payload = build_evidence_payload_from_assess(resp, req)
    assert payload["modelled_rv"] == 17000
    assert payload["crime_adjustment_indicator"] == "Moderate"
    assert payload["flood_adjustment_indicator"] == "None"
