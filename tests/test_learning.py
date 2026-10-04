"""Tests for ThermalLearning auto-calibration."""

import pytest
from custom_components.vtherm_mpc_fan.thermal_learning import ThermalLearning
from custom_components.vtherm_mpc_fan.mpc_controller import MPCController
from custom_components.vtherm_mpc_fan.sensor import SmartFanLearnedDeadTimeSensor, SmartFanEffectiveTimeoutSensor

FAN_MODES = ["low", "medium", "high"]


def _build_mpc(learning: ThermalLearning) -> MPCController:
    """Build a minimal MPCController for sensor tests."""
    return MPCController(
        fan_modes=FAN_MODES,
        learning=learning,
        deadband=0.2,
        min_interval=10,
    )


class TestThermalLearning:
    """Test auto-calibration and optimal parameter computation."""

    def test_optimal_parameters_reports_response_samples(self):
        """compute_optimal_parameters exposes the response-sample count once ready."""
        learning = ThermalLearning()

        for _ in range(250):
            learning.add_slope_sample("medium", 0.3, 0.1)
        assert learning.is_ready()

        for response_time in [10, 11, 12, 13]:
            learning.add_response_event(response_time)

        optimal = learning.compute_optimal_parameters()

        # limit_timeout is no longer computed; deadband and diagnostics remain.
        assert "limit_timeout" not in optimal
        assert optimal["response_samples"] == 4
        assert optimal["deadband"] > 0

    def test_learned_dead_time_sensor_reports_median_response(self):
        """The diagnostic dead-time sensor should expose the median response delay."""
        learning = ThermalLearning()
        for response_time in [6.0, 8.0, 8.0, 10.0]:
            learning.add_response_event(response_time)
        mpc = _build_mpc(learning)

        sensor = SmartFanLearnedDeadTimeSensor("entry", "climate.test", mpc)

        assert sensor.native_value == 8.0

    def test_effective_timeout_sensor_shows_runtime_timeout(self):
        """The effective-timeout sensor should expose dead_time × 1.5 once learning is ready."""
        learning = ThermalLearning()
        for _ in range(250):
            learning.add_slope_sample("medium", 0.3, 0.1)
        # At least MIN_RESPONSE_EVENTS_FOR_ADAPTIVE_INTERVAL events, so the dead
        # time is trusted; median stays 8.0.
        for response_time in [8.0, 8.0, 8.0, 9.0, 9.0]:
            learning.add_response_event(response_time)
        mpc = _build_mpc(learning)

        sensor = SmartFanEffectiveTimeoutSensor("entry", "climate.test", mpc)

        assert sensor.native_value == 12.0

    def test_set_mode_effective_slope_replaces_samples(self):
        """set_mode_effective_slope should replace existing samples and produce the target slope."""
        learning = ThermalLearning()

        # Add some initial samples for silent/heat
        for _ in range(15):
            learning.add_slope_sample("silent", 0.8, 0.2, hvac_mode="heat")

        assert learning.get_mode_effective_slope("silent", "heat") == pytest.approx(0.8, abs=0.01)

        # Override to a lower value
        learning.set_mode_effective_slope("silent", "heat", 0.15)

        assert learning.get_mode_effective_slope("silent", "heat") == pytest.approx(0.15, abs=0.001)
        assert learning.get_mode_sample_count("silent", "heat") == 10

    def test_set_mode_effective_slope_cool_inverts(self):
        """In cool mode, effective slope sign is inverted vs raw slope."""
        learning = ThermalLearning()

        learning.set_mode_effective_slope("high", "cool", 0.5)

        # effective_slope should be 0.5 (positive = towards target)
        assert learning.get_mode_effective_slope("high", "cool") == pytest.approx(0.5, abs=0.001)

    def test_set_mode_effective_slope_preserves_other_profiles(self):
        """Overriding one profile should not affect other profiles."""
        learning = ThermalLearning()

        for _ in range(15):
            learning.add_slope_sample("silent", 0.8, 0.2, hvac_mode="heat")
        for _ in range(15):
            learning.add_slope_sample("med", 0.5, 0.2, hvac_mode="heat")

        learning.set_mode_effective_slope("silent", "heat", 0.15)

        assert learning.get_mode_effective_slope("silent", "heat") == pytest.approx(0.15, abs=0.001)
        assert learning.get_mode_effective_slope("med", "heat") == pytest.approx(0.5, abs=0.01)

    def test_median_resists_outliers(self):
        """Median should resist a single extreme outlier sample."""
        learning = ThermalLearning()

        # 12 normal samples at ~0.15, plus 3 outlier at 1.29 (inertia contamination)
        for _ in range(12):
            learning.add_slope_sample("silent", 0.15, 0.2, hvac_mode="heat")
        for _ in range(3):
            learning.add_slope_sample("silent", 1.29, 0.2, hvac_mode="heat")

        slope = learning.get_mode_effective_slope("silent", "heat")
        # Median of [0.15]*12 + [1.29]*3 = 0.15 (most values are 0.15)
        assert slope is not None
        assert slope == pytest.approx(0.15, abs=0.01)

    def test_gap_model_learns_positive_gain(self):
        """A profile whose slope scales with the comfort error yields a+b·error."""
        learning = ThermalLearning()
        # Perfectly linear: effective_slope = 0.4 + 0.8 * error
        for err in [0.5, 1.0, 1.5, 2.0] * 3:
            learning.add_slope_sample("superhigh", 0.4 + 0.8 * err, err, hvac_mode="heat")

        a, b = learning.get_mode_slope_model("superhigh", "heat")
        assert a == pytest.approx(0.4, abs=0.01)
        assert b == pytest.approx(0.8, abs=0.01)
        assert learning.get_mode_slope_gain("superhigh", "heat") == pytest.approx(0.8, abs=0.01)

    def test_effective_slope_reports_working_value_at_reference(self):
        """get_mode_effective_slope returns the model at REFERENCE_SLOPE_ERROR, not the median."""
        learning = ThermalLearning()
        for err in [0.5, 1.0, 1.5, 2.0] * 3:
            learning.add_slope_sample("superhigh", 0.4 + 0.8 * err, err, hvac_mode="heat")

        # Working slope at error=1.0 -> 0.4 + 0.8 = 1.2 (the median of the samples is ~1.4)
        assert learning.get_mode_effective_slope("superhigh", "heat") == pytest.approx(1.2, abs=0.02)

    def test_get_mode_effective_slope_at_scales_with_error(self):
        """The modelled slope scales with the gap and floors the error at 0."""
        learning = ThermalLearning()
        for err in [0.5, 1.0, 1.5, 2.0] * 3:
            learning.add_slope_sample("superhigh", 0.4 + 0.8 * err, err, hvac_mode="heat")

        assert learning.get_mode_effective_slope_at("superhigh", "heat", 2.0) == pytest.approx(2.0, abs=0.02)
        assert learning.get_mode_effective_slope_at("superhigh", "heat", 0.0) == pytest.approx(0.4, abs=0.02)
        # Negative error is floored at 0 -> intercept only
        assert learning.get_mode_effective_slope_at("superhigh", "heat", -3.0) == pytest.approx(0.4, abs=0.02)

    def test_gap_model_cool_inversion(self):
        """In cool mode the raw slope is inverted before fitting the gap model."""
        learning = ThermalLearning()
        for err in [0.5, 1.0, 1.5, 2.0] * 3:
            # raw cooling slope is negative; effective = 0.4 + 0.8*err
            learning.add_slope_sample("superhigh", -(0.4 + 0.8 * err), err, hvac_mode="cool")

        a, b = learning.get_mode_slope_model("superhigh", "cool")
        assert a == pytest.approx(0.4, abs=0.02)
        assert b == pytest.approx(0.8, abs=0.02)

    def test_constant_error_yields_zero_gain(self):
        """When all samples share the same error, the model is the constant median."""
        learning = ThermalLearning()
        for _ in range(12):
            learning.add_slope_sample("high", 0.9, 0.3, hvac_mode="heat")

        a, b = learning.get_mode_slope_model("high", "heat")
        assert b == 0.0
        assert a == pytest.approx(0.9, abs=0.001)
        assert learning.get_mode_effective_slope("high", "heat") == pytest.approx(0.9, abs=0.001)

    def test_gain_clamped_non_negative(self):
        """A spurious negative correlation (slope falls as gap grows) is clamped to 0."""
        learning = ThermalLearning()
        for err in [0.5, 1.0, 1.5, 2.0] * 3:
            learning.add_slope_sample("high", 2.0 - 0.5 * err, err, hvac_mode="heat")

        a, b = learning.get_mode_slope_model("high", "heat")
        assert b == 0.0  # never model cooling/heating as weaker further from target

    def test_error_persisted_and_restored(self):
        """Sample errors survive a to_dict/from_dict round-trip so the gap model is stable."""
        learning = ThermalLearning()
        for err in [0.5, 1.0, 1.5, 2.0] * 3:
            learning.add_slope_sample("superhigh", 0.4 + 0.8 * err, err, hvac_mode="heat")

        restored = ThermalLearning.from_dict(learning.to_dict())
        a, b = restored.get_mode_slope_model("superhigh", "heat")
        assert a == pytest.approx(0.4, abs=0.02)
        assert b == pytest.approx(0.8, abs=0.02)

    def test_r_squared_perfect_fit(self):
        """A perfectly linear profile yields R² ≈ 1."""
        learning = ThermalLearning()
        for err in [0.5, 1.0, 1.5, 2.0] * 3:
            learning.add_slope_sample("superhigh", 0.4 + 0.8 * err, err, hvac_mode="heat")

        assert learning.get_mode_slope_r2("superhigh", "heat") == pytest.approx(1.0, abs=0.001)

    def test_r_squared_and_time_constant_none_for_constant_profile(self):
        """A constant (no-gain) profile has no regression R² and no time constant."""
        learning = ThermalLearning()
        for _ in range(12):
            learning.add_slope_sample("high", 0.9, 0.3, hvac_mode="heat")

        assert learning.get_mode_slope_r2("high", "heat") is None
        assert learning.get_mode_time_constant("high", "heat") is None

    def test_thermal_time_constant_is_inverse_gain(self):
        """The thermal time constant equals 1/gain (hours)."""
        learning = ThermalLearning()
        for err in [0.5, 1.0, 1.5, 2.0] * 3:
            learning.add_slope_sample("superhigh", 0.4 + 0.8 * err, err, hvac_mode="heat")

        # gain b = 0.8 -> tau = 1.25 h
        assert learning.get_mode_time_constant("superhigh", "heat") == pytest.approx(1.25, abs=0.02)

    def test_profile_summary_exposes_r2_and_time_constant(self):
        """get_mode_profiles surfaces r_squared and thermal_time_constant_h."""
        learning = ThermalLearning()
        for err in [0.5, 1.0, 1.5, 2.0] * 3:
            learning.add_slope_sample("superhigh", 0.4 + 0.8 * err, err, hvac_mode="heat")

        prof = learning.get_mode_profiles("heat", ["superhigh"])["superhigh"]
        assert prof["r_squared"] == pytest.approx(1.0, abs=0.001)
        assert prof["thermal_time_constant_h"] == pytest.approx(1.25, abs=0.02)
        assert prof["slope_gain"] == pytest.approx(0.8, abs=0.02)

    def test_get_dead_time_decoupled_by_hvac_mode(self):
        """Test that get_dead_time handles separate heating and cooling events correctly."""
        learning = ThermalLearning()
        learning.add_response_event(12.0, "heat")
        learning.add_response_event(14.0, "heat")
        learning.add_response_event(16.0, "heat")

        learning.add_response_event(6.0, "cool")
        learning.add_response_event(8.0, "cool")
        learning.add_response_event(10.0, "cool")

        # Specific dead times
        assert learning.get_dead_time("heat") == 14.0
        assert learning.get_dead_time("cool") == 8.0
        # Unknown fallback behavior (uses joint / all response times)
        assert learning.get_dead_time("unknown") == 11.0


