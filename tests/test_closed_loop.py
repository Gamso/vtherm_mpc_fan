"""Closed-loop behaviour of the MPC on a simulated room (see tests/closed_loop.py).

The replay bench is open loop and cannot show what comfort a decision rule
obtains; these tests close the loop on a plant with a first-order inverter, an
actuator delay, a 0.2 degC sensor publishing on change (one reading every ~18
min while the room is steady) and VTherm's EMA slope.
"""

import pytest

from custom_components.vtherm_mpc_fan.mpc_controller import MPCController
from custom_components.vtherm_mpc_fan.thermal_learning import ThermalLearning

from closed_loop import FAN_MODES, PlantConfig, run_closed_loop, seed_true_profiles

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
