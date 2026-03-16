"""
Targeted tests for CSA rate normalisation.

Verifies that:
  1. tone is derived from normalised rate (£/m² NIA), not raw RV
  2. two comparables with different sizes but the same rate level do not
     distort tone
  3. Tier 1 (unadjusted_psm) is preferred over Tier 2 (rv_over_nia)
  4. fallback to rv/nia_sqm fires when unadjusted_price_psm is absent
  5. comparables with zero/null area are excluded and counted
  6. all three segments (retail, restaurant_cafe, nursery) return a valid
     estimated_rv from run_csa()
"""
from __future__ import annotations

import pytest

from api.engine.csa import Comparable, run_csa


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _comp(
    uarn: str,
    rv: float,
    nia_sqm: float,
    unadjusted_price_psm: float | None = None,
    has_summary: bool = False,
    lat: float = 51.5,
    lon: float = -0.1,
    scat_code: int = 249,
) -> Comparable:
    return Comparable(
        uarn=uarn,
        address="",
        scat_code=scat_code,
        rv=rv,
        nia_sqm=nia_sqm,
        unadjusted_price_psm=unadjusted_price_psm,
        unit_of_measurement="NIA",
        has_summary=has_summary,
        lat=lat,
        lon=lon,
    )


def _run(comps, nia_sqm=100.0, voa_rv=10_000.0, business_type="retail"):
    return run_csa(
        comps=comps,
        lat=51.5,
        lon=-0.1,
        business_type=business_type,
        nia_sqm=nia_sqm,
        voa_rv=voa_rv,
    )


# ---------------------------------------------------------------------------
# 1. Tone is rate-based, not raw-RV-based
# ---------------------------------------------------------------------------

class TestToneIsRateBased:

    def test_different_size_same_rate_gives_stable_tone(self):
        """
        A small shop and a large shop with the same £/m² should produce a tone
        equal to that rate, regardless of their raw RVs.

        Small: 50 m², rate = £200/m², rv = £10,000
        Large: 200 m², rate = £200/m², rv = £40,000

        If tone were derived from raw RVs the median would be influenced by
        the size difference.  With normalisation, tone = £200/m².
        """
        small = _comp("A", rv=10_000, nia_sqm=50,
                      unadjusted_price_psm=200.0, has_summary=True)
        large = _comp("B", rv=40_000, nia_sqm=200,
                      unadjusted_price_psm=200.0, has_summary=True)

        result = _run([small, large], nia_sqm=100.0)

        assert result["signal"] != "Insufficient Data"
        assert abs(result["tone_rate"] - 200.0) < 1.0, (
            f"Expected tone ≈ 200, got {result['tone_rate']}"
        )

    def test_estimated_rv_uses_nia_not_itza(self):
        """
        estimated_rv must equal tone × subject_nia, not tone × ITZA.

        With tone = £200/m² and subject NIA = 100 m²:
          NIA reconstruction:  200 × 100 = £20,000  ✓
          ITZA reconstruction: 200 × ≈57 = ~£11,400  ✗  (itza_from_nia(100))
        """
        comp = _comp("A", rv=20_000, nia_sqm=100,
                     unadjusted_price_psm=200.0, has_summary=True)
        result = _run([comp], nia_sqm=100.0)

        assert result["signal"] != "Insufficient Data"
        assert result["estimated_rv"] == pytest.approx(20_000, abs=200), (
            f"Expected estimated_rv ≈ 20,000, got {result['estimated_rv']}"
        )


# ---------------------------------------------------------------------------
# 2. normalised_rate() tier logic
# ---------------------------------------------------------------------------

