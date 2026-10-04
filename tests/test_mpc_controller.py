"""Tests for the MPC diagnostics and guardrails."""

import csv
import random
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.vtherm_mpc_fan import mpc_controller as mpc_module
from custom_components.vtherm_mpc_fan.data_collection import DataCollector
from custom_components.vtherm_mpc_fan.mpc_controller import MPCController
from custom_components.vtherm_mpc_fan.number import EffectiveSlopeNumber
from custom_components.vtherm_mpc_fan.sensor import (
    SmartFanLearningResponseSensor,
    SmartFanSensor,
)
from custom_components.vtherm_mpc_fan.thermal_learning import ThermalLearning

FAN_MODES = ["low", "medium", "high"]


def _build_learning(*, fan_modes=None) -> ThermalLearning:  # pylint: disable=unused-argument
    """Build a ThermalLearning instance for test use."""
    return ThermalLearning()


def _build_mpc(learning: ThermalLearning, *, fan_modes=None, min_interval: int = 10) -> MPCController:
    """Build an MPCController with default test parameters."""
    return MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=min_interval,
        fan_modes=fan_modes or FAN_MODES,
    )


def _prime_learning_profiles(learning: ThermalLearning) -> None:
    """Feed enough slope samples for all profiles to become ready."""
    for _ in range(60):
        learning.add_slope_sample("low", 0.25, 0.8, "heat")
        learning.add_slope_sample("medium", 0.9, 0.8, "heat")
        learning.add_slope_sample("high", 1.5, 0.8, "heat")
    learning.add_response_event(8.0)
    learning.add_response_event(10.0)
    learning.add_response_event(12.0)


def _make_executor_hass() -> MagicMock:
    """Create a mock HomeAssistant with synchronous executor."""
    hass = MagicMock()

    async def run_in_executor(target, *args):
        """Run target synchronously for tests."""
        return target(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=run_in_executor)
    return hass


def test_mpc_idle_for_unsimulated_hvac_modes() -> None:
    """MPC reports idle status for unsimulated HVAC modes."""
    learning = ThermalLearning()
    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=FAN_MODES,
    )

    result = mpc.evaluate(
        current_temp=19.2,
        target_temp=20.0,
        vtherm_slope=0.4,
        hvac_mode="off",
        current_fan="medium",
        is_window_open=False,
    )

    assert result["mpc_status"] == "Idle"
    assert result["mpc_fan_mode"] == "medium"
    assert result["mpc_would_change_now"] == "no"


def test_mpc_idle_for_modes_without_a_comfort_direction() -> None:
    """Only heat/cool are regulated; every other mode pauses the MPC."""
    mpc = MPCController(
        learning=ThermalLearning(),
        deadband=0.3,
        min_interval=10,
        fan_modes=FAN_MODES,
    )

    for hvac_mode in ("dry", "fan_only", "heat_cool", "auto"):
        result = mpc.evaluate(
            current_temp=26.0,
            target_temp=24.0,
            vtherm_slope=0.0,
            hvac_mode=hvac_mode,
            current_fan="medium",
        )

        assert result["mpc_status"] == "Idle", hvac_mode
        assert result["mpc_fan_mode"] == "medium"
        assert "not regulated" in result["mpc_reason"]


def test_mpc_prefers_stronger_fan_when_profiles_support_it() -> None:
    """MPC picks a stronger fan mode when learned profiles support it."""
    learning = ThermalLearning()
    _prime_learning_profiles(learning)
    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=FAN_MODES,
    )

    result = mpc.evaluate(
        current_temp=19.0,
        target_temp=20.0,
        vtherm_slope=0.25,
        hvac_mode="heat",
        current_fan="low",
        is_window_open=False,
        minutes_since_change=20.0,
    )

    assert result["mpc_fan_mode"] == "high"
    assert result["mpc_would_change_now"] == "yes"
    assert result["mpc_known_profiles"] == 3


def test_mpc_holds_superhigh_while_still_below_target() -> None:
    """MPC holds superhigh when temperature is still below target."""
    fan_modes = ["low", "medium", "high", "superhigh"]
    learning = ThermalLearning()
    for _ in range(60):
        learning.add_slope_sample("low", 0.2, 0.4, "heat")
        learning.add_slope_sample("medium", 0.5, 0.4, "heat")
        learning.add_slope_sample("high", 0.8, 0.4, "heat")
        learning.add_slope_sample("superhigh", 1.0, 0.4, "heat")
    learning.add_response_event(30.0)

    mpc = MPCController(
        learning=learning,
        deadband=0.2,
        min_interval=10,
        fan_modes=fan_modes,
    )

    result = mpc.evaluate(
        current_temp=19.95,
        target_temp=20.0,
        vtherm_slope=0.2,
        hvac_mode="heat",
        current_fan="superhigh",
        is_window_open=False,
        minutes_since_change=40.0,
    )

    assert result["mpc_fan_mode"] == "superhigh"
    assert result["mpc_would_change_now"] == "no"
    # Inside the deadband the weaker speeds are cheaper, but not by the margin a
    # step down while under target requires.
    assert "Hysteresis holds superhigh" in result["mpc_reason"] or "Below target: holding superhigh" in result["mpc_reason"]


def test_mpc_pauses_when_window_is_open() -> None:
    """MPC pauses evaluation when a window is open."""
    learning = ThermalLearning()
    _prime_learning_profiles(learning)
    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=FAN_MODES,
    )

    result = mpc.evaluate(
        current_temp=19.3,
        target_temp=20.0,
        vtherm_slope=0.2,
        hvac_mode="heat",
        current_fan="medium",
        is_window_open=True,
        minutes_since_change=12.0,
    )

    assert result["mpc_status"] == "Disturbed"
    assert result["mpc_fan_mode"] == "medium"
    assert result["mpc_would_change_now"] == "no"
    assert "paused" in result["mpc_reason"]


def test_mpc_still_reports_known_profiles_while_paused() -> None:
    """A pause must not read as the model having lost its learning.

    known_profiles was only counted inside the simulation loop, which a pause
    returns before reaching -- so the sensor dropped to 0 every time the unit
    stopped, while the learned profiles were still there.
    """
    learning = ThermalLearning()
    _prime_learning_profiles(learning)
    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=FAN_MODES,
    )

    result = mpc.evaluate(
        current_temp=19.3,
        target_temp=20.0,
        vtherm_slope=0.2,
        hvac_mode="heat",
        current_fan="medium",
        is_hvac_idle=True,
        minutes_since_change=12.0,
    )

    assert result["mpc_status"] == "Disturbed"
    assert result["mpc_known_profiles"] > 0


def test_adaptive_interval_engages_on_response_events_not_global_readiness() -> None:
    """The production case: profiles learned, dead time known, is_ready() false.

    On a 24 h trace every per-mode profile was learned and the dead time sat at
    17.5-28.5 min, but is_ready() (which counts slope samples) never flipped, so
    the change interval stayed pinned at its 10-minute floor and the controller
    re-decided about twice per dead time -- acting before it could measure.
    """
    learning = ThermalLearning()
    for _ in range(20):
        learning.add_response_event(24.0, "cool")
    mpc = _build_mpc(learning, min_interval=10)

    assert learning.is_ready() is False  # no slope samples: the old gate
    assert mpc._effective_min_interval(24.0) == 24.0  # noqa: SLF001
    assert mpc.get_effective_timeout("cool") == 24.0 * 1.5


def test_mpc_resolves_the_dead_time_for_the_active_hvac_mode() -> None:
    """A heat pump's heating lag and cooling lag are different numbers.

    evaluate() used to call get_dead_time() with no argument, which pools every
    mode's response events into one median. That dead time drives the change
    gate, the phase split and every candidate's change_delay, so a cooling cycle
    was optimised against a heat-contaminated lag.
    """
    learning = ThermalLearning()
    for _ in range(20):
        learning.add_response_event(6.0, "heat")
    for _ in range(20):
        learning.add_response_event(24.0, "cool")
    mpc = _build_mpc(learning)

    assert learning.get_dead_time() == 15.0  # pooled: neither mode's real lag

    cool = mpc.evaluate(
        current_temp=25.0,
        target_temp=24.0,
        vtherm_slope=-0.5,
        hvac_mode="cool",
        current_fan="medium",
        minutes_since_change=30.0,
    )
    heat = mpc.evaluate(
        current_temp=19.0,
        target_temp=20.0,
        vtherm_slope=0.5,
        hvac_mode="heat",
        current_fan="medium",
        minutes_since_change=30.0,
    )

    assert cool["mpc_dead_time"] == 24.0
    assert heat["mpc_dead_time"] == 6.0


def test_mpc_still_reports_known_profiles_on_a_setpoint_drop() -> None:
    """The setpoint-drop shortcut skips the simulation loop too.

    Same defect as the pause paths: known_profiles is only counted inside the
    loop, so any return path that shortcuts it reported 0 learned profiles in
    the CSV and in VTherm's attributes while the model was fully trained.
    """
    learning = ThermalLearning()
    _prime_learning_profiles(learning)
    mpc = _build_mpc(learning)

    # Night setpoint: the user drops the target away below the room, so the
    # comfort error goes strongly negative (< THRESHOLD_TARGET_DROP) and the
    # MPC shortcuts.
    _evaluate_heat(mpc, current_temp=22.0, target_temp=22.0, current_fan="medium")
    result = mpc.evaluate(
        current_temp=22.0,
        target_temp=20.0,
        vtherm_slope=0.2,
        hvac_mode="heat",
        current_fan="medium",
        minutes_since_change=30.0,
    )

    assert result["mpc_status"] == "Setpoint drop"
    assert result["mpc_known_profiles"] > 0


def test_mpc_sensor_can_clear_to_none() -> None:
    """MPC sensor value can be cleared to None."""
    sensor = SmartFanSensor(
        "entry-1",
        "climate.living_room",
        "MPC Cost",
        "mpc_cost",
        "mpc_cost",
        None,
        None,
        "mdi:calculator",
    )

    sensor.update_from_mpc({"mpc_cost": 3.2})
    assert sensor.native_value == 3.2

    sensor.update_from_mpc({"mpc_cost": None})
    assert sensor.native_value is None


