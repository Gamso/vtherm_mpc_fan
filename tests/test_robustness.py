"""Decision invariants of the MPC on random learned ladders, in heat and in cool.

The scenario tests pin one ladder each; production ladders are noisy fits whose
lines cross (2026-11 incident: superhigh 0.19 + 0.68·e under high's flat 0.49
near the setpoint, R² 0.05). These tests draw plausible ladders -- increasing at
the reference error, gains noisy and crossing, some weak speeds unlearned,
error envelopes of 0.6 to 2.5 degC -- and check rules no ladder may break:

- a heat decision and its mirrored cool decision are the same;
- past the band on the comfortable side, the room still moving away: never a
  stronger speed than the current one (the incident: 3 % of these cases on the
  controller before the ladder envelope);
- a room further past the setpoint never gets a stronger speed;
- short of the band and losing ground: never a weaker speed.

Seeds are fixed: a failure lists the cases that broke the rule, reproducible
with ``_case(seed)``.
"""

from __future__ import annotations

from dataclasses import dataclass
import random

import pytest

from custom_components.vtherm_mpc_fan.mpc_controller import MPCController
from custom_components.vtherm_mpc_fan.thermal_learning import ProfileFit, ThermalLearning

FAN_MODES = ["silent", "low", "med", "high", "superhigh"]
CASES = 400
SETPOINT = {"heat": 20.0, "cool": 24.0}


@dataclass(frozen=True)
class Case:
    """One random ladder and controller context."""

    seed: int
    fits: dict[str, ProfileFit]
    current_fan: str
    minutes_since_change: float
    dead_time: float


def _case(seed: int) -> Case:
    """Draw a plausible learned ladder and context from *seed*.

    Values at the reference error (1 degC) increase with the rank, as the
    monotone constraint would see them; gains are noisy, so the lines cross
    near the setpoint. A weak speed is sometimes unlearned.
    """
    rng = random.Random(seed)
    reference = sorted(rng.uniform(-0.2, 1.2) for _ in FAN_MODES)
    fits = {}
    for fan_mode, value in zip(FAN_MODES, reference):
        if fan_mode not in ("high", "superhigh") and rng.random() < 0.15:
            continue
        gain = max(0.0, rng.gauss(0.3, 0.3))
        fits[fan_mode] = ProfileFit(
            value - gain,
            gain,
            rng.uniform(0.0, 0.6),
            effective_samples=rng.uniform(8.0, 80.0),
            error_max=rng.choice([0.6, 1.2, 2.5]),
        )
    return Case(
        seed=seed,
        fits=fits,
        current_fan=rng.choice(list(fits)),
        minutes_since_change=rng.choice([40.0, 120.0, 1e6]),
        dead_time=rng.choice([6.0, 12.0, 20.0]),
    )


def _decide(case: Case, hvac_mode: str, past: float, slope: float) -> tuple[str, str]:
    """Return (fan mode, reason) for *case* with the room *past* degC beyond the setpoint.

    *past* > 0 is the comfortable side (warmer than the setpoint in heat,
    colder in cool), *past* < 0 the short side. *slope* > 0 means the room is
    moving further past (away from the setpoint when *past* > 0).
    """
    learning = ThermalLearning()
    learning._fit_mode_slope = lambda fan_mode, mode: case.fits.get(fan_mode) if mode == hvac_mode else None  # noqa: SLF001
    learning.has_measured_profile = lambda fan_mode, mode: mode == hvac_mode and fan_mode in case.fits
    learning.add_response_event(case.dead_time, hvac_mode)
    mpc = MPCController(learning=learning, deadband=0.2, min_interval=10, fan_modes=FAN_MODES)
    sign = 1.0 if hvac_mode == "heat" else -1.0
    room = round(SETPOINT[hvac_mode] + sign * past, 1)
    mpc._observe_reading(round(room - sign * 0.2, 1))  # noqa: SLF001
    decision = mpc.evaluate(
        current_temp=room,
        target_temp=room,
        user_target_temp=SETPOINT[hvac_mode],
        vtherm_slope=sign * slope,
        hvac_mode=hvac_mode,
        current_fan=case.current_fan,
        minutes_since_change=case.minutes_since_change,
    )
    return decision["mpc_fan_mode"], decision["mpc_reason"]


