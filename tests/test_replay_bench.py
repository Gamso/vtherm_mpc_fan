"""Tests for replay bench snapshot replay helpers."""

import importlib.util
from pathlib import Path
import sys

import pytest

from custom_components.vtherm_mpc_fan.thermal_learning import ThermalLearning

_SPEC = importlib.util.spec_from_file_location(
    "replay_bench",
    Path(__file__).resolve().parent.parent / "scripts" / "replay_bench.py",
)
assert _SPEC is not None and _SPEC.loader is not None
replay_bench = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = replay_bench
_SPEC.loader.exec_module(replay_bench)


def _row(timestamp: str, temp: float) -> "replay_bench.Row":
    """Build one replay row; only the timestamp and temperature matter here."""
    return replay_bench.Row(
        timestamp=timestamp,
        hvac_mode="cool",
        current_temp=temp,
        target_temp=24.0,
        vtherm_slope=0.0,
        current_fan="med",
        decided_fan="med",
        minutes_since_change=60.0,
        is_window_open=False,
        defrost_active=False,
        hvac_idle=False,
        mpc_fan="med",
        mpc_status="Ready",
        phase="ESTABLISHED",
        dead_time=10.0,
    )


def test_metrics_report_the_persistence_baseline_next_to_the_model() -> None:
    """A model MAE means nothing alone: persistence is computed on the same rows.

    The room cools by 0.1 degC per 5-minute row. A perfect model has zero error,
    persistence is off by the 10/30-minute change (0.2 / 0.6 degC). Lookaheads are
    resolved on timestamps, so the irregular gap at the end is not mistaken for
    a 10-minute horizon.
    """
    rows = [_row(f"2026-08-20T12:{5 * k:02d}:00Z", 25.0 - 0.1 * k) for k in range(10)]
    rows.append(_row("2026-08-20T14:00:00Z", 20.0))
    results = []
    for k in range(len(rows)):
        payload = {
            "mpc_status": "Ready",
            "mpc_predicted_temperature_10m": 25.0 - 0.1 * (k + 2),
            "mpc_predicted_temperature_30m": 25.0 - 0.1 * (k + 6),
        }
        results.append((payload, "med"))

    metrics = replay_bench.compute_metrics("baseline", {}, rows, results)

    assert len(metrics.prediction_errors_10m) == 8
    assert len(metrics.prediction_errors_30m) == 4
    assert replay_bench.mean_abs(metrics.prediction_errors_10m) == pytest.approx(0.0, abs=1e-9)
    assert replay_bench.mean_abs(metrics.persistence_errors_10m) == pytest.approx(0.2)
    assert replay_bench.mean_abs(metrics.persistence_errors_30m) == pytest.approx(0.6)


def test_lookahead_index_refuses_a_gap() -> None:
    """No row within the tolerance after the horizon means no comparison."""
    times = [0.0, 300.0, 600.0, 4000.0]

    assert replay_bench.lookahead_index(times, 0, 10) == 2
    assert replay_bench.lookahead_index(times, 1, 10) is None


def _write_snapshot_csv(path: Path, rows: list[tuple[str, str, str]]) -> None:
    """Write a small effective-slope snapshot CSV for tests."""
    content = ["entity_id,state,last_changed"]
    content.extend(f"{entity_id},{state},{changed_at}" for entity_id, state, changed_at in rows)
    path.write_text("\n".join(content) + "\n", encoding="utf-8")


def test_load_snapshot_profiles_returns_next_replay_index(tmp_path: Path) -> None:
    """Initial snapshot seeding should stop at the first event beyond the grace window."""
    _write_snapshot_csv(
        tmp_path / "superhigh_effective_slope.csv",
        [
            (
                "sensor.vtherm_mpc_fan_salon_heat_superhigh_effective_slope",
                "unknown",
                "2026-04-16T15:58:37.509Z",
            ),
            (
                "sensor.vtherm_mpc_fan_salon_heat_superhigh_effective_slope",
                "1.075",
                "2026-04-16T16:00:37.448Z",
            ),
            (
                "sensor.vtherm_mpc_fan_salon_heat_superhigh_effective_slope",
                "unknown",
                "2026-04-18T06:58:39.280Z",
            ),
        ],
    )

    events = replay_bench.load_snapshot_events(
        str(tmp_path),
        default_hvac_mode="heat",
        fan_modes=["superhigh"],
    )

    profiles, next_index = replay_bench.load_snapshot_profiles(
        events,
        trace_start=replay_bench.parse_timestamp("2026-04-16T16:00:00Z"),
    )

    assert profiles == {("superhigh", "heat"): 1.075}
    assert next_index == 2


def test_apply_snapshot_events_until_updates_and_clears_profile() -> None:
    """Dynamic snapshot replay should set a profile value and later clear it."""
    learning = ThermalLearning()
    events = [
        replay_bench.SnapshotEvent(
            timestamp=replay_bench.parse_timestamp("2026-04-16T16:00:37Z"),
            fan_mode="superhigh",
            hvac_mode="heat",
            slope=1.075,
        ),
        replay_bench.SnapshotEvent(
            timestamp=replay_bench.parse_timestamp("2026-04-18T06:58:39Z"),
            fan_mode="superhigh",
            hvac_mode="heat",
            slope=None,
        ),
    ]

    index = replay_bench.apply_snapshot_events_until(
        learning,
        events,
        0,
        replay_bench.parse_timestamp("2026-04-16T16:04:37Z"),
    )
    assert index == 1
    assert learning.get_mode_effective_slope("superhigh", "heat") == 1.075

    index = replay_bench.apply_snapshot_events_until(
        learning,
        events,
        index,
        replay_bench.parse_timestamp("2026-04-18T07:02:39Z"),
    )
    assert index == 2
    assert learning.get_mode_effective_slope("superhigh", "heat") is None
