# MPC Controller — Technical Design

## Purpose

The MPC controller is the sole decision engine for fan speed.
It maintains a learned thermal model, scores every candidate fan mode over a horizon of the dead time plus 60 minutes, and selects the mode with the lowest cost.
When MPC status is actionable (`Ready`, `Setpoint boost`, `Setpoint drop`, `Overshoot`, `Low confidence`), the integration applies the fan recommendation. When paused (`Disturbed`, `Idle`, `Unavailable`), the current fan mode is held. The MPC only regulates `heat` and `cool`; in any other HVAC mode it reports `Idle`, unless that mode has a fixed fan speed (status `Fixed`, set by the feature manager, not by the MPC). A `force_fan` override reports `Forced`.

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
   applied only when its status is actionable (`Ready`, `Setpoint boost`, `Setpoint drop`, `Overshoot`, `Low confidence`), the
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

A **setpoint boost** is the opposite move: the user's setpoint asks for at least
`SETPOINT_BOOST_DEMAND` (1 °C) more between two cycles — the evening drop from 24 to 22 °C in cool.
The MPC returns the strongest speed at once (status `Setpoint boost`, min interval bypassed) and keeps
it until the comfort error is back within the deadband; a move back, a mode change or `off` cancels it.
A smaller raise of at least the deadband only lets a climb bypass the min interval.

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

Each profile therefore learns a linear model over its `(comfort_error, effective_slope)` samples:

```text
effective_slope(error) = a + b * error      (b clamped to >= 0)
```

- **Robust fit**: `b` is the weighted median of the pairwise slopes (Theil–Sen; at most
  `THEIL_SEN_MAX_POINTS` samples, evenly spread in time), `a` the weighted median of `y − b·x`;
  legacy samples weigh `LEGACY_SAMPLE_WEIGHT`.
- **Autocorrelation and shrinkage**: `n_eff = Σw / (1 + 2ρ)`, ρ the lag-1 autocorrelation of the
  residuals within a run of samples; `b ← (n_eff·b + GAIN_PRIOR_SAMPLES·b_pool) / (n_eff +
  GAIN_PRIOR_SAMPLES)`, `b_pool` the within-speed Theil–Sen gain pooled over the HVAC mode's speeds.
- **No extrapolation**: the envelope `error_max` is the largest error measured;
  `get_mode_effective_slope()` reports the line at `reference_error = min(REFERENCE_SLOPE_ERROR,
  error_max)`, and the simulator caps the error at `error_max`. A profile with `error_max <
  REFERENCE_SLOPE_ERROR` is *partial* (`is_profile_partial()`); the multi-rank step-down guard treats
  it as unmeasured.
- **Uncertainty**: `slope_sigma = 1.4826·MAD(residuals) / sqrt(n_eff)`, inflated by the distance of
  the reference error from the samples' mean error; a seed carries `SEEDED_SLOPE_SIGMA`.
- **Regulation offset**: an optional shrunk term `c·(offset − (α + β·error))` (see *Comfort setpoint*).
- `learning.get_mode_fit()` returns the whole `ProfileFit`; `get_mode_slope_model()` returns `(a, b)`.
- The simulator recomputes the slope **at each step** from the simulated error, so the projection
  decelerates realistically as the room approaches the setpoint instead of cooling/heating at a fixed
  rate (which produced phantom overshoot past the target).

### Monotone ladder

`build_monotone_slopes()` is a weighted isotonic regression (pool-adjacent-violators) over the speed
ranks, each learned profile weighing its `n_eff` (a seed `MIN_MODE_PROFILE_SAMPLES`). Inside a pooled
block — or between two profiles learned at exactly the same value — the heaviest profile keeps the
block value and the others are separated one `LADDER_CAPACITY_RATIO` step per rank, respaced evenly
when that would pass halfway to the neighbouring block. With `sample=True` (option
`thompson_sampling`), each value is first drawn from `N(value, slope_sigma²)`.

### Cold start

While no profile of the HVAC mode exists at all (neither measured nor seeded), the decision is a step
law on the comfort error (`STEP_LAW_ERROR_PER_RANK` = 0.3 °C per rank beyond the deadband, never down
while short, one rank down past the deadband on the other side), under the usual min interval,
learning hold and escalation. The simulation still runs for the forecasts and diagnostics.

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

The controller simulates every available fan mode over `dead_time + DEFAULT_HORIZON_MINUTES` (60)
minutes, in 2-minute steps. The candidate fan action is held constant over the horizon, but its
effective slope is recomputed at each step from the simulated comfort error (see the gap-dependent
slope model above). During the first `dead_time` minutes **every** candidate — the current speed
included — follows the observed slope, then its own model: staying and switching start from the same
trajectory. The observed slope is VTherm's, bounded by `sensor_resolution × 60 / t` once the reading
has not changed for `t` minutes (`_bounded_slope`): VTherm only recomputes its slope when the sensor
publishes, so a room that stopped moving would otherwise keep the slope of its last step for hours. The simulator supports both `heat` and `cool`; cooling uses the same learned
effective-power model with the sign inverted back to room-temperature evolution.

