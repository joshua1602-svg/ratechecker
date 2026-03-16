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
        Three shops with different sizes but the same £/m² should produce a tone
        equal to that rate, regardless of their raw RVs.

        Small: 75 m², rate = £200/m², rv = £15,000
        Mid:  100 m², rate = £200/m², rv = £20,000
        Large: 125 m², rate = £200/m², rv = £25,000

        All three sizes sit within the ±30% primary size band (subject NIA = 100 m²).
        Three comps are required to meet _MIN_COMPS_FOR_VALUATION.
        If tone were derived from raw RVs the median would be influenced by the
        size difference; with normalisation, tone = £200/m².
        """
        small = _comp("A", rv=15_000, nia_sqm=75,
                      unadjusted_price_psm=200.0, has_summary=True)
        mid   = _comp("C", rv=20_000, nia_sqm=100,
                      unadjusted_price_psm=200.0, has_summary=True)
        large = _comp("B", rv=25_000, nia_sqm=125,
                      unadjusted_price_psm=200.0, has_summary=True)

        result = _run([small, mid, large], nia_sqm=100.0)

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

        # Build three comparables whose unadjusted_price_psm IS the Zone A rate
        # (not rv/nia — that would be an NIA rate, which is only correct for nurseries).
        # Three comps are required to meet _MIN_COMPS_FOR_VALUATION.
        comps = [
            _comp(uarn, rv=rv, nia_sqm=nia,
                  unadjusted_price_psm=zone_a_rate, has_summary=True)
            for uarn in ("A", "B", "C")
        ]
        result = _run(comps, nia_sqm=nia, business_type="retail")

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

        # Three comps required to meet _MIN_COMPS_FOR_VALUATION.
        comps = [
            _comp(uarn, rv=rv, nia_sqm=nia,
                  unadjusted_price_psm=nia_rate, has_summary=True,
                  scat_code=85)
            for uarn in ("A", "B", "C")
        ]
        result = _run(comps, nia_sqm=nia, business_type="nursery")

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

        Three valid comps are required to meet _MIN_COMPS_FOR_VALUATION.
        """
        bad  = _comp("BAD", rv=0, nia_sqm=100, has_summary=False)  # rate = 0 → excluded
        good1 = _comp("OK1", rv=10_000, nia_sqm=100,
                      unadjusted_price_psm=100.0, has_summary=True)
        good2 = _comp("OK2", rv=10_000, nia_sqm=100,
                      unadjusted_price_psm=100.0, has_summary=True)
        good3 = _comp("OK3", rv=10_000, nia_sqm=100,
                      unadjusted_price_psm=100.0, has_summary=True)
        result = _run([bad, good1, good2, good3], nia_sqm=100.0)

        rn = result.get("rate_normalisation")
        assert rn is not None
        assert rn["excluded_no_rate"] == 1
        assert rn["tier_unadjusted_psm"] == 3


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
# 4. Minimum comparable guardrail
# ---------------------------------------------------------------------------

class TestMinimumComparableGuardrail:
    """run_csa must return Insufficient Data when fewer than 3 rated comps survive."""

    def test_zero_comps_returns_insufficient_data(self):
        result = _run([], nia_sqm=100.0)
        assert result["signal"] == "Insufficient Data"

    def test_one_comp_returns_insufficient_data(self):
        """1 valid comparable — below _MIN_COMPS_FOR_VALUATION."""
        comp = _comp("A", rv=10_000, nia_sqm=100,
                     unadjusted_price_psm=100.0, has_summary=True)
        result = _run([comp], nia_sqm=100.0)
        assert result["signal"] == "Insufficient Data"

    def test_two_comps_returns_insufficient_data(self):
        """2 valid comparables — below _MIN_COMPS_FOR_VALUATION."""
        comps = [
            _comp("A", rv=10_000, nia_sqm=100, unadjusted_price_psm=100.0, has_summary=True),
            _comp("B", rv=10_500, nia_sqm=100, unadjusted_price_psm=105.0, has_summary=True),
        ]
        result = _run(comps, nia_sqm=100.0)
        assert result["signal"] == "Insufficient Data"

    def test_three_comps_produces_valuation(self):
        """Exactly 3 valid comparables meets the threshold — must not return Insufficient Data."""
        comps = [
            _comp("A", rv=10_000, nia_sqm=100, unadjusted_price_psm=100.0, has_summary=True),
            _comp("B", rv=10_500, nia_sqm=100, unadjusted_price_psm=105.0, has_summary=True),
            _comp("C", rv=10_000, nia_sqm=100, unadjusted_price_psm=100.0, has_summary=True),
        ]
        result = _run(comps, nia_sqm=100.0)
        assert result["signal"] != "Insufficient Data"
        assert result["estimated_rv"] is not None

    def test_nursery_two_comps_returns_insufficient_data(self):
        """Guardrail applies to nursery too (shared rule)."""
        comps = [
            _comp("A", rv=24_000, nia_sqm=200, unadjusted_price_psm=120.0,
                  has_summary=True, scat_code=85),
            _comp("B", rv=24_000, nia_sqm=200, unadjusted_price_psm=120.0,
                  has_summary=True, scat_code=85),
        ]
        result = _run(comps, nia_sqm=200.0, business_type="nursery")
        assert result["signal"] == "Insufficient Data"


