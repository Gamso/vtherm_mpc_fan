"""Closed-loop test bench: a room, an inverter air conditioner, VTherm and a coarse sensor.

The replay bench (scripts/replay_bench.py) is open loop: the recorded
temperature does not react to the simulated decisions, so it says nothing about
the comfort a decision rule actually obtains. This plant does react, and is
shaped on the production trace (cooling, 20 Aug - 4 Oct 2026):

- schedule: the unit cools from 10:00 to midnight; the user's setpoint is
  24 degC, and 22 degC from 21:30. While the unit is off the room relaxes
  toward a 26 degC night equilibrium (Newton, 6 h time constant);
- VTherm: its auto-regulation integrates the comfort error into an offset
  (regulated minus user setpoint, down to -0.8 degC; about -0.6 when the
  demand is high) and the *unit* regulates on the regulated setpoint;
- an inverter compressor: the cooling a fan speed delivers grows with the
  unit's own error (room minus regulated setpoint) and shrinks below it;
- delays: the fan command reaches the room ``actuator_delay`` minutes late
  (~10 min) and the delivered power follows with a first-order lag;
- a coarse sensor: the room temperature is published quantised to
  ``sensor_step`` (0.2 degC), only when the quantised value changes (one
  publication every ~18 min while the room drifts slowly), and VTherm's slope
  is an EMA updated on each publication only.

Comfort is measured on the TRUE room temperature against the USER setpoint,
over the minutes the unit cools. ``run_closed_loop`` also drives the 8d7bccd
controller (no ``user_target_temp``: it regulated on the regulated setpoint),
which is how the baseline references of tests/test_closed_loop.py were measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import math
import random
import statistics

from custom_components.vtherm_mpc_fan.thermal_learning import ThermalLearning

FAN_MODES = ("silent", "low", "med", "high", "superhigh")
#: Cooling (degC/h) each speed delivers at zero unit error.
CAPACITY = {"silent": 0.2, "low": 0.35, "med": 0.55, "high": 0.75, "superhigh": 0.95}
#: Regulation offset the seeded profiles were "measured" at.
TYPICAL_OFFSET = -0.3

DAY_START = 10 * 60  # unit switched on (minute of the day)
EVENING = 21 * 60 + 30  # setpoint lowered to 22 degC


@dataclass
class PlantConfig:
    """Physical parameters of the simulated room, unit and VTherm."""

    load: float = 0.5  # degC/h of heat gain
    load_swing: float = 0.15  # daily sinusoidal swing of the load
    load_noise: float = 0.05
    inverter_gain: float = 0.5  # extra capacity per degC of unit error
    actuator_delay: float = 10.0  # minutes
    power_tau: float = 6.0  # minutes
    sensor_step: float = 0.2
    slope_ema: float = 0.4  # weight of a new reading in VTherm's slope EMA
    regulation_gain: float = 0.6  # degC of offset per degC.h of comfort error
    regulation_min: float = -0.8
    cycle_minutes: int = 5
    start_temp: float = 25.0
    start_fan: str = "superhigh"
    known: tuple[str, ...] = FAN_MODES  # speeds with a profile at the start


VARIANTS = {
    "base": PlantConfig(),
    "load0.3": PlantConfig(load=0.3),
    "load0.7": PlantConfig(load=0.7),
    "load0.85": PlantConfig(load=0.85, load_swing=0.1),
    "gain0.2": PlantConfig(inverter_gain=0.2),
    "slowdelay": PlantConfig(actuator_delay=15.0, power_tau=12.0),
    "partial": PlantConfig(known=("med", "high", "superhigh")),
}


@dataclass
class LoopResult:
    """What one closed-loop run produced."""

    hours: float
    changes: int = 0
    cooling_minutes: int = 0
    errors: list[float] = field(default_factory=list)  # true room - user setpoint, per cooling minute
    descents: list[float] = field(default_factory=list)  # minutes from 21:30 to the room at 22.2 degC
    fan_minutes: dict[str, float] = field(default_factory=lambda: {fan: 0.0 for fan in FAN_MODES})
    prediction_errors_30m: list[float] = field(default_factory=list)
    persistence_errors_30m: list[float] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def comfort_mae(self) -> float:
        """Mean absolute error of the true room temperature against the user's setpoint."""
        return statistics.mean(abs(e) for e in self.errors)

    def _share(self, predicate) -> float:
        """Percentage of the cooling minutes whose error satisfies *predicate*."""
        return 100.0 * sum(1 for e in self.errors if predicate(e)) / len(self.errors)

    @property
    def in_band_pct(self) -> float:
        """Share of the cooling time within +/-0.2 degC of the user's setpoint."""
        return self._share(lambda e: abs(e) <= 0.2)

    @property
    def too_cold_pct(self) -> float:
        """Share of the cooling time more than 0.2 degC below the user's setpoint."""
        return self._share(lambda e: e < -0.2)

    @property
    def too_warm_pct(self) -> float:
        """Share of the cooling time more than 0.2 degC above the user's setpoint."""
        return self._share(lambda e: e > 0.2)

    @property
    def descent_minutes(self) -> float:
        """Mean time from the 21:30 setpoint drop to the room at 22.2 degC (150 when missed)."""
        return statistics.mean(self.descents)

    @property
    def changes_per_hour(self) -> float:
        """Fan changes per hour of cooling."""
        return self.changes / (self.cooling_minutes / 60.0)

    @property
    def superhigh_pct(self) -> float:
        """Share of the cooling time spent on the strongest speed."""
        return 100.0 * self.fan_minutes["superhigh"] / max(self.cooling_minutes, 1)

    @property
    def prediction_mae_30m(self) -> float:
        """MAE of the MPC's 30-minute forecast against the reading 30 minutes later."""
        return statistics.mean(self.prediction_errors_30m)

    @property
    def persistence_mae_30m(self) -> float:
        """MAE of the persistence forecast (reading in 30 min = reading now)."""
        return statistics.mean(self.persistence_errors_30m)


