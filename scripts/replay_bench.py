#!/usr/bin/env python3
"""Offline replay bench for VTherm MPC Fan MPC.

Replays data-collection CSV traces through the MPC controller with configurable
cost weights, producing comparison metrics across variants.

NOTE: This is an open-loop replay — real temperature data from the CSV is used
as-is, but the *simulated fan state* is tracked per variant.  Because the
thermal trajectory is fixed, results show "what would MPC have decided" rather
than "what would have happened".  This is valid for comparing cost-weight
sensitivity on the same trace.

Prediction quality is always reported against the *persistence* baseline
("the temperature in 10/30 minutes is the temperature now"). A room sensor that
moves in 0.2 degC steps every ~18 minutes makes persistence a strong forecaster,
so a model MAE is only informative next to it: on the 1 078 h production trace
the model's T+30 MAE was 0.243 degC against 0.251 for persistence.

Usage:
    python scripts/replay_bench.py data.csv
    python scripts/replay_bench.py data.csv --variant baseline --variant high_rank:MODE_RANK_COST=0.3
    python scripts/replay_bench.py data.csv \\
        --variant baseline \\
        --variant aggressive:COMFORT_ERROR_WEIGHT=2.0,OVERSHOOT_QUADRATIC_WEIGHT=5.0 \\
        --output replay_results.csv
"""

from __future__ import annotations

import argparse
import bisect
import csv
from datetime import datetime
from importlib import import_module
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Make the custom component importable
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "custom_components"))

_mpc_mod = import_module("vtherm_mpc_fan.mpc_controller")
_const_mod = import_module("vtherm_mpc_fan.const")
_thermal_mod = import_module("vtherm_mpc_fan.thermal_learning")

MPCController = _mpc_mod.MPCController
ThermalLearning = _thermal_mod.ThermalLearning
MIN_MODE_PROFILE_SAMPLES = _const_mod.MIN_MODE_PROFILE_SAMPLES

# Quiet the debug chatter during bulk replay
logging.getLogger("custom_components.vtherm_mpc_fan").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Tunable cost constants (module-level in mpc_controller.py)
# ---------------------------------------------------------------------------
TUNABLE = [
    "COMFORT_ERROR_WEIGHT",
    "OVERSHOOT_QUADRATIC_WEIGHT",
    "FLOOR_VIOLATION_LINEAR_WEIGHT",
    "FLOOR_VIOLATION_QUADRATIC_WEIGHT",
    "MODE_CHANGE_DISTANCE_COST",
    "MODE_RANK_COST",
    "MIN_INTERVAL_CHANGE_PENALTY",
    "URGENCY_SENSITIVITY",
    "HOLD_EQUILIBRIUM",
    "HOLD_UNDERSHOOT_TOLERANCE",
    "HOLD_RANK_SCALE",
]
_DEFAULTS = {k: getattr(_mpc_mod, k) for k in TUNABLE}
_PROFILE_ENTITY_RE = re.compile(r"_(heat|cool)_([^_]+)_effective_slope$")
_SNAPSHOT_START_GRACE_SECONDS = 120


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Row:
    """One parsed CSV row."""

    timestamp: str
    hvac_mode: str
    current_temp: float
    target_temp: float
    vtherm_slope: float
    current_fan: str
    decided_fan: str
    minutes_since_change: float
    is_window_open: bool
    defrost_active: bool
    hvac_idle: bool
    mpc_fan: str
    mpc_status: str
    phase: str
    dead_time: float
    user_target_temp: float | None = None
    regulation_offset: float | None = None