def test_mpc_logs_the_per_mode_profiles_used_every_cycle(caplog) -> None:
    """The per-mode fan_mode_order and effective_slopes_used stay inspectable.

    The "MPC Heat/Cool Profiles" sensors that used to expose this were
    dropped as standalone entities (no history-graph value, and the number.*
    entities they linked to are already documented directly). This asserts
    the DEBUG line in evaluate() that replaces them as the diagnostic path.
    """
    learning = ThermalLearning()
    mpc = _build_mpc(learning)
    for _ in range(15):
        learning.add_slope_sample("medium", 0.5, 0.3, "heat")

    with caplog.at_level("DEBUG", logger="custom_components.vtherm_mpc_fan.mpc_controller"):
        mpc.evaluate(
            current_temp=19.0,
            target_temp=20.0,
            vtherm_slope=0.2,
            hvac_mode="heat",
            current_fan="medium",
            minutes_since_change=30.0,
        )

    profile_logs = [r.message for r in caplog.records if "MPC heat profiles" in r.message]
    assert len(profile_logs) == 1
    assert "fan_mode_order=['low', 'medium', 'high']" in profile_logs[0]
    assert "'medium': 0.5" in profile_logs[0]


def test_profile_effective_slope_number_exposes_historizable_state() -> None:
    """Profile effective slope number is historizable with correct attributes."""
    learning = ThermalLearning()
    mpc = _build_mpc(learning)
    for _ in range(12):
        learning.add_slope_sample("high", 0.9, 0.3, "heat")

    number = EffectiveSlopeNumber(
        "entry-1",
        "climate.living_room",
        mpc,
        "heat",
        "high",
    )

    assert number.entity_id == "number.vtherm_mpc_fan_living_room_heat_high_effective_slope"
    assert number.native_value == 0.9
    assert number.extra_state_attributes["samples"] == 12
    assert number.extra_state_attributes["ready"] is True
    assert number.extra_state_attributes["spread"] == 0.0
    assert number.extra_state_attributes["quality"] == "good"
    # A learned (ready) profile has a real measurement; no need for a guess.
    assert number.extra_state_attributes["value_source"] == "learned"


@pytest.mark.asyncio
async def test_setting_the_number_persists_a_ready_synthetic_profile() -> None:
    """Editing the number replaces the profile's samples and calls through to save."""
    learning = ThermalLearning()
    mpc = _build_mpc(learning)
    on_change = AsyncMock()

    # hass is deliberately left unset (None): this entity was never added to a
    # platform, and async_set_native_value must not crash trying to publish
    # state to Home Assistant that has never actually adopted it.
    number = EffectiveSlopeNumber("entry-1", "climate.living_room", mpc, "cool", "low", on_change=on_change)

    await number.async_set_native_value(0.42)

    on_change.assert_awaited_once()
    assert number.native_value == 0.42
    assert number.extra_state_attributes["ready"] is True
    # Ready, but on the user's word rather than on measurements: the MPC's
    # exploration guards still treat this speed as unmeasured.
    assert number.extra_state_attributes["value_source"] == "seeded"
    assert number.extra_state_attributes["real_samples"] == 0


def test_get_live_mode_slope_is_none_before_any_cycle_ran() -> None:
    """No evaluate() call yet means no live estimate exists to report."""
    mpc = _build_mpc(ThermalLearning())
    assert mpc.get_live_mode_slope("medium", "heat") is None


def test_get_live_mode_slope_reflects_the_last_evaluated_cycle() -> None:
    """Every candidate's live slope estimate is captured on each evaluate() call."""
    learning = ThermalLearning()
    mpc = _build_mpc(learning)

    mpc.evaluate(
        current_temp=19.0,
        target_temp=20.0,
        vtherm_slope=0.3,
        hvac_mode="heat",
        current_fan="medium",
        minutes_since_change=20.0,
    )

    live = mpc.get_live_mode_slope("medium", "heat")
    assert live is not None
    slope, is_learned = live
    assert is_learned is False  # nothing learned yet -> rank-scaled fallback
    assert slope > 0


def test_get_live_mode_slope_is_none_for_a_different_hvac_mode() -> None:
    """Only one hvac_mode is live per cycle; the other has nothing to show."""
    mpc = _build_mpc(ThermalLearning())
    mpc.evaluate(
        current_temp=19.0,
        target_temp=20.0,
        vtherm_slope=0.3,
        hvac_mode="heat",
        current_fan="medium",
        minutes_since_change=20.0,
    )

    assert mpc.get_live_mode_slope("medium", "cool") is None


def test_number_surfaces_the_live_fallback_before_learning() -> None:
    """An unlearned profile's number shows the live guess actually driving decisions.

    There is no fixed default for an unlearned profile, so the field shows
    whatever rank-scaled estimate the MPC is substituting for this candidate
    right now -- giving the user something real to look at (and start
    correcting from) instead of an empty field. ``value_source`` marks it as a
    guess rather than a measurement, since the state alone can't tell the two
    apart.
    """
    learning = ThermalLearning()
    mpc = _build_mpc(learning)
    mpc.evaluate(
        current_temp=19.0,
        target_temp=20.0,
        vtherm_slope=0.3,
        hvac_mode="heat",
        current_fan="medium",
        minutes_since_change=20.0,
    )

    number = EffectiveSlopeNumber("entry-1", "climate.living_room", mpc, "heat", "high")

    assert number.native_value is not None  # a live guess, not a blank field
    assert number.extra_state_attributes["ready"] is False
    assert number.extra_state_attributes["value_source"] == "live_fallback_estimate"


def test_get_profile_spread_returns_none_when_insufficient_samples() -> None:
    """get_profile_spread returns None when the profile has fewer than MIN_MODE_PROFILE_SAMPLES."""
    learning = ThermalLearning()
    learning.add_slope_sample("high", 0.9, 0.3, "heat")
    assert learning.get_profile_spread("high", "heat") is None


def test_get_profile_spread_returns_zero_for_identical_samples() -> None:
    """get_profile_spread returns 0.0 for perfectly consistent slope samples."""
    learning = ThermalLearning()
    for _ in range(12):
        learning.add_slope_sample("high", 0.9, 0.3, "heat")
    assert learning.get_profile_spread("high", "heat") == 0.0


def test_get_profile_spread_reflects_variability() -> None:
    """get_profile_spread increases with sample variability."""
    learning_tight = ThermalLearning()
    learning_noisy = ThermalLearning()
    slopes_tight = [0.88, 0.89, 0.90, 0.91, 0.92, 0.89, 0.90, 0.91, 0.88, 0.90]
    slopes_noisy = [0.30, 0.60, 0.90, 1.20, 0.45, 0.80, 1.10, 0.50, 0.70, 1.00]
    for s in slopes_tight:
        learning_tight.add_slope_sample("high", s, 0.3, "heat")
    for s in slopes_noisy:
        learning_noisy.add_slope_sample("high", s, 0.3, "heat")
    spread_tight = learning_tight.get_profile_spread("high", "heat")
    spread_noisy = learning_noisy.get_profile_spread("high", "heat")
    assert spread_tight is not None
    assert spread_noisy is not None
    assert spread_noisy > spread_tight


def test_confidence_penalised_by_high_spread() -> None:
    """MPC confidence is lower when a known profile has high spread."""
    learning_tight = ThermalLearning()
    learning_noisy = ThermalLearning()
    slopes_tight = [0.88, 0.89, 0.90, 0.91, 0.92, 0.89, 0.90, 0.91, 0.88, 0.90]
    slopes_noisy = [0.30, 0.60, 0.90, 1.20, 0.45, 0.80, 1.10, 0.50, 0.70, 1.00]
    for i in range(100):
        learning_tight.add_slope_sample("high", slopes_tight[i % len(slopes_tight)], 0.3, "heat")
        learning_noisy.add_slope_sample("high", slopes_noisy[i % len(slopes_noisy)], 0.3, "heat")

    mpc_tight = _build_mpc(learning_tight)
    mpc_noisy = _build_mpc(learning_noisy)

    common_kwargs = dict(
        current_temp=19.5,
        target_temp=20.0,
        vtherm_slope=0.9,
        hvac_mode="heat",
        current_fan="high",
        minutes_since_change=30.0,
    )
    result_tight = mpc_tight.evaluate(**common_kwargs)
    result_noisy = mpc_noisy.evaluate(**common_kwargs)
    assert result_tight["mpc_confidence"] > result_noisy["mpc_confidence"]


def test_confidence_high_with_full_coverage_before_global_ready() -> None:
    """Full per-mode profile coverage yields a Ready-level confidence even when
    the global sample threshold (is_ready) has not been reached.

    Regression guard for the previous behaviour where an HVAC mode used only
    part of the year stayed stuck at 'Low confidence' despite every fan-mode
    profile being fully learned.
    """
    learning = ThermalLearning()
    # 12 tight samples per mode -> every profile ready, but well under the
    # global MIN_SAMPLES_LEARNING threshold, so is_ready() stays False.
    for _ in range(12):
        learning.add_slope_sample("low", 0.25, 0.8, "heat")
        learning.add_slope_sample("medium", 0.9, 0.8, "heat")
        learning.add_slope_sample("high", 1.5, 0.8, "heat")
    assert learning.is_ready() is False

    mpc = _build_mpc(learning)
    result = mpc.evaluate(
        current_temp=19.5,
        target_temp=20.0,
        vtherm_slope=0.9,
        hvac_mode="heat",
        current_fan="high",
        minutes_since_change=30.0,
    )
    assert result["mpc_known_profiles"] == 3
    assert result["mpc_confidence"] >= 50.0
    assert result["mpc_status"] == "Ready"


