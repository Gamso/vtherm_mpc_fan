# Analysis Scripts — VTherm MPC Fan

Offline tools for analysing and testing MPC algorithm behaviour.

---

## `replay_bench.py` — MPC replay on CSV traces

Replays (open-loop) a CSV file produced by the data collector through `MPCController`,
with support for comparing multiple cost-weight variants simultaneously.

### Prerequisites

The CSV file must be in `data_collection.py` format (columns: `timestamp`, `hvac_mode`,
`current_temp`, `target_temp`, `vtherm_slope`, `current_fan`, etc.).

### Basic usage

```bash
python scripts/replay_bench.py /config/smart_fan_controller_data_XXXXXXXX.csv
```

### Comparing cost variants

```bash
python scripts/replay_bench.py data.csv \
  --variant baseline \
  --variant high_rank:MODE_RANK_COST=0.3 \
  --variant aggressive:COMFORT_ERROR_WEIGHT=2.0,URGENCY_SENSITIVITY=4.0
```

### Exporting per-row decisions

```bash
python scripts/replay_bench.py data.csv \
  --variant baseline \
  --variant high_rank:MODE_RANK_COST=0.3 \
  --output /tmp/replay_results.csv
```

### Analysing a specific time window

```bash
python scripts/replay_bench.py data.csv \
  --start 2026-04-17T17:40:00Z \
  --end 2026-04-17T18:15:00Z \
  --fan-order silent,low,med,high,superhigh
```

### Options

| Option                         | Default       | Description                                                  |
| ------------------------------ | ------------- | ------------------------------------------------------------ |
| `--variant NAME[:KEY=VAL,...]` | `baseline`    | Variant to replay (repeatable)                               |
| `--deadband FLOAT`             | `0.2`         | Deadband in degrees                                          |
| `--min-interval INT`           | `10`          | Minimum interval between fan changes (min)                   |
| `--cycle-minutes INT`          | `5`           | VTherm `cycle_min` the trace was recorded at                 |
| `--fan-order a,b,c`            | auto-detected | Fan modes ordered weakest to strongest                       |
| `--seed-snapshots-dir DIR`     | CSV directory | Seed missing profiles from `*_effective_slope.csv` snapshots |
| `--start ISO_TIMESTAMP`        | —             | Inclusive start of the replay window                         |
| `--end ISO_TIMESTAMP`          | —             | Inclusive end of the replay window                           |
| `--output FILE`                | —             | Write per-row decisions to a CSV file                        |

### Tunable constants

| Constant                           | Default | Role                                              |
| ---------------------------------- | ------- | ------------------------------------------------- |
| `COMFORT_ERROR_WEIGHT`             | `1.0`   | Weight of the comfort error term                  |
| `OVERSHOOT_QUADRATIC_WEIGHT`       | `3.0`   | Quadratic penalty for overshoot                   |
| `FLOOR_VIOLATION_LINEAR_WEIGHT`    | `12.0`  | Linear penalty for floor violation                |
| `FLOOR_VIOLATION_QUADRATIC_WEIGHT` | `30.0`  | Quadratic penalty for floor violation             |
| `MODE_CHANGE_DISTANCE_COST`        | `0.15`  | Distance cost when switching fan mode             |
| `MODE_RANK_COST`                   | `1.0`   | Rank cost (energy, × `MODE_POWER_RATIO`^rank)     |
| `URGENCY_SENSITIVITY`              | `2.0`   | Urgency scaling when error exceeds deadband       |

### Report output

- **Learning profiles**: learned effective slope per mode/HVAC mode + sample count
- **Fan changes/hour**: stability comparison across variants
- **Agreement with live (%)**: match with decisions recorded in the CSV
- **Avg MPC cost**: mean weighted cost (relative comparison between variants)
- **Prediction MAE T+10 / T+30**: absolute prediction error for temperature at T+10 / T+30 min,
  each followed by the **persistence** baseline on the same rows (forecast = current
  temperature). A room sensor moving in 0.2 °C steps makes persistence hard to beat, so
  a model MAE is only meaningful next to it. Lookaheads are resolved on timestamps (a
  row within 5 min after the horizon), so gaps in the trace are skipped
- **Fan distribution**: percentage of time spent in each fan mode

If snapshot CSV files are present, the replay bench uses them in two ways:

- it seeds profiles that are still missing near the start of the replay window
- it replays later snapshot changes during the trace, including transitions to
  `unknown` / `unavailable`

This makes long replays closer to the live controller state when profile
sensors are restored after a restart or disappear later in the trace.

---

## `analyze_mpc_viability.py` — Shadow MPC vs production analysis

Compares decisions from the legacy shadow MPC (beta period, CSV format `mpc_shadow_*`)
against live decisions from the production controller.

> **Note:** This script expects a CSV file at `data/smart_fan_controller_data_01KK6PQT.csv`
> in the historical format (columns `mpc_shadow_fan`, `mpc_shadow_cost`, etc.).
> It is no longer compatible with the current data collector format.

### Usage

```bash
python scripts/analyze_mpc_viability.py
```

Produces 12 analysis sections: global statistics, reactivity, fan speed distribution,
live/MPC divergence episodes, aggressiveness evaluation, etc.

---

## `analyze_real_data.py` / `analyze_real_data_v2.py` — Initial calibration

One-shot scripts used to calibrate algorithm parameters from raw HA data
(`sensor.thermometre_salon_temperature` + VTherm slope).

Data is embedded directly in the script (no external file needed).

- `analyze_real_data.py`: dead time, slopes per fan mode, heating/maintenance behaviour
- `analyze_real_data_v2.py`: in-depth analysis of real dead time and maintenance-mode oscillations

### Usage

```bash
python scripts/analyze_real_data.py
python scripts/analyze_real_data_v2.py
```