def unit_slope(fan_mode: str, unit_error: float, load: float, config: PlantConfig) -> float:
    """Return the effective slope (degC/h toward the setpoint) a speed holds at a unit error."""
    return CAPACITY[fan_mode] * max(0.2, 1.0 + config.inverter_gain * unit_error) - load


def seed_true_profiles(learning: ThermalLearning, config: PlantConfig, *, fans: tuple[str, ...] | None = None) -> None:
    """Give the learner the true profiles of *fans*, as a mature installation would have them."""
    has_dwell = "dwell_minutes" in inspect.signature(learning.add_slope_sample).parameters
    for fan_mode in config.known if fans is None else fans:
        for error in (0.0, 0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.0) * 2:
            effective = unit_slope(fan_mode, error - TYPICAL_OFFSET, config.load, config)
            if has_dwell:
                learning.add_slope_sample(fan_mode, -effective, error, hvac_mode="cool", regulation_offset=TYPICAL_OFFSET, dwell_minutes=10.0)
            else:  # 8d7bccd: the error was taken against the regulated setpoint
                learning.add_slope_sample(fan_mode, -effective, error - TYPICAL_OFFSET, hvac_mode="cool")
    for _ in range(6):
        learning.add_response_event(12.0, "cool")


def schedule(minute_of_day: float) -> tuple[str, float]:
    """Return (hvac_mode, user setpoint) at a minute of the day."""
    if minute_of_day < DAY_START:
        return "off", 24.0
    if minute_of_day >= EVENING:
        return "cool", 22.0
    return "cool", 24.0