@dataclass
class Metrics:
    """Aggregate metrics for one replay variant."""

    name: str
    overrides: dict[str, float]
    total_rows: int = 0
    active_rows: int = 0
    fan_changes: int = 0
    fan_dist: Counter = field(default_factory=Counter)
    costs: list[float] = field(default_factory=list)
    agree_with_live: int = 0
    prediction_errors_10m: list[float] = field(default_factory=list)
    prediction_errors_30m: list[float] = field(default_factory=list)
    persistence_errors_10m: list[float] = field(default_factory=list)
    persistence_errors_30m: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class SnapshotEvent:
    """One effective-slope snapshot change."""

    timestamp: datetime
    fan_mode: str
    hvac_mode: str
    slope: float | None


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO timestamp with optional trailing Z."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def filter_rows_by_time(
    rows: list[Row],
    *,
    start: str | None,
    end: str | None,
) -> list[Row]:
    """Return rows filtered to an inclusive time window."""
    if start is None and end is None:
        return rows

    start_ts = parse_timestamp(start) if start is not None else None
    end_ts = parse_timestamp(end) if end is not None else None
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError("--start must be <= --end")

    filtered: list[Row] = []
    for row in rows:
        row_ts = parse_timestamp(row.timestamp)
        if start_ts is not None and row_ts < start_ts:
            continue
        if end_ts is not None and row_ts > end_ts:
            continue
        filtered.append(row)
    return filtered


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------
def load_csv(path: str) -> list[Row]:
    """Parse a data_collection CSV into Row objects."""
    rows: list[Row] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                # Guard against occasional corrupted export rows (e.g. embedded
                # null bytes in the timestamp) so a single bad line can't abort
                # an outdoor-temperature lookup mid-replay.
                parse_timestamp(r["timestamp"])
                rows.append(
                    Row(
                        timestamp=r["timestamp"],
                        hvac_mode=r["hvac_mode"],
                        current_temp=float(r["current_temp"]),
                        target_temp=float(r["target_temp"]),
                        vtherm_slope=float(r["vtherm_slope"]),
                        current_fan=r["current_fan"],
                        decided_fan=r["decided_fan"],
                        minutes_since_change=float(r["minutes_since_change"]),
                        is_window_open=bool(int(r.get("is_window_open", "0"))),
                        defrost_active=bool(int(r.get("defrost_active", "0"))),
                        hvac_idle=bool(int(r.get("hvac_idle", "0"))),
                        mpc_fan=r.get("mpc_fan", ""),
                        mpc_status=r.get("mpc_status", ""),
                        phase=r.get("phase", ""),
                        dead_time=float(r["dead_time"]) if r.get("dead_time") else 10.0,
                        user_target_temp=float(r["user_target_temp"]) if r.get("user_target_temp") else None,
                        regulation_offset=float(r["regulation_offset"]) if r.get("regulation_offset") else None,
                    )
                )
            except (ValueError, KeyError) as exc:
                print(f"  Warning: skipping row: {exc}", file=sys.stderr)
    return rows


# ---------------------------------------------------------------------------
# Learning profile builder
# ---------------------------------------------------------------------------
def build_learning(rows: list[Row]) -> ThermalLearning:
    """Seed a ThermalLearning from established, undisturbed CSV rows."""
    learning = ThermalLearning()
    for row in rows:
        if row.phase != "ESTABLISHED":
            continue
        if row.is_window_open or row.defrost_active or row.hvac_idle:
            continue
        if row.hvac_mode in ("off", "dry", "fan_only"):
            continue
        if not row.current_fan:
            continue
        # The comfort error is taken against the user's setpoint when the trace
        # has it (CSV from this version on), like the live learner does.
        comfort_target = row.user_target_temp if row.user_target_temp is not None else row.target_temp
        error = (
            row.current_temp - comfort_target
            if row.hvac_mode == "cool"
            else comfort_target - row.current_temp
        )
        learning.add_slope_sample(
            fan_mode=row.current_fan,
            slope=row.vtherm_slope,
            temperature_error=error,
            hvac_mode=row.hvac_mode,
            regulation_offset=row.regulation_offset,
        )
    return learning


def clone_learning(learning: ThermalLearning) -> ThermalLearning:
    """Clone learning state for one replay variant."""
    return ThermalLearning.from_dict(learning.to_dict())


