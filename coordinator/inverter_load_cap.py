"""Opt-in SoC latch and AC capacity budget; no forecast or PV estimate."""
from __future__ import annotations

import math


def finite_nonnegative(value):
    """Unknown readings are not zero demand."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def inverter_load_budget(*, was_active, soc, auto_start_soc, buffer_soc,
                         limit_w, total_load_w, managed_draw_w, margin_w=50.0):
    """Return (latched permission, total managed-load allowance).

    The allowance includes already-running managed loads. All other demand
    (including EV commitments) is reserved once. Restart in the dead band
    leaves permission off; unavailable/invalid inputs revoke it immediately.
    """
    values = [finite_nonnegative(v) for v in (
        soc, auto_start_soc, buffer_soc, limit_w, total_load_w,
        managed_draw_w, margin_w)]
    if any(v is None for v in values):
        return False, 0.0
    soc, start, floor, limit, total, managed, margin = values
    if not (floor < start <= 100 and soc <= 100 and limit > 0) or managed > total:
        return False, 0.0
    active = soc > floor and (bool(was_active) or soc >= start)
    other = total - managed
    return active, max(0.0, limit - other - margin)


def inverter_total_load_w(power, ev_committed_w=0.0):
    """House excludes EV in SEM. Reserve the larger of live/commanded EV.

    The house balance is unreliable when any contributing source is dark.
    Export also occupies the inverter's AC output until it falls away.
    """
    if any(bool(getattr(power, flag, False)) for flag in (
        "inputs_degraded", "solar_power_unavailable", "grid_power_unavailable",
        "battery_power_all_unavailable", "ev_power_held",
    )) or bool(getattr(power, "held_w", None)):
        return None
    values = [finite_nonnegative(v) for v in (
        getattr(power, "home_consumption_power", None),
        getattr(power, "ev_power", None), ev_committed_w,
        getattr(power, "grid_export_power", None),
    )]
    if any(v is None for v in values):
        return None
    home, ev, committed, export = values
    return home + max(ev, committed) + export
