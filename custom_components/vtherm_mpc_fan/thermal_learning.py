"""Learned thermal model: slope samples, response events, per-profile slope fits."""

import logging
import time
import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from .const import (
    MEASURED_PROFILE_MINUTES,
    MIN_MEASURED_PROFILE_SAMPLES,
    MIN_SAMPLES_LEARNING,
    MIN_MODE_PROFILE_SAMPLES,
    DEFAULT_DEAD_TIME,
    PROFILE_HVAC_MODES,
    PROFILE_RETENTION_SAMPLES,
    REFERENCE_SLOPE_ERROR,
    SAMPLE_INTERVAL_MINUTES,
)

#: Cap on persisted slope samples (see to_dict). ~7 days at 2-min intervals.
MAX_STORED_SLOPE_SAMPLES = 5000

# --- Persisted sample format ------------------------------------------------
# A slope sample is a tuple, persisted as a JSON list:
#   (timestamp, fan_mode, raw_slope, hvac_mode, comfort_error, regulation_offset, dwell_minutes)
# ``dwell_minutes`` is how much established regime the sample stands for (see
# SAMPLE_INTERVAL_MINUTES); a 6-item sample stands for SAMPLE_INTERVAL_MINUTES.
# Older stores hold shorter tuples, all still accepted by from_dict():
#   3 items: no hvac_mode, no error          -> hvac "unknown", error None
#   4 items: no error                        -> error None (a constant, like a seed)
#   5 items: "legacy" measurement            -> see LEGACY_SAMPLE_WEIGHT
# ``regulation_offset`` is VTherm's regulated setpoint minus the user's (raw
# degC, None when unknown). The store also carries ``format`` (see
# LEARNING_DATA_FORMAT); an older version of the plugin ignores both the key
# and the trailing tuple items, so the format is readable in both directions.
LEARNING_DATA_FORMAT = 2

# Samples recorded before format 2 carry an error measured against VTherm's
# *regulated* setpoint, not the user's. The two differ by the auto-regulation
# offset, which was not stored (0.6 degC in median on the production trace while
# the strongest speed ran), so their error cannot be recomputed. They are kept --
# an existing installation must not lose its profiles on upgrade -- but weigh
# this much of a current sample in the fits, and they age out of the 7-day
# window / per-profile retention like any other sample.
LEGACY_SAMPLE_WEIGHT = 0.25
# Legacy samples were only taken on a distinct sensor reading and carry no
# dwell. Ten of them used to make a profile "measured"; crediting each with 9
# minutes keeps exactly that (10 x 9 = MEASURED_PROFILE_MINUTES), so an upgrade
# changes no profile's status.
LEGACY_SAMPLE_DWELL_MINUTES = 9.0

# Consecutive samples of one regime are strongly autocorrelated (VTherm's slope
# is an EMA and the sensor moves rarely), so n samples carry the information of
# n_eff = n / (1 + 2 * rho), rho the lag-1 autocorrelation of the fit residuals
# within a run of samples. DEFAULT_SAMPLE_AUTOCORRELATION is used while too few
# consecutive pairs exist to estimate it.
DEFAULT_SAMPLE_AUTOCORRELATION = 0.5
MAX_SAMPLE_AUTOCORRELATION = 0.9
MIN_AUTOCORRELATION_PAIRS = 5
# A profile's gain b is shrunk toward the gain pooled over every speed of the
# HVAC mode, as if GAIN_PRIOR_SAMPLES independent samples had shown the pooled
# value: b = (n_eff * b_profile + k * b_pool) / (n_eff + k). A thin profile
# cannot then invent a steep gain from a few autocorrelated points.
GAIN_PRIOR_SAMPLES = 10.0

# The line is fitted by a weighted Theil-Sen estimator (weighted median of the
# pairwise slopes, then weighted median of the intercepts): a few contaminated
# samples (a door opened, a transient mislabelled established) cannot drag it,
# where least squares followed any three outliers out of fifteen. Pairs are
# O(n^2), so at most THEIL_SEN_MAX_POINTS samples, evenly spread in time, enter
# the estimator; every sample still enters the effective sample size.
THEIL_SEN_MAX_POINTS = 150
# Uncertainty assumed for a profile the user seeded by hand (degC/h): a guess,
# not a measurement.
SEEDED_SLOPE_SIGMA = 0.3

# The regulation offset (sign-aligned, positive = VTherm asks the unit for more)
# is a proxy for how hard the inverter compressor is driven, which the fan speed
# alone does not capture. It enters a profile's fit as a second regressor,
# slope = a + b*error + c*offset_residual, only when it varied enough to be
# identified, and with c shrunk toward 0 as if OFFSET_PRIOR_SAMPLES observations
# with an offset spread of OFFSET_PRIOR_SPREAD had shown no effect.
OFFSET_MIN_SAMPLES = 10
OFFSET_MIN_SPREAD = 0.1  # degC, standard deviation of the offset not explained by the error
OFFSET_PRIOR_SAMPLES = 10.0
OFFSET_PRIOR_SPREAD = 0.2  # degC


