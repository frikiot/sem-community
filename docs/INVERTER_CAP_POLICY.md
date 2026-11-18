# SoC-based inverter-cap load policy

This opt-in addition runs SEM's surplus-managed loads against a configured
inverter AC ceiling when the home battery is sufficiently charged. It does
not require measured solar surplus or a forecast in that mode.

## Enable

In SEM's configuration, open the **Battery Charge Scheduler** options page:

1. Set **Inverter AC output limit** (`inverter_ac_limit_w`) to the actual
   continuous AC limit for the inverter output supplying these loads.
2. Enable **Use inverter capacity above Auto-start SoC**
   (`inverter_cap_loads_enabled`, default **false**).
3. Set **Auto-start SoC above Buffer SoC**, for example 95% and 90%.

Neither charge scheduling nor charge pacing needs to be enabled. The option
reloads SEM. The existing `solar_energy_management.set_option` action can
also save `inverter_cap_loads_enabled: true`; use an actual boolean.

## Behaviour

- At or above Auto-start: enter inverter-cap mode.
- While above Buffer: remain in that mode, including at night.
- At or below Buffer: return to the current measured solar-surplus pool.
  No battery/grid runtime top-up or deadline force can restart discretionary
  loads in this policy. Re-enter inverter-cap mode only at Auto-start.
- Restarting between the two thresholds leaves battery permission off until
  Auto-start is reached again. Missing SoC/power, an invalid ceiling or invalid
  thresholds revoke the permission and produce no capacity allowance.

The configured Battery priority/reserve SoC remains a lower bound on the
cutoff. A forecast-derived dynamic floor does not raise this policy's Buffer.
The battery controller and inverter retain their own operating protections.

The allocator's total managed-load allowance is:

    inverter AC ceiling - other demand - regulation margin

Other demand includes the house's unmanaged consumption, EV demand and
export. EV demand is the larger of measured fleet draw and the fleet's
already-issued power commands, so a newly starting car cannot donate watts
its meter has not reported yet. The existing regulation offset (normally
50 W) is reserved even on a zero-export installation. The allowance includes
already-running managed loads and is spent once, in their priority order.

Daily **minimum** runtime does not end this mode. Daily **maximum** runtime,
device stop conditions, dependencies, manual off, phase holds, grid-peak
shedding and minimum run/pause times still apply. SEM stops lower-priority
loads before admitting replacements, and a load that cannot stop continues
to reserve its capacity. Device power ratings must be accurate.

The opt-in applies to the surplus controller's loads, including loads
previously labelled Solar only: this switch explicitly authorizes the new
source policy for them. Off, Peak only, externally managed devices and
equipment-owned runs remain outside its allocation. Their demand is reserved.
EV control itself is unchanged; this patch does not make the EV start on the
new SoC policy or change the charger-versus-load priority system.

The decision log identifies capacity-funded runs as `inverter_cap`. The
distributable-load reading shows the capacity allowance while active; measured
solar and forecast readings are unchanged. Capacity left over is never
advertised as free solar through the surplus-availability event.

## Scope and operating limits

This is a software allocation ceiling for controllable loads, not an
instantaneous hardware overload limiter. Unmanaged demand, motor starting
surges, delayed telemetry, refused commands and compressor minimum-run times
can temporarily exceed it. It cannot bring demand below the ceiling if the
unmanaged loads or EV alone exceed it. It assumes SEM's house/EV measurements
describe the same output; it is not a separate budget for each inverter in
a mixed installation.

The AC rating is not a promise of available generation. If PV and the
battery's permitted discharge cannot supply the allocated loads, the grid
may cover the shortfall, subject to the existing grid controls. This policy
does not alter inverter/BMS discharge settings or provide a no-import guarantee.

When this option is off, the stock load-control path is retained. The
pre-existing adaptive-forecast fork changes are preserved independently.

## Validation

Run from this checkout:

    py -B tests/test_inverter_cap_offline.py
    py -B tests/test_fork_disable_adaptive_offline.py

The new tests execute the real controller update, intent and reconciliation
code with simulated device I/O. They cover zero-solar starts after the daily
minimum, thresholds, invalid inputs, aggregate capacity, EV reservations,
house-load increases, failed/delayed stops, observer mode, existing stop
conditions, paid-source suppression and the disabled stock paths.

Home Assistant and pytest are not installed in this environment. No live HA
reload, hardware response or full HA-dependent suite has been verified.

This fork starts from upstream SEM 2.2.0. The forecast controls and inverter-cap
policy are separate commits. Preview 2.2.1b1 packages both changes for HACS;
the release asset is `solar_energy_management.zip`. The integration domain
stays `solar_energy_management`, so installing it replaces the current SEM
files rather than creating a second integration.
