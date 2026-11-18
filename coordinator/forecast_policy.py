"""Immutable forecast options for one coordinator lifetime.

Options changes reload the integration. The old lifetime is drained before
replacement; neither learner may observe half of a new configuration.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..consts.core import (
    CONF_DISABLE_ADAPTIVE_FORECAST,
    CONF_DISABLE_CURTAILMENT_CHAIN,
    DEFAULT_DISABLE_ADAPTIVE_FORECAST,
    DEFAULT_DISABLE_CURTAILMENT_CHAIN,
)

DISABLED_FORECAST_SENSORS = frozenset({
    "pv_performance_vs_forecast", "pv_daily_performance_ratio", "pv_health",
    "forecast_trust_d1", "forecast_trust_d2",
})


@dataclass(frozen=True)
class ForecastPolicy:
    disable_curtailment_chain: bool = DEFAULT_DISABLE_CURTAILMENT_CHAIN
    disable_adaptive_forecast: bool = DEFAULT_DISABLE_ADAPTIVE_FORECAST

    @classmethod
    def from_config(cls, config) -> ForecastPolicy:
        snapshot = dict(config)
        return cls(
            bool(snapshot.get(CONF_DISABLE_CURTAILMENT_CHAIN,
                              DEFAULT_DISABLE_CURTAILMENT_CHAIN)),
            bool(snapshot.get(CONF_DISABLE_ADAPTIVE_FORECAST,
                              DEFAULT_DISABLE_ADAPTIVE_FORECAST)),
        )


def forecast_policy(owner) -> ForecastPolicy:
    """Use the resolved lifetime snapshot (defaults for lightweight callers)."""
    policy = getattr(owner, "_forecast_policy", None)
    if isinstance(policy, ForecastPolicy):
        return policy
    return ForecastPolicy.from_config(getattr(owner, "config", {}) or {})


def publish_disabled_forecast(data, tracker, raw_today) -> None:
    """Enforce absence at the final publisher, including failed analytics cycles.

    This also clears ratio attributes: a disabled sensor must not retain the
    previous cycle's apparent measurement. Counts are real zeros, not yields.
    """
    data.update(tracker.get_data())
    data.update({
        "forecast_adaptive_source": "disabled",
        "forecast_corrected_today": raw_today,
        "forecast_trust_d1": None,
        "forecast_trust_d2": None,
        "forecast_trust_path": "disabled",
        "pv_performance_vs_forecast": None,
        "pv_daily_performance_ratio": None,
        "pv_performance_path": "disabled",
        "pv_health": None,
        "pv_health_attrs": {
            "source": "disabled", "ratio_7d": None, "settled_days": 0,
            "days_since_full_yield": None, "snow": None,
            "downtime_min_today": (data.get("pv_health_attrs") or {}).get(
                "downtime_min_today"),
        },
    })
