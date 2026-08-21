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