Each candidate mode gets a scalar cost:

```text
J(mode) =
    mean over steps of [ comfort_error × urgency
                         + 3.0 × overshoot²
                         + 12.0 × urgency × floor_violation + 30.0 × floor_violation²
                         + 160 × (16 if t > 0 else 1) × huber_0.3(t) ]
  + 0.15 × fan_step_distance
  + 0.1 × 1.82^rank × (0.15 inside the hold zone)
```

Where, with `e` the simulated comfort error against the user's setpoint (positive = short):

- `comfort_error = max(|e| - deadband, 0)`, urgency `1 + 2 × comfort_error`
- `overshoot = max(-e - tolerance, 0)` — going past target (`tolerance` = 0.3 inside the hold zone, else 0)
- `floor_violation = max(e, 0)` — any shortfall: below the setpoint in heat, **above** it in cool
- `t = e + TRACKING_TARGET_OFFSET` — the distance to a tracking target 0.12 °C inside the band on the
  comfortable side (below the setpoint in cool, above it in heat); `huber_0.3` is quadratic up to
  0.3 °C, linear beyond, and the short side weighs 16 times the other (`TRACKING_*`, `_tracking_cost`)
- `fan_step_distance` penalizes unnecessary fan jumps
- the energy term grows geometrically with the rank and is a tie-breaker: one rank is worth about
  0.01–0.03 °C of sustained tracking error