@pytest.mark.asyncio
async def test_data_collector_records_mpc_columns(tmp_path: Path) -> None:
    """Data collector CSV includes MPC-specific columns."""
    hass = _make_executor_hass()
    collector = DataCollector(hass, str(tmp_path), "entry123456")

    await collector.async_initialize()
    await collector.async_record(
        hvac_mode="heat",
        current_temp=19.0,
        target_temp=20.0,
        vtherm_slope=0.25,
        is_window_open=False,
        decision={
            "temperature_error": 1.0,
            "projected_temperature": 19.2,
            "projected_temperature_error": 0.8,
            "minutes_since_last_change": 20.0,
            "current_fan": "low",
            "fan_mode": "high",
            "reason": "Emergency: High error (1.0C)",
        },
        phase="ESTABLISHED",
        effective_slope=0.25,
        effective_timeout=15.0,
        force=True,
        learning_ready=True,
        dead_time=10.0,
        mpc_decision={
            "mpc_status": "Ready",
            "mpc_fan_mode": "high",
            "mpc_would_change_now": "yes",
            "mpc_cost": 4.321,
            "mpc_confidence": 75.0,
            "mpc_predicted_temperature_10m": 19.3,
            "mpc_predicted_temperature_30m": 19.8,
            "mpc_known_profiles": 3,
            "mpc_disturbance_bias": -0.25,
        },
    )

    with open(collector.path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))

    header = rows[0]
    row = rows[1]

    assert "mpc_would_change" in header
    assert "mpc_known_profiles" in header
    assert "mpc_disturbance" in header
    assert row[header.index("mpc_would_change")] == "yes"
    assert row[header.index("mpc_known_profiles")] == "3"
    assert row[header.index("mpc_disturbance")] == "-0.25"


def _evaluate_heat(mpc: MPCController, *, current_temp: float, target_temp: float, current_fan: str, user_target_temp: float | None = None) -> dict:
    """Run one heating cycle well past the min interval (establishes the previous setpoint)."""
    return mpc.evaluate(
        current_temp=current_temp,
        target_temp=target_temp,
        vtherm_slope=0.0,
        hvac_mode="heat",
        current_fan=current_fan,
        minutes_since_change=60.0,
        user_target_temp=user_target_temp,
    )


def test_mpc_setpoint_drop_forces_lowest_mode() -> None:
    """When the user drops the target significantly, MPC should go to the lowest fan mode."""
    learning = ThermalLearning()
    _prime_learning_profiles(learning)
    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=FAN_MODES,
    )
    _evaluate_heat(mpc, current_temp=20.4, target_temp=20.5, current_fan="high")

    result = mpc.evaluate(
        current_temp=20.4,
        target_temp=17.5,
        vtherm_slope=0.0,
        hvac_mode="heat",
        current_fan="high",
        is_window_open=False,
        minutes_since_change=5.0,
    )

    assert result["mpc_status"] == "Setpoint drop"
    assert result["mpc_fan_mode"] == "low"
    assert "Setpoint drop" in result["mpc_reason"]


def test_mpc_setpoint_drop_reports_would_change() -> None:
    """Setpoint drop should report would_change correctly."""
    learning = ThermalLearning()
    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=FAN_MODES,
    )
    _evaluate_heat(mpc, current_temp=20.0, target_temp=20.0, current_fan="medium")

    result = mpc.evaluate(
        current_temp=20.0,
        target_temp=17.5,
        vtherm_slope=-0.2,
        hvac_mode="heat",
        current_fan="medium",
        is_window_open=False,
        minutes_since_change=15.0,
    )

    assert result["mpc_fan_mode"] == "low"
    assert result["mpc_would_change_now"] == "yes"


def test_mpc_no_setpoint_drop_when_error_above_threshold() -> None:
    """Normal over-target should NOT trigger setpoint drop."""
    learning = ThermalLearning()
    _prime_learning_profiles(learning)
    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=FAN_MODES,
    )

    result = mpc.evaluate(
        current_temp=20.3,
        target_temp=20.0,
        vtherm_slope=0.0,
        hvac_mode="heat",
        current_fan="high",
        is_window_open=False,
        minutes_since_change=15.0,
    )

    assert result["mpc_status"] != "Setpoint drop"


def test_a_room_past_the_setpoint_without_a_setpoint_change_is_an_overshoot() -> None:
    """No setpoint moved: the lowest speed the guards allow, status Overshoot.

    The old rule fired "Setpoint drop" on any comfort error below -1 degC. On the
    production trace that was 27 % of the active time, 42 episodes of ~2 h, all
    triggered by VTherm's regulated setpoint drifting -- no user action.
    """
    learning = ThermalLearning()
    _prime_learning_profiles(learning)
    mpc = _build_mpc(learning)
    _evaluate_heat(mpc, current_temp=21.2, target_temp=20.0, current_fan="high")

    result = _evaluate_heat(mpc, current_temp=21.3, target_temp=20.0, current_fan="high")

    assert result["mpc_status"] == "Overshoot"
    assert result["mpc_fan_mode"] == "low"
    assert result["mpc_would_change_now"] == "yes"
    assert "Overshoot" in result["mpc_reason"]


def test_an_overshoot_respects_the_step_down_guard() -> None:
    """The lowest *allowed* speed: a multi-rank plunge to a speed that loses ground stays blocked."""
    learning = ThermalLearning()
    learning.set_mode_effective_slope("low", "heat", -0.3)
    learning.set_mode_effective_slope("medium", "heat", 0.6)
    learning.set_mode_effective_slope("high", "heat", 1.2)
    mpc = _build_mpc(learning)

    result = _evaluate_heat(mpc, current_temp=21.5, target_temp=20.0, current_fan="high")

    assert result["mpc_status"] == "Overshoot"
    assert result["mpc_fan_mode"] == "medium"


def test_regulation_drift_is_not_a_setpoint_drop() -> None:
    """VTherm moving its regulated setpoint is not the user moving theirs.

    The user's setpoint stays 24 degC (cool) while the regulated one drifts 1.5
    degC lower: the MPC regulates on the user's, so nothing happens.
    """
    learning = ThermalLearning()
    _prime_cool_profiles(learning)
    mpc = _build_mpc(learning)
    for regulated in (23.8, 23.0, 22.3):
        result = mpc.evaluate(
            current_temp=24.0,
            target_temp=regulated,
            user_target_temp=24.0,
            vtherm_slope=0.0,
            hvac_mode="cool",
            current_fan="medium",
            minutes_since_change=60.0,
        )

    assert result["mpc_status"] != "Setpoint drop"
    assert result["mpc_comfort_error"] == pytest.approx(0.0)
    assert result["mpc_regulation_offset"] == pytest.approx(-1.7)


def test_a_user_setpoint_raise_in_cool_is_a_setpoint_drop() -> None:
    """In cool, asking for less cooling means a *higher* setpoint."""
    learning = ThermalLearning()
    _prime_cool_profiles(learning)
    mpc = _build_mpc(learning)
    mpc.evaluate(current_temp=22.2, target_temp=22.0, user_target_temp=22.0, vtherm_slope=0.0, hvac_mode="cool", current_fan="high", minutes_since_change=60.0)

    result = mpc.evaluate(current_temp=22.2, target_temp=24.0, user_target_temp=24.0, vtherm_slope=0.0, hvac_mode="cool", current_fan="high", minutes_since_change=60.0)

    assert result["mpc_status"] == "Setpoint drop"
    assert result["mpc_fan_mode"] == "low"


def test_comfort_is_judged_against_the_user_setpoint() -> None:
    """The cost and status use the user's setpoint; the regulated one only sets the offset."""
    learning = ThermalLearning()
    _prime_cool_profiles(learning)
    mpc = _build_mpc(learning)

    result = mpc.evaluate(current_temp=24.1, target_temp=23.4, user_target_temp=24.0, vtherm_slope=0.0, hvac_mode="cool", current_fan="medium", minutes_since_change=60.0)

    assert result["mpc_comfort_error"] == pytest.approx(0.1)
    assert result["mpc_regulation_offset"] == pytest.approx(-0.6)
    # Without the user setpoint the regulated one is used for both.
    fallback = _build_mpc(learning).evaluate(current_temp=24.1, target_temp=23.4, vtherm_slope=0.0, hvac_mode="cool", current_fan="medium", minutes_since_change=60.0)
    assert fallback["mpc_comfort_error"] == pytest.approx(0.7)
    assert fallback["mpc_regulation_offset"] is None


def _prime_cool_profiles(learning: ThermalLearning) -> None:
    """Seed a low/medium/high cooling ladder."""
    learning.set_mode_effective_slope("low", "cool", 0.2)
    learning.set_mode_effective_slope("medium", "cool", 0.6)
    learning.set_mode_effective_slope("high", "cool", 1.2)


def test_mpc_pauses_during_defrost() -> None:
    """MPC should pause when defrost is active, like window-open."""
    learning = ThermalLearning()
    _prime_learning_profiles(learning)
    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=FAN_MODES,
    )

    result = mpc.evaluate(
        current_temp=19.3,
        target_temp=20.0,
        vtherm_slope=0.2,
        hvac_mode="heat",
        current_fan="high",
        is_window_open=False,
        is_defrost_active=True,
        minutes_since_change=12.0,
    )

    assert result["mpc_status"] == "Disturbed"
    assert result["mpc_fan_mode"] == "high"
    assert result["mpc_would_change_now"] == "no"
    assert "Defrost" in result["mpc_reason"]


def test_mpc_disturbance_bias_decays_during_defrost() -> None:
    """Disturbance bias should decay, not update, during defrost."""
    learning = ThermalLearning()
    _prime_learning_profiles(learning)
    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=FAN_MODES,
    )

    # Prime the disturbance bias with a normal cycle
    mpc.evaluate(
        current_temp=19.5,
        target_temp=20.0,
        vtherm_slope=0.5,
        hvac_mode="heat",
        current_fan="medium",
        is_window_open=False,
        minutes_since_change=20.0,
    )
    bias_before = mpc.disturbance_bias

    # Defrost cycle with sharp slope drop — should NOT poison the bias
    mpc.evaluate(
        current_temp=19.5,
        target_temp=20.0,
        vtherm_slope=-1.0,
        hvac_mode="heat",
        current_fan="high",
        is_window_open=False,
        is_defrost_active=True,
        minutes_since_change=25.0,
    )
    bias_after = mpc.disturbance_bias

    # Bias should have decayed, not grown from the -1.0 slope residual
    assert abs(bias_after) <= abs(bias_before)