def load_snapshot_events(
    snapshot_dir: str | None,
    *,
    default_hvac_mode: str,
    fan_modes: list[str],
) -> list[SnapshotEvent]:
    """Load all profile snapshot events from *_effective_slope.csv files."""
    if snapshot_dir is None:
        return []

    snapshot_path = Path(snapshot_dir)
    if not snapshot_path.exists():
        return []

    events: list[SnapshotEvent] = []
    for path in sorted(snapshot_path.glob("*_effective_slope.csv")):
        fallback_mode = path.stem.removesuffix("_effective_slope")
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                entity_id = row.get("entity_id", "")
                match = _PROFILE_ENTITY_RE.search(entity_id)
                if match:
                    hvac_mode = match.group(1)
                    fan_mode = match.group(2)
                else:
                    hvac_mode = default_hvac_mode
                    fan_mode = fallback_mode

                if fan_mode not in fan_modes:
                    continue

                try:
                    changed_at = parse_timestamp(row["last_changed"])
                except (KeyError, ValueError):
                    continue

                state = row.get("state", "").strip()
                if state in ("", "unknown", "unavailable"):
                    slope = None
                else:
                    try:
                        slope = float(state)
                    except ValueError:
                        continue

                events.append(
                    SnapshotEvent(
                        timestamp=changed_at,
                        fan_mode=fan_mode,
                        hvac_mode=hvac_mode,
                        slope=slope,
                    )
                )

    events.sort(key=lambda event: (event.timestamp, event.hvac_mode, event.fan_mode))
    return events


def load_snapshot_profiles(
    snapshot_events: list[SnapshotEvent],
    *,
    trace_start: datetime,
) -> tuple[dict[tuple[str, str], float], int]:
    """Load the latest known profile values near trace start from snapshot CSV files.

    A short grace window is allowed after the first trace row to handle Home
    Assistant restarts where the profile sensors briefly report unavailable and
    then republish their restored value a few milliseconds later.
    """
    snapshot_deadline = trace_start.timestamp() + _SNAPSHOT_START_GRACE_SECONDS
    profiles: dict[tuple[str, str], float] = {}
    next_index = 0
    for next_index, event in enumerate(snapshot_events):
        if event.timestamp.timestamp() > snapshot_deadline:
            break
        profiles[(event.fan_mode, event.hvac_mode)] = event.slope
    else:
        next_index = len(snapshot_events)

    return {key: slope for key, slope in profiles.items() if slope is not None}, next_index


def seed_learning_from_snapshots(
    learning: ThermalLearning,
    snapshot_profiles: dict[tuple[str, str], float],
) -> list[tuple[str, str, float]]:
    """Seed missing learned profiles from snapshot CSV values.

    Ready profiles learned from the trace are preserved.  Snapshot values only
    backfill modes that would otherwise remain unavailable in the replay.
    """
    seeded: list[tuple[str, str, float]] = []
    for (fan_mode, hvac_mode), slope in snapshot_profiles.items():
        if learning.get_mode_effective_slope(fan_mode, hvac_mode) is not None:
            continue
        learning.set_mode_effective_slope(fan_mode, hvac_mode, slope)
        seeded.append((fan_mode, hvac_mode, slope))
    return seeded


def _set_learning_profile_value(
    learning: ThermalLearning,
    *,
    fan_mode: str,
    hvac_mode: str,
    effective_slope: float,
    timestamp: datetime,
) -> None:
    """Replace one profile with a synthetic snapshot value at a replay timestamp."""
    raw_slope = -effective_slope if hvac_mode == "cool" else effective_slope
    retained = [sample for sample in learning.slope_samples if not (sample[1] == fan_mode and sample[3] == hvac_mode)]
    base_ts = timestamp.timestamp()
    retained.extend((base_ts + offset, fan_mode, raw_slope, hvac_mode) for offset in range(MIN_MODE_PROFILE_SAMPLES))
    learning.slope_samples = retained
    learning.recompute_slope_stats()


def _clear_learning_profile_value(
    learning: ThermalLearning,
    *,
    fan_mode: str,
    hvac_mode: str,
) -> None:
    """Remove one profile when its snapshot becomes unknown/unavailable."""
    retained = [sample for sample in learning.slope_samples if not (sample[1] == fan_mode and sample[3] == hvac_mode)]
    if len(retained) == len(learning.slope_samples):
        return
    learning.slope_samples = retained
    learning.recompute_slope_stats()


def apply_snapshot_events_until(
    learning: ThermalLearning,
    snapshot_events: list[SnapshotEvent],
    start_index: int,
    current_timestamp: datetime,
) -> int:
    """Apply all snapshot events up to the current replay row timestamp."""
    index = start_index
    while index < len(snapshot_events) and snapshot_events[index].timestamp <= current_timestamp:
        event = snapshot_events[index]
        if event.slope is None:
            _clear_learning_profile_value(
                learning,
                fan_mode=event.fan_mode,
                hvac_mode=event.hvac_mode,
            )
        else:
            _set_learning_profile_value(
                learning,
                fan_mode=event.fan_mode,
                hvac_mode=event.hvac_mode,
                effective_slope=event.slope,
                timestamp=event.timestamp,
            )
        index += 1
    return index


