# Changelog

## 1.2.0 — 2026-10-31

Changes since 1.1.0. The regulation was recalibrated to put comfort first, learning was reworked to measure fan speeds reliably on coarse room sensors, and a fixed fan speed can now be pinned per HVAC mode.

### ⚠ Breaking changes

- Removed the `vtherm_mpc_fan.apply_learned_settings` service and the `sensor.vtherm_mpc_fan_*_learned_deadband` entity. The "learned deadband" was a statistic of the slope spread that nothing in the controller read. An existing installation keeps an orphaned, unavailable entity that can be deleted by hand.

### New features

- **Fixed fan speed per HVAC mode**: pin a fan speed in chosen HVAC modes (dry, fan only, ...); the MPC regulates only in heat/cool. The pin is held while the unit is stopped, a window is open or the unit is defrosting, and never feeds learning.
- **Setpoint boost**: when the user asks for ≥ 1 °C more demand (for example 24 → 22 °C in the evening), the strongest speed is sent until the room is back within the deadband (new status `Setpoint boost`).
- **Exploration of unmeasured speeds**: an opportunistic downward probe (on by default, `exploration_probe`) tries one rank down to a never-measured speed while the room holds. Optional UCB bonus (`exploration_ucb`), measurement under load (`exploration_under_load`) and Thompson sampling (`thompson_sampling`) are available, off by default. New `mpc_exploration_probes` diagnostic.
- **Standing down for VTherm's built-in auto-fan**: the plugin no longer fights VTherm's own auto-fan when it is enabled.
- **VTherm log export**: the plugin now logs through `vtherm_api`, so its lines (and "NEW EVENT" lines for each fan command and `force_fan` override) appear in the log export attached to bug reports.
- **Data collection** CSV now also logs the user setpoint, the regulation offset and the comfort error.
- Number entities and profile summaries expose `slope_sigma`, `reference_error`, `partial`, `effective_samples` and `measured_minutes`. New options are available in the config and options flows (en/fr).

### Control improvements

- **Comfort is measured against the user's setpoint**, not VTherm's regulated setpoint, which auto-regulation drifts away from.
- **Comfort-first cost**: an asymmetric Huber tracking term pulls the prediction to 0.12 °C inside the band on the comfortable side; the deadband is a decision hysteresis rather than a free zone; the fan-rank cost and switch margins are rescaled; the horizon is 60 min. The number of fan changes is no longer a target.
- **Resolution-aware escalation**: the room sensor resolution is auto-detected and a single sensor step no longer triggers an escalation or breaks a hold.
- **Guards**: no step down to a speed the model sees losing ground after the dead time; multi-rank drops only to a full, viable profile and one rank at a time while short of the setpoint; the learning hold and climb guard release once the room is out of the band.
- **Observed slope bounded** by the sensor resolution and the time since the reading last changed (VTherm keeps a stale slope while the sensor is silent).
- **Cold start**: while no speed of the HVAC mode has a profile, a step law on the comfort error replaces the cost-based choice.
- **Dead time measured on temperature**: a response event is the first move of at least one sensor step in the expected direction (per HVAC mode). The gate and learning hold use it capped at 15 min.

### Learning improvements

- **Time-based sampling**: one slope sample per 10 min of established regime even when the slope has not moved, so a speed that holds the room still can finally be measured. A profile is *measured* once its samples cover 90 min in ≥ 6 samples.
- **Robust fits**: weighted Theil–Sen replaces OLS, gain shrunk toward the pooled gain with an autocorrelation-aware effective sample size.
- **No extrapolation**: slopes are evaluated at `min(1 °C, largest error measured)`; a profile that never reached 1 °C is *partial*.
- **Monotone ladder** via weighted isotonic regression (PAV); tied speeds are separated by one capacity step.
- Learning gaps fixed: dead time restricted to heat/cool and trusted per HVAC mode; cheaper per-profile fits.

### Fixes

- Fixed-fan: no forced pin on a manager's first cycle, no dead-end config form, honest HVAC mode fallback.
- Setpoint-drop cooldown starts in heat/cool only.
- `is_detected` reports MPC regulation only.
- `set_effective_slope` and `force_fan` service inputs are bounded.
- Only the targeted VTherm is reloaded when the last entry unloads.
- Options-flow errors moved under `options.error` in translations.
- The factory accepts any read-only mapping as `entry_infos`.
- HACS: dropped the `domain` key from `hacs.json`.

### Project

- Supports the `vtherm_api` 0.4.0 surface (Versatile Thermostat ≥ 10.2.0); CI tests the latest release and the 0.4.0 floor, and runs black, pylint, hassfest and HACS validation.
- Closed-loop test plant calibrated on a production trace; tests cover `__init__`, factory and flows on a real Home Assistant core.
- MIT `LICENSE` added; README, `docs/mpc_mode.md` (new Calibration section) and `CLAUDE.md` aligned with the code.

## 1.1.0

Intermediate fan speeds can be measured: exploration guards and learning-data fixes.

## 1.0.0

First release.
