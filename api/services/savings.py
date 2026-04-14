from __future__ import annotations

from datetime import date, datetime

_ESTIMATED_RATES_MULTIPLIER = 0.49
_CYCLE_SWITCH_DATE = date(2026, 7, 31)
_SAVING_MARGIN = 0.05


def estimate_annual_rates_payable(rateable_value: float | int | None) -> float | None:
    """Estimate annual rates payable from an RV using a shared multiplier."""
    if rateable_value is None:
        return None
    rv = float(rateable_value)
    if rv <= 0:
        return 0.0
    return rv * _ESTIMATED_RATES_MULTIPLIER


def get_years_remaining_in_cycle(*, as_of: date | datetime | None = None) -> int:
    """Return years left in the current cycle under the fixed business rule."""
    if as_of is None:
        as_of_date = datetime.utcnow().date()
    elif isinstance(as_of, datetime):
        as_of_date = as_of.date()
    else:
        as_of_date = as_of
    return 3 if as_of_date < _CYCLE_SWITCH_DATE else 2


def _non_negative(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, value)


def build_downside_rv_range(
    modelled_rv_point: float | int | None,
    *,
    margin: float = _SAVING_MARGIN,
) -> tuple[int | None, int | None]:
    """Build a downside-only low/high RV range around a point estimate.

    The low value applies the configured downside margin; the high value is
    capped at the rounded point estimate.
    """
    if modelled_rv_point is None:
        return None, None
    low = round(float(modelled_rv_point) * (1 - margin) / 100) * 100
    high = round(float(modelled_rv_point) / 100) * 100
    return int(low), int(high)


def calculate_implied_savings(
    *,
    current_rv: float | int | None,
    modelled_rv_point: float | int | None,
    modelled_rv_low: float | int | None,
    modelled_rv_high: float | int | None,
    as_of: date | datetime | None = None,
) -> dict[str, float | int | None]:
    """Calculate implied cash savings from RVs via estimated annual rates payable."""
    current_annual = estimate_annual_rates_payable(current_rv)
    point_annual = estimate_annual_rates_payable(modelled_rv_point)
    low_bound_annual = estimate_annual_rates_payable(modelled_rv_high)
    high_bound_annual = estimate_annual_rates_payable(modelled_rv_low)

    annual_point = _non_negative(None if current_annual is None or point_annual is None else current_annual - point_annual)
    annual_low = _non_negative(None if current_annual is None or low_bound_annual is None else current_annual - low_bound_annual)
    annual_high = _non_negative(None if current_annual is None or high_bound_annual is None else current_annual - high_bound_annual)

    years_remaining = get_years_remaining_in_cycle(as_of=as_of)

    def _total(annual: float | None) -> float | None:
        return None if annual is None else annual * years_remaining

    return {
        "implied_annual_saving_point": None if annual_point is None else round(annual_point),
        "implied_annual_saving_low": None if annual_low is None else round(annual_low),
        "implied_annual_saving_high": None if annual_high is None else round(annual_high),
        "implied_total_saving_point": None if annual_point is None else round(_total(annual_point)),
        "implied_total_saving_low": None if annual_low is None else round(_total(annual_low)),
        "implied_total_saving_high": None if annual_high is None else round(_total(annual_high)),
        "years_remaining_in_cycle": years_remaining,
    }
