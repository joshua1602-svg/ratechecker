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

        Small: 75 m², rate = £200/m², rv = £15,000
        Large: 125 m², rate = £200/m², rv = £25,000

        Both sizes sit within the ±30% primary size band (subject NIA = 100 m²),
        so no fallback is required.  If tone were derived from raw RVs the
        median would be influenced by the size difference; with normalisation,
        tone = £200/m².
        """
        small = _comp("A", rv=15_000, nia_sqm=75,
                      unadjusted_price_psm=200.0, has_summary=True)
        large = _comp("B", rv=25_000, nia_sqm=125,
                      unadjusted_price_psm=200.0, has_summary=True)

        result = _run([small, large], nia_sqm=100.0)

        assert result["signal"] != "Insufficient Data"
        assert abs(result["tone_rate"] - 200.0) < 1.0, (
            f"Expected tone ≈ 200, got {result['tone_rate']}"
        )

    def test_retail_reconstruction_uses_itza_not_nia(self):
        """
        For retail, estimated_rv must equal tone × ITZA, not tone × NIA.

        VOA values retail on ITZA (Zone A basis).  unadjusted_price_psm is a
        Zone A rate; total_area_or_units in the SV header is ITZA.
        Multiplying a Zone A rate by NIA instead of ITZA would overvalue
        by approximately NIA/ITZA ≈ 1.75 for a typical 1:3 aspect shop.

        Zone A rate implied by comp: rv / itza_from_nia(nia_sqm)
        Subject ITZA:                itza_from_nia(100) ≈ 57.3 m²
        Expected estimated_rv:       Zone_A_rate × 57.3 ≈ comp rv (self-consistent)
        """
        from api.engine.csa import itza_from_nia
        nia = 100.0
        zone_a_rate = 200.0
        itza = itza_from_nia(nia)
        rv = zone_a_rate * itza  # what VOA would record: rate × ITZA

        # Build a comparable whose unadjusted_price_psm IS the Zone A rate
        # (not rv/nia — that would be an NIA rate, which is only correct for nurseries).
        comp = _comp("A", rv=rv, nia_sqm=nia,
                     unadjusted_price_psm=zone_a_rate, has_summary=True)
        result = _run([comp], nia_sqm=nia, business_type="retail")

        assert result["signal"] != "Insufficient Data"
        expected = round(zone_a_rate * itza / 100) * 100
        assert result["estimated_rv"] == pytest.approx(expected, abs=200), (
            f"Expected estimated_rv ≈ {expected} (tone×ITZA), got {result['estimated_rv']}"
        )
        assert result["rate_normalisation"]["subject_basis_label"] == "ITZA"

    def test_nursery_reconstruction_uses_nia(self):
        """
        For nursery, estimated_rv must equal tone × NIA.

        Nurseries are valued on NIA only; unadjusted_price_psm is an NIA rate.
        """
        nia = 200.0
        nia_rate = 120.0
        rv = nia_rate * nia  # nursery: rate × NIA = RV

        comp = _comp("A", rv=rv, nia_sqm=nia,
                     unadjusted_price_psm=nia_rate, has_summary=True,
                     scat_code=85)
        result = _run([comp], nia_sqm=nia, business_type="nursery")

        assert result["signal"] != "Insufficient Data"
        expected = round(nia_rate * nia / 100) * 100
        assert result["estimated_rv"] == pytest.approx(expected, abs=200), (
            f"Expected estimated_rv ≈ {expected} (tone×NIA), got {result['estimated_rv']}"
        )
        assert result["rate_normalisation"]["subject_basis_label"] == "NIA"


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


# ---------------------------------------------------------------------------
# 4. Rate-band clustering
# ---------------------------------------------------------------------------

class TestRateClustering:
    """
    Tests for _find_rate_clusters() and _select_rate_cluster(), and the
    end-to-end effect of clustering on the retail tone.
    """

    from api.engine.csa import _find_rate_clusters, _select_rate_cluster

    def _rated(self, rates, nia_sqm=100.0):
        """Build minimal (comp, dist, rate, weight) tuples for cluster tests."""
        from api.engine.csa import _find_rate_clusters, _select_rate_cluster
        return [
            (_comp(str(i), rv=r * nia_sqm, nia_sqm=nia_sqm,
                   unadjusted_price_psm=r, has_summary=True),
             50.0, float(r), 1.0)
            for i, r in enumerate(rates)
        ]

    def test_uniform_rates_produce_one_cluster(self):
        """When all rates are nearly equal there is only one cluster."""
        from api.engine.csa import _find_rate_clusters
        items = self._rated([200, 205, 198, 202, 201])
        clusters = _find_rate_clusters(items)
        assert len(clusters) == 1

    def test_bimodal_rates_produce_two_clusters(self):
        """A clear gap in the rate distribution must yield two clusters."""
        from api.engine.csa import _find_rate_clusters
        # Low group: 400-450, high group: 780-820 — gap of ~330 (>> 20% of range)
        items = self._rated([400, 420, 440, 450, 780, 800, 810, 820])
        clusters = _find_rate_clusters(items)
        assert len(clusters) == 2
        low_rates = [r for _, _, r, _ in clusters[0]]
        high_rates = [r for _, _, r, _ in clusters[1]]
        assert max(low_rates) < min(high_rates)

    def test_small_pool_returns_single_cluster(self):
        """Pools with fewer than 4 items must not be split."""
        from api.engine.csa import _find_rate_clusters
        items = self._rated([300, 800, 400])
        clusters = _find_rate_clusters(items)
        assert len(clusters) == 1

    def test_cluster_selection_prefers_denser_nearby_cluster(self):
        """
        When two clusters exist, the one with more weight (= more/closer
        comparables) should be selected when size similarity is equal.
        """
        from api.engine.csa import _find_rate_clusters, _select_rate_cluster
        # Low cluster: 5 items near subject; high cluster: 2 items
        low = self._rated([400, 410, 420, 430, 440], nia_sqm=100.0)
        high = self._rated([800, 820], nia_sqm=100.0)
        all_items = low + high
        clusters = _find_rate_clusters(all_items)
        selected, idx = _select_rate_cluster(clusters, subject_nia=100.0, min_comps=4)
        selected_rates = [r for _, _, r, _ in selected]
        assert all(r < 600 for r in selected_rates), (
            f"Expected low cluster to be selected, got rates {selected_rates}"
        )

    def test_cluster_fallback_when_best_cluster_too_thin(self):
        """
        If the best cluster has fewer than min_comps, the full pool must be
        returned (cluster_id = -1).
        """
        from api.engine.csa import _find_rate_clusters, _select_rate_cluster
        # Only 2 items in each cluster
        low = self._rated([400, 420], nia_sqm=100.0)
        high = self._rated([800, 820], nia_sqm=100.0)
        clusters = _find_rate_clusters(low + high)
        _, idx = _select_rate_cluster(clusters, subject_nia=100.0, min_comps=4)
        assert idx == -1, "Expected fallback (-1) when both clusters are thin"

    def test_retail_tone_uses_lower_cluster_for_secondary_subject(self):
        """
        End-to-end: a retail pool with clear prime/secondary split should
        produce a tone from the lower cluster when the lower cluster is
        denser near the subject.

        Pool: 6 secondary comps at £420–480 + 2 prime comps at £820–840.
        Subject NIA matches all comps.  The secondary cluster has 6 items
        so it wins; tone must be ≤ 500.
        """
        secondary = [
            _comp(f"s{i}", rv=r * 100, nia_sqm=100,
                  unadjusted_price_psm=float(r), has_summary=True)
            for i, r in enumerate([420, 430, 440, 450, 460, 480])
        ]
        prime = [
            _comp(f"p{i}", rv=r * 100, nia_sqm=100,
                  unadjusted_price_psm=float(r), has_summary=True)
            for i, r in enumerate([820, 840])
        ]
        result = _run(secondary + prime, nia_sqm=100.0, voa_rv=45_000.0)
        assert result["signal"] != "Insufficient Data"
        assert result["tone_rate"] <= 500, (
            f"Expected secondary-cluster tone (≤500), got {result['tone_rate']}"
        )
