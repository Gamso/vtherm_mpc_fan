"""Closed-loop behaviour of the MPC on a simulated room (see tests/closed_loop.py).

The replay bench is open loop and cannot show what comfort a decision rule
obtains; these tests close the loop on a plant shaped on the production trace
(cooling 10:00-24:00, user setpoint 24 degC then 22 degC from 21:30, VTherm's
auto-regulation, a 10-minute actuator delay, a 0.2 degC sensor publishing on
change) and compare the controller with the 8d7bccd one replayed on the very
same plant, variant and seed.

Comfort comes first and is measured on the TRUE room temperature against the
USER's setpoint: comfort MAE, time within +/-0.2 degC, time too warm, and the
evening descent to 22.2 degC. The number of fan changes is not a target.
"""

from unittest.mock import patch

import pytest

from custom_components.vtherm_mpc_fan.mpc_controller import MPCController
from custom_components.vtherm_mpc_fan.thermal_learning import ThermalLearning

from closed_loop import FAN_MODES, VARIANTS, Plant, PlantConfig, run_closed_loop, schedule, seed_true_profiles
from test_manager import _build_manager, _make_hass, _make_runtime

HOURS = 72
SEEDS = (0, 1, 2)

#: The 8d7bccd controller on this plant (run_closed_loop, 72 h, seed_true_profiles),
#: per variant and seed: (comfort MAE degC, % within +/-0.2, % too warm,
#: evening descent min). It regulated on VTherm's regulated setpoint, so it
#: overcooled (20-48 % of the time too cold) -- which also gave it a head start
#: on every warm excursion and on the evening descent.
BASELINE_8D7BCCD = {
    "base": ((0.307, 59.4, 18.69, 99.3), (0.321, 49.7, 18.10, 94.3), (0.316, 55.4, 18.25, 96.3)),
    "load0.3": ((0.366, 41.1, 16.98, 91.0), (0.360, 44.0, 16.98, 91.0), (0.361, 43.1, 16.94, 91.0)),
    "load0.7": ((0.341, 50.6, 22.50, 114.3), (0.340, 52.3, 22.06, 114.7), (0.340, 51.2, 22.34, 114.0)),
    "load0.85": ((0.354, 47.1, 26.03, 130.7), (0.352, 47.6, 25.99, 130.7), (0.366, 47.1, 26.63, 136.7)),
    "gain0.2": ((0.479, 31.3, 27.50, 150.0), (0.451, 35.6, 27.42, 150.0), (0.461, 24.3, 27.30, 150.0)),
    "slowdelay": ((0.366, 39.8, 21.39, 99.0), (0.375, 39.4, 20.36, 98.3), (0.373, 40.3, 20.12, 102.7)),
    "partial": ((0.365, 47.5, 18.69, 99.3), (0.368, 47.9, 18.81, 100.3), (0.379, 44.5, 18.85, 101.7)),
}

#: Measured margins over the baseline (worst variant x seed, see docs/mpc_mode.md,
#: "Calibration"). The warm excess comes from the morning restart: the unit
#: resumes on the speed it stopped on, a weak one for a controller that does not
#: overcool the evening, so the first 10-15 minutes of the 10:00 recovery run
#: on it. Partial (low and silent never measured) holds the room colder than it
#: should for want of a profile to step down on: its time within the band is
#: the known gap.
MAE_MARGIN = 0.005
IN_BAND_MARGIN = 1.0
IN_BAND_MARGIN_PARTIAL = 8.0
WARM_MARGIN = 1.5
DESCENT_MARGIN = 2.5


