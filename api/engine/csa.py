"""
Comparable Selection Algorithm (CSA).

Implements the rules defined in rules/csa.yaml:
  - Distance filtering and proximity weighting
  - Size-band filtering
  - Launderette exclusion
  - Zone A rate extraction (Tier 1 = VOA published; Tier 2 = implied)
  - Outlier removal (10th–90th percentile)
  - Tone derivation (weighted median, fallback IQR mean)
  - Confidence banding (High ≥5, Medium ≥3, Low ≥1)
  - Signal determination vs. supplied VOA RV
"""
from __future__ import annotations

import logging
import math
import re
import statistics

_csa_log = logging.getLogger(__name__)
from dataclasses import dataclass, field
from typing import List, Optional

from api.engine.rules import csa_rules


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Comparable:
    uarn: str
    address: str
    scat_code: int
    rv: float
    nia_sqm: float
    unadjusted_price_psm: Optional[float]  # Tier 1: VOA published Zone A rate
    unit_of_measurement: str               # "NIA" or "GIA"
    has_summary: bool
    lat: float
    lon: float
    description: str = field(default="")

    @property
    def rate_source(self) -> str:
        if (
            self.has_summary
            and self.unit_of_measurement == "NIA"
            and self.unadjusted_price_psm
        ):
            return "voa_published"
        return "implied"

    def zone_a_rate(self, zone_depth_m: float = 6.1) -> Optional[float]:
        """Return the Zone A equivalent rate (£/m²).  Kept for report-flow use."""
        if self.rate_source == "voa_published" and self.unadjusted_price_psm:
            return float(self.unadjusted_price_psm)
        # Tier 2: back-calculate from RV using standard ITZA model
        itza = itza_from_nia(self.nia_sqm, zone_depth_m)
        if itza > 0:
            return self.rv / itza
        return None

    def normalised_rate(self) -> tuple[Optional[float], str]:
        """Return (rate_£_per_sqm_nia, tier) for use in CSA tone derivation.

        Tier priority (explicit and deterministic):
          1. 'unadjusted_psm'  — svh.unadjusted_price_psm, used when the
             comparable has a VOA summary valuation with NIA measurement and
             a positive published rate.  This is the most reliable input.
          2. 'rv_over_nia'     — rv / nia_sqm, used when no published rate is
             available but the NIA area is known and positive.
          3. 'excluded'        — neither can be computed safely; returns
             (None, 'excluded').  The caller must skip this comparable.

        Both tiers produce a £/m² NIA rate so they are directly comparable
        and can be safely combined in the same weighted-median pool.
        """
        if (
            self.has_summary
            and self.unit_of_measurement == "NIA"
            and self.unadjusted_price_psm is not None
            and float(self.unadjusted_price_psm) > 0
        ):
            return float(self.unadjusted_price_psm), "unadjusted_psm"
        if self.nia_sqm and self.nia_sqm > 0:
            return self.rv / self.nia_sqm, "rv_over_nia"
        return None, "excluded"


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def itza_from_nia(nia_sqm: float, zone_depth_m: float = 6.1) -> float:
    """
    Convert NIA (m²) to ITZA (In Terms of Zone A) using standard halving method.

    Assumes a rectangular unit with a 1:3 width-to-depth aspect ratio, which is
    the standard fallback when measured frontage/depth are not supplied.
    This is consistent with the Tier 2 implied-rate approach in INGEST_SPEC.md.

    When actual frontage and depth are available, prefer itza_from_geometry().
    """
    if nia_sqm <= 0:
        return 0.0

    aspect_ratio = 3.0  # depth ÷ width; typical high-street retail
    width = math.sqrt(nia_sqm / aspect_ratio)
    depth_total = nia_sqm / width  # == width * aspect_ratio

    return itza_from_geometry(width, depth_total, zone_depth_m)


def itza_from_geometry(width_m: float, depth_m: float, zone_depth_m: float = 6.1) -> float:
    """
    Compute ITZA from measured frontage (width) and depth using the standard
    halving method.  Used when actual property geometry is known.
    """
    if width_m <= 0 or depth_m <= 0:
        return 0.0

    zone_relativities = [
        (zone_depth_m, 1.000),
        (zone_depth_m, 0.500),
        (zone_depth_m, 0.250),
        (float("inf"), 0.125),
    ]

    itza = 0.0
    remaining = depth_m
    for zone_depth, relativity in zone_relativities:
        used = min(remaining, zone_depth)
        itza += width_m * used * relativity
        remaining -= used
        if remaining <= 0:
            break

    return itza


# ---------------------------------------------------------------------------
# Weight helpers
# ---------------------------------------------------------------------------

def _proximity_weight(distance_m: float, rules: dict, alpha: float | None = None) -> float:  # noqa: ARG001
    """Smooth inverse-distance weight: 1 / (1 + alpha × distance_km).

    alpha defaults to _DISTANCE_DECAY_ALPHA (0.5).  Pass a smaller value for
    segments where distance is a sanity filter only, not a pricing signal.
    """
    a = alpha if alpha is not None else _DISTANCE_DECAY_ALPHA
    distance_km = distance_m / 1000.0
    return 1.0 / (1.0 + a * distance_km)