@dataclass(frozen=True, slots=True)
class ProfileFit:
    """The fitted slope model of one (fan_mode, hvac_mode) profile.

    ``slope(error, offset) = intercept + gain * error
    + offset_gain * (offset - (offset_base + offset_trend * error))``

    where ``offset`` is the sign-aligned regulation offset. ``offset_base`` and
    ``offset_trend`` describe the offset usually seen at a given error, so the
    offset term only adds what the error does not already explain, and is zero
    whenever the offset is the usual one (or unknown).
    """

    intercept: float
    gain: float
    r_squared: float | None
    effective_samples: float | None = None
    offset_gain: float = 0.0
    offset_base: float = 0.0
    offset_trend: float = 0.0
    # Largest comfort error the measurements covered (None: a seed, no envelope).
    # The line is never evaluated past it: a speed only ever seen near the
    # setpoint says nothing about what it does 1 degC away.
    error_max: float | None = None
    # Standard deviation (degC/h) of the representative slope estimate.
    slope_sigma: float | None = None

    @property
    def reference_error(self) -> float:
        """The comfort error the representative slope is evaluated at (no extrapolation)."""
        if self.error_max is None:
            return REFERENCE_SLOPE_ERROR
        return max(0.0, min(REFERENCE_SLOPE_ERROR, self.error_max))

    @property
    def partial(self) -> bool:
        """True when the measurements never reached REFERENCE_SLOPE_ERROR."""
        return self.error_max is not None and self.error_max < REFERENCE_SLOPE_ERROR

    def slope_at(self, error: float) -> float:
        """Return the modelled slope at *error*, floored at 0 and capped at the envelope."""
        error = max(error, 0.0)
        if self.error_max is not None:
            error = min(error, max(self.error_max, 0.0))
        return self.intercept + self.gain * error

    def offset_correction(self, error: float, demand: float | None) -> float:
        """Return the slope correction for an unusual (sign-aligned) regulation offset, 0 when unknown."""
        if demand is None or self.offset_gain == 0.0:
            return 0.0
        return self.offset_gain * (demand - (self.offset_base + self.offset_trend * error))


def demand_offset(regulation_offset: float | None, hvac_mode: str) -> float | None:
    """Return the regulation offset sign-aligned so that positive means "asks for more".

    In heat a regulated setpoint above the user's pushes the unit harder; in
    cool it is a regulated setpoint *below* the user's.
    """
    if regulation_offset is None:
        return None
    return -regulation_offset if hvac_mode == "cool" else regulation_offset


def sample_weight(sample) -> float:
    """Return a measurement's weight in the fits (see LEGACY_SAMPLE_WEIGHT)."""
    return 1.0 if len(sample) > 5 else LEGACY_SAMPLE_WEIGHT


def sample_dwell(sample) -> float:
    """Return the minutes of established regime a measurement stands for."""
    if len(sample) > 6 and sample[6] is not None:
        return float(sample[6])
    return SAMPLE_INTERVAL_MINUTES if len(sample) > 5 else LEGACY_SAMPLE_DWELL_MINUTES


def is_measurement(sample) -> bool:
    """True for a measured sample (it carries a comfort error), False for a seed."""
    return len(sample) > 4 and sample[4] is not None


def sample_offset(sample) -> float | None:
    """Return a sample's raw regulation offset, None when unknown or legacy."""
    return sample[5] if len(sample) > 5 else None


def weighted_median(values: list[float], weights: list[float]) -> float:
    """Return the weighted median; the plain median when every weight is equal."""
    if len(set(weights)) <= 1:
        return statistics.median(values)
    pairs = sorted(zip(values, weights))
    half = sum(weights) / 2.0
    cumulative = 0.0
    for index, (value, weight) in enumerate(pairs):
        cumulative += weight
        if cumulative > half:
            return value
        if cumulative == half and index + 1 < len(pairs):
            return (value + pairs[index + 1][0]) / 2.0
    return pairs[-1][0]


def weighted_theil_sen(points: list[tuple[float, float, float]]) -> tuple[float, float] | None:
    """Weighted Theil-Sen fit of y on x over ``(x, y, w)``: (intercept, slope).

    The slope is the weighted median of the pairwise slopes (pair weight
    w_i * w_j, pairs with no x spread skipped), the intercept the weighted
    median of y - slope * x. None when no pair has an x spread.
    """
    slopes: list[float] = []
    weights: list[float] = []
    for index, (x_i, y_i, w_i) in enumerate(points):
        for x_j, y_j, w_j in points[index + 1 :]:
            if abs(x_j - x_i) < 1e-6:
                continue
            slopes.append((y_j - y_i) / (x_j - x_i))
            weights.append(w_i * w_j)
    if not slopes:
        return None
    slope = weighted_median(slopes, weights)
    intercept = weighted_median([y - slope * x for x, y, _ in points], [w for _, _, w in points])
    return intercept, slope


