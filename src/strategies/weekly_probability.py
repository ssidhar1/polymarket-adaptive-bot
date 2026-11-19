"""Research-only lognormal probability primitives for weekly crypto markets.

Inputs must be sourced as-of the forecast timestamp. The default zero drift is
an explicit baseline assumption, not a calibrated or risk-neutral forecast.
This module never authorizes a trade by itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import erf, isfinite, log, sqrt


@dataclass(frozen=True)
class LognormalRangeModel:
    model_id: str = "lognormal_zero_drift_baseline_v1"

    @staticmethod
    def _cdf(z: float) -> float:
        return 0.5 * (1.0 + erf(z / sqrt(2.0)))

    def _cdf_price(
        self,
        boundary: float | None,
        *,
        spot: float,
        annualized_volatility: float,
        years: float,
        annualized_drift: float,
    ) -> float:
        if boundary is None:
            return 0.0
        if boundary <= 0:
            return 0.0
        sigma_root_t = annualized_volatility * sqrt(years)
        if sigma_root_t == 0:
            terminal = spot
            return 1.0 if terminal <= boundary else 0.0
        mean_log_return = (annualized_drift - 0.5 * annualized_volatility**2) * years
        z = (log(boundary / spot) - mean_log_return) / sigma_root_t
        return self._cdf(z)

    def probability_between(
        self,
        *,
        spot: float,
        lower: float | None,
        upper: float | None,
        annualized_volatility: float,
        hours_to_expiry: float,
        annualized_drift: float = 0.0,
    ) -> float:
        self._validate(spot, annualized_volatility, hours_to_expiry, annualized_drift)
        if lower is not None and (not isfinite(float(lower)) or lower <= 0):
            raise ValueError("lower boundary must be positive or None")
        if upper is not None and (not isfinite(float(upper)) or upper <= 0):
            raise ValueError("upper boundary must be positive or None")
        if lower is not None and upper is not None and lower >= upper:
            raise ValueError("lower boundary must be below upper boundary")
        if hours_to_expiry == 0:
            return float((lower is None or spot >= lower) and (upper is None or spot < upper))
        years = hours_to_expiry / (365.25 * 24.0)
        low_cdf = self._cdf_price(
            lower, spot=spot, annualized_volatility=annualized_volatility,
            years=years, annualized_drift=annualized_drift,
        )
        high_cdf = 1.0 if upper is None else self._cdf_price(
            upper, spot=spot, annualized_volatility=annualized_volatility,
            years=years, annualized_drift=annualized_drift,
        )
        return max(0.0, min(1.0, high_cdf - low_cdf))

    def probability_above(self, *, threshold: float, **kwargs) -> float:
        return self.probability_between(lower=threshold, upper=None, **kwargs)

    def probability_below(self, *, threshold: float, **kwargs) -> float:
        return self.probability_between(lower=None, upper=threshold, **kwargs)

    @staticmethod
    def _validate(
        spot: float,
        annualized_volatility: float,
        hours_to_expiry: float,
        annualized_drift: float,
    ) -> None:
        values = (spot, annualized_volatility, hours_to_expiry, annualized_drift)
        if any(not isinstance(v, (int, float)) or not isfinite(float(v)) for v in values):
            raise ValueError("model inputs must be finite numbers")
        if spot <= 0 or annualized_volatility < 0 or hours_to_expiry < 0:
            raise ValueError("spot must be positive; volatility and horizon non-negative")