def test_monotone_constraint_enforces_ordering() -> None:
    """An inverted weak profile is clamped down, leaving the stronger ones untouched."""
    learning = ThermalLearning()

    # Create inverted profiles: silent reads stronger than low (the real-world bug)
    learning.set_mode_effective_slope("silent", "heat", 0.53)
    learning.set_mode_effective_slope("low", "heat", 0.0)
    learning.set_mode_effective_slope("med", "heat", 0.45)
    learning.set_mode_effective_slope("high", "heat", 0.96)
    learning.set_mode_effective_slope("superhigh", "heat", 1.35)

    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=["silent", "low", "med", "high", "superhigh"],
    )

    monotone = mpc.build_monotone_slopes(["silent", "low", "med", "high", "superhigh"], "heat")
    assert isinstance(monotone, dict)

    # Equal sample counts, so the violation resolves downward: silent's estimate is
    # discarded and re-synthesised strictly below low rather than dragging low up.
    assert monotone["silent"] < monotone["low"]
    # low sits at exactly 0.0, where multiplicative spacing would collapse, so the
    # absolute fallback separation applies.
    assert monotone["silent"] == pytest.approx(-0.05, abs=0.001)
    # Profiles that were already ordered keep their learned values exactly.
    assert monotone["low"] == pytest.approx(0.0, abs=0.001)
    assert monotone["med"] == pytest.approx(0.45, abs=0.001)
    assert monotone["high"] == pytest.approx(0.96, abs=0.001)
    assert monotone["superhigh"] == pytest.approx(1.35, abs=0.001)


def test_monotone_constraint_returns_partial_dict_for_partial_profiles() -> None:
    """On a fresh install with incomplete profiles, monotone should return an empty dict."""
    learning = ThermalLearning()
    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=FAN_MODES,
    )
    # No profiles learned yet
    result = mpc.build_monotone_slopes(FAN_MODES, "heat")
    assert isinstance(result, dict)
    assert len(result) == 0


def test_monotone_constraint_partial_profiles_enforces_known_pairs() -> None:
    """With some profiles missing, monotone should still enforce ordering among known ones."""
    learning = ThermalLearning()
    fan_modes = ["silent", "low", "med", "high", "superhigh"]

    # Real-world snapshot inversion seen in the collected data:
    # high=1.59 while superhigh=1.075.
    learning.set_mode_effective_slope("high", "heat", 1.59)
    learning.set_mode_effective_slope("superhigh", "heat", 1.075)

    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=fan_modes,
    )

    result = mpc.build_monotone_slopes(fan_modes, "heat")
    assert isinstance(result, dict)
    # Only known profiles are in the dict
    assert "silent" not in result
    assert "low" not in result
    assert "med" not in result
    assert "high" in result
    assert "superhigh" in result
    # Equal sample counts: the weaker mode is the one rewritten, so superhigh keeps
    # its own learned value instead of being inflated to high's, and high lands one
    # ladder step below it rather than tied with it.
    assert result["superhigh"] == pytest.approx(1.075, abs=0.001)
    assert result["high"] == pytest.approx(1.075 / mpc_module.LADDER_CAPACITY_RATIO, abs=0.001)
    assert result["high"] < result["superhigh"]


def test_monotone_constraint_trusts_the_better_sampled_profile() -> None:
    """A noisy estimate from a rarely-used speed must not corrupt well-sampled ones.

    Mirrors the real deployment: superhigh runs constantly (thousands of
    samples), high often, med almost never. A plain forward max() pass would
    propagate med's noisy high reading into both stronger profiles.
    """
    learning = ThermalLearning()
    for _ in range(1656):
        learning.add_slope_sample("superhigh", -0.9, 1.0, "cool")
    for _ in range(145):
        learning.add_slope_sample("high", -0.5, 1.0, "cool")
    for _ in range(10):
        learning.add_slope_sample("med", -1.6, 1.0, "cool")  # noisy: looks stronger than superhigh

    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=["silent", "low", "med", "high", "superhigh"],
    )
    result = mpc.build_monotone_slopes(["silent", "low", "med", "high", "superhigh"], "cool")

    # The two well-sampled profiles keep their learned values...
    assert result["high"] == pytest.approx(0.5, abs=0.001)
    assert result["superhigh"] == pytest.approx(0.9, abs=0.001)
    # ...and the 10-sample outlier is rewritten one ladder step below high, not
    # pinned onto it: an exact tie would leave the MPC unable to tell med from
    # high thermally, so the energy term would always pick med.
    assert result["med"] == pytest.approx(0.5 / mpc_module.LADDER_CAPACITY_RATIO, abs=0.001)
    assert result["med"] < result["high"]


def test_monotone_constraint_raises_a_poorly_sampled_stronger_mode() -> None:
    """The trust rule is symmetric: a thin *stronger* profile is raised, not trusted."""
    learning = ThermalLearning()
    for _ in range(800):
        learning.add_slope_sample("high", -0.9, 1.0, "cool")
    for _ in range(10):
        learning.add_slope_sample("superhigh", -0.3, 1.0, "cool")  # thin, reads weaker than high

    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=["silent", "low", "med", "high", "superhigh"],
    )
    result = mpc.build_monotone_slopes(["silent", "low", "med", "high", "superhigh"], "cool")

    assert result["high"] == pytest.approx(0.9, abs=0.001)  # 800 samples: untouched
    # Lifted a ladder step *above* high, not tied with it: tying them would make the
    # energy term always prefer high, so superhigh would never run again and its
    # profile could never recover from being thin.
    assert result["superhigh"] == pytest.approx(0.9 * mpc_module.LADDER_CAPACITY_RATIO, abs=0.001)
    assert result["superhigh"] > result["high"]


def test_monotone_constraint_keeps_a_consistent_ladder_untouched() -> None:
    """A ladder that is already ordered is returned verbatim, whatever its spacing.

    The ladder ratio only ever synthesises a replacement for a rejected estimate;
    it must never be imposed as a minimum gap between measured values, or a real
    ladder spaced more tightly than the ratio would be silently rewritten.
    """
    learning = ThermalLearning()
    # The production ladder: adjacent ratios are 2.08 and 1.76, i.e. both sides of
    # LADDER_CAPACITY_RATIO, and the bottom two speeds are negative.
    real = {"silent": -0.56, "low": -0.1, "med": 0.24, "high": 0.5, "superhigh": 0.879}
    for fan_mode, slope in real.items():
        learning.set_mode_effective_slope(fan_mode, "cool", slope)

    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=["silent", "low", "med", "high", "superhigh"],
    )
    result = mpc.build_monotone_slopes(["silent", "low", "med", "high", "superhigh"], "cool")

    for fan_mode, slope in real.items():
        assert result[fan_mode] == pytest.approx(slope, abs=0.001)


def test_monotone_constraint_always_returns_an_ordered_ladder() -> None:
    """Fuzz the invariant the rest of the MPC relies on: the result is never inverted.

    Covers ladders with missing profiles, wildly inconsistent estimates, sign
    changes and lopsided sample counts — including the case that broke an earlier
    pairwise implementation, where a thin profile sat between two better-sampled
    ones that were themselves inverted.
    """
    fan_modes = ["silent", "low", "med", "high", "superhigh"]
    rng = random.Random(7)

    for _ in range(300):
        learning = ThermalLearning()
        for fan_mode in fan_modes:
            if rng.random() < 0.25:
                continue  # profile never learned
            slope = rng.uniform(-1.5, 2.0)
            # Only the *relative* sample counts steer placement order, so modest
            # spreads exercise the same branches without a slow suite.
            for _ in range(rng.choice([10, 10, 14, 40])):
                learning.add_slope_sample(fan_mode, -slope, 1.0, "cool")

        mpc = MPCController(learning=learning, deadband=0.3, min_interval=10, fan_modes=fan_modes)
        result = mpc.build_monotone_slopes(fan_modes, "cool")

        values = [result[fan_mode] for fan_mode in fan_modes if fan_mode in result]
        assert values == sorted(values), f"inverted ladder produced: {result}"


def test_monotone_constraint_noop_when_already_ordered() -> None:
    """When profiles are already monotone, the constraint should not change values."""
    learning = ThermalLearning()

    learning.set_mode_effective_slope("low", "heat", 0.15)
    learning.set_mode_effective_slope("medium", "heat", 0.5)
    learning.set_mode_effective_slope("high", "heat", 1.0)

    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=FAN_MODES,
    )

    monotone = mpc.build_monotone_slopes(FAN_MODES, "heat")
    assert isinstance(monotone, dict)
    assert monotone["low"] == pytest.approx(0.15, abs=0.001)
    assert monotone["medium"] == pytest.approx(0.5, abs=0.001)
    assert monotone["high"] == pytest.approx(1.0, abs=0.001)


def test_mpc_handles_long_dead_time_without_blindness() -> None:
    """MPC detects faster modes even with a long dead time due to adaptive horizon."""
    learning = ThermalLearning()
    # Mock high and superhigh learned slopes
    learning.set_mode_effective_slope("high", "cool", 1.2)
    learning.set_mode_effective_slope("superhigh", "cool", 1.5)
    # Mock a long dead time of 27 minutes
    learning.add_response_event(27.0)

    mpc = MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=["silent", "low", "med", "high", "superhigh"],
    )

    # Evaluate cooling with a large error (3.2), current_fan is 'med' (which is unlearned)
    result = mpc.evaluate(
        current_temp=25.2,
        target_temp=22.0,
        vtherm_slope=0.0,
        hvac_mode="cool",
        current_fan="med",
        is_window_open=False,
        minutes_since_change=50.0,
    )

    # Thanks to adaptive horizon, MPC sees past the 27-minute dead-time delay
    # and correctly recommends superhigh (or high) over med, instead of remaining blind
    assert result["mpc_fan_mode"] in ("high", "superhigh")
    assert result["mpc_would_change_now"] == "yes"


def _seed_gap_profile(learning: ThermalLearning, fan_mode: str, hvac_mode: str, a: float, b: float) -> None:
    """Seed a profile whose effective slope follows a + b·error."""
    import time

    now = time.time()
    sign = -1.0 if hvac_mode == "cool" else 1.0
    samples = []
    for i, err in enumerate([0.5, 1.0, 1.5, 2.0, 2.5, 3.0] * 2):
        samples.append((now + i, fan_mode, sign * (a + b * err), hvac_mode, err))
    learning.slope_samples = list(learning.slope_samples) + samples


