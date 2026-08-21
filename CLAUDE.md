# VTherm MPC Fan — Project Guidelines

## Architecture

This is a **Versatile Thermostat (VTherm) Feature Manager plugin**, not a
standalone integration: it registers with `vtherm_api` and attaches to one
`over_climate` VTherm rather than driving a raw climate entity directly. The
integration lives under `custom_components/vtherm_mpc_fan/`. Key modules:

| File | Role |
|------|------|
| `manager.py` | `MpcFanFeatureManager` — the VTherm feature manager: owns the control cycle (`refresh_state()` called once per VTherm cycle), disturbance detection, entity lifecycle |
| `mpc_controller.py` | `MPCController` — MPC-lite: learned thermal model, cost-based fan selection, hysteresis guards |
| `thermal_learning.py` | `ThermalLearning` — slope samples, response-time events, gap-dependent slope model, profile calibration |
| `registry.py` | `hass.data` rendez-vous point between VTherm-driven manager creation and HA-driven entity platform setup, keyed by VTherm `unique_id` |
| `factory.py` | `InterfaceFeatureManagerFactory` implementation registered with `vtherm_api` |
| `__init__.py` | HA integration entry point: factory registration, services, config-entry lifecycle |
| `config_flow.py` | Config and options UI flows |
| `sensor.py` / `number.py` | HA entity platforms (diagnostics as sensors, editable per-profile effective slopes as numbers) |
| `data_collection.py` | CSV logger for offline analysis (`vtherm_mpc_fan_data_*.csv` in the HA config dir) |

## Domain Vocabulary

- **error**: always positive when the system needs more heating/cooling (`target - current` in heat, `current - target` in cool)
- **effective_slope**: learned slope per fan mode, gap-dependent (`slope(error) = intercept + gain * error`, least-squares fit in `ThermalLearning`); raw slope comes from VTherm's own EMA
- **dead_time**: thermal lag between a fan change and first observable slope response (learned via response events). Resolved **per hvac mode** — heating lag and cooling lag are different numbers, and `get_dead_time()` pools every mode when called without one. It gates the change interval, the phase split and each candidate's simulated `change_delay`.
- **trusted dead time**: whether the learned dead time may raise the change interval above the configured floor. Gated on `response_event_count()` (see `MIN_RESPONSE_EVENTS_FOR_ADAPTIVE_INTERVAL`), **not** on `ThermalLearning.is_ready()` — that flag counts *slope* samples, which a coarse room sensor (0.2 °C steps are common) accumulates so slowly it can stay false indefinitely while the dead time is already well established.
- **defrost_active**: heat-pump defrost cycle; detected from the underlying climate's own `hvac_action == "defrosting"` when it reports one, falling back to the optional `CONF_DEFROST_ENTITY` when it doesn't. Pauses MPC decisions and learning.
- **hvac_idle**: the underlying is not producing; read from the underlying climate's own `hvac_action` (`_underlying_hvac_action()` in `manager.py`), **not** VTherm's `is_device_active`. VTherm falls back to a bare target-vs-current sign check when the underlying reports no `hvac_action`, which reads IDLE across the whole "at or past setpoint" region — exactly the equilibrium-holding case this controller exists to manage, even while the unit is actually running. No `hvac_action` published means unknown, not idle.

## Control Flow

VTherm drives the cycle, not a private timer: `manager.py → MpcFanFeatureManager.refresh_state()` is called once per VTherm control cycle, right after VTherm recomputes its regulated setpoint.

1. `_async_ensure_loaded()` — lazy-load the persisted model and build `MPCController` (once)
2. `ensure_entities()` — create sensor/number entities once their platform's `async_add_entities` callback has arrived (each platform sets up on its own HA schedule)
3. `_async_run_cycle()`:
   - Read runtime state (temp, target, slope, hvac_mode, current fan) from the VTherm `InterfaceThermostatRuntime`
   - Detect disturbances (window open, defrost, hvac idle)
   - Call `mpc_controller.evaluate()` → returns the decision payload (`mpc_*` keys)
   - Collect learning data (slope samples, response events) with gating for phase, defrost, idle, window, setpoint-drop cooldown
   - Record to CSV if data collection enabled
   - Push decision onto entities and VTherm's own `extra_state_attributes.mpc_fan`
   - Apply the fan change if the MPC status is actionable, else hold the current fan

## MPC Decision Engine

The MPC controller (`mpc_controller.py`) evaluates all candidate fan modes over a 30-min horizon:

- **Cost function**: comfort error + overshoot penalty + floor violation + mode-change distance cost + geometric mode-rank cost (`MODE_POWER_RATIO ** candidate_index`, tie-breaker-scaled near equilibrium via `HOLD_RANK_SCALE`) + min-interval-change penalty
- **Hysteresis**: requires minimum cost improvement before switching (margin scales with proximity to target)
- **Step-down guards**: blocks downward moves when under target and not established or predicted shortfall
- **Disturbance bias**: EMA tracker for unmodeled effects (solar, occupancy); decays during paused periods
- **Monotone constraint**: when all profiles learned, enforces slope(mode_i) ≤ slope(mode_i+1)
- **Pause conditions**: window open, defrost, hvac idle → returns "Disturbed" status (see vocabulary above for how idle/defrost are actually detected)

