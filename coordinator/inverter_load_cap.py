"""Opt-in SoC latch and AC capacity budget; no forecast or PV estimate."""
from __future__ import annotations

import math
from datetime import datetime

START_SETTLE_SECONDS = 60.0


def finite_nonnegative(value):
    """Unknown readings are not zero demand."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def reported_running_power_w(device):
    """A direct running-power reading, including a thermostat's zero.

    An old OFF reading must not erase a new start's reservation. Energy-only
    estimates and unreadable sensors cannot establish an idle thermostat.
    A new report releases the start reservation early. After a minute the
    readable current value is accepted even for sensors that only report
    changes; the aggregate house meter still counts their actual demand.
    The caller keeps the rated/commanded reservation when this returns None.
    """
    from .units import power_state_to_watts

    entity = getattr(device, "power_entity_id", None)
    states = getattr(getattr(device, "hass", None), "states", None)
    if not entity or states is None or not getattr(device, "is_active", False):
        return None
    state = states.get(entity)
    watts = finite_nonnegative(power_state_to_watts(state))
    reported = (getattr(state, "last_reported", None)
                or getattr(state, "last_updated", None))
    started = getattr(device, "_last_activated", None)
    if watts is None or started is None:
        return None
    try:
        # HA reports are timezone-aware; the device's datetime.now() epoch
        # is local. Timestamps compare them without mixing aware/naive dates.
        started_ts = started.timestamp()
        if (reported is not None and reported.tzinfo is not None
                and reported.timestamp() >= started_ts):
            return watts
        if datetime.now().timestamp() - started_ts >= START_SETTLE_SECONDS:
            return watts
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return None


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
