# MPC Controller — Technical Design

## Purpose

The MPC controller is the sole decision engine for fan speed.
It maintains a learned thermal model, scores every candidate fan mode over a 30-minute horizon, and selects the mode with the lowest cost.
When MPC status is actionable (`Ready`, `Setpoint drop`, `Low confidence`), the integration applies the fan recommendation. When paused (`Disturbed`, `Idle`, `Not ready`), the current fan mode is held.

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

1. Read VTherm temperature, target, slope, HVAC mode, and current fan mode.
2. Detect disturbances (defrost, HVAC idle, window open).
3. Compute phase (DEAD_TIME / TRANSIENT / ESTABLISHED).
4. Run `MPCController.evaluate(...)` → `mpc_decision` dict.
5. Push `mpc_decision` to all sensors via `sensor.update_from_mpc()`.
6. Collect learning data (slope samples, response events) with gating.
7. Append MPC information to the CSV log.
8. Apply the MPC fan recommendation when the MPC status is actionable (`Ready`, `Setpoint drop`,
   `Low confidence`); hold the current fan otherwise (e.g. status `Not ready`,
   `Disturbed`, `Idle`).

### HA Entities

| Platform | Entity ID                                                      | Purpose                                            |
| -------- | -------------------------------------------------------------- | -------------------------------------------------- |
| `sensor` | `sensor.smart_fan_controller_mpc_status`                       | Current MPC recommendation status                  |
| `sensor` | `sensor.smart_fan_controller_mpc_reason`                       | Human-readable reason for the recommendation       |
| `sensor` | `sensor.smart_fan_controller_mpc_fan_mode`                     | Recommended fan mode                               |
| `sensor` | `sensor.smart_fan_controller_mpc_would_change_now`             | Whether MPC would change the fan this cycle        |
| `sensor` | `sensor.smart_fan_controller_mpc_cost`                         | Best cost score for the recommended mode           |
| `sensor` | `sensor.smart_fan_controller_mpc_confidence`                   | Confidence percentage                              |
| `sensor` | `sensor.smart_fan_controller_mpc_predicted_temperature_10_min` | 10-minute temperature forecast                     |
| `sensor` | `sensor.smart_fan_controller_mpc_predicted_temperature_30_min` | 30-minute temperature forecast                     |
| `sensor` | `sensor.smart_fan_controller_mpc_dead_time`                    | Dead time used by the simulator (min)              |
| `sensor` | `sensor.smart_fan_controller_mpc_known_profiles`               | Number of reliable learned profiles                |
| `sensor` | `sensor.smart_fan_controller_mpc_disturbance_bias`             | Slow disturbance correction term                   |
| `sensor` | `sensor.smart_fan_controller_mpc_heat_profiles`                | Per-mode learned profiles for heat                 |
| `sensor` | `sensor.smart_fan_controller_mpc_cool_profiles`                | Per-mode learned profiles for cool                 |

CSV log fields are prefixed with `mpc_*`.

The MPC controller owns its runtime parameters (`deadband`, `min_interval`, `fan_modes`) and consumes learned profiles from `ThermalLearning`.

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

- `alpha = 0.35` in the current scaffold
- `delta_t = 2 min`
- `raw_slope = +q_hat` in heating and `-q_hat` in cooling

### Source of Learned Parameters

The model reuses the current learning subsystem:

- `learning.get_dead_time()` for thermal delay
- `learning.get_mode_slope_model(fan_mode, hvac_mode)` for reliable per-mode profiles

If a reliable profile is not yet available for a fan mode, the MPC model falls back to a coarse rank-based estimate derived from the current slope. Confidence is lowered accordingly.

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

- `custom_components/smart_fan_controller/mpc_controller.py`: MPC thermal model and cost-based scorer
- `custom_components/smart_fan_controller/thermal_learning.py`: slope samples, response events, profile calibration
- `custom_components/smart_fan_controller/__init__.py`: control loop, learning collection, services
- `custom_components/smart_fan_controller/sensor.py`: MPC and learning sensors exposed in Home Assistant
- `custom_components/smart_fan_controller/data_collection.py`: CSV logger for offline analysis

## Next Steps

1. Improve the thermal model with residual correction and better disturbance handling.
2. Add offline replay tooling against recorded CSV traces.