def test_gap_model_projects_faster_cooling_when_far_from_target() -> None:
    """A gap-dependent profile cools faster from a hot room than the same constant slope."""
    gap_learning = ThermalLearning()
    _seed_gap_profile(gap_learning, "superhigh", "cool", a=0.5, b=1.0)
    gap_mpc = MPCController(learning=gap_learning, deadband=0.3, min_interval=10, fan_modes=["superhigh"])

    # Equivalent constant-slope profile pinned to the gap model's working value.
    working = gap_learning.get_mode_effective_slope("superhigh", "cool")
    const_learning = ThermalLearning()
    const_learning.set_mode_effective_slope("superhigh", "cool", working)
    const_mpc = MPCController(learning=const_learning, deadband=0.3, min_interval=10, fan_modes=["superhigh"])

    kwargs = dict(current_temp=26.0, target_temp=24.0, vtherm_slope=-1.0, hvac_mode="cool", current_fan="superhigh", minutes_since_change=20.0)
    gap = gap_mpc.evaluate(**kwargs)
    const = const_mpc.evaluate(**kwargs)

    # Hot room (error 2.0): gap model uses ~2.5 °C/h, so it cools further than the constant ~1.5.
    assert gap["mpc_predicted_temperature_30m"] < const["mpc_predicted_temperature_30m"]


def test_gap_model_does_not_plunge_past_target() -> None:
    """The gap model decelerates near the setpoint instead of projecting phantom overshoot."""
    learning = ThermalLearning()
    _seed_gap_profile(learning, "superhigh", "cool", a=0.5, b=1.0)
    mpc = MPCController(learning=learning, deadband=0.3, min_interval=10, fan_modes=["superhigh"])

    result = mpc.evaluate(
        current_temp=24.4,
        target_temp=24.0,
        vtherm_slope=-0.5,
        hvac_mode="cool",
        current_fan="superhigh",
        minutes_since_change=20.0,
    )
    # Starting only 0.4°C above target, a 30-min projection must asymptote toward 24.0,
    # not dive well below it the way a constant-slope model would.
    assert result["mpc_predicted_temperature_30m"] >= 23.7


def test_gap_aware_disturbance_bias_stays_small_without_disturbance() -> None:
    """When the observed slope matches the gap model at the current error, bias stays ~0."""
    learning = ThermalLearning()
    _seed_gap_profile(learning, "superhigh", "cool", a=0.5, b=1.0)
    mpc = MPCController(learning=learning, deadband=0.3, min_interval=10, fan_modes=["superhigh"])

    # Room 2°C above target -> model expects ~2.5 °C/h effective cooling (raw vtherm_slope ≈ -2.5).
    for _ in range(10):
        mpc.evaluate(
            current_temp=26.0,
            target_temp=24.0,
            vtherm_slope=-2.5,
            hvac_mode="cool",
            current_fan="superhigh",
            minutes_since_change=40.0,
        )
    assert abs(mpc.disturbance_bias) < 0.2


def test_learning_response_sensor_with_mixed_tuple_lengths() -> None:
    """SmartFanLearningResponseSensor extra_state_attributes handles mixed lengths in response_events."""
    import time

    learning = ThermalLearning()
    mpc = _build_mpc(learning)

    # Inject mixed length response events
    learning.response_events = [
        (time.time() - 100, 12.0),  # 2-tuple (old format)
        (time.time() - 200, 15.0, "heat"),  # 3-tuple (new format with hvac_mode)
        (time.time() - 300, 0.0, "cool"),  # 3-tuple to be ignored (t <= 0)
    ]

    sensor = SmartFanLearningResponseSensor("entry-1", "climate.living_room", mpc)

    assert sensor.native_value == 3
    attrs = sensor.extra_state_attributes
    assert attrs["response_samples"] == 0  # because is_ready() is False, returns fallback 0
    assert attrs["avg_response_time_min"] == pytest.approx(13.5)


# --- Hold-equilibrium (economic) mode ------------------------------------
_HOLD_FAN_MODES = ["low", "medium", "high"]


def _build_hold_cool_mpc() -> MPCController:
    """Build an MPC with cool profiles where low barely conditions and med/high hold."""
    learning = ThermalLearning()
    learning.set_mode_effective_slope("low", "cool", 0.05)
    # medium is *measured*, not merely seeded: a climb from low to high may not
    # skip over an unmeasured intermediate rung (see MULTI_RANK_JUMP_ERROR), and
    # these tests are about the hold-equilibrium trade-off, not exploration.
    for _ in range(12):
        learning.add_slope_sample("medium", -0.9, 0.3, "cool")
    learning.set_mode_effective_slope("high", "cool", 1.8)
    learning.add_response_event(10.0, "cool")
    return MPCController(
        learning=learning,
        deadband=0.3,
        min_interval=10,
        fan_modes=_HOLD_FAN_MODES,
    )


def _evaluate_hold(current_temp: float) -> dict:
    """Run a cool evaluation at ``current_temp`` against a 24.0 setpoint."""
    return _build_hold_cool_mpc().evaluate(
        current_temp=current_temp,
        target_temp=24.0,
        vtherm_slope=0.0,
        hvac_mode="cool",
        current_fan="low",
        minutes_since_change=60.0,
    )


def test_hold_equilibrium_enabled_by_default() -> None:
    """The economic hold mode ships enabled after validation on the production trace."""
    assert mpc_module.HOLD_EQUILIBRIUM is True


def test_hold_equilibrium_dormant_beyond_deadband(monkeypatch: pytest.MonkeyPatch) -> None:
    """Far from the setpoint (error > deadband) the flag must not change anything."""
    # 25.0 vs 24.0 target => 1.0 C error, well outside the 0.3 deadband.
    monkeypatch.setattr(mpc_module, "HOLD_EQUILIBRIUM", False)
    off = _evaluate_hold(25.0)
    monkeypatch.setattr(mpc_module, "HOLD_EQUILIBRIUM", True)
    on = _evaluate_hold(25.0)

    assert on["mpc_fan_mode"] == off["mpc_fan_mode"]
    assert on["mpc_cost"] == pytest.approx(off["mpc_cost"])