# --- Audit 2026-09: learning-data integrity --------------------------------
def test_near_zero_slopes_are_learned_as_a_holding_profile() -> None:
    """A speed that holds the room shows |slope| ~ 0 and must still build a profile.

    The old 0.15 degC/h stagnation cut rejected 8 of these 10 readings, which is
    how an intermediate speed that only ever runs near the setpoint could never
    reach MIN_MODE_PROFILE_SAMPLES (22 of high's 27 distinct readings on the
    production trace).
    """
    learning = ThermalLearning()
    for slope in [-0.05, -0.1, -0.12, 0.05, -0.08, -0.14, 0.02, -0.16, -0.2, -0.11]:
        learning.add_slope_sample("medium", slope, 0.1, hvac_mode="cool")

    assert learning.get_mode_sample_count("medium", "cool") == 10
    assert learning.get_mode_effective_slope("medium", "cool") == pytest.approx(0.105, abs=0.01)


def test_seeded_profile_moves_with_each_measurement() -> None:
    """Measurements pull a seeded value visibly, instead of hiding behind its median.

    Ten identical synthetic samples are the median of any mixed set of fewer
    than twenty, so the displayed value used to sit at the seeded number until
    the measurements outnumbered them -- on a speed collecting three a week,
    indefinitely.
    """
    learning = ThermalLearning()
    learning.set_mode_effective_slope("high", "cool", 0.6)
    for _ in range(3):
        learning.add_slope_sample("high", -0.1, 0.3, hvac_mode="cool")  # measured: 0.1 towards target

    blended = learning.get_mode_effective_slope("high", "cool")
    assert blended == pytest.approx(0.6 + (3 / 13) * (0.1 - 0.6), abs=0.001)
    assert learning.get_mode_real_sample_count("high", "cool") == 3
    assert learning.has_measured_profile("high", "cool") is False

    for _ in range(7):
        learning.add_slope_sample("high", -0.1, 0.3, hvac_mode="cool")

    # Ten measurements: the profile rests on them alone, the seed is gone.
    assert learning.has_measured_profile("high", "cool") is True
    assert learning.get_mode_effective_slope("high", "cool") == pytest.approx(0.1, abs=0.001)