Every `evaluate()` return path funnels through `_payload()`, which also logs the full decision at DEBUG on every cycle — several point-in-time sensors (status, reason, fan mode, would-change-now, cost, known-profile counts, per-mode profile map) were deliberately dropped as standalone entities in favour of this log line, since they had no history-graph or automation value and most duplicate VTherm's own `extra_state_attributes.mpc_fan`.

## Test Conventions

- Framework: **pytest** in `tests/`; run with `python -m pytest tests/ -q`
- Never use `time.sleep`; mock `time.time` via `unittest.mock.patch`
- Protected-member access (`manager._is_hvac_idle()`) is normal in tests — add `# noqa: SLF001` on that line, not a module-level pylint disable
- Every test function and helper needs a docstring
- Test helpers: `_build_learning()` / `_build_mpc(learning)` in `test_mpc_controller.py`; `_make_runtime()` / `_make_hass()` / `_build_manager()` in `test_manager.py` build stand-ins for VTherm's `InterfaceThermostatRuntime` and a `hass` stub respectively
- A fix for a reported bug should include a **control test**: temporarily revert the fix and confirm the new test actually fails before restoring it — proves the test would have caught the regression

## Home Assistant Patterns

- `async_write_ha_state()` is called after any state mutation in sensor/number entities (guarded by `if self.hass is not None` when the entity may not be attached to a platform yet, e.g. in unit tests)
- Config flow uses `vol.Schema` with selectors from `homeassistant.helpers.selector`
- Entity IDs are built with `build_entity_id()` / `build_scoped_entity_id()` / `build_unique_id()` from `const.py`
- `hass.data[DOMAIN]` is a rendez-vous registry (`registry.py`), keyed by **VTherm `unique_id`**, not config-entry id: `DATA_MANAGERS` (live managers), `DATA_ADD_ENTITIES` (per-platform `async_add_entities` callbacks), `DATA_ENTITIES` (per-VTherm entity buckets). `clear_registry()` must only be called from `async_unload_entry`, never from `MpcFanFeatureManager.stop_listening()` — see the docstrings on both for why.

## Build & Test

```bash
python -m pytest tests/ -q          # run all tests
python -m pytest tests/test_X.py -q # run one file
./container coverage                 # coverage via Docker container
./container hassfest                 # validate manifest / translations
bash validate.sh                     # syntax/JSON/required-file checks (mirrors CI)
```

## Dev Container Workflow

- When working inside this repository's provided devcontainer / Docker environment, do **not** spend time creating or configuring a separate Python environment by default.
- Treat the container runtime and the already available project tooling as the authoritative execution environment.
- Prefer the existing shell environment and direct test commands such as `pytest tests/ -q` unless the user explicitly asks for Python environment debugging.

## Key Constants (const.py)

- `THRESHOLD_TARGET_DROP = -1.0` — setpoint-drop trigger (°C)
- `DEFAULT_DEADBAND` — tunable via options flow
- `CONF_DEFROST_ENTITY` — optional entity for external defrost signal; fallback only, the underlying's own `hvac_action` is checked first
- `CONF_FAN_MODE_ORDER` — optional explicit weakest-to-strongest fan speed order, overrides what the underlying climate reports
- `SETPOINT_DROP_LEARNING_COOLDOWN = 30.0` — minutes to suppress learning after a large setpoint drop
- `MIN_ESTABLISHED_RATIO = 2.0` — multiplier on dead_time; fan mode must be active this long before learning
- `MIN_MODE_PROFILE_SAMPLES = 10` — minimum samples per fan mode before its profile is trusted

## Important Constraints

- **Avoid over-engineering**: only add code that directly addresses the requirement
- **No second-order slope terms**: VTherm slope is already EMA-smoothed; parabolic projection amplifies noise
- **Learning data integrity**: exclude window-open, defrost, hvac-idle, setpoint-drop cooldown (30 min), and insufficiently-stable periods (< 2× dead_time) from slope samples
- **Monotone constraint**: when all fan-mode profiles are learned, MPC enforces slope(mode_i) ≤ slope(mode_i+1) via isotonic forward pass; partial profiles skip the constraint
- **Idle/defrost detection**: never fall back to VTherm's `is_device_active`/simulated `hvac_action` as the primary signal for pausing control — it is wrong precisely at equilibrium (see vocabulary above). Read the underlying's own `hvac_action` first.
- **Never gate on `is_ready()` for anything but overall learning progress**: it counts slope samples against a global threshold and lags far behind per-mode profiles and the dead time, both of which mature much earlier. Confidence (`_compute_confidence`) and the adaptive interval (`_dead_time_is_trusted`) each had to be moved off it after it left them stuck at their fallback values indefinitely on real hardware.
