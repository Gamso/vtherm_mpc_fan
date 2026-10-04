---
name: modify-mpc
description: "Modify the MPC controller: cost function tuning, hysteresis thresholds, simulation model, disturbance tracking, monotone constraint, or adding new pausing conditions. Use when: changing how the MPC evaluates or recommends fan modes; adjusting cost weights or switch-gain margins; adding new disturbance sources; modifying the step-down hold logic; changing how confidence is computed."
argument-hint: "Description of the MPC behavior to change (e.g. 'reduce overshoot penalty weight')"
---

# Modify MPC Controller

## Background: MPC at a Glance

MPC (Model Predictive Control) optimizes a control action by:
1. Sampling the current plant state
2. Rolling out candidate actions over a finite prediction horizon
3. Picking the action that minimizes a cost function subject to constraints
4. Applying **only the first step**, then re-solving next cycle (receding horizon)

This project uses a **discrete MPC-lite** adapted for residential heat-pump HVAC:
- **State**: room temperature (scalar)
- **Control input**: fan speed mode (discrete: silent → low → med → high → superhigh)
- **Model**: linear `T(t+Δ) = T(t) + slope × Δt + disturbance_bias × Δt`
- **Horizon**: dead time + 60 min (`DEFAULT_HORIZON_MINUTES`), simulation step 2 min (independent of the control cadence)
- **Optimizer**: exhaustive enumeration over discrete fan modes (small action space)
- **Cost**: comfort error + overshoot penalty + mode-change cost (see table below)

Unlike classic MPC, the model parameters (slopes) are **learned online** by `ThermalLearning`. This makes it a self-calibrating, data-driven MPC with dead-time handling.

## Architecture

The MPC controller lives in `mpc_controller.py` as `MPCController`. This project is a **VTherm Feature Manager plugin**: it has no control loop of its own. `manager.py`'s `MpcFanFeatureManager.refresh_state()` is called by VTherm once per its own control cycle, and calls `mpc_controller.evaluate()` to get a decision dict that feeds sensors/numbers, CSV logs, and the fan command.

When MPC status is actionable (`Ready`, `Setpoint drop`, `Overshoot`, `Low confidence`), the manager applies the fan recommendation. When paused (`Disturbed`, `Idle`, `Unavailable`), the current fan is held.

### Key Flow in `evaluate()`

```
1. Early exits: idle HVAC mode, no fan modes
2. Resolve fan modes and active fan
3. Compute effective slope, comfort error (user setpoint), dead time (gate-capped for the phase), sensor resolution, escalation state
4. Update disturbance bias (EMA tracking)
5. Pause conditions: window-open, defrost, HVAC idle → return "Disturbed"
6. Setpoint drop (genuine user setpoint move, `_track_setpoint`) → return lowest mode immediately; a comfort error < −1 °C without one is `Overshoot` (lowest mode the guards allow, after the simulation)
7. Build monotone slope map (over the learned profiles, partial ladders included)
8. Simulate ALL fan modes over the horizon (dead time + 60 min); all of them follow the observed slope during the dead time
9. Select best by lowest cost (eligibility: climb guard, multi-rank step-down guard; overshoot → lowest eligible)
10. Apply guards: min-interval hold (confirmed escalation bypasses upward), hysteresis, step-down hold
11. Exploration: measurement under load (option), downward probe (default on); cold start → step law
12. Build and return payload
```

### Cost Function (`_simulate_mode`)

Each candidate fan mode is simulated step-by-step over the horizon:

| Cost Component | Weight | Purpose |
|---|---|---|
| `comfort_error × urgency` | 1.0 × (1 + excess error × 2) | Penalizes being outside deadband |
| `overshoot²` | 3.0 | Penalizes going past target |
| `floor_violation` (linear) | 12.0 × urgency | Penalizes a shortfall beyond the deadband (below target in heat, above in cool) |
| `floor_violation²` | 30.0 | Strongly penalizes large shortfalls |
| `mode_change_cost` | 0.15 × distance | Penalizes switching fan modes |
| `mode_rank_cost` | 1.0 × `1.82^rank`, scaled to 0.15× near equilibrium (`HOLD_RANK_SCALE`) | Energy: one more rank costs what ~0.05–0.13 °C of sustained shortfall costs |