def detect_fan_modes(rows: list[Row]) -> list[str]:
    """Return fan modes in first-seen order."""
    seen: dict[str, None] = {}
    for row in rows:
        if row.current_fan and row.current_fan not in seen:
            seen[row.current_fan] = None
    return list(seen.keys())


# ---------------------------------------------------------------------------
# Variant replay (semi-closed-loop: simulated fan, real temperatures)
# ---------------------------------------------------------------------------
def _apply(overrides: dict[str, float]) -> None:
    """Monkey-patch mpc_controller cost constants."""
    for k, v in overrides.items():
        setattr(_mpc_mod, k, v)


def _restore() -> None:
    """Restore original mpc_controller cost constants."""
    for k, v in _DEFAULTS.items():
        setattr(_mpc_mod, k, v)


def replay(
    rows: list[Row],
    learning: ThermalLearning,
    fan_modes: list[str],
    overrides: dict[str, float],
    deadband: float,
    min_interval: int,
    snapshot_events: list[SnapshotEvent] | None = None,
    snapshot_start_index: int = 0,
    cycle_minutes: int = 2,
) -> list[tuple[dict, str]]:
    """Replay CSV through MPC, tracking simulated fan state.

    The simulated time since the last change advances by the real gap between
    rows; *cycle_minutes* only sets the controller's nominal cadence.

    Returns [(mpc_payload, simulated_fan), ...] for each row.
    """
    _apply(overrides)
    try:
        mpc = MPCController(
            learning=learning,
            deadband=deadband,
            min_interval=min_interval,
            fan_modes=fan_modes,
            cycle_minutes=cycle_minutes,
        )
        results: list[tuple[dict, str]] = []
        sim_fan = rows[0].current_fan if rows else fan_modes[0]
        sim_minutes = rows[0].minutes_since_change if rows else 0.0
        snapshot_index = snapshot_start_index
        previous_ts: float | None = None

        for row in rows:
            # Advance the simulated clock by the real gap between rows: the CSV
            # is written once per VTherm cycle, whatever its cycle_min.
            row_ts = parse_timestamp(row.timestamp).timestamp()
            if previous_ts is not None:
                sim_minutes += max(0.0, (row_ts - previous_ts) / 60.0)
            previous_ts = row_ts
            if snapshot_events:
                snapshot_index = apply_snapshot_events_until(
                    learning,
                    snapshot_events,
                    snapshot_index,
                    parse_timestamp(row.timestamp),
                )
            payload = mpc.evaluate(
                current_temp=row.current_temp,
                target_temp=row.target_temp,
                vtherm_slope=row.vtherm_slope,
                hvac_mode=row.hvac_mode,
                current_fan=sim_fan,
                is_window_open=row.is_window_open,
                is_defrost_active=row.defrost_active,
                is_hvac_idle=row.hvac_idle,
                minutes_since_change=sim_minutes,
                user_target_temp=row.user_target_temp,
            )
            rec_fan = payload.get("mpc_fan_mode") or sim_fan
            would_change = payload.get("mpc_would_change_now", "no")

            if would_change == "yes" and rec_fan != sim_fan:
                sim_fan = rec_fan
                sim_minutes = 0.0
                mpc.notify_fan_change()

            results.append((payload, sim_fan))
        return results
    finally:
        _restore()


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------
#: A row counts as "the temperature N minutes later" when it lands within this
#: many minutes after the horizon. Cycles are not perfectly regular (VTherm
#: skips a cycle on missing data, restarts leave gaps), so the lookahead is
#: resolved on timestamps rather than on a fixed number of rows.
LOOKAHEAD_TOLERANCE_MINUTES = 5.0


def lookahead_index(times: list[float], index: int, minutes: float, tolerance: float = LOOKAHEAD_TOLERANCE_MINUTES) -> int | None:
    """Return the first row at least *minutes* after row *index*, or None.

    *times* are the rows' epoch seconds, sorted. None when no row falls within
    *tolerance* minutes after the horizon (a gap in the trace).
    """
    target = times[index] + minutes * 60.0
    found = bisect.bisect_left(times, target)
    if found >= len(times) or times[found] - target > tolerance * 60.0:
        return None
    return found


