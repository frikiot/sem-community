"""Offline acceptance: run `py -B tests/test_fork_disable_adaptive_offline.py`.

No pytest/HA dependency. Pure modules run intact; HA imports in the tracker
and analyzer are replaced by a controlled clock. Coordinator, service and
sensor methods are compiled from their real AST, with unrelated orchestration
omitted. This exercises the publication/persistence seams, not a full HA boot.
All test packages have a private name; no real HA modules are replaced.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import importlib
import json
import logging
import re
import sys
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "_sem_fork_offline"
NOW = datetime(2026, 11, 13, 16, 47, tzinfo=timezone(timedelta(hours=2)))
CLOCK = types.SimpleNamespace(
    now=lambda: NOW, utcnow=lambda: NOW, DEFAULT_TIME_ZONE=NOW.tzinfo)


def package(name, path):
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module
    return module


for suffix in ("", ".coordinator", ".analytics", ".consts"):
    package(PREFIX + suffix, ROOT / suffix.lstrip("."))


def source(path):
    return ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=path)


def compile_module(name, path, nodes=None, **bindings):
    tree = source(path)
    if nodes is not None:
        tree.body = nodes
    else:
        tree.body = [n for n in tree.body if not (
            isinstance(n, ast.ImportFrom) and (n.module or "").startswith("homeassistant"))]
    tree.body = [n for n in tree.body if not (
        isinstance(n, ast.ImportFrom) and n.module == "__future__")]
    tree.body.insert(0, ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0))
    module = types.ModuleType(PREFIX + "." + name)
    module.__package__ = module.__name__.rpartition(".")[0]
    module.__dict__.update(bindings)
    sys.modules[module.__name__] = module
    exec(compile(ast.fix_missing_locations(tree), str(ROOT / path), "exec"), module.__dict__)
    return module


core = importlib.import_module(PREFIX + ".consts.core")
policy = importlib.import_module(PREFIX + ".coordinator.forecast_policy")
ledger_mod = importlib.import_module(PREFIX + ".coordinator.forecast_ledger")
types_mod = importlib.import_module(PREFIX + ".coordinator.types")
health_mod = importlib.import_module(PREFIX + ".analytics.pv_health")
probe_mod = importlib.import_module(PREFIX + ".coordinator.curtailment")
tracker_mod = compile_module("coordinator.forecast_tracker", "coordinator/forecast_tracker.py",
                             dt_util=CLOCK, STATE_UNKNOWN="unknown", STATE_UNAVAILABLE="unavailable")
pv_mod = compile_module("analytics.pv_performance", "analytics/pv_performance.py", dt_util=CLOCK)


def class_node(path, name):
    return next(n for n in source(path).body if isinstance(n, ast.ClassDef) and n.name == name)


coord_class = class_node("coordinator/coordinator.py", "SEMCoordinator")
methods = {n.name: n for n in coord_class.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
ctor = copy.deepcopy(methods["__init__"])
ctor_names = {"hass", "config", "_forecast_policy", "_forecast_lock", "_forecast_retired",
              "_forecast_tracker", "_forecast_ledger"}
ctor.body = [n for n in ctor.body if isinstance(n, ast.Assign) and any(
    isinstance(t, ast.Attribute) and t.attr in ctor_names for t in n.targets)]
# The exact production forecast phase, before tariff/load/EV analytics begin.
analytics = copy.deepcopy(methods["_update_analytics_phases"])
cut = next(i for i, n in enumerate(analytics.body) if isinstance(n, ast.Assign)
           and any(isinstance(t, ast.Name) and t.id == "tariff_data" for t in n.targets))
analytics.body = analytics.body[:cut] + [ast.Return(value=ast.Tuple(elts=[
    ast.Name(id="forecast_data", ctx=ast.Load()), ast.Name(id="tracker_data", ctx=ast.Load())], ctx=ast.Load()))]
selected = [ctor, analytics] + [methods[n] for n in (
    "async_retire_forecast", "_async_update_data", "_restore_adaptive_forecast_state",
    "_persist_adaptive_forecast_state", "_record_forecast_horizons",
    "_curtailment_grant_w", "_pv_health_verdict")]
minimal_class = ast.ClassDef(name="Coordinator", bases=[], keywords=[], body=selected, decorator_list=[])
helpers = [n for n in source("coordinator/coordinator.py").body
           if isinstance(n, ast.FunctionDef) and n.name == "_f_or_none"]
coord_mod = compile_module("coordinator.harness", "coordinator/coordinator.py",
    nodes=helpers + [minimal_class], asyncio=asyncio, time=time, dt_util=CLOCK,
    _LOGGER=logging.getLogger("fork-test"), STATE_UNKNOWN="unknown",
    ForecastPolicy=policy.ForecastPolicy, forecast_policy=policy.forecast_policy,
    publish_disabled_forecast=policy.publish_disabled_forecast,
    ForecastTracker=tracker_mod.ForecastTracker, ForecastLedger=ledger_mod.ForecastLedger,
    ForecastSensorData=types_mod.ForecastSensorData,
    intraday_forecast_on=lambda owner: owner.config.get("intraday_forecast", False))
Coordinator = coord_mod.Coordinator

# Storage accessors and its real lifetime-ending save behavior.
storage_class = class_node("coordinator/storage.py", "SEMStorage")
storage_methods = [n for n in storage_class.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name in {"get_forecast_ledger_state", "set_forecast_ledger_state",
                       "export_forecast_tracker_state", "import_forecast_tracker_state",
                       "async_close", "_async_write_all", "_ended", "async_save_energy_now"}]
storage_mod = compile_module("coordinator.storage_harness", "coordinator/storage.py", nodes=[
    ast.ClassDef(name="Storage", bases=[], keywords=[], body=storage_methods, decorator_list=[])],
    dt_util=CLOCK, logging=logging, _LOGGER=logging.getLogger("fork-test"))

# Actual option listener and nested service functions, lifted with their bodies intact.
init_tree = source("__init__.py")
option_fn = next(n for n in init_tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_update_options")
register = next(n for n in init_tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_async_register_services")
service_fns = [n for n in register.body if isinstance(n, ast.AsyncFunctionDef)
               and n.name in {"async_backfill_forecast_ledger_service", "_async_backfill_forecast_ledger_unlocked"}]
init_mod = compile_module("entry_harness", "__init__.py", nodes=[option_fn] + service_fns,
    dt_util=CLOCK, _LOGGER=logging.getLogger("fork-test"), _SKIP_RELOAD_SNAPSHOT_TTL_S=60)

# Sensor publication including last-good-value hold and actual attribute branches.
sensor_class = class_node("sensor.py", "SEMSolarSensor")
sensor_methods = [n for n in sensor_class.body if isinstance(n, ast.FunctionDef)
                  and n.name in {"_update_from_coordinator", "native_value", "available",
                                 "extra_state_attributes", "_extra_state_attributes_base"}]
sensor_mod = compile_module("sensor_harness", "sensor.py", nodes=[
    ast.ClassDef(name="SEMSolarSensor", bases=[], keywords=[], body=sensor_methods, decorator_list=[])],
    _LOGGER=logging.getLogger("fork-test"), datetime=datetime,
    SensorDeviceClass=types.SimpleNamespace(TIMESTAMP="timestamp", ENUM="enum", POWER="power",
                                            ENERGY="energy", MONETARY="monetary", BATTERY="battery"),
    SENSOR_DARK_READ_GRACE_S=120, _HELD_TEXT_KEY=re.compile(r"battery_status"),
    _decision_attributes=lambda *a: {}, fit_state_attributes=lambda attrs, _: attrs)


def storage():
    s = storage_mod.Storage()
    s._energy_data = {"forecast_ledger": {"max_days": 120, "days": {
        f"2026-11-{d:02d}": {"actual": 43.43, "f": {"0": 132.8589, "1": 132.8589}}
        for d in range(1, 13)}}, "grid_export": 0.14, "ev_sessions": {"untouched": 1}}
    s._daily_data = {"forecast_tracker": {"history": [
        {"date": f"2026-11-{d:02d}", "forecast": 132.8589, "actual": 43.43,
         "factor": 0.327, "accuracy": 32.7, "weather": "unknown"} for d in range(1, 13)],
        "correction_factor": 0.7}, "daily_solar": 54.17, "grid_import": 0.04}
    s._loaded = True
    s._closed = False
    s._shutdown_listener = Mock()
    s._energy_store = types.SimpleNamespace(async_save=AsyncMock())
    s._daily_store = types.SimpleNamespace(async_save=AsyncMock())
    return s


def owner(chain=False, adaptive=False, *, store=None, intraday=False):
    config = {core.CONF_DISABLE_CURTAILMENT_CHAIN: chain,
              core.CONF_DISABLE_ADAPTIVE_FORECAST: adaptive, "intraday_forecast": intraday,
              "curtailment_probe_enabled": True, "battery_capacity_kwh": 25}
    obj = Coordinator(types.SimpleNamespace(states=types.SimpleNamespace(async_all=lambda _: [])), config)
    obj._storage = store if store is not None else storage()
    obj.data = {}
    obj.last_update_success = True
    obj._tracker_date = NOW.date()
    obj.battery_capacity_kwh = 25
    obj._predictor = types.SimpleNamespace(predict_consumption_today_kwh=lambda _: 10)
    obj._forecast_reader = types.SimpleNamespace(get_charging_recommendation=lambda *a: "solar")
    obj._cycle_forecast = types.SimpleNamespace(
        available=True, forecast_today_kwh=132.8589, forecast_tomorrow_kwh=128.29,
        forecast_d2_kwh=120, forecast_d2_path="read", forecast_remaining_today_kwh=38.14,
        power_now_w=7557, power_next_hour_w=6000, peak_power_today_w=15945,
        peak_time_today="12:30", source="solcast")
    obj._record_source_ledgers = Mock()
    obj._estimate_best_surplus_window = Mock(return_value="afternoon")
    obj._outdoor_temperature_or_none = lambda: 20
    obj.async_request_refresh = AsyncMock()
    return obj


POWER = types_mod.PowerReadings(solar_power=2417, battery_soc=100)
ENERGY = types_mod.EnergyTotals(daily_solar=54.17, daily_home=40.19, monthly_solar=500)


async def cycle(obj):
    async def unlocked():
        fc, track = await obj._update_analytics_phases(POWER, ENERGY, None, None, 0)
        pv = pv_mod.PVPerformanceAnalyzer(None, system_size_kwp=16).update(
            ENERGY.daily_solar, ENERGY.monthly_solar, POWER.solar_power, fc.forecast_today_kwh,
            disable_adaptive_forecast=policy.forecast_policy(obj).disable_adaptive_forecast)
        health = obj._pv_health_verdict(POWER, fc)
        obj._persist_adaptive_forecast_state()
        return {"forecast_today_kwh": fc.forecast_today_kwh, **track, **pv.to_dict(),
                "pv_health": health.state, "pv_health_attrs": health.as_attributes(),
                "forecast_surplus_kwh": fc.forecast_surplus_kwh,
                "forecast_trust_d1": obj._planning_evidence.get("forecast_trust_d1"),
                "forecast_trust_d2": obj._planning_evidence.get("forecast_trust_d2")}
    obj._async_update_data_unlocked = unlocked
    obj.data = await obj._async_update_data()
    return obj.data


class Acceptance(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        global NOW
        NOW = datetime(2026, 11, 13, 16, 47, tzinfo=timezone(timedelta(hours=2)))

    def assert_disabled(self, obj):
        data = obj.data
        self.assertEqual(data["forecast_dampening_factor"], 1.0)
        self.assertEqual(data["forecast_correction_factor"], 1.0)
        self.assertEqual(data["forecast_corrected_factor"], 1.0)
        self.assertEqual(data["forecast_correction_path"], "disabled")
        self.assertEqual(data["forecast_dampening_path"], "disabled")
        self.assertEqual(data["forecast_corrected_today"], data["forecast_today_kwh"])
        for key in policy.DISABLED_FORECAST_SENSORS:
            self.assertIsNone(data[key], key)
            sensor = sensor_mod.SEMSolarSensor()
            sensor.coordinator = obj
            sensor.entity_description = types.SimpleNamespace(
                key=key, device_class="enum" if key == "pv_health" else None,
                translation_key="", translation_placeholders={})
            sensor._last_good_value = 40.74 if key != "pv_health" else "orange"
            sensor._last_good_at = 99
            sensor._now_monotonic = lambda: 100
            self.assertIsNone(sensor.native_value, key)
            if key != "pv_health":
                self.assertFalse(sensor.available, key)
            self.assertIsNone(sensor._last_good_value)
            self.assertEqual(sensor.extra_state_attributes["source"], "disabled")
        for key in ("forecast_dampening_live_ratio", "forecast_dampening_normalized_ratio",
                    "forecast_dampening_smoothed_ratio", "forecast_dampening_confidence"):
            self.assertIsNone(data[key])
        self.assertIsNone(data["pv_health_attrs"]["ratio_7d"])
        self.assertEqual(data["pv_health_attrs"]["source"], "disabled")
        self.assertEqual(obj._forecast_tracker.get_state()["history"], [])
        self.assertEqual(obj._forecast_ledger.to_dict()["days"], {})

    async def test_published_contract_setup_restore_and_rollover(self):
        global NOW
        for chain in (False, True):
            for intraday in (False, True):
                with self.subTest(chain=chain, intraday=intraday):
                    obj = owner(chain, True, intraday=intraday)
                    before = copy.deepcopy((obj._storage._energy_data, obj._storage._daily_data))
                    # Setup with disabled learners, then attempted poisoned restore.
                    await cycle(obj)
                    self.assert_disabled(obj)
                    obj._restore_adaptive_forecast_state()
                    obj._forecast_tracker.restore_state(obj._storage.export_forecast_tracker_state())
                    await cycle(obj)
                    self.assert_disabled(obj)
                    self.assertAlmostEqual(obj.data["forecast_surplus_kwh"], 28.14)
                    self.assertEqual(obj._estimate_best_surplus_window.call_args.args[-1], 38.14)
                    # Preserve guard independence even when adaptive outputs are disabled.
                    obj._curtailment_day = NOW.date()
                    NOW += timedelta(days=1)
                    await cycle(obj)
                    self.assert_disabled(obj)
                    self.assertEqual(before, (obj._storage._energy_data, obj._storage._daily_data))
                    self.assertEqual(obj._record_source_ledgers.call_count, 3)

    async def test_all_four_flag_combinations_and_allowed_poisoning(self):
        global NOW
        for chain in (False, True):
            for adaptive in (False, True):
                with self.subTest(chain=chain, adaptive=adaptive):
                    obj = owner(chain, adaptive)
                    obj._restore_adaptive_forecast_state()
                    obj._forecast_tracker.update(132.8589, 43.43)
                    obj._curtailment_day = NOW.date()
                    await cycle(obj)
                    expected = 54.17 if chain else 43.43
                    self.assertEqual(obj._forecast_tracker._today_actual, expected)
                    if adaptive:
                        self.assert_disabled(obj)
                    else:
                        self.assertEqual(obj.data["forecast_dampening_factor"], 0.5)
                        self.assertEqual(obj.data["forecast_correction_factor"], 0.7)
                        self.assertEqual(obj.data["pv_health"], "red")
                        self.assertIsNotNone(obj.data["pv_performance_vs_forecast"])
                        self.assertEqual(obj._forecast_ledger.actual_for(str(NOW.date())), 54.17)
                    NOW += timedelta(days=1)
                    await cycle(obj)
                    if adaptive:
                        self.assert_disabled(obj)
                    else:
                        self.assertEqual(obj._forecast_tracker._history[-1].actual_kwh, expected)

    async def test_option_reload_waits_for_cycle_and_recreates_both_learners(self):
        global NOW
        old = owner()
        old._restore_adaptive_forecast_state()
        old._curtailment_day = NOW.date()
        old._curtailment_last = {"state": "harvest", "grant_w": 7176}
        old._forecast_tracker.update(132.8589, 43.43)
        old_tracker, old_ledger = old._forecast_tracker, old._forecast_ledger
        shutdown_listener = old._storage._shutdown_listener
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_cycle():
            entered.set()
            await release.wait()
            old._persist_adaptive_forecast_state()
            return {"forecast_today_kwh": 132.8589}

        old._async_update_data_unlocked = slow_cycle
        entry = types.SimpleNamespace(data={}, options={core.CONF_DISABLE_CURTAILMENT_CHAIN: True,
            core.CONF_DISABLE_ADAPTIVE_FORECAST: True}, runtime_data=old, entry_id="test")
        # Even a matching runtime skip snapshot must not swallow a policy change.
        old._skip_options_reload = dict(entry.options)

        async def reload(_):
            await old._storage.async_close()
            replacement_store = storage()
            replacement_store._energy_data = copy.deepcopy(old._storage._energy_data)
            replacement_store._daily_data = copy.deepcopy(old._storage._daily_data)
            entry.runtime_data = owner(True, True, store=replacement_store)
            entry.runtime_data._restore_adaptive_forecast_state()
            await cycle(entry.runtime_data)
            return True

        hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_reload=AsyncMock(side_effect=reload)))
        running = asyncio.create_task(old._async_update_data())
        await entered.wait()
        switching = asyncio.create_task(init_mod.async_update_options(hass, entry))
        await asyncio.sleep(0)
        hass.config_entries.async_reload.assert_not_awaited()
        NOW = NOW.replace(hour=23, minute=59) + timedelta(minutes=2)
        release.set()
        await running
        await switching
        new = entry.runtime_data
        self.assertIsNot(old_tracker, new._forecast_tracker)
        self.assertIsNot(old_ledger, new._forecast_ledger)
        self.assertFalse(hasattr(new, "_curtailment_day"))
        self.assertFalse(hasattr(new, "_curtailment_last"))
        self.assert_disabled(new)
        self.assertEqual(len(old_tracker._history), 12)  # no synthetic old-day record
        self.assertTrue(old._forecast_retired)
        shutdown_listener.assert_called_once()
        self.assertIsNone(old._storage._shutdown_listener)
        old._storage._energy_store.async_save.assert_awaited_once()
        old._storage._daily_store.async_save.assert_awaited_once()
        old._async_update_data_unlocked = AsyncMock(side_effect=AssertionError("retired cycle"))
        await old._async_update_data()
        # Re-enabling intentionally restores the forensic payload; no reset is claimed.
        restored = owner(store=new._storage)
        restored._restore_adaptive_forecast_state()
        self.assertEqual(restored._forecast_tracker.correction_factor, 0.7)
        self.assertEqual(len(restored._forecast_tracker._history), 12)

    async def test_every_options_transition_has_a_fresh_policy_and_objects(self):
        for chain_before in (False, True):
            for adaptive_before in (False, True):
                for chain_after in (False, True):
                    for adaptive_after in (False, True):
                        with self.subTest(before=(chain_before, adaptive_before),
                                          after=(chain_after, adaptive_after)):
                            old = owner(chain_before, adaptive_before)
                            old._restore_adaptive_forecast_state()
                            old._curtailment_day = NOW.date()
                            old._curtailment_last = {"state": "harvest"}
                            await cycle(old)
                            entry = types.SimpleNamespace(data={}, options={
                                core.CONF_DISABLE_CURTAILMENT_CHAIN: chain_after,
                                core.CONF_DISABLE_ADAPTIVE_FORECAST: adaptive_after},
                                runtime_data=old, entry_id="test")

                            async def reload(_, old=old, entry=entry,
                                             chain_after=chain_after, adaptive_after=adaptive_after):
                                await old._storage.async_close()
                                s = storage()
                                s._energy_data = copy.deepcopy(old._storage._energy_data)
                                s._daily_data = copy.deepcopy(old._storage._daily_data)
                                entry.runtime_data = owner(chain_after, adaptive_after, store=s)
                                entry.runtime_data._restore_adaptive_forecast_state()
                                await cycle(entry.runtime_data)

                            hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_reload=reload))
                            await init_mod.async_update_options(hass, entry)
                            new = entry.runtime_data
                            self.assertIsNot(old._forecast_tracker, new._forecast_tracker)
                            self.assertIsNot(old._forecast_ledger, new._forecast_ledger)
                            self.assertFalse(hasattr(new, "_curtailment_day"))
                            self.assertFalse(hasattr(new, "_curtailment_last"))
                            if adaptive_after:
                                self.assert_disabled(new)
                            else:
                                self.assertEqual(new.data["forecast_correction_factor"], 0.7)
                                self.assertEqual(new._forecast_tracker._today_actual, 54.17)

    async def test_backfill_disabled_and_in_flight_reload_serialization(self):
        for adaptive in (False, True):
            obj = owner(adaptive=adaptive)
            init_mod.coordinator, init_mod.hass = obj, obj.hass
            entered, release = asyncio.Event(), asyncio.Event()

            async def backfill(call, obj=obj, entered=entered, release=release):
                entered.set()
                await release.wait()
                obj._forecast_ledger.record("2026-11-12", 1, 100)
                obj._forecast_ledger.settle("2026-11-12", 40)

            with patch.object(init_mod, "_async_backfill_forecast_ledger_unlocked", AsyncMock(side_effect=backfill)) as run:
                task = asyncio.create_task(init_mod.async_backfill_forecast_ledger_service(types.SimpleNamespace(data={})))
                if adaptive:
                    await task
                    run.assert_not_awaited()
                    self.assertEqual(obj._forecast_ledger.days(), [])
                else:
                    await entered.wait()
                    retiring = asyncio.create_task(obj.async_retire_forecast())
                    await asyncio.sleep(0)
                    self.assertFalse(retiring.done())
                    release.set()
                    await task
                    await retiring
                    self.assertEqual(obj._forecast_ledger.actual_for("2026-11-12"), 40)
                    await init_mod.async_backfill_forecast_ledger_service(types.SimpleNamespace(data={}))
                    self.assertEqual(run.await_count, 1)

    async def test_failed_reload_resumes_only_open_lifetime(self):
        obj = owner()
        entry = types.SimpleNamespace(data={}, options={}, runtime_data=obj, entry_id="test")
        hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_reload=AsyncMock(return_value=False)))
        await init_mod.async_update_options(hass, entry)
        self.assertFalse(obj._forecast_retired)
        obj._storage._closed = True
        await init_mod.async_update_options(hass, entry)
        self.assertTrue(obj._forecast_retired)

    async def test_final_publisher_clears_poisoned_error_fallback(self):
        obj = owner(adaptive=True)
        obj._async_update_data_unlocked = AsyncMock(return_value={
            "forecast_today_kwh": 132.8589, "pv_health": "orange",
            "pv_health_attrs": {"ratio_7d": 0.464, "snow": True},
            "pv_performance_vs_forecast": 40.74, "forecast_correction_factor": 0.7,
            "forecast_dampening_factor": 0.5})
        obj.data = await obj._async_update_data()
        self.assert_disabled(obj)

    def test_probe_chain_returns_before_import_or_autodetect(self):
        for adaptive in (False, True):
            obj = owner(True, adaptive)
            obj._curtailment_day = NOW.date()
            with patch.dict(sys.modules, {PREFIX + ".coordinator.curtailment": None,
                                          PREFIX + ".coordinator.sensor_reader": None}):
                self.assertEqual(obj._curtailment_grant_w(POWER), 0)
            self.assertIsNone(obj._curtailment_day)
            self.assertEqual(obj._curtailment_last, {"state": "disabled", "grant_w": 0.0})

    def test_probe_still_harvests_with_only_adaptive_disabled(self):
        sensor_reader = types.ModuleType(PREFIX + ".coordinator.sensor_reader")
        sensor_reader.parse_export_limited = lambda *a: None
        obj = owner(False, True)
        obj._sensor_reader = types.SimpleNamespace(detect_export_limit_entity=Mock(return_value=None))
        p = types.SimpleNamespace(solar_power=2417, grid_export_power=0,
            home_consumption_power=2417, battery_charge_power=0, ev_power=0, ev_connected=True)
        with patch.dict(sys.modules, {sensor_reader.__name__: sensor_reader}), \
                patch.object(probe_mod, "solar_mode_floor", return_value=(True, 1380)), \
                patch.object(coord_mod.time, "monotonic", side_effect=[0, 61, 62]):
            obj._curtailment_grant_w(p)
            self.assertGreater(obj._curtailment_grant_w(p), 0)
            p.solar_power += 2000
            p.ev_power = 2000
            obj._curtailment_grant_w(p)
        self.assertEqual(obj._curtailment_day, NOW.date())
        self.assertEqual(obj._curtailment_last["state"], "harvest")
        self.assertEqual(obj._sensor_reader.detect_export_limit_entity.call_count, 3)

    def test_identity_and_disabled_direct_ledger_backfill(self):
        tracker = tracker_mod.ForecastTracker(disabled=True)
        for raw in (0, 38.14, 132.8589):
            self.assertEqual(tracker.apply_correction(raw), raw)
            self.assertEqual(tracker.apply_dampening(raw), raw)
            self.assertEqual(tracker.corrected_remaining_kwh(raw), raw)
        for attr in ("accuracy_today", "raw_accuracy_today", "accuracy_7d", "deviation_today"):
            self.assertIsNone(getattr(tracker, attr))
        led = ledger_mod.ForecastLedger.from_dict(storage().get_forecast_ledger_state(), disabled=True)
        led.record("2026-11-13", 1, 100)
        led.settle("2026-11-13", 40)
        self.assertEqual(led.backfill({"2026-11-12": (100, 40)}, 1), 0)
        self.assertEqual(led.days(), [])
        self.assertIsNone(led.trust(1))
        self.assertIsNone(led.accuracy(1))
        self.assertEqual(led.horizon_state(1), "disabled")

    def test_option_snapshot_defaults_and_labels(self):
        self.assertEqual(policy.ForecastPolicy.from_config({}), policy.ForecastPolicy(False, False))
        obj = owner()
        obj.config[core.CONF_DISABLE_ADAPTIVE_FORECAST] = True
        self.assertFalse(policy.forecast_policy(obj).disable_adaptive_forecast)
        text = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
        english = json.loads((ROOT / "translations/en.json").read_text(encoding="utf-8"))
        for key in (core.CONF_DISABLE_ADAPTIVE_FORECAST, core.CONF_DISABLE_CURTAILMENT_CHAIN):
            for payload in (text, english):
                step = payload["options"]["step"]["settings_ev"]
                self.assertTrue(step["data"][key])
                self.assertTrue(step["data_description"][key])


if __name__ == "__main__":
    unittest.main(verbosity=2)
