# MPC Controller — Technical Design

## Purpose

The MPC controller is the sole decision engine for fan speed.
It maintains a learned thermal model, scores every candidate fan mode over a 30-minute horizon, and selects the mode with the lowest cost.
When MPC status is actionable (`Ready`, `Setpoint drop`, `Overshoot`, `Low confidence`), the integration applies the fan recommendation. When paused (`Disturbed`, `Idle`, `Unavailable`), the current fan mode is held. The MPC only regulates `heat` and `cool`; in any other HVAC mode it reports `Idle`, unless that mode has a fixed fan speed (status `Fixed`, set by the feature manager, not by the MPC). A `force_fan` override reports `Forced`.

## Goals

- Learn the thermal behavior of a specific room with minimal manual tuning.
- Reuse the data already collected by the integration.
- Keep the controller explainable and easy to debug from Home Assistant diagnostics.

## Non-Goals

- No reinforcement learning or opaque black-box model is introduced.
- No second-order slope terms: VTherm slope is already EMA-smoothed, so the model avoids parabolic
  projection that would amplify noise.

## Runtime Architecture

Each control cycle follows this flow:

VTherm drives the cycle: it calls `MpcFanFeatureManager.refresh_state()` (`manager.py`) once per
control cycle, right after recomputing its regulated setpoint.