def test_rare_profile_keeps_measurements_older_than_the_window() -> None:
    """A speed measured a few times a week accumulates across weeks.

    Expiring by date alone kept only MIN_MODE_PROFILE_SAMPLES of them, so the
    week-old samples a rare speed needed to ever reach the gate were the first
    to go.
    """
    import time

    now = time.time()
    old = [(now - 8 * 24 * 3600 - i, "high", -0.3, "cool", 0.4) for i in range(25)]
    fresh = [(now - i, "superhigh", -1.0, "cool", 1.0) for i in range(5)]

    restored = ThermalLearning.from_dict({"slope_samples": old + fresh, "response_events": []})

    assert restored.get_mode_sample_count("high", "cool") == 25
    assert restored.get_mode_sample_count("superhigh", "cool") == 5


def test_storage_cap_keeps_rare_profile_samples() -> None:
    """The persisted tail cut must not drop the retained samples of a rare speed.

    Those are by construction the oldest rows, so a plain ``[-N:]`` removed
    exactly what the per-profile retention had kept.
    """
    import time

    from custom_components.vtherm_mpc_fan import thermal_learning as tl_module

    now = time.time()
    learning = ThermalLearning()
    rare = [(now - 10_000 - i, "high", -0.3, "cool", 0.4) for i in range(12)]
    bulk = [(now - i, "superhigh", -1.0, "cool", 1.0) for i in range(tl_module.MAX_STORED_SLOPE_SAMPLES + 50)]
    learning.slope_samples = rare + bulk

    data = learning.to_dict()

    assert sum(1 for s in data["slope_samples"] if s[1] == "high") == 12
    assert len(data["slope_samples"]) <= tl_module.MAX_STORED_SLOPE_SAMPLES + len(rare)


