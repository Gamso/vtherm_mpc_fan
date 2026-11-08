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

Robustness scenarios (shaped on the Apr-Nov 2026 production logs of the same
installation, heat and cool):

- ``hvac_mode="heat"``: the plant runs unchanged and the controller sees it
  mirrored about 22 degC (room 24.2 -> 19.8, setpoint 24 -> 20, the evening
  setpoint 22 -> 22 becomes a raise). The error and the slopes are
  sign-aligned, so a heat run must reproduce the cool run;
- ``defrost_period``: every so many minutes the unit stops heating for
  ``defrost_minutes`` and pulls the room back; the controller is told
  (``is_defrost_active``), as the manager does;
- ``unit="modulating"``: the unit holds its own setpoint with a compressor
  capped by the fan speed, as the logs show -- every speed that can carry the
  load holds the room at ~0 degC/h near the setpoint and the speeds only part
  far from it. Linear profile fits are then misspecified;
- ``slope_noise``: the seeded profile samples carry measurement noise, so the
  learner's fits come out with gains as noisy as production's (R² ~0.05);
- ``restarts``: Home Assistant restarts at these minutes -- a fresh controller
  on the same learning, VTherm's regulation offset back to 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import math
import random
import statistics
from typing import Any, Callable

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
    hvac_mode: str = "cool"  # "heat": the controller sees the plant mirrored about MIRROR
    unit: str = "capacity"  # "modulating": the unit holds its own setpoint, capped by the fan speed
    modulation_gain: float = 1.5  # degC/h of power per degC of unit error ("modulating")
    defrost_period: float = 0.0  # minutes between defrosts while running (0: none)
    defrost_minutes: float = 8.0
    defrost_power: float = -0.6  # degC/h the defrost pulls the room away from the setpoint
    slope_noise: float = 0.0  # degC/h of noise on each seeded profile sample
    regime_sampling: bool = False  # seed each speed only over the errors it is used at (REGIME_ERRORS)
    restarts: tuple[float, ...] = ()  # simulation minutes at which Home Assistant restarts


#: Heat runs show the controller the plant mirrored about this temperature.
MIRROR = 22.0
#: Compressor cap per speed of the "modulating" unit (degC/h): low and above carry the base load.
MODULATING_CAPACITY = {fan: 1.6 * capacity for fan, capacity in CAPACITY.items()}
#: Comfort errors each speed is measured at in production: the strong speeds far
#: from the setpoint, the weak ones near or past it. Fitted over these ranges
#: only, the lines extrapolate and cross (2026-11 incident).
REGIME_ERRORS = {
    "silent": (-0.4, -0.3, -0.2, -0.1, 0.0, 0.1),
    "low": (-0.2, -0.1, 0.0, 0.1, 0.2, 0.3),
    "med": (0.0, 0.1, 0.2, 0.3, 0.4, 0.6),
    "high": (0.2, 0.3, 0.4, 0.6, 0.8, 1.0),
    "superhigh": (0.6, 0.8, 1.0, 1.5, 2.0, 2.5),
}

VARIANTS = {
    "base": PlantConfig(),
    "load0.3": PlantConfig(load=0.3),
    "load0.7": PlantConfig(load=0.7),
    "load0.85": PlantConfig(load=0.85, load_swing=0.1),
    "gain0.2": PlantConfig(inverter_gain=0.2),
    "slowdelay": PlantConfig(actuator_delay=15.0, power_tau=12.0),
    "partial": PlantConfig(known=("med", "high", "superhigh")),
}

