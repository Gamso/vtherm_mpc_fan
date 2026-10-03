"""Tests for replay bench snapshot replay helpers."""

import importlib.util
from pathlib import Path
import sys

from custom_components.vtherm_mpc_fan.thermal_learning import ThermalLearning

_SPEC = importlib.util.spec_from_file_location(
    "replay_bench",
    Path(__file__).resolve().parent.parent / "scripts" / "replay_bench.py",
)
assert _SPEC is not None and _SPEC.loader is not None
replay_bench = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = replay_bench
_SPEC.loader.exec_module(replay_bench)


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
