import logging
import time
import statistics

from .const import (
    MIN_SAMPLES_LEARNING,
    MIN_MODE_PROFILE_SAMPLES,
    DEFAULT_DEAD_TIME,
    PROFILE_HVAC_MODES,
    PROFILE_RETENTION_SAMPLES,
    REFERENCE_SLOPE_ERROR,
)

#: Cap on persisted slope samples (see to_dict). ~7 days at 2-min intervals.
MAX_STORED_SLOPE_SAMPLES = 5000

_LOGGER = logging.getLogger(__name__)


class ThermalLearning:
    """Auto-calibration of thermal parameters based on observed system behavior."""

    def __init__(self):
        # Data collection with sliding window
        self._slope_samples = []  # (timestamp, fan_mode, slope, hvac_mode, temperature_error)
        self._response_events = []  # (timestamp, response_time_minutes) - thermal response from fan change to slope change
        self._learning_window_hours = 168  # 7 days sliding window
        self._min_samples = MIN_SAMPLES_LEARNING  # Minimum samples for initial readiness (48-72h typical activity)
        self._ready_once = False  # Flag to track if we've ever reached ready state
        self._profile_ready_logged: set[tuple[str, str]] = set()

        # Incremental statistics using Welford's algorithm
        self._slope_count = 0  # Number of slope samples processed
        self._slope_mean = 0.0  # Running mean of absolute slopes
        self._slope_m2 = 0.0  # Sum of squared differences for variance
        self._slope_max = 0.0  # Maximum absolute slope

        # Cache for computed optimal parameters (invalidated on each new sample)
        self._optimal_cache: dict | None = None

    def reset(self) -> None:
        """Reset all learning data and statistics."""
        self._slope_samples.clear()
        self._response_events.clear()
        self._slope_count = 0
        self._slope_mean = 0.0
        self._slope_m2 = 0.0
        self._slope_max = 0.0
        self._ready_once = False
        self._optimal_cache = None
        self._profile_ready_logged.clear()
        _LOGGER.info("Learning: reset requested; data cleared")

    def add_slope_sample(self, fan_mode: str, slope: float, temperature_error: float = 0, hvac_mode: str = "unknown", is_window_open: bool = False):
        """Record slope only if in normal operating range.

        Samples are filtered out when:
        - Setpoint drop / night mode (error < -1°C)
        - Window is open (external disturbance, not representative)

        A near-zero slope is deliberately *not* filtered out. The learned model
        is ``slope(error) = a + b*error``, and a speed that holds the room at the
        setpoint produces exactly that: slope ~0 at error ~0, which is the
        measurement of its intercept ``a``. Rejecting |slope| below a threshold
        censored the bottom of the distribution and biased ``a`` upward -- the
        over-estimated low-speed profiles seen in production -- while starving
        the intermediate speeds, whose only visits happen near equilibrium, of
        the few samples they get (on a 23-day trace, 22 of high's 27 distinct
        readings fell under the old 0.15 degC/h cut).

        Note: re-reading the *same* measurement because the control loop polled
        faster than the temperature source updates is a sampling artifact, not a
        modelling one, and is filtered upstream by the feature manager (see
        SLOPE_SAMPLE_MIN_DELTA). This method treats every call as one genuine
        observation.
        """
        if temperature_error < -1.0:
            _LOGGER.debug("Learning: Skipped sample (setpoint drop, err=%.2f)", temperature_error)
            return

        if is_window_open:
            _LOGGER.debug("Learning: Skipped sample (window open)")
            return

        self._slope_samples.append((time.time(), fan_mode, slope, hvac_mode, temperature_error))
        profile_samples = self.get_mode_sample_count(fan_mode, hvac_mode)
        _LOGGER.debug(
            "Learning: Collected slope sample #%d (fan=%s, slope=%.2f, err=%.2f, hvac=%s, profile=%d/%d)",
            len(self._slope_samples),
            fan_mode,
            slope,
            temperature_error,
            hvac_mode,
            profile_samples,
            MIN_MODE_PROFILE_SAMPLES,
        )

        self._optimal_cache = None

        self._update_slope_stats(abs(slope))

        profile_key = (hvac_mode, fan_mode)
        if (
            hvac_mode != "unknown"
            and profile_samples >= MIN_MODE_PROFILE_SAMPLES
            and profile_key not in self._profile_ready_logged
        ):
            self._profile_ready_logged.add(profile_key)
            effective_slope = self.get_mode_effective_slope(fan_mode, hvac_mode)
            _LOGGER.info(
                "Learning: profile %s/%s is ready with %d samples (effective_slope=%s)",
                hvac_mode,
                fan_mode,
                profile_samples,
                f"{effective_slope:.3f}" if effective_slope is not None else "n/a",
            )

        # Cleanup: keep only data within sliding window (7 days), but retain the
        # PROFILE_RETENTION_SAMPLES newest per profile so a rarely-used mode keeps
        # accumulating measurements across weeks instead of losing them.
        cutoff_time = time.time() - (self._learning_window_hours * 3600)
        before = len(self._slope_samples)
        self._slope_samples = self.trim_with_min_retention(self._slope_samples, cutoff_time, PROFILE_RETENTION_SAMPLES)

        if len(self._slope_samples) < before:
            self.recompute_slope_stats()
            _LOGGER.debug(
                "Learning: dropped %d expired slope samples from the sliding window",
                before - len(self._slope_samples),
            )

    @staticmethod
    def trim_with_min_retention(
        samples: list,
        cutoff_time: float,
        min_per_profile: int,
    ) -> list:
        """Apply sliding-window cutoff while retaining at least min_per_profile
        newest samples per (fan_mode, hvac_mode) profile.

        Prevents a rarely-used mode from losing its learned profile solely
        because it hasn't been active in the past 7 days.
        Samples are 5-tuples: (timestamp, fan_mode, slope, hvac_mode, temperature_error).
        """
        within = [s for s in samples if s[0] > cutoff_time]
        expired = [s for s in samples if s[0] <= cutoff_time]
        if not expired:
            return within

        # Count within-window samples per (fan_mode, hvac_mode) profile
        profile_counts: dict[tuple, int] = {}
        for s in within:
            key = (s[1], s[3])
            profile_counts[key] = profile_counts.get(key, 0) + 1

        # Group expired samples by profile
        expired_by_profile: dict[tuple, list] = {}
        for s in expired:
            key = (s[1], s[3])
            expired_by_profile.setdefault(key, []).append(s)

        extras: list = []
        for key, exp_list in expired_by_profile.items():
            shortfall = min_per_profile - profile_counts.get(key, 0)
            if shortfall > 0:
                # Keep the newest expired ones for this profile
                exp_list.sort(key=lambda s: s[0], reverse=True)
                extras.extend(exp_list[:shortfall])

        return within + extras

    def _update_slope_stats(self, abs_slope: float) -> None:
        """Update slope statistics incrementally using Welford's algorithm."""
        self._slope_count += 1
        delta = abs_slope - self._slope_mean
        self._slope_mean += delta / self._slope_count
        delta2 = abs_slope - self._slope_mean
        self._slope_m2 += delta * delta2
        self._slope_max = max(self._slope_max, abs_slope)
        _LOGGER.debug("Learning: Updated stats (count=%d, mean=%.3f, max=%.3f)", self._slope_count, self._slope_mean, self._slope_max)

    def add_response_event(self, minutes_to_response: float, hvac_mode: str = "unknown"):
        """Record time until slope changed significantly after fan change."""
        self._response_events.append((time.time(), minutes_to_response, hvac_mode))
        _LOGGER.debug(
            "Learning: Recorded response time #%d: %.1f min (hvac=%s)",
            len(self._response_events),
            minutes_to_response,
            hvac_mode,
        )

        # Invalidate cached optimal parameters
        self._optimal_cache = None

        # Cleanup: keep only data within sliding window (7 days)
        cutoff_time = time.time() - (self._learning_window_hours * 3600)
        before = len(self._response_events)
        self._response_events = [
            (ts, t, hm) if len(item) == 3 else (ts, t, "unknown")
            for item in self._response_events
            if (ts := item[0]) > cutoff_time and (t := item[1]) is not None and (hm := (item[2] if len(item) == 3 else "unknown"))
        ]
        if len(self._response_events) < before:
            _LOGGER.debug(
                "Learning: dropped %d expired response events from the sliding window",
                before - len(self._response_events),
            )

    def slope_sample_count(self) -> int:
        """Return number of collected slope samples."""
        return len(self._slope_samples)

    def response_event_count(self, hvac_mode: str | None = None) -> int:
        """Return the number of recorded response events.

        Without *hvac_mode* every stored event is counted (diagnostics). With
        one, only the events that :meth:`get_dead_time` would use for that mode
        are counted -- so "is the dead time of cool trusted?" is answered by
        cool's own events, never by heating's or by a non-regulated mode's.
        """
        if hvac_mode is None:
            return len(self._response_events)
        return len(self._mode_response_times(hvac_mode))

    @staticmethod
    def _event_mode(item) -> str:
        """Return the HVAC mode a response event was recorded in (legacy: unknown)."""
        return item[2] if len(item) == 3 else "unknown"

    def _mode_response_times(self, hvac_mode: str) -> list[float]:
        """Return the response times that describe *hvac_mode*'s dead time.

        Events recorded in a non-regulated mode (dry, fan_only...) are never
        used: the slope there is not driven by heating or cooling, so its
        "response" says nothing about the lag the MPC works with. ``unknown``
        pools the regulated modes; legacy events without a mode count for any.
        """
        times = []
        for item in self._response_events:
            event_mode = self._event_mode(item)
            if item[1] <= 0 or (event_mode != "unknown" and event_mode not in PROFILE_HVAC_MODES):
                continue
            if hvac_mode == "unknown" or event_mode in (hvac_mode, "unknown"):
                times.append(item[1])
        return times

    def get_progress(self) -> float:
        """Return learning progress as percentage (0-100).

        Once is_ready() has been reached (based on _ready_once), this always
        returns 100.0 so the UI remains consistent even if the sliding window
        later drops below _min_samples.
        """
        if self._ready_once:
            return 100.0
        sample_count = len(self._slope_samples)
        return min(100.0, (sample_count / self._min_samples) * 100)

    @property
    def slope_count(self) -> int:
        """Return the number of slope samples processed."""
        return self._slope_count

    @property
    def slope_mean(self) -> float:
        """Return the running mean of absolute slopes."""
        return self._slope_mean

    @property
    def slope_m2(self) -> float:
        """Return the sum of squared differences for variance."""
        return self._slope_m2

    @property
    def slope_max(self) -> float:
        """Return the maximum absolute slope."""
        return self._slope_max

    @property
    def min_samples(self) -> int:
        """Return the minimum samples required for readiness."""
        return self._min_samples

    @property
    def slope_samples(self) -> list:
        """Return the list of slope samples."""
        return self._slope_samples

    @slope_samples.setter
    def slope_samples(self, value: list) -> None:
        self._slope_samples = value
        self._optimal_cache = None

    @property
    def response_events(self) -> list:
        """Return the list of response events."""
        return self._response_events

    @response_events.setter
    def response_events(self, value: list) -> None:
        self._response_events = value
        self._optimal_cache = None

    @property
    def optimal_cache(self) -> dict | None:
        """Return the cached optimal parameters, or None if not yet computed."""
        return self._optimal_cache

    def is_ready(self) -> bool:
        """Check if enough data has been collected."""
        if not self._ready_once and len(self._slope_samples) >= self._min_samples:
            self._ready_once = True
            _LOGGER.info("Learning: Initial readiness reached with %d samples", self._min_samples)
        return self._ready_once

    def get_dead_time(self, hvac_mode: str = "unknown") -> float:
        """Return the learned dead time (median response time) in minutes for specified HVAC mode.

        Falls back to the regulated modes pooled together when the requested
        mode has no event yet, then to DEFAULT_DEAD_TIME when there is none at
        all. Events from non-regulated modes are ignored (see
        :meth:`_mode_response_times`).
        """
        response_times = self._mode_response_times(hvac_mode)

        if not response_times:
            # Try any regulated mode if the specific one has none yet
            response_times = self._mode_response_times("unknown")

        if not response_times:
            return DEFAULT_DEAD_TIME
        return statistics.median(response_times)

    def _fit_mode_slope(self, fan_mode: str, hvac_mode: str) -> tuple[float, float, float | None] | None:
        """Fit the gap-dependent slope model and return (intercept_a, gain_b, r_squared).

        The effective cooling/heating rate is not constant: it scales with the
        comfort error (distance to the setpoint), per Newton's law of cooling.
        We model it as a linear relationship:

            effective_slope(error) = a + b * error

        fitted by ordinary least squares over the profile's (error, effective_slope)
        samples. ``error`` is the signed comfort error (positive = needs more
        cooling/heating). ``effective_slope`` is positive when moving towards target
        (raw VTherm slope is inverted in cooling).

        ``r_squared`` is the coefficient of determination of the fit (0..1); it is
        ``None`` for the constant fallback (no real regression was performed).

        Falls back to a constant model ``(median_effective_slope, 0.0, None)`` when
        there are too few error-bearing samples or the error has no spread — which
        keeps behaviour identical to the previous median estimator for legacy data
        and for synthetic profiles seeded via ``set_mode_effective_slope``.

        The gain ``b`` is clamped to be non-negative: a larger gap can only cool/heat
        at least as fast, never slower.

        Returns None if fewer than MIN_MODE_PROFILE_SAMPLES are available.
        """
        sign = -1.0 if hvac_mode == "cool" else 1.0
        matching = [s for s in self._slope_samples if s[1] == fan_mode and s[3] == hvac_mode]
        if len(matching) < MIN_MODE_PROFILE_SAMPLES:
            return None

        # Only samples that carry a stored error are measurements; the others are
        # synthetic (set_mode_effective_slope) or predate the error column.
        points = [(s[4], sign * s[2]) for s in matching if len(s) > 4 and s[4] is not None]
        constants = [sign * s[2] for s in matching if len(s) <= 4 or s[4] is None]
        if len(points) < MIN_MODE_PROFILE_SAMPLES:
            # Not enough measurements for a regression. A plain median over the
            # mixed set would return the seeded value unchanged until the real
            # samples outnumber the synthetic ones (ten identical values are the
            # median of any set of fewer than twenty), hiding every bit of
            # progress. Weight the two medians by their counts instead, so each
            # measurement visibly pulls the profile toward what was observed.
            median_constant = statistics.median(constants) if constants else None
            median_measured = statistics.median([y for _, y in points]) if points else None
            if median_constant is None:
                return (median_measured, 0.0, None)
            if median_measured is None:
                return (median_constant, 0.0, None)
            weight = len(points) / (len(points) + len(constants))
            return (median_constant + weight * (median_measured - median_constant), 0.0, None)

        median_eff = statistics.median([y for _, y in points])

        n = len(points)
        mean_x = sum(x for x, _ in points) / n
        mean_y = sum(y for _, y in points) / n
        var_x = sum((x - mean_x) ** 2 for x, _ in points)
        if var_x < 1e-6:
            # All samples taken at (nearly) the same error: no slope can be fitted.
            return (median_eff, 0.0, None)

        cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in points)
        gain_b = max(0.0, cov_xy / var_x)
        intercept_a = mean_y - gain_b * mean_x

        # Coefficient of determination against the (clamped) fitted line.
        ss_tot = sum((y - mean_y) ** 2 for _, y in points)
        if ss_tot < 1e-9:
            r_squared = None
        else:
            ss_res = sum((y - (intercept_a + gain_b * x)) ** 2 for x, y in points)
            r_squared = max(0.0, 1.0 - ss_res / ss_tot)
        return (intercept_a, gain_b, r_squared)

    def get_mode_slope_model(self, fan_mode: str, hvac_mode: str) -> tuple[float, float] | None:
        """Return the gap-dependent slope model ``(intercept_a, gain_b)`` for a profile.

        See :meth:`_fit_mode_slope` for the model definition. Returns None if the
        profile has fewer than MIN_MODE_PROFILE_SAMPLES samples.
        """
        fit = self._fit_mode_slope(fan_mode, hvac_mode)
        return None if fit is None else (fit[0], fit[1])

    def get_mode_slope_gain(self, fan_mode: str, hvac_mode: str) -> float:
        """Return the gap gain ``b`` (°C/h per °C of comfort error) for a profile.

        Returns 0.0 when the profile is unknown or the model is constant.
        """
        model = self.get_mode_slope_model(fan_mode, hvac_mode)
        return 0.0 if model is None else model[1]

    def get_mode_slope_r2(self, fan_mode: str, hvac_mode: str) -> float | None:
        """Return the R² (goodness of fit, 0..1) of the gap-dependent slope model.

        Higher values mean the cooling/heating rate is well explained by the
        distance to the setpoint, i.e. the learned thermal model is reliable.
        Returns None when the model is a constant fallback (no regression).
        """
        fit = self._fit_mode_slope(fan_mode, hvac_mode)
        return None if fit is None else fit[2]

    def get_mode_time_constant(self, fan_mode: str, hvac_mode: str) -> float | None:
        """Return the thermal time constant τ (hours) for a profile.

        In a first-order RC thermal model the rate of approach to the setpoint is
        ``error / τ``, so the learned gain ``b`` (°C/h per °C) approximates ``1/τ``.
        A larger τ means more thermal inertia / resistance (slower response).
        Returns None when the gain is ~0 (constant model: τ is undefined).
        """
        gain = self.get_mode_slope_gain(fan_mode, hvac_mode)
        if gain < 1e-3:
            return None
        return 1.0 / gain

    def get_mode_effective_slope_at(self, fan_mode: str, hvac_mode: str, error: float) -> float | None:
        """Return the modelled effective slope at a given comfort error.

        The error is floored at 0: at/below the setpoint there is no driving
        force, so the modelled active cooling/heating rate is the intercept only.
        Returns None if the profile is not learned yet.
        """
        model = self.get_mode_slope_model(fan_mode, hvac_mode)
        if model is None:
            return None
        intercept_a, gain_b = model
        return intercept_a + gain_b * max(error, 0.0)

    def get_mode_effective_slope(self, fan_mode: str, hvac_mode: str) -> float | None:
        """Return the representative "working" effective slope for a profile.

        This is the gap-dependent model evaluated at REFERENCE_SLOPE_ERROR — a
        representative non-trivial gap — so the reported value reflects the fan's
        real cooling/heating power instead of the near-equilibrium median (which is
        structurally diluted by the many samples collected close to the setpoint).

        For legacy/synthetic constant profiles (gain == 0) this is exactly the old
        median estimator, preserving backward compatibility.

        Effective slope is positive when moving towards target:
        - In heating: positive raw slope is good
        - In cooling: negative raw slope is good (inverted)

        Returns None if fewer than MIN_MODE_PROFILE_SAMPLES are available.
        """
        model = self.get_mode_slope_model(fan_mode, hvac_mode)
        if model is None:
            return None
        intercept_a, gain_b = model
        return intercept_a + gain_b * REFERENCE_SLOPE_ERROR

    def get_profile_spread(self, fan_mode: str, hvac_mode: str) -> float | None:
        """Return the MAD/median ratio for a profile's absolute slopes.

        Measures internal consistency of collected slope samples:
        - 0.00–0.15 : good (tight cluster)
        - 0.15–0.30 : fair
        - > 0.30    : poor (high variability, low-confidence profile)

        Returns None if the profile has fewer than MIN_MODE_PROFILE_SAMPLES samples.
        """
        abs_slopes = [abs(s[2]) for s in self._slope_samples if s[1] == fan_mode and s[3] == hvac_mode]
        if len(abs_slopes) < MIN_MODE_PROFILE_SAMPLES:
            return None
        med = statistics.median(abs_slopes)
        if med < 0.01:
            return None
        mad = statistics.median([abs(s - med) for s in abs_slopes])
        return round(mad / med, 3)

    def set_mode_effective_slope(self, fan_mode: str, hvac_mode: str, target_slope: float) -> None:
        """Replace all samples for a fan/HVAC profile with synthetic ones producing target_slope.

        The raw slope stored in samples is the signed VTherm value:
        - In heating: raw slope == effective slope
        - In cooling: raw slope == -effective slope (inverted on read)
        """
        raw_slope = -target_slope if hvac_mode == "cool" else target_slope

        # Remove existing samples for this profile
        before = len(self._slope_samples)
        self._slope_samples = [s for s in self._slope_samples if not (s[1] == fan_mode and s[3] == hvac_mode)]
        removed = before - len(self._slope_samples)

        # Insert MIN_MODE_PROFILE_SAMPLES synthetic samples at current time.
        # error is None so they produce a constant model (gain 0) at exactly target_slope.
        now = time.time()
        for i in range(MIN_MODE_PROFILE_SAMPLES):
            self._slope_samples.append((now + i, fan_mode, raw_slope, hvac_mode, None))

        self.recompute_slope_stats()

        _LOGGER.info(
            "Learning: set_mode_effective_slope %s/%s = %.3f (removed %d, inserted %d synthetic samples)",
            hvac_mode, fan_mode, target_slope, removed, MIN_MODE_PROFILE_SAMPLES,
        )

    def get_mode_sample_count(self, fan_mode: str, hvac_mode: str) -> int:
        """Return the number of collected samples for one fan/HVAC profile."""
        return sum(1 for s in self._slope_samples if s[1] == fan_mode and s[3] == hvac_mode)

    def get_mode_real_sample_count(self, fan_mode: str, hvac_mode: str) -> int:
        """Return how many of a profile's samples are actual measurements.

        Synthetic samples (``set_mode_effective_slope``) and pre-schema ones carry
        no comfort error. Only measured samples can drive the regression, so this
        is the count that says whether the profile is *learned* rather than
        merely *seeded* -- the distinction the exploration guards in the MPC and
        the ``value_source`` attribute of the number entities rely on.
        """
        return sum(
            1
            for s in self._slope_samples
            if s[1] == fan_mode and s[3] == hvac_mode and len(s) > 4 and s[4] is not None
        )

    def has_measured_profile(self, fan_mode: str, hvac_mode: str) -> bool:
        """True once a profile rests on MIN_MODE_PROFILE_SAMPLES real measurements."""
        return self.get_mode_real_sample_count(fan_mode, hvac_mode) >= MIN_MODE_PROFILE_SAMPLES

    def get_known_fan_modes(self) -> list[str]:
        """Return unique fan modes seen in slope samples, preserving first-seen order."""
        seen: dict[str, None] = {}
        for s in self._slope_samples:
            seen[s[1]] = None
        return list(seen.keys())

    def get_mode_profiles(self, hvac_mode: str, fan_modes: list[str] | None = None) -> dict[str, dict]:
        """Return the learned profile summary for one HVAC mode."""
        if fan_modes:
            ordered_modes = list(dict.fromkeys(fan_modes))
        else:
            ordered_modes = sorted({s[1] for s in self._slope_samples if s[3] == hvac_mode})

        profiles: dict[str, dict] = {}
        for fan_mode in ordered_modes:
            fit = self._fit_mode_slope(fan_mode, hvac_mode)
            sample_count = self.get_mode_sample_count(fan_mode, hvac_mode)
            if fit is None:
                effective_slope = gain = r_squared = time_constant = None
            else:
                intercept_a, gain, r_squared = fit
                effective_slope = intercept_a + gain * REFERENCE_SLOPE_ERROR
                time_constant = (1.0 / gain) if gain >= 1e-3 else None
            profiles[fan_mode] = {
                "effective_slope": round(effective_slope, 3) if effective_slope is not None else None,
                "slope_gain": round(gain, 3) if gain is not None else None,
                "r_squared": round(r_squared, 3) if r_squared is not None else None,
                "thermal_time_constant_h": round(time_constant, 2) if time_constant is not None else None,
                "samples": sample_count,
                "real_samples": self.get_mode_real_sample_count(fan_mode, hvac_mode),
                "ready": fit is not None,
            }
        return profiles

    def compute_optimal_parameters(self) -> dict:
        """Calculate optimal parameters from learned data.

        The result is cached and invalidated whenever a new sample or response
        event is recorded, so repeated property accesses from sensors have
        zero recomputation cost.
        """
        if not self.is_ready():
            return {}

        if self._optimal_cache is not None:
            return self._optimal_cache

        # Use incremental statistics (already computed on each sample)
        if self._slope_count == 0:
            return {}

        # Variance from Welford's algorithm
        slope_variance = self._slope_m2 / (self._slope_count - 1) if self._slope_count > 1 else 0.01
        slope_stdev = slope_variance**0.5  # Standard deviation

        # Analyze response times (surfaced as response_samples/avg for diagnostics)
        response_times = [item[1] for item in self._response_events if item[1] > 0]
        # Use median instead of mean to be robust against outliers
        avg_response = statistics.median(response_times) if response_times else 10.0

        # Adapt thresholds to slope characteristics
        # High volatility → larger deadbands to avoid oscillations
        volatility_factor = min(slope_stdev / max(self._slope_mean, 0.1), 3.0)

        optimal_deadband = 0.15 + (volatility_factor * 0.2)

        _LOGGER.info(
            "Auto-calibration complete: avg_slope=%.2f std=%.2f max=%.2f | avg_response=%.1fmin | deadband=%.2f",
            self._slope_mean,
            slope_stdev,
            self._slope_max,
            avg_response,
            optimal_deadband,
        )

        result = {
            "deadband": round(optimal_deadband, 2),
            "samples_count": self._slope_count,
            "response_samples": len(response_times),
        }
        self._optimal_cache = result
        return result

    def to_dict(self) -> dict:
        """Serialize for storage.

        Both collections are capped to prevent unbounded storage growth:
        - slope_samples: newest MAX_STORED_SLOPE_SAMPLES entries, plus whatever
          older ones the per-profile retention keeps -- a plain tail cut dropped
          exactly the retained samples of rarely-used speeds, since those are by
          construction the oldest
        - response_events: last 100 entries (more than enough for statistics)
        """
        samples = self._slope_samples
        if len(samples) > MAX_STORED_SLOPE_SAMPLES:
            cutoff = sorted(s[0] for s in samples)[-MAX_STORED_SLOPE_SAMPLES]
            samples = self.trim_with_min_retention(samples, cutoff, PROFILE_RETENTION_SAMPLES)
        return {
            "slope_samples": samples,
            "response_events": self._response_events[-100:],
            "slope_count": self._slope_count,
            "slope_mean": self._slope_mean,
            "slope_m2": self._slope_m2,
            "slope_max": self._slope_max,
        }

    def recompute_slope_stats(self) -> None:
        """Rebuild Welford statistics from current sliding window."""
        self._slope_count = 0
        self._slope_mean = 0.0
        self._slope_m2 = 0.0
        self._slope_max = 0.0
        self._optimal_cache = None  # Invalidate cache when stats are rebuilt
        self._profile_ready_logged = {
            (s[3], s[1])
            for s in self._slope_samples
            if s[3] != "unknown" and self.get_mode_sample_count(s[1], s[3]) >= MIN_MODE_PROFILE_SAMPLES
        }

        for sample in self._slope_samples:
            self._update_slope_stats(abs(sample[2]))

        _LOGGER.debug(
            "Learning: Recomputed stats from window (count=%d, mean=%.3f, max=%.3f)",
            self._slope_count,
            self._slope_mean,
            self._slope_max,
        )

    @classmethod
    def from_dict(cls, data: dict):
        """Restore from storage.

        Handles backward compatibility for slope_samples across schema versions:
        - 3-tuple (timestamp, fan_mode, slope) → hvac_mode="unknown", error=None
        - 4-tuple (timestamp, fan_mode, slope, hvac_mode) → error=None
        - 5-tuple (timestamp, fan_mode, slope, hvac_mode, temperature_error) → as-is
        Samples without a stored error simply don't contribute to the gap-slope
        regression (they fall back to the constant median model).
        Old 2-tuple response_events are migrated to 3-tuples by appending hvac_mode="unknown".
        """
        instance = cls()

        # Migrate slope_samples to the canonical 5-tuple shape.
        raw_samples = data.get("slope_samples", [])
        instance._slope_samples = []
        for sample in raw_samples:
            if len(sample) == 3:
                instance._slope_samples.append((sample[0], sample[1], sample[2], "unknown", None))
            elif len(sample) == 4:
                instance._slope_samples.append((sample[0], sample[1], sample[2], sample[3], None))
            else:
                instance._slope_samples.append(tuple(sample))

        # Migrate response_events: support both 2-tuple (old) and 3-tuple (new)
        raw_response_events = data.get("response_events", [])
        instance._response_events = []
        for event in raw_response_events:
            if len(event) == 2:
                instance._response_events.append((event[0], event[1], "unknown"))
            else:
                instance._response_events.append(tuple(event))

        # Apply sliding window cleanup on restore, keeping the newest
        # PROFILE_RETENTION_SAMPLES per profile so rarely-used modes keep what
        # they gathered across weeks.
        cutoff_time = time.time() - (instance._learning_window_hours * 3600)
        instance._slope_samples = ThermalLearning.trim_with_min_retention(instance._slope_samples, cutoff_time, PROFILE_RETENTION_SAMPLES)
        instance._response_events = [item for item in instance._response_events if item[0] > cutoff_time]

        # Rebuild stats from cleaned window
        instance.recompute_slope_stats()

        # Mark as ready once if we have enough data
        if instance._slope_count >= instance._min_samples:
            instance._ready_once = True

        instance._optimal_cache = None
        _LOGGER.debug(
            "Learning: restored %d slope samples and %d response events from storage",
            len(instance._slope_samples),
            len(instance._response_events),
        )

        return instance