1. Read temperature, target, slope, HVAC mode, and current fan mode from the VTherm runtime.
2. Detect disturbances (defrost and HVAC idle from the underlying climate's own `hvac_action`,
   window open from VTherm's `hvac_off_reason`).
3. Run `MPCController.evaluate(...)` → `mpc_decision` dict.
4. Resolve the effective fan: `force_fan` override > fixed speed of the HVAC mode > MPC.
5. Compute phase (DEAD_TIME / TRANSIENT / ESTABLISHED) with the dead time of the current HVAC mode.
6. Collect learning data (slope samples, response events) with gating — `heat`/`cool` only.
7. Append MPC information to the CSV log.
8. Push the decision to the sensors (`update_from_mpc()`) and to the VTherm's `mpc_fan` attribute.
9. Apply the effective fan when it differs from the current one; the MPC recommendation is
   applied only when its status is actionable (`Ready`, `Setpoint drop`, `Overshoot`, `Low confidence`), the
   current fan is held otherwise (`Disturbed`, `Idle`, `Unavailable`).

### HA Entities

Entity IDs are scoped by the VTherm entity: `<platform>.vtherm_mpc_fan_<vtherm object id>_<key>`
(below, for `climate.living_room`).

| Platform | Entity ID                                                         | Purpose                                            |
| -------- | ----------------------------------------------------------------- | -------------------------------------------------- |
| `sensor` | `sensor.vtherm_mpc_fan_living_room_fan_mode`                         | Effective fan mode after the cycle                 |
| `sensor` | `sensor.vtherm_mpc_fan_living_room_mpc_confidence`                   | Confidence percentage                              |
| `sensor` | `sensor.vtherm_mpc_fan_living_room_mpc_predicted_temperature_10_min` | 10-minute temperature forecast                     |
| `sensor` | `sensor.vtherm_mpc_fan_living_room_mpc_predicted_temperature_30_min` | 30-minute temperature forecast                     |
| `sensor` | `sensor.vtherm_mpc_fan_living_room_mpc_disturbance_bias`             | Slow disturbance correction term                   |
| `sensor` | `sensor.vtherm_mpc_fan_living_room_learned_dead_time`                | Learned dead time (pooled over heat/cool, min)     |
| `number` | `number.vtherm_mpc_fan_living_room_<hvac>_<fan>_effective_slope`     | Editable effective slope of one profile            |

Status, reason, recommended fan mode, would-change-now, cost, the dead time used by the simulator
and the number of known profiles have no entity of their own: they are published in the VTherm's
`mpc_fan` attribute section and logged at DEBUG on every cycle by `MPCController._payload()`. The
full entity list is in the README.

CSV log fields are prefixed with `mpc_*`.

The MPC controller owns its runtime parameters (`deadband`, `min_interval`, `fan_modes`) and consumes learned profiles from `ThermalLearning`.

## Comfort setpoint

`evaluate(target_temp=..., user_target_temp=...)` receives both setpoints: `target_temp` is VTherm's
`regulated_target_temperature` (what the unit is sent), `user_target_temp` the user's
`target_temperature`. Every comfort quantity — the error, the simulated cost, the deadband, the
escalation, the learning hold, the setpoint drop — uses the user's setpoint (the regulated one when
the user's is unknown). `regulated − user` is reported as `mpc_regulation_offset` and stored with each
learning sample.

A **setpoint drop** is a move of the user's setpoint of at least `|THRESHOLD_TARGET_DROP|` away from
the demand between two cycles (`_track_setpoint`); while the comfort error stays below
`THRESHOLD_TARGET_DROP` the MPC returns the lowest speed and the manager pauses learning for
`SETPOINT_DROP_LEARNING_COOLDOWN`. A comfort error below the threshold without such a move is an
**overshoot**: the normal selection runs, then the lowest speed the step-down guard allows is taken
(hysteresis is skipped, the min interval still applies) and no cooldown starts.

## Learned Thermal Model

The current version uses a temperature-state model driven by learned fan-mode gains, a learned dead time, and a disturbance correction term.
It still consumes `temperature_slope`, but the slope is treated as an observation that helps estimate thermal power, not as the final state to optimize directly.

Definitions:

- `T_hat[k]`: predicted room temperature at step `k`
- `q_hat[k]`: predicted effective thermal power at step `k` (expressed in equivalent `°C/h`)
- `u`: candidate fan mode held constant over the horizon
- `dead_time`: learned thermal delay between a fan change and a visible sensor response
- `bias`: slow disturbance correction term for unmodeled effects such as solar gain or occupancy

For each candidate mode:

```text
if elapsed <= dead_time:
    target_power = current_effective_power
else:
    target_power = learned_mode_power(candidate_mode) + bias

q_hat[k+1] = q_hat[k] + alpha * (target_power - q_hat[k])
T_hat[k+1] = T_hat[k] + delta_t_hours * raw_slope(q_hat[k+1], hvac_mode)
```

Where:

- `alpha = THERMAL_POWER_BLEND = 0.45` (`mpc_controller.py`)
- `delta_t = 2 min`
- `raw_slope = +q_hat` in heating and `-q_hat` in cooling

### Source of Learned Parameters

The model reuses the current learning subsystem:

- `learning.get_dead_time(hvac_mode)` for thermal delay — per HVAC mode, from `heat`/`cool` response
  events only; it raises the change interval only once trusted (`MIN_RESPONSE_EVENTS_FOR_ADAPTIVE_INTERVAL`
  events in that mode)
- `learning.get_mode_slope_model(fan_mode, hvac_mode)` for reliable per-mode profiles

If a reliable profile is not yet available for a fan mode, the MPC model falls back to an estimate: for the speed currently running, the slope the room is actually showing (never floored — a speed losing ground must lose ground in the simulation); for the other candidates, the nearest learned profile stepped along the ladder by `LADDER_CAPACITY_RATIO`, or a coarse rank scaling of the current slope when nothing is learned at all. Confidence is lowered accordingly.

A profile seeded by hand (`set_effective_slope`, or the number entities) counts as *known* for the slope but not as *measured*: the exploration guards below only trust `ThermalLearning.has_measured_profile()`, i.e. samples that carry a comfort error and cover `MEASURED_PROFILE_MINUTES` (90) of established regime in at least `MIN_MEASURED_PROFILE_SAMPLES` (6). The manager takes one sample per `SAMPLE_INTERVAL_MINUTES` (10) of established regime even when the slope has not moved, and records with it the minutes it stands for (`dwell_minutes`); within that interval an unchanged reading is a duplicate.

### Gap-Dependent Slope Model

Effective cooling/heating power is **not constant**: per Newton's law of cooling it scales with
the distance to the setpoint. A single scalar (the historical median) is structurally diluted by the
many samples collected near equilibrium, where the slope is naturally shallow — it under-states the
fan's real working power.

Each profile therefore learns a linear model by ordinary least squares over its
`(comfort_error, effective_slope)` samples:

```text
effective_slope(error) = a + b * error      (b clamped to >= 0)
```

- `learning.get_mode_slope_model()` returns `(a, b)`; `get_mode_slope_gain()` returns `b`.
- `learning.get_mode_effective_slope()` reports the representative **working** slope, i.e. the model
  evaluated at `REFERENCE_SLOPE_ERROR` (1 °C). For legacy/synthetic constant profiles (`b == 0`) this
  is exactly the previous median estimator, so behaviour is unchanged for those.
- The simulator recomputes the slope **at each step** from the simulated error, so the projection
  decelerates realistically as the room approaches the setpoint instead of cooling/heating at a fixed
  rate (which produced phantom overshoot past the target), and uses the higher real power when the
  room is far from target (faster, more accurate catch-up).

### Disturbance Handling

When the current fan mode has a reliable learned profile and the controller is in `ESTABLISHED` phase, the MPC model estimates a slow disturbance bias from the residual:

```text
residual = observed_effective_power - expected_effective_power(current_error)
bias[k+1] = (1 - beta) * bias[k] + beta * residual
```

The expected power is the gap-dependent model evaluated **at the current comfort error**, not at the
reference gap. This keeps the bias clean: it captures only genuine external disturbances (solar gain,
occupancy) instead of the systematic variation of power with the distance to setpoint, which the gap
model now explains directly.

This helps the MPC model remain usable when the room is slightly helped or hindered by effects not directly modeled by the fan itself.

When a window is detected as open, the MPC model does not trust its own predictions and switches to a `Disturbed` status instead of producing an actionable recommendation.

## MPC-lite Decision Rule

The controller simulates every available fan mode on a short fixed horizon of 30 minutes.
The candidate fan action is held constant over the horizon, but its effective slope is recomputed at
each step from the simulated comfort error (see the gap-dependent slope model above).
The simulator supports both `heat` and `cool`; cooling uses the same learned effective-power model with the sign inverted back to room-temperature evolution.

Each candidate mode gets a scalar cost:

```text
J(mode) =
    sum(comfort_error × urgency)
  + 3.0 * sum(overshoot²)
  + 12.0 * urgency * sum(floor_violation) + 30.0 * sum(floor_violation²)
  + 0.15 * fan_step_distance
  + 0.05 * fan_energy_rank
  + min_interval_penalty
```

Where:

- `comfort_error = max(abs(error) - deadband, 0)`, amplified by urgency (`1 + excess × 2`)
- `overshoot = max(-error, 0)` — going past target
- `floor_violation = max(target - predicted, 0)` — dropping below setpoint
- `fan_step_distance` penalizes unnecessary fan jumps
- `fan_energy_rank` lightly discourages staying on the highest modes all the time
- `min_interval_penalty` prevents changes before the effective timeout has elapsed

In addition, the current implementation applies a mode-independent floor penalty when the predicted room temperature drops below the setpoint. This reflects a conservative comfort rule: if the target is `20°C`, predictions below `20°C` are considered increasingly unacceptable in both `heat` and `cool`.

The selected mode is the one with the lowest total cost.
To avoid fan yo-yo near the setpoint, a recommendation that changes the fan must also beat the current mode by a minimum gain. If the gain is only marginal, the MPC controller keeps the current fan and reports that hysteresis blocked the switch.

### Exploration guards

A purely cost-driven controller never visits a speed it has no profile for, and a speed that is never visited never gets a profile. Two guards break that loop; both are diagnosed in `mpc_reason`.

- **Learning hold** — while the current speed has no measured profile, the change interval is raised to `dead_time × MIN_ESTABLISHED_RATIO + LEARNING_HOLD_EXTRA_MINUTES`, so the learning gate has time to record samples before the speed is left. Released by the emergency escalation (error growing past `DEAD_TIME_ESCALATION_GROWTH`), by an overshoot that keeps worsening, and not applied at all past `MULTI_RANK_JUMP_ERROR`.
- **Climb guard** — a move of more than one rank *up* may not skip an intermediate speed that has no measured profile and a positive estimated slope: that rung is selected instead, because it can only be measured under load. Exceptions: comfort error above `MULTI_RANK_JUMP_ERROR` (a setpoint step is a recovery and belongs on the strongest speed at once) and the emergency escalation. The pre-existing step-down guard (no multi-rank drop to a profile that cannot sustain progress) is unchanged.

## Diagnostics

The MPC exposes:

- recommendation status
- recommendation reason
- recommended fan mode
- whether it would actually change the fan now
- 10-minute and 30-minute predicted temperatures
- confidence percentage
- dead time used by the simulator
- number of reliable learned profiles available
- current disturbance bias estimate

The CSV log also stores the MPC recommendation so we can replay and compare decisions offline.

## Implementation Map

Current files involved:

- `custom_components/vtherm_mpc_fan/mpc_controller.py`: MPC thermal model and cost-based scorer
- `custom_components/vtherm_mpc_fan/thermal_learning.py`: slope samples, response events, profile calibration
- `custom_components/vtherm_mpc_fan/manager.py`: the VTherm feature manager — control cycle, disturbance detection, learning collection, fixed-speed pin
- `custom_components/vtherm_mpc_fan/__init__.py`: integration setup, factory registration, services
- `custom_components/vtherm_mpc_fan/sensor.py` / `number.py`: diagnostic sensors and editable profile slopes
- `custom_components/vtherm_mpc_fan/data_collection.py`: CSV logger for offline analysis

## Next Steps

1. Improve the thermal model with residual correction and better disturbance handling.
2. Add offline replay tooling against recorded CSV traces.