def _rank(fan_mode: str) -> int:
    """Rank of *fan_mode* in the ladder, weakest first."""
    return FAN_MODES.index(fan_mode)


def _report(violations: list[str]) -> str:
    """Format the first violations for an assertion message."""
    return f"{len(violations)} / {CASES} cases:\n" + "\n".join(violations[:8])


def test_heat_and_mirrored_cool_decide_the_same() -> None:
    """The error and the slopes are sign-aligned: heat and mirrored cool must not differ."""
    violations = []
    for seed in range(CASES):
        case = _case(seed)
        state = random.Random(-seed)
        past = state.choice([-1.0, -0.5, -0.3, 0.0, 0.3, 0.5, 0.8])
        slope = state.choice([-0.5, 0.0, 0.2, 0.5, 0.9])
        heat = _decide(case, "heat", past, slope)
        cool = _decide(case, "cool", past, slope)
        if heat[0] != cool[0]:
            violations.append(f"seed {seed} {case.current_fan} past {past} slope {slope}: heat {heat[0]} / cool {cool[0]} -- {heat[1][:120]}")
    assert not violations, _report(violations)


@pytest.mark.parametrize("hvac_mode", ["heat", "cool"])
def test_never_climbs_past_the_band_while_the_room_moves_away(hvac_mode: str) -> None:
    """Past the band on the comfortable side and still drifting away, a stronger speed is never right."""
    violations = []
    for seed in range(CASES):
        case = _case(seed)
        state = random.Random(-seed)
        past = state.choice([0.3, 0.4, 0.6, 0.8])
        slope = state.choice([0.2, 0.5, 0.9])
        fan_mode, reason = _decide(case, hvac_mode, past, slope)
        if _rank(fan_mode) > _rank(case.current_fan):
            violations.append(f"seed {seed} past {past} slope {slope}: {case.current_fan} -> {fan_mode} -- {reason[:120]}")
    assert not violations, _report(violations)


@pytest.mark.parametrize("hvac_mode", ["heat", "cool"])
def test_a_room_further_past_never_gets_a_stronger_speed(hvac_mode: str) -> None:
    """Everything else equal, 0.4 degC further past the setpoint never asks for more."""
    violations = []
    for seed in range(CASES):
        case = _case(seed)
        state = random.Random(-seed)
        past = state.choice([0.3, 0.4, 0.6])
        slope = state.choice([0.0, 0.2, 0.5])
        near, _ = _decide(case, hvac_mode, past, slope)
        far, reason = _decide(case, hvac_mode, past + 0.4, slope)
        if _rank(far) > _rank(near):
            violations.append(f"seed {seed} {case.current_fan} past {past} slope {slope}: {near} -> {far} at +0.4 -- {reason[:120]}")
    assert not violations, _report(violations)


@pytest.mark.parametrize("hvac_mode", ["heat", "cool"])
def test_never_drops_short_of_the_band_while_losing_ground(hvac_mode: str) -> None:
    """Short of the band and losing ground, a weaker speed is never right."""
    violations = []
    for seed in range(CASES):
        case = _case(seed)
        state = random.Random(-seed)
        short = state.choice([0.4, 0.6, 1.0])
        slope = state.choice([0.2, 0.5])
        fan_mode, reason = _decide(case, hvac_mode, -short, slope)
        if _rank(fan_mode) < _rank(case.current_fan):
            violations.append(f"seed {seed} short {short} losing {slope}: {case.current_fan} -> {fan_mode} -- {reason[:120]}")
    assert not violations, _report(violations)
