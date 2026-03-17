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

import math
import re
import statistics
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

def _proximity_weight(distance_m: float, rules: dict) -> float:  # noqa: ARG001
    """Smooth inverse-distance weight: 1 / (1 + alpha × distance_km)."""
    distance_km = distance_m / 1000.0
    return 1.0 / (1.0 + _DISTANCE_DECAY_ALPHA * distance_km)


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
_RESTAURANT_RADIUS_M: int = 3_000
_RESTAURANT_SIZE_BAND_PCT: int = 75

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
    subject_implied_rate: float | None,
    min_comps: int = _MIN_COMPS_FOR_VALUATION,
    same_street_anchor: str | None = None,
) -> tuple[list[_ClusterItem], int, str]:
    """
    Conservative two-stage cluster selection for retail.

    Stage A — plausibility gate (when subject_implied_rate is known):
        Reject any cluster whose median rate exceeds
        subject_implied_rate × _PRIME_PLAUSIBILITY_THRESHOLD (1.25).
        Last-resort exception: if ALL clusters are rejected, admit the
        lowest-rate cluster to avoid spurious Insufficient Data when the
        entire local market is genuinely prime.

    Stage B — selection among plausible clusters:
        Score per cluster: density + nia_similarity + rate_proximity²
        (same formula as _select_rate_cluster).
        Tie-break: lower-rate cluster wins (conservative bias, FR3).

    No mixed-pool reblending (FR2):
        If the best plausible cluster has fewer than min_comps comparables,
        the next plausible cluster (by score) is tried.  If none has
        sufficient comps the function returns ([], -2, reason) — the caller
        must return an Insufficient Data result.

    Single-cluster input:
        Returned unchanged regardless of rate level; confidence capping in
        run_csa will flag any rate mismatch.

    Returns (items, cluster_idx, reason).
        cluster_idx = -2  → no plausible cluster; caller should return
                            _insufficient_data().
    """
    if len(clusters) <= 1:
        return (clusters[0] if clusters else []), 0, "single_cluster"

    # --- Stage A: plausibility gate ---
    if subject_implied_rate and subject_implied_rate > 0:
        upper_limit = subject_implied_rate * _PRIME_PLAUSIBILITY_THRESHOLD
        plausible = [i for i, cl in enumerate(clusters)
                     if _cluster_median_rate(cl) <= upper_limit]
        if not plausible:
            # All clusters too high — last resort: admit the lowest
            lowest = min(range(len(clusters)),
                         key=lambda i: _cluster_median_rate(clusters[i]))
            plausible = [lowest]
            last_resort = True
        else:
            last_resort = False
    else:
        # No implied rate — all clusters are candidates; bias downward in ties
        plausible = list(range(len(clusters)))
        last_resort = False

    # --- Stage B: score and rank plausible clusters ---
    total_weight = sum(w for cl in clusters for _, _, _, w in cl)

    def _score_cluster(ci: int) -> float:
        cl = clusters[ci]
        density = (sum(w for _, _, _, w in cl) / total_weight
                   if total_weight > 0 else 0.0)
        nia_vals = sorted(c.nia_sqm for c, _, _, _ in cl)
        med_nia = nia_vals[len(nia_vals) // 2]
        denom = max(subject_nia, med_nia)
        size_sim = min(subject_nia, med_nia) / denom if denom > 0 else 0.0
        if subject_implied_rate and subject_implied_rate > 0:
            rate_dist = (abs(_cluster_median_rate(cl) - subject_implied_rate)
                         / subject_implied_rate)
            rate_prox = max(0.0, 1.0 - rate_dist) ** 2
        else:
            rate_prox = 0.0
        score = density + size_sim + rate_prox
        # Same-street soft boost: 10% when cluster contains ≥2 comps from the
        # dominant nearby street.  Bias only — does not override the gate.
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
        plausible,
        key=lambda i: (-_score_cluster(i), _cluster_median_rate(clusters[i])),
    )

    # Pick first plausible cluster with sufficient evidence
    for ci in ranked:
        if len(clusters[ci]) >= min_comps:
            reason = ("prime_last_resort" if last_resort
                      else f"cluster_{ci}_selected")
            return clusters[ci], ci, reason

    # No plausible cluster has enough evidence
    return [], -2, "all_plausible_clusters_too_thin"


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

    # --- Size-band filter ---
    # Retail uses a tighter fallback band (±35%) than the general ±50% because
    # retail micro-markets are more size-homogeneous; admitting very large or
    # very small units adds noise rather than evidence.
    _retail_like = business_type in ("retail", "hair_beauty")
    size_fallback_pct = 35 if _retail_like else rules["filters"]["size_band_pct_fallback"]
    if _is_nursery:
        # Nursery pools are sparse and dispersed; use a materially wider size
        # tolerance than retail so evidence is not dropped too early.
        size_fallback_pct = 75

    if _is_restaurant:
        # Restaurant/cafe units vary more by layout and use; use a broader
        # fixed size band to preserve enough catchment evidence.
        size_pct = _RESTAURANT_SIZE_BAND_PCT
        size_fallback_pct = _RESTAURANT_SIZE_BAND_PCT
    else:
        size_pct = rules["filters"]["size_band_pct"]

    filtered = _filter_size(comps, nia_sqm, size_pct)
    if len(filtered) < rules["confidence"]["low_if_min_comps"]:
        size_pct = size_fallback_pct
        filtered = _filter_size(comps, nia_sqm, size_pct)

    # --- Launderette exclusion (modelling rule per INGEST_SPEC D4) ---
    filtered = [
        c for c in filtered
        if not (c.scat_code == 249 and "LAUNDERETTE" in c.description.upper())
    ]

    # --- Distance filter ---
    if _is_nursery:
        max_radius = _NURSERY_RADIUS_M
    elif _is_restaurant:
        max_radius = _RESTAURANT_RADIUS_M
    else:
        max_radius = rules["filters"]["distance_m"]["fallback"]
    with_dist: list[tuple[Comparable, float]] = []
    for c in filtered:
        d = haversine_m(lat, lon, c.lat, c.lon)
        if d <= max_radius:
            with_dist.append((c, d))

    if not with_dist:
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
        w_prox = _proximity_weight(d, rules)
        w_src = _source_weight(c, rules)
        rated.append((c, d, rate, w_prox * w_src))

    if not rated:
        return _insufficient_data()

    pre_trim_comparable_count = len(rated)

    # --- Outlier removal ---
    # Nursery: light-touch trimming only on larger pools.
    if _is_nursery:
        if len(rated) >= 5:
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
    subject_implied_rate: float | None = None  # computed for retail; used for confidence cap
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

        # Tier selection.
        if _raw_same_street_count >= _RETAIL_SAME_STREET_TIER_MIN and _dominant_street:
            pool = _street_buckets[_dominant_street]
            same_street_key = _dominant_street
            same_street_count = _raw_same_street_count
            # Prime-rate safety: if same-street pool is materially above subject
            # implied rate, revert to full pool (preserves existing FR4 behaviour).
            if subject_implied_rate and subject_implied_rate > 0:
                if _cluster_median_rate(pool) > subject_implied_rate * _PRIME_PLAUSIBILITY_THRESHOLD:
                    pool = rated
                    same_street_key = None
                    same_street_count = 0
                    same_street_reverted = True
                    _ss_anchor = None
                    _location_tier = "full_pool"
                else:
                    _location_tier = "same_street"
            else:
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

        pool, selected_cluster_id, _selection_reason = _retail_select_cluster(
            clusters,
            subject_nia=nia_sqm,
            subject_implied_rate=subject_implied_rate,
            min_comps=_MIN_COMPS_FOR_VALUATION,
            same_street_anchor=_ss_anchor,
        )
        rated = pool

    if not rated:
        return _insufficient_data()

    # --- Minimum evidence guardrail ---
    # Nursery uses a lower floor (_NURSERY_MIN_COMPS=2) because nurseries are
    # sparse in most sectors and the standard floor of 3 produces too many
    # Insufficient Data results at reasonable radii.
    _min_comps = _NURSERY_MIN_COMPS if _is_nursery else _MIN_COMPS_FOR_VALUATION
    if len(rated) < _min_comps:
        return _insufficient_data()

    # --- Tone derivation ---
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
        }
        if _is_restaurant:
            _debug["pre_trim_comparable_count"] = pre_trim_comparable_count
            _debug["post_trim_comparable_count"] = post_trim_comparable_count
            _debug["min_distance_used"] = round(min_distance_used, 0) if min_distance_used is not None else None
            _debug["max_distance_used"] = round(max_distance_used, 0) if max_distance_used is not None else None
            _debug["size_band_pct_used"] = size_pct
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

    # --- Retail confidence cap (rate-gap penalty) ---
    # When the selected tone differs materially from the subject's implied rate,
    # cap confidence regardless of comparable count.  This prevents an
    # unjustified prime tone from being labelled High confidence.
    if _retail_like and subject_implied_rate and subject_implied_rate > 0:
        _rate_gap_pct = abs(tone - subject_implied_rate) / subject_implied_rate
        if _rate_gap_pct > _CONFIDENCE_CAP_LOW_GAP:
            if confidence != "Low":
                _confidence_reason = f"capped_low_gap_{_rate_gap_pct:.0%}_vs_implied"
                confidence = "Low"
        elif _rate_gap_pct > _CONFIDENCE_CAP_MEDIUM_GAP:
            if confidence == "High":
                _confidence_reason = f"capped_medium_gap_{_rate_gap_pct:.0%}_vs_implied"
                confidence = "Medium"
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
    # Retail / restaurant_cafe: VOA values these on ITZA (In Terms of Zone A).
    #   svh.total_area_or_units for these properties is ITZA, and
    #   unadjusted_price_psm is the Zone A rate (£/m² Zone A).
    #   Tier 2 (rv / nia_sqm where nia_sqm=ITZA) also yields the Zone A rate.
    #   → Correct reconstruction: Zone A rate × subject ITZA
    #
    # Nursery: VOA values on NIA only (no zoning).
    #   unadjusted_price_psm is an NIA rate; total_area_or_units is NIA.
    #   → Correct reconstruction: NIA rate × subject NIA
    #
    # Using NIA reconstruction for retail produces a ~1.75× uplift because
    # NIA / ITZA ≈ 1.75 for a typical rectangular shop (1:3 aspect ratio).
    if _is_nursery:
        estimated_rv = round(tone * nia_sqm / 100) * 100
        subject_basis = nia_sqm
        basis_label = "NIA"
    elif _retail_like and subject_retail_method == "area_retail":
        # Retail warehouse / big-box: tone is an NIA rate; reconstruct on NIA basis.
        estimated_rv = round(tone * nia_sqm / 100) * 100
        subject_basis = nia_sqm
        basis_label = "NIA"
    else:
        # Standard high-street retail and restaurant_cafe: tone is a Zone A rate.
        itza = itza_from_nia(nia_sqm, zone_depth)
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

    result = {
        "signal": signal,
        "explanation": _explanation(signal, confidence, tone, estimated_rv, voa_rv, n_comps, business_type),
        "comparable_count": n_comps,
        "saving_estimate": saving_str,
        "tone_rate": tone,
        "estimated_rv": estimated_rv,
        "confidence": confidence,
        "rate_normalisation": {
            "tier_unadjusted_psm": tier_counts.get("unadjusted_psm", 0),
            "tier_rv_over_nia": tier_counts.get("rv_over_nia", 0),
            "excluded_no_rate": excluded_no_rate,
            "subject_basis_sqm": round(subject_basis, 2),
            "subject_basis_label": basis_label,
        },
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