def _source_weight(comp: Comparable, rules: dict) -> float:
    sw = rules["comparable_source_weights"]
    if comp.rate_source == "voa_published":
        return sw["voa_published_rate"]
    return sw["implied_rate"]


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def _weighted_median(values: list[float], weights: list[float]) -> float:
    pairs = sorted(zip(values, weights), key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    target = total / 2
    cumulative = 0.0
    for v, w in pairs:
        cumulative += w
        if cumulative >= target:
            return v
    return pairs[-1][0]


def _iqr_mean(values: list[float]) -> float:
    """Mean of the interquartile range."""
    s = sorted(values)
    n = len(s)
    q1 = s[n // 4]
    q3 = s[3 * n // 4]
    iqr_vals = [v for v in s if q1 <= v <= q3]
    return statistics.mean(iqr_vals) if iqr_vals else statistics.mean(s)


# ---------------------------------------------------------------------------
# Same-street preference (retail only)
# ---------------------------------------------------------------------------

# Common UK street-type words used to extract a normalised street identifier
# from a VOA full_property_identifier address string.
# "MARKET", "ARCADE", "CENTRE", "MALL" are intentionally excluded: they are
# typically part of a street name (e.g. "MARKET ROAD"), not a type suffix, and
# including them causes the extractor to match them before the real suffix.
_STREET_SUFFIXES: frozenset[str] = frozenset({
    "STREET", "ROAD", "AVENUE", "LANE", "WAY", "CLOSE", "GROVE",
    "PLACE", "GARDENS", "COURT", "DRIVE", "ROW", "TERRACE", "WALK",
    "PARADE", "GATE", "BRIDGE", "HILL", "SQUARE", "MEWS", "YARD",
    "QUAY", "WHARF", "BROADWAY", "CRESCENT", "APPROACH", "PRECINCT",
})

# Minimum same-street comparables required to use the same-street pool instead
# of the full postcode-sector pool.
_MIN_SAME_STREET_COMPS: int = 4

# Retail micro-location weighting: same-street/same-parade comparables receive
# materially higher location influence within the standard multiplicative
# weighting stack (proximity × source × size × street-bias).
_RETAIL_SAME_STREET_WEIGHT_MULTIPLIER: float = 1.75

# Retail primary-tone thresholds: when same-street evidence is sufficiently
# deep/dominant, derive the primary tone from that subset only.
_RETAIL_PRIMARY_TONE_SAME_STREET_MIN_COUNT: int = 6
_RETAIL_PRIMARY_TONE_SAME_STREET_MIN_SHARE: float = 0.50

# Location-tier pool selection thresholds (retail only).
# Tier 1 — same street  : use if dominant street has ≥ this many comps.
# Tier 2 — same sector  : use if same postcode sector has ≥ this many comps.
# Tier 3 — full pool    : fallback when neither threshold is met.
_RETAIL_SAME_STREET_TIER_MIN: int = 3
_RETAIL_SECTOR_TIER_MIN: int = 6

# UK postcode regex: matches "SW4 9JN", "EC1A 1BB", "M1 1AA" etc.
_POSTCODE_RE = re.compile(r"[A-Z]{1,2}[0-9][0-9A-Z]?\s[0-9][A-Z]{2}")

# Minimum comparables in the final rated pool required to produce a valuation.
# Pools below this threshold return Insufficient Data regardless of confidence.
_MIN_COMPS_FOR_VALUATION: int = 3

# Nursery-specific overrides: larger search radius and lower evidence floor.
# Nurseries are sparse in most postcode sectors; using the same radius/threshold
# as high-street retail produces Insufficient Data for a large share of subjects.
_NURSERY_RADIUS_M: int = 10_000
_NURSERY_MIN_COMPS: int = 2
_NURSERY_NEAREST_CAP: int = 10
# Nursery size-band and distance-decay calibration.
# Nursery is sparse and size-led; use a generous initial band and a flatter
# distance decay so all comps within the search radius contribute equally.
_NURSERY_SIZE_BAND_PCT: int = 75
_NURSERY_SIZE_BAND_PCT_FALLBACK: int = 100
_NURSERY_DISTANCE_DECAY_ALPHA: float = 0.25   # vs global 0.5 — distance is sanity filter only
_RESTAURANT_RADIUS_M: int = 3_000
_RESTAURANT_SIZE_BAND_PCT: int = 75
_RESTAURANT_MAX_DISTANCE_M: int = 1_500
# Median-distance gate — tiered by local restaurant density.
# Density proxy: number of candidates within the 2500 m broad radius
# (before the 1500 m hard cap).  More candidates → denser market →
# tighter gate, because close evidence exists.  Fewer candidates →
# rural or sparse market → looser gate, because the nearest stock is
# genuinely further away.
#
#   Dense      ≥ 100 comps  →  1 000 m  (central London, major city centres)
#   Medium      50–99 comps  →  1 300 m  (inner suburbs, e.g. SW19 Wimbledon)
#   Small urban 20–49 comps  →  1 400 m  (market towns, small cities)
#   Rural         < 20 comps  →  1 500 m  (= hard cap; gate effectively off)
_RESTAURANT_DENSE_POOL_MIN:  int = 100
_RESTAURANT_MEDIUM_POOL_MIN: int = 50
_RESTAURANT_SMALL_POOL_MIN:  int = 20
_RESTAURANT_MEDIAN_GATE_DENSE:  int = 1_000
_RESTAURANT_MEDIAN_GATE_MEDIUM: int = 1_300
_RESTAURANT_MEDIAN_GATE_SMALL:  int = 1_400
_RESTAURANT_MEDIAN_GATE_RURAL:  int = 1_500
_RESTAURANT_RATE_GAP_LIMIT_SOFT_MEDIUM: float = 20.0
_RESTAURANT_RATE_GAP_LIMIT_SOFT_LOW: float = 35.0
_RESTAURANT_RATE_GAP_LIMIT_HARD: float = 50.0
# Low-density mode thresholds for the restaurant/cafe pipeline.
# Activated when the candidate pool within the broad radius is thin,
# indicating a rural or market-town market.  In low-density mode the
# hard distance cap is relaxed and the minimum rated-comp count is
# reduced so the pipeline can proceed on weaker evidence rather than
# returning Insufficient Data purely due to market sparsity.
#
# Threshold: fewer than 10 candidates within the broad radius.
# BA14-type case:  after_radius=3  → low-density (3 < 10)
# SW19-type case:  after_radius=89 → normal      (89 ≥ 10)
_RESTAURANT_LOW_DENSITY_POOL_MAX: int = 10
_RESTAURANT_LOW_DENSITY_MAX_DISTANCE_M: int = 2_500   # matches default max_radius
_RESTAURANT_LOW_DENSITY_MIN_COMPS: int = 2
# Restaurant pool cap: in dense cities, limit pool after outlier trim to
# prevent a single dense cluster from dominating the tone.
_RESTAURANT_POOL_CAP: int = 30
# Retail size-band calibration: tighter than before to reduce mixed-size
# distortion; ITZA normalization still handles rate comparability across sizes.
_RETAIL_SIZE_BAND_PCT: int = 100
_RETAIL_SIZE_BAND_PCT_FALLBACK: int = 200
# Retail post-cluster comp cap: after cluster selection, keep top N by weight
# to prevent very large dense clusters from overwhelming the tone estimate.
_RETAIL_POST_CLUSTER_MAX_COMPS: int = 25

# Retail size-similarity weight: log-ratio penalty applied multiplicatively to
# per-comp weight so larger size mismatches contribute less to tone without
# being cut from the pool.  Symmetric: same penalty for 2× too large or too small.
#   ratio 1.5× → weight × 0.67;  ratio 2× → weight × 0.50;  ratio 3× → weight × 0.33.
# Set to 1.0 — strong enough to make size the dominant driver, conservative
# enough not to zero-out comps at the wide end of the size band.
_RETAIL_SIZE_LOG_PENALTY: float = 1.0

# Smooth distance decay: weight = 1 / (1 + alpha × distance_km).
# alpha=0.5 → 0 km→1.0, 1 km→0.67, 2 km→0.5.
# Replaces the old step-based proximity bands.
_DISTANCE_DECAY_ALPHA: float = 0.5

# ---------------------------------------------------------------------------
# Retail basis classification
# ---------------------------------------------------------------------------

# Primary description text fragments that indicate an area-based (NIA) valuation
# rather than the standard Zone A / ITZA high-street zoning method.
# Matching is case-insensitive substring; most specific terms are listed first.
_RETAIL_WAREHOUSE_TERMS: tuple[str, ...] = (
    "RETAIL WAREHOUSE",
    "GARDEN CENTRE",
    "PLANT CENTRE",
)


def _classify_retail_method(
    description: str,
    sv_line_descs: tuple[str, ...] = (),
) -> str:
    """
    Return 'itza_retail' or 'area_retail' for a retail property.

    Priority:
    1. If any SV line description contains 'RETAIL ZONE A', return 'itza_retail'
       (explicit VOA evidence of a zoning-method valuation — strongest signal).
    2. If the primary description text matches a warehouse/big-box term,
       return 'area_retail' (NIA area basis).
    3. Default: 'itza_retail' (standard high-street assumption).
    """
    for line in sv_line_descs:
        if "RETAIL ZONE A" in line.upper():
            return "itza_retail"
    desc_upper = description.upper()
    for term in _RETAIL_WAREHOUSE_TERMS:
        if term in desc_upper:
            return "area_retail"
    return "itza_retail"


# ---------------------------------------------------------------------------
# Retail conservative-selection constants
# ---------------------------------------------------------------------------

# A cluster whose median rate exceeds subject_implied_rate × this factor is
# deemed implausibly prime and is rejected in Stage A of _retail_select_cluster.
# Set to 1.25 — a 25% band above the subject's own implied Zone A rate.
_PRIME_PLAUSIBILITY_THRESHOLD: float = 1.25

# Rate-gap fractions at which retail confidence is capped.
# If abs(tone − implied) / implied > _CONFIDENCE_CAP_LOW_GAP → cap at "Low".
# If abs(tone − implied) / implied > _CONFIDENCE_CAP_MEDIUM_GAP → cap at "Medium".
_CONFIDENCE_CAP_MEDIUM_GAP: float = 0.20
_CONFIDENCE_CAP_LOW_GAP: float = 0.30

# Retail cluster coherence: cap confidence when selected cluster's internal
# IQR spread (= (p75 - p25) / median) is too wide — indicates a noisy or
# mixed pool.  Independent of VOA implied rate.
_RETAIL_CLUSTER_IQR_MEDIUM: float = 0.30   # IQR/median > 30% → cap at Medium (if High)
_RETAIL_CLUSTER_IQR_HIGH: float = 0.50     # IQR/median > 50% → cap at Low

# full_pool cluster dominance: when using full_pool, require the selected
# cluster to represent at least this fraction of the pool before awarding High.
_RETAIL_FULL_POOL_MIN_DOMINANT_SHARE: float = 0.45

# Coherence bonus weight in retail cluster scoring.  A cluster's IQR/median
# ratio is mapped linearly to [0,1]: ratio=0 → 1.0; ratio ≥ scale → 0.0.
_RETAIL_COHERENCE_SCALE: float = 0.60

# Same-street coherence gate: reject the same-street tier if the pool's
# (max_rate − min_rate) / median exceeds this.  A wide spread means the street
# has multiple competing pitch levels and should not be treated as one tone.
_RETAIL_SAME_STREET_MAX_SPREAD: float = 0.70

# full_pool selection guard: the largest cluster must cover at least this
# fraction of the pool for a full_pool estimate to be defensible.  Below this
# the pool is too fragmented to drive a point estimate.
_RETAIL_FULL_POOL_SELECTION_MIN_DOMINANT_SHARE: float = 0.45


def _extract_street_key(address: str) -> str | None:
    """
    Return a normalised street identifier from a VOA address string, or None.

    Strips punctuation, uppercases the text, then finds the first token that
    is a known UK street suffix and returns "PRECEDING_WORD SUFFIX" as the key.

    Examples:
        "SHOP, 15, HIGH STREET, LONDON"    → "HIGH STREET"
        "UNIT 2, MARKET ROAD, BRISTOL"     → "MARKET ROAD"
        "SHOP AND PREMISES, OXFORD STREET" → "OXFORD STREET"
    """
    clean = re.sub(r"['\-]", "", address.upper())
    tokens = re.sub(r"[^A-Z0-9 ]", " ", clean).split()
    for i, token in enumerate(tokens):
        if token in _STREET_SUFFIXES and i > 0:
            return f"{tokens[i - 1]} {token}"
    return None


def _extract_postcode_sector(text: str) -> str | None:
    """
    Return the postcode sector from a VOA address string, or None.

    The sector is the outward code plus the first digit of the inward code,
    e.g. "SW4 9JN" → "SW4 9", "EC1A 1BB" → "EC1A 1", "M1 1AA" → "M1 1".
    """
    m = _POSTCODE_RE.search(text.upper())
    if m:
        pc = m.group(0)          # e.g. "SW4 9JN"
        parts = pc.split()       # ["SW4", "9JN"]
        if len(parts) == 2:
            return f"{parts[0]} {parts[1][0]}"
    return None


def _same_street_pool(
    rated: list[_ClusterItem],
    min_comps: int = _MIN_SAME_STREET_COMPS,
) -> tuple[list[_ClusterItem], str | None]:
    """
    Identify the dominant street in the comparable pool and return that street's
    comparables when there are sufficient to be useful.

    The dominant street is the one with the highest total comparable weight
    (proximity × source quality).  Because nearby comparables carry the highest
    proximity weights, this reliably identifies the street immediately around
    the subject.

    Returns:
        (pool, street_key) — narrowed pool and its key if min_comps is met.
        (rated, None)      — full pool when no street key can be extracted or
                             the dominant street has fewer than min_comps comps.
    """
    buckets: dict[str, list[_ClusterItem]] = {}
    for item in rated:
        c, _d, _r, _w = item
        key = _extract_street_key(c.address)
        if key:
            buckets.setdefault(key, []).append(item)

    if not buckets:
        return rated, None

    dominant = max(buckets, key=lambda s: sum(w for _, _, _, w in buckets[s]))
    same_street = buckets[dominant]

    if len(same_street) >= min_comps:
        return same_street, dominant

    return rated, None


# ---------------------------------------------------------------------------
# Rate-band clustering (retail only)
# ---------------------------------------------------------------------------

_ClusterItem = tuple  # (Comparable, dist_m, rate, weight)


def _cluster_median_rate(cluster: list[_ClusterItem]) -> float:
    """Return the median rate of a cluster (sorted lower-half median)."""
    rates = sorted(r for _, _, r, _ in cluster)
    return rates[len(rates) // 2] if rates else 0.0


def _cluster_coherence(cl: list[_ClusterItem]) -> float:
    """
    Coherence bonus in [0, 1] based on how tight the cluster's rate spread is.

    Uses IQR/median as the spread measure, mapped linearly onto [0, 1]:
        ratio = 0                    → 1.0  (perfectly tight)
        ratio = _RETAIL_COHERENCE_SCALE → 0.0  (very wide)

    Small clusters (< 2 items) are assumed internally coherent (return 1.0).
    """
    rates = sorted(r for _, _, r, _ in cl)
    n = len(rates)
    if n < 2:
        return 1.0
    med = rates[n // 2]
    if med <= 0:
        return 0.0
    p25 = rates[max(0, int(n * 0.25))]
    p75 = rates[min(n - 1, int(n * 0.75))]
    iqr_ratio = (p75 - p25) / med
    return max(0.0, 1.0 - iqr_ratio / _RETAIL_COHERENCE_SCALE)


def _find_rate_clusters(
    rated: list[_ClusterItem],
    min_gap_fraction: float = 0.20,
    max_clusters: int = 3,
) -> list[list[_ClusterItem]]:
    """
    Split rated-comparable tuples into up to max_clusters bands by finding
    natural breaks in the sorted rate distribution.

    A break is significant when the gap between two adjacent rates exceeds
    min_gap_fraction × (max_rate − min_rate).  At most (max_clusters − 1)
    breaks are used; the two largest qualifying gaps are chosen.

    Returns a list of non-empty clusters ordered from low-rate to high-rate.
    Falls back to a single-cluster list when the pool is too small (< 4) or
    the rates are effectively uniform.
    """
    if len(rated) < 4:
        return [list(rated)]

    sorted_items = sorted(rated, key=lambda x: x[2])
    rates = [r for _, _, r, _ in sorted_items]
    rate_range = rates[-1] - rates[0]

    if rate_range < 1.0:
        return [sorted_items]

    # A break must be at least 20% of the pool's rate range *and* at least
    # £50/m² in absolute terms.  The absolute floor prevents splitting a
    # near-uniform pool whose total spread happens to be narrow (e.g. a
    # single-pitch sector where rates vary by only £20–£30/m²).
    min_gap = max(min_gap_fraction * rate_range, 50.0)

    # Find all significant inter-rate gaps, keep the (max_clusters-1) largest.
    gaps = [(rates[i + 1] - rates[i], i) for i in range(len(rates) - 1)]
    break_positions = sorted(
        (i for gap, i in gaps if gap >= min_gap),
        key=lambda i: -(rates[i + 1] - rates[i]),
    )[: max_clusters - 1]

    if not break_positions:
        return [sorted_items]

    # Re-order by position for slicing.
    break_positions = sorted(break_positions)
    clusters: list[list] = []
    start = 0
    for bp in break_positions:
        clusters.append(sorted_items[start : bp + 1])
        start = bp + 1
    clusters.append(sorted_items[start:])

    return [c for c in clusters if c]


def _select_rate_cluster(
    clusters: list[list[_ClusterItem]],
    subject_nia: float,
    subject_implied_rate: float | None = None,
    min_comps: int = 3,
) -> tuple[list[_ClusterItem], int]:
    """
    Choose the cluster most representative of the subject.

    Scoring per cluster (all three terms are in [0, 1]; max total = 3.0):

        density_fraction  = cluster weight / total pool weight
                            → rewards clusters with more/closer comparables

        nia_similarity    = min(subject_nia, cluster_median_nia)
                            / max(subject_nia, cluster_median_nia)
                            → rewards clusters with matching size profile

        rate_proximity    = max(0, 1 − |cluster_median_rate − subject_implied|
                                        / subject_implied) ** 2
                            → squared soft preference for clusters near the
                              subject's own implied rate; using a squared decay
                              means clusters grossly inconsistent with the
                              subject rate lose credit rapidly (e.g. 60% off →
                              0.16 rather than 0.40 in a linear scheme) without
                              ever being hard-excluded; 0 when subject_implied_rate
                              is unknown (preserves backward compatibility)

        score = density_fraction + nia_similarity + rate_proximity

    The rate_proximity term is a *soft penalty*, not a hard filter.  A cluster
    distant in rate can still win if it is substantially denser and better-sized.

    Returns (cluster_items, cluster_index_0based).
    Falls back to the full flat pool (index -1) when:
      - there is only one cluster, or
      - the best-scoring cluster has fewer than min_comps comparables.
    """
    if len(clusters) <= 1:
        return (clusters[0] if clusters else []), 0

    total_weight = sum(w for cl in clusters for _, _, _, w in cl)
    if total_weight <= 0:
        return clusters[0], 0

    scores: list[float] = []
    for cluster in clusters:
        # --- density ---
        density = sum(w for _, _, _, w in cluster) / total_weight

        # --- size similarity ---
        nia_vals = sorted(c.nia_sqm for c, _, _, _ in cluster)
        med_nia = nia_vals[len(nia_vals) // 2]
        denom = max(subject_nia, med_nia)
        size_sim = min(subject_nia, med_nia) / denom if denom > 0 else 0.0

        # --- rate proximity (soft squared decay, requires subject_implied_rate) ---
        # Squaring the linear proximity score strongly penalises clusters that
        # are far from the subject's implied rate while leaving clusters that
        # are close largely unaffected.  A cluster 60% off the subject rate
        # scores 0.16 (vs 0.40 with a linear formula); a cluster 10% off
        # scores 0.81 (vs 0.90).  This prevents a grossly mismatched prime
        # cluster from winning solely on density.
        if subject_implied_rate and subject_implied_rate > 0:
            cluster_rates = sorted(r for _, _, r, _ in cluster)
            cluster_med_rate = cluster_rates[len(cluster_rates) // 2]
            rate_dist_frac = abs(cluster_med_rate - subject_implied_rate) / subject_implied_rate
            rate_proximity = max(0.0, 1.0 - rate_dist_frac) ** 2
        else:
            rate_proximity = 0.0

        scores.append(density + size_sim + rate_proximity)

    best = max(range(len(clusters)), key=lambda i: scores[i])

    if len(clusters[best]) >= min_comps:
        return clusters[best], best

    # Best cluster too thin — fall back to the full pool.
    return [item for cl in clusters for item in cl], -1


def _retail_select_cluster(
    clusters: list[list[_ClusterItem]],
    subject_nia: float,
    min_comps: int = _MIN_COMPS_FOR_VALUATION,
    same_street_anchor: str | None = None,
) -> tuple[list[_ClusterItem], int, str]:
    """
    Evidence-quality cluster selection for retail.

    Selects the cluster that best represents the local market using only
    independent evidence signals — no reference to the subject's VOA-implied
    rate at any point.

    Scoring per cluster (all terms in [0, 1]; max total = 3.0):

        density   = cluster weight / total pool weight
                    → rewards clusters with more / closer comparables

        size_sim  = min(subject_nia, cluster_median_nia)
                    / max(subject_nia, cluster_median_nia)
                    → rewards clusters whose size profile matches the subject

        coherence = max(0, 1 − (IQR/median) / _RETAIL_COHERENCE_SCALE)
                    → rewards internally tight rate bands; penalises noisy
                    clusters with wide IQR relative to their median rate

        score = density + size_sim + coherence

    Same-street soft boost: ×1.10 when the cluster contains ≥2 comps from
    the dominant nearby street.

    Tie-break: lower-rate cluster wins (conservative downward bias).

    No mixed-pool reblending:
        If the best-scoring cluster has fewer than min_comps, the next is
        tried.  If none has sufficient comps the function returns
        ([], -2, reason) and the caller must return Insufficient Data.

    Single-cluster input:
        Returned unchanged.

    Returns (items, cluster_idx, reason).
        cluster_idx = -2  → no cluster has enough evidence.
    """
    if len(clusters) <= 1:
        return (clusters[0] if clusters else []), 0, "single_cluster"

    total_weight = sum(w for cl in clusters for _, _, _, w in cl)

    def _score_cluster(ci: int) -> float:
        cl = clusters[ci]
        density = (sum(w for _, _, _, w in cl) / total_weight
                   if total_weight > 0 else 0.0)
        nia_vals = sorted(c.nia_sqm for c, _, _, _ in cl)
        med_nia = nia_vals[len(nia_vals) // 2]
        denom = max(subject_nia, med_nia)
        size_sim = min(subject_nia, med_nia) / denom if denom > 0 else 0.0
        coherence = _cluster_coherence(cl)
        score = density + size_sim + coherence
        # Same-street soft boost: 10% when cluster contains ≥2 comps from the
        # dominant nearby street.  Bias only.
        if same_street_anchor:
            ss_in_cluster = sum(
                1 for c, _, _, _ in cl
                if _extract_street_key(c.address) == same_street_anchor
            )
            if ss_in_cluster >= 2:
                score *= 1.10
        return score

    # Sort: highest score first; lower median rate breaks ties (downward bias)
    ranked = sorted(
        range(len(clusters)),
        key=lambda i: (-_score_cluster(i), _cluster_median_rate(clusters[i])),
    )

    # Pick first cluster with sufficient evidence
    for ci in ranked:
        if len(clusters[ci]) >= min_comps:
            return clusters[ci], ci, f"cluster_{ci}_selected"

    # No cluster has enough evidence
    return [], -2, "all_clusters_too_thin"


# ---------------------------------------------------------------------------
# Main CSA entry point
# ---------------------------------------------------------------------------

def run_csa(
    comps: list[Comparable],
    lat: float,
    lon: float,
    business_type: str,
    nia_sqm: float,
    voa_rv: float,
    subject_description: str = "",
    subject_sv_line_descs: tuple[str, ...] = (),
    subject_postcode_sector: str = "",
    subject_itza_sqm: float | None = None,
    subject_address: str = "",
) -> dict:
    """
    Run the CSA on a list of pre-fetched Comparable objects.

    Returns a dict with keys:
        signal, explanation, comparable_count, saving_estimate,
        tone_rate, estimated_rv, confidence
    """
    rules = csa_rules()
    zone_depth = 6.1
    _is_nursery = business_type == "nursery"
    _is_restaurant = business_type == "restaurant_cafe"
    _subject_street_key = _extract_street_key(subject_address) if subject_address else None

    # --- Size-band filter ---
    _retail_like = business_type in ("retail", "hair_beauty")
    if _is_restaurant:
        # Restaurant/cafe units vary in layout; keep a moderate fixed band.
        size_pct = _RESTAURANT_SIZE_BAND_PCT          # 75%
        size_fallback_pct = _RESTAURANT_SIZE_BAND_PCT
    elif _is_nursery:
        # Nursery is sparse and size-led; start generous, widen if thin.
        # Wider than the generic yaml default (30%) to preserve evidence.
        size_pct = _NURSERY_SIZE_BAND_PCT             # 75%
        size_fallback_pct = _NURSERY_SIZE_BAND_PCT_FALLBACK  # 100%
    elif _retail_like:
        # ITZA normalization converts all shops to a Zone A equivalent rate,
        # so a 50 sqm unit is directly comparable to a 200 sqm unit once the
        # zoning formula is applied.  Keep a moderately wide initial band;
        # the post-cluster cap (_RETAIL_POST_CLUSTER_MAX_COMPS) handles density
        # distortion without requiring an extremely wide DB pre-filter.
        size_pct = _RETAIL_SIZE_BAND_PCT              # 100% (was 200%)
        size_fallback_pct = _RETAIL_SIZE_BAND_PCT_FALLBACK  # 200% (was 400%)
    else:
        size_pct = rules["filters"]["size_band_pct"]
        size_fallback_pct = rules["filters"]["size_band_pct_fallback"]

    filtered = _filter_size(comps, nia_sqm, size_pct)
    if len(filtered) < rules["confidence"]["low_if_min_comps"]:
        size_pct = size_fallback_pct
        filtered = _filter_size(comps, nia_sqm, size_pct)

    # --- Launderette exclusion (modelling rule per INGEST_SPEC D4) ---
    filtered = [
        c for c in filtered
        if not (c.scat_code == 249 and "LAUNDERETTE" in c.description.upper())
    ]

    if _is_restaurant:
        _csa_log.debug("CSA_RESTAURANT_DEBUG stage=1_input_to_csa comps_in=%s filtered_after_size=%s size_pct=%s", len(comps), len(filtered), size_pct)

    # --- Distance filter ---
    if business_type == "nursery":
        max_radius = _NURSERY_RADIUS_M
    elif business_type == "restaurant_cafe":
        max_radius = rules["filters"]["distance_m"]["fallback"] * 1.25
    else:
        max_radius = rules["filters"]["distance_m"]["fallback"]
    with_dist: list[tuple[Comparable, float]] = []
    for c in filtered:
        d = haversine_m(lat, lon, c.lat, c.lon)
        if d <= max_radius:
            with_dist.append((c, d))

    # _low_density_mode is set inside the restaurant block below; initialise
    # False here so later restaurant-specific blocks can reference it safely.
    _low_density_mode: bool = False
    _distance_cap_used: int = _RESTAURANT_MAX_DISTANCE_M  # overridden below if restaurant

    if _is_restaurant:
        _before_hard_cap = len(with_dist)
        # Density detection: enter low-density mode when the broad-radius pool
        # is thin, indicating a rural or sparse market.
        _low_density_mode = _before_hard_cap < _RESTAURANT_LOW_DENSITY_POOL_MAX
        _distance_cap_used = (
            _RESTAURANT_LOW_DENSITY_MAX_DISTANCE_M if _low_density_mode
            else _RESTAURANT_MAX_DISTANCE_M
        )
        _low_density_reason = (
            f"pre_cap_count={_before_hard_cap}<{_RESTAURANT_LOW_DENSITY_POOL_MAX}"
            if _low_density_mode else "normal_density"
        )
        _csa_log.debug(
            "CSA_RESTAURANT_DEBUG density_mode=%s reason=%s",
            "low" if _low_density_mode else "normal", _low_density_reason,
        )
        with_dist = [(c, d) for c, d in with_dist if d <= _distance_cap_used]
        _csa_log.debug(
            "CSA_RESTAURANT_DEBUG stage=2_distance_filter after_radius=%s after_cap=%s cap_used=%s max_radius_used=%s",
            _before_hard_cap, len(with_dist), _distance_cap_used, max_radius,
        )

    if not with_dist:
        _csa_log.debug("CSA_RESTAURANT_DEBUG stage=2_distance_filter RETURNING_INSUFFICIENT with_dist=0")
        return _insufficient_data()

    # --- Extract effective Zone A rates and combined weights ---
    # For itza_retail comparables we ALWAYS use rv / itza_from_nia(nia_sqm)
    # (the effective rate), NOT unadjusted_price_psm (the matrix rate).
    # The VOA "unadjusted" field is the primary survey unit rate before quantity
    # allowances (e.g. ~15-25% deductions applied to large or irregular shops).
    # Using the matrix rate systematically over-states tone by ~20%.
    # Using rv/itza gives the effective rate actually used to set the RV, and
    # also self-corrects for any aspect-ratio error in itza_from_nia because
    # the same formula is applied to both comparable and subject.
    # For area_retail and non-retail segments, unadjusted_price_psm (or
    # rv/nia_sqm) is used as-is via normalised_rate().
    rated: list[tuple[Comparable, float, float, float]] = []  # (comp, dist, rate, weight)
    tier_counts: dict[str, int] = {"unadjusted_psm": 0, "rv_over_nia": 0}
    excluded_no_rate = 0
    # Nursery: use a flatter distance decay so all comps within the search
    # radius are treated more equally — distance is a sanity filter, not a
    # pricing signal for a sparse segment.
    _prox_alpha = _NURSERY_DISTANCE_DECAY_ALPHA if _is_nursery else None
    for c, d in with_dist:
        rate, tier = c.normalised_rate()
        # Effective-rate override for itza_retail (both tier-1 and tier-2).
        if _retail_like and _classify_retail_method(c.description) == "itza_retail":
            _c_itza = itza_from_nia(c.nia_sqm, zone_depth)
            if _c_itza > 0 and c.rv > 0:
                rate = c.rv / _c_itza
        if rate is None or rate <= 0:
            excluded_no_rate += 1
            continue
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        w_prox = _proximity_weight(d, rules, alpha=_prox_alpha)
        w_src = _source_weight(c, rules)
        # Retail: apply a log-ratio size-similarity penalty so comps whose NIA
        # diverges from the subject contribute proportionally less to tone.
        # Does not cut any comp from the pool — purely a soft weight adjustment.
        # Other segments (nursery, restaurant) are unchanged.
        if _retail_like and nia_sqm > 0 and c.nia_sqm > 0:
            _size_log_ratio = abs(math.log(c.nia_sqm / nia_sqm))
            w_size = math.exp(-_RETAIL_SIZE_LOG_PENALTY * _size_log_ratio)
        else:
            w_size = 1.0
        w_street = 1.0
        if _retail_like and _subject_street_key:
            if _extract_street_key(c.address) == _subject_street_key:
                w_street = _RETAIL_SAME_STREET_WEIGHT_MULTIPLIER
        rated.append((c, d, rate, w_prox * w_src * w_size * w_street))

    if not rated:
        _csa_log.debug("CSA_RESTAURANT_DEBUG stage=3_rate_extraction RETURNING_INSUFFICIENT rated=0 excluded_no_rate=%s", excluded_no_rate)
        return _insufficient_data()

    if _is_restaurant:
        _csa_log.debug("CSA_RESTAURANT_DEBUG stage=3_rate_extraction rated=%s excluded_no_rate=%s", len(rated), excluded_no_rate)

    # Restaurant path: in normal-density markets use the standard floor (3);
    # in low-density mode accept 2 rated comps so rural cases are not
    # discarded solely because of market sparsity.
    if _is_restaurant:
        _min_comps_restaurant = (
            _RESTAURANT_LOW_DENSITY_MIN_COMPS if _low_density_mode
            else _MIN_COMPS_FOR_VALUATION
        )
        _csa_log.debug(
            "CSA_RESTAURANT_DEBUG stage=3_min_comps normal_threshold=%s low_density_threshold=%s threshold_applied=%s low_density_mode=%s",
            _MIN_COMPS_FOR_VALUATION, _RESTAURANT_LOW_DENSITY_MIN_COMPS,
            _min_comps_restaurant, _low_density_mode,
        )
        if len(rated) < _min_comps_restaurant:
            _csa_log.debug(
                "CSA_RESTAURANT_DEBUG stage=3_rate_extraction RETURNING_INSUFFICIENT rated=%s < MIN_COMPS=%s",
                len(rated), _min_comps_restaurant,
            )
            return _insufficient_data()

    pre_trim_comparable_count = len(rated)

    # --- Outlier removal ---
    # Nursery: light-touch 5th–95th percentile trim.  Apply consistently from
    # pool size ≥ 3 (at n=3 this removes nothing; at n=10+ it clips one tail
    # entry each side).  Conservative: avoids over-depleting thin nursery pools.
    if _is_nursery:
        if len(rated) >= 3:
            rates_sorted = sorted(r for _, _, r, _ in rated)
            n = len(rates_sorted)
            lo = rates_sorted[max(0, int(n * 0.05))]
            hi = rates_sorted[min(n - 1, int(n * 0.95))]
            rated = [(c, d, r, w) for c, d, r, w in rated if lo <= r <= hi]
    elif _is_restaurant:
        _n_rest = len(rated)
        if _n_rest >= 6:
            rates_sorted = sorted(r for _, _, r, _ in rated)
            n = len(rates_sorted)
            lo = rates_sorted[max(0, int(n * 0.10))]
            hi = rates_sorted[min(n - 1, int(n * 0.90))]
            rated = [(c, d, r, w) for c, d, r, w in rated if lo <= r <= hi]
        elif _n_rest >= 4:
            rates_sorted = sorted(r for _, _, r, _ in rated)
            n = len(rates_sorted)
            lo = rates_sorted[max(0, int(n * 0.05))]
            hi = rates_sorted[min(n - 1, int(n * 0.95))]
            rated = [(c, d, r, w) for c, d, r, w in rated if lo <= r <= hi]
    else:
        rates_sorted = sorted(r for _, _, r, _ in rated)
        n = len(rates_sorted)
        lo = rates_sorted[max(0, int(n * 0.10))]
        hi = rates_sorted[min(n - 1, int(n * 0.90))]
        rated = [(c, d, r, w) for c, d, r, w in rated if lo <= r <= hi]

    if not rated:
        _csa_log.debug("CSA_RESTAURANT_DEBUG stage=4_outlier_trim RETURNING_INSUFFICIENT rated=0")
        return _insufficient_data()

    if _is_restaurant:
        _csa_log.debug("CSA_RESTAURANT_DEBUG stage=4_outlier_trim rated_after_trim=%s", len(rated))
        # In dense cities a restaurant pool can be 50–100 comps; cap after trim
        # to prevent one dense pitch cluster from dominating the tone estimate.
        if len(rated) > _RESTAURANT_POOL_CAP:
            rated = sorted(rated, key=lambda x: x[1])[:_RESTAURANT_POOL_CAP]

    _median_distance_m: float | None = None
    if _is_restaurant:
        # Weighted median distance: proximity weights (already on each rated tuple)
        # mean that 8 comps at 300 m outweigh 17 comps at 1 200 m, so the gate
        # reflects the effective centre of evidence mass rather than the geometric
        # midpoint of the distance distribution.
        _median_distance_m = _weighted_median(
            [d for _, d, _, _ in rated],
            [w for _, _, _, w in rated],
        )
        # Select gate threshold based on local restaurant density.
        if _before_hard_cap >= _RESTAURANT_DENSE_POOL_MIN:
            _median_gate_m = _RESTAURANT_MEDIAN_GATE_DENSE
        elif _before_hard_cap >= _RESTAURANT_MEDIUM_POOL_MIN:
            _median_gate_m = _RESTAURANT_MEDIAN_GATE_MEDIUM
        elif _before_hard_cap >= _RESTAURANT_SMALL_POOL_MIN:
            _median_gate_m = _RESTAURANT_MEDIAN_GATE_SMALL
        else:
            _median_gate_m = _RESTAURANT_MEDIAN_GATE_RURAL
        _csa_log.debug(
            "CSA_RESTAURANT_DEBUG stage=5_median_distance median_m=%s gate_m=%s density_count=%s",
            round(_median_distance_m, 1), _median_gate_m, _before_hard_cap,
        )
        if _median_distance_m > _median_gate_m:
            _csa_log.debug("CSA_RESTAURANT_DEBUG stage=5_median_distance RETURNING_INSUFFICIENT median_too_far")
            return _insufficient_data()

    # --- Nursery nearest-N cap ---
    # Keep broad search radius but limit the final nursery pool to the closest
    # comparables after all prior filtering steps.
    pre_cap_comparable_count = len(rated)
    if _is_nursery and len(rated) > _NURSERY_NEAREST_CAP:
        rated = sorted(rated, key=lambda item: item[1])[:_NURSERY_NEAREST_CAP]
    post_cap_comparable_count = len(rated)
    post_trim_comparable_count = len(rated)
    min_distance_used = min((d for _, d, _, _ in rated), default=None)
    max_distance_used = max((d for _, d, _, _ in rated), default=None)

    # --- Rate-band clustering (retail only) ---
    # Split the post-outlier pool into pitch clusters and pick the one whose
    # size profile, local density, and rate proximity best match the subject.
    # Nursery and restaurant_cafe are deliberately excluded.
    n_after_outlier = len(rated)
    cluster_count = 1
    selected_cluster_id = 0
    same_street_key: str | None = None
    same_street_count = 0
    same_street_reverted = False
    _selection_reason = "not_retail"
    subject_implied_rate: float | None = None  # computed for retail; diagnostic only
    subject_retail_method: str = "itza_retail"  # overridden inside retail block
    _location_tier: str = "not_retail"          # set inside retail block; used for debug
    _raw_same_street_count: int = 0             # dominant-street count in post-outlier pool
    same_postcode_sector_count: int = 0         # same-sector count in post-outlier pool

    if _retail_like:
        # Classify the subject's valuation basis.
        subject_retail_method = _classify_retail_method(
            subject_description, subject_sv_line_descs
        )

        # Pre-compute subject implied rate on the appropriate basis.  This is a
        # *diagnostic and selection signal only* — the final RV is derived purely
        # from the tone of comparables, not anchored to this figure.
        if voa_rv and voa_rv > 0:
            if subject_retail_method == "area_retail":
                if nia_sqm > 0:
                    subject_implied_rate = voa_rv / nia_sqm
            else:
                _s_itza = itza_from_nia(nia_sqm, zone_depth)
                if _s_itza > 0:
                    subject_implied_rate = voa_rv / _s_itza

        # --- Location-tier pool selection (retail only) ---
        # Preference order: same-street → same-postcode-sector → full pool.
        # Pool selection happens before clustering; clustering operates on
        # the selected pool unchanged.

        # Build dominant-street bucket from post-outlier rated pool.
        _street_buckets: dict[str, list] = {}
        for _item in rated:
            _c, _, _, _ = _item
            _sk = _extract_street_key(_c.address)
            if _sk:
                _street_buckets.setdefault(_sk, []).append(_item)

        if _street_buckets:
            _dominant_street = max(
                _street_buckets,
                key=lambda s: sum(w for _, _, _, w in _street_buckets[s]),
            )
            _raw_same_street_count = len(_street_buckets[_dominant_street])
        else:
            _dominant_street = None
            _raw_same_street_count = 0

        # Soft boost anchor: dominant street if ≥ 2 comps (for _retail_select_cluster).
        _ss_anchor: str | None = (
            _dominant_street if _raw_same_street_count >= 2 else None
        )

        # Same-postcode-sector comparables (using subject_postcode_sector param).
        if subject_postcode_sector:
            _sector_pool = [
                _item for _item in rated
                if _extract_postcode_sector(_item[0].address) == subject_postcode_sector
            ]
            same_postcode_sector_count = len(_sector_pool)
        else:
            _sector_pool = []
            same_postcode_sector_count = 0

        # Coherence pre-check for same-street tier: accept only when the pool's
        # rate spread is within a defensible range.  A wide spread signals that
        # the street itself contains multiple competing pitch levels and should
        # not be collapsed into a single-tier pool without further scrutiny.
        _ss_accepted = False
        if _raw_same_street_count >= _RETAIL_SAME_STREET_TIER_MIN and _dominant_street:
            _ss_rates = sorted(r for _, _, r, _ in _street_buckets[_dominant_street])
            _ss_n = len(_ss_rates)
            _ss_med = _ss_rates[_ss_n // 2] if _ss_n else 0.0
            _ss_spread = (
                (_ss_rates[-1] - _ss_rates[0]) / _ss_med
                if _ss_med > 0 and _ss_n >= 2
                else 0.0
            )
            if _ss_spread <= _RETAIL_SAME_STREET_MAX_SPREAD:
                _ss_accepted = True
            else:
                # Count-eligible but too incoherent; mark and fall through.
                same_street_reverted = True
                _ss_anchor = None

        # Tier selection.
        if _ss_accepted:
            pool = _street_buckets[_dominant_street]
            same_street_key = _dominant_street
            same_street_count = _raw_same_street_count
            _location_tier = "same_street"

        elif same_postcode_sector_count >= _RETAIL_SECTOR_TIER_MIN:
            pool = _sector_pool
            same_street_key = None
            same_street_count = 0
            _location_tier = "same_postcode_sector"

        else:
            pool = rated
            same_street_key = None
            same_street_count = 0
            _location_tier = "full_pool"

        # Cluster the selected pool and apply conservative retail cluster selection.
        clusters = _find_rate_clusters(pool)
        cluster_count = len(clusters)

        # full_pool fragmentation guard: before selecting a point estimate,
        # verify that the pool has a single defensible dominant tone.  If the
        # largest cluster covers less than _RETAIL_FULL_POOL_SELECTION_MIN_DOMINANT_SHARE
        # of the pool, the evidence is too fragmented and no estimate is produced.
        # This rejects at the selection stage — not merely in confidence labeling.
        if _location_tier == "full_pool" and cluster_count >= 2:
            _pool_size = len(pool)
            _largest_cluster = max(len(cl) for cl in clusters)
            _dominant_share = _largest_cluster / _pool_size if _pool_size > 0 else 1.0
            if _dominant_share < _RETAIL_FULL_POOL_SELECTION_MIN_DOMINANT_SHARE:
                return _insufficient_data()

        pool, selected_cluster_id, _selection_reason = _retail_select_cluster(
            clusters,
            subject_nia=nia_sqm,
            min_comps=_MIN_COMPS_FOR_VALUATION,
            same_street_anchor=_ss_anchor,
        )
        rated = pool
        # Retail: after cluster selection, cap the pool to limit large dense
        # cluster distortion.  Keep highest-weight comps (proximity × source).
        if len(rated) > _RETAIL_POST_CLUSTER_MAX_COMPS:
            rated = sorted(rated, key=lambda x: x[3], reverse=True)[:_RETAIL_POST_CLUSTER_MAX_COMPS]

    if not rated:
        return _insufficient_data()

    # --- Minimum evidence guardrail ---
    # Nursery uses a lower floor (_NURSERY_MIN_COMPS=2) because nurseries are
    # sparse in most sectors and the standard floor of 3 produces too many
    # Insufficient Data results at reasonable radii.
    # Restaurant/cafe in low-density mode also uses a reduced floor (2) for
    # the same reason; this mirrors the stage-3 early check above.
    if _is_restaurant:
        _min_comps = (
            _RESTAURANT_LOW_DENSITY_MIN_COMPS if _low_density_mode
            else _MIN_COMPS_FOR_VALUATION
        )
    elif _is_nursery:
        _min_comps = _NURSERY_MIN_COMPS
    else:
        _min_comps = _MIN_COMPS_FOR_VALUATION
    if len(rated) < _min_comps:
        return _insufficient_data()

    # --- Tone derivation ---
    tone_source = "wider_local"
    tone_source_label = "Primary tone source: Wider local comparable set"
    primary_tone_comp_count = len(rated)
    primary_tone_same_street_count = 0
    same_street_share_final = 0.0

    _same_street_subset: list[tuple[Comparable, float, float, float]] = []
    if _retail_like and _subject_street_key:
        _same_street_subset = [
            item for item in rated
            if _extract_street_key(item[0].address) == _subject_street_key
        ]
        _final_count = len(rated)
        _same_count = len(_same_street_subset)
        same_street_share_final = (_same_count / _final_count) if _final_count > 0 else 0.0
        primary_tone_same_street_count = _same_count
        _same_street_primary = (
            _same_count >= _RETAIL_PRIMARY_TONE_SAME_STREET_MIN_COUNT
            or same_street_share_final >= _RETAIL_PRIMARY_TONE_SAME_STREET_MIN_SHARE
        )
        if _same_street_primary and _same_count > 0:
            tone_source = "same_street_evidence"
            tone_source_label = (
                "Primary tone source: Same street evidence "
                "(sufficiently strong same-street set)"
            )
            primary_tone_comp_count = _same_count
            rate_vals = [r for _, _, r, _ in _same_street_subset]
            weights = [w for _, _, _, w in _same_street_subset]
        else:
            rate_vals = [r for _, _, r, _ in rated]
            weights = [w for _, _, _, w in rated]
    else:
        rate_vals = [r for _, _, r, _ in rated]
        weights = [w for _, _, _, w in rated]

    try:
        tone = _weighted_median(rate_vals, weights)
    except Exception:
        tone = _iqr_mean(rate_vals)

    # --- Debug fields (retail / hair_beauty / restaurant_cafe) ---
    # Temporary diagnostics — remove once tone accuracy is confirmed.
    _debug: dict = {}
    if _retail_like or business_type == "restaurant_cafe":
        sorted_rates = sorted(rate_vals)
        n_debug = len(sorted_rates)

        # subject_implied_rate is already computed for retail; derive for
        # restaurant_cafe here so the debug field is always populated.
        _sir: float | None = None
        if voa_rv and voa_rv > 0:
            if _is_restaurant:
                _sir = round(voa_rv / nia_sqm, 1) if nia_sqm > 0 else None
            else:
                _s_itza = itza_from_nia(nia_sqm, zone_depth)
                _sir = round(voa_rv / _s_itza, 1) if _s_itza > 0 else None

        _debug = {
            "n_initial_comps": len(comps),
            "n_after_size_and_launderette": len(filtered),
            "n_after_distance": len(with_dist),
            "n_after_outlier_removal": n_after_outlier,
            # Location-tier pool selection
            "location_tier_used": _location_tier,
            "same_street_count": _raw_same_street_count,
            "same_postcode_sector_count": same_postcode_sector_count,
            # Same-street narrowing (pool-level counts)
            "same_street_comparable_count": same_street_count,
            "same_street_key": same_street_key,
            "same_street_reverted": same_street_reverted,
            "subject_street_key": _subject_street_key,
            # Clustering and selection
            "retail_method": subject_retail_method if _retail_like else None,
            "cluster_count": cluster_count,
            "selected_cluster_id": selected_cluster_id,
            "selection_reason": _selection_reason,
            "n_in_selected_cluster": n_debug,
            "cluster_rate_min": round(sorted_rates[0], 1) if sorted_rates else None,
            "cluster_rate_p25": round(sorted_rates[max(0, int(n_debug * 0.25))], 1) if sorted_rates else None,
            "cluster_rate_median": round(sorted_rates[n_debug // 2], 1) if sorted_rates else None,
            "cluster_rate_p75": round(sorted_rates[min(n_debug - 1, int(n_debug * 0.75))], 1) if sorted_rates else None,
            "cluster_rate_max": round(sorted_rates[-1], 1) if sorted_rates else None,
            # Subject anchor (purely diagnostic — not used in tone calculation)
            "subject_implied_zone_a_rate": _sir,
            "rate_distance_to_subject": (
                round(abs(tone - _sir), 1) if _sir is not None else None
            ),
            "rate_gap_pct": (
                round(abs(tone - _sir) / _sir * 100, 1) if _sir and _sir > 0 else None
            ),
            # Tier breakdown
            "tier1_count": tier_counts.get("unadjusted_psm", 0),
            "tier2_count": tier_counts.get("rv_over_nia", 0),
            "tier1_rate_median": None,
            "tier2_rate_median": None,
            "top5_by_weight": [],
            # Primary tone-source diagnostics
            "tone_source": tone_source,
            "primary_tone_comp_count": primary_tone_comp_count,
            "primary_tone_same_street_count": primary_tone_same_street_count,
            "same_street_share_final": round(same_street_share_final, 3),
            "same_street_primary_rule_min_count": _RETAIL_PRIMARY_TONE_SAME_STREET_MIN_COUNT,
            "same_street_primary_rule_min_share": _RETAIL_PRIMARY_TONE_SAME_STREET_MIN_SHARE,
        }
        if _is_restaurant:
            _debug["pre_trim_comparable_count"] = pre_trim_comparable_count
            _debug["post_trim_comparable_count"] = post_trim_comparable_count
            _debug["min_distance_used"] = round(min_distance_used, 0) if min_distance_used is not None else None
            _debug["max_distance_used"] = round(max_distance_used, 0) if max_distance_used is not None else None
            _debug["size_band_pct_used"] = size_pct
            _debug["density_mode"] = "low" if _low_density_mode else "normal"
            _debug["distance_cap_used"] = _distance_cap_used
        t1_rates = sorted(r for c, _, r, _ in rated if c.rate_source == "voa_published")
        t2_rates = sorted(r for c, _, r, _ in rated if c.rate_source == "implied")
        if t1_rates:
            _debug["tier1_rate_median"] = round(t1_rates[len(t1_rates) // 2], 1)
        if t2_rates:
            _debug["tier2_rate_median"] = round(t2_rates[len(t2_rates) // 2], 1)
        top5 = sorted(rated, key=lambda x: x[3], reverse=True)[:5]
        _debug["top5_by_weight"] = [
            {
                "address": c.address[:60],
                "rate": round(r, 1),
                "tier": c.rate_source,
                "distance_m": round(d, 0),
                "weight": round(w, 3),
            }
            for c, d, r, w in top5
        ]

    # --- Confidence (count-based baseline) ---
    n_comps = len(rated)
    if n_comps >= rules["confidence"]["high_if_min_comps"]:
        confidence = "High"
    elif n_comps >= rules["confidence"]["medium_if_min_comps"]:
        confidence = "Medium"
    else:
        confidence = "Low"
    _confidence_reason = "count_based"

    # --- Retail confidence cap (evidence quality) ---
    # Downgrade based on independent market-evidence signals only.
    # No reference to the subject's VOA-implied rate.
    if _retail_like:
        # (1) Cluster IQR spread: wide internal spread → noisy tone.
        _cl_rates = sorted(rate_vals)
        _n_cl = len(_cl_rates)
        if _n_cl >= 2:
            _cl_p25 = _cl_rates[max(0, int(_n_cl * 0.25))]
            _cl_p75 = _cl_rates[min(_n_cl - 1, int(_n_cl * 0.75))]
            _cl_med = _cl_rates[_n_cl // 2]
            if _cl_med > 0:
                _iqr_ratio = (_cl_p75 - _cl_p25) / _cl_med
                if _iqr_ratio > _RETAIL_CLUSTER_IQR_HIGH:
                    if confidence != "Low":
                        _confidence_reason = f"capped_low_iqr_spread_{_iqr_ratio:.0%}"
                        confidence = "Low"
                elif _iqr_ratio > _RETAIL_CLUSTER_IQR_MEDIUM:
                    if confidence == "High":
                        _confidence_reason = f"capped_medium_iqr_spread_{_iqr_ratio:.0%}"
                        confidence = "Medium"
        # (2) full_pool with weak cluster dominance → Mixed evidence; cap at Medium.
        if _location_tier == "full_pool" and n_after_outlier > 0:
            _cluster_share = n_comps / n_after_outlier
            if _cluster_share < _RETAIL_FULL_POOL_MIN_DOMINANT_SHARE:
                if confidence == "High":
                    _confidence_reason = f"capped_medium_full_pool_share_{_cluster_share:.0%}"
                    confidence = "Medium"
    restaurant_rejection_reason: str | None = None
    restaurant_quality_gate_passed = True
    rate_distance_band: str | None = None
    _rate_distance_to_subject: float | None = None

    if _is_restaurant:
        _restaurant_implied_rate = (voa_rv / nia_sqm) if (voa_rv and voa_rv > 0 and nia_sqm > 0) else None

        if _restaurant_implied_rate is None:
            # No voa_rv supplied — skip rate-distance gate and proceed on market
            # evidence alone.  Mark diagnostically but do not hard-reject.
            restaurant_rejection_reason = "no_voa_rv_rate_distance_skipped"
        else:
            _rate_distance_to_subject = abs(tone - _restaurant_implied_rate)
            if _rate_distance_to_subject <= _RESTAURANT_RATE_GAP_LIMIT_SOFT_MEDIUM:
                rate_distance_band = "0_20"
            elif _rate_distance_to_subject <= _RESTAURANT_RATE_GAP_LIMIT_SOFT_LOW:
                rate_distance_band = "20_35"
                if confidence == "High":
                    confidence = "Medium"
                    _confidence_reason = "restaurant_rate_distance_20_35_cap_medium"
            elif _rate_distance_to_subject <= _RESTAURANT_RATE_GAP_LIMIT_HARD:
                rate_distance_band = "35_50"
                _high_quality_cluster = (
                    len(rated) >= 4
                )

                _moderate_quality_cluster = (
                    len(rated) >= 3
                    and (_median_distance_m is not None and _median_distance_m <= 1100)
                )

                _location_support = False

                _weak_broad_pool = not (
                    _high_quality_cluster
                    or _moderate_quality_cluster
                    or _location_support
                )
                if _weak_broad_pool:
                    restaurant_rejection_reason = "weak_pool_with_rate_distance_gt35"
                    restaurant_quality_gate_passed = False
                else:
                    # NEW: downward bias check
                    _downward_bias = (
                        _restaurant_implied_rate is not None
                        and tone < (_restaurant_implied_rate * 0.85)
                    )

                    if _downward_bias and _rate_distance_to_subject > 25:
                        restaurant_rejection_reason = "downward_biased_cluster"
                        restaurant_quality_gate_passed = False
                    else:
                        confidence = "Low"
                        _confidence_reason = "restaurant_rate_distance_35_50_cap_low"
            else:
                rate_distance_band = "gt_50"
                restaurant_rejection_reason = "rate_distance_gt_50"
                # Rate distance > 50 is a confidence signal, not a hard block.
                # Regression shows NIA drives RV; rate misalignment reflects
                # genuine overassessment — exactly the cases the product targets.
                confidence = "Low"
                _confidence_reason = "restaurant_rate_distance_gt_50_cap_low"

        if len(rated) == 2 and _rate_distance_to_subject is not None and _rate_distance_to_subject > 35:
            restaurant_rejection_reason = "two_comp_rate_distance_gt35"
            restaurant_quality_gate_passed = False

        if _debug:
            _debug["rate_distance_to_subject"] = (
                round(_rate_distance_to_subject, 1) if _rate_distance_to_subject is not None else None
            )
            _debug["restaurant_rate_distance_band"] = rate_distance_band
            _debug["rate_distance_band"] = rate_distance_band
            _debug["restaurant_rejection_reason"] = restaurant_rejection_reason
            _debug["restaurant_quality_gate_passed"] = restaurant_quality_gate_passed

        _csa_log.debug(
            "CSA_RESTAURANT_DEBUG stage=6_quality_gate passed=%s rejection_reason=%s rated_final=%s tone=%s rate_distance=%s",
            restaurant_quality_gate_passed, restaurant_rejection_reason, len(rated),
            round(tone, 2),
            round(_rate_distance_to_subject, 2) if _rate_distance_to_subject is not None else None,
        )
        if not restaurant_quality_gate_passed:
            _csa_log.debug("CSA_RESTAURANT_DEBUG stage=6_quality_gate RETURNING_INSUFFICIENT reason=%s", restaurant_rejection_reason)
            _ins = _insufficient_data()
            if _debug:
                _ins["_debug"] = _debug
            return _ins

    if _is_nursery:
        _debug = {
            "pre_cap_comparable_count": pre_cap_comparable_count,
            "post_cap_comparable_count": post_cap_comparable_count,
            "min_distance_used": round(min_distance_used, 0) if min_distance_used is not None else None,
            "max_distance_used": round(max_distance_used, 0) if max_distance_used is not None else None,
        }
    if _debug:
        _debug["confidence_reason"] = _confidence_reason

    # --- Estimated RV ---
    # The reconstruction basis must match the rate basis used by the comparables.
    #
    # Retail: standard high-street units are reconstructed on ITZA.
    #   svh.total_area_or_units for these properties is ITZA, and
    #   unadjusted_price_psm is the Zone A rate (£/m² Zone A).
    #   Tier 2 (rv / nia_sqm where nia_sqm=ITZA) also yields the Zone A rate.
    #   → Correct reconstruction: Zone A rate × subject ITZA
    #
    # Nursery + restaurant_cafe: reconstructed on NIA.
    #   unadjusted_price_psm is an NIA rate; total_area_or_units is NIA.
    #   → Correct reconstruction: NIA rate × subject NIA
    #
    # Using NIA reconstruction for retail produces a ~1.75× uplift because
    # NIA / ITZA ≈ 1.75 for a typical rectangular shop (1:3 aspect ratio).
    if _is_nursery or _is_restaurant:
        estimated_rv = round(tone * nia_sqm / 100) * 100
        subject_basis = nia_sqm
        basis_label = "NIA"
    elif _retail_like and subject_retail_method == "area_retail":
        # Retail warehouse / big-box: tone is an NIA rate; reconstruct on NIA basis.
        estimated_rv = round(tone * nia_sqm / 100) * 100
        subject_basis = nia_sqm
        basis_label = "NIA"
    else:
        # Standard high-street retail: tone is a Zone A rate.
        itza = (
            float(subject_itza_sqm)
            if subject_itza_sqm is not None and float(subject_itza_sqm) > 0
            else itza_from_nia(nia_sqm, zone_depth)
        )
        estimated_rv = round(tone * itza / 100) * 100
        subject_basis = itza
        basis_label = "ITZA"

    # --- Signal ---
    if voa_rv <= 0:
        signal = "Low"
        saving = None
    else:
        delta_pct = (voa_rv - estimated_rv) / voa_rv
        saving = max(0.0, voa_rv - estimated_rv)
        if delta_pct >= 0.20 and confidence in ("High", "Medium"):
            signal = "High"
        elif delta_pct >= 0.10:
            signal = "Medium"
        else:
            signal = "Low"

    saving_str = f"£{saving:,.0f}" if saving and saving > 0 else None

    if _debug:
        _debug["num_comps_used"] = len(rated)
        if rated:
            _dists = sorted(d for _, d, _, _ in rated)
            _debug["median_distance"] = round(statistics.median(_dists), 1)
            if nia_sqm and nia_sqm > 0:
                _ratios = [c.nia_sqm / nia_sqm for c, _, _, _ in rated]
                _debug["size_ratio_median"] = round(statistics.median(_ratios), 3)
            else:
                _debug["size_ratio_median"] = None
        else:
            _debug["median_distance"] = None
            _debug["size_ratio_median"] = None

    # --- Expose rated comps for downstream layers (e.g. layout overweighting) ---
    _rated_comps = [
        {
            "uarn": c.uarn,
            "address": c.address,
            "rv": c.rv,
            "nia_sqm": c.nia_sqm,
            "rate": round(r, 4),
            "weight": round(w, 6),
            "distance_m": round(d, 1),
            "is_same_street": bool(
                _subject_street_key and _extract_street_key(c.address) == _subject_street_key
            ),
        }
        for c, d, r, w in rated
    ]

    result = {
        "signal": signal,
        "explanation": _explanation(signal, confidence, tone, estimated_rv, voa_rv, n_comps, business_type),
        "comparable_count": n_comps,
        "saving_estimate": saving_str,
        "tone_rate": tone,
        "estimated_rv": estimated_rv,
        "confidence": confidence,
        "tone_source": tone_source,
        "tone_source_label": tone_source_label,
        "rate_normalisation": {
            "tier_unadjusted_psm": tier_counts.get("unadjusted_psm", 0),
            "tier_rv_over_nia": tier_counts.get("rv_over_nia", 0),
            "excluded_no_rate": excluded_no_rate,
            "subject_basis_sqm": round(subject_basis, 2),
            "subject_basis_label": basis_label,
        },
        "_rated_comps": _rated_comps,
    }
    if _debug:
        result["_debug"] = _debug
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _filter_size(comps: list[Comparable], target: float, pct: float) -> list[Comparable]:
    lo = target * (1 - pct / 100)
    hi = target * (1 + pct / 100)
    return [c for c in comps if lo <= c.nia_sqm <= hi]


def _insufficient_data() -> dict:
    return {
        "signal": "Insufficient Data",
        "explanation": (
            "We couldn't find enough comparable properties near you to make an accurate "
            "assessment. This may be because your area has limited comparable rental evidence "
            "in the VOA dataset, or your property type is uncommon locally."
        ),
        "comparable_count": 0,
        "saving_estimate": None,
        "tone_rate": None,
        "estimated_rv": None,
        "confidence": None,
    }


def _explanation(
    signal: str,
    confidence: str,
    tone: float,
    estimated_rv: float,
    voa_rv: float,
    n_comps: int,
    business_type: str,
) -> str:
    labels = {
        "restaurant_cafe": "restaurant/café",
        "retail": "retail",
        "hair_beauty": "hair/beauty",
        "nursery": "nursery",
        "pub": "pub",
    }
    label = labels.get(business_type, business_type)

    if signal == "High":
        delta = voa_rv - estimated_rv
        return (
            f"Based on {n_comps} comparable {label} properties nearby, the market tone is "
            f"approximately £{tone:,.0f}/m². This gives an estimated rateable value of "
            f"£{estimated_rv:,.0f}, suggesting your property may be overassessed by around "
            f"£{delta:,.0f} per year. Confidence: {confidence}."
        )
    if signal == "Medium":
        return (
            f"Based on {n_comps} comparable {label} properties nearby, the local tone of "
            f"£{tone:,.0f}/m² gives an estimated RV of £{estimated_rv:,.0f}. There may "
            f"be a modest case for overassessment against your current RV of £{voa_rv:,.0f}. "
            f"Confidence: {confidence}."
        )
    if signal == "Low":
        return (
            f"Based on {n_comps} comparable {label} properties nearby, your rateable value "
            f"appears broadly in line with the local market tone of £{tone:,.0f}/m². "
            f"Confidence: {confidence}."
        )
    return "Assessment complete."