class TestNormalisedRate:

    def test_tier1_unadjusted_psm_used_when_available(self):
        """unadjusted_price_psm is returned as Tier 1 when valid."""
        c = _comp("A", rv=5_000, nia_sqm=50,
                  unadjusted_price_psm=150.0, has_summary=True)
        rate, tier = c.normalised_rate()
        assert tier == "unadjusted_psm"
        assert rate == pytest.approx(150.0)

    def test_tier2_rv_over_nia_used_as_fallback(self):
        """When no published rate, rv / nia_sqm is used as Tier 2."""
        c = _comp("A", rv=8_000, nia_sqm=80, has_summary=False)
        rate, tier = c.normalised_rate()
        assert tier == "rv_over_nia"
        assert rate == pytest.approx(100.0)  # 8000 / 80

    def test_tier1_preferred_over_implied_rv_nia(self):
        """
        When unadjusted_price_psm and rv/nia disagree, Tier 1 wins.
        unadjusted_psm = £200, rv/nia = £100 — must return 200.
        """
        c = _comp("A", rv=10_000, nia_sqm=100,
                  unadjusted_price_psm=200.0, has_summary=True)
        rate, tier = c.normalised_rate()
        assert tier == "unadjusted_psm"
        assert rate == pytest.approx(200.0)

    def test_excluded_when_nia_zero(self):
        """A comparable with nia_sqm=0 cannot be rated and must be excluded."""
        c = _comp("A", rv=5_000, nia_sqm=0, has_summary=False)
        rate, tier = c.normalised_rate()
        assert tier == "excluded"
        assert rate is None

    def test_excluded_counted_in_rate_normalisation_output(self):
        """run_csa must count comparables dropped for a zero/invalid rate.

        A comparable with rv=0 and no unadjusted_psm gives rv/nia = 0.0,
        which fails the rate > 0 guard and must appear in excluded_no_rate.
        (A comp with nia_sqm=0 is dropped earlier by the size-band filter
        and does not reach the rate extraction stage.)
        """
        bad = _comp("BAD", rv=0, nia_sqm=100, has_summary=False)  # rate = 0 → excluded
        good = _comp("OK",  rv=10_000, nia_sqm=100,
                     unadjusted_price_psm=100.0, has_summary=True)
        result = _run([bad, good], nia_sqm=100.0)

        rn = result.get("rate_normalisation")
        assert rn is not None
        assert rn["excluded_no_rate"] == 1
        assert rn["tier_unadjusted_psm"] == 1


# ---------------------------------------------------------------------------
# 3. All three segments return a valid estimated_rv
# ---------------------------------------------------------------------------

class TestAllSegments:
    """Each segment must produce an estimated_rv > 0 given valid comparables."""

    SCAT_BY_SEGMENT = {
        "retail": 249,
        "restaurant_cafe": 409,
        "nursery": 85,
    }

    def _comps_for(self, segment: str) -> list[Comparable]:
        scat = self.SCAT_BY_SEGMENT[segment]
        return [
            _comp(f"{segment}_1", rv=15_000, nia_sqm=100,
                  unadjusted_price_psm=150.0, has_summary=True,
                  scat_code=scat),
            _comp(f"{segment}_2", rv=12_000, nia_sqm=80,
                  has_summary=False, scat_code=scat),
            _comp(f"{segment}_3", rv=18_000, nia_sqm=120,
                  unadjusted_price_psm=155.0, has_summary=True,
                  scat_code=scat),
        ]

    @pytest.mark.parametrize("segment", ["retail", "restaurant_cafe", "nursery"])
    def test_estimated_rv_positive(self, segment):
        comps = self._comps_for(segment)
        result = _run(comps, nia_sqm=100.0, voa_rv=20_000.0,
                      business_type=segment)
        assert result["signal"] != "Insufficient Data", (
            f"{segment}: got Insufficient Data"
        )
        assert result["estimated_rv"] is not None
        assert result["estimated_rv"] > 0, (
            f"{segment}: estimated_rv={result['estimated_rv']}"
        )

    @pytest.mark.parametrize("segment", ["retail", "restaurant_cafe", "nursery"])
    def test_rate_normalisation_present(self, segment):
        """rate_normalisation key must be in the result for all segments."""
        comps = self._comps_for(segment)
        result = _run(comps, nia_sqm=100.0, business_type=segment)
        assert "rate_normalisation" in result