class Plant:
    """The room, the unit, the sensor and VTherm, advanced minute by minute."""

    def __init__(self, config: PlantConfig, seed: int) -> None:
        self.config = config
        self.rng = random.Random(seed)
        self.temp = config.start_temp
        self.fan = config.start_fan  # fan whose effect currently reaches the room
        self.pending: list[tuple[float, str]] = []
        self.power = 0.0
        self.published = round(self.temp / config.sensor_step) * config.sensor_step
        self.last_publish = 0.0
        self.slope = 0.0
        self.offset = 0.0
        self.minute = 0.0  # minute 0 = DAY_START of day 1

    def time_of_day(self) -> float:
        """Minute of the day of the current simulation minute."""
        return (DAY_START + self.minute) % 1440

    def command(self, fan_mode: str) -> None:
        """Send a fan command; it reaches the room after the actuator delay."""
        self.pending.append((self.minute + self.config.actuator_delay, fan_mode))

    def step(self) -> None:
        """Advance one minute."""
        config = self.config
        while self.pending and self.pending[0][0] <= self.minute:
            self.fan = self.pending.pop(0)[1]
        mode, user = schedule(self.time_of_day())
        if mode == "cool":
            load = config.load + config.load_swing * math.sin(2 * math.pi * (self.minute - 300) / 1440.0) + self.rng.gauss(0.0, config.load_noise)
            unit_error = self.temp - (user + self.offset)
            target_power = CAPACITY[self.fan] * max(0.2, 1.0 + config.inverter_gain * unit_error)
        else:
            load = (26.0 - self.temp) / 6.0  # relaxation toward the night equilibrium
            target_power = 0.0
        self.power += (target_power - self.power) / config.power_tau
        self.temp += (load - self.power) / 60.0
        self.minute += 1.0
        quantised = round(self.temp / config.sensor_step) * config.sensor_step
        if abs(quantised - self.published) > 1e-9:
            raw = (quantised - self.published) / ((self.minute - self.last_publish) / 60.0)
            self.slope = (1 - config.slope_ema) * self.slope + config.slope_ema * raw
            self.published, self.last_publish = quantised, self.minute

    def regulate(self) -> tuple[str, float, float]:
        """Run VTherm's regulation for this cycle; return (hvac_mode, user, regulated) setpoints."""
        mode, user = schedule(self.time_of_day())
        if mode == "cool":
            step = self.config.regulation_gain * (self.published - user) * self.config.cycle_minutes / 60.0
            self.offset = min(0.0, max(self.config.regulation_min, self.offset - step))
        else:
            self.offset = 0.0
        return mode, user, user + self.offset


def run_closed_loop(mpc, hours: float, *, seed: int = 0, config: PlantConfig | None = None) -> LoopResult:
    """Drive *mpc* (an MPCController) against the plant for *hours* and return what happened."""
    config = config or PlantConfig()
    plant = Plant(config, seed)
    new_api = "user_target_temp" in inspect.signature(mpc.evaluate).parameters
    fan_cmd = config.start_fan
    last_change = -1e6
    result = LoopResult(hours=hours)
    evening_start: float | None = None
    forecasts: list[tuple[float, float, float]] = []
    for _ in range(int(hours * 60 / config.cycle_minutes)):
        # VTherm runs its cycle first (regulation, then this plugin), then the
        # room evolves until the next cycle -- so a session's first cycle
        # happens the minute the unit is switched on.
        while forecasts and forecasts[0][0] <= plant.minute:
            _, forecast, reading = forecasts.pop(0)
            result.prediction_errors_30m.append(abs(forecast - plant.published))
            result.persistence_errors_30m.append(abs(reading - plant.published))
        mode, user, regulated = plant.regulate()
        call = {
            "current_temp": plant.published,
            "target_temp": regulated,
            "vtherm_slope": plant.slope,
            "hvac_mode": mode,
            "current_fan": fan_cmd,
            "minutes_since_change": plant.minute - last_change,
        }
        if new_api:
            call["user_target_temp"] = user
        decision = mpc.evaluate(**call)
        if mode == "cool":
            if decision.get("mpc_predicted_temperature_30m") is not None:
                forecasts.append((plant.minute + 30.0, decision["mpc_predicted_temperature_30m"], plant.published))
            if decision["mpc_would_change_now"] == "yes" and decision["mpc_fan_mode"] != fan_cmd:
                fan_cmd = decision["mpc_fan_mode"]
                plant.command(fan_cmd)
                last_change = plant.minute
                mpc.notify_fan_change()
                result.changes += 1
                result.reasons.append(decision["mpc_reason"])
        for _ in range(config.cycle_minutes):
            tod = plant.time_of_day()
            mode, user = schedule(tod)
            plant.step()
            if mode == "cool":
                result.errors.append(plant.temp - user)
                result.cooling_minutes += 1
                result.fan_minutes[plant.fan] += 1
                if tod == EVENING:
                    evening_start = plant.minute
                if evening_start is not None and plant.temp <= user + 0.2:
                    result.descents.append(plant.minute - evening_start)
                    evening_start = None
            elif evening_start is not None:
                result.descents.append(150.0)
                evening_start = None
    return result
