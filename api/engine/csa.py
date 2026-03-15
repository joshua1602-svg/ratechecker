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
        """Return the Zone A equivalent rate (£/m²)."""
        if self.rate_source == "voa_published" and self.unadjusted_price_psm:
            return float(self.unadjusted_price_psm)
        # Tier 2: back-calculate from RV using standard ITZA model
        itza = itza_from_nia(self.nia_sqm, zone_depth_m)
        if itza > 0:
            return self.rv / itza
        return None


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
    """
    if nia_sqm <= 0:
        return 0.0

    aspect_ratio = 3.0  # depth ÷ width; typical high-street retail
    width = math.sqrt(nia_sqm / aspect_ratio)
    depth_total = nia_sqm / width  # == width * aspect_ratio

    zone_relativities = [
        (zone_depth_m, 1.000),
        (zone_depth_m, 0.500),
        (zone_depth_m, 0.250),
        (float("inf"), 0.125),
    ]

    itza = 0.0
    remaining = depth_total
    for zone_depth, relativity in zone_relativities:
        used = min(remaining, zone_depth)
        itza += width * used * relativity
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
    size_pct = rules["filters"]["size_band_pct"]
    filtered = _filter_size(comps, nia_sqm, size_pct)
    if len(filtered) < rules["confidence"]["low_if_min_comps"]:
        size_pct = rules["filters"]["size_band_pct_fallback"]
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

    # --- Extract Zone A rates and combined weights ---
    rated: list[tuple[Comparable, float, float, float]] = []  # (comp, dist, rate, weight)
    for c, d in with_dist:
        rate = c.zone_a_rate(zone_depth)
        if rate is None or rate <= 0:
            continue
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

    # --- Tone derivation ---
    rate_vals = [r for _, _, r, _ in rated]
    weights = [w for _, _, _, w in rated]
    try:
        tone = _weighted_median(rate_vals, weights)
    except Exception:
        tone = _iqr_mean(rate_vals)

    # --- Confidence ---
    n_comps = len(rated)
    if n_comps >= rules["confidence"]["high_if_min_comps"]:
        confidence = "High"
    elif n_comps >= rules["confidence"]["medium_if_min_comps"]:
        confidence = "Medium"
    else:
        confidence = "Low"

    # --- Estimated RV ---
    itza = itza_from_nia(nia_sqm, zone_depth)
    estimated_rv = round(tone * itza / 100) * 100  # round to nearest £100

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

    return {
        "signal": signal,
        "explanation": _explanation(signal, confidence, tone, estimated_rv, voa_rv, n_comps, business_type),
        "comparable_count": n_comps,
        "saving_estimate": saving_str,
        "tone_rate": tone,
        "estimated_rv": estimated_rv,
        "confidence": confidence,
    }


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
            f"approximately £{tone:,.0f}/m² Zone A. This gives an estimated rateable value of "
            f"£{estimated_rv:,.0f}, suggesting your property may be overassessed by around "
            f"£{delta:,.0f} per year. Confidence: {confidence}."
        )
    if signal == "Medium":
        return (
            f"Based on {n_comps} comparable {label} properties nearby, the local tone of "
            f"£{tone:,.0f}/m² Zone A gives an estimated RV of £{estimated_rv:,.0f}. There may "
            f"be a modest case for overassessment against your current RV of £{voa_rv:,.0f}. "
            f"Confidence: {confidence}."
        )
    if signal == "Low":
        return (
            f"Based on {n_comps} comparable {label} properties nearby, your rateable value "
            f"appears broadly in line with the local market tone of £{tone:,.0f}/m² Zone A. "
            f"Confidence: {confidence}."
        )
    return "Assessment complete."