def test_hold_equilibrium_is_never_weaker_and_never_costlier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside the hold zone the controller never coasts to a weaker mode and never
    costs more than without it.

    With the deadband on both comfort terms, a speed that keeps the room inside
    the band is charged nothing thermal, so the cheapest one -- low, which still
    conditions slowly -- wins either way.
    """
    monkeypatch.setattr(mpc_module, "HOLD_EQUILIBRIUM", False)
    off = _evaluate_hold(24.2)
    monkeypatch.setattr(mpc_module, "HOLD_EQUILIBRIUM", True)
    on = _evaluate_hold(24.2)

    off_rank = _HOLD_FAN_MODES.index(off["mpc_fan_mode"])
    on_rank = _HOLD_FAN_MODES.index(on["mpc_fan_mode"])

    # Never weaker than baseline, and the relaxed penalties never raise the cost.
    assert on_rank >= off_rank
    assert on["mpc_cost"] <= off["mpc_cost"] + 1e-9
    assert (off["mpc_fan_mode"], on["mpc_fan_mode"]) == ("low", "low")


# --- Adaptive min interval (coupled to learned dead time) -----------------
def _ready_learning_with_dead_time(dead_time: float) -> ThermalLearning:
    """Build a ready ThermalLearning whose learned dead time is ``dead_time``."""
    learning = ThermalLearning()
    for _ in range(90):  # 270 samples >= MIN_SAMPLES_LEARNING => is_ready()
        learning.add_slope_sample("low", 0.3, 0.8, "heat")
        learning.add_slope_sample("medium", 0.9, 0.8, "heat")
        learning.add_slope_sample("high", 1.5, 0.8, "heat")
    # Enough response events for the dead time to be trusted: that -- not the
    # slope-sample count behind is_ready() -- is what unlocks the adaptive
    # interval (see MIN_RESPONSE_EVENTS_FOR_ADAPTIVE_INTERVAL).
    for _ in range(mpc_module.MIN_RESPONSE_EVENTS_FOR_ADAPTIVE_INTERVAL):
        learning.add_response_event(dead_time, "heat")
    assert learning.is_ready()
    return learning


def test_effective_min_interval_rises_to_learned_dead_time() -> None:
    """When the dead time exceeds the configured floor, the effective dwell follows it."""
    learning = _ready_learning_with_dead_time(20.0)
    mpc = _build_mpc(learning, min_interval=10)
    assert mpc._effective_min_interval(learning.get_dead_time()) == pytest.approx(20.0)


def test_effective_min_interval_is_floored_by_config() -> None:
    """A short learned dead time never lowers the effective dwell below the config floor."""
    learning = _ready_learning_with_dead_time(6.0)
    mpc = _build_mpc(learning, min_interval=10)
    assert mpc._effective_min_interval(learning.get_dead_time()) == pytest.approx(10.0)


def test_effective_min_interval_is_capped() -> None:
    """A spuriously large dead time is capped at MAX_ADAPTIVE_INTERVAL_FACTOR x floor."""
    learning = _ready_learning_with_dead_time(40.0)
    mpc = _build_mpc(learning, min_interval=10)
    cap = 10 * mpc_module.MAX_ADAPTIVE_INTERVAL_FACTOR
    assert mpc._effective_min_interval(learning.get_dead_time()) == pytest.approx(cap)


def test_effective_min_interval_uses_config_before_the_dead_time_is_trusted() -> None:
    """A dead time resting on a single event must not stretch the interval."""
    learning = ThermalLearning()
    learning.add_response_event(25.0, "heat")
    mpc = _build_mpc(learning, min_interval=10)
    assert learning.response_event_count() < mpc_module.MIN_RESPONSE_EVENTS_FOR_ADAPTIVE_INTERVAL
    assert mpc._effective_min_interval(learning.get_dead_time()) == pytest.approx(10.0)


def test_adaptive_interval_holds_change_until_dead_time_elapses() -> None:
    """A beneficial change is held until the dead-time-based interval elapses."""
    learning = _ready_learning_with_dead_time(20.0)
    mpc = _build_mpc(learning, min_interval=10)
    # First call establishes the comfort-error baseline for this hold (small
    # growth budget below).
    mpc.evaluate(
        current_temp=19.25,
        target_temp=20.0,
        vtherm_slope=0.25,
        hvac_mode="heat",
        current_fan="low",
        minutes_since_change=1.0,
    )
    # 15 min since last change: allowed under the old fixed 10-min rule, but the
    # learned 20-min dead time means the previous change is not observable yet.
    # Error only grew 0.05C since the baseline, well under the escalation
    # threshold, so the hold is not bypassed.
    held = mpc.evaluate(
        current_temp=19.2,
        target_temp=20.0,
        vtherm_slope=0.25,
        hvac_mode="heat",
        current_fan="low",
        minutes_since_change=15.0,
    )
    assert held["mpc_would_change_now"] == "no"
    assert "Min interval active" in held["mpc_reason"]

    allowed = mpc.evaluate(
        current_temp=19.2,
        target_temp=20.0,
        vtherm_slope=0.25,
        hvac_mode="heat",
        current_fan="low",
        minutes_since_change=25.0,
    )
    assert allowed["mpc_would_change_now"] == "yes"


def test_dead_time_lock_escalates_on_growing_error() -> None:
    """Comfort error worsening since the change bypasses the dead-time lock.

    Regression test: a misjudged step-down (e.g. hold-equilibrium picking a
    fan mode that turns out too weak) used to leave the room drifting away
    from target for the full ~20 min adaptive interval with no way out. The
    escalation is trend-based (growth since the change) rather than a static
    error threshold, so it fires however small the deadband is.
    """
    learning = _ready_learning_with_dead_time(20.0)
    mpc = _build_mpc(learning, min_interval=10)
    # First call establishes the baseline right after the change (small error).
    mpc.evaluate(
        current_temp=19.6,
        target_temp=20.0,
        vtherm_slope=0.25,
        hvac_mode="heat",
        current_fan="low",
        minutes_since_change=1.0,
    )
    # 6 min later the room has drifted 0.4C further from target (past the
    # 0.30C threshold of a 0.2C sensor) while still inside the 20-min learned
    # dead time. One cycle is not enough: the growth must be confirmed.
    first = mpc.evaluate(
        current_temp=19.2,
        target_temp=20.0,
        vtherm_slope=0.25,
        hvac_mode="heat",
        current_fan="low",
        minutes_since_change=6.0,
    )
    assert first["mpc_would_change_now"] == "no"
    escalated = mpc.evaluate(
        current_temp=19.2,
        target_temp=20.0,
        vtherm_slope=0.25,
        hvac_mode="heat",
        current_fan="low",
        minutes_since_change=11.0,
    )
    assert escalated["mpc_would_change_now"] == "yes"
    assert escalated["mpc_fan_mode"] != "low"
    assert "Emergency escalation" in escalated["mpc_reason"]


def test_dead_time_lock_does_not_escalate_downward() -> None:
    """The emergency escalation never lets the lock be bypassed to step down."""
    learning = _ready_learning_with_dead_time(20.0)
    mpc = _build_mpc(learning, min_interval=10)
    # Overshoot past the setpoint in heat mode, still growing further past
    # target - a large and worsening error, but the unconstrained candidate is
    # weaker than the current fan, so this must stay held rather than treated
    # as an emergency.
    mpc.evaluate(
        current_temp=20.9,
        target_temp=20.0,
        vtherm_slope=1.5,
        hvac_mode="heat",
        current_fan="high",
        minutes_since_change=1.0,
    )
    held = mpc.evaluate(
        current_temp=21.0,
        target_temp=20.0,
        vtherm_slope=1.5,
        hvac_mode="heat",
        current_fan="high",
        minutes_since_change=8.0,
    )
    assert held["mpc_would_change_now"] == "no"
    assert held["mpc_fan_mode"] == "high"
    assert "Emergency escalation" not in held["mpc_reason"]
    assert "Min interval active" in held["mpc_reason"]


def test_multi_rank_stepdown_blocked_when_candidate_cannot_sustain_progress() -> None:
    """A 2+ rank drop to a mode with a non-positive own profile is blocked.

    Regression test for a real incident: with fan_modes ordered weakest to
    strongest and learned profiles silent=-0.56, low=-0.1, med=0.24, high=0.5,
    superhigh=1.05 C/h, the controller jumped straight from superhigh to low
    near the deadband. Low's own profile shows it cannot cool this room at
    all (negative slope even 1C from target) - the pick only looked good
    because the forecast used for the switch-down check is dominated by
    superhigh's momentum for the whole dead-time window, masking low's real
    (in)capability. Once switched, the room drifted away from target.
    """
    fan_modes = ["silent", "low", "med", "high", "superhigh"]
    slopes = {"silent": -0.56, "low": -0.1, "med": 0.24, "high": 0.5, "superhigh": 1.05}
    learning = ThermalLearning()
    for mode, slope in slopes.items():
        learning.set_mode_effective_slope(mode, "cool", slope)
    learning.add_response_event(20.0, "cool")

    mpc = MPCController(learning=learning, deadband=0.2, min_interval=10, fan_modes=fan_modes)
    decision = mpc.evaluate(
        current_temp=24.2,
        target_temp=24.0,
        vtherm_slope=-1.02,
        hvac_mode="cool",
        current_fan="superhigh",
        minutes_since_change=300.0,
    )
    assert decision["mpc_fan_mode"] not in ("low", "silent")
    assert "Blocked 3-rank drop to low" in decision["mpc_reason"]

    # Adjacent-rank switching stays untouched: high (1 rank down) is a
    # legitimate cost-minimising pick when its own profile is sound.
    slopes_adjacent_only = dict(slopes)
    learning2 = ThermalLearning()
    for mode, slope in slopes_adjacent_only.items():
        learning2.set_mode_effective_slope(mode, "cool", slope)
    learning2.add_response_event(20.0, "cool")
    mpc2 = _build_mpc(learning2, fan_modes=fan_modes, min_interval=10)
    decision2 = mpc2.evaluate(
        current_temp=24.6,
        target_temp=24.0,
        vtherm_slope=-0.4,
        hvac_mode="cool",
        current_fan="superhigh",
        minutes_since_change=300.0,
    )
    assert "Blocked" not in decision2["mpc_reason"]


# --- Audit 2026-09: exploration and unmeasured speeds ----------------------
def test_current_unmeasured_speed_is_modelled_on_the_observed_slope() -> None:
    """A speed with no profile that is losing ground must not be modelled as gaining.

    Production scenario: learned superhigh, unmeasured high, room warming at
    0.3 degC/h in cool. The +0.2 degC/h floor on the fallback kept the
    controller on high until the room was 2 degC off target; with the observed
    slope as the fallback it escalates while the error is still 0.6.
    """
    fan_modes = ["silent", "low", "med", "high", "superhigh"]
    learning = ThermalLearning()
    _seed_gap_profile(learning, "superhigh", "cool", a=0.4, b=0.6)
    for _ in range(5):
        learning.add_response_event(25.0, "cool")
    mpc = MPCController(learning=learning, deadband=0.2, min_interval=10, fan_modes=fan_modes)

    result = mpc.evaluate(
        current_temp=24.6,
        target_temp=24.0,
        vtherm_slope=0.3,  # raw slope positive in cool: the room is warming
        hvac_mode="cool",
        current_fan="high",
        minutes_since_change=60.0,
    )

    assert mpc.get_live_mode_slope("high", "cool") == (pytest.approx(-0.3), False)
    assert result["mpc_fan_mode"] == "superhigh"
    assert result["mpc_would_change_now"] == "yes"


def test_a_negative_seeded_slope_reaches_the_simulator() -> None:
    """A speed seeded as losing ground must be simulated as losing ground.

    ``_gap_slope`` floored every modelled slope at 0, so a silent seeded at
    -0.5 degC/h was simulated as neutral -- the only place the negative value
    ever mattered was the multi-rank step-down guard.
    """
    learning = ThermalLearning()
    learning.set_mode_effective_slope("silent", "cool", -0.5)
    learning.add_response_event(10.0, "cool")
    mpc = MPCController(learning=learning, deadband=0.2, min_interval=10, fan_modes=["silent"])

    result = mpc.evaluate(
        current_temp=24.0,
        target_temp=24.0,
        vtherm_slope=0.0,
        hvac_mode="cool",
        current_fan="silent",
        minutes_since_change=60.0,
    )

    assert result["mpc_predicted_temperature_30m"] > 24.1


def _learning_with_measured_medium_and_high() -> ThermalLearning:
    """Measured medium/high profiles, nothing known about low, default dead time."""
    learning = ThermalLearning()
    for _ in range(12):
        learning.add_slope_sample("medium", 0.9, 0.8, "heat")
        learning.add_slope_sample("high", 1.5, 0.8, "heat")
    return learning


def test_learning_hold_keeps_an_unmeasured_speed_until_it_can_be_sampled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dwell on a speed without a measured profile covers the learning gate.

    Otherwise the change gate (1 x dead time) re-opens before the first sample
    is recordable (1.5 x dead time) and the speed is left unmeasured -- hence
    never credible on cost, hence never chosen again. Dead time here is the
    10-minute default: hold = 15 + LEARNING_HOLD_EXTRA_MINUTES = 25 min.
    """
    mpc = _build_mpc(_learning_with_measured_medium_and_high())
    kwargs = dict(current_temp=19.4, target_temp=20.0, vtherm_slope=0.0, hvac_mode="heat", current_fan="low")

    held = mpc.evaluate(**kwargs, minutes_since_change=20.0)
    assert held["mpc_would_change_now"] == "no"
    assert "Learning hold: low has no measured profile yet" in held["mpc_reason"]

    released = mpc.evaluate(**kwargs, minutes_since_change=30.0)
    assert released["mpc_would_change_now"] == "yes"

    # Control: without the hold the very same cycle would have switched.
    monkeypatch.setattr(mpc_module, "LEARNING_HOLD_EXTRA_MINUTES", -100.0)
    assert mpc.evaluate(**kwargs, minutes_since_change=20.0)["mpc_would_change_now"] == "yes"