Comfort comes first. The deadband is a **decision** hysteresis (switch margins, hold zone,
escalation), not a zone the cost ignores: with a free deadband the controller let the room sit at its
warm edge in cool, and a 0.2 °C sensor plus a 10-minute actuator delay turned every load bump into time
spent too warm. The asymmetric tracking term makes an excursion to the short side cheaper to prevent
than to correct. Averaging over the steps keeps the costs, the energy term and the hysteresis margins
on one scale whatever the dead time. The minimum interval is a gate (see below), not a cost. The
weights were calibrated on two closed-loop plants (see [Calibration](#calibration)).

The selected mode is the one with the lowest total cost.
A recommendation that changes the fan must also beat the current mode by a minimum gain
(`_required_switch_gain`: 0.05 far from target, 0.075 approaching, 0.125 inside the deadband, +0.05
outside the established phase, +0.025 per rank, +0.4 + 1.0/°C for a step down while short of the
tracking target). These margins only filter out ties; stability comes from the guards below, which do
not trade comfort for fewer changes. If the gain is only marginal, the MPC controller keeps the current
fan and reports that hysteresis blocked the switch.

Step-down guards, all reported in `mpc_reason`:

- **Below target** (`_step_down_hold_note`) — while the room is short of the tracking target, a step
  down waits for the established phase and for a predicted shortfall at 10 minutes under the reserve.
- **Within the band** — while the comfort error is above `-deadband`, a weaker speed that the model
  itself sees losing ground after the dead time (predicted error growing from 10 to 30 minutes) is not
  taken: the cost finds it harmless over the horizon, but by the time the coarse reading shows the
  drift and a stronger speed lands, the room is past the band.
- **Multi-rank drop** — a drop of more than one rank goes only to a speed whose own full profile
  sustains progress (`MIN_VIABLE_MULTI_RANK_STEPDOWN_SLOPE`); a speed with no profile or a partial one
  does not qualify, except in an Overshoot. While the room is short of the setpoint, every step down is
  one rank at a time.

An unlearned candidate is estimated from the nearest learned profile along the ladder, then bounded by
the current speed (`_bound_by_current`): a weaker speed is never rated above the speed running now, a
stronger one never below.

### Escalation

The min interval can be bypassed upward when the comfort error has grown since the last change by more
than `escalation_threshold = max(DEAD_TIME_ESCALATION_GROWTH, 1.5 × sensor_resolution)` on
`ESCALATION_CONFIRM_CYCLES` (2) consecutive cycles, or on the first cycle once the comfort error exceeds
`MULTI_RANK_JUMP_ERROR`. `sensor_resolution` is the smallest non-zero change between two consecutive
readings over the last `SENSOR_RESOLUTION_WINDOW` cycles, bounded to [0.05, 0.5], 0.2 until three
changes have been seen.

### Exploration guards

A purely cost-driven controller never visits a speed it has no profile for, and a speed that is never visited never gets a profile. The guards and strategies below break that loop; all are diagnosed in `mpc_reason`.

- **Learning hold** — while the current speed has no measured profile, the change interval is raised to `gate_dead_time × MIN_ESTABLISHED_RATIO + LEARNING_HOLD_EXTRA_MINUTES` (`gate_dead_time` = the learned dead time capped at `DEAD_TIME_MAX_FOR_GATE`, 15 min), so the learning gate has time to record samples before the speed is left. Released by the confirmed escalation, by an overshoot that keeps worsening past the escalation threshold, by an abandoned probe, and not applied at all past `out_of_band_error` (`deadband + 0.5 × sensor_resolution`: the first reading past the band edge on the short side — comfort first) or below `THRESHOLD_TARGET_DROP`.
- **Climb guard** — a move of more than one rank *up* may not skip an intermediate speed that has no measured profile and a positive estimated slope: that rung is selected instead, because it can only be measured under load. It applies only while the room is within the band: past `out_of_band_error`, and on an emergency escalation, the climb is decided on cost alone.
- **S1, exploration probe** (`exploration_probe`, on by default) — when the final decision is to stay, the comfort error is within the deadband, the phase is `ESTABLISHED`, `|bias| < PROBE_MAX_BIAS` (0.1 °C/h), the speed one rank below has no measured profile and was not probed for `PROBE_INTERVAL_HOURS` (6 h; `ThermalLearning.record_probe` / `last_probe_time`, persisted as `probe_times`/`probe_count`), the controller switches to it. The learning hold keeps it; the probe is abandoned (hold released, change allowed at once) when the comfort error exceeds `deadband + sensor_resolution`.
- **S2, information bonus** (`exploration_ucb`, off) — inside the deadband, `INFO_BONUS / sqrt(1 + measured_minutes / SAMPLE_INTERVAL_MINUTES)` (1.0) is subtracted from each candidate's cost.
- **S3, measurement under load** (`exploration_under_load`, off) — when a climb survives the guards outside a recovery (no escalation, comfort error ≤ `MULTI_RANK_JUMP_ERROR`), the lowest unmeasured rung on the way whose slope at the current error plus the bias is positive is selected instead.

## Dead time

A response event is the time from a fan change to the first move of the room temperature of at least
one `sensor_resolution` in the expected direction (`MpcFanFeatureManager._detect_response`): the
direction is +1 for a stronger speed, −1 for a weaker one, from the ladder; an event is recorded
between 2 and 60 minutes, in `heat`/`cool`, undisturbed. The median per HVAC mode sets the horizon and
the adaptive change interval; `MPCController.gate_dead_time()` caps it at `DEAD_TIME_MAX_FOR_GATE` for
the learning gate, the phase split and the learning hold.

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

## Validation

- `tests/closed_loop.py` is a closed-loop plant shaped on the production trace (see
  [Calibration](#calibration)); `tests/test_closed_loop.py` compares the controller, per variant and
  seed, with the 8d7bccd one replayed on the same plant (comfort MAE, time within ±0.2 °C, time too
  warm, evening descent, within documented margins), checks the evening setpoint boost, reports the T+30
  forecast error next to persistence, and drives the real feature manager from a cold-ish start to
  check that every speed gets measured.
- `scripts/replay_bench.py` replays recorded CSV traces in open loop: decisions are compared, the
  temperature does not react to them. It reports the forecast MAE next to the persistence baseline.

## Calibration

The cost weights, the margins and the guards were chosen on two closed-loop plants, comfort first,
against the 8d7bccd controller replayed on the same plant, variant and seed. Comfort is measured on the
**true** room temperature against the **user's** setpoint, over the minutes the unit cools. The
number of fan changes was not a target.

**`tests/closed_loop.py`** — cooling 10:00–24:00, user setpoint 24 °C then 22 °C from 21:30, the room
relaxing toward 26 °C at night; VTherm's auto-regulation integrating the comfort error into an offset
(down to −0.8 °C) on which the inverter unit regulates; a 10-minute actuator delay and a 6-minute power
lag; a 0.2 °C sensor publishing on change and VTherm's EMA slope. Seven variants × 3 seeds × 72 h, true
profiles seeded (`partial`: low and silent unknown). Ranges over the seeds:

| Variant   | Controller | Comfort MAE (°C) | ±0.2 °C (%) | Too cold (%) | Too warm (%) | Descent (min) | Changes/h | Superhigh (%) |
|-----------|------------|------------------|-------------|--------------|--------------|---------------|-----------|---------------|
| base      | 8d7bccd    | 0.31–0.32        | 50–59       | 22–32        | 18.1–18.7    | 94–99         | 0.57–0.79 | 41–46         |
|           | current    | 0.27             | 60–65       | 16–22        | 18.5–18.7    | 93–94         | 1.24–1.50 | 26–32         |
| load0.3   | 8d7bccd    | 0.36–0.37        | 41–44       | 39–42        | 16.9–17.0    | 91            | 0.69–0.76 | 32            |
|           | current    | 0.26             | 59–60       | 23–25        | 16.5         | 84            | 0.69–0.74 | 19            |
| load0.7   | 8d7bccd    | 0.34             | 51–52       | 26–27        | 22.1–22.5    | 114–115       | 0.48      | 69–70         |
|           | current    | 0.29–0.30        | 62–66       | 10–16        | 22.3–23.5    | 112–114       | 1.19–1.33 | 50–53         |
| load0.85  | 8d7bccd    | 0.35–0.37        | 47–48       | 26–27        | 26.0–26.6    | 131–137       | 0.40–0.45 | 73–78         |
|           | current    | 0.30             | 64–65       | 9–10         | 25.4–25.8    | 124–126       | 1.17–1.21 | 60–65         |
| gain0.2   | 8d7bccd    | 0.45–0.48        | 24–36       | 37–48        | 27.3–27.5    | 150           | 0.69–0.81 | 39–43         |
|           | current    | 0.33–0.34        | 67–69       | 4–6          | 27.3–27.4    | 150           | 1.45–1.50 | 36            |
| slowdelay | 8d7bccd    | 0.37             | 39–40       | 39–40        | 20.1–21.4    | 98–103        | 0.76      | 51–53         |
|           | current    | 0.31–0.34        | 45–58       | 22–35        | 20.1–20.3    | 97–100        | 1.14–1.29 | 33–35         |
| partial   | 8d7bccd    | 0.37–0.38        | 44–48       | 33–37        | 18.7–18.8    | 99–102        | 0.55–0.62 | 35–36         |
|           | current    | 0.36             | 41–42       | 39           | 19.1–19.4    | 99–102        | 0.45      | 24–25         |

Per variant × seed (21 runs): comfort MAE lower on all 21, time within ±0.2 °C higher on 18 (not on
`partial`, which stays on the cool side for want of a profile to step down on), time too warm at
most +1.2 points, evening descent at most +1.7 min. The residual warm time comes from the morning
restart: the unit resumes on the speed it stopped on, which is a weak one for a controller that does not
overcool the evening, so the first 10–15 minutes of the 10:00 recovery run on it.

**Trace-calibrated bench** (outside the repository) — the identified plant of the production trace
(20 Aug–4 Oct 2026, 3 variants × 3 seeds, every session replayed with its recorded outdoor
temperature and schedule, a VTherm PI model) driving the real feature manager, with no prior learning
(`none`) or with the state 8d7bccd had learned (`legacy`). With no prior learning, ranges over the
seeds:

| Variant | Controller | Comfort MAE (°C) | ±0.2 °C (%) | Too cold (%) | Too warm (%) | Too warm at 24 °C (%) | Too warm at 22 °C (%) | Descent (min) | Changes/h | Superhigh (%) |
|---------|------------|------------------|-------------|--------------|--------------|-----------------------|-----------------------|---------------|-----------|---------------|
| V1      | 8d7bccd    | 0.66–0.69        | 30–36       | 46–53        | 15.6–17.6    | 4.6–5.7               | 48–54                 | 71–80         | 0.90–0.92 | 27–33         |
|         | current    | 0.66–0.68        | 31–36       | 48–53        | 15.5–16.6    | 4.5–5.2               | 47–52                 | 70–75         | 0.70–0.82 | 28–38         |
| V2      | 8d7bccd    | 0.64–0.67        | 30–37       | 45–53        | 15.9–18.1    | 4.6–6.3               | 48–54                 | 69–80         | 0.91–1.07 | 32–37         |
|         | current    | 0.63–0.66        | 32–38       | 45–52        | 15.8–17.7    | 4.8–6.0               | 47–54                 | 69–79         | 0.74–0.80 | 32–42         |
| V3      | 8d7bccd    | 0.64–0.68        | 30–37       | 45–53        | 15.6–17.8    | 5.2–6.2               | 47–53                 | 70–78         | 0.82–1.09 | 31–37         |
|         | current    | 0.61–0.64        | 33–40       | 43–50        | 15.9–17.5    | 5.1–6.0               | 46–53                 | 68–80         | 0.76–0.77 | 30–39         |

Per variant × seed, both starts (18 runs): comfort MAE lower or equal on 17 (+0.001 on one), time
within ±0.2 °C higher on 15 (−0.3 to −0.6 points on the others), time too warm at 22 °C lower or equal
on all 18, time too warm at 24 °C lower or equal on 11 (at most +0.6 points), evening descent (median)
shorter or equal on 15 (+1.5 to +3 min on the others: the room starts the descent a little warmer than
under 8d7bccd, which overcooled the day). The mean comfort error on this bench is dominated by the
22 °C slot, which the unit cannot reach before midnight on most evenings.
