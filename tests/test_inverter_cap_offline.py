"""Run with py -B tests/test_inverter_cap_offline.py (no HA/pytest needed).

The real controller module, complete update, intent and reconciliation paths
run against fake device I/O. HA-only imports are replaced, not the policy.
This does not test Home Assistant lifecycle or physical inverter responses.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import json
import sys
import types
import unittest
from datetime import datetime
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "_sem_inverter_test"


def module(name, **values):
    result = types.ModuleType(name)
    result.__dict__.update(values)
    sys.modules[name] = result
    return result


for suffix in ("", ".coordinator", ".devices", ".utils", ".consts"):
    package = module(PREFIX + suffix)
    package.__path__ = [str(ROOT / suffix.lstrip("."))]


class Mode(Enum):
    OFF = "off"
    PEAK_ONLY = "peak_only"
    SURPLUS = "surplus"


class State(Enum):
    IDLE = "idle"
    ACTIVE = "active"


class Kind(Enum):
    SWITCH = "switch"
    CLIMATE = "climate"


class Peak:
    NORMAL = "normal"
    WARNING = "warning"
    SHEDDING = "shedding"
    EMERGENCY = "emergency"


module(PREFIX + ".const", LoadManagementState=Peak)
module(PREFIX + ".devices.base", ControllableDevice=object,
       DeviceState=State, DeviceControlMode=Mode, DeviceType=Kind, ScheduleDevice=type("ScheduleDevice", (), {}))
module(PREFIX + ".utils.log_gate", log_on_change=lambda *a, **kw: None)
module(PREFIX + ".coordinator.device_tariff", device_price_level=lambda d, p: p,
       DeviceTariffs=lambda h: types.SimpleNamespace(stamp=lambda d: None))
module(PREFIX + ".coordinator.device_reconciler", reconcile_all=lambda d: None)
module(PREFIX + ".coordinator.price_signal", is_cheap_name=lambda p: p == "cheap",
       is_expensive_name=lambda p: p == "expensive")
module(PREFIX + ".coordinator.decision_log", DecisionLog=lambda h: None,
       record_load=lambda *a, **kw: None)
module(PREFIX + ".coordinator.shed_signal", INERT=None)


class StripHA(ast.NodeTransformer):
    def visit_ImportFrom(self, node):
        return None if (node.module or "").startswith("homeassistant") else node


tree = StripHA().visit(ast.parse((ROOT / "coordinator/surplus_controller.py").read_text(encoding="utf-8")))
sc = module(PREFIX + ".coordinator.surplus_controller", HomeAssistant=object,
            dt_util=types.SimpleNamespace(now=datetime.now))
sc.__package__ = PREFIX + ".coordinator"
exec(compile(ast.fix_missing_locations(tree), "surplus_controller.py", "exec"), sc.__dict__)
cap = importlib.import_module(PREFIX + ".coordinator.inverter_load_cap")


class Device:
    def __init__(self, name, watts, priority=1, active=False, **kw):
        self.device_id = self.name = name
        self.rated_power = self.min_power_threshold = watts
        self.priority = priority
        self.is_active = active
        self.is_enabled = True
        self.managed_externally = False
        self.control_mode = Mode.SURPLUS
        self.device_type = Kind.SWITCH
        self.battery_assist_enabled = True
        self.daily_targets_met = True
        self.daily_max_runtime_reached = self.stop_condition_met = False
        self._offpeak_forced = self._batt_overnight_forced = False
        self._offpeak_forced_date = self._batt_overnight_forced_date = None
        self._sem_owned = self._sem_commanded = active
        self.has_runtime_deficit = True
        self.needs_offpeak_activation = False
        self.battery_eligible_overnight = True
        self.top_up_policy = "cheap_hours"
        self.remaining_daily_runtime_sec = 3600
        self._daily_runtime_meter_day = datetime.now().date()
        self.start_reserve_w = 0
        self.status = types.SimpleNamespace(allocated_power_w=watts if active else 0,
                                            state=State.ACTIVE if active else State.IDLE)
        self.can_stop = self.can_start = True
        self.calls = []
        self.__dict__.update(kw)

    def get_current_consumption(self):
        return self.rated_power if self.is_active else 0

    def can_activate(self):
        return self.can_start and not self.daily_max_runtime_reached and not self.stop_condition_met

    def can_deactivate(self):
        return self.can_stop

    async def activate(self, watts):
        self.calls.append("start")
        self.is_active = True
        self.status.state = State.ACTIVE
        self.status.allocated_power_w = self.rated_power
        return self.rated_power

    async def adjust_power(self, watts):
        self.calls.append("adjust")
        return self.rated_power

    async def deactivate(self):
        self.calls.append("stop")
        self.is_active = False
        self.status.state = State.IDLE
        self.status.allocated_power_w = 0

    def record_activated(self):
        self._sem_owned = self._sem_commanded = True

    def record_deactivated(self):
        self._sem_owned = self._sem_commanded = False

    def reset_surplus_timer(self):
        pass


class BudgetTests(unittest.TestCase):
    def budget(self, **overrides):
        args = dict(was_active=False, soc=95, auto_start_soc=95, buffer_soc=90,
                    limit_w=12000, total_load_w=2000, managed_draw_w=0, margin_w=50)
        args.update(overrides)
        return cap.inverter_load_budget(**args)

    def test_latch_boundaries_and_restart(self):
        self.assertTrue(self.budget()[0])
        self.assertTrue(self.budget(was_active=True, soc=92)[0])
        self.assertFalse(self.budget(soc=92)[0])
        self.assertFalse(self.budget(was_active=True, soc=90)[0])
        self.assertFalse(self.budget(was_active=True, soc=89)[0])

    def test_one_envelope_not_one_per_load(self):
        self.assertEqual(self.budget()[1], 9950)
        self.assertEqual(self.budget(total_load_w=8000, managed_draw_w=6000)[1], 9950)
        self.assertEqual(self.budget(total_load_w=14000)[1], 0)

    def test_bad_inputs_fail_closed(self):
        for key in ("soc", "auto_start_soc", "buffer_soc", "limit_w", "total_load_w", "managed_draw_w"):
            for bad in (None, "unavailable", float("nan"), float("inf"), -1):
                with self.subTest(key=key, bad=bad):
                    self.assertEqual(self.budget(**{key: bad}), (False, 0))
        for change in ({"limit_w": 0}, {"buffer_soc": 95}, {"auto_start_soc": 101},
                       {"soc": 101}, {"managed_draw_w": 3000}):
            self.assertEqual(self.budget(**change), (False, 0))

    def test_ev_reserves_pending_command_and_existing_draw(self):
        p = types.SimpleNamespace(home_consumption_power=2000, ev_power=1000, grid_export_power=0)
        self.assertEqual(cap.inverter_total_load_w(p, 7000), 9000)
        self.assertEqual(cap.inverter_total_load_w(p, 0), 3000)
        p.grid_export_power = 400
        self.assertEqual(cap.inverter_total_load_w(p, 0), 3400)
        p.inputs_degraded = True
        self.assertIsNone(cap.inverter_total_load_w(p, 0))


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, *devices):
        c = sc.SurplusController(types.SimpleNamespace())
        c._devices = {d.device_id: d for d in devices}
        return c

    async def tick(self, c, **overrides):
        managed = sum(d.get_current_consumption() for d in c._devices.values())
        args = dict(available_power_w=0, price_level="cheap", battery_soc=100,
                    battery_buffer_soc=90, battery_reserve_soc=30,
                    battery_assist_budget_w=8000, inverter_cap_w=12000,
                    inverter_total_load_w=2000 + managed,
                    inverter_auto_start_soc=95, inverter_buffer_soc=90,
                    is_night=True)
        args.update(overrides)
        return await c.update(**args)

    async def test_full_battery_zero_solar_and_met_minimum(self):
        a, b, extra = Device("pump", 3000), Device("heater", 6000, 2), Device("extra", 2000, 3)
        c = self.controller(a, b, extra)
        result = await self.tick(c)
        self.assertEqual([d.is_active for d in (a, b, extra)], [True, True, False])
        self.assertEqual(result.total_surplus_w, 0)  # no forged solar reading
        self.assertEqual(result.unallocated_w, 0)   # no false surplus event
        self.assertEqual(result.distributable_surplus_w, 9950)
        await self.tick(c, battery_soc=92)
        self.assertTrue(a.is_active and b.is_active)

    async def test_buffer_reverts_to_solar_without_smoothing_or_topups(self):
        a, b = Device("pump", 3000), Device("heater", 6000, 2)
        c = self.controller(a, b)
        await self.tick(c)
        await self.tick(c, battery_soc=90, available_power_w=3500)
        self.assertTrue(a.is_active)
        self.assertFalse(b.is_active)
        await self.tick(c, battery_soc=93, available_power_w=0)
        self.assertFalse(a.is_active or b.is_active)
        await self.tick(c, battery_soc=95)
        self.assertTrue(a.is_active and b.is_active)

    async def test_house_spike_sheds_low_priority_first(self):
        a, b = Device("first", 3000), Device("second", 6000, 2)
        c = self.controller(a, b)
        await self.tick(c)
        await self.tick(c, inverter_total_load_w=14000)
        self.assertTrue(a.is_active)
        self.assertFalse(b.is_active)

    async def test_pending_stop_cannot_fund_a_new_start(self):
        a, b = Device("first", 6000), Device("held", 6000, 2, active=True, can_stop=False)
        c = self.controller(a, b)
        await self.tick(c)
        self.assertFalse(a.is_active)
        self.assertTrue(b.is_active)
        b.can_stop = True
        await self.tick(c)
        self.assertTrue(a.is_active)
        self.assertFalse(b.is_active)

    async def test_minimum_run_reservation_keeps_priority_for_other_loads(self):
        high = Device("high", 2600, 1)
        idle_heater = Device("thermostat_idle", 3238, 2, active=True)
        pump = Device("minimum_run", 1084, 3, active=True, can_stop=False)
        idle_heater.get_current_consumption = lambda: 0
        c = self.controller(high, idle_heater, pump)
        result = await self.tick(c, inverter_total_load_w=7019)
        self.assertEqual(result.distributable_surplus_w, 6015)
        self.assertTrue(high.is_active, "lower-priority minimum run must not starve the first load")
        self.assertFalse(idle_heater.is_active, "release its restart reservation before starting the first load")
        self.assertTrue(pump.is_active, "preserve minimum-on time")
        self.assertEqual(idle_heater.calls, ["stop"])

    async def test_minimum_run_reservation_is_counted_once(self):
        held = Device("held", 3000, 1, active=True, can_stop=False)
        extra = Device("extra", 6000, 2)
        c = self.controller(held, extra)
        await self.tick(c)
        self.assertTrue(held.is_active and extra.is_active)

    async def test_no_extra_allowance_from_reclaim_or_assist(self):
        a = Device("too_large", 11000)
        c = self.controller(a)
        await self.tick(c, reclaim_w=15000, battery_assist_budget_w=20000, available_power_w=20000)
        self.assertFalse(a.is_active)

    def test_capacity_rejection_does_not_blame_solar(self):
        d = Device("load", 3000)
        intent = sc.compute_load_intent(d, remaining_surplus_w=2000, bounded_source="inverter_cap")
        self.assertEqual(intent.code, sc.RC.LOAD_CAPACITY_SHORT)
        self.assertIn("2000W left; needs 3000W", intent.reason)
        solar = sc.compute_load_intent(d, remaining_surplus_w=2000, bounded_source="solar")
        self.assertEqual(solar.code, sc.RC.LOAD_SURPLUS_SHORT)

    async def test_unknown_soc_or_meter_revokes_permission(self):
        for overrides in ({"battery_soc": None}, {"inverter_total_load_w": None}, {"inverter_cap_w": 0}):
            a = Device("pump", 3000)
            c = self.controller(a)
            await self.tick(c)
            await self.tick(c, **overrides)
            self.assertFalse(a.is_active)
            self.assertFalse(c._inverter_cap_active)

    async def test_goals_user_modes_phase_and_peak_still_apply(self):
        for settings in ({"daily_max_runtime_reached": True}, {"stop_condition_met": True},
                         {"control_mode": Mode.OFF}, {"control_mode": Mode.PEAK_ONLY},
                         {"_phase_hold": "L1"}, {"can_start": False}, {"locked_by_operator": True}):
            d = Device("load", 1000, **settings)
            await self.tick(self.controller(d))
            self.assertFalse(d.is_active, settings)
        d = Device("load", 1000)
        c = self.controller(d)
        await self.tick(c, peak_state=Peak.WARNING)
        self.assertFalse(d.is_active)
        await self.tick(c)
        await self.tick(c, peak_state=Peak.EMERGENCY)
        self.assertFalse(d.is_active)

    async def test_observer_never_operates_devices(self):
        d = Device("load", 3000)
        c = self.controller(d)
        await self.tick(c, observer=True)
        self.assertFalse(d.is_active)
        self.assertEqual(d.calls, [])

    async def test_disabled_keeps_stock_minimum_runtime_gate(self):
        d = Device("load", 3000, has_runtime_deficit=False)
        c = self.controller(d)
        # Exercise stock declarative path as well as the enabled policy path.
        c._use_desired_state = True
        await self.tick(c, inverter_cap_w=None)
        self.assertFalse(d.is_active)
        self.assertIsNone(c._inverter_policy_source)

    async def test_deadline_and_cheap_grid_cannot_bypass_envelope(self):
        d = Device("deadline", 11000, is_deadline_approaching=True, daily_targets_met=False)
        c = self.controller(d)
        await self.tick(c)
        self.assertFalse(d.is_active)

    async def test_disabled_imperative_path_still_uses_solar(self):
        d = Device("load", 3000, has_runtime_deficit=False)
        c = self.controller(d)
        await self.tick(c, inverter_cap_w=None, price_level="normal", is_night=False)
        self.assertFalse(d.is_active)
        await self.tick(c, inverter_cap_w=None, available_power_w=15000,
                        price_level="normal", is_night=False)
        self.assertTrue(d.is_active)

    async def test_no_start_on_restart_in_the_soc_band(self):
        d = Device("load", 3000)
        c = self.controller(d)
        await self.tick(c, battery_soc=92)
        self.assertFalse(d.is_active)

    async def test_higher_dynamic_floor_does_not_change_explicit_buffer(self):
        d = Device("load", 3000)
        c = self.controller(d)
        await self.tick(c, battery_buffer_soc=99)
        await self.tick(c, battery_soc=92, battery_buffer_soc=99)
        self.assertTrue(d.is_active)
        await self.tick(c, battery_soc=90, battery_buffer_soc=99)
        self.assertFalse(d.is_active)

    async def test_unmanaged_and_equipment_owned_draw_is_not_recredited(self):
        off = Device("manual", 4000, 1, active=True, control_mode=Mode.OFF,
                     _sem_owned=False, _sem_commanded=False)
        own = Device("own", 3000, 2, active=True, runs_without_surplus=True)
        extra = Device("extra", 4000, 3)
        c = self.controller(off, own, extra)
        await self.tick(c)
        self.assertTrue(off.is_active and own.is_active)
        self.assertFalse(extra.is_active)

    async def test_no_start_when_ev_uses_the_remaining_capacity(self):
        d = Device("load", 3000)
        c = self.controller(d)
        await self.tick(c, inverter_total_load_w=10000)
        self.assertFalse(d.is_active)

    async def test_buffer_stop_clears_old_paid_markers(self):
        d = Device("load", 3000, active=True, _offpeak_forced=True,
                   _batt_overnight_forced=True, daily_targets_met=False)
        c = self.controller(d)
        await self.tick(c, battery_soc=90)
        self.assertFalse(d.is_active)
        self.assertFalse(d._offpeak_forced or d._batt_overnight_forced)


class WiringTests(unittest.TestCase):
    def test_option_and_english_labels(self):
        for path in ("strings.json", "translations/en.json"):
            text = (ROOT / path).read_text(encoding="utf-8")
            json.loads(text)
            self.assertIn('"inverter_cap_loads_enabled"', text)
        flow = (ROOT / "config_flow.py").read_text(encoding="utf-8")
        self.assertIn('default=_c("inverter_cap_loads_enabled", False)', flow)


if __name__ == "__main__":
    unittest.main()