def thin_evenly(items: list, limit: int) -> list:
    """Return at most *limit* items of *items*, evenly spread over the list."""
    if len(items) <= limit:
        return items
    step = len(items) / limit
    return [items[int(index * step)] for index in range(limit)]


def weighted_line(points: list[tuple[float, float, float]]) -> tuple[float, float, float, float] | None:
    """Weighted least squares of y on x over ``(x, y, w)``: (intercept, slope, sxx, mean_x).

    None when x has no spread (sxx below 1e-6).
    """
    total = sum(w for _, _, w in points)
    if total <= 0:
        return None
    mean_x = sum(w * x for x, _, w in points) / total
    mean_y = sum(w * y for _, y, w in points) / total
    sxx = sum(w * (x - mean_x) ** 2 for x, _, w in points)
    if sxx < 1e-6:
        return None
    sxy = sum(w * (x - mean_x) * (y - mean_y) for x, y, w in points)
    slope = sxy / sxx
    return mean_y - slope * mean_x, slope, sxx, mean_x


_LOGGER = logging.getLogger(__name__)


class ThermalLearning:
    """Auto-calibration of thermal parameters based on observed system behavior."""

    def __init__(self, clock: Callable[[], float] | None = None):
        # Wall clock (epoch seconds). Injected by tests and by offline benches
        # that replay a trace at their own pace; time.time otherwise, looked up
        # at call time so that patching time.time still works.
        self._clock = clock
        # Data collection with sliding window
        self._slope_samples = []  # (timestamp, fan_mode, slope, hvac_mode, temperature_error, regulation_offset)
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

        # Per-profile regression results, keyed (fan_mode, hvac_mode). Every
        # entity and every MPC candidate reads the same few fits several times
        # per cycle; they only change when the sample list does.
        self._fit_cache: dict[tuple[str, str], ProfileFit | None] = {}
        self._pooled_gain_cache: dict[str, float] = {}
        # Exploration probes (see MPCController): when each (hvac_mode, fan_mode)
        # was last probed, and how many probes were started overall. Persisted so
        # a restart neither re-probes at once nor loses the count.
        self._probe_times: dict[str, float] = {}
        self._probe_count = 0

    def now(self) -> float:
        """Return the current time (epoch seconds) from the injected clock."""
        return self._clock() if self._clock is not None else time.time()

    @staticmethod
    def _probe_key(fan_mode: str, hvac_mode: str) -> str:
        """Return the persisted key of one profile's probe timestamp."""
        return f"{hvac_mode}|{fan_mode}"

    def record_probe(self, fan_mode: str, hvac_mode: str) -> None:
        """Remember that an exploration probe of this profile starts now."""
        self._probe_times[self._probe_key(fan_mode, hvac_mode)] = self.now()
        self._probe_count += 1

    def last_probe_time(self, fan_mode: str, hvac_mode: str) -> float | None:
        """Return when this profile was last probed (epoch seconds), None if never."""
        return self._probe_times.get(self._probe_key(fan_mode, hvac_mode))

    @property
    def probe_count(self) -> int:
        """Return how many exploration probes were started (persisted)."""
        return self._probe_count

    def _invalidate_fits(self) -> None:
        """Drop every cached fit (per profile and pooled); the samples changed."""
        self._fit_cache.clear()
        self._pooled_gain_cache.clear()

    def reset(self) -> None:
        """Reset all learning data and statistics."""
        self._slope_samples.clear()
        self._response_events.clear()
        self._slope_count = 0
        self._slope_mean = 0.0
        self._slope_m2 = 0.0
        self._slope_max = 0.0
        self._ready_once = False
        self._invalidate_fits()
        self._profile_ready_logged.clear()
        self._probe_times.clear()
        self._probe_count = 0
        _LOGGER.info("Learning: reset requested; data cleared")

    def add_slope_sample(
        self,
        fan_mode: str,
        slope: float,
        temperature_error: float = 0,
        hvac_mode: str = "unknown",
        is_window_open: bool = False,
        regulation_offset: float | None = None,
        dwell_minutes: float | None = None,
    ):
        """Record slope only if in normal operating range.

        ``temperature_error`` is the comfort error against the *user's* setpoint
        and ``regulation_offset`` VTherm's regulated setpoint minus the user's
        (raw degC, None when unknown): see ``ProfileFit`` for how it is used.
        ``dwell_minutes`` is the established regime the sample stands for
        (SAMPLE_INTERVAL_MINUTES when not given); the profile counts as measured
        once these add up to MEASURED_PROFILE_MINUTES.

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

        dwell = SAMPLE_INTERVAL_MINUTES if dwell_minutes is None else max(0.0, float(dwell_minutes))
        self._slope_samples.append((self.now(), fan_mode, slope, hvac_mode, temperature_error, regulation_offset, dwell))
        self._invalidate_fits()
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

        self._update_slope_stats(abs(slope))

        profile_key = (hvac_mode, fan_mode)
        if hvac_mode != "unknown" and profile_key not in self._profile_ready_logged and self.has_measured_profile(fan_mode, hvac_mode):
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
        cutoff_time = self.now() - (self._learning_window_hours * 3600)
        before = len(self._slope_samples)
        self._slope_samples = self.trim_with_min_retention(self._slope_samples, cutoff_time, PROFILE_RETENTION_SAMPLES)
        # The trim may reorder samples even when it drops none, and the
        # regression sums in list order: never serve a fit from the old order.
        self._invalidate_fits()

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
        self._response_events.append((self.now(), minutes_to_response, hvac_mode))
        _LOGGER.debug(
            "Learning: Recorded response time #%d: %.1f min (hvac=%s)",
            len(self._response_events),
            minutes_to_response,
            hvac_mode,
        )

        # Cleanup: keep only data within sliding window (7 days)
        cutoff_time = self.now() - (self._learning_window_hours * 3600)
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
        self._invalidate_fits()

    @property
    def response_events(self) -> list:
        """Return the list of response events."""
        return self._response_events

    @response_events.setter
    def response_events(self, value: list) -> None:
        self._response_events = value

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

    def _fit_mode_slope(self, fan_mode: str, hvac_mode: str) -> ProfileFit | None:
        """Return the profile's fit, computed once per change of the sample list.

        See :meth:`_compute_mode_fit` for the model. The result is a frozen
        ``ProfileFit`` (or None), so it is safe to share.
        """
        key = (fan_mode, hvac_mode)
        if key not in self._fit_cache:
            self._fit_cache[key] = self._compute_mode_fit(fan_mode, hvac_mode)
        return self._fit_cache[key]

    def _compute_mode_fit(self, fan_mode: str, hvac_mode: str) -> ProfileFit | None:
        """Fit the gap-dependent slope model of one profile.

        The effective cooling/heating rate is not constant: it grows with the
        comfort error (distance to the user's setpoint) -- mostly because the
        inverter compressor is driven harder further from it. We model it as a
        linear relationship:

            effective_slope(error) = a + b * error

        fitted by weighted least squares over the profile's (error,
        effective_slope) samples, legacy samples weighing LEGACY_SAMPLE_WEIGHT.
        ``error`` is the signed comfort error (positive = needs more
        cooling/heating). ``effective_slope`` is positive when moving towards
        target (raw VTherm slope is inverted in cooling). When the regulation
        offset varied enough, a shrunk second term models it (see ProfileFit).

        The samples are autocorrelated: the fit reports its effective sample
        size, and the gain is shrunk toward the gain pooled over the HVAC mode's
        speeds in proportion to it (GAIN_PRIOR_SAMPLES).

        ``r_squared`` is the coefficient of determination of the fit (0..1); it is
        ``None`` for the constant fallback (no real regression was performed).

        Falls back to a constant model ``(median_effective_slope, 0.0, None)`` when
        the error has no spread. A profile that is not measured yet (see
        :meth:`has_measured_profile`) but was seeded blends the seeded and
        measured medians.

        The gain ``b`` is clamped to be non-negative: a larger gap can only cool/heat
        at least as fast, never slower.

        Returns None for a profile neither measured nor seeded.
        """
        sign = -1.0 if hvac_mode == "cool" else 1.0
        matching = [s for s in self._slope_samples if s[1] == fan_mode and s[3] == hvac_mode]
        # Only samples that carry a stored error are measurements; the others are
        # synthetic (set_mode_effective_slope) or predate the error column.
        measured = sorted((s for s in matching if is_measurement(s)), key=lambda s: s[0])
        points = [(s[4], sign * s[2], sample_weight(s)) for s in measured]
        constants = [sign * s[2] for s in matching if not is_measurement(s)]
        if not self._covers_measured_profile(measured):
            if not constants:
                return None
            # Not enough measurements for a regression. A plain median over the
            # mixed set would return the seeded value unchanged until the real
            # samples outnumber the synthetic ones (ten identical values are the
            # median of any set of fewer than twenty), hiding every bit of
            # progress. Weight the two medians by their counts instead, so each
            # measurement visibly pulls the profile toward what was observed.
            median_constant = statistics.median(constants)
            if not points:
                return ProfileFit(median_constant, 0.0, None, slope_sigma=SEEDED_SLOPE_SIGMA)
            median_measured = weighted_median([y for _, y, _ in points], [w for _, _, w in points])
            weight = len(points) / (len(points) + len(constants))
            return ProfileFit(median_constant + weight * (median_measured - median_constant), 0.0, None, slope_sigma=SEEDED_SLOPE_SIGMA)

        total = sum(w for _, _, w in points)
        error_max = max(x for x, _, _ in points)
        robust = weighted_theil_sen(thin_evenly(points, THEIL_SEN_MAX_POINTS))
        if robust is None:
            # All samples taken at (nearly) the same error: no slope can be fitted.
            median_y = weighted_median([y for _, y, _ in points], [w for _, _, w in points])
            residuals = [y - median_y for _, y, _ in points]
            n_eff = self._effective_sample_count(measured, residuals, total)
            constant = ProfileFit(
                median_y,
                0.0,
                None,
                effective_samples=n_eff,
                error_max=error_max,
                slope_sigma=self._slope_sigma(residuals, [w for _, _, w in points], n_eff),
            )
            return self._fit_offset_term(constant, measured, sign, hvac_mode)

        intercept_a, gain_b = robust
        n_eff = self._effective_sample_count(measured, [y - (intercept_a + gain_b * x) for x, y, _ in points], total)
        gain_b = max(0.0, gain_b)
        pooled = self._pooled_gain(hvac_mode)
        gain_b = (n_eff * gain_b + GAIN_PRIOR_SAMPLES * pooled) / (n_eff + GAIN_PRIOR_SAMPLES)
        intercept_a = weighted_median([y - gain_b * x for x, y, _ in points], [w for _, _, w in points])

        # Coefficient of determination against the (clamped, shrunk) fitted line.
        mean_y = sum(w * y for _, y, w in points) / total
        ss_tot = sum(w * (y - mean_y) ** 2 for _, y, w in points)
        residuals = [y - (intercept_a + gain_b * x) for x, y, _ in points]
        if ss_tot < 1e-9:
            r_squared = None
        else:
            ss_res = sum(w * r * r for (_, _, w), r in zip(points, residuals))
            r_squared = max(0.0, 1.0 - ss_res / ss_tot)
        mean_x = sum(w * x for x, _, w in points) / total
        sxx = sum(w * (x - mean_x) ** 2 for x, _, w in points) * (n_eff / total)
        reference = max(0.0, min(REFERENCE_SLOPE_ERROR, error_max))
        sigma = self._slope_sigma(residuals, [w for _, _, w in points], n_eff)
        if sigma is not None and sxx > 1e-9:
            sigma *= (1.0 + n_eff * (reference - mean_x) ** 2 / sxx) ** 0.5
        fit = ProfileFit(intercept_a, gain_b, r_squared, effective_samples=n_eff, error_max=error_max, slope_sigma=sigma)
        return self._fit_offset_term(fit, measured, sign, hvac_mode)

    @staticmethod
    def _slope_sigma(residuals: list[float], weights: list[float], n_eff: float) -> float | None:
        """Return the standard deviation of a fitted level: robust scale / sqrt(n_eff).

        The scale is 1.4826 x the weighted median absolute residual, floored at
        0.02 degC/h so a profile of identical readings is not taken as certain.
        """
        if n_eff <= 0:
            return None
        scale = 1.4826 * weighted_median([abs(r) for r in residuals], weights)
        return max(scale, 0.02) / n_eff**0.5

    @staticmethod
    def _covers_measured_profile(measured: list) -> bool:
        """True when *measured* samples cover MEASURED_PROFILE_MINUTES of regime, in enough samples."""
        if len(measured) < MIN_MEASURED_PROFILE_SAMPLES:
            return False
        return sum(sample_dwell(s) for s in measured) >= MEASURED_PROFILE_MINUTES - 1e-6

    @staticmethod
    def _effective_sample_count(measured: list, residuals: list[float], total_weight: float) -> float:
        """Return n_eff = weight / (1 + 2 rho) for time-sorted *measured* samples.

        rho is the lag-1 autocorrelation of the fit residuals over consecutive
        samples of one run (gap <= 2.5 x SAMPLE_INTERVAL_MINUTES), clamped to
        [0, MAX_SAMPLE_AUTOCORRELATION]; DEFAULT_SAMPLE_AUTOCORRELATION when
        fewer than MIN_AUTOCORRELATION_PAIRS pairs exist or the residuals are flat.
        """
        max_gap = 2.5 * SAMPLE_INTERVAL_MINUTES * 60.0
        products = 0.0
        squares = 0.0
        pairs = 0
        for index in range(1, len(measured)):
            if measured[index][0] - measured[index - 1][0] > max_gap:
                continue
            first, second = residuals[index - 1], residuals[index]
            products += first * second
            squares += (first * first + second * second) / 2.0
            pairs += 1
        if pairs < MIN_AUTOCORRELATION_PAIRS or squares < 1e-12:
            rho = DEFAULT_SAMPLE_AUTOCORRELATION
        else:
            rho = min(MAX_SAMPLE_AUTOCORRELATION, max(0.0, products / squares))
        return total_weight / (1.0 + 2.0 * rho)

    def _pooled_gain(self, hvac_mode: str) -> float:
        """Return the gain pooled over every speed of *hvac_mode* (within-speed slope).

        Each speed keeps its own intercept; only the dependence on the error is
        shared, which is what the profiles have in common (the compressor's
        response to the gap). Robust like the per-profile fit: the weighted
        median of the pairwise slopes taken *within* each speed. Clamped
        non-negative, 0 when nothing varies.
        """
        if hvac_mode in self._pooled_gain_cache:
            return self._pooled_gain_cache[hvac_mode]
        sign = -1.0 if hvac_mode == "cool" else 1.0
        by_fan: dict[str, list[tuple[float, float, float]]] = {}
        for s in sorted(self._slope_samples, key=lambda sample: sample[0]):
            if s[3] == hvac_mode and is_measurement(s):
                by_fan.setdefault(s[1], []).append((s[4], sign * s[2], sample_weight(s)))
        slopes: list[float] = []
        weights: list[float] = []
        for points in by_fan.values():
            thinned = thin_evenly(points, THEIL_SEN_MAX_POINTS)
            for index, (x_i, y_i, w_i) in enumerate(thinned):
                for x_j, y_j, w_j in thinned[index + 1 :]:
                    if abs(x_j - x_i) >= 1e-6:
                        slopes.append((y_j - y_i) / (x_j - x_i))
                        weights.append(w_i * w_j)
        pooled = max(0.0, weighted_median(slopes, weights)) if slopes else 0.0
        self._pooled_gain_cache[hvac_mode] = pooled
        return pooled

    @staticmethod
    def _fit_offset_term(fit: ProfileFit, measured: list, sign: float, hvac_mode: str) -> ProfileFit:
        """Add the regulation-offset term to *fit* when the data can identify it.

        Frisch-Waugh: the offset is first regressed on the error, and only the
        part the error does not explain (its residual) is related to the slope
        residual. That leaves ``a`` and ``b`` exactly as fitted, and keeps a
        compressor that simply follows the error from being counted twice.
        Skipped below OFFSET_MIN_SAMPLES samples with a known offset or when the
        unexplained offset spread is under OFFSET_MIN_SPREAD; the gain is shrunk
        toward 0 and clamped non-negative (more demand never cools/heats less).
        """
        rows = []
        for sample in measured:
            offset = demand_offset(sample_offset(sample), hvac_mode)
            if offset is not None:
                rows.append((sample[4], sign * sample[2], offset))
        if len(rows) < OFFSET_MIN_SAMPLES:
            return fit
        trend = weighted_line([(x, d, 1.0) for x, _, d in rows])
        if trend is None:
            base, slope_d = sum(d for _, _, d in rows) / len(rows), 0.0
        else:
            base, slope_d = trend[0], trend[1]
        residuals = [(d - (base + slope_d * x), y - (fit.intercept + fit.gain * x)) for x, y, d in rows]
        spread_sq = sum(rd * rd for rd, _ in residuals)
        if (spread_sq / len(residuals)) ** 0.5 < OFFSET_MIN_SPREAD:
            return fit
        offset_gain = sum(rd * ry for rd, ry in residuals) / (spread_sq + OFFSET_PRIOR_SAMPLES * OFFSET_PRIOR_SPREAD**2)
        if offset_gain <= 0.0:
            return fit
        return ProfileFit(fit.intercept, fit.gain, fit.r_squared, fit.effective_samples, offset_gain, base, slope_d, fit.error_max, fit.slope_sigma)

    def get_mode_fit(self, fan_mode: str, hvac_mode: str) -> ProfileFit | None:
        """Return the full fitted model of a profile (see ProfileFit), None if unknown."""
        return self._fit_mode_slope(fan_mode, hvac_mode)

    def get_mode_slope_model(self, fan_mode: str, hvac_mode: str) -> tuple[float, float] | None:
        """Return the gap-dependent slope model ``(intercept_a, gain_b)`` for a profile.

        See :meth:`_compute_mode_fit` for the model definition. Returns None for
        a profile neither measured nor seeded.
        """
        fit = self._fit_mode_slope(fan_mode, hvac_mode)
        return None if fit is None else (fit.intercept, fit.gain)

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
        return None if fit is None else fit.r_squared

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

    def get_mode_offset_correction(self, fan_mode: str, hvac_mode: str, error: float, regulation_offset: float | None) -> float:
        """Return the slope correction a profile's fit applies for this regulation offset.

        ``regulation_offset`` is raw (regulated minus user setpoint). 0.0 when the
        profile is unknown, has no offset term, or the offset is unknown.
        """
        fit = self._fit_mode_slope(fan_mode, hvac_mode)
        if fit is None:
            return 0.0
        return fit.offset_correction(max(error, 0.0), demand_offset(regulation_offset, hvac_mode))

    def get_mode_effective_slope_at(self, fan_mode: str, hvac_mode: str, error: float) -> float | None:
        """Return the modelled effective slope at a given comfort error.

        The error is floored at 0: at/below the setpoint there is no driving
        force, so the modelled active cooling/heating rate is the intercept only.
        It is also capped at the largest error the measurements covered: the
        line is never extrapolated. Returns None if the profile is not learned yet.
        """
        fit = self._fit_mode_slope(fan_mode, hvac_mode)
        return None if fit is None else fit.slope_at(error)

    def get_mode_effective_slope(self, fan_mode: str, hvac_mode: str) -> float | None:
        """Return the representative "working" effective slope for a profile.

        This is the gap-dependent model evaluated at REFERENCE_SLOPE_ERROR — a
        representative non-trivial gap — so the reported value reflects the fan's
        real cooling/heating power instead of the near-equilibrium median (which is
        structurally diluted by the many samples collected close to the setpoint).
        A profile whose measurements never reached that gap is evaluated at the
        largest one they did (``ProfileFit.reference_error``): extrapolating a line
        fitted on errors of 0-0.3 degC to 1 degC turned noise into capacity (P95 of
        2.5 degC/h for a true 0.3 with realistic noise) and the profile is then
        *partial*.

        For legacy/synthetic constant profiles (gain == 0) this is exactly the old
        median estimator, preserving backward compatibility.

        Effective slope is positive when moving towards target:
        - In heating: positive raw slope is good
        - In cooling: negative raw slope is good (inverted)

        Returns None if fewer than MIN_MODE_PROFILE_SAMPLES are available.
        """
        fit = self._fit_mode_slope(fan_mode, hvac_mode)
        if fit is None:
            return None
        return fit.intercept + fit.gain * fit.reference_error

    def is_profile_partial(self, fan_mode: str, hvac_mode: str) -> bool:
        """True when a measured profile never covered REFERENCE_SLOPE_ERROR."""
        fit = self._fit_mode_slope(fan_mode, hvac_mode)
        return fit is not None and fit.partial

    def get_mode_slope_sigma(self, fan_mode: str, hvac_mode: str) -> float | None:
        """Return the uncertainty (degC/h) of a profile's representative slope."""
        fit = self._fit_mode_slope(fan_mode, hvac_mode)
        return None if fit is None else fit.slope_sigma

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
        self._invalidate_fits()
        removed = before - len(self._slope_samples)

        # Insert MIN_MODE_PROFILE_SAMPLES synthetic samples at current time.
        # error is None so they produce a constant model (gain 0) at exactly target_slope.
        now = self.now()
        for i in range(MIN_MODE_PROFILE_SAMPLES):
            self._slope_samples.append((now + i, fan_mode, raw_slope, hvac_mode, None))

        self.recompute_slope_stats()

        _LOGGER.info(
            "Learning: set_mode_effective_slope %s/%s = %.3f (removed %d, inserted %d synthetic samples)",
            hvac_mode,
            fan_mode,
            target_slope,
            removed,
            MIN_MODE_PROFILE_SAMPLES,
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
        return sum(1 for s in self._slope_samples if s[1] == fan_mode and s[3] == hvac_mode and is_measurement(s))

    def get_mode_measured_minutes(self, fan_mode: str, hvac_mode: str) -> float:
        """Return the established regime (minutes) a profile's measurements cover."""
        return sum(sample_dwell(s) for s in self._slope_samples if s[1] == fan_mode and s[3] == hvac_mode and is_measurement(s))

    def has_measured_profile(self, fan_mode: str, hvac_mode: str) -> bool:
        """True once a profile's measurements cover MEASURED_PROFILE_MINUTES of regime.

        At least MIN_MEASURED_PROFILE_SAMPLES of them are required too. Seeded
        values never count: they are what the user guessed, not what the room did.
        """
        return self._covers_measured_profile([s for s in self._slope_samples if s[1] == fan_mode and s[3] == hvac_mode and is_measurement(s)])

    def is_profile_ready(self, fan_mode: str, hvac_mode: str) -> bool:
        """True when a profile has a value to serve: measured, or seeded by the user."""
        return self._fit_mode_slope(fan_mode, hvac_mode) is not None

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
                offset_gain = None
            else:
                intercept_a, gain, r_squared = fit.intercept, fit.gain, fit.r_squared
                offset_gain = fit.offset_gain
                effective_slope = intercept_a + gain * fit.reference_error
                time_constant = (1.0 / gain) if gain >= 1e-3 else None
            profiles[fan_mode] = {
                "effective_slope": round(effective_slope, 3) if effective_slope is not None else None,
                "slope_gain": round(gain, 3) if gain is not None else None,
                "r_squared": round(r_squared, 3) if r_squared is not None else None,
                "thermal_time_constant_h": round(time_constant, 2) if time_constant is not None else None,
                "offset_gain": round(offset_gain, 3) if offset_gain is not None else None,
                "effective_samples": round(fit.effective_samples, 1) if fit is not None and fit.effective_samples is not None else None,
                "measured_minutes": round(self.get_mode_measured_minutes(fan_mode, hvac_mode), 1),
                "measured": self.has_measured_profile(fan_mode, hvac_mode),
                "reference_error": round(fit.reference_error, 2) if fit is not None else None,
                "partial": fit.partial if fit is not None else None,
                "slope_sigma": round(fit.slope_sigma, 3) if fit is not None and fit.slope_sigma is not None else None,
                "legacy_samples": sum(1 for s in self._slope_samples if s[1] == fan_mode and s[3] == hvac_mode and len(s) == 5 and s[4] is not None),
                "samples": sample_count,
                "real_samples": self.get_mode_real_sample_count(fan_mode, hvac_mode),
                "ready": fit is not None,
            }
        return profiles

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
            "format": LEARNING_DATA_FORMAT,
            "slope_samples": samples,
            "response_events": self._response_events[-100:],
            "slope_count": self._slope_count,
            "slope_mean": self._slope_mean,
            "slope_m2": self._slope_m2,
            "slope_max": self._slope_max,
            "probe_times": dict(self._probe_times),
            "probe_count": self._probe_count,
        }

    def recompute_slope_stats(self) -> None:
        """Rebuild Welford statistics from current sliding window."""
        self._slope_count = 0
        self._slope_mean = 0.0
        self._slope_m2 = 0.0
        self._slope_max = 0.0
        self._invalidate_fits()
        # One counting pass: asking get_mode_sample_count() for every sample
        # rescanned the whole list each time, O(n^2) on the event loop at load.
        profile_counts = Counter((s[3], s[1]) for s in self._slope_samples)
        self._profile_ready_logged = {profile for profile, count in profile_counts.items() if profile[0] != "unknown" and count >= MIN_MODE_PROFILE_SAMPLES}

        for sample in self._slope_samples:
            self._update_slope_stats(abs(sample[2]))

        _LOGGER.debug(
            "Learning: Recomputed stats from window (count=%d, mean=%.3f, max=%.3f)",
            self._slope_count,
            self._slope_mean,
            self._slope_max,
        )

    @classmethod
    def from_dict(cls, data: dict, clock: Callable[[], float] | None = None):
        """Restore from storage.

        Handles backward compatibility for slope_samples across schema versions:
        - 3-tuple (timestamp, fan_mode, slope) → hvac_mode="unknown", error=None
        - 4-tuple (timestamp, fan_mode, slope, hvac_mode) → error=None
        - 5-tuple (timestamp, fan_mode, slope, hvac_mode, temperature_error) → kept
          as a 5-tuple: a *legacy* measurement, whose error was taken against the
          regulated setpoint (see LEGACY_SAMPLE_WEIGHT)
        - 6-tuple (…, temperature_error, regulation_offset) → dwell SAMPLE_INTERVAL_MINUTES
        - 7-tuple (…, regulation_offset, dwell_minutes) → current format
        Samples without a stored error simply don't contribute to the gap-slope
        regression (they fall back to the constant median model). Nothing is
        dropped or rewritten: an existing installation restarts with every
        profile it had.
        Old 2-tuple response_events are migrated to 3-tuples by appending hvac_mode="unknown".
        """
        instance = cls(clock=clock)

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
        cutoff_time = instance.now() - (instance._learning_window_hours * 3600)
        probe_times = data.get("probe_times")
        if isinstance(probe_times, dict):
            instance._probe_times = {str(key): float(value) for key, value in probe_times.items() if isinstance(value, (int, float))}
        instance._probe_count = int(data.get("probe_count", 0) or 0)
        instance._slope_samples = ThermalLearning.trim_with_min_retention(instance._slope_samples, cutoff_time, PROFILE_RETENTION_SAMPLES)
        instance._response_events = [item for item in instance._response_events if item[0] > cutoff_time]

        # Rebuild stats from cleaned window
        instance.recompute_slope_stats()

        # Mark as ready once if we have enough data
        if instance._slope_count >= instance._min_samples:
            instance._ready_once = True

        _LOGGER.debug(
            "Learning: restored %d slope samples and %d response events from storage",
            len(instance._slope_samples),
            len(instance._response_events),
        )

        return instance
