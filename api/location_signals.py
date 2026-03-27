"""Directional location-signal enrichment for Evidence Pack support only.

This module intentionally does NOT alter valuation outcomes.
It provides cautious, directional public-signal context for report narrative.
"""
from __future__ import annotations

import json
import logging
import os
import math
import statistics
import time
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db import engine

log = logging.getLogger(__name__)

LOCATION_SIGNALS_ENABLED = os.environ.get("LOCATION_SIGNALS_ENABLED", "true").lower() == "true"
LOCATION_SIGNALS_CACHE_TTL_SECONDS = int(os.environ.get("LOCATION_SIGNALS_CACHE_TTL_SECONDS", "21600"))
LOCATION_SIGNALS_HTTP_TIMEOUT_SECONDS = float(os.environ.get("LOCATION_SIGNALS_HTTP_TIMEOUT_SECONDS", "4.0"))

_POLICE_BASE = "https://data.police.uk/api"
_FLOOD_BASE = "https://environment.data.gov.uk/flood-monitoring/id"

_RETAIL_RELEVANT_CATEGORIES = (
    "shoplifting",
    "anti-social-behaviour",
    "criminal-damage-arson",
    "violence-and-sexual-offences",
    "public-order",
    "robbery",
    "burglary",
)


# Calibration controls (conservative, directional-only v1).
_MIN_BENCHMARK_SAMPLES = 5
_MIN_BENCHMARK_BASELINE = 3.0
_MIN_BENCHMARK_NONZERO_POINTS = 2


@dataclass
class _CrimeCounts:
    total: int
    retail_relevant: int
    categories: dict[str, int]


class LocationSignalCache:
    def get(self, key: str) -> dict | None:
        raise NotImplementedError

    def set(self, key: str, payload: dict, ttl_seconds: int) -> None:
        raise NotImplementedError


class DatabaseLocationSignalCache(LocationSignalCache):
    """DB-backed shared cache with TTL expiry for horizontal scale."""

    def get(self, key: str) -> dict | None:
        sql = text(
            """
            SELECT payload_json
            FROM location_signal_cache
            WHERE cache_key = :cache_key
              AND expires_at_epoch > :now_epoch
            LIMIT 1
            """
        )
        try:
            with Session(engine) as session:
                row = session.execute(sql, {"cache_key": key, "now_epoch": int(time.time())}).fetchone()
            if row is None:
                return None
            return json.loads(str(row.payload_json))
        except Exception:
            log.warning("location-signals cache get failed key=%s", key, exc_info=True)
            return None

    def set(self, key: str, payload: dict, ttl_seconds: int) -> None:
        now_epoch = int(time.time())
        expires_epoch = now_epoch + max(1, int(ttl_seconds))
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        sql = text(
            """
            INSERT INTO location_signal_cache(cache_key, payload_json, created_at_epoch, expires_at_epoch)
            VALUES (:cache_key, :payload_json, :created_at_epoch, :expires_at_epoch)
            ON CONFLICT(cache_key) DO UPDATE
            SET payload_json = excluded.payload_json,
                created_at_epoch = excluded.created_at_epoch,
                expires_at_epoch = excluded.expires_at_epoch
            """
        )
        try:
            with Session(engine) as session:
                session.execute(
                    sql,
                    {
                        "cache_key": key,
                        "payload_json": payload_json,
                        "created_at_epoch": now_epoch,
                        "expires_at_epoch": expires_epoch,
                    },
                )
                session.commit()
        except Exception:
            log.warning("location-signals cache set failed key=%s", key, exc_info=True)


