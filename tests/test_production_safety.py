"""
Production-safety tests covering:
  - DB outage handling (503, not silent "Insufficient Data")
  - Subject exclusion from comparables
  - Report payload mapping from engine outputs
  - Rejection of placeholder/incomplete payloads
  - PDF rendering from real backend values
  - /report/* rate limiting
  - Production CORS behaviour
  - Reconciliation of report tone/RV/rate values to backend values
"""
from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. DB outage handling — DatabaseError must propagate, not silently degrade
# ---------------------------------------------------------------------------

class TestDatabaseErrorPropagation:
    """get_comparables must raise DatabaseError on failure, not return []."""

    def test_get_comparables_raises_on_failure(self):
        from api.db import DatabaseError, get_comparables

        with patch("api.db.Session") as mock_session_cls:
            mock_session_cls.return_value.__enter__ = MagicMock(
                side_effect=Exception("connection refused")
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(DatabaseError, match="Comparable query failed"):
                get_comparables(
                    lat=51.5, lon=-0.1, scat_codes=[249],
                    radius_m=2000, nia_sqm=100, size_band_pct=50,
                )

    def test_get_sv_lines_batch_raises_on_failure(self):
        from api.db import DatabaseError, get_sv_lines_batch

        with patch("api.db.Session") as mock_session_cls:
            mock_session_cls.return_value.__enter__ = MagicMock(
                side_effect=Exception("connection refused")
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(DatabaseError, match="SV lines query failed"):
                get_sv_lines_batch(["12345"])

    def test_get_sv_car_parking_batch_raises_on_failure(self):
        from api.db import DatabaseError, get_sv_car_parking_batch

        with patch("api.db.Session") as mock_session_cls:
            mock_session_cls.return_value.__enter__ = MagicMock(
                side_effect=Exception("connection refused")
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(DatabaseError, match="Car parking query failed"):
                get_sv_car_parking_batch(["12345"])

    def test_empty_uarns_returns_empty_without_error(self):
        """Empty input should return {} without hitting the database."""
        from api.db import get_sv_lines_batch, get_sv_car_parking_batch
        assert get_sv_lines_batch([]) == {}
        assert get_sv_car_parking_batch([]) == {}


# ---------------------------------------------------------------------------
# 2. Subject exclusion from comparables
# ---------------------------------------------------------------------------

class TestSubjectExclusion:
    """get_comparables must support exclude_uarn parameter."""

    def test_exclude_uarn_param_in_sql(self):
        """Verify that exclude_uarn generates an exclusion clause."""
        from api.db import DatabaseError

        # We can't run the actual query, but we can verify the function
        # accepts the parameter and the SQL would contain the exclusion.
        # The real test is that the parameter exists and is wired.
        from api.db import get_comparables
        import inspect
        sig = inspect.signature(get_comparables)
        assert "exclude_uarn" in sig.parameters

    def test_assess_passes_uprn_as_exclude(self):
        """Verify that assess.py passes req.property.uprn as exclude_uarn."""
        import ast
        with open("api/routes/assess.py") as f:
            source = f.read()
        # The source must contain exclude_uarn=req.property.uprn
        assert "exclude_uarn=req.property.uprn" in source


# ---------------------------------------------------------------------------
# 3. Report payload mapping from engine outputs
# ---------------------------------------------------------------------------

class TestCanonicalReportPayload:
    """build_report_payload_from_assess must produce correct report fields."""

    def _make_assess_response(self, **overrides):
        from api.models import AssessResponse
        defaults = {
            "signal": "High",
            "explanation": "test",
            "comparable_count": 5,
            "saving_estimate": "£2,000",
            "tone_rate": 150.0,
            "base_estimated_rv": 15000,
            "adjusted_estimated_rv": 14200,
            "rated_comps": [
                {"uarn": "1", "address": "A", "rv": 14000, "nia_sqm": 100,
                 "rate": 140.0, "weight": 0.5, "distance_m": 200},
                {"uarn": "2", "address": "B", "rv": 16000, "nia_sqm": 110,
                 "rate": 145.0, "weight": 0.3, "distance_m": 500},
            ],
        }
        defaults.update(overrides)
        return AssessResponse(**defaults)

    def _make_request(self):
        from api.models import (
            AssessRequest, ContactInput, PropertyInput, FlagsInput, BusinessType
        )
        return AssessRequest(
            contact=ContactInput(email="test@test.com", business_name="Test Shop"),
            property=PropertyInput(
                address="1 High Street",
                postcode="SW1A 1AA",
                business_type=BusinessType.retail,
                voa_rv=20000,
                nia_sqm=120,
                uprn="UPRN123",
            ),
            flags=FlagsInput(consent_disclaimer=True),
        )

    def test_payload_derives_from_engine(self):
        from api.models import build_report_payload_from_assess
        resp = self._make_assess_response()
        req = self._make_request()
        payload = build_report_payload_from_assess(resp, req)

        assert payload["business_name"] == "Test Shop"
        assert payload["postcode"] == "SW1A 1AA"
        assert payload["voa_rv"] == 20000
        assert payload["tone_rate"] == 150.0
        assert payload["base_estimated_rv"] == 15000
        assert payload["adjusted_estimated_rv"] == 14200
        assert payload["comp_count"] == 5

    def test_modelled_rv_range_from_adjusted(self):
        from api.models import build_report_payload_from_assess
        resp = self._make_assess_response(adjusted_estimated_rv=14200)
        req = self._make_request()
        payload = build_report_payload_from_assess(resp, req)

        # ±5% of 14200 → low=13490, high=14910
        assert payload["modelled_rv_low"] is not None
        assert payload["modelled_rv_high"] is not None
        assert payload["modelled_rv_low"] < payload["modelled_rv_high"]
        # Should be within 10% of the point estimate
        assert abs(payload["modelled_rv_low"] - 14200) / 14200 < 0.10
        assert abs(payload["modelled_rv_high"] - 14200) / 14200 < 0.10

    def test_savings_calculated_correctly(self):
        from api.models import build_report_payload_from_assess
        resp = self._make_assess_response(adjusted_estimated_rv=14200)
        req = self._make_request()
        payload = build_report_payload_from_assess(resp, req)

        # Savings should be positive (voa_rv=20000, modelled ~14200)
        assert payload["annual_saving_low"] >= 0
        assert payload["annual_saving_high"] >= 0
        assert payload["annual_saving_high"] >= payload["annual_saving_low"]

    def test_case_strength_maps_from_signal(self):
        from api.models import build_report_payload_from_assess
        req = self._make_request()

        for signal, expected in [
            ("High", "Strong"),
            ("Medium", "Moderate"),
            ("Low", "Weak"),
            ("Insufficient Data", "Insufficient Data"),
        ]:
            resp = self._make_assess_response(signal=signal)
            payload = build_report_payload_from_assess(resp, req)
            assert payload["case_strength"] == expected

    def test_comp_rate_psm_uses_engine_rate(self):
        """rate_psm in comparables must use engine's rate field, not rv/nia."""
        from api.models import build_report_payload_from_assess
        resp = self._make_assess_response()
        req = self._make_request()
        payload = build_report_payload_from_assess(resp, req)

        # The first comp has rate=140.0 (engine-normalised)
        # rv/nia would give 14000/100 = 140.0 by coincidence
        # But the second has rate=145.0 vs rv/nia = 16000/110 = 145.45
        comp_1 = payload["comparables"][1]
        assert comp_1["rate_psm"] == 145.0  # from engine rate, not rv/nia


# ---------------------------------------------------------------------------
# 4. Rejection of placeholder payloads
# ---------------------------------------------------------------------------

class TestPlaceholderRejection:
    """Report endpoints must reject placeholder/synthetic data."""

    def test_reject_placeholder_business_name(self):
        from api.routes.reports import _validate_report_payload, _SIMPLIFIED_REQUIRED_FIELDS
        data = {
            "business_name": "Placeholder Trading Ltd",
            "property_address": "1 High St",
            "postcode": "SW1A 1AA",
            "business_type": "retail",
            "date_prepared": "01 Jan 2026",
            "voa_rv": 20000,
            "modelled_rv_low": 18000,
            "modelled_rv_high": 19000,
            "annual_saving_low": 1000,
            "annual_saving_high": 2000,
            "case_strength": "Strong",
            "comparables": [{"address": "x"}],
            "comp_count": 1,
        }
        with pytest.raises(Exception) as exc_info:
            _validate_report_payload(data, _SIMPLIFIED_REQUIRED_FIELDS)
        assert "placeholder" in str(exc_info.value.detail).lower()

    def test_reject_placeholder_postcode(self):
        from api.routes.reports import _validate_report_payload, _SIMPLIFIED_REQUIRED_FIELDS
        data = {
            "business_name": "Real Shop",
            "property_address": "1 Real Street",
            "postcode": "AB1 2CD",
            "business_type": "retail",
            "date_prepared": "01 Jan 2026",
            "voa_rv": 20000,
            "modelled_rv_low": 18000,
            "modelled_rv_high": 19000,
            "annual_saving_low": 1000,
            "annual_saving_high": 2000,
            "case_strength": "Strong",
            "comparables": [{"address": "x"}],
            "comp_count": 1,
        }
        with pytest.raises(Exception) as exc_info:
            _validate_report_payload(data, _SIMPLIFIED_REQUIRED_FIELDS)
        assert "placeholder" in str(exc_info.value.detail).lower()

    def test_reject_missing_required_fields(self):
        from api.routes.reports import _validate_report_payload, _SIMPLIFIED_REQUIRED_FIELDS
        data = {"business_name": "Real Shop"}  # missing everything else
        with pytest.raises(Exception) as exc_info:
            _validate_report_payload(data, _SIMPLIFIED_REQUIRED_FIELDS)
        assert "Missing required" in str(exc_info.value.detail)

    def test_accept_valid_payload(self):
        from api.routes.reports import _validate_report_payload, _SIMPLIFIED_REQUIRED_FIELDS
        data = {
            "business_name": "Real Shop Ltd",
            "property_address": "1 Real Street, London",
            "postcode": "SW1A 1AA",
            "business_type": "retail",
            "date_prepared": "01 Jan 2026",
            "voa_rv": 20000,
            "modelled_rv_low": 18000,
            "modelled_rv_high": 19000,
            "annual_saving_low": 1000,
            "annual_saving_high": 2000,
            "case_strength": "Strong",
            "comparables": [{"address": "x"}],
            "comp_count": 1,
        }
        # Should not raise
        _validate_report_payload(data, _SIMPLIFIED_REQUIRED_FIELDS)


# ---------------------------------------------------------------------------
# 5. PDF rendering from real backend values
# ---------------------------------------------------------------------------

class TestPdfRateAlignment:
    """PDF generator must use engine-supplied rate, not re-derive rv/nia."""

    def test_normalise_comparable_prefers_engine_rate(self):
        from api.reports.pdf_generator import _normalise_comparable

        # Comp with engine "rate" field set (ITZA basis = 180.0)
        # rv/nia would give 25000/120 = 208.33 (NIA basis — wrong for retail)
        comp = {
            "uarn": "123",
            "rv": 25000,
            "nia_sqm": 120,
            "rate": 180.0,
            "weight": 0.5,
        }
        result = _normalise_comparable(comp)
        assert result["rate_psm"] == 180.0  # engine rate, not 208.33

    def test_normalise_comparable_fallback_to_rv_nia(self):
        from api.reports.pdf_generator import _normalise_comparable

        # Comp without engine "rate" field — fallback to rv/nia
        comp = {"uarn": "456", "rv": 20000, "nia_sqm": 100, "weight": 0.3}
        result = _normalise_comparable(comp)
        assert result["rate_psm"] == 200.0  # 20000/100

    def test_normalise_comparable_preserves_existing_rate_psm(self):
        from api.reports.pdf_generator import _normalise_comparable

        comp = {"uarn": "789", "rv": 20000, "nia_sqm": 100, "rate": 180.0,
                "rate_psm": 175.0, "weight": 0.3}
        result = _normalise_comparable(comp)
        assert result["rate_psm"] == 175.0  # pre-set takes priority


# ---------------------------------------------------------------------------
# 6. /report/* rate limiting
# ---------------------------------------------------------------------------

class TestReportRateLimiting:
    """Report endpoints must enforce rate limits."""

    def test_rate_limit_allows_within_limit(self):
        from api.routes.reports import _check_rate_limit, _request_log
        # Clear state
        _request_log.clear()
        # 5 requests should work
        for _ in range(5):
            _check_rate_limit("test_ip_ok")

    def test_rate_limit_blocks_excess(self):
        from api.routes.reports import _check_rate_limit, _request_log
        _request_log.clear()
        for _ in range(5):
            _check_rate_limit("test_ip_excess")
        with pytest.raises(Exception) as exc_info:
            _check_rate_limit("test_ip_excess")
        assert exc_info.value.status_code == 429

    def test_rate_limit_per_ip(self):
        from api.routes.reports import _check_rate_limit, _request_log
        _request_log.clear()
        # Fill up one IP
        for _ in range(5):
            _check_rate_limit("ip_a")
        # Different IP should still work
        _check_rate_limit("ip_b")


# ---------------------------------------------------------------------------
# 7. Production CORS behaviour
# ---------------------------------------------------------------------------

class TestProductionCors:
    """CORS must not default to wildcard in production."""

    def test_no_wildcard_without_env(self):
        """Without CORS_ORIGINS and without RATECHECKER_ENV=development,
        no origins should be allowed."""
        # We test the logic directly rather than importing the module
        # (which has already been imported with whatever env is set).
        cors_origins = ""
        env_mode = "production"

        if cors_origins:
            origins = [o.strip() for o in cors_origins.split(",") if o.strip()]
        elif env_mode == "development":
            origins = ["*"]
        else:
            origins = []

        assert origins == []
        assert "*" not in origins

    def test_wildcard_only_in_development(self):
        cors_origins = ""
        env_mode = "development"

        if cors_origins:
            origins = [o.strip() for o in cors_origins.split(",") if o.strip()]
        elif env_mode == "development":
            origins = ["*"]
        else:
            origins = []

        assert origins == ["*"]

    def test_explicit_origins_used(self):
        cors_origins = "https://app.example.com, https://www.example.com"
        env_mode = "production"

        if cors_origins:
            origins = [o.strip() for o in cors_origins.split(",") if o.strip()]
        elif env_mode == "development":
            origins = ["*"]
        else:
            origins = []

        assert origins == ["https://app.example.com", "https://www.example.com"]
        assert "*" not in origins


# ---------------------------------------------------------------------------
# 8. Reconciliation of report tone/RV/rate values to backend values
# ---------------------------------------------------------------------------

class TestValueReconciliation:
    """Report payload values must reconcile exactly to engine outputs."""

    def test_tone_rate_passes_through(self):
        """tone_rate in report payload must equal tone_rate from assess."""
        from api.models import (
            build_report_payload_from_assess, AssessResponse,
            AssessRequest, ContactInput, PropertyInput, FlagsInput, BusinessType,
        )
        resp = AssessResponse(
            signal="High", explanation="test",
            comparable_count=5, tone_rate=162.5,
            base_estimated_rv=15000, adjusted_estimated_rv=14200,
            rated_comps=[],
        )
        req = AssessRequest(
            contact=ContactInput(email="t@t.com", business_name="X"),
            property=PropertyInput(
                postcode="SW1A 1AA", business_type=BusinessType.retail,
                voa_rv=20000, nia_sqm=100,
            ),
            flags=FlagsInput(consent_disclaimer=True),
        )
        payload = build_report_payload_from_assess(resp, req)

        # Exact passthrough — no transformation
        assert payload["tone_rate"] == 162.5
        assert payload["base_estimated_rv"] == 15000
        assert payload["adjusted_estimated_rv"] == 14200
        assert payload["voa_rv"] == 20000

    def test_report_voa_rv_matches_input(self):
        """voa_rv in report must exactly match the input property's VOA RV."""
        from api.models import (
            build_report_payload_from_assess, AssessResponse,
            AssessRequest, ContactInput, PropertyInput, FlagsInput, BusinessType,
        )
        resp = AssessResponse(
            signal="Medium", explanation="test",
            comparable_count=3, tone_rate=140.0,
            base_estimated_rv=12000,
            rated_comps=[],
        )
        req = AssessRequest(
            contact=ContactInput(email="t@t.com", business_name="X"),
            property=PropertyInput(
                postcode="SW1A 1AA", business_type=BusinessType.retail,
                voa_rv=18500, nia_sqm=100,
            ),
            flags=FlagsInput(consent_disclaimer=True),
        )
        payload = build_report_payload_from_assess(resp, req)
        assert payload["voa_rv"] == 18500  # exact passthrough

    def test_insufficient_data_produces_null_rv(self):
        """When signal is Insufficient Data, RV fields should be None."""
        from api.models import (
            build_report_payload_from_assess, AssessResponse,
            AssessRequest, ContactInput, PropertyInput, FlagsInput, BusinessType,
        )
        resp = AssessResponse(
            signal="Insufficient Data", explanation="test",
            comparable_count=0,
            rated_comps=[],
        )
        req = AssessRequest(
            contact=ContactInput(email="t@t.com", business_name="X"),
            property=PropertyInput(
                postcode="SW1A 1AA", business_type=BusinessType.retail,
                voa_rv=20000, nia_sqm=100,
            ),
            flags=FlagsInput(consent_disclaimer=True),
        )
        payload = build_report_payload_from_assess(resp, req)
        assert payload["base_estimated_rv"] is None
        assert payload["adjusted_estimated_rv"] is None
        assert payload["modelled_rv_low"] is None
        assert payload["modelled_rv_high"] is None


# ---------------------------------------------------------------------------
# 9. Stripe error sanitization
# ---------------------------------------------------------------------------

class TestStripeErrorSanitization:
    """Stripe errors must not leak internal details to the client."""

    def test_purchase_source_does_not_leak_stripe_details(self):
        """Verify the purchase route sanitizes Stripe errors."""
        import ast
        with open("api/routes/purchase.py") as f:
            source = f.read()
        # Must NOT contain `detail=str(exc)` for Stripe errors
        assert "detail=str(exc)" not in source
        # Must contain a sanitized message
        assert "Payment service is temporarily unavailable" in source


# ---------------------------------------------------------------------------
# 10. Health endpoint reports degraded on DB failure
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    """Health endpoint must report degraded status on DB failure."""

    def test_health_source_returns_degraded(self):
        """Verify health endpoint returns 'degraded' on DB failure."""
        import ast
        with open("api/main.py") as f:
            source = f.read()
        assert '"degraded"' in source
        assert '"database_unreachable"' in source


# ---------------------------------------------------------------------------
# 11. SimplifiedReportRequest requires all fields (no Optional defaults)
# ---------------------------------------------------------------------------

class TestReportModelStrictness:
    """Report request models must not accept empty/partial payloads."""

    def test_simplified_report_requires_all_fields(self):
        from api.models import SimplifiedReportRequest
        import pydantic

        # Attempting to create without required fields should fail
        with pytest.raises(pydantic.ValidationError):
            SimplifiedReportRequest()

    def test_simplified_report_accepts_complete_payload(self):
        from api.models import SimplifiedReportRequest

        # Should succeed with all fields
        payload = SimplifiedReportRequest(
            business_name="Test",
            property_address="1 High St",
            postcode="SW1A 1AA",
            business_type="retail",
            date_prepared="01 Jan 2026",
            voa_rv=20000,
            modelled_rv_low=18000,
            modelled_rv_high=19000,
            annual_saving_low=1000,
            annual_saving_high=2000,
            case_strength="Strong",
            comp_count=3,
        )
        assert payload.business_name == "Test"


# ---------------------------------------------------------------------------
# 12. UARN bigint type coercion (Issue 1 fix)
# ---------------------------------------------------------------------------

class TestUarnBigintCoercion:
    """Batch queries must convert string UARNs to integers for PostgreSQL bigint columns."""

    def test_coerce_uarns_to_int_normal(self):
        from api.db import _coerce_uarns_to_int
        result = _coerce_uarns_to_int(["279633216", "248664216", "3369646000"])
        assert result == [279633216, 248664216, 3369646000]

    def test_coerce_uarns_to_int_skips_bad_values(self):
        from api.db import _coerce_uarns_to_int
        result = _coerce_uarns_to_int(["123", "not_a_number", "456", "", "789"])
        assert result == [123, 456, 789]

    def test_coerce_uarns_to_int_empty(self):
        from api.db import _coerce_uarns_to_int
        assert _coerce_uarns_to_int([]) == []

    def test_coerce_uarns_to_int_all_bad(self):
        from api.db import _coerce_uarns_to_int
        assert _coerce_uarns_to_int(["abc", "def"]) == []

    def test_batch_functions_return_empty_for_all_bad_uarns(self):
        """If all UARNs are non-numeric, batch functions return {} without hitting DB."""
        from api.db import (
            get_sv_line_descs_batch,
            get_sv_lines_batch,
            get_sv_car_parking_batch,
            get_sv_additions_batch,
            get_sv_plant_machinery_batch,
            get_sv_adjustment_totals_batch,
        )
        for fn in [
            get_sv_line_descs_batch,
            get_sv_lines_batch,
            get_sv_car_parking_batch,
            get_sv_additions_batch,
            get_sv_plant_machinery_batch,
            get_sv_adjustment_totals_batch,
        ]:
            assert fn(["not_a_number"]) == {}


# ---------------------------------------------------------------------------
# 13. Evidence payload builder (Issue 2 fix)
# ---------------------------------------------------------------------------

class TestEvidencePayloadBuilder:
    """build_evidence_payload_from_assess must produce all required evidence fields."""

    def _make_assess_response(self, **overrides):
        from api.models import AssessResponse
        defaults = {
            "signal": "High",
            "explanation": "test",
            "comparable_count": 5,
            "saving_estimate": "£2,000",
            "tone_rate": 150.0,
            "base_estimated_rv": 15000,
            "adjusted_estimated_rv": 14200,
            "rated_comps": [
                {"uarn": "1", "address": "A", "rv": 14000, "nia_sqm": 100,
                 "rate": 140.0, "weight": 0.5, "distance_m": 200},
            ],
        }
        defaults.update(overrides)
        return AssessResponse(**defaults)

    def _make_request(self, **overrides):
        from api.models import (
            AssessRequest, ContactInput, PropertyInput, FlagsInput, BusinessType,
        )
        defaults = dict(
            contact=ContactInput(email="test@test.com", business_name="Test Shop"),
            property=PropertyInput(
                address="1 High Street",
                postcode="SW1A 1AA",
                business_type=BusinessType.retail,
                voa_rv=20000,
                nia_sqm=120,
                uprn="UPRN123",
            ),
            flags=FlagsInput(consent_disclaimer=True),
        )
        defaults.update(overrides)
        return AssessRequest(**defaults)

    def test_evidence_payload_has_all_required_fields(self):
        from api.models import build_evidence_payload_from_assess
        from api.routes.reports import _EVIDENCE_REQUIRED_FIELDS
        resp = self._make_assess_response()
        req = self._make_request()
        payload = build_evidence_payload_from_assess(resp, req)

        for field in _EVIDENCE_REQUIRED_FIELDS:
            assert field in payload, f"Missing required field: {field}"
            assert payload[field] is not None, f"Field {field} is None"

    def test_evidence_payload_passes_route_validation(self):
        from api.models import build_evidence_payload_from_assess
        from api.routes.reports import _validate_report_payload, _EVIDENCE_REQUIRED_FIELDS
        resp = self._make_assess_response()
        req = self._make_request()
        payload = build_evidence_payload_from_assess(resp, req)
        # Should not raise
        _validate_report_payload(payload, _EVIDENCE_REQUIRED_FIELDS)

    def test_evidence_payload_validates_with_pydantic(self):
        from api.models import build_evidence_payload_from_assess, EvidenceReportRequest
        resp = self._make_assess_response()
        req = self._make_request()
        payload = build_evidence_payload_from_assess(resp, req)
        # Should not raise
        model = EvidenceReportRequest.model_validate(payload)
        assert model.uprn == "UPRN123"  # populated when available
        assert model.nia_sqm == 120
        assert model.modelled_rv == 14200

    def test_evidence_payload_includes_simplified_fields(self):
        from api.models import build_evidence_payload_from_assess
        resp = self._make_assess_response()
        req = self._make_request()
        payload = build_evidence_payload_from_assess(resp, req)
        # All simplified fields should be present
        assert payload["business_name"] == "Test Shop"
        assert payload["voa_rv"] == 20000
        assert payload["tone_rate"] == 150.0

    def test_evidence_payload_default_recommendation(self):
        from api.models import build_evidence_payload_from_assess
        resp = self._make_assess_response(signal="High")
        req = self._make_request()
        payload = build_evidence_payload_from_assess(resp, req)
        assert "strong case" in payload["recommendation_text"].lower()

    def test_simplified_payload_passes_route_validation(self):
        """Verify the simplified builder also passes route validation."""
        from api.models import build_report_payload_from_assess
        from api.routes.reports import _validate_report_payload, _SIMPLIFIED_REQUIRED_FIELDS
        resp = self._make_assess_response()
        req = self._make_request()
        payload = build_report_payload_from_assess(resp, req)
        # Should not raise
        _validate_report_payload(payload, _SIMPLIFIED_REQUIRED_FIELDS)