The four thermal terms are averaged over the simulation steps (per-step units); all are zero inside the deadband. Cost weights are module-level constants (e.g. `FLOOR_VIOLATION_LINEAR_WEIGHT = 12.0`); calibrate them on the closed-loop plant (`tests/closed_loop.py`, `tests/test_closed_loop.py`), not on the open-loop replay alone.

### Hysteresis (`_required_switch_gain`)

The MPC requires a minimum cost improvement before switching:

| Situation | Base Margin |
|---|---|
| Over-target or far under | 0.2 |
| Approaching target | 0.3 |
| Near target (within deadband) | 0.5 |

Plus bonuses for non-established phase (+0.2), step distance (+0.1/step) and a step down while under target (+0.4 + 1.0/°C).

### Step-Down Hold (`_step_down_hold_note`)

Blocks downward fan moves when still under target and either not yet in ESTABLISHED phase or predicted shortfall at 10 min exceeds the reserve threshold.

### Disturbance Bias (`_update_disturbance_bias`)

Tracks slow external perturbations (solar gains, occupancy). EMA of residual `observed_slope − expected_slope`. Only updates during ESTABLISHED phase with a known profile. Decays during window-open, defrost, or HVAC idle. Clamped to ±2.0 °C/h.

Access via the `disturbance_bias` property (read-only).

### Monotone Constraint (`build_monotone_slopes`)

