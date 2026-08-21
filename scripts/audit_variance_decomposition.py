#!/usr/bin/env python3
"""Variance decomposition of dT/dt: what actually drives the cooling rate?

Fits a nested sequence of OLS models on the controller trace (merged with an
outdoor-temperature export) and reports each model's R2, to quantify how much
of the variation in dT/dt is explained by the envelope model (conductance +
per-fan power, whether free or airflow-constrained) versus by the comfort-error
(demand / distance-to-setpoint) term that the envelope model omits.

    M0: intercept only
    M1: + k_env*(Text-T)                      (envelope conductance)
    M2: M1 + per-fan intercept                (current free per-fan envelope fit)
    M3: k_env + a + b*airflow                 (airflow-constrained fit)
    M4: M2 + comfort_error                    (demand term added)
    M5: M2 + comfort_error*fan                (demand slope per fan)

This is an *offline audit* helper, not part of the running integration.

Usage:
    python scripts/audit_variance_decomposition.py CONTROLLER.csv OUTDOOR.csv \\
        [--hvac cool] [--outdoor-entity sensor.gardanne_temperature]

Rated airflow (m3/h) per fan speed is hard-coded in AIRFLOW below; edit it to
match the fan being analysed.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
from collections import Counter

# Rated airflow (m3/h) from the fan's spec sheet, for the M3 airflow-constrained fit.
AIRFLOW = {"silent": 216, "low": 300, "med": 378, "high": 468, "superhigh": 666}


def parse_ts(s: str) -> dt.datetime:
    """Parse an ISO-8601 timestamp with optional trailing Z."""
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_outdoor(path: str, entity: str | None) -> list[tuple[dt.datetime, float]]:
    """Load and sort (timestamp, value) pairs from an HA history CSV."""
    series: list[tuple[dt.datetime, float]] = []
    for r in csv.reader(open(path, encoding="utf-8")):
        if not r or r[0] == "entity_id":
            continue
        if entity and r[0] != entity:
            continue
        try:
            series.append((parse_ts(r[2]), float(r[1])))
        except (ValueError, IndexError):
            continue
    series.sort()
    return series


def interpolator(series: list[tuple[dt.datetime, float]]):
    """Return a linear-interpolation function over a sorted time series."""
    times = [t for t, _ in series]

    def at(t: dt.datetime) -> float:
        if t <= series[0][0]:
            return series[0][1]
        if t >= series[-1][0]:
            return series[-1][1]
        i = bisect.bisect_right(times, t)
        t0, v0 = series[i - 1]
        t1, v1 = series[i]
        return v0 + (v1 - v0) * ((t - t0) / (t1 - t0))

    return at


def fnum(x: str) -> float | None:
    """Parse a float or return None."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def solve(rows: list[tuple[list[float], float]], p: int) -> list[float] | None:
    """Solve OLS beta for the design rows via the normal equations, or None if singular."""
    a = [[0.0] * p for _ in range(p)]
    b = [0.0] * p
    for x, y in rows:
        for i in range(p):
            b[i] += x[i] * y
            for j in range(p):
                a[i][j] += x[i] * x[j]
    m = [a[i][:] + [b[i]] for i in range(p)]
    for c in range(p):
        piv = max(range(c, p), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        if abs(m[c][c]) < 1e-12:
            return None
        pv = m[c][c]
        m[c] = [v / pv for v in m[c]]
        for r in range(p):
            if r != c and abs(m[r][c]) > 1e-12:
                f = m[r][c]
                m[r] = [m[r][k] - f * m[c][k] for k in range(p + 1)]
    return [m[i][p] for i in range(p)]


def r2(rows: list[tuple[list[float], float]], beta: list[float]) -> float:
    """Coefficient of determination of the fitted model over the rows."""
    ys = [y for _, y in rows]
    ybar = sum(ys) / len(ys)
    ss_tot = sum((y - ybar) ** 2 for y in ys)
    ss_res = sum((y - sum(bi * xi for bi, xi in zip(beta, x))) ** 2 for x, y in rows)
    return 1 - ss_res / ss_tot if ss_tot else float("nan")


def fit_report(name: str, rows: list[tuple[list[float], float]], p: int) -> None:
    """Fit one model and print its R2 (or 'singular')."""
    beta = solve(rows, p)
    print(f"{name:48s}  singular" if beta is None else f"{name:48s}  R2 = {r2(rows, beta):.4f}")


def main() -> None:
    """Entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("controller_csv")
    ap.add_argument("outdoor_csv")
    ap.add_argument("--hvac", default="cool")
    ap.add_argument("--outdoor-entity", default=None, help="Filter outdoor CSV to this entity_id")
    args = ap.parse_args()

    ext = load_outdoor(args.outdoor_csv, args.outdoor_entity)
    if not ext:
        raise SystemExit("No outdoor samples loaded (check --outdoor-entity).")
    iext = interpolator(ext)

    rows = [r for r in csv.DictReader(open(args.controller_csv, newline="", encoding="utf-8")) if r["hvac_mode"] == args.hvac]
    # (fan, Text-T, dT/dt, comfort_error)
    S: list[tuple[str, float, float, float]] = []
    for r in rows:
        if r["phase"] != "ESTABLISHED":
            continue
        if r.get("is_window_open") in ("1", "True", "true"):
            continue
        if r.get("hvac_idle") in ("1", "True", "true"):
            continue
        T = fnum(r["current_temp"])
        vs = fnum(r["vtherm_slope"])
        er = fnum(r["current_error"])
        if None in (T, vs, er):
            continue
        S.append((r["current_fan"], iext(parse_ts(r["timestamp"])) - T, vs, er))

    fans: list[str] = []
    for f, *_ in S:
        if f not in fans:
            fans.append(f)
    idx = {f: i for i, f in enumerate(fans)}
    print(f"n = {len(S)}  per fan: {dict(Counter(s[0] for s in S))}\n")

    fit_report("M0: intercept only", [([1.0], vs) for _, _, vs, _ in S], 1)
    fit_report("M1: + k_env*(Text-T)", [([1.0, g], vs) for _, g, vs, _ in S], 2)

    def m2row(s):
        f, g, vs, _ = s
        x = [0.0] * (1 + len(fans))
        x[0] = g
        x[1 + idx[f]] = 1.0
        return (x, vs)

    fit_report("M2: k_env + per-fan intercept (free fit)", [m2row(s) for s in S], 1 + len(fans))
    fit_report("M3: k_env + a + b*airflow", [([g, 1.0, AIRFLOW[f]], vs) for f, g, vs, _ in S if f in AIRFLOW], 3)

    def m4row(s):
        f, g, vs, er = s
        x = [0.0] * (2 + len(fans))
        x[0] = g
        x[1] = max(er, 0.0)
        x[2 + idx[f]] = 1.0
        return (x, vs)

    fit_report("M4: M2 + comfort_error (shared demand term)", [m4row(s) for s in S], 2 + len(fans))

    def m5row(s):
        f, g, vs, er = s
        x = [0.0] * (1 + 2 * len(fans))
        x[0] = g
        x[1 + idx[f]] = 1.0
        x[1 + len(fans) + idx[f]] = max(er, 0.0)
        return (x, vs)

    fit_report("M5: M2 + comfort_error*fan (per-fan demand)", [m5row(s) for s in S], 1 + 2 * len(fans))


if __name__ == "__main__":
    main()