def ensure_location_signal_cache_table() -> None:
    stmts = [
        """
        CREATE TABLE IF NOT EXISTS location_signal_cache (
            cache_key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            created_at_epoch INTEGER NOT NULL,
            expires_at_epoch INTEGER NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_location_signal_cache_expires ON location_signal_cache(expires_at_epoch)",
    ]
    with Session(engine) as session:
        for stmt in stmts:
            session.execute(text(stmt))
        session.commit()


def _crime_na(status: str = "unavailable") -> dict:
    return {
        "status": status,
        "source": "data.police.uk",
        "month": None,
        "total_crimes": None,
        "retail_relevant_count": None,
        "retail_relevant_categories": {k: 0 for k in _RETAIL_RELEVANT_CATEGORIES},
        "baseline_method": None,
        "relative_ratio": None,
        "signal_level": "N/A",
        "included_in_report": False,
        "narrative": "N/A",
    }


def _flood_na(status: str = "unavailable") -> dict:
    return {
        "status": status,
        "source": "environment-agency",
        "in_flood_area": None,
        "active_alert": None,
        "active_warning": None,
        "nearest_area_label": None,
        "signal_level": "N/A",
        "included_in_report": False,
        "narrative": "N/A",
    }


def _is_valid_coordinate(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    return -90.0 <= float(lat) <= 90.0 and -180.0 <= float(lon) <= 180.0


def _offset_points(lat: float, lon: float) -> list[tuple[float, float]]:
    # Approximate 500m offsets in cardinal/inter-cardinal directions.
    lat_delta = 0.0045
    lon_delta = 0.0045 / max(abs(math.cos(math.radians(lat))), 0.2)
    return [
        (lat + lat_delta, lon),
        (lat - lat_delta, lon),
        (lat, lon + lon_delta),
        (lat, lon - lon_delta),
        (lat + lat_delta, lon + lon_delta),
        (lat + lat_delta, lon - lon_delta),
        (lat - lat_delta, lon + lon_delta),
        (lat - lat_delta, lon - lon_delta),
    ]


def _police_latest_month(client: httpx.Client) -> str | None:
    try:
        resp = client.get(f"{_POLICE_BASE}/crimes-street-dates")
        resp.raise_for_status()
        rows = resp.json() or []
        if not rows:
            return None
        return str(rows[0].get("date") or "") or None
    except Exception:
        return None


def _police_counts(client: httpx.Client, lat: float, lon: float, month: str) -> _CrimeCounts | None:
    params = {"lat": f"{lat:.6f}", "lng": f"{lon:.6f}", "date": month}
    resp = client.get(f"{_POLICE_BASE}/crimes-street/all-crime", params=params)
    resp.raise_for_status()
    payload = resp.json() or []
    categories = {k: 0 for k in _RETAIL_RELEVANT_CATEGORIES}
    total = 0
    retail = 0
    for row in payload:
        if not isinstance(row, dict):
            continue
        total += 1
        category = str(row.get("category") or "")
        if category in categories:
            categories[category] += 1
            retail += 1
    return _CrimeCounts(total=total, retail_relevant=retail, categories=categories)


def _classify_crime_ratio(subject_count: int, benchmark_median: float) -> tuple[str, float]:
    # Minimum baseline floor dampens noise where local benchmark is near-zero.
    baseline = max(float(benchmark_median), _MIN_BENCHMARK_BASELINE)
    ratio = float(subject_count) / baseline
    if ratio < 1.15:
        return "low", ratio
    if ratio < 1.5:
        return "moderate", ratio
    return "elevated", ratio


def get_crime_signal(latitude: float, longitude: float, *, client: httpx.Client | None = None) -> dict:
    if not _is_valid_coordinate(latitude, longitude):
        return _crime_na("unavailable")

    external_client = client is not None
    client = client or httpx.Client(timeout=LOCATION_SIGNALS_HTTP_TIMEOUT_SECONDS)
    try:
        month = _police_latest_month(client)
        if not month:
            return _crime_na("unavailable")

        subject_counts = _police_counts(client, latitude, longitude, month)
        if subject_counts is None:
            return _crime_na("unavailable")

        benchmark_counts: list[int] = []
        for b_lat, b_lon in _offset_points(latitude, longitude):
            try:
                counts = _police_counts(client, b_lat, b_lon, month)
            except Exception:
                counts = None
            if counts is not None:
                benchmark_counts.append(int(counts.retail_relevant))

        if len(benchmark_counts) < _MIN_BENCHMARK_SAMPLES:
            log.info(
                "location-signals crime benchmark_thin month=%s benchmark_n=%s",
                month,
                len(benchmark_counts),
            )
            return _crime_na("unavailable")

        nonzero_points = sum(1 for n in benchmark_counts if n > 0)
        if nonzero_points < _MIN_BENCHMARK_NONZERO_POINTS:
            log.info(
                "location-signals crime benchmark_unreliable month=%s benchmark_n=%s nonzero=%s",
                month,
                len(benchmark_counts),
                nonzero_points,
            )
            return _crime_na("unavailable")

        benchmark_median = float(statistics.median(benchmark_counts))
        signal_level, ratio = _classify_crime_ratio(subject_counts.retail_relevant, benchmark_median)
        log.info(
            "location-signals crime benchmark month=%s subject_retail=%s benchmark_n=%s benchmark_median=%.2f baseline_floor=%.1f ratio=%s level=%s",
            month,
            subject_counts.retail_relevant,
            len(benchmark_counts),
            benchmark_median,
            _MIN_BENCHMARK_BASELINE,
            f"{ratio:.3f}",
            signal_level,
        )

        if signal_level in {"moderate", "elevated"}:
            narrative = (
                f"Local public crime data indicates a {signal_level} level of retail-relevant crime intensity "
                "relative to nearby benchmark areas. This may support further review of potential end allowances "
                "depending on trading impact."
            )
            include = True
        else:
            narrative = (
                "Local public crime data does not indicate a materially elevated retail-relevant crime signal "
                "relative to nearby benchmark areas."
            )
            include = False

        return {
            "status": "ok",
            "source": "data.police.uk",
            "month": month,
            "total_crimes": subject_counts.total,
            "retail_relevant_count": subject_counts.retail_relevant,
            "retail_relevant_categories": subject_counts.categories,
            "baseline_method": "8-point offset local median benchmark (same month)",
            "relative_ratio": round(ratio, 3) if ratio is not None else None,
            "signal_level": signal_level,
            "included_in_report": include,
            "narrative": narrative,
        }
    except httpx.TimeoutException:
        log.warning("location-signals crime timeout")
        return _crime_na("error")
    except Exception:
        log.warning("location-signals crime error", exc_info=True)
        return _crime_na("error")
    finally:
        if not external_client:
            client.close()


def get_flood_signal(latitude: float, longitude: float, *, client: httpx.Client | None = None) -> dict:
    if not _is_valid_coordinate(latitude, longitude):
        return _flood_na("unavailable")

    external_client = client is not None
    client = client or httpx.Client(timeout=LOCATION_SIGNALS_HTTP_TIMEOUT_SECONDS)
    try:
        params = {
            "lat": f"{latitude:.6f}",
            "long": f"{longitude:.6f}",
            "dist": "5",
        }
        resp = client.get(f"{_FLOOD_BASE}/floods", params=params)
        resp.raise_for_status()
        payload = resp.json() or {}
        items = payload.get("items") or []

        active_alert = False
        active_warning = False
        nearest_area_label = None
        in_flood_area = bool(items)

        min_distance = None
        for item in items:
            if not isinstance(item, dict):
                continue
            severity_level = item.get("severityLevel")
            if severity_level == 3:
                active_alert = True
            if severity_level in {1, 2}:
                active_warning = True
            label = item.get("floodArea", {}).get("description") or item.get("description")
            if nearest_area_label is None and isinstance(label, str):
                nearest_area_label = label
            dval = item.get("distance")
            if isinstance(dval, (float, int)) and (min_distance is None or dval < min_distance):
                min_distance = float(dval)
                nearest_area_label = label if isinstance(label, str) else nearest_area_label

        signal_level = "active" if (active_alert or active_warning) else "none"
        include = signal_level == "active"
        narrative = (
            "Active public flood signalling is present in the surrounding area and may support further review of potential end allowances where operational impact can be evidenced."
            if include
            else "No active public flood signalling was identified."
        )

        log.info(
            "location-signals flood items=%s active_alert=%s active_warning=%s level=%s",
            len(items),
            active_alert,
            active_warning,
            signal_level,
        )
        return {
            "status": "ok",
            "source": "environment-agency",
            "in_flood_area": in_flood_area,
            "active_alert": active_alert,
            "active_warning": active_warning,
            "nearest_area_label": nearest_area_label,
            "signal_level": signal_level,
            "included_in_report": include,
            "narrative": narrative,
        }
    except httpx.TimeoutException:
        log.warning("location-signals flood timeout")
        return _flood_na("error")
    except Exception:
        log.warning("location-signals flood error", exc_info=True)
        return _flood_na("error")
    finally:
        if not external_client:
            client.close()


def _cache_key(latitude: float, longitude: float) -> str:
    # 3dp (~100m) balances cache reuse with local-signal fidelity.
    return f"location-signals:v1:{latitude:.3f}:{longitude:.3f}"


def get_location_signals(
    postcode: str | None,
    latitude: float | None,
    longitude: float | None,
    *,
    cache: LocationSignalCache | None = None,
) -> dict:
    """Fetch directional location signals for evidence-pack support.

    Fail-open by design and never raises.
    """
    if not LOCATION_SIGNALS_ENABLED:
        log.info("location-signals disabled")
        return {
            "available": False,
            "crime": _crime_na("unavailable"),
            "flood": _flood_na("unavailable"),
        }

    if not _is_valid_coordinate(latitude, longitude):
        log.info("location-signals unavailable reason=invalid_coords postcode=%s", postcode)
        return {
            "available": False,
            "crime": _crime_na("unavailable"),
            "flood": _flood_na("unavailable"),
        }

    lat = float(latitude)
    lon = float(longitude)
    key = _cache_key(lat, lon)
    cache_impl = cache or DatabaseLocationSignalCache()

    cached = cache_impl.get(key)
    if cached is not None:
        log.info("location-signals cache_hit key=%s", key)
        return cached

    log.info("location-signals cache_miss key=%s", key)
    try:
        with httpx.Client(timeout=LOCATION_SIGNALS_HTTP_TIMEOUT_SECONDS) as client:
            crime = get_crime_signal(lat, lon, client=client)
            flood = get_flood_signal(lat, lon, client=client)
    except Exception:
        log.warning("location-signals orchestration error", exc_info=True)
        crime = _crime_na("error")
        flood = _flood_na("error")

    payload = {
        "available": True,
        "crime": crime,
        "flood": flood,
    }

    cache_impl.set(key, payload, LOCATION_SIGNALS_CACHE_TTL_SECONDS)
    return payload