# ---------------------------------------------------------------------------
# 5. Rate-band clustering
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

    def test_rate_proximity_overrides_denser_prime_cluster(self):
        """
        When subject_implied_rate sits clearly in the secondary band, the rate-
        proximity signal must allow the secondary cluster to beat a denser prime
        cluster that would otherwise win on density alone.

        Prime (7 comps, density ≈ 0.58) vs secondary (5 comps, density ≈ 0.42).
        Without rate proximity, prime would win on density.
        With rate proximity (subject ≈ 500, secondary median ≈ 500), secondary wins.
        """
        from api.engine.csa import _find_rate_clusters, _select_rate_cluster
        prime_items = self._rated([770, 780, 790, 800, 810, 820, 830], nia_sqm=100.0)
        sec_items   = self._rated([460, 480, 500, 520, 540], nia_sqm=100.0)
        all_items = prime_items + sec_items
        clusters = _find_rate_clusters(all_items)
        assert len(clusters) == 2, "Expected bimodal split"

        # Without rate proximity: prime (denser) wins
        selected_no_rp, _ = _select_rate_cluster(
            clusters, subject_nia=100.0, subject_implied_rate=None, min_comps=4
        )
        rates_no_rp = [r for _, _, r, _ in selected_no_rp]
        assert all(r > 600 for r in rates_no_rp), (
            "Without rate proximity, dense prime cluster should be selected"
        )

        # With rate proximity near secondary: secondary wins
        selected_rp, _ = _select_rate_cluster(
            clusters, subject_nia=100.0, subject_implied_rate=500.0, min_comps=4
        )
        rates_rp = [r for _, _, r, _ in selected_rp]
        assert all(r < 600 for r in rates_rp), (
            f"With rate proximity at 500, secondary cluster should be selected; got {rates_rp}"
        )


# ---------------------------------------------------------------------------
# 6. Street extraction and same-street preference
# ---------------------------------------------------------------------------

