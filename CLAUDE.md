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

- **error**: always positive when the system needs more heating/cooling (`target - current` in heat, `current - target` in cool). The MPC's comfort error uses the **user's** setpoint (`target_temperature`), not VTherm's `regulated_target_temperature`; their difference is the `regulation_offset` (regulated − user), stored with each slope sample
- **setpoint drop / overshoot**: `Setpoint drop` only follows a genuine move of the user's setpoint (≥ 1 °C away from the demand between two cycles, `MPCController._track_setpoint`) and starts the learning cooldown; a room > 1 °C past an unchanged setpoint is `Overshoot` (lowest speed the guards allow, no cooldown)
- **legacy samples**: 5-tuple slope samples from stores older than `LEARNING_DATA_FORMAT` 2; their error was measured against the regulated setpoint, so they weigh `LEGACY_SAMPLE_WEIGHT` in the fits
- **effective_slope**: learned slope per fan mode, gap-dependent (`slope(error) = intercept + gain * error`, weighted Theil–Sen fit in `ThermalLearning`, gain shrunk toward the pooled gain with `n_eff`), evaluated at `min(REFERENCE_SLOPE_ERROR, error_max)` — never extrapolated past the errors measured (*partial* profile otherwise); raw slope comes from VTherm's own EMA
- **dead_time**: thermal lag between a fan change and the first move of the room temperature, of at least one sensor step, in the direction the change should produce (learned via response events, `_detect_response`). Resolved **per hvac mode** — heating lag and cooling lag are different numbers, and `get_dead_time()` pools every mode when called without one. The learned value sets the horizon, the adaptive change interval and each candidate's simulated `change_delay`; the learning gate, the phase split and the learning hold use it capped at `DEAD_TIME_MAX_FOR_GATE` (15 min, `MPCController.gate_dead_time()`).
- **trusted dead time**: whether the learned dead time may raise the change interval above the configured floor. Gated per HVAC mode on `response_event_count(hvac_mode)` (see `MIN_RESPONSE_EVENTS_FOR_ADAPTIVE_INTERVAL`) — response events are only recorded in heat/cool, and events stored from other modes are ignored — **not** on `ThermalLearning.is_ready()` — that flag counts *slope* samples, which a coarse room sensor (0.2 °C steps are common) accumulates so slowly it can stay false indefinitely while the dead time is already well established.
- **defrost_active**: heat-pump defrost cycle; detected from the underlying climate's own `hvac_action == "defrosting"` when it reports one, falling back to the optional `CONF_DEFROST_ENTITY` when it doesn't. Pauses MPC decisions and learning.
- **fixed fan (pin)**: in the HVAC modes listed in `CONF_FIXED_FAN_HVAC_MODES` (never heat/cool), the manager sends `CONF_FIXED_FAN_SPEED` instead of the MPC decision (status `Fixed`; `force_fan` still wins). Applied on entering the mode, otherwise only after `min_interval` since the last change — counted from the manager's first cycle after a restart/reload — and held while a window is open, the underlying is off or defrosting. It never feeds learning.
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

The MPC controller (`mpc_controller.py`) evaluates all candidate fan modes over `dead_time + DEFAULT_HORIZON_MINUTES` (60 min); every candidate, the current one included, follows the observed slope during the dead time:

- **Cost function**: thermal terms averaged per step — comfort error + overshoot penalty + floor violation (shortfall *beyond the deadband*) — + mode-change distance cost + geometric mode-rank cost (`MODE_RANK_COST × MODE_POWER_RATIO ** candidate_index`, one rank ≈ 0.05–0.13 °C of sustained shortfall, scaled down near equilibrium via `HOLD_RANK_SCALE`). No term charges a predicted error inside the deadband; the min interval is a gate, not a cost
- **Escalation**: growth of the comfort error since the change past `max(0.15, 1.5 × sensor_resolution)` on 2 consecutive cycles (immediate past `MULTI_RANK_JUMP_ERROR`); `sensor_resolution` is auto-detected from the readings
- **Hysteresis**: requires minimum cost improvement before switching (margin scales with proximity to target)
- **Step-down guards**: blocks downward moves when under target and not established or predicted shortfall
- **Disturbance bias**: EMA tracker for unmodeled effects (solar, occupancy); decays during paused periods
- **Monotone constraint**: weighted isotonic regression (PAV) over the learned profiles (partial ladders included), ties separated one `LADDER_CAPACITY_RATIO` step apart
- **Cold start**: while no profile of the HVAC mode exists, a step law on the comfort error replaces the cost-based choice
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
bash validate.sh                     # syntax/JSON/required-file checks
black --check custom_components tests   # formatting, enforced by CI (pyproject.toml, 180 cols)
pylint custom_components/vtherm_mpc_fan # enforced by CI (.pylintrc)
```

CI (`.github/workflows/`) runs on every push and PR: pytest, black, pylint, `validate.sh`, hassfest and the HACS action.

Tests that need a real Home Assistant core (setup/unload, services, flows) request the `integration` fixture from `tests/conftest.py`; everything else uses the lightweight stand-ins above.

## Dev Container Workflow

- When working inside this repository's provided devcontainer / Docker environment, do **not** spend time creating or configuring a separate Python environment by default.
- Treat the container runtime and the already available project tooling as the authoritative execution environment.
- Prefer the existing shell environment and direct test commands such as `pytest tests/ -q` unless the user explicitly asks for Python environment debugging.

## Key Constants (const.py)

- `THRESHOLD_TARGET_DROP = -1.0` — setpoint-drop trigger (°C): the user's setpoint moving away by this much, and the comfort error below it
- `DEFAULT_DEADBAND` — tunable via options flow
- `CONF_DEFROST_ENTITY` — optional entity for external defrost signal; fallback only, the underlying's own `hvac_action` is checked first
- `CONF_FAN_MODE_ORDER` — optional explicit weakest-to-strongest fan speed order, overrides what the underlying climate reports
- `SETPOINT_DROP_LEARNING_COOLDOWN = 30.0` — minutes to suppress learning after a large setpoint drop
- `MIN_ESTABLISHED_RATIO = 1.5` (= `DEAD_TIME_SAFETY_FACTOR`) — multiplier on dead_time; fan mode must be active this long before learning. Deliberately equal to the `ESTABLISHED` phase threshold: a stricter factor pushed the first sample past the point where the controller may change speed again
- `SAMPLE_INTERVAL_MINUTES = 10`, `MEASURED_PROFILE_MINUTES = 90`, `MIN_MEASURED_PROFILE_SAMPLES = 6` — one slope sample per 10 min of established regime even when the slope has not moved (a holding speed keeps the sensor silent), each carrying the minutes it stands for; a profile is *measured* (`has_measured_profile()`) once its measured samples cover 90 min in ≥ 6 samples. Seeded samples never count. `MIN_MODE_PROFILE_SAMPLES = 10` is now only the number of synthetic samples a seed writes
- Fits use an effective sample size `n_eff = n/(1+2ρ)` (ρ = lag-1 autocorrelation of the residuals) and shrink each profile's gain toward the gain pooled over the HVAC mode's speeds (`GAIN_PRIOR_SAMPLES`)
- `PROFILE_RETENTION_SAMPLES = 40` — newest samples kept per profile regardless of age, so rare speeds accumulate across 7-day windows
- `MIN_SAMPLES_LEARNING = 120` — global readiness; sized to what the window can hold (the old 240 was unreachable on real hardware)
- `LEARNING_HOLD_EXTRA_MINUTES`, `MULTI_RANK_JUMP_ERROR` (mpc_controller.py) — the exploration guards: hold an unmeasured current speed until it can be sampled; never climb past an unmeasured, viable-looking rung unless the error is a recovery (> 1 °C) or the escalation guard fires
- `PROBE_INTERVAL_HOURS`, `PROBE_MAX_BIAS` (mpc_controller.py) — the opportunistic downward probe (`CONF_EXPLORATION_PROBE`, on by default): one rank down to an unmeasured speed while the room holds; `INFO_BONUS` (`CONF_EXPLORATION_UCB`) and the measurement under load (`CONF_EXPLORATION_UNDER_LOAD`) are off by default

## Important Constraints

- **Avoid over-engineering**: only add code that directly addresses the requirement
- **No second-order slope terms**: VTherm slope is already EMA-smoothed; parabolic projection amplifies noise
- **Learning data integrity**: exclude window-open, defrost, hvac-idle, setpoint-drop cooldown (30 min), and insufficiently-stable periods (< 1.5× dead_time, the same `detect_phase()` the MPC uses) from slope samples. Do **not** filter near-zero slopes: a speed holding the room produces them, and they measure its intercept
- **Exploration is a control concern**: a speed that is never measured is never credible on cost and never chosen, so the controller must create the observations (learning hold, climb guard, exploration probe) — no estimator can synthesise them. A fan change made outside the plugin restarts the dead time like one of ours (`_register_fan_change`)
- **The current unmeasured speed is modelled on its observed slope**, never floored: a speed observably losing ground must lose ground in the simulation, and a negative reference slope (learned, seeded or observed) passes through `_gap_slope`
- **Monotone constraint**: `build_monotone_slopes` enforces slope(mode_i) ≤ slope(mode_i+1) over whatever profiles are learned (unlearned modes are simply absent): a weighted isotonic regression (PAV, weights = `n_eff`), then the members of a pooled or tied block separated one `LADDER_CAPACITY_RATIO` step from the heaviest one, never left equal
- **Idle/defrost detection**: never fall back to VTherm's `is_device_active`/simulated `hvac_action` as the primary signal for pausing control — it is wrong precisely at equilibrium (see vocabulary above). Read the underlying's own `hvac_action` first.
- **Never gate on `is_ready()` for anything but overall learning progress**: it counts slope samples against a global threshold and lags far behind per-mode profiles and the dead time, both of which mature much earlier. Confidence (`_compute_confidence`) and the adaptive interval (`_dead_time_is_trusted`) each had to be moved off it after it left them stuck at their fallback values indefinitely on real hardware.
