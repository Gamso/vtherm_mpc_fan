"""MPC controller: learned thermal model with cost-based fan selection."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from .const import (
    DEAD_TIME_SAFETY_FACTOR,
    DEFAULT_DEAD_TIME,
    DEFAULT_CYCLE_MINUTES,
    MIN_ESTABLISHED_RATIO,
    PHASE_DEAD_TIME,
    PHASE_ESTABLISHED,
    PHASE_TRANSIENT,
    PROFILE_HVAC_MODES,
    REFERENCE_SLOPE_ERROR,
    THRESHOLD_TARGET_DROP,
)
from .thermal_learning import ThermalLearning

_LOGGER = logging.getLogger(__name__)

# Cost function weights (simulation loop in _simulate_mode)
COMFORT_ERROR_WEIGHT = 1.0
OVERSHOOT_QUADRATIC_WEIGHT = 3.0
FLOOR_VIOLATION_LINEAR_WEIGHT = 12.0
FLOOR_VIOLATION_QUADRATIC_WEIGHT = 30.0
MODE_CHANGE_DISTANCE_COST = 0.15
MODE_RANK_COST = 0.05
# Geometric growth of relative power draw per fan-mode rank. ~6**(1/3) so a
# 4-mode ladder reproduces the legacy [1.0, 1.5, 3.0, 6.0] power scaling while
# extending naturally to any number of modes.
MODE_POWER_RATIO = 1.82
MIN_INTERVAL_CHANGE_PENALTY = 25.0
URGENCY_SENSITIVITY = 2.0

# The configured min_interval is a floor; once the dead time is trusted the
# effective dwell before a fan change is raised toward it (you cannot observe a
# change's effect faster than the dead time, so changing sooner just invites
# oscillation). The rise is capped at this factor x the configured floor so a
# spuriously large learned dead time cannot stall the controller.
MAX_ADAPTIVE_INTERVAL_FACTOR = 3.0

# How many recorded response events the dead time must rest on before it is
# allowed to raise the change interval above the configured floor.
#
# Deliberately NOT gated on ``ThermalLearning.is_ready()``, which counts *slope*
# samples against MIN_SAMPLES_LEARNING. That global counter lags far behind the
# thing actually being asked about here: on a 24 h production trace every
# per-mode profile was learned and the dead time was well established at
# 17.5-28.5 min, yet is_ready() stayed false the whole time, so the interval sat
# at its 10-minute floor and the controller kept re-deciding roughly twice per
# dead time -- acting before it could measure. `_compute_confidence` already
# carries the same lesson in its own docstring. Room sensors with coarse
# resolution (0.2 degC steps are common) make slope samples accumulate slowly
# enough that is_ready() can stay false ~indefinitely, so it is the wrong gate.
#
# A handful of events is enough: this only ever raises a dwell time, the
# emergency-escalation path (DEAD_TIME_ESCALATION_GROWTH) can still bypass the
# lock when comfort is actually degrading, and the factor above caps the rise.
MIN_RESPONSE_EVENTS_FOR_ADAPTIVE_INTERVAL = 5

# The dead-time lock above has no visibility into whether the current fan mode
# is actually holding comfort — a misjudged step (e.g. a multi-rank drop) can
# leave the room drifting away from target for the full adaptive interval with
# no escape. A static error threshold does not fit every deadband/system, so
# instead this tracks how much the comfort error has *grown* since the fan
# last changed (comparing against the error at that moment) — a direct read of
# the room's actual trajectory rather than an arbitrary absolute cutoff. Past
# this much growth, an *escalation only* (never a step-down) is allowed to
# bypass the lock.
DEAD_TIME_ESCALATION_GROWTH = 0.15  # degC comfort error allowed to worsen since the change

# --- Exploration: give an unmeasured speed the time it needs to be measured ---
# The learning gate only accepts slope samples once a fan mode has been active
# for MIN_ESTABLISHED_RATIO x dead_time, while the change gate above re-opens at
# 1 x dead_time. A speed with no measured profile could therefore be left before
# its first sample was recordable -- and a speed that is never measured is never
# credible on cost, so it is never chosen again: a loop closed on itself. While
# the current speed has no measured profile, the dwell is raised to the learning
# gate plus this much time for samples to land. In minutes, not control cycles:
# VTherm recomputes the slope on sensor events, so the sampling rate is the
# room sensor's, whatever cadence the controller is driven at. Comfort keeps its
# escape hatches: the emergency escalation (error growing past
# DEAD_TIME_ESCALATION_GROWTH) and its mirror for overshoot (see
# _learning_hold_active) both release the hold.
LEARNING_HOLD_EXTRA_MINUTES = 10.0

# Rank jumps toward a speed *above* the current one may not skip over an
# intermediate rung that has no measured profile and looks viable (positive
# estimated slope): that rung must be tried first, since it can only ever be
# measured under load and load is exactly what a rising error means. Two
# exceptions keep recovery direct: a comfort error beyond this many degrees
# (a setpoint step -- the evening pre-cool on the production trace runs at
# +1.7 degC and belongs on the strongest speed at once), and the emergency
# escalation. Measured on 23 days of production: 26 of 29 climbs to the top
# speed came straight from the two weakest ones.
MULTI_RANK_JUMP_ERROR = 1.0  # degC

# --- Hold-equilibrium (economic) mode -------------------------------------
# When enabled, near the setpoint the controller matches fan output to the
# system's steady thermal production instead of collapsing to the lowest speed.
# Rationale: on a running heat pump the compressor draws the dominant power and
# keeps producing cold/heat regardless of fan speed, so the fan-rank penalty
# (which models fan watts) optimises the wrong term there. Holding the room flat
# with a steady speed avoids the drift-then-blast cycle and lets an inverter
# compressor modulate at high COP instead of short-cycling. A small, bounded cold
# undershoot is tolerated so the holding speed can slightly lead the load rather
# than lag it — this is what lets a discrete speed ladder actually hold.
# Enabled by default: on the 899 h production trace it shifted ~14% of time from
# `low` to a steady `med` hold and cut fan changes 357->313 with no change in
# predicted comfort (MAE T+10) or average cost. The feature is dormant whenever
# the error exceeds the deadband, so far-from-setpoint escalation is untouched.
HOLD_EQUILIBRIUM = True
HOLD_UNDERSHOOT_TOLERANCE = 0.3  # °C of free wrong-side undershoot inside the hold zone
HOLD_RANK_SCALE = 0.15  # shrink the fan-rank (energy) penalty to a tie-breaker in the zone

# Disturbance bias tracker
DISTURBANCE_EMA_ALPHA = 0.2
DISTURBANCE_DECAY = 0.85
MAX_DISTURBANCE_BIAS = 2.0

# Hysteresis margins
BASE_SWITCH_GAIN_MARGIN = 0.1
NEAR_TARGET_SWITCH_GAIN_MARGIN = 0.3
APPROACHING_TARGET_SWITCH_GAIN_MARGIN = 0.15
PHASE_SWITCH_MARGIN_BONUS = 0.1
STEP_SWITCH_MARGIN = 0.05
UNDER_TARGET_STEPDOWN_GAIN_MARGIN = 0.2
UNDER_TARGET_STEPDOWN_GAIN_PER_DEG = 0.5
UNDER_TARGET_SHORTFALL_RESERVE = 0.1

# Guard against jumping straight to a fan mode more than one rank below the
# current one when that candidate's own learned profile shows it cannot
# sustain progress (net effective slope <= 0 at the reference error). The
# cost-based forecast cannot be trusted for this: during the dead-time-blind
# window (see sim_horizon below) every candidate's near-term trajectory is
# dominated by the *current* mode's momentum, not the candidate's own
# behaviour, so a weak mode can look deceptively good right up until the
# switch is committed. Adjacent-rank switches are left untouched — this only
# blocks multi-rank plunges to a mode with no track record of holding.
MIN_VIABLE_MULTI_RANK_STEPDOWN_SLOPE = 0.0

# Integration step of the predicted temperature trajectory, in minutes. This is a
# numerical-accuracy choice and is deliberately independent of how often the
# controller is actually invoked: the two used to share one constant only because
# both happened to be 2 minutes. Once the control cadence became VTherm's
# ``cycle_min`` (5 minutes by default), reusing it here coarsened the 30-minute
# horizon from 15 steps to 6, which changed which fan mode won on cost -- in the
# dead-time escalation scenario the coarse grid made a too-weak `low` look
# adequate, so the controller had nothing to escalate to. How finely we predict
# and how often we act are separate concerns; keep this fine.
SIMULATION_STEP_MINUTES = 2

# First-order lag applied to the predicted thermal output at each simulation
# step: the room's response ramps toward the candidate's slope rather than
# snapping to it, so a fan change is not modelled as instantaneous. 0 would
# freeze the trajectory at the current slope, 1 would remove the ramp entirely.
THERMAL_POWER_BLEND = 0.45

# Capacity spacing between adjacent fan speeds, used when a profile's own
# estimate is discarded for violating the strength order (see
# build_monotone_slopes). Collapsing the rejected speed onto its neighbour's
# value would leave the MPC unable to tell them apart thermally, so the only
# remaining tie-breaker would be the energy term — which always prefers the
# weaker rank. That starves the stronger speed of samples, which keeps its
# profile thin, which keeps it being rejected: a self-reinforcing trap. Placing
# it one geometric step away instead keeps the ladder ordered *and* separated.
# Calibrated on the production ladder, whose measured ratios between adjacent
# speeds are 2.08 (med->high) and 1.76 (high->superhigh), geometric mean ~1.9.
# Deliberately a separate constant from MODE_POWER_RATIO: that one models
# electrical draw (~RPM^3), this one thermal delivery (~airflow), and they
# should be tunable independently even though they happen to sit close together.
LADDER_CAPACITY_RATIO = 1.8

# Multiplicative spacing collapses around zero (0 / anything is still 0), which
# would reintroduce the very equality the ratio exists to avoid. Below this much
# separation, fall back to an absolute step.
MIN_LADDER_SEPARATION = 0.05  # degC/h


@dataclass(slots=True)
class ModeSimulation:
    """One candidate fan-mode simulation over the MPC horizon."""

    fan_mode: str
    total_cost: float
    predicted_temp_10m: float
    predicted_temp_30m: float
    known_profile: bool


class MPCController:
    """Background-only learned model + MPC-lite scaffold."""

    def __init__(
        self,
        *,
        learning: ThermalLearning,
        deadband: float,
        min_interval: int,
        fan_modes: list[str] | None = None,
        horizon_minutes: int = 30,
        cycle_minutes: int = DEFAULT_CYCLE_MINUTES,
    ) -> None:
        self._learning = learning
        self._deadband = deadband
        self._min_interval = min_interval
        self._fan_modes = fan_modes
        self._horizon_minutes = horizon_minutes
        self._cycle_minutes = cycle_minutes
        self._disturbance_bias = 0.0
        self._error_at_lock_start: float | None = None
        # Snapshot of what every candidate slope resolved to on the last
        # evaluate() call: {hvac_mode, slopes: {fan_mode: (slope, is_learned)}}.
        # Lets diagnostics show the live rank-scaled estimate a not-yet-learned
        # profile is actually being evaluated at, instead of just "unknown".
        self._last_mode_slopes: dict[str, Any] | None = None

    @property
    def fan_modes(self) -> list[str] | None:
        """Return the currently known fan modes."""
        return self._fan_modes

    @fan_modes.setter
    def fan_modes(self, modes: list[str] | None) -> None:
        """Update the available fan modes.

        Fan modes are assumed ordered weakest-to-strongest.  A warning is
        logged when learned profiles are available and their slopes violate
        this ordering, which usually indicates a configuration issue.
        """
        self._fan_modes = modes
        if modes and len(modes) >= 2:
            self._warn_if_unordered(modes)

    def _warn_if_unordered(self, modes: list[str]) -> None:
        """Log a warning when learned slopes don't match the assumed mode ordering.

        Checks every hvac mode (not just heating — a cooling-only install would
        otherwise never be checked) and skips profiles that aren't learned yet
        instead of giving up at the first one, so a partially-learned ladder is
        still validated on the profiles that do exist.
        """
        for hvac_mode in PROFILE_HVAC_MODES:
            prev_mode: str | None = None
            prev_slope: float | None = None
            for mode in modes:
                slope = self._learning.get_mode_effective_slope(mode, hvac_mode)
                if slope is None:
                    continue  # not learned yet — skip, keep checking the others
                if prev_slope is not None and slope < prev_slope:
                    _LOGGER.warning(
                        "Fan modes may not be ordered weakest-to-strongest for %s: "
                        "'%s' (%.2f C/h) is learned as weaker than '%s' (%.2f C/h). "
                        "If the order is wrong, set it explicitly in the integration options",
                        hvac_mode,
                        mode,
                        slope,
                        prev_mode,
                        prev_slope,
                    )
                    break
                prev_mode, prev_slope = mode, slope

    @property
    def learning(self) -> ThermalLearning:
        """Return the ThermalLearning instance used by this controller."""
        return self._learning

    def get_live_mode_slope(self, fan_mode: str, hvac_mode: str) -> tuple[float, bool] | None:
        """Return ``(slope, is_learned)`` as used on the last evaluate() call.

        For a profile with fewer than ``MIN_MODE_PROFILE_SAMPLES``,
        :meth:`ThermalLearning.get_mode_effective_slope` reports ``None`` --
        correctly, since there is no *measured* value yet -- but the MPC does not
        simply skip that candidate: every cycle it substitutes a live estimate,
        scaled by rank from whichever fan is currently running (see
        ``_get_mode_slope``). That estimate is what actually drives cost
        comparisons and step-down decisions for an unlearned speed, so it is
        surfaced here for diagnostics rather than only ever showing "unknown".

        Returns None when no cycle has evaluated this ``hvac_mode`` yet, or when
        the last cycle evaluated a different one (only one hvac_mode is live at a
        time, so a "heat" profile has nothing to show while only "cool" has run).
        """
        if self._last_mode_slopes is None or self._last_mode_slopes["hvac_mode"] != hvac_mode:
            return None
        return self._last_mode_slopes["slopes"].get(fan_mode)

    def notify_fan_change(self) -> None:
        """Tell the controller the fan mode just changed (by us or externally).

        The comfort-error baseline used by the emergency escalation is
        re-snapshotted on the next evaluate() call. Relying on
        ``minutes_since_change < cycle_minutes`` alone to detect a fresh hold was
        fragile: the first cycle after a change lands within milliseconds of
        that bound, and a cycle skipped for missing inputs keeps a stale
        baseline for the whole hold.
        """
        self._error_at_lock_start = None

    def _dead_time_is_trusted(self) -> bool:
        """True when the learned dead time rests on enough real response events.

        See MIN_RESPONSE_EVENTS_FOR_ADAPTIVE_INTERVAL for why this is not
        ``ThermalLearning.is_ready()``: that flag counts slope samples, which
        answers a different question and lags so far behind that the adaptive
        interval could never engage on a coarse room sensor.
        """
        return self._learning.response_event_count() >= MIN_RESPONSE_EVENTS_FOR_ADAPTIVE_INTERVAL

    def get_effective_timeout(self, hvac_mode: str = "unknown") -> float:
        """Return the adaptive advisory timeout (diagnostic only).

        This value is surfaced in sensors and the data-collection CSV to show how
        long the controller would wait before forcing a re-evaluation; it does not
        gate any control decision (that is the job of ``min_interval``). Until the
        dead time is trusted it falls back to the default dead time scaled by the
        safety factor.
        """
        if self._dead_time_is_trusted():
            learned_dead_time = self._learning.get_dead_time(hvac_mode)
            return max(self._min_interval, learned_dead_time * DEAD_TIME_SAFETY_FACTOR)
        return DEFAULT_DEAD_TIME * DEAD_TIME_SAFETY_FACTOR

    def _effective_min_interval(self, dead_time: float) -> float:
        """Return the minimum dwell (minutes) before a fan change is allowed.

        The configured ``min_interval`` is a floor. Once the dead time is trusted
        the effective dwell is raised toward it — changing the fan faster than the
        dead time means acting before the previous change's effect can be
        observed, a guaranteed source of oscillation. The rise is capped at
        ``MAX_ADAPTIVE_INTERVAL_FACTOR`` × the configured floor so a spuriously
        large learned dead time cannot make the controller sluggish. Urgent
        overrides (setpoint drop, window/defrost/idle) are handled before this
        gate, so they are never blocked by a long adaptive interval.
        """
        if not self._dead_time_is_trusted():
            return float(self._min_interval)
        capped = min(dead_time, self._min_interval * MAX_ADAPTIVE_INTERVAL_FACTOR)
        return max(float(self._min_interval), capped)

    @property
    def disturbance_bias(self) -> float:
        """Return the current disturbance bias estimate (°C/h)."""
        return self._disturbance_bias

    def evaluate(
        self,
        *,
        current_temp: float,
        target_temp: float,
        vtherm_slope: float,
        hvac_mode: str,
        current_fan: str | None,
        is_window_open: bool = False,
        is_defrost_active: bool = False,
        is_hvac_idle: bool = False,
        minutes_since_change: float = 0.0,
    ) -> dict:
        """Evaluate the best fan mode for the current cycle."""
        _LOGGER.debug(
            "MPC evaluate: hvac=%s current_temp=%.2f target=%.2f slope=%.3f current_fan=%s minutes_since_change=%.1f window_open=%s",
            hvac_mode,
            current_temp,
            target_temp,
            vtherm_slope,
            current_fan,
            minutes_since_change,
            is_window_open,
        )

        # Only heat/cool have a defined comfort-error direction and learned profiles.
        if hvac_mode not in PROFILE_HVAC_MODES:
            return self._payload(
                status="Idle",
                fan_mode=current_fan,
                reason=f"HVAC mode '{hvac_mode}' is not regulated",
                would_change_now="no",
            )

        fan_modes = self._fan_modes or ([] if current_fan is None else [current_fan])
        if not fan_modes:
            return self._payload(
                status="Unavailable",
                fan_mode=current_fan,
                reason="No fan modes available yet",
                would_change_now="no",
            )

        active_fan = current_fan if current_fan in fan_modes else fan_modes[0]
        current_effective_slope = -vtherm_slope if hvac_mode == "cool" else vtherm_slope
        current_error = self._temperature_error(current_temp, target_temp, hvac_mode)
        # Mode-specific: a heat pump's heating lag and cooling lag are different
        # numbers, and this dead time drives the change gate, the phase split and
        # every candidate's change_delay. Omitting the argument pools both modes'
        # response events into one median. There is no robustness argument for
        # pooling here either -- get_dead_time() already falls back to the pooled
        # set on its own when the requested mode has no events yet.
        dead_time = self._learning.get_dead_time(hvac_mode)
        # Snapshot the comfort error at the start of each hold so growth since
        # the fan last changed can be measured (see DEAD_TIME_ESCALATION_GROWTH).
        # notify_fan_change() clears the snapshot explicitly; the time-based
        # test remains as a fallback for callers that never notify.
        if minutes_since_change < self._cycle_minutes or self._error_at_lock_start is None:
            self._error_at_lock_start = current_error
        error_growth_since_change = current_error - self._error_at_lock_start
        effective_min_interval = self._effective_min_interval(dead_time)
        learning_hold_minutes = dead_time * MIN_ESTABLISHED_RATIO + LEARNING_HOLD_EXTRA_MINUTES
        learning_hold = self._learning_hold_active(
            active_fan=active_fan,
            hvac_mode=hvac_mode,
            minutes_since_change=minutes_since_change,
            learning_hold_minutes=learning_hold_minutes,
            current_error=current_error,
            error_growth_since_change=error_growth_since_change,
        )
        if learning_hold:
            effective_min_interval = max(effective_min_interval, learning_hold_minutes)
        change_allowed = minutes_since_change >= effective_min_interval
        phase = self.detect_phase(minutes_since_change, dead_time)
        monotone_slopes = self.build_monotone_slopes(fan_modes, hvac_mode)
        current_mode_slope, current_known_profile = self._get_mode_slope(
            active_fan,
            hvac_mode,
            active_fan,
            current_effective_slope,
            fan_modes,
            monotone_slopes,
        )
        # Compare the observed slope against what the gap-dependent model expects
        # *at the current error*, not at the reference gap. This keeps the
        # disturbance bias clean: it only captures genuine external disturbances
        # (solar gain, occupancy) instead of the systematic variation of cooling
        # power with the distance to setpoint.
        current_mode_gain = (
            self._learning.get_mode_slope_gain(active_fan, hvac_mode) if current_known_profile else 0.0
        )
        expected_slope_now = self._gap_slope(current_mode_slope, current_mode_gain, current_error)
        self._update_disturbance_bias(
            observed_effective_slope=current_effective_slope,
            expected_effective_slope=expected_slope_now,
            known_profile=current_known_profile,
            phase=phase,
            is_window_open=is_window_open,
            is_defrost_active=is_defrost_active,
            is_hvac_idle=is_hvac_idle,
        )

        if is_window_open or is_defrost_active or is_hvac_idle:
            if is_window_open:
                reason = "Window open detected: MPC paused"
            elif is_defrost_active:
                reason = "Defrost active: MPC paused"
            else:
                reason = "HVAC idle: compressor off, MPC paused"
            return self._payload(
                status="Disturbed",
                fan_mode=active_fan,
                reason=reason,
                would_change_now="no",
                dead_time=dead_time,
                known_profiles=self._count_known_profiles(fan_modes, hvac_mode),
                disturbance_bias=self._disturbance_bias,
            )

        # Setpoint drop: when the target moves far away (e.g. night setpoint),
        # there is no point running the full MPC cost optimisation — the answer
        # is always the lowest mode.
        if current_error < THRESHOLD_TARGET_DROP:
            lowest_fan = fan_modes[0]
            would_change = "yes" if active_fan != lowest_fan else "no"
            return self._payload(
                status="Setpoint drop",
                fan_mode=lowest_fan,
                reason=f"Setpoint drop: target moved away ({current_error:.1f}°C), minimum speed",
                would_change_now=would_change,
                dead_time=dead_time,
                known_profiles=self._count_known_profiles(fan_modes, hvac_mode),
                disturbance_bias=self._disturbance_bias,
            )

        simulations: list[ModeSimulation] = []
        known_profiles = 0
        worst_spread = 0.0
        current_index = fan_modes.index(active_fan)

        # Monotone-enforced slope map (built above, before the current mode's own
        # slope was resolved): higher fan modes are never assigned a lower slope
        # than lower modes. Modes without a learned profile are absent and fall
        # back to an estimate anchored on their nearest learned neighbour.
        _LOGGER.debug(
            "MPC %s profiles: fan_mode_order=%s effective_slopes_used=%s",
            hvac_mode,
            fan_modes,
            {mode: round(slope, 3) for mode, slope in monotone_slopes.items()},
        )

        # Ensure the horizon is at least dead_time + base_horizon so that
        # any mode changes are simulated for at least a full default horizon (e.g. 30 minutes)
        # of their actual candidate slope, preventing the "dead-time blindness".
        sim_horizon = max(self._horizon_minutes, int(dead_time) + self._horizon_minutes)

        mode_slopes_snapshot: dict[str, tuple[float, bool]] = {}
        for fan_mode in fan_modes:
            mode_slope, known_profile = self._get_mode_slope(
                fan_mode,
                hvac_mode,
                active_fan,
                current_effective_slope,
                fan_modes,
                monotone_slopes,
            )
            mode_slopes_snapshot[fan_mode] = (mode_slope, known_profile)
            mode_gain = self._learning.get_mode_slope_gain(fan_mode, hvac_mode) if known_profile else 0.0
            sim = self._simulate_mode(
                current_temp=current_temp,
                target_temp=target_temp,
                hvac_mode=hvac_mode,
                current_fan=active_fan,
                candidate_fan=fan_mode,
                current_effective_slope=current_effective_slope,
                candidate_mode_slope=mode_slope,
                candidate_mode_gain=mode_gain,
                dead_time=dead_time,
                candidate_index=fan_modes.index(fan_mode),
                current_index=current_index,
                change_allowed=change_allowed,
                known_profile=known_profile,
                horizon_minutes=sim_horizon,
            )
            simulations.append(sim)
            known_profiles += int(known_profile)
            if known_profile:
                spread = self._learning.get_profile_spread(fan_mode, hvac_mode)
                if spread is not None:
                    worst_spread = max(worst_spread, spread)

        self._last_mode_slopes = {"hvac_mode": hvac_mode, "slopes": mode_slopes_snapshot}

        current_simulation = next(sim for sim in simulations if sim.fan_mode == active_fan)
        unfiltered_best = min(simulations, key=lambda item: item.total_cost)

        def _skipped_unmeasured_rung(candidate_index: int) -> str | None:
            """Return the first viable-looking, unmeasured rung a climb would skip."""
            if candidate_index <= current_index + 1:
                return None
            if current_error > MULTI_RANK_JUMP_ERROR or error_growth_since_change > DEAD_TIME_ESCALATION_GROWTH:
                return None
            for rung in fan_modes[current_index + 1 : candidate_index]:
                rung_slope, _ = mode_slopes_snapshot[rung]
                if rung_slope > 0 and not self._learning.has_measured_profile(rung, hvac_mode):
                    return rung
            return None

        def _rank_move_capable(sim: ModeSimulation) -> bool:
            candidate_index = fan_modes.index(sim.fan_mode)
            if candidate_index >= current_index:
                # Upward: never skip an intermediate speed that has not been
                # measured yet and looks able to do the job -- see
                # MULTI_RANK_JUMP_ERROR.
                return _skipped_unmeasured_rung(candidate_index) is None
            if current_index - candidate_index <= 1:
                return True
            raw_slope = self._learning.get_mode_effective_slope(sim.fan_mode, hvac_mode)
            return raw_slope is None or raw_slope > MIN_VIABLE_MULTI_RANK_STEPDOWN_SLOPE

        eligible_simulations = [sim for sim in simulations if _rank_move_capable(sim)]
        best = min(eligible_simulations, key=lambda item: item.total_cost)
        selection_note = ""
        blocked_note = ""

        if unfiltered_best.fan_mode != best.fan_mode:
            blocked_index = fan_modes.index(unfiltered_best.fan_mode)
            if blocked_index > current_index:
                ranks = blocked_index - current_index
                rung = _skipped_unmeasured_rung(blocked_index)
                blocked_note = (
                    f"Blocked {ranks}-rank jump to {unfiltered_best.fan_mode}: "
                    f"{rung} has no measured profile yet and must be tried first"
                )
            else:
                ranks = current_index - blocked_index
                blocked_slope = self._learning.get_mode_effective_slope(unfiltered_best.fan_mode, hvac_mode)
                blocked_note = (
                    f"Blocked {ranks}-rank drop to {unfiltered_best.fan_mode}: "
                    f"its own profile ({blocked_slope:.2f}C/h) can't sustain progress"
                )

        if not change_allowed and best.fan_mode != active_fan:
            best_index = fan_modes.index(best.fan_mode)
            if best_index > current_index and error_growth_since_change > DEAD_TIME_ESCALATION_GROWTH:
                selection_note = (
                    f"Emergency escalation to {best.fan_mode}: comfort error worsened by "
                    f"{error_growth_since_change:.2f}C since the change bypasses the min interval"
                )
                change_allowed = True
            else:
                selection_note = f"Min interval holds {active_fan} until a change is allowed"
                best = current_simulation
        elif change_allowed and best.fan_mode != active_fan:
            best_index = fan_modes.index(best.fan_mode)
            required_gain = self._required_switch_gain(
                current_error=current_error,
                phase=phase,
                candidate_index=best_index,
                current_index=current_index,
            )
            actual_gain = current_simulation.total_cost - best.total_cost
            if actual_gain < required_gain:
                selection_note = (
                    f"Hysteresis holds {active_fan}: {best.fan_mode} only improves by "
                    f"{actual_gain:.2f} < {required_gain:.2f}"
                )
                best = current_simulation
            else:
                hold_note = self._step_down_hold_note(
                    candidate=best,
                    active_fan=active_fan,
                    hvac_mode=hvac_mode,
                    target_temp=target_temp,
                    current_error=current_error,
                    candidate_index=best_index,
                    current_index=current_index,
                    phase=phase,
                )
                if hold_note:
                    selection_note = hold_note
                    best = current_simulation

        confidence = self._compute_confidence(known_profiles, len(fan_modes), phase, worst_spread)
        would_change_now = "yes" if change_allowed and best.fan_mode != active_fan else "no"
        status = "Ready" if confidence >= 0.5 else "Low confidence"
        _LOGGER.debug(
            "MPC candidates: %s",
            [
                (
                    sim.fan_mode,
                    round(sim.total_cost, 3),
                    round(sim.predicted_temp_10m, 2),
                    round(sim.predicted_temp_30m, 2),
                    sim.known_profile,
                )
                for sim in simulations
            ],
        )
        reason = (
            f"MPC recommends {best.fan_mode}: cost={best.total_cost:.2f}, "
            f"T+10={best.predicted_temp_10m:.2f}C, T+30={best.predicted_temp_30m:.2f}C"
        )
        if blocked_note:
            reason += f" | {blocked_note}"
        if selection_note:
            reason += f" | {selection_note}"
        # Surface capacity saturation: strongest fan selected yet still well short
        # of target means the HVAC system is capacity-bound, not a control issue.
        if best.fan_mode == fan_modes[-1] and current_error > self._deadband:
            reason += f" | Saturated: max fan, {current_error:.1f}C from target (capacity-bound)"
        if abs(self._disturbance_bias) >= 0.05:
            reason += f" | Bias={self._disturbance_bias:+.2f}C/h"
        if not change_allowed:
            reason += (
                f" | Min interval active ({minutes_since_change:.1f}/"
                f"{effective_min_interval:.1f} min)"
            )
            if learning_hold:
                reason += f" | Learning hold: {active_fan} has no measured profile yet"

        return self._payload(
            status=status,
            fan_mode=best.fan_mode,
            reason=reason,
            predicted_10m=best.predicted_temp_10m,
            predicted_30m=best.predicted_temp_30m,
            cost=best.total_cost,
            confidence=confidence * 100.0,
            would_change_now=would_change_now,
            dead_time=dead_time,
            known_profiles=known_profiles,
            disturbance_bias=self._disturbance_bias,
        )

    def _learning_hold_active(
        self,
        *,
        active_fan: str,
        hvac_mode: str,
        minutes_since_change: float,
        learning_hold_minutes: float,
        current_error: float,
        error_growth_since_change: float,
    ) -> bool:
        """True while the current speed should be kept so it can be measured.

        Applies only to a speed without a measured profile (seeded values do not
        count: they are what the user guessed, not what the room did), and only
        until the learning gate has had LEARNING_HOLD_EXTRA_MINUTES to record
        samples. Released when the room is far off target (past
        MULTI_RANK_JUMP_ERROR: that is a recovery, and a speed that leaves the
        room there has already told us what it can do) or overshooting *and*
        getting worse -- the downward mirror of the emergency escalation, which
        is evaluated separately and releases the hold upward.
        """
        if self._learning.has_measured_profile(active_fan, hvac_mode):
            return False
        if minutes_since_change >= learning_hold_minutes:
            return False
        if current_error > MULTI_RANK_JUMP_ERROR:
            return False
        overshooting_and_worsening = (
            current_error < -self._deadband and error_growth_since_change < -DEAD_TIME_ESCALATION_GROWTH
        )
        return not overshooting_and_worsening

    def _count_known_profiles(self, fan_modes: list[str], hvac_mode: str) -> int:
        """Return how many fan modes have a learned profile for this hvac mode.

        Used by the return paths that skip the simulation loop (pauses, setpoint
        drop), which is where ``known_profiles`` is otherwise left at its default
        of 0 -- reading as "the model lost its learning" in the CSV and in
        VTherm's own attributes, when nothing was lost and only the optimisation
        was skipped.
        """
        return sum(
            1
            for fan_mode in fan_modes
            if self._learning.get_mode_effective_slope(fan_mode, hvac_mode) is not None
        )

    def _get_mode_slope(
        self,
        fan_mode: str,
        hvac_mode: str,
        current_fan: str,
        current_effective_slope: float,
        fan_modes: list[str],
        monotone_slopes: dict[str, float] | None = None,
    ) -> tuple[float, bool]:
        """Return the effective slope estimate for a candidate fan mode."""
        if monotone_slopes is not None and fan_mode in monotone_slopes:
            learned = monotone_slopes[fan_mode]
        else:
            learned = self._learning.get_mode_effective_slope(fan_mode, hvac_mode)
        if learned is not None:
            _LOGGER.debug(
                "MPC slope model: using learned profile for %s/%s = %.3f",
                hvac_mode,
                fan_mode,
                learned,
            )
            return learned, True

        if fan_mode == current_fan:
            # The speed that is running right now has no profile, but it has
            # something better for the next dead time: the slope the room is
            # actually showing. Flooring it at +0.2 degC/h modelled a speed that
            # was observably losing ground as one gaining it, and on a learned
            # dead time of 25 min that kept the controller on it until the room
            # was 2 degC off target.
            _LOGGER.debug(
                "MPC slope model: using observed slope for current unlearned %s/%s = %.3f",
                hvac_mode,
                fan_mode,
                current_effective_slope,
            )
            return current_effective_slope, False

        candidate_rank = fan_modes.index(fan_mode)
        if monotone_slopes:
            # Anchor on the nearest learned neighbour and step along the ladder,
            # rather than scaling the current speed's slope by rank ratio -- which
            # near equilibrium (slope ~0, floored to 0.2) rated every unlearned
            # speed at 0.04-0.16 degC/h against a learned 1.0 and made them all
            # look useless.
            anchor = min(monotone_slopes, key=lambda fm: abs(fan_modes.index(fm) - candidate_rank))
            anchor_rank = fan_modes.index(anchor)
            scaled = monotone_slopes[anchor]
            for _ in range(abs(candidate_rank - anchor_rank)):
                scaled = self._one_rank_stronger(scaled) if candidate_rank > anchor_rank else self._one_rank_weaker(scaled)
            _LOGGER.debug(
                "MPC slope model: using ladder estimate for %s/%s = %.3f (anchor=%s)",
                hvac_mode,
                fan_mode,
                scaled,
                anchor,
            )
            return scaled, False

        baseline_slope = max(current_effective_slope, 0.2)
        current_rank = fan_modes.index(current_fan) + 1
        scaled = baseline_slope * ((candidate_rank + 1) / max(current_rank, 1))
        _LOGGER.debug(
            "MPC slope model: using fallback for %s/%s = %.3f (baseline=%.3f current_fan=%s)",
            hvac_mode,
            fan_mode,
            scaled,
            baseline_slope,
            current_fan,
        )
        return scaled, False

    @staticmethod
    def _gap_slope(reference_slope: float, gain: float, error: float) -> float:
        """Return the modelled effective slope at a given comfort error.

        ``reference_slope`` is the representative slope at REFERENCE_SLOPE_ERROR
        (a + b·REF) and ``gain`` is b, so the model at ``error`` is
        reference_slope + b·(error − REF). The error is floored at 0 (no driving
        force at/below setpoint). The result is floored at 0 for a speed that
        moves the room toward target, so the gap term never projects active
        cooling/heating away from the setpoint -- but a *negative* reference
        slope is a speed known (learned, seeded or observed) to lose ground, and
        that loss must reach the simulator: flooring it to 0 made a seeded
        silent = -0.5 degC/h look neutral and an observed -0.3 look harmless. The
        additive disturbance bias is applied separately by the caller.
        """
        modelled = reference_slope + gain * (max(error, 0.0) - REFERENCE_SLOPE_ERROR)
        return max(min(0.0, reference_slope), modelled)

    @staticmethod
    def detect_phase(minutes_since_change: float, dead_time: float) -> str:
        """Classify the response phase since the last fan change.

        Public because the feature manager gates slope-sample collection on the
        same phase: one clock for the controller and the learner, so a sample is
        never taken while the MPC still considers the room in its dead time.
        """
        effective_dead_time = DEFAULT_DEAD_TIME if dead_time <= 0 else dead_time
        if minutes_since_change < effective_dead_time:
            return PHASE_DEAD_TIME
        if minutes_since_change < effective_dead_time * DEAD_TIME_SAFETY_FACTOR:
            return PHASE_TRANSIENT
        return PHASE_ESTABLISHED

    @staticmethod
    def _one_rank_weaker(value: float) -> float:
        """Return a slope one ladder step below *value*, strictly smaller than it.

        Scaling is multiplicative so the step tracks the magnitude of the
        neighbour, but a positive slope shrinks toward zero while a negative one
        (a speed that loses ground) grows more negative — both mean "weaker".
        """
        scaled = value / LADDER_CAPACITY_RATIO if value > 0 else value * LADDER_CAPACITY_RATIO
        if value - scaled < MIN_LADDER_SEPARATION:
            scaled = value - MIN_LADDER_SEPARATION
        return scaled

    @staticmethod
    def _one_rank_stronger(value: float) -> float:
        """Return a slope one ladder step above *value*, strictly greater than it."""
        scaled = value * LADDER_CAPACITY_RATIO if value > 0 else value / LADDER_CAPACITY_RATIO
        if scaled - value < MIN_LADDER_SEPARATION:
            scaled = value + MIN_LADDER_SEPARATION
        return scaled

    def build_monotone_slopes(self, fan_modes: list[str], hvac_mode: str) -> dict[str, float]:
        """Return monotone-enforced slopes for all known profiles.

        Fan modes are assumed ordered from weakest to strongest, so learned
        slopes must be non-decreasing along that order. Modes without a learned
        profile are omitted — the caller falls back to rank-scaled estimation.

        Profiles are placed **best-sampled first**, each one clipped into the
        window left by those already placed. So the most trusted estimate is
        never overwritten, and a noisy estimate from a rarely-used speed cannot
        propagate into the profiles that have the most data behind them — which
        a plain forward ``max()`` pass does, since it always resolves upward
        (a 10-sample ``med`` reading +1.6 would drag a 1656-sample ``superhigh``
        from +0.9 up to +1.6).

        A profile whose own estimate falls outside that window is discarded and
        **synthesised one ladder step from the neighbour that constrained it**
        (see LADDER_CAPACITY_RATIO) rather than pinned onto it: two speeds
        sharing an identical slope are thermally indistinguishable to the MPC,
        so the energy term alone would arbitrate — always picking the weaker
        rank, which then starves the stronger one of the samples it needs to
        ever be trusted.

        Placement is a single pass in trust order, so the result is monotone by
        construction with no iteration to converge. A ladder that is already
        consistent is returned untouched, whatever its own spacing: the ratio
        only ever synthesises a replacement, it is never imposed as a minimum
        gap between measured values.
        """
        measured = {
            fm: slope
            for fm in fan_modes
            if (slope := self._learning.get_mode_effective_slope(fm, hvac_mode)) is not None
        }
        if not measured:
            return {}

        rank = {fm: index for index, fm in enumerate(fan_modes)}
        counts = {fm: self._learning.get_mode_sample_count(fm, hvac_mode) for fm in measured}

        # Place profiles best-sampled first, so every later profile is constrained
        # by evidence at least as strong as its own and the most trusted estimate
        # is never overwritten. On an equal sample count the stronger rank is
        # placed first, which makes a tied violation resolve by weakening the
        # lower speed rather than strengthening the higher one.
        placement_order = sorted(measured, key=lambda fm: (counts[fm], rank[fm]), reverse=True)

        placed: dict[str, float] = {}
        for fm in placement_order:
            below = [v for f, v in placed.items() if rank[f] < rank[fm]]
            above = [v for f, v in placed.items() if rank[f] > rank[fm]]
            floor = max(below, default=float("-inf"))
            ceiling = min(above, default=float("inf"))

            value = measured[fm]
            if value > ceiling:
                # Own estimate claims more capacity than an already-placed stronger
                # speed. Discard it and synthesise one ladder step below that speed
                # rather than pinning it onto the ceiling: identical slopes are
                # thermally indistinguishable to the MPC, so the energy term alone
                # would arbitrate and would always pick the weaker rank.
                value = self._one_rank_weaker(ceiling)
            elif value < floor:
                value = self._one_rank_stronger(floor)
            # A synthesised value can overshoot the opposite bound when the two
            # placed neighbours sit close together; the window wins, since ordering
            # is the invariant the rest of the MPC depends on and separation is
            # only a preference. Collapses to a bound only when there is genuinely
            # no room left between the neighbours.
            placed[fm] = min(max(value, floor), ceiling)

        enforced = {fm: placed[fm] for fm in fan_modes if fm in placed}
        if enforced != measured:
            _LOGGER.debug(
                "Slope ordering enforced for %s: %s -> %s (samples: %s)",
                hvac_mode,
                {fm: round(v, 3) for fm, v in measured.items()},
                {fm: round(v, 3) for fm, v in enforced.items()},
                counts,
            )
        return enforced

    def _update_disturbance_bias(
        self,
        *,
        observed_effective_slope: float,
        expected_effective_slope: float,
        known_profile: bool,
        phase: str,
        is_window_open: bool,
        is_defrost_active: bool = False,
        is_hvac_idle: bool = False,
    ) -> None:
        """Track slow external disturbances such as solar gains or occupancy."""
        if is_window_open or is_defrost_active or is_hvac_idle:
            self._disturbance_bias *= DISTURBANCE_DECAY
            if is_window_open:
                decay_reason = "window is open"
            elif is_defrost_active:
                decay_reason = "defrost is active"
            else:
                decay_reason = "HVAC compressor is idle"
            _LOGGER.debug(
                "MPC disturbance bias decayed to %.3f because %s",
                self._disturbance_bias,
                decay_reason,
            )
            return

        if not known_profile or phase != PHASE_ESTABLISHED:
            self._disturbance_bias *= DISTURBANCE_DECAY
            _LOGGER.debug(
                "MPC disturbance bias decayed to %.3f because known_profile=%s phase=%s",
                self._disturbance_bias,
                known_profile,
                phase,
            )
            return

        residual = observed_effective_slope - expected_effective_slope
        updated = ((1 - DISTURBANCE_EMA_ALPHA) * self._disturbance_bias) + (DISTURBANCE_EMA_ALPHA * residual)
        self._disturbance_bias = max(-MAX_DISTURBANCE_BIAS, min(MAX_DISTURBANCE_BIAS, updated))
        _LOGGER.debug(
            "MPC disturbance bias updated to %.3f (observed=%.3f expected=%.3f residual=%.3f)",
            self._disturbance_bias,
            observed_effective_slope,
            expected_effective_slope,
            residual,
        )

    def _simulate_mode(
        self,
        *,
        current_temp: float,
        target_temp: float,
        hvac_mode: str,
        current_fan: str,
        candidate_fan: str,
        current_effective_slope: float,
        candidate_mode_slope: float,
        candidate_mode_gain: float = 0.0,
        dead_time: float,
        candidate_index: int,
        current_index: int,
        change_allowed: bool,
        known_profile: bool,
        horizon_minutes: int | None = None,
    ) -> ModeSimulation:
        """Simulate one fan mode over the prediction horizon.

        The candidate's effective slope is gap-dependent: at each step it is
        recomputed from the simulated comfort error as
        ``candidate_mode_slope + candidate_mode_gain·(error − REFERENCE_SLOPE_ERROR)``
        (floored at 0), plus the disturbance bias. This makes the projection
        decelerate realistically as the room approaches the setpoint instead of
        cooling/heating at a constant rate, eliminating the phantom overshoot that a
        constant-slope model produces past the target.
        """
        horizon = horizon_minutes if horizon_minutes is not None else self._horizon_minutes
        steps = max(1, int(horizon / SIMULATION_STEP_MINUTES))
        step_hours = SIMULATION_STEP_MINUTES / 60.0
        sim_temp = current_temp
        predicted_10m = None
        predicted_30m = None
        cost = 0.0
        thermal_power = current_effective_slope
        change_delay = 0.0 if candidate_fan == current_fan else dead_time
        # Hold zone: within one deadband of the setpoint we optimise for holding
        # equilibrium (match the compressor's steady output) rather than for the
        # lowest fan rank. See the HOLD_EQUILIBRIUM constant block for rationale.
        hold_active = (
            HOLD_EQUILIBRIUM
            and abs(self._temperature_error(current_temp, target_temp, hvac_mode)) <= self._deadband
        )
        undershoot_tolerance = HOLD_UNDERSHOOT_TOLERANCE if hold_active else 0.0

        for step in range(1, steps + 1):
            elapsed = step * SIMULATION_STEP_MINUTES
            if elapsed <= change_delay:
                target_effective_slope = current_effective_slope
            else:
                step_error = self._temperature_error(sim_temp, target_temp, hvac_mode)
                target_effective_slope = (
                    self._gap_slope(candidate_mode_slope, candidate_mode_gain, step_error)
                    + self._disturbance_bias
                )

            thermal_power += THERMAL_POWER_BLEND * (target_effective_slope - thermal_power)
            raw_slope = -thermal_power if hvac_mode == "cool" else thermal_power
            sim_temp += step_hours * raw_slope

            if elapsed >= 10 and predicted_10m is None:
                predicted_10m = sim_temp
            if elapsed >= 30 and predicted_30m is None:
                predicted_30m = sim_temp

            error = self._temperature_error(sim_temp, target_temp, hvac_mode)
            comfort_error = max(abs(error) - self._deadband, 0.0)
            overshoot = max(-error - undershoot_tolerance, 0.0)
            floor_violation = max(target_temp - sim_temp, 0.0) if hvac_mode == "heat" else max(sim_temp - target_temp, 0.0)

            # Step-by-step urgency weight calculated dynamically based on current simulated step comfort error
            step_urgency_weight = 1.0 + comfort_error * URGENCY_SENSITIVITY
            cost += COMFORT_ERROR_WEIGHT * comfort_error * step_urgency_weight
            cost += OVERSHOOT_QUADRATIC_WEIGHT * overshoot * overshoot
            cost += FLOOR_VIOLATION_LINEAR_WEIGHT * floor_violation * step_urgency_weight
            cost += FLOOR_VIOLATION_QUADRATIC_WEIGHT * floor_violation * floor_violation

        cost += MODE_CHANGE_DISTANCE_COST * abs(candidate_index - current_index)
        # Apply a non-linear economic mode-ranking cost representing physical power
        # scaling. Relative power grows geometrically with the mode rank so every
        # mode is differentiated regardless of how many the climate entity exposes
        # (a 4-mode system reproduces the previous 1.0 / 1.8 / 3.3 / 6.0 ramp).
        relative_power = MODE_POWER_RATIO ** candidate_index
        rank_scale = HOLD_RANK_SCALE if hold_active else 1.0
        cost += MODE_RANK_COST * relative_power * rank_scale
        if candidate_fan != current_fan and not change_allowed:
            cost += MIN_INTERVAL_CHANGE_PENALTY

        return ModeSimulation(
            fan_mode=candidate_fan,
            total_cost=cost,
            predicted_temp_10m=current_temp if predicted_10m is None else predicted_10m,
            predicted_temp_30m=current_temp if predicted_30m is None else predicted_30m,
            known_profile=known_profile,
        )

    def _required_switch_gain(
        self,
        *,
        current_error: float,
        phase: str,
        candidate_index: int,
        current_index: int,
    ) -> float:
        """Return the minimum cost gain required before switching fan mode."""
        if current_error < -self._deadband:
            margin = BASE_SWITCH_GAIN_MARGIN
        elif current_error > (self._deadband * 2):
            margin = BASE_SWITCH_GAIN_MARGIN
        elif current_error > self._deadband:
            margin = APPROACHING_TARGET_SWITCH_GAIN_MARGIN
        else:
            margin = NEAR_TARGET_SWITCH_GAIN_MARGIN

        if phase != PHASE_ESTABLISHED:
            margin += PHASE_SWITCH_MARGIN_BONUS

        margin += STEP_SWITCH_MARGIN * abs(candidate_index - current_index)

        if candidate_index < current_index and current_error > 0:
            margin += UNDER_TARGET_STEPDOWN_GAIN_MARGIN
            margin += UNDER_TARGET_STEPDOWN_GAIN_PER_DEG * current_error

        return margin

    def _step_down_hold_note(
        self,
        *,
        candidate: ModeSimulation,
        active_fan: str,
        hvac_mode: str,
        target_temp: float,
        current_error: float,
        candidate_index: int,
        current_index: int,
        phase: str,
    ) -> str | None:
        """Return a note when a downward switch should be held despite lower cost."""
        if candidate_index >= current_index or current_error <= 0:
            return None

        if phase != PHASE_ESTABLISHED:
            return f"Below target: holding {active_fan} until the current response is established"

        predicted_error_10m = self._temperature_error(candidate.predicted_temp_10m, target_temp, hvac_mode)
        reserve = max(self._deadband * 0.5, UNDER_TARGET_SHORTFALL_RESERVE)
        if predicted_error_10m > reserve:
            return (
                f"Below target: holding {active_fan} because {candidate.fan_mode} still leaves "
                f"{predicted_error_10m:.2f}C shortfall at 10 min"
            )

        return None

    def _compute_confidence(self, known_profiles: int, total_profiles: int, phase: str, worst_spread: float = 0.0) -> float:
        """Return a coarse confidence score for the current recommendation.

        Confidence is driven primarily by per-mode profile *coverage* and
        *quality* (spread), not by the global readiness flag.  Previously the
        global ``is_ready()`` threshold (240 samples) halved the score, so a
        controller with every per-mode profile fully learned could still be
        stuck reporting "Low confidence" until that global count was reached —
        which never happened for an HVAC mode used only part of the year.

        - coverage  : fraction of fan modes with a learned profile (main driver).
        - readiness : small bonus once the global sample threshold is reached.
        - phase     : transient/dead-time phases attenuate confidence.
        - penalties : sustained disturbance bias and high profile spread.

        worst_spread is the maximum MAD/median ratio across all known profiles.
        Profiles with high spread (> 0.15) reduce confidence proportionally,
        capped at a 0.20 penalty.
        """
        coverage = known_profiles / max(total_profiles, 1)
        coverage_score = 0.3 + 0.7 * coverage
        readiness_bonus = 0.1 if self._learning.is_ready() else 0.0
        phase_factor = 1.0 if phase == PHASE_ESTABLISHED else 0.85
        disturbance_penalty = min(abs(self._disturbance_bias) / MAX_DISTURBANCE_BIAS, 0.35)
        spread_penalty = min(max(worst_spread - 0.15, 0.0) * 0.4, 0.20)
        return max(0.1, min(1.0, (coverage_score + readiness_bonus) * phase_factor - disturbance_penalty - spread_penalty))

    def _payload(
        self,
        *,
        status: str,
        fan_mode: str | None,
        reason: str,
        predicted_10m: float | None = None,
        predicted_30m: float | None = None,
        cost: float | None = None,
        confidence: float | None = None,
        would_change_now: str = "no",
        dead_time: float | None = None,
        known_profiles: int = 0,
        disturbance_bias: float | None = None,
    ) -> dict:
        """Build the MPC payload injected into sensors and CSV logs.

        Every ``evaluate()`` return path -- the full simulation as well as the
        early pauses (window/defrost/idle, setpoint drop, hvac_mode off) --
        funnels through here, so this is the one place that must log the
        result: status, reason, cost and would_change_now no longer have
        their own sensor entities (see sensor.py), and this DEBUG line is
        what keeps them inspectable.
        """
        payload = {
            "mpc_status": status,
            "mpc_fan_mode": fan_mode,
            "mpc_reason": reason,
            "mpc_predicted_temperature_10m": round(predicted_10m, 2) if predicted_10m is not None else None,
            "mpc_predicted_temperature_30m": round(predicted_30m, 2) if predicted_30m is not None else None,
            "mpc_cost": round(cost, 3) if cost is not None else None,
            "mpc_confidence": round(confidence, 1) if confidence is not None else None,
            "mpc_would_change_now": would_change_now,
            "mpc_dead_time": round(dead_time, 2) if dead_time is not None else None,
            "mpc_known_profiles": known_profiles,
            "mpc_disturbance_bias": round(disturbance_bias, 3) if disturbance_bias is not None else None,
        }
        _LOGGER.debug(
            "MPC decision: status=%s fan_mode=%s would_change_now=%s cost=%s "
            "confidence=%s known_profiles=%d dead_time=%s bias=%s reason=%s",
            status,
            fan_mode,
            would_change_now,
            payload["mpc_cost"],
            payload["mpc_confidence"],
            known_profiles,
            payload["mpc_dead_time"],
            payload["mpc_disturbance_bias"],
            reason,
        )
        return payload

    @staticmethod
    def _temperature_error(temp: float, target_temp: float, hvac_mode: str) -> float:
        """Return the signed comfort error aligned with the active HVAC mode."""
        return (temp - target_temp) if hvac_mode == "cool" else (target_temp - temp)