`build_monotone_slopes()` enforces `slope(mode_i) ≤ slope(mode_i+1)` across the learned profiles. It returns a dict containing **only the modes that have a learned profile** (`{}` when none do) — it never returns `None`, and unlearned modes are simply absent, so the caller falls back to rank-scaled estimation for those. The ladder is a weighted **isotonic regression** (pool-adjacent-violators, weights = each profile's effective sample size), so a thin noisy estimate barely moves a well-sampled one; inside a pooled or tied block the heaviest profile keeps the value and the others are separated one `LADDER_CAPACITY_RATIO` step per rank (never left equal: the energy term would then always pick the weaker). With `sample=True` (Thompson sampling) values are drawn from `N(value, slope_sigma²)` first. This prevents a contaminated low-speed profile from ranking above a stronger mode in cost comparison.

### Pause Conditions

The MPC pauses (returns "Disturbed") during:
- **Window open**: thermal model unreliable
- **Defrost active**: slope data corrupted by heat-pump defrost cycle
- **HVAC idle**: the underlying is not producing

All three conditions decay, not update, the disturbance bias.

`is_window_open`/`is_defrost_active`/`is_hvac_idle` are computed in `manager.py`, not `mpc_controller.py` — `evaluate()` just receives them as booleans. Critically, idle/defrost detection reads the underlying climate's own `hvac_action` (`_underlying_hvac_action()`); no `hvac_action` means unknown (not idle), and VTherm's `is_device_active` is never used. Defrost falls back to the optional `CONF_DEFROST_ENTITY` when the underlying does not report `defrosting`. Do not "simplify" this back to `vtherm.is_device_active` alone — see the vocabulary section in `CLAUDE.md` for why that reads IDLE at equilibrium even while the unit is running.

## Control Cycle (`manager.py`)

`MpcFanFeatureManager.refresh_state()` — called by VTherm once per its own control cycle, not on a private timer:
1. Reads runtime state from the VTherm `InterfaceThermostatRuntime` (temp, target, slope, hvac_mode, current fan)
2. Detects disturbances (`_is_window_open`, `_is_defrost_active`, `_is_hvac_idle`)
3. Calls `mpc_controller.evaluate()` → decision dict
4. Determines `effective_fan`: MPC choice when status is actionable, else holds the current fan (or a `FanOverride` if `set_force_fan_mode` is active)
5. Collects learning data (slope samples, response events) with gating
6. Records to CSV (`_async_record`) and pushes the decision onto entities + VTherm's `extra_state_attributes.mpc_fan` (`_push_to_entities`)
7. Applies the fan change via `self._vtherm.async_set_underlying_fan_mode()` if needed

State tracking is instance attributes on `MpcFanFeatureManager`, not a closure dict: `_last_change_time`, `_response_armed`/`_response_start_temp`/`_response_direction` (temperature-based dead time), `_last_sample` (time-based sampling), `_defrost_active`/`_defrost_start_time`, `_last_setpoint_drop_time`, `_last_hvac_mode`.

## Public API Surface

| Member | Type | Purpose |
|--------|------|---------|
| `fan_modes` | property (r/w) | Update available fan modes |
| `learning` | property (read-only) | Access `ThermalLearning` instance |
| `disturbance_bias` | property (read-only) | Current external disturbance estimate |
| `evaluate(..., user_target_temp=None)` | method | Run one MPC cycle; returns result dict. `target_temp` is the regulated setpoint, `user_target_temp` the user's (comfort reference; falls back to `target_temp`) |
| `sensor_resolution`, `escalation_threshold` | properties | Auto-detected sensor step and the resulting escalation threshold |
| `gate_dead_time(dead_time)` | static method | Dead time capped at `DEAD_TIME_MAX_FOR_GATE` for the learning gate, phase and hold |
| `get_effective_timeout(hvac_mode)` | method | Runtime advisory timeout (learned or configured); diagnostic only, does not gate control |
| `get_live_mode_slope(fan_mode, hvac_mode)` | method | Slope used for a mode in the last evaluated cycle, or None |
| `build_monotone_slopes(fan_modes, hvac_mode, *, error, regulation_offset, sample)` | method | Weighted isotonic (PAV) slope map over the learned profiles only (`{}` if none) |

## Procedure

### 1. Identify the Change Area

| Change | File/Method |
|---|---|
| Cost weights | module-level constants in `mpc_controller.py` |
| Hysteresis margins | module-level constants + `_required_switch_gain()` |
| Step-down hold | `_step_down_hold_note()` |
| New pause condition | `evaluate()` after disturbance update, before setpoint-drop check |
| Disturbance tracking | `_update_disturbance_bias()` |
| Simulation model | `_simulate_mode()` inner loop |
| Confidence | `_compute_confidence()` |
| Monotone constraint | `build_monotone_slopes()`, `_separate()` |
| Exploration | `_probe_candidate()`, the S3 block in `evaluate()`, `INFO_BONUS` |
| Cold start | `_step_law_fan()` |
| Profile fit | `thermal_learning.py` → `_compute_mode_fit()`, `ProfileFit` |
| Learning collection | `manager.py` → `_async_run_cycle()`, `_is_duplicate_slope()`, `_sample_dwell_minutes()`, `_detect_response()` |
| Idle/defrost detection | `manager.py` → `_underlying_hvac_action()`, `_is_hvac_idle()`, `_is_defrost_active()` |

### 2. Make the Change

Module-level constants (e.g., `FLOOR_VIOLATION_LINEAR_WEIGHT = 12.0`) control all weights. If a new constant is user-tunable, add it to `const.py` with `DEFAULT_` prefix and expose in `config_flow.py`.

Key constraints:
- **Never call HA services** from the MPC controller
- **Respect the payload format** — result keys use `mpc_` prefix (e.g. `mpc_fan_mode`)
- **Log at DEBUG level** — MPC logs are verbose by design
- **disturbance_bias and build_monotone_slopes are public** — do not prefix with `_`

### 3. Write Tests

Tests go in `tests/test_mpc_controller.py`. Use the existing helpers:

```python
def test_mpc_<behavior>() -> None:
    """<What this tests>."""
    learning = _build_learning()
    _prime_learning_profiles(learning)
    mpc = _build_mpc(learning)

    result = mpc.evaluate(
        current_temp=..., target_temp=...,
        vtherm_slope=..., hvac_mode="heat",
        current_fan="medium",
        is_window_open=False, minutes_since_change=20.0,
    )

    assert result["mpc_fan_mode"] == ...
    assert result["mpc_status"] == ...
```

To test `build_monotone_slopes` or `disturbance_bias` directly, call them on the `mpc` instance — no `_` prefix.

### 4. Update Documentation

- **`README.md`** — MPC section if user-visible behavior changes
- **`docs/mpc_mode.md`** — technical design document
- **`CLAUDE.md`** — if adding new constraints or changing the control-flow / vocabulary described there

### 5. Validate

```bash
python -m pytest tests/test_mpc_controller.py -q   # MPC tests
python -m pytest tests/ -q                          # all tests
```
