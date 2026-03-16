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

def _proximity_weight(distance_m: float, rules: dict) -> float:
    w = rules["normalisation"]["proximity_weighting"]
    if distance_m <= 100:
        return w["same_parade"]
    if distance_m <= 500:
        return w["close"]
    if distance_m <= 1000:
        return w["broader"]
    return w["fallback"]


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
# Rate-band clustering (retail only)
# ---------------------------------------------------------------------------

_ClusterItem = tuple  # (Comparable, dist_m, rate, weight)


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
    min_comps: int = 4,
) -> tuple[list[_ClusterItem], int]:
    """
    Choose the cluster most representative of the subject using physical
    signals only — no reference to the subject's VOA RV.

    Scoring per cluster:
        density_fraction = cluster's total weight / total pool weight
        nia_similarity   = min(subject_nia, cluster_median_nia)
                           / max(subject_nia, cluster_median_nia)
        score = density_fraction + nia_similarity

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
        density = sum(w for _, _, _, w in cluster) / total_weight

        nia_vals = sorted(c.nia_sqm for c, _, _, _ in cluster)
        med_nia = nia_vals[len(nia_vals) // 2]
        denom = max(subject_nia, med_nia)
        size_sim = min(subject_nia, med_nia) / denom if denom > 0 else 0.0

        scores.append(density + size_sim)

    best = max(range(len(clusters)), key=lambda i: scores[i])

    if len(clusters[best]) >= min_comps:
        return clusters[best], best

    # Best cluster too thin — fall back to the full pool.
    return [item for cl in clusters for item in cl], -1


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
) -> dict:
    """
    Run the CSA on a list of pre-fetched Comparable objects.

    Returns a dict with keys:
        signal, explanation, comparable_count, saving_estimate,
        tone_rate, estimated_rv, confidence
    """
    rules = csa_rules()
    zone_depth = 6.1

    # --- Size-band filter ---
    # Retail uses a tighter fallback band (±35%) than the general ±50% because
    # retail micro-markets are more size-homogeneous; admitting very large or
    # very small units adds noise rather than evidence.
    _retail_like = business_type in ("retail", "hair_beauty")
    size_fallback_pct = 35 if _retail_like else rules["filters"]["size_band_pct_fallback"]

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
    max_radius = rules["filters"]["distance_m"]["fallback"]
    with_dist: list[tuple[Comparable, float]] = []
    for c in filtered:
        d = haversine_m(lat, lon, c.lat, c.lon)
        if d <= max_radius:
            with_dist.append((c, d))

    if not with_dist:
        return _insufficient_data()

    # --- Extract normalised NIA rates and combined weights ---
    # Both tiers (unadjusted_psm and rv_over_nia) produce £/m² NIA so they
    # are directly comparable in the weighted-median pool.
    rated: list[tuple[Comparable, float, float, float]] = []  # (comp, dist, rate, weight)
    tier_counts: dict[str, int] = {"unadjusted_psm": 0, "rv_over_nia": 0}
    excluded_no_rate = 0
    for c, d in with_dist:
        rate, tier = c.normalised_rate()
        if rate is None or rate <= 0:
            excluded_no_rate += 1
            continue
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        w_prox = _proximity_weight(d, rules)
        w_src = _source_weight(c, rules)
        rated.append((c, d, rate, w_prox * w_src))

    if not rated:
        return _insufficient_data()

    # --- Outlier removal (10th–90th percentile) ---
    rates_sorted = sorted(r for _, _, r, _ in rated)
    n = len(rates_sorted)
    lo = rates_sorted[max(0, int(n * 0.10))]
    hi = rates_sorted[min(n - 1, int(n * 0.90))]
    rated = [(c, d, r, w) for c, d, r, w in rated if lo <= r <= hi]

    if not rated:
        return _insufficient_data()

    # --- Rate-band clustering (retail only) ---
    # Split the post-outlier pool into pitch clusters and pick the one whose
    # size profile and local density best matches the subject.  This prevents
    # prime-pitch comparables (high Zone A rates) from dominating the weighted
    # median when the subject sits in the secondary market.
    #
    # Nursery and restaurant_cafe are deliberately excluded: nurseries have no
    # pitch tiers; restaurants are already filtered by cuisine/SCAT.
    n_after_outlier = len(rated)
    cluster_count = 1
    selected_cluster_id = 0

    if _retail_like:
        clusters = _find_rate_clusters(rated)
        rated, selected_cluster_id = _select_rate_cluster(
            clusters, subject_nia=nia_sqm, min_comps=4
        )
        cluster_count = len(clusters)

    if not rated:
        return _insufficient_data()

    # --- Tone derivation ---
    rate_vals = [r for _, _, r, _ in rated]
    weights = [w for _, _, _, w in rated]
    try:
        tone = _weighted_median(rate_vals, weights)
    except Exception:
        tone = _iqr_mean(rate_vals)

    # --- Debug fields (retail / hair_beauty) ---
    # Temporary diagnostics — exposes pitch-contamination evidence and cluster
    # selection outcome.  Remove once tone accuracy is confirmed.
    _debug: dict = {}
    if _retail_like or business_type == "restaurant_cafe":
        sorted_rates = sorted(rate_vals)
        n_debug = len(sorted_rates)
        _debug = {
            "n_initial_comps": len(comps),
            "n_after_size_and_launderette": len(filtered),
            "n_after_distance": len(with_dist),
            "n_after_outlier_removal": n_after_outlier,
            "cluster_count": cluster_count,
            "selected_cluster_id": selected_cluster_id,
            "n_in_selected_cluster": n_debug,
            "cluster_rate_min": round(sorted_rates[0], 1) if sorted_rates else None,
            "cluster_rate_p25": round(sorted_rates[max(0, int(n_debug * 0.25))], 1) if sorted_rates else None,
            "cluster_rate_median": round(sorted_rates[n_debug // 2], 1) if sorted_rates else None,
            "cluster_rate_p75": round(sorted_rates[min(n_debug - 1, int(n_debug * 0.75))], 1) if sorted_rates else None,
            "cluster_rate_max": round(sorted_rates[-1], 1) if sorted_rates else None,
            "tier1_count": tier_counts.get("unadjusted_psm", 0),
            "tier2_count": tier_counts.get("rv_over_nia", 0),
            "tier1_rate_median": None,
            "tier2_rate_median": None,
            "top5_by_weight": [],
        }
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
        if voa_rv and voa_rv > 0:
            _subject_itza = itza_from_nia(nia_sqm, zone_depth)
            _debug["subject_implied_zone_a_rate"] = (
                round(voa_rv / _subject_itza, 1) if _subject_itza > 0 else None
            )

    # --- Confidence ---
    n_comps = len(rated)
    if n_comps >= rules["confidence"]["high_if_min_comps"]:
        confidence = "High"
    elif n_comps >= rules["confidence"]["medium_if_min_comps"]:
        confidence = "Medium"
    else:
        confidence = "Low"

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
    if business_type == "nursery":
        estimated_rv = round(tone * nia_sqm / 100) * 100
        subject_basis = nia_sqm
        basis_label = "NIA"
    else:
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