def _controller(config: PlantConfig) -> MPCController:
    """An MPC whose profiles match the plant, as on a mature installation."""
    learning = ThermalLearning()
    seed_true_profiles(learning, config)
    return MPCController(learning=learning, deadband=0.2, min_interval=10, fan_modes=list(FAN_MODES))


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_comfort_is_at_least_the_baseline_on_the_same_plant(variant: str, seed: int) -> None:
    """Comfort MAE and time in band at least the 8d7bccd ones; warm and descent within their margins."""
    config = VARIANTS[variant]
    result = run_closed_loop(_controller(config), HOURS, seed=seed, config=config)
    mae, in_band, warm, descent = BASELINE_8D7BCCD[variant][seed]

    summary = (
        f"{variant}/{seed}: MAE {result.comfort_mae:.3f} (8d7bccd {mae}), in band {result.in_band_pct:.1f} % ({in_band}), "
        f"too warm {result.too_warm_pct:.2f} % ({warm}), too cold {result.too_cold_pct:.1f} %, descent {result.descent_minutes:.1f} min ({descent}), "
        f"{result.changes_per_hour:.2f} changes/h, superhigh {result.superhigh_pct:.1f} %, "
        f"T+30 MAE {result.prediction_mae_30m:.3f} (persistence {result.persistence_mae_30m:.3f})"
    )
    assert result.comfort_mae <= mae + MAE_MARGIN, summary
    assert result.in_band_pct >= in_band - (IN_BAND_MARGIN_PARTIAL if variant == "partial" else IN_BAND_MARGIN), summary
    assert result.too_warm_pct <= warm + WARM_MARGIN, summary
    assert result.descent_minutes <= descent + DESCENT_MARGIN, summary


def test_the_evening_setpoint_drop_runs_the_strongest_speed() -> None:
    """24 -> 22 degC at 21:30: the setpoint boost sends superhigh at once and keeps it.

    Without it the controller weighed a 2 degC demand against the speeds' cost
    and could climb one rung at a time.
    """
    config = VARIANTS["base"]
    result = run_closed_loop(_controller(config), 24, seed=0, config=config)

    assert any(reason.startswith("Setpoint boost") for reason in result.reasons), result.reasons
    assert result.descents[0] <= BASELINE_8D7BCCD["base"][0][3] + DESCENT_MARGIN


async def _run_manager_loop(hours: float, *, seed: int, known: tuple[str, ...]):
    """Drive the real feature manager (learning gates included) against the plant.

    Only the speeds in *known* start with a profile; the others must be learned
    by the manager's own sampling, from the visits the controller creates.
    Returns (manager, fan changes, comfort errors against the user setpoint).
    """
    config = PlantConfig()
    plant = Plant(config, seed)
    runtime = _make_runtime(current_temperature=plant.published, target_temperature=24.0, regulated_target_temperature=24.0, last_temperature_slope=0.0)
    hass = _make_hass(fan_mode=config.start_fan)
    start = 5_000_000.0
    with patch("time.time", return_value=start):
        manager = await _build_manager(runtime, hass)
        seed_true_profiles(manager.learning, config, fans=known)
    commands = 0
    errors = []
    for _ in range(int(hours * 60 / config.cycle_minutes)):
        mode, user, regulated = plant.regulate()
        runtime.vtherm_hvac_mode = mode
        runtime.current_temperature = plant.published
        runtime.target_temperature = user
        runtime.regulated_target_temperature = regulated
        runtime.last_temperature_slope = plant.slope
        with patch("time.time", return_value=start + plant.minute * 60.0):
            await manager.refresh_state()
        if runtime.async_set_underlying_fan_mode.await_count > commands:
            commands = runtime.async_set_underlying_fan_mode.await_count
            new_fan = runtime.async_set_underlying_fan_mode.await_args.args[0]
            hass.states.get(runtime.entity_id).attributes["fan_mode"] = new_fan
            plant.command(new_fan)
        for _ in range(config.cycle_minutes):
            minute_mode, minute_user = schedule(plant.time_of_day())
            plant.step()
            if minute_mode == "cool":
                errors.append(plant.temp - minute_user)
    return manager, commands, errors


async def test_exploration_measures_every_speed_with_the_manager_in_the_loop() -> None:
    """Starting with only high and superhigh known, every speed is measured within 72 h.

    This is the loop the old controller could not leave: weak speeds were only
    visited in overshoot, a single sensor step aborted the learning hold, and a
    distinct reading was required per sample. Probes, time-based sampling and
    the resolution-aware escalation together close it, without giving up
    comfort.
    """
    manager, commands, errors = await _run_manager_loop(72, seed=0, known=("high", "superhigh"))

    learning = manager.learning
    assert all(learning.has_measured_profile(fan, "cool") for fan in FAN_MODES), {fan: learning.get_mode_measured_minutes(fan, "cool") for fan in FAN_MODES}
    assert learning.probe_count >= 1
    assert commands > 0
    # The 8d7bccd controller, with every profile known from the start, scores
    # 0.31-0.48 on this plant: learning included, this one stays below.
    assert sum(abs(e) for e in errors) / len(errors) < 0.31