class TestStreetExtraction:
    """Tests for _extract_street_key() and _same_street_pool()."""

    def test_high_street_extracted(self):
        from api.engine.csa import _extract_street_key
        assert _extract_street_key("SHOP, 15, HIGH STREET, LONDON") == "HIGH STREET"

    def test_market_road_extracted(self):
        from api.engine.csa import _extract_street_key
        assert _extract_street_key("UNIT 2, MARKET ROAD, BRISTOL") == "MARKET ROAD"

    def test_oxford_street_extracted(self):
        from api.engine.csa import _extract_street_key
        assert _extract_street_key("SHOP AND PREMISES, OXFORD STREET, W1") == "OXFORD STREET"

    def test_no_suffix_returns_none(self):
        from api.engine.csa import _extract_street_key
        assert _extract_street_key("") is None
        assert _extract_street_key("SHOP, UNIT 4, WESTFIELD") is None

    def test_punctuation_stripped(self):
        from api.engine.csa import _extract_street_key
        # Apostrophes and hyphens should not break matching
        result = _extract_street_key("SHOP, 3, KING'S ROAD, LONDON")
        assert result == "KINGS ROAD"

    def _rated_with_address(self, address: str, rate: float,
                             dist: float = 50.0, weight: float = 1.0) -> tuple:
        c = _comp("x", rv=rate * 100, nia_sqm=100.0,
                  unadjusted_price_psm=rate, has_summary=True)
        c.address = address
        return (c, dist, rate, weight)

    def test_same_street_pool_returns_dominant_street(self):
        """Dominant street (highest total weight) is selected when ≥ min_comps."""
        from api.engine.csa import _same_street_pool
        high_st = [
            self._rated_with_address("SHOP, 1, HIGH STREET, LONDON", 450.0, weight=1.3)
            for _ in range(4)
        ]
        market_rd = [
            self._rated_with_address("SHOP, 2, MARKET ROAD, LONDON", 800.0, weight=0.5)
            for _ in range(2)
        ]
        pool, key = _same_street_pool(high_st + market_rd, min_comps=3)
        assert key == "HIGH STREET"
        assert len(pool) == 4

    def test_same_street_pool_falls_back_when_thin(self):
        """If dominant street has fewer than min_comps, full pool is returned."""
        from api.engine.csa import _same_street_pool
        items = [
            self._rated_with_address("SHOP, 1, HIGH STREET, LONDON", 450.0)
            for _ in range(2)  # only 2, below min_comps=3
        ] + [
            self._rated_with_address("SHOP, 1, MARKET ROAD, LONDON", 800.0)
            for _ in range(2)
        ]
        pool, key = _same_street_pool(items, min_comps=3)
        assert key is None
        assert len(pool) == 4  # full pool returned

    def test_same_street_pool_with_no_street_address(self):
        """All comparables with empty addresses → full pool returned (no key)."""
        from api.engine.csa import _same_street_pool
        items = [(_comp(str(i), rv=500*100, nia_sqm=100,
                        unadjusted_price_psm=500.0, has_summary=True),
                  50.0, 500.0, 1.0)
                 for i in range(5)]
        pool, key = _same_street_pool(items, min_comps=3)
        assert key is None
        assert pool is items  # exact same object returned

    def test_end_to_end_same_street_filters_out_prime_distant_street(self):
        """
        End-to-end: 4 secondary-rate comparables on HIGH STREET (80m, high weight)
        and 4 prime-rate comparables on MARKET ROAD (600m, low weight).
        Same-street filtering should narrow the pool to HIGH STREET, giving a
        secondary-level tone.
        """
        from api.engine.csa import itza_from_nia

        # HIGH STREET: 80m away (same_parade → proximity 1.0 × source 1.3 = 1.3)
        hs_lat = 51.5 + 80 / 111_000
        high_st = [
            _comp(f"hs{i}", rv=r * 100, nia_sqm=100,
                  unadjusted_price_psm=float(r), has_summary=True,
                  lat=hs_lat, lon=-0.1)
            for i, r in enumerate([440, 450, 460, 480])
        ]
        for c in high_st:
            c.address = "SHOP, 1, HIGH STREET, LONDON"

        # MARKET ROAD: 600m away (broader → proximity 0.5 × source 1.3 = 0.65)
        mr_lat = 51.5 + 600 / 111_000
        market_rd = [
            _comp(f"mr{i}", rv=r * 100, nia_sqm=100,
                  unadjusted_price_psm=float(r), has_summary=True,
                  lat=mr_lat, lon=-0.1)
            for i, r in enumerate([780, 800, 820, 840])
        ]
        for c in market_rd:
            c.address = "SHOP, 1, MARKET ROAD, LONDON"

        # voa_rv implies subject Zone A rate ≈ 450
        voa_rv = round(450 * itza_from_nia(100.0))
        result = _run(high_st + market_rd, nia_sqm=100.0, voa_rv=float(voa_rv))

        assert result["signal"] != "Insufficient Data"
        assert result["tone_rate"] < 600, (
            f"Same-street filtering should give secondary tone (<600), got {result['tone_rate']}"
        )
        dbg = result.get("_debug", {})
        assert dbg.get("same_street_comparable_count", 0) == 4
        assert dbg.get("same_street_key") == "HIGH STREET"