# --- Format 2: comfort error on the user's setpoint, regulation offset -----
def test_legacy_samples_are_kept_but_weigh_less() -> None:
    """Pre-format-2 samples (error vs the regulated setpoint) survive, at a reduced weight.

    Twelve legacy samples say 0.9, twelve current ones 0.3, at the same errors:
    a plain mean would sit at 0.6; at weight 0.25 the fit sits at 0.42.
    """
    import time

    from custom_components.vtherm_mpc_fan.thermal_learning import LEGACY_SAMPLE_WEIGHT

    now = time.time()
    legacy = [(now - 100 - i, "high", 0.9, "heat", 0.5 + 0.1 * (i % 4)) for i in range(12)]
    restored = ThermalLearning.from_dict({"slope_samples": legacy, "response_events": []})
    assert restored.get_mode_effective_slope("high", "heat") == pytest.approx(0.9)
    assert restored.get_mode_profiles("heat", ["high"])["high"]["legacy_samples"] == 12

    for i in range(12):
        restored.add_slope_sample("high", 0.3, 0.5 + 0.1 * (i % 4), hvac_mode="heat", regulation_offset=0.0)

    expected = (12 * LEGACY_SAMPLE_WEIGHT * 0.9 + 12 * 0.3) / (12 * LEGACY_SAMPLE_WEIGHT + 12)
    assert restored.get_mode_effective_slope("high", "heat") == pytest.approx(expected, abs=1e-6)


