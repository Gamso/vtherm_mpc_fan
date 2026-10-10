"""Cost guards for the learned model's hot paths, on a full 5 000-sample store.

Both paths run on Home Assistant's event loop: recompute_slope_stats() at every
load and whenever the sliding window drops samples, the profile fits on every
control cycle (MPC candidates, number entities). The guards count work rather
than time it, so they are deterministic, and each one also checks that the
optimisation returns exactly what the unoptimised computation returns.
"""

import random
import time
from unittest.mock import patch

from custom_components.vtherm_mpc_fan.const import MIN_MODE_PROFILE_SAMPLES
from custom_components.vtherm_mpc_fan.thermal_learning import MAX_STORED_SLOPE_SAMPLES, ThermalLearning

FAN_MODES = ("silent", "low", "med", "high", "superhigh")
HVAC_MODES = ("heat", "cool")


def _full_store() -> ThermalLearning:
    """Return a model holding MAX_STORED_SLOPE_SAMPLES varied, in-window samples."""
    rng = random.Random(1)
    now = time.time()
    learning = ThermalLearning()
    learning.slope_samples = [
        (now - i * 60, rng.choice(FAN_MODES), rng.uniform(-1.0, 1.0), rng.choice(HVAC_MODES), rng.uniform(-0.5, 2.0)) for i in range(MAX_STORED_SLOPE_SAMPLES)
    ]
    return learning


def test_recompute_slope_stats_counts_profiles_in_one_pass() -> None:
    """P-1: no per-sample rescan of the whole list.

    The ready-profile set used to call get_mode_sample_count() -- a full scan --
    once per sample: 25 million tuple visits for a 5 000-sample store, about
    half a second on the event loop at every load.
    """
    learning = _full_store()

    with patch.object(ThermalLearning, "get_mode_sample_count", wraps=learning.get_mode_sample_count) as counter:
        learning.recompute_slope_stats()

    assert counter.call_count == 0
    # Reference: the old definition, asked once per distinct profile instead of once per sample.
    profiles = {(s[3], s[1]) for s in learning.slope_samples}
    expected = {(hvac, fan) for hvac, fan in profiles if hvac != "unknown" and learning.get_mode_sample_count(fan, hvac) >= MIN_MODE_PROFILE_SAMPLES}
    assert learning._profile_ready_logged == expected  # noqa: SLF001
    assert learning.slope_count == MAX_STORED_SLOPE_SAMPLES


def test_profile_fits_are_computed_once_per_sample_change() -> None:
    """P-2: repeated reads within a cycle reuse the fit; a new sample refreshes it."""
    learning = _full_store()
    calls = []
    original = ThermalLearning._compute_mode_fit  # noqa: SLF001

    def _counting(self, fan_mode, hvac_mode):
        """Record the call, then fit as usual."""
        calls.append((fan_mode, hvac_mode))
        return original(self, fan_mode, hvac_mode)

    with patch.object(ThermalLearning, "_compute_mode_fit", _counting):
        for _ in range(5):  # five reads per profile, as the number entities do
            for hvac_mode in HVAC_MODES:
                for fan_mode in FAN_MODES:
                    learning.get_mode_effective_slope(fan_mode, hvac_mode)
                    learning.get_mode_slope_gain(fan_mode, hvac_mode)
                    learning.get_mode_slope_r2(fan_mode, hvac_mode)
        assert len(calls) == len(FAN_MODES) * len(HVAC_MODES)

        learning.add_slope_sample("low", 0.4, 0.5, "heat")
        refreshed = learning.get_mode_effective_slope("low", "heat")
        assert calls[-1] == ("low", "heat")  # the new sample forced a refit
        settled = len(calls)
        for _ in range(5):
            assert learning.get_mode_effective_slope("low", "heat") == refreshed
        assert len(calls) == settled


def test_cached_fits_equal_fresh_fits() -> None:
    """The cache changes the cost, never the number."""
    learning = _full_store()
    for hvac_mode in HVAC_MODES:
        for fan_mode in FAN_MODES:
            cached = learning._fit_mode_slope(fan_mode, hvac_mode)  # noqa: SLF001
            assert learning._fit_mode_slope(fan_mode, hvac_mode) is cached  # noqa: SLF001
            assert cached == learning._compute_mode_fit(fan_mode, hvac_mode)  # noqa: SLF001


def test_every_sample_mutation_invalidates_the_fits() -> None:
    """No path that changes the samples may leave a stale fit behind."""
    learning = _full_store()

    def _warm():
        """Fill the cache for one profile and return its fit."""
        return learning._fit_mode_slope("low", "heat")  # noqa: SLF001

    before = _warm()
    learning.set_mode_effective_slope("low", "heat", 0.9)
    assert _warm() != before and _warm().intercept == 0.9

    learning.slope_samples = _full_store().slope_samples
    assert _warm() == learning._compute_mode_fit("low", "heat")  # noqa: SLF001

    learning.reset()
    assert _warm() is None

    restored = ThermalLearning.from_dict(_full_store().to_dict())
    assert restored._fit_mode_slope("low", "heat") == restored._compute_mode_fit("low", "heat")  # noqa: SLF001