def test_learning_hold_yields_to_comfort() -> None:
    """The hold never keeps a speed that is failing the room.

    Released by the emergency escalation when the error grows past the budget,
    and not applied at all past MULTI_RANK_JUMP_ERROR (a recovery, where the
    speed has already shown what it can do).
    """
    mpc = _build_mpc(_learning_with_measured_medium_and_high())
    kwargs = dict(target_temp=20.0, vtherm_slope=0.0, hvac_mode="heat", current_fan="low")

    mpc.evaluate(current_temp=19.6, minutes_since_change=6.0, **kwargs)  # baseline: error 0.4
    mpc.evaluate(current_temp=19.2, minutes_since_change=12.0, **kwargs)  # grew 0.4: first breach
    drifting = mpc.evaluate(current_temp=19.2, minutes_since_change=17.0, **kwargs)  # confirmed
    assert drifting["mpc_would_change_now"] == "yes"
    assert "Emergency escalation" in drifting["mpc_reason"]

    far_off = _build_mpc(_learning_with_measured_medium_and_high()).evaluate(current_temp=18.5, minutes_since_change=20.0, **kwargs)
    assert far_off["mpc_would_change_now"] == "yes"
    assert "Learning hold" not in far_off["mpc_reason"]


def test_a_climb_does_not_skip_an_unmeasured_rung() -> None:
    """Rising past an intermediate speed nobody has measured is not allowed.

    On 23 days of production, 26 of 29 climbs to superhigh came straight from
    silent or low, so med and high were only ever run while the room was
    already too cold -- where their slope is ~0 and says nothing. The rung is
    tried first, unless the error is a recovery (> MULTI_RANK_JUMP_ERROR).
    """
    learning = ThermalLearning()
    for _ in range(12):
        learning.add_slope_sample("low", 0.2, 0.8, "heat")
        learning.add_slope_sample("high", 1.5, 0.8, "heat")
    learning.set_mode_effective_slope("medium", "heat", 0.9)  # seeded, never measured
    mpc = _build_mpc(learning)
    kwargs = dict(target_temp=20.0, vtherm_slope=0.2, hvac_mode="heat", current_fan="low", minutes_since_change=60.0)

    stepped = mpc.evaluate(current_temp=19.1, **kwargs)
    assert stepped["mpc_fan_mode"] == "medium"
    assert "Blocked 2-rank jump to high: medium has no measured profile yet" in stepped["mpc_reason"]

    recovery = mpc.evaluate(current_temp=18.5, **kwargs)
    assert recovery["mpc_fan_mode"] == "high"
    assert "Blocked" not in recovery["mpc_reason"]


def test_notify_fan_change_resets_the_escalation_baseline() -> None:
    """A fan change reported explicitly starts a fresh comfort-error baseline.

    The time-based detection (first cycle within one cycle length of the
    change) is a fallback only; a skipped cycle or an externally changed fan
    left the escalation comparing against a baseline from a previous hold.
    """
    learning = _ready_learning_with_dead_time(20.0)
    mpc = _build_mpc(learning, min_interval=10)
    mpc.evaluate(current_temp=19.6, target_temp=20.0, vtherm_slope=0.25, hvac_mode="heat", current_fan="low", minutes_since_change=1.0)

    # Without notification the old 0.4 baseline is kept: 0.8 reads as +0.4 growth.
    mpc.evaluate(current_temp=19.2, target_temp=20.0, vtherm_slope=0.25, hvac_mode="heat", current_fan="low", minutes_since_change=6.0)
    stale = mpc.evaluate(current_temp=19.2, target_temp=20.0, vtherm_slope=0.25, hvac_mode="heat", current_fan="low", minutes_since_change=11.0)
    assert "Emergency escalation" in stale["mpc_reason"]

    # The same room state right after a (reported) change is a fresh baseline.
    mpc.notify_fan_change()
    mpc.evaluate(current_temp=19.2, target_temp=20.0, vtherm_slope=0.25, hvac_mode="heat", current_fan="low", minutes_since_change=6.0)
    fresh = mpc.evaluate(current_temp=19.2, target_temp=20.0, vtherm_slope=0.25, hvac_mode="heat", current_fan="low", minutes_since_change=11.0)
    assert "Emergency escalation" not in fresh["mpc_reason"]
    assert fresh["mpc_would_change_now"] == "no"


def test_dead_time_trust_is_counted_per_hvac_mode() -> None:
    """Five heating events unlock the heating interval, not the cooling one.

    The trust gate counted every stored event, so a dead time that cool only
    reaches through the pooled fallback still raised cool's change interval.
    """
    learning = ThermalLearning()
    for _ in range(mpc_module.MIN_RESPONSE_EVENTS_FOR_ADAPTIVE_INTERVAL):
        learning.add_response_event(24.0, "heat")
    mpc = _build_mpc(learning, min_interval=10)

    assert mpc._dead_time_is_trusted("heat") is True  # noqa: SLF001
    assert mpc._dead_time_is_trusted("cool") is False  # noqa: SLF001
    assert mpc._effective_min_interval(24.0, "heat") == 24.0  # noqa: SLF001
    assert mpc._effective_min_interval(24.0, "cool") == 10.0  # noqa: SLF001


def test_events_from_unregulated_modes_never_build_a_dead_time() -> None:
    """A month of dry operation must not hand cool a learned, trusted dead time.

    Response events recorded in dry or fan_only (stores written before the
    manager stopped recording them) are ignored by the dead time, its pooled
    fallback and the trust gate alike.
    """
    learning = ThermalLearning()
    for _ in range(20):
        learning.add_response_event(45.0, "dry")
    mpc = _build_mpc(learning, min_interval=10)

    assert learning.get_dead_time("cool") == mpc_module.DEFAULT_DEAD_TIME
    assert learning.get_dead_time() == mpc_module.DEFAULT_DEAD_TIME
    assert mpc._dead_time_is_trusted("cool") is False  # noqa: SLF001
    assert mpc._dead_time_is_trusted() is False  # noqa: SLF001
    # The diagnostic total still reports what is stored.
    assert learning.response_event_count() == 20


# --- Step 4 guards: sensor resolution, confirmed escalation, cost scale ---------
def test_sensor_resolution_is_detected_from_the_smallest_reading_step() -> None:
    """The smallest non-zero change between readings, bounded, with a 0.2 fallback."""
    mpc = _build_mpc(ThermalLearning())
    assert mpc.sensor_resolution == pytest.approx(0.2)
    assert mpc.escalation_threshold == pytest.approx(0.3)

    for temp in (20.0, 20.1, 20.1, 20.3, 20.2, 20.4):
        mpc.evaluate(current_temp=temp, target_temp=20.0, vtherm_slope=0.0, hvac_mode="heat", current_fan="low", minutes_since_change=60.0)

    assert mpc.sensor_resolution == pytest.approx(0.1)
    assert mpc.escalation_threshold == pytest.approx(0.15)

    fine = _build_mpc(ThermalLearning())
    for temp in (20.0, 20.01, 20.02, 20.03, 20.04):
        fine.evaluate(current_temp=temp, target_temp=20.0, vtherm_slope=0.0, hvac_mode="heat", current_fan="low", minutes_since_change=60.0)
    assert fine.sensor_resolution == pytest.approx(0.05)


def test_one_sensor_step_does_not_escalate() -> None:
    """A single 0.2 degC reading change during a hold is not an emergency.

    With the old 0.15 threshold one step of a 0.2 degC sensor bypassed the min
    interval, the hysteresis, the climb guard and the learning hold.
    """
    learning = _ready_learning_with_dead_time(20.0)
    mpc = _build_mpc(learning, min_interval=10)
    mpc.evaluate(current_temp=19.6, target_temp=20.0, vtherm_slope=0.25, hvac_mode="heat", current_fan="low", minutes_since_change=1.0)

    for minutes in (6.0, 11.0, 16.0):
        result = mpc.evaluate(current_temp=19.4, target_temp=20.0, vtherm_slope=0.25, hvac_mode="heat", current_fan="low", minutes_since_change=minutes)
        assert "Emergency escalation" not in result["mpc_reason"]
        assert result["mpc_would_change_now"] == "no"


def test_a_large_error_escalates_without_waiting_for_confirmation() -> None:
    """Past MULTI_RANK_JUMP_ERROR the growth escalates on its first cycle."""
    learning = _ready_learning_with_dead_time(20.0)
    mpc = _build_mpc(learning, min_interval=10)
    mpc.evaluate(current_temp=19.3, target_temp=20.0, vtherm_slope=0.25, hvac_mode="heat", current_fan="low", minutes_since_change=1.0)

    result = mpc.evaluate(current_temp=18.9, target_temp=20.0, vtherm_slope=0.25, hvac_mode="heat", current_fan="low", minutes_since_change=6.0)

    assert result["mpc_would_change_now"] == "yes"
    assert "Emergency escalation" in result["mpc_reason"]


def test_no_change_inside_the_deadband_on_the_short_side() -> None:
    """A predicted shortfall inside the deadband costs nothing: no climb for 0.1 degC.

    Without a deadband on the floor term, +0.1 degC of error (half the 0.2
    deadband) made the controller climb med -> high.
    """
    learning = ThermalLearning()
    learning.set_mode_effective_slope("low", "heat", 0.1)
    learning.set_mode_effective_slope("medium", "heat", 0.4)
    learning.set_mode_effective_slope("high", "heat", 0.9)
    mpc = MPCController(learning=learning, deadband=0.2, min_interval=10, fan_modes=FAN_MODES, exploration_probe=False)

    result = mpc.evaluate(current_temp=19.9, target_temp=20.0, vtherm_slope=0.05, hvac_mode="heat", current_fan="medium", minutes_since_change=60.0)

    assert result["mpc_fan_mode"] == "medium"
    assert result["mpc_would_change_now"] == "no"


def test_cost_terms_are_ordered_comfort_over_energy() -> None:
    """One rank of energy weighs what 0.05-0.1 degC of sustained shortfall does, not less.

    A sustained shortfall of x beyond the deadband costs 13x + 56x^2 per step;
    the energy step between the two lowest ranks must sit between x = 0.03 and
    x = 0.1, and every thermal term must be zero inside the deadband.
    """

    def sustained(excess: float) -> float:
        return 13 * excess + 56 * excess**2

    lowest_step = mpc_module.MODE_RANK_COST * (mpc_module.MODE_POWER_RATIO - 1)
    assert sustained(0.03) < lowest_step < sustained(0.1)

    learning = ThermalLearning()
    learning.set_mode_effective_slope("low", "heat", 0.0)
    mpc = MPCController(learning=learning, deadband=0.3, min_interval=10, fan_modes=["low"])
    held = mpc.evaluate(current_temp=19.8, target_temp=20.0, vtherm_slope=0.0, hvac_mode="heat", current_fan="low", minutes_since_change=60.0)
    # Only the (tie-breaker scaled) energy of rank 0 remains: no thermal cost at all.
    assert held["mpc_cost"] == pytest.approx(mpc_module.MODE_RANK_COST * mpc_module.HOLD_RANK_SCALE)