def test_store_round_trip_keeps_the_format_and_legacy_marking() -> None:
    """to_dict writes the format marker; legacy 5-tuples stay legacy, offsets are kept."""
    import time

    learning = ThermalLearning.from_dict({"slope_samples": [(time.time(), "low", 0.2, "cool", 0.1)], "response_events": []})
    learning.add_slope_sample("low", -0.2, 0.1, hvac_mode="cool", regulation_offset=-0.4)

    data = learning.to_dict()
    restored = ThermalLearning.from_dict(data)

    assert data["format"] == 2
    assert [len(s) for s in restored.slope_samples] == [5, 7]
    assert restored.slope_samples[1][5] == pytest.approx(-0.4)


def test_the_regulation_offset_enters_the_fit_when_it_varies() -> None:
    """With a well-spread offset the fit learns how much more a harder-pushed unit cools.

    Cool: slope = 0.5 + 0.3 * error + 0.8 * demand, demand = -offset (regulated
    below the user's setpoint pushes harder). The error is held constant so the
    offset is the only thing explaining the spread.
    """
    learning = ThermalLearning()
    for i in range(40):
        demand = (i % 5) * 0.2
        learning.add_slope_sample("high", -(0.5 + 0.3 * 0.4 + 0.8 * demand), 0.4, hvac_mode="cool", regulation_offset=-demand)

    profile = learning.get_mode_profiles("cool", ["high"])["high"]
    # 40 samples, demand variance 0.08 -> sum of squares 3.2, shrunk by 10 x 0.2^2.
    assert profile["offset_gain"] == pytest.approx(0.8 * 3.2 / (3.2 + 0.4), rel=0.01)
    # At the usual offset the correction is nil, an unusually hard push adds slope.
    assert learning.get_mode_offset_correction("high", "cool", 0.4, -0.4) == pytest.approx(0.0, abs=1e-6)
    assert learning.get_mode_offset_correction("high", "cool", 0.4, -0.8) > 0.1


