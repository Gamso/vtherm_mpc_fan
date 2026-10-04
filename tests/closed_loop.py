"""Closed-loop test bench: a room, an inverter air conditioner and a coarse sensor.

The replay bench (scripts/replay_bench.py) is open loop: the recorded
temperature does not react to the simulated decisions, so it says nothing about
the comfort a decision rule actually obtains. This plant does react. It is
deliberately simple, but it carries the three features that shaped the
controller's problems on real hardware:

- an inverter compressor: the cooling a fan speed delivers grows with the
  comfort error (``inverter_gain``) and shrinks below the setpoint, which gives
  each speed an equilibrium temperature;
- delays: the fan command reaches the unit ``actuator_delay`` minutes late and
  the delivered power follows it with a first-order lag (``power_tau``);
- a coarse sensor: the room temperature is published quantised to
  ``sensor_step`` (0.2 degC), only when the quantised value changes -- slow
  drifts give one publication every ~18 minutes, as on the production trace --
  and VTherm's slope is an EMA updated on each publication only.

Cooling only: heating is the mirror image and adds nothing here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import random

from custom_components.vtherm_mpc_fan.mpc_controller import MPCController
from custom_components.vtherm_mpc_fan.thermal_learning import ThermalLearning

FAN_MODES = ("silent", "low", "med", "high", "superhigh")
#: Cooling (degC/h) each speed delivers at zero comfort error.
CAPACITY = {"silent": 0.2, "low": 0.35, "med": 0.55, "high": 0.75, "superhigh": 0.95}


@dataclass
class PlantConfig:
    """Physical parameters of the simulated room and unit."""

    setpoint: float = 24.0
    load_mean: float = 0.5  # degC/h of heat gain
    load_swing: float = 0.15  # daily sinusoidal swing of the load
    load_noise: float = 0.05
    inverter_gain: float = 0.5  # extra capacity per degC of comfort error
    actuator_delay: float = 5.0  # minutes
    power_tau: float = 8.0  # minutes
    sensor_step: float = 0.2
    slope_ema: float = 0.4  # weight of a new reading in VTherm's slope EMA
    cycle_minutes: int = 5
    start_temp: float = 24.6
    start_fan: str = "superhigh"


@dataclass
class LoopResult:
    """What one closed-loop run produced."""

    hours: float
    changes: int
    comfort_errors: list[float] = field(default_factory=list)
    prediction_errors_30m: list[float] = field(default_factory=list)
    persistence_errors_30m: list[float] = field(default_factory=list)
    fan_minutes: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def changes_per_hour(self) -> float:
        """Fan changes per hour of operation."""
        return self.changes / self.hours

    @property
    def comfort_mae(self) -> float:
        """Mean absolute comfort error read by the sensor (degC)."""
        return sum(abs(e) for e in self.comfort_errors) / len(self.comfort_errors)

    @property
    def prediction_mae_30m(self) -> float:
        """MAE of the MPC's 30-minute forecast against the sensor 30 minutes later."""
        return sum(self.prediction_errors_30m) / len(self.prediction_errors_30m)

    @property
    def persistence_mae_30m(self) -> float:
        """MAE of the persistence forecast (temperature in 30 min = temperature now)."""
        return sum(self.persistence_errors_30m) / len(self.persistence_errors_30m)


def plant_slope(fan_mode: str, error: float, load: float, config: PlantConfig) -> float:
    """Return the effective slope (degC/h toward the setpoint) a speed holds at *error*."""
    return CAPACITY[fan_mode] * max(0.2, 1.0 + config.inverter_gain * error) - load


def seed_true_profiles(learning: ThermalLearning, config: PlantConfig, *, fans=FAN_MODES) -> None:
    """Give the learner each speed's true profile, as a mature installation would have it."""
    for fan_mode in fans:
        for error in (0.0, 0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.0) * 2:
            effective = plant_slope(fan_mode, error, config.load_mean, config)
            learning.add_slope_sample(fan_mode, -effective, error, hvac_mode="cool", regulation_offset=0.0, dwell_minutes=10.0)
    for _ in range(6):
        learning.add_response_event(10.0, "cool")


def run_closed_loop(mpc: MPCController, hours: float, *, seed: int = 0, config: PlantConfig | None = None) -> LoopResult:
    """Drive *mpc* against the plant for *hours* and return what happened."""
    config = config or PlantConfig()
    rng = random.Random(seed)
    temp = config.start_temp
    fan_cmd = config.start_fan
    fan_effective = fan_cmd
    pending: list[tuple[float, str]] = []
    power = CAPACITY[fan_effective]
    published = round(temp / config.sensor_step) * config.sensor_step
    last_publish_time = 0.0
    slope_ema = 0.0
    last_change = -1e6
    minute = 0.0
    result = LoopResult(hours=hours, changes=0, fan_minutes={fan: 0.0 for fan in FAN_MODES})
    forecasts: list[tuple[float, float, float]] = []  # (due minute, forecast, reading when made)

    for cycle in range(int(hours * 60 / config.cycle_minutes)):
        del cycle
        for _ in range(config.cycle_minutes):
            while pending and pending[0][0] <= minute:
                fan_effective = pending.pop(0)[1]
            load = config.load_mean + config.load_swing * math.sin(2 * math.pi * minute / 1440.0) + rng.gauss(0.0, config.load_noise)
            error = temp - config.setpoint
            target_power = CAPACITY[fan_effective] * max(0.2, 1.0 + config.inverter_gain * error)
            power += (target_power - power) / config.power_tau
            temp += (load - power) / 60.0
            minute += 1.0
            result.fan_minutes[fan_effective] += 1.0
            quantised = round(temp / config.sensor_step) * config.sensor_step
            if abs(quantised - published) > 1e-9:
                raw = (quantised - published) / ((minute - last_publish_time) / 60.0)
                slope_ema = (1 - config.slope_ema) * slope_ema + config.slope_ema * raw
                published = quantised
                last_publish_time = minute

        while forecasts and forecasts[0][0] <= minute:
            _, forecast, reading = forecasts.pop(0)
            result.prediction_errors_30m.append(abs(forecast - published))
            result.persistence_errors_30m.append(abs(reading - published))

        result.comfort_errors.append(published - config.setpoint)
        decision = mpc.evaluate(
            current_temp=published,
            target_temp=config.setpoint,
            user_target_temp=config.setpoint,
            vtherm_slope=slope_ema,
            hvac_mode="cool",
            current_fan=fan_cmd,
            minutes_since_change=minute - last_change,
        )
        if decision.get("mpc_predicted_temperature_30m") is not None:
            forecasts.append((minute + 30.0, decision["mpc_predicted_temperature_30m"], published))
        if decision["mpc_would_change_now"] == "yes" and decision["mpc_fan_mode"] != fan_cmd:
            fan_cmd = decision["mpc_fan_mode"]
            pending.append((minute + config.actuator_delay, fan_cmd))
            last_change = minute
            mpc.notify_fan_change()
            result.changes += 1
            result.reasons.append(decision["mpc_reason"])
    return result