def test_every_candidate_starts_on_the_observed_slope() -> None:
    """Staying and switching are simulated on the same basis during the dead time.

    The current speed used to switch to its model at once while every other
    candidate kept the observed slope for the dead time: with a learned model
    far from what the room shows, the comparison favoured or penalised staying
    for a reason unrelated to the candidates.
    """
    learning = ThermalLearning()
    learning.set_mode_effective_slope("low", "heat", 2.0)
    learning.set_mode_effective_slope("medium", "heat", 2.5)
    learning.set_mode_effective_slope("high", "heat", 3.0)
    for _ in range(5):
        learning.add_response_event(20.0, "heat")
    mpc = MPCController(learning=learning, deadband=0.2, min_interval=10, fan_modes=FAN_MODES)

    result = mpc.evaluate(current_temp=19.0, target_temp=20.0, vtherm_slope=-0.6, hvac_mode="heat", current_fan="medium", minutes_since_change=60.0)

    # The room is observed losing 0.6 degC/h: during the 20-minute dead time the
    # forecast for staying follows that, not medium's learned +2.5 degC/h.
    assert result["mpc_predicted_temperature_10m"] < 19.0


def test_an_unlearned_weaker_speed_is_never_rated_above_the_current_one() -> None:
    """An observed losing speed bounds every weaker unlearned estimate from above."""
    fan_modes = ["silent", "low", "med", "high", "superhigh"]
    learning = ThermalLearning()
    _seed_gap_profile(learning, "superhigh", "cool", a=0.4, b=0.6)
    mpc = MPCController(learning=learning, deadband=0.2, min_interval=10, fan_modes=fan_modes)

    mpc.evaluate(current_temp=24.6, target_temp=24.0, vtherm_slope=0.3, hvac_mode="cool", current_fan="high", minutes_since_change=60.0)

    current, _ = mpc.get_live_mode_slope("high", "cool")
    for weaker in ("silent", "low", "med"):
        slope, learned = mpc.get_live_mode_slope(weaker, "cool")
        assert learned is False
        assert slope < current


# --- Step 5: exploration and dead-time gate ---------------------------------
class _Clock:
    """A settable wall clock for ThermalLearning."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _probe_setup(clock: _Clock | None = None, **mpc_kwargs) -> MPCController:
    """medium and high measured, low only seeded (losing ground): medium holds the room."""
    learning = ThermalLearning(clock=clock)
    learning.set_mode_effective_slope("low", "heat", -0.3)
    for _ in range(12):
        learning.add_slope_sample("medium", 0.0, 0.0, "heat", dwell_minutes=10.0)
        learning.add_slope_sample("high", 0.6, 0.0, "heat", dwell_minutes=10.0)
    return MPCController(learning=learning, deadband=0.2, min_interval=10, fan_modes=FAN_MODES, **mpc_kwargs)


def _hold_medium(mpc: MPCController, *, temp: float = 20.0, minutes: float = 60.0, fan: str = "medium") -> dict:
    """One heating cycle with the room at *temp* (setpoint 20) on *fan*."""
    return mpc.evaluate(current_temp=temp, target_temp=20.0, vtherm_slope=0.0, hvac_mode="heat", current_fan=fan, minutes_since_change=minutes)


def test_an_unmeasured_weaker_speed_is_probed_while_the_room_holds() -> None:
    """In band, established, no bias: step down to the unmeasured speed to measure it."""
    clock = _Clock()
    mpc = _probe_setup(clock)

    result = _hold_medium(mpc)

    assert result["mpc_fan_mode"] == "low"
    assert result["mpc_would_change_now"] == "yes"
    assert "Exploration probe" in result["mpc_reason"]
    assert result["mpc_exploration_probes"] == 1
    assert mpc.learning.last_probe_time("low", "heat") == clock.now


def test_a_probe_is_not_repeated_within_six_hours() -> None:
    """The same speed is probed again only after PROBE_INTERVAL_HOURS."""
    clock = _Clock()
    mpc = _probe_setup(clock)
    _hold_medium(mpc)

    clock.now += 5 * 3600
    assert _hold_medium(mpc)["mpc_fan_mode"] == "medium"

    clock.now += 2 * 3600
    assert _hold_medium(mpc)["mpc_fan_mode"] == "low"
    assert mpc.learning.probe_count == 2


@pytest.mark.parametrize(
    ("temp", "minutes"),
    [(19.7, 60.0), (20.0, 12.0)],
    ids=["outside the deadband", "not established"],
)
def test_no_probe_outside_a_steady_hold(temp: float, minutes: float) -> None:
    """A probe needs the room inside the deadband and an established regime."""
    mpc = _probe_setup()

    result = _hold_medium(mpc, temp=temp, minutes=minutes)

    assert "Exploration probe" not in result["mpc_reason"]


def test_no_probe_when_disabled_or_when_the_weaker_speed_is_measured() -> None:
    """Option off, or nothing left to measure below: no probe."""
    assert _hold_medium(_probe_setup(exploration_probe=False))["mpc_fan_mode"] == "medium"

    mpc = _probe_setup()
    for _ in range(12):
        mpc.learning.add_slope_sample("low", -0.3, 0.0, "heat", dwell_minutes=10.0)
    assert "Exploration probe" not in _hold_medium(mpc)["mpc_reason"]


def test_a_probe_is_abandoned_once_the_room_leaves_the_band() -> None:
    """Past deadband + one sensor step, the hold is released and the climb is immediate."""
    mpc = _probe_setup()
    _hold_medium(mpc)
    mpc.notify_fan_change()

    held = _hold_medium(mpc, temp=19.9, minutes=6.0, fan="low")
    assert held["mpc_fan_mode"] == "low"
    assert "Learning hold" in held["mpc_reason"]

    abandoned = _hold_medium(mpc, temp=19.55, minutes=11.0, fan="low")
    assert "Exploration probe abandoned" in abandoned["mpc_reason"]
    assert abandoned["mpc_fan_mode"] in ("medium", "high")
    assert abandoned["mpc_would_change_now"] == "yes"


def test_probe_timestamps_survive_a_restart() -> None:
    """Probe times and count are stored with the learning data."""
    clock = _Clock()
    mpc = _probe_setup(clock)
    _hold_medium(mpc)

    restored = ThermalLearning.from_dict(mpc.learning.to_dict(), clock=clock)

    assert restored.last_probe_time("low", "heat") == clock.now
    assert restored.probe_count == 1


def test_the_information_bonus_favours_a_poorly_measured_speed_when_enabled() -> None:
    """S2: with near-identical trajectories the unmeasured speed wins only with the bonus."""
    learning = ThermalLearning()
    learning.set_mode_effective_slope("low", "heat", 0.05)
    for _ in range(12):
        learning.add_slope_sample("medium", 0.05, 0.0, "heat", dwell_minutes=10.0)
        learning.add_slope_sample("high", 0.06, 0.0, "heat", dwell_minutes=10.0)

    def run(ucb: bool) -> dict:
        mpc = MPCController(learning=learning, deadband=0.3, min_interval=10, fan_modes=FAN_MODES, exploration_probe=False, exploration_ucb=ucb)
        return mpc.evaluate(current_temp=20.0, target_temp=20.0, vtherm_slope=0.05, hvac_mode="heat", current_fan="medium", minutes_since_change=60.0)

    assert run(False)["mpc_fan_mode"] == "medium"
    assert run(True)["mpc_fan_mode"] == "low"


def test_a_climb_stops_on_the_lowest_unmeasured_rung_when_measuring_under_load() -> None:
    """S3: a climb lands first on an unmeasured rung predicted to make progress under load.

    medium is seeded at -0.1 degC/h, so the climb guard (which only protects
    rungs that look viable on their own) lets low -> high skip it. With a +0.2
    degC/h disturbance helping, medium would make progress: measuring it under
    load is the only way to learn its gain.
    """
    fan_modes = ["low", "medium", "high", "superhigh"]
    learning = ThermalLearning()
    for _ in range(12):
        learning.add_slope_sample("low", -0.3, 0.5, "heat", dwell_minutes=10.0)
        learning.add_slope_sample("high", 1.2, 0.5, "heat", dwell_minutes=10.0)
        learning.add_slope_sample("superhigh", 1.6, 0.5, "heat", dwell_minutes=10.0)
    learning.set_mode_effective_slope("medium", "heat", -0.1)

    def run(under_load: bool) -> dict:
        mpc = MPCController(learning=learning, deadband=0.2, min_interval=10, fan_modes=fan_modes, exploration_probe=False, exploration_under_load=under_load)
        mpc._disturbance_bias = 0.2  # noqa: SLF001
        return mpc.evaluate(current_temp=19.3, target_temp=20.0, vtherm_slope=-0.1, hvac_mode="heat", current_fan="low", minutes_since_change=60.0)

    off, on = run(False), run(True)
    assert fan_modes.index(off["mpc_fan_mode"]) > 1, off["mpc_reason"]
    assert on["mpc_fan_mode"] == "medium", on["mpc_reason"]
    assert "under load" in on["mpc_reason"]


def test_the_learning_gate_caps_a_long_dead_time() -> None:
    """A 30-minute learned dead time sets the horizon, but the phase and hold use 15."""
    learning = ThermalLearning()
    for _ in range(5):
        learning.add_response_event(30.0, "heat")
    mpc = _build_mpc(learning)

    assert MPCController.gate_dead_time(30.0) == 15.0
    assert MPCController.detect_phase(23.0, MPCController.gate_dead_time(30.0)) == "ESTABLISHED"
    result = mpc.evaluate(current_temp=19.9, target_temp=20.0, vtherm_slope=0.0, hvac_mode="heat", current_fan="low", minutes_since_change=25.0)
    assert result["mpc_dead_time"] == 30.0
    # Learning hold of an unmeasured speed: 1.5 x 15 + 10 = 32.5 min, not 55.
    assert "/32.5 min" in result["mpc_reason"]
