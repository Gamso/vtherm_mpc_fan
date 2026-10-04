---
name: analyze-control-data
description: "Analyze VTherm MPC Fan CSV data files to diagnose algorithm behavior, identify missed MPC opportunities, detect defrost artifacts, and propose improvements. Use when: the user provides a CSV data file; asking to analyze overnight or morning behavior; asking why the fan did or did not change; evaluating MPC decision quality; diagnosing defrost or setpoint-drop events."
argument-hint: "CSV file path or description of the behavior to diagnose"
---

# Analyze Control Data

## When to Use

- User provides a `vtherm_mpc_fan_data_*.csv` file (written next to `configuration.yaml` in the HA config dir)
- User describes unexpected behavior ("fan stayed on med all night", "slow morning rise")
- User wants to evaluate MPC decision quality
- Diagnosing defrost artifacts, setpoint drops, or window-open disturbances

## Column Reference

Keep this in sync with `_HEADER` in `custom_components/vtherm_mpc_fan/data_collection.py` — that module is authoritative.

| Column | Meaning |
|--------|---------|
| `timestamp` | ISO datetime of each control cycle |
| `hvac_mode` | `heat` or `cool` |
| `current_temp` | Sensor temperature (°C) |
| `target_temp` | Regulated setpoint VTherm sends to the unit (`regulated_target_temperature`, °C) |
| `current_error` | Signed error against `target_temp` (positive = needs action) |
| `vtherm_slope` | EMA-smoothed slope from VTherm (°C/h) |
| `effective_slope` | Slope actually used for decisions (sign-aligned: positive = cooling/heating progress) |
| `projected_temp` / `projected_error` | Simple linear projection from the current slope (not the MPC's own horizon simulation) |
| `phase` | `DEAD_TIME` / `TRANSIENT` / `ESTABLISHED` |
| `minutes_since_change` | Time since the fan mode was last changed |
| `effective_timeout` | Adaptive advisory timeout in use (diagnostic only, does not gate control) |
| `current_fan` | Fan mode at start of cycle |
| `decided_fan` | Fan mode actually applied this cycle (may differ from `mpc_fan` when forced or when the MPC is paused and the current fan is held) |
| `force` | `True` if a manual override (`set_force_fan_mode`) was active |
| `reason` | Full decision reason string (MPC reason, or the pause/force reason) |
| `learning_ready` | `True` when the model has enough samples overall |
| `dead_time` | Learned thermal dead time (minutes) |
| `is_window_open` | `True`/`False` |
| `mpc_status` | `Ready` / `Low confidence` / `Setpoint drop` / `Overshoot` / `Disturbed` / `Idle` / `Unavailable` / `Fixed` / `Forced` |
| `mpc_fan` | Fan mode the MPC itself would choose (before force/pause overrides) |
| `mpc_would_change` | `yes` / `no` — whether the MPC would change the fan mode right now |
| `mpc_cost` | MPC optimization cost of the chosen mode |
| `mpc_confidence` | Profile coverage % |
| `mpc_temp_10m` / `mpc_temp_30m` | MPC 10/30-minute temperature predictions |
| `mpc_known_profiles` | Count of fan modes with a profile of their own (measured — 90 min of regime in ≥ 6 samples — or seeded) |
| `mpc_disturbance` | Current disturbance-bias EMA (°C/h) |
| `defrost_active` | `True` when defrost protection is active — from the underlying's own `hvac_action == "defrosting"`, or the optional defrost entity |
| `hvac_idle` | `True` when the underlying reports it is not producing (its own `hvac_action`, not VTherm's simulated one — see `CLAUDE.md`) |
| `outdoor_temp` | Outdoor temperature reported by VTherm, if available |
| `user_target_temp` | The user's own setpoint (`target_temperature`); `target_temp` above is VTherm's *regulated* setpoint |
| `regulation_offset` | `target_temp − user_target_temp`: how far VTherm's auto-regulation moved the setpoint sent to the unit (raw °C) |
| `comfort_error` | Signed error against `user_target_temp` (positive = needs action) — the error the MPC regulates on |

## Analysis Procedure

### 1. Load and Inspect
- Read the CSV with pandas or csv module
- Check time range, number of rows, unique HVAC modes
- Identify gaps (missing cycles, restarts)

### 2. Key Diagnostic Checks

**Setpoint drop events (`reason` contains "Setpoint drop")**
- Check the `user_target_temp` drop magnitude (a genuine user move is required; a room past an unchanged setpoint reports `Overshoot`)
- Verify `fan_mode` went to lowest mode
- Check MPC also reported "Setpoint drop"

**Defrost events (`defrost_active == True`)**
- Find the slope drop that triggered detection: look for `vtherm_slope` crossing sharply negative
- Verify MPC paused (status should be "Disturbed")
- Check duration: 20-min cooldown from detection

**MPC disturbed periods (`mpc_status == "Disturbed"`)**
- Identify what triggered the disturbance: defrost, window open, HVAC idle
- Check `mpc_confidence` — low confidence = not enough learning data
- Check `mpc_known_profiles` — a speed is measured once its samples cover 90 min of established regime

**Slow temperature recovery**
- Plot `current_temp` vs `target_temp` over time
- Look for unnecessary step-downs (reason contains "Hysteresis" or step-down hold)
- Cross-check with `defrost_active` — was defrost correctly detected?

**Night setpoint drop mismatch (MPC on "med" instead of "silent")**
- Filter to period where `target_temp` dropped significantly
- Check `mpc_status` — should be "Setpoint drop" with lowest fan
- If not, check whether defrost or idle paused the MPC

### 3. Report Format

Provide a narrative structured as:
1. **Period summary**: time range, hvac mode, temperature trajectory
2. **Key events**: defrost cycles, setpoint drops, fan changes — with timestamps
3. **Algorithm assessment**: MPC decisions triggered correctly? any missed steps?
4. **MPC quality**: confidence levels, key disturbance periods and their cause
5. **Recommendations**: concrete parameter changes or code fixes if warranted

## Code Hints

- Load CSV: `pd.read_csv(path, parse_dates=["timestamp"])`
- Filter defrost: `df[df["defrost_active"] == True]`
- Find mode changes: `df[df["current_fan"] != df["decided_fan"]]`
- MPC disturbance: `df[df["mpc_status"] == "Disturbed"]`
- Reason summary: `df["reason"].value_counts()`