def mean_abs(values: list[float]) -> float | None:
    """Return the mean of *values*, or None when there are none."""
    return sum(values) / len(values) if values else None


def compute_metrics(
    name: str,
    overrides: dict[str, float],
    rows: list[Row],
    results: list[tuple[dict, str]],
    cycle_minutes: int = 2,
) -> Metrics:
    """Compute aggregate metrics from a replay run."""
    del cycle_minutes  # lookaheads are resolved on timestamps
    m = Metrics(name=name, overrides=overrides, total_rows=len(rows))
    prev_fan: str | None = None
    times = [parse_timestamp(row.timestamp).timestamp() for row in rows]

    for i, (row, (payload, sim_fan)) in enumerate(zip(rows, results)):
        status = payload.get("mpc_status", "")

        m.fan_dist[sim_fan] += 1

        if status in ("Disturbed", "Idle", "Unavailable"):
            prev_fan = sim_fan
            continue

        m.active_rows += 1

        # Fan change
        if prev_fan is not None and sim_fan != prev_fan:
            m.fan_changes += 1

        # Agreement with live system
        if sim_fan == row.decided_fan:
            m.agree_with_live += 1

        # Cost
        cost = payload.get("mpc_cost")
        if cost is not None:
            m.costs.append(cost)

        # Prediction error at T+10 / T+30, next to the persistence baseline
        # (same rows, forecast = current temperature).
        for minutes, key, model_errors, persistence_errors in (
            (10, "mpc_predicted_temperature_10m", m.prediction_errors_10m, m.persistence_errors_10m),
            (30, "mpc_predicted_temperature_30m", m.prediction_errors_30m, m.persistence_errors_30m),
        ):
            predicted = payload.get(key)
            future = lookahead_index(times, i, minutes)
            if predicted is None or future is None:
                continue
            actual = rows[future].current_temp
            model_errors.append(abs(predicted - actual))
            persistence_errors.append(abs(row.current_temp - actual))

        prev_fan = sim_fan

    return m


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_report(
    rows: list[Row],
    fan_modes: list[str],
    learning: ThermalLearning,
    metrics_list: list[Metrics],
    cycle_minutes: int = 2,
) -> None:
    """Print a formatted comparison report."""
    del cycle_minutes
    total_hours = (parse_timestamp(rows[-1].timestamp) - parse_timestamp(rows[0].timestamp)).total_seconds() / 3600.0 if rows else 0.0

    # --- Learning profiles ---
    print("\n=== Learning Profiles ===")
    hvac_modes_seen = sorted(
        {r.hvac_mode for r in rows if r.hvac_mode not in ("off", "dry", "fan_only")}
    )
    for hvac in hvac_modes_seen:
        profiles = learning.get_mode_profiles(hvac, fan_modes)
        print(f"\n  {hvac}:")
        for mode, info in profiles.items():
            slope = info["effective_slope"]
            samples = info["samples"]
            status = "+" if info["ready"] else "."
            slope_str = f"{slope:+.3f} C/h" if slope is not None else "n/a"
            print(f"    {status} {mode:>10s}: {slope_str}  ({samples} samples)")

    print(f"\n  Dead time: {learning.get_dead_time():.1f} min")
    print(f"  Total slope samples: {learning.slope_sample_count()}")
    print(f"  Trace duration: {total_hours:.1f} hours ({len(rows)} rows)")

    # --- Variant comparison table ---
    print("\n=== Variant Comparison ===\n")
    col_w = max(18, max(len(m.name) for m in metrics_list) + 4)
    header = f"{'Metric':<32s}"
    for m in metrics_list:
        header += f"{m.name:>{col_w}s}"
    print(header)
    print("-" * len(header))

    def _row(label: str, values: list[str]) -> None:
        line = f"{label:<32s}"
        for v in values:
            line += f"{v:>{col_w}s}"
        print(line)

    _row(
        "Fan changes/hour",
        [f"{m.fan_changes / max(total_hours, 0.01):.2f}" for m in metrics_list],
    )
    _row("Total fan changes", [str(m.fan_changes) for m in metrics_list])
    _row(
        "Agreement with live (%)",
        [f"{100.0 * m.agree_with_live / max(m.active_rows, 1):.1f}" for m in metrics_list],
    )
    _row(
        "Avg MPC cost",
        [f"{sum(m.costs) / max(len(m.costs), 1):.2f}" for m in metrics_list],
    )
    def _mae(values: list[float]) -> str:
        mae = mean_abs(values)
        return f"{mae:.3f}" if mae is not None else "n/a"

    _row("Prediction MAE T+10 (C)", [_mae(m.prediction_errors_10m) for m in metrics_list])
    _row("  persistence T+10 (C)", [_mae(m.persistence_errors_10m) for m in metrics_list])
    _row("Prediction MAE T+30 (C)", [_mae(m.prediction_errors_30m) for m in metrics_list])
    _row("  persistence T+30 (C)", [_mae(m.persistence_errors_30m) for m in metrics_list])
    _row("Active rows", [str(m.active_rows) for m in metrics_list])
    _row("Disturbed/idle rows", [str(m.total_rows - m.active_rows) for m in metrics_list])

    # Fan distribution
    print(f"\n{'--- Fan Distribution (%) ---':<32s}")
    for fm in fan_modes:
        _row(
            f"  {fm}",
            [
                f"{100.0 * m.fan_dist.get(fm, 0) / max(sum(m.fan_dist.values()), 1):.1f}"
                for m in metrics_list
            ],
        )

    # Overrides summary
    print("\n--- Cost Overrides ---")
    for m in metrics_list:
        if m.overrides:
            parts = ", ".join(f"{k}={v}" for k, v in m.overrides.items())
            print(f"  {m.name}: {parts}")
        else:
            print(f"  {m.name}: (defaults)")


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------
def write_output_csv(
    path: str,
    rows: list[Row],
    variants: list[tuple[str, dict[str, float]]],
    all_results: dict[str, list[tuple[dict, str]]],
) -> None:
    """Write per-row replay decisions for all variants to a CSV."""
    variant_names = [name for name, _ in variants]
    header = ["timestamp", "hvac_mode", "current_temp", "target_temp", "vtherm_slope", "live_fan"]
    for vn in variant_names:
        header.extend([f"{vn}_fan", f"{vn}_cost", f"{vn}_status", f"{vn}_would_change"])

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for i, row in enumerate(rows):
            out: list[object] = [
                row.timestamp,
                row.hvac_mode,
                row.current_temp,
                row.target_temp,
                row.vtherm_slope,
                row.decided_fan,
            ]
            for vn in variant_names:
                payload, sim_fan = all_results[vn][i]
                cost = payload.get("mpc_cost")
                out.extend([
                    sim_fan,
                    f"{cost:.3f}" if cost is not None else "",
                    payload.get("mpc_status", ""),
                    payload.get("mpc_would_change_now", "no"),
                ])
            writer.writerow(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_variant_spec(spec: str) -> tuple[str, dict[str, float]]:
    """Parse 'name:KEY=val,KEY=val' into (name, {overrides})."""
    if ":" not in spec:
        return spec, {}
    name, params = spec.split(":", 1)
    overrides: dict[str, float] = {}
    for part in params.split(","):
        key, val = part.split("=", 1)
        key = key.strip()
        if key not in TUNABLE:
            raise argparse.ArgumentTypeError(
                f"Unknown tunable: {key}. Valid: {', '.join(TUNABLE)}"
            )
        overrides[key] = float(val)
    return name, overrides


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Replay CSV traces through the MPC controller with configurable cost weights.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Variant syntax:\n"
            "  --variant NAME               Use default cost weights\n"
            "  --variant NAME:KEY=VAL,...    Override specific constants\n\n"
            "Tunable constants (defaults):\n"
            + "\n".join(f"  {k} = {v}" for k, v in _DEFAULTS.items())
        ),
    )
    parser.add_argument("csv_file", help="Path to a data_collection CSV file")
    parser.add_argument(
        "--variant",
        action="append",
        default=[],
        metavar="SPEC",
        help="Variant specification (repeatable). Format: NAME[:KEY=VAL,...]",
    )
    parser.add_argument("--deadband", type=float, default=0.2, help="Deadband in degrees (default: 0.2)")
    parser.add_argument("--min-interval", type=int, default=10, help="Min interval in minutes (default: 10)")
    parser.add_argument("--cycle-minutes", type=int, default=5, help="VTherm cycle_min the trace was recorded at (default: 5)")
    parser.add_argument(
        "--fan-order",
        help="Comma-separated fan modes, weakest to strongest (auto-detected if omitted)",
    )
    parser.add_argument(
        "--output",
        help="Write per-row replay decisions to this CSV path",
    )
    parser.add_argument(
        "--seed-snapshots-dir",
        help=("Directory containing *_effective_slope.csv snapshots used to seed missing " "profiles (defaults to the CSV file directory when such files are present)"),
    )
    parser.add_argument(
        "--start",
        help="Inclusive start timestamp (ISO-8601, e.g. 2026-04-17T17:40:00Z)",
    )
    parser.add_argument(
        "--end",
        help="Inclusive end timestamp (ISO-8601, e.g. 2026-04-17T18:15:00Z)",
    )
    args = parser.parse_args()

    # Parse variants
    variants: list[tuple[str, dict[str, float]]] = []
    if not args.variant:
        variants.append(("baseline", {}))
    for spec in args.variant:
        try:
            variants.append(parse_variant_spec(spec))
        except (ValueError, argparse.ArgumentTypeError) as exc:
            parser.error(str(exc))

    # Load data
    print(f"Loading {args.csv_file}...")
    rows = load_csv(args.csv_file)
    if not rows:
        print("Error: no valid rows found.", file=sys.stderr)
        sys.exit(1)
    print(f"  {len(rows)} rows loaded ({rows[0].timestamp} -> {rows[-1].timestamp})")

    try:
        rows = filter_rows_by_time(rows, start=args.start, end=args.end)
    except ValueError as exc:
        parser.error(str(exc))
    if not rows:
        print("Error: no rows remain after applying the time filter.", file=sys.stderr)
        sys.exit(1)
    if args.start or args.end:
        print(f"  Windowed to {len(rows)} rows ({rows[0].timestamp} -> {rows[-1].timestamp})")

    # Fan modes
    if args.fan_order:
        fan_modes = [m.strip() for m in args.fan_order.split(",")]
    else:
        fan_modes = detect_fan_modes(rows)
    print(f"  Fan modes: {fan_modes}")

    # Build learning
    print("Building learning profiles...")
    learning = build_learning(rows)
    print(f"  {learning.slope_sample_count()} slope samples ingested")

    snapshot_dir = args.seed_snapshots_dir or str(Path(args.csv_file).resolve().parent)
    snapshot_events = load_snapshot_events(
        snapshot_dir,
        default_hvac_mode=rows[0].hvac_mode,
        fan_modes=fan_modes,
    )
    snapshot_profiles = load_snapshot_profiles(
        snapshot_events,
        trace_start=parse_timestamp(rows[0].timestamp),
    )
    initial_snapshot_profiles, snapshot_start_index = snapshot_profiles
    seeded_profiles = seed_learning_from_snapshots(learning, initial_snapshot_profiles)
    if seeded_profiles:
        seeded_summary = ", ".join(f"{hvac_mode}/{fan_mode}={slope:.3f}" for fan_mode, hvac_mode, slope in seeded_profiles)
        print(f"  Seeded {len(seeded_profiles)} missing profiles from snapshots: {seeded_summary}")

    # Run variants
    all_metrics: list[Metrics] = []
    all_results: dict[str, list[tuple[dict, str]]] = {}
    report_learning = learning

    for name, overrides in variants:
        label = name
        if overrides:
            label += " (" + ", ".join(f"{k}={v}" for k, v in overrides.items()) + ")"
        print(f"Replaying variant '{label}'...")
        variant_learning = clone_learning(learning)
        results = replay(
            rows,
            variant_learning,
            fan_modes,
            overrides,
            args.deadband,
            args.min_interval,
            snapshot_events=snapshot_events,
            snapshot_start_index=snapshot_start_index,
            cycle_minutes=args.cycle_minutes,
        )
        m = compute_metrics(name, overrides, rows, results)
        all_metrics.append(m)
        all_results[name] = results
        report_learning = variant_learning

    # Report
    print_report(rows, fan_modes, report_learning, all_metrics)

    # Optional CSV output
    if args.output:
        write_output_csv(args.output, rows, variants, all_results)
        print(f"\nPer-row decisions written to {args.output}")


if __name__ == "__main__":
    main()