def test_a_steady_regulation_offset_is_ignored() -> None:
    """An offset that never varies cannot be told apart from the intercept: no term."""
    learning = ThermalLearning()
    for err in [0.2, 0.4, 0.6, 0.8] * 5:
        learning.add_slope_sample("high", -(0.5 + 0.3 * err), err, hvac_mode="cool", regulation_offset=-0.6)

    assert learning.get_mode_profiles("cool", ["high"])["high"]["offset_gain"] == 0.0
    assert learning.get_mode_offset_correction("high", "cool", 0.4, -1.5) == 0.0


# --- Time-based sampling: a profile is measured by regime duration --------
def test_a_profile_is_measured_by_regime_duration_not_distinct_readings() -> None:
    """Nine 10-minute samples of one unchanged reading make 90 min: measured."""
    learning = ThermalLearning()
    for _ in range(8):
        learning.add_slope_sample("med", -0.1, 0.1, hvac_mode="cool", dwell_minutes=10.0)
    assert learning.get_mode_measured_minutes("med", "cool") == pytest.approx(80.0)
    assert learning.has_measured_profile("med", "cool") is False
    assert learning.is_profile_ready("med", "cool") is False

    learning.add_slope_sample("med", -0.1, 0.1, hvac_mode="cool", dwell_minutes=10.0)

    assert learning.has_measured_profile("med", "cool") is True
    assert learning.get_mode_effective_slope("med", "cool") == pytest.approx(0.1)


def test_a_measured_profile_also_needs_enough_samples() -> None:
    """Long dwells cannot make a profile out of a handful of samples."""
    learning = ThermalLearning()
    for _ in range(5):
        learning.add_slope_sample("med", -0.1, 0.1, hvac_mode="cool", dwell_minutes=30.0)

    assert learning.get_mode_measured_minutes("med", "cool") == pytest.approx(150.0)
    assert learning.has_measured_profile("med", "cool") is False


def test_ten_legacy_samples_keep_a_profile_measured() -> None:
    """An upgrade changes no profile's status: ten legacy samples still make it measured."""
    import time

    now = time.time()
    legacy = [(now - i, "high", -0.6, "cool", 0.3) for i in range(10)]

    restored = ThermalLearning.from_dict({"slope_samples": legacy, "response_events": []})

    assert restored.has_measured_profile("high", "cool") is True
    assert restored.get_mode_effective_slope("high", "cool") == pytest.approx(0.6)


def test_autocorrelated_samples_count_for_less() -> None:
    """Consecutive samples of a slowly drifting residual carry less than one sample each."""
    import time
    from unittest.mock import patch

    learning = ThermalLearning()
    start = time.time()
    for i in range(30):
        with patch("time.time", return_value=start + i * 600):
            # Residual drifts slowly: strongly autocorrelated.
            learning.add_slope_sample("high", -(0.5 + 0.3 * (i % 6) * 0.1 + 0.05 * (i // 10)), (i % 6) * 0.1, hvac_mode="cool")

    profile = learning.get_mode_profiles("cool", ["high"])["high"]
    assert profile["effective_samples"] < 30 / 1.5


def test_a_thin_profile_gain_is_shrunk_toward_the_pooled_gain() -> None:
    """A steep gain read from a few points of one speed is pulled toward what every speed shows."""
    learning = ThermalLearning()
    for err in [0.2, 0.4, 0.6, 0.8, 1.0, 1.2] * 10:
        learning.add_slope_sample("superhigh", 0.5 + 0.3 * err, err, hvac_mode="heat")
    for err in [0.1, 0.15, 0.2, 0.25, 0.3, 0.1, 0.15, 0.2, 0.25]:
        learning.add_slope_sample("low", 0.1 + 2.0 * err, err, hvac_mode="heat")

    _, gain = learning.get_mode_slope_model("low", "heat")
    assert 0.3 < gain < 2.0