#: Robustness scenarios, each a deviation the production logs showed (see the module docstring).
SCENARIOS = {
    "heat": PlantConfig(hvac_mode="heat"),
    # Heat as logged: 5-20 min dead time, a defrost every ~1.5 h.
    "heat_defrost": PlantConfig(hvac_mode="heat", actuator_delay=6.0, power_tau=4.0, defrost_period=90.0),
    "modulating": PlantConfig(unit="modulating"),
    "modulating_heat": PlantConfig(unit="modulating", hvac_mode="heat"),
    "noisy": PlantConfig(slope_noise=0.4),
    "noisy_heat": PlantConfig(slope_noise=0.4, hvac_mode="heat"),
    "noisy_slowdelay": PlantConfig(slope_noise=0.4, actuator_delay=15.0, power_tau=12.0),
    # Each speed measured only where it is used, with noise: the production ladder.
    "regime": PlantConfig(regime_sampling=True, slope_noise=0.2),
    "regime_modulating": PlantConfig(regime_sampling=True, slope_noise=0.2, unit="modulating"),
    # Restarts at 14:00 on day 2 (room held) and 22:00 on day 3 (evening descent).
    "restart": PlantConfig(restarts=(1680.0, 3600.0)),
    "restart_heat": PlantConfig(restarts=(1680.0, 3600.0), hvac_mode="heat"),
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


def unit_power(fan_mode: str, unit_error: float, load: float, config: PlantConfig) -> float:
    """Return the cooling power (degC/h) the unit delivers on *fan_mode* at a unit error."""
    if config.unit == "modulating":
        # The unit's own regulation: enough to carry the load and close its
        # error, never more than the fan speed allows.
        return max(0.0, min(MODULATING_CAPACITY[fan_mode], load + config.modulation_gain * unit_error))
    return CAPACITY[fan_mode] * max(0.2, 1.0 + config.inverter_gain * unit_error)


def unit_slope(fan_mode: str, unit_error: float, load: float, config: PlantConfig) -> float:
    """Return the effective slope (degC/h toward the setpoint) a speed holds at a unit error."""
    return unit_power(fan_mode, unit_error, load, config) - load


def seed_true_profiles(learning: ThermalLearning, config: PlantConfig, *, fans: tuple[str, ...] | None = None, seed: int = 0) -> None:
    """Give the learner the true profiles of *fans*, as a mature installation would have them.

    With ``config.slope_noise`` each sample carries that much Gaussian noise
    (drawn from *seed*), so the fits are as uncertain as production's. In heat
    the samples are the mirrored ones: raw slope and regulation offset change
    sign, the comfort error does not.
    """
    has_dwell = "dwell_minutes" in inspect.signature(learning.add_slope_sample).parameters
    hvac_mode = config.hvac_mode
    sign = 1.0 if hvac_mode == "heat" else -1.0  # raw slope of a positive effective slope
    rng = random.Random(seed)
    for fan_mode in config.known if fans is None else fans:
        errors = REGIME_ERRORS[fan_mode] * 3 if config.regime_sampling else (0.0, 0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.0) * 2
        for error in errors:
            effective = unit_slope(fan_mode, error - TYPICAL_OFFSET, config.load, config) + rng.gauss(0.0, config.slope_noise)
            if has_dwell:
                learning.add_slope_sample(fan_mode, sign * effective, error, hvac_mode=hvac_mode, regulation_offset=-sign * TYPICAL_OFFSET, dwell_minutes=10.0)
            else:  # 8d7bccd: the error was taken against the regulated setpoint
                learning.add_slope_sample(fan_mode, sign * effective, error - TYPICAL_OFFSET, hvac_mode=hvac_mode)
    for _ in range(6):
        learning.add_response_event(12.0, hvac_mode)


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

    def defrost_active(self) -> bool:
        """Whether the unit is defrosting this minute (the last ``defrost_minutes`` of each period)."""
        period = self.config.defrost_period
        if period <= 0 or schedule(self.time_of_day())[0] != "cool":
            return False
        return self.minute % period >= period - self.config.defrost_minutes

    def restart(self) -> None:
        """Home Assistant restarts: VTherm's auto-regulation starts again from no offset."""
        self.offset = 0.0

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
            target_power = config.defrost_power if self.defrost_active() else unit_power(self.fan, unit_error, load, config)
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


def run_closed_loop(mpc, hours: float, *, seed: int = 0, config: PlantConfig | None = None, rebuild: Callable[[], Any] | None = None) -> LoopResult:
    """Drive *mpc* (an MPCController) against the plant for *hours* and return what happened.

    At each of ``config.restarts`` the controller is replaced by ``rebuild()``
    (a fresh controller on the same learning) and the time since the last fan
    change is forgotten, as the manager forgets it. In heat, the errors and
    the speeds are recorded in the plant's own (cool) frame: ``too_warm_pct``
    is then the time short of the setpoint, ``too_cold_pct`` the time past it.
    """
    config = config or PlantConfig()
    plant = Plant(config, seed)
    new_api = "user_target_temp" in inspect.signature(mpc.evaluate).parameters
    defrost_api = "is_defrost_active" in inspect.signature(mpc.evaluate).parameters
    heat = config.hvac_mode == "heat"

    def shown(temperature: float) -> float:
        """The temperature as the controller sees it: mirrored about MIRROR in heat."""
        return 2.0 * MIRROR - temperature if heat else temperature

    restarts = list(config.restarts)
    fan_cmd = config.start_fan
    last_change = -1e6
    result = LoopResult(hours=hours)
    evening_start: float | None = None
    forecasts: list[tuple[float, float, float]] = []
    for _ in range(int(hours * 60 / config.cycle_minutes)):
        if restarts and plant.minute >= restarts[0]:
            restarts.pop(0)
            plant.restart()
            mpc = rebuild()
            last_change = -1e6
        # VTherm runs its cycle first (regulation, then this plugin), then the
        # room evolves until the next cycle -- so a session's first cycle
        # happens the minute the unit is switched on.
        while forecasts and forecasts[0][0] <= plant.minute:
            _, forecast, reading = forecasts.pop(0)
            result.prediction_errors_30m.append(abs(forecast - plant.published))
            result.persistence_errors_30m.append(abs(reading - plant.published))
        mode, user, regulated = plant.regulate()
        call = {
            "current_temp": shown(plant.published),
            "target_temp": shown(regulated),
            "vtherm_slope": -plant.slope if heat else plant.slope,
            "hvac_mode": config.hvac_mode if mode == "cool" else mode,
            "current_fan": fan_cmd,
            "minutes_since_change": plant.minute - last_change,
        }
        if new_api:
            call["user_target_temp"] = shown(user)
        if defrost_api and plant.defrost_active():
            call["is_defrost_active"] = True
        decision = mpc.evaluate(**call)
        if mode == "cool":
            if decision.get("mpc_predicted_temperature_30m") is not None:
                forecasts.append((plant.minute + 30.0, shown(decision["mpc_predicted_temperature_30m"]), plant.published))
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
