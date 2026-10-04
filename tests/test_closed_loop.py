"""Closed-loop behaviour of the MPC on a simulated room (see tests/closed_loop.py).

The replay bench is open loop and cannot show what comfort a decision rule
obtains; these tests close the loop on a plant with a first-order inverter, an
actuator delay, a 0.2 degC sensor publishing on change (one reading every ~18
min while the room is steady) and VTherm's EMA slope.
"""

import math
import random
from unittest.mock import patch

import pytest

from custom_components.vtherm_mpc_fan.mpc_controller import MPCController
from custom_components.vtherm_mpc_fan.thermal_learning import ThermalLearning

from closed_loop import CAPACITY, FAN_MODES, PlantConfig, run_closed_loop, seed_true_profiles
from test_manager import _build_manager, _make_hass, _make_runtime

SCENARIOS = {
    "steady": PlantConfig(),
    "recovery": PlantConfig(start_temp=25.6),
    "heavy load": PlantConfig(load_mean=0.85, load_swing=0.1),
}


def _controller(config: PlantConfig) -> MPCController:
    """An MPC whose profiles match the plant, as on a mature installation."""
    learning = ThermalLearning()
    seed_true_profiles(learning, config)
    return MPCController(learning=learning, deadband=0.2, min_interval=10, fan_modes=list(FAN_MODES))


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("seed", [0, 1])
def test_closed_loop_is_stable_and_comfortable(scenario: str, seed: int) -> None:
    """Under 0.5 fan changes per hour and under 0.25 degC of comfort MAE over 48 h.

    The model's 30-minute forecast is reported next to the persistence forecast
    on the same instants: a coarse sensor makes persistence hard to beat, and a
    model MAE alone says nothing. It is not asserted on: while the room is
    steady the sensor publishes nothing, VTherm's slope stays at its last value
    and the forecast inherits it, which persistence does not.
    """
    config = SCENARIOS[scenario]
    result = run_closed_loop(_controller(config), 48, seed=seed, config=config)

    summary = (
        f"{scenario}/{seed}: {result.changes_per_hour:.2f} changes/h, comfort MAE {result.comfort_mae:.3f}, "
        f"T+30 MAE {result.prediction_mae_30m:.3f} (persistence {result.persistence_mae_30m:.3f}), time per speed {result.fan_minutes}"
    )
    assert result.changes_per_hour < 0.5, summary
    assert result.comfort_mae < 0.25, summary


def test_closed_loop_holds_on_the_speed_that_matches_the_load() -> None:
    """At equilibrium the controller settles on the speed that holds the band.

    medium holds this room 0.18 degC below the setpoint; with a 30-minute
    horizon the controller alternated a cheap low with a strong high around it.
    """
    config = PlantConfig(load_swing=0.0)
    result = run_closed_loop(_controller(config), 24, seed=0, config=config)

    total = sum(result.fan_minutes.values())
    assert result.fan_minutes["med"] / total > 0.6, result.fan_minutes


async def _run_manager_loop(hours: float, *, seed: int, known: tuple[str, ...]):
    """Drive the real feature manager (learning gates included) against the plant.

    Only the speeds in *known* start with a profile; the others must be learned
    by the manager's own sampling, from the visits the controller creates.
    Returns (manager, fan changes, comfort errors).
    """
    config = PlantConfig()
    rng = random.Random(seed)
    runtime = _make_runtime(current_temperature=config.start_temp, target_temperature=config.setpoint, regulated_target_temperature=config.setpoint, last_temperature_slope=0.0)
    hass = _make_hass(fan_mode=config.start_fan)
    start = 5_000_000.0
    with patch("time.time", return_value=start):
        manager = await _build_manager(runtime, hass)
        seed_true_profiles(manager.learning, config, fans=known)
    temp = config.start_temp
    fan_effective = config.start_fan
    pending: list[tuple[float, str]] = []
    power = CAPACITY[fan_effective]
    published = round(temp / config.sensor_step) * config.sensor_step
    last_publish = 0.0
    slope = 0.0
    minute = 0.0
    commands = 0
    errors = []
    for _ in range(int(hours * 60 / config.cycle_minutes)):
        for _ in range(config.cycle_minutes):
            while pending and pending[0][0] <= minute:
                fan_effective = pending.pop(0)[1]
            load = config.load_mean + config.load_swing * math.sin(2 * math.pi * minute / 1440.0) + rng.gauss(0.0, config.load_noise)
            power += (CAPACITY[fan_effective] * max(0.2, 1.0 + config.inverter_gain * (temp - config.setpoint)) - power) / config.power_tau
            temp += (load - power) / 60.0
            minute += 1.0
            quantised = round(temp / config.sensor_step) * config.sensor_step
            if abs(quantised - published) > 1e-9:
                slope = (1 - config.slope_ema) * slope + config.slope_ema * (quantised - published) / ((minute - last_publish) / 60.0)
                published, last_publish = quantised, minute
        runtime.current_temperature = published
        runtime.last_temperature_slope = slope
        errors.append(published - config.setpoint)
        with patch("time.time", return_value=start + minute * 60.0):
            await manager.refresh_state()
        if runtime.async_set_underlying_fan_mode.await_count > commands:
            commands = runtime.async_set_underlying_fan_mode.await_count
            new_fan = runtime.async_set_underlying_fan_mode.await_args.args[0]
            hass.states.get(runtime.entity_id).attributes["fan_mode"] = new_fan
            pending.append((minute + config.actuator_delay, new_fan))
    return manager, commands, errors


async def test_exploration_measures_every_speed_with_the_manager_in_the_loop() -> None:
    """Starting with only high and superhigh known, every speed is measured within 72 h.

    This is the loop the old controller could not leave: weak speeds were only
    visited in overshoot, a single sensor step aborted the learning hold, and a
    distinct reading was required per sample. Probes, time-based sampling and
    the resolution-aware escalation together close it, without giving up
    comfort or stability.
    """
    manager, commands, errors = await _run_manager_loop(72, seed=0, known=("high", "superhigh"))

    learning = manager.learning
    assert all(learning.has_measured_profile(fan, "cool") for fan in FAN_MODES), {fan: learning.get_mode_measured_minutes(fan, "cool") for fan in FAN_MODES}
    assert learning.probe_count >= 1
    assert commands / 72 < 0.5
    # Looser than the 0.25 of a mature installation: this includes the learning.
    assert sum(abs(e) for e in errors) / len(errors) < 0.3
