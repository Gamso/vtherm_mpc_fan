#!/usr/bin/env python3
"""Analyse approfondie : dead time réel et oscillations de maintenance."""

import csv
import io
from datetime import datetime, timedelta, timezone

def parse_ts(s):
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.strptime(s.split(".")[0], "%Y-%m-%dT%H:%M:%S")

def load_csv(raw_csv):
    reader = csv.DictReader(io.StringIO(raw_csv.strip()))
    data = []
    for row in reader:
        state = row["state"].strip()
        if state in ("unavailable", "unknown", ""):
            continue
        try:
            val = float(state)
            ts = parse_ts(row["last_changed"].strip())
            data.append((ts, val))
        except (ValueError, TypeError):
            continue
    data.sort(key=lambda x: x[0])
    return data

def mins(t1, t2):
    return (t2 - t1).total_seconds() / 60.0

# ─── Data (abbreviated - reuse from main script) ──────────────────────────
# Import data from the main analysis script
import sys
sys.path.insert(0, "/workspaces/vtherm_mpc_fan/scripts")
from analyze_real_data import TEMP_SALON_CSV, VSLOPE_CSV

temp_data = load_csv(TEMP_SALON_CSV)
slope_data = load_csv(VSLOPE_CSV)

print("=" * 80)
print("ANALYSE APPROFONDIE - Dead Time Réel & Oscillations")
print("=" * 80)

# ═══════════════════════════════════════════════════════════════════════════
# 1. DEAD TIME RÉEL : Mesure depuis l'activation AC (slope jump)
# ═══════════════════════════════════════════════════════════════════════════
print("\n" + "─" * 80)
print("1. DEAD TIME RÉEL (depuis activation AC détectée par slope)")
print("─" * 80)
print("\n  Méthode : on cherche le moment où slope passe de ~0 à > 0.5 (activation AC)")
print("  puis on mesure le délai jusqu'à la première montée de temp de +0.2°C")

# Find AC activation events: slope jumps from near-0 to > 0.5
ac_events = []
for i in range(1, len(slope_data)):
    prev_ts, prev_v = slope_data[i - 1]
    curr_ts, curr_v = slope_data[i]
    if prev_v < 0.15 and curr_v > 0.5:
        # Confirm it's sustained (not a blip) - look at next reading if available
        ac_events.append({
            "activation_time": curr_ts,
            "prev_slope": prev_v,
            "first_slope": curr_v,
            "slope_jump_min": mins(prev_ts, curr_ts),
        })

print(f"\n  Événements d'activation AC détectés : {len(ac_events)}")

for idx, event in enumerate(ac_events, 1):
    act_time = event["activation_time"]

    # Find temperature at activation time (closest reading before)
    temp_at_activation = None
    temp_ts_at_activation = None
    for ts, val in temp_data:
        if ts <= act_time:
            temp_at_activation = val
            temp_ts_at_activation = ts
        else:
            break

    # Find first temp reading that's >= base + 0.2 after activation
    first_response_temp = None
    first_response_time = None
    if temp_at_activation is not None:
        for ts, val in temp_data:
            if ts > act_time and val >= temp_at_activation + 0.2:
                first_response_temp = val
                first_response_time = ts
                break

    # Find peak slope in the heating phase
    peak_slope = 0
    slopes_after = [(ts, v) for ts, v in slope_data if act_time <= ts <= act_time + timedelta(minutes=30)]
    if slopes_after:
        peak_slope = max(v for _, v in slopes_after)

    dead_time = None
    if first_response_time:
        dead_time = mins(act_time, first_response_time)

    print(f"\n  AC Event {idx}: {act_time.strftime('%m/%d %H:%M:%S')}")
    print(f"    Slope : {event['prev_slope']:.2f} → {event['first_slope']:.2f} (Δt={event['slope_jump_min']:.0f}min entre lectures)")
    print(f"    Peak slope dans 30min : {peak_slope:.2f} °C/h")
    if temp_at_activation:
        print(f"    Temp base : {temp_at_activation:.1f}°C")
    if dead_time is not None:
        print(f"    ► DEAD TIME (AC→temp+0.2°C) : {dead_time:.1f} min")
    else:
        print(f"    ► DEAD TIME : Non mesurable (pas de montée dans la fenêtre)")

# Collect valid dead times (exclude outliers > 60 min - those are re-heating blips)
valid_dead_times = []
for idx, event in enumerate(ac_events):
    act_time = event["activation_time"]
    temp_at_activation = None
    for ts, val in temp_data:
        if ts <= act_time:
            temp_at_activation = val
        else:
            break
    if temp_at_activation is not None:
        for ts, val in temp_data:
            if ts > act_time and val >= temp_at_activation + 0.2:
                dt = mins(act_time, ts)
                if dt < 60:  # Exclude outliers (re-heating blips)
                    valid_dead_times.append(dt)
                break

if valid_dead_times:
    print(f"\n  ► RÉSUMÉ DEAD TIME RÉEL:")
    print(f"    Valeurs : {', '.join(f'{d:.1f}' for d in valid_dead_times)} min")
    print(f"    Minimum : {min(valid_dead_times):.1f} min")
    print(f"    Médiane : {sorted(valid_dead_times)[len(valid_dead_times)//2]:.1f} min")
    print(f"    Maximum : {max(valid_dead_times):.1f} min")
    print(f"    Moyenne : {sum(valid_dead_times)/len(valid_dead_times):.1f} min")

# ═══════════════════════════════════════════════════════════════════════════
# 2. OSCILLATIONS EN PHASE DE MAINTIEN (temperature ≈ 20 ± 0.6)
# ═══════════════════════════════════════════════════════════════════════════
print("\n\n" + "─" * 80)
print("2. ANALYSE DES OSCILLATIONS EN PHASE DE MAINTIEN")
print("─" * 80)

# Identify maintenance periods: when temp stays in [19.4, 20.6] for > 1h
# and there are frequent changes
utc = timezone.utc
maintenance_periods = [
    ("Feb 27 AM", datetime(2026, 2, 27, 5, 27, tzinfo=utc), datetime(2026, 2, 27, 9, 39, tzinfo=utc)),
    ("Feb 26 PM", datetime(2026, 2, 26, 14, 42, tzinfo=utc), datetime(2026, 2, 26, 18, 17, tzinfo=utc)),
    ("Feb 28 AM", datetime(2026, 2, 28, 5, 48, tzinfo=utc), datetime(2026, 2, 28, 7, 52, tzinfo=utc)),
    ("Feb 28 mid", datetime(2026, 2, 28, 9, 2, tzinfo=utc), datetime(2026, 2, 28, 12, 19, tzinfo=utc)),
    ("Mar 01 AM", datetime(2026, 3, 1, 5, 27, tzinfo=utc), datetime(2026, 3, 1, 8, 24, tzinfo=utc)),
    ("Mar 01 PM", datetime(2026, 3, 1, 15, 53, tzinfo=utc), datetime(2026, 3, 1, 18, 51, tzinfo=utc)),
]

all_half_periods = []
all_osc_slopes = []

for name, start, end in maintenance_periods:
    period_temps = [(ts, v) for ts, v in temp_data if start <= ts <= end]
    period_slopes = [(ts, v) for ts, v in slope_data if start <= ts <= end]

    if len(period_temps) < 3:
        continue

    # Analyze oscillation: find local min/max alternation
    turning_points = []
    for k in range(1, len(period_temps) - 1):
        prev_t = period_temps[k-1][1]
        curr_t = period_temps[k][1]
        next_t = period_temps[k+1][1]
        if (curr_t >= prev_t and curr_t >= next_t) or (curr_t <= prev_t and curr_t <= next_t):
            turning_points.append(period_temps[k])

    # Calculate half-periods (min→max or max→min)
    half_periods = []
    for k in range(1, len(turning_points)):
        hp = mins(turning_points[k-1][0], turning_points[k][0])
        if hp > 0:
            half_periods.append(hp)

    # Slope stats during this period
    slope_vals = [v for _, v in period_slopes]
    pos_slopes = [v for v in slope_vals if v > 0.1]
    neg_slopes = [v for v in slope_vals if v < -0.1]
    near_zero = [v for v in slope_vals if abs(v) <= 0.1]

    print(f"\n  [{name}] {start.strftime('%m/%d %H:%M')} → {end.strftime('%H:%M')}")
    print(f"    Samples temp : {len(period_temps)}")
    temps = [v for _, v in period_temps]
    print(f"    Range temp   : {min(temps):.1f} - {max(temps):.1f}°C")
    print(f"    Turning pts  : {len(turning_points)}")
    if half_periods:
        print(f"    Demi-périodes: {', '.join(f'{hp:.0f}' for hp in half_periods)} min")
        print(f"    Demi-période moy : {sum(half_periods)/len(half_periods):.0f} min")
        print(f"    Période complète ≈ {2*sum(half_periods)/len(half_periods):.0f} min")
        all_half_periods.extend(half_periods)

    if slope_vals:
        print(f"    Slopes range : [{min(slope_vals):.2f}, {max(slope_vals):.2f}]")
        print(f"    Slopes > 0.1 : {len(pos_slopes)}/{len(slope_vals)} → moy={sum(pos_slopes)/len(pos_slopes):.2f}" if pos_slopes else "    Slopes > 0.1 : 0")
        print(f"    Slopes < -0.1: {len(neg_slopes)}/{len(slope_vals)} → moy={sum(neg_slopes)/len(neg_slopes):.2f}" if neg_slopes else "    Slopes < -0.1: 0")
        print(f"    Slopes ≈ 0   : {len(near_zero)}/{len(slope_vals)}")
        all_osc_slopes.extend(slope_vals)

if all_half_periods:
    print(f"\n  ► RÉSUMÉ OSCILLATIONS EN MAINTIEN:")
    print(f"    Demi-période globale : {sum(all_half_periods)/len(all_half_periods):.0f} min")
    print(f"    Période complète     : ≈{2*sum(all_half_periods)/len(all_half_periods):.0f} min")
    print(f"    Demi-période min     : {min(all_half_periods):.0f} min")
    print(f"    Demi-période max     : {max(all_half_periods):.0f} min")

if all_osc_slopes:
    pos_os = [v for v in all_osc_slopes if v > 0.1]
    neg_os = [v for v in all_osc_slopes if v < -0.1]
    print(f"\n    Slopes en oscillation:")
    print(f"    Positifs moy : {sum(pos_os)/len(pos_os):.2f} °C/h" if pos_os else "")
    print(f"    Négatifs moy : {sum(neg_os)/len(neg_os):.2f} °C/h" if neg_os else "")
    print(f"    Range typique: [{min(all_osc_slopes):.2f}, {max(all_osc_slopes):.2f}] °C/h")


# ═══════════════════════════════════════════════════════════════════════════
# 3. ANALYSE DE LA MONTÉE RAPIDE (profil de chauffe)
# ═══════════════════════════════════════════════════════════════════════════
print("\n\n" + "─" * 80)
print("3. PROFIL DE CHAUFFE - Temps pour chaque palier de +0.2°C")
print("─" * 80)

heating_starts = [
    ("Feb 26", datetime(2026, 2, 26, 13, 20, tzinfo=utc), datetime(2026, 2, 26, 14, 36, tzinfo=utc)),
    ("Feb 27", datetime(2026, 2, 27, 4, 10, tzinfo=utc), datetime(2026, 2, 27, 4, 56, tzinfo=utc)),
    ("Feb 28", datetime(2026, 2, 28, 5, 10, tzinfo=utc), datetime(2026, 2, 28, 6, 39, tzinfo=utc)),
    ("Mar 01", datetime(2026, 3, 1, 5, 8, tzinfo=utc), datetime(2026, 3, 1, 5, 54, tzinfo=utc)),
    ("Mar 02", datetime(2026, 3, 2, 14, 37, tzinfo=utc), datetime(2026, 3, 2, 15, 54, tzinfo=utc)),
]

for name, start, end in heating_starts:
    period_temps = [(ts, v) for ts, v in temp_data if start <= ts <= end]
    if len(period_temps) < 3:
        continue

    print(f"\n  [{name}] {start.strftime('%m/%d %H:%M')} → {end.strftime('%H:%M')}")

    # Show time between each 0.2°C step
    prev_ts = period_temps[0][0]
    prev_temp = period_temps[0][1]
    print(f"    {prev_ts.strftime('%H:%M:%S')} : {prev_temp:.1f}°C (départ)")

    for ts, temp in period_temps[1:]:
        dt = mins(prev_ts, ts)
        if temp > prev_temp:
            # Temperature increased
            rate = (temp - prev_temp) / (dt / 60) if dt > 0 else 0
            print(f"    {ts.strftime('%H:%M:%S')} : {temp:.1f}°C  (+{temp-prev_temp:.1f}°C en {dt:.0f}min = {rate:.1f}°C/h)")
        elif temp < prev_temp:
            print(f"    {ts.strftime('%H:%M:%S')} : {temp:.1f}°C  ({temp-prev_temp:.1f}°C en {dt:.0f}min) ← petite perte")
        prev_ts = ts
        prev_temp = temp

# ═══════════════════════════════════════════════════════════════════════════
# 4. VITESSE VTherm SLOPE vs RÉALITÉ TEMPÉRATURE
# ═══════════════════════════════════════════════════════════════════════════
print("\n\n" + "─" * 80)
print("4. COMPARAISON SLOPE VTHERM vs MONTÉE RÉELLE DE TEMPÉRATURE")
print("─" * 80)

print("\n  Le slope VTherm est un calcul interne basé sur une fenêtre glissante.")
print("  Comparons-le à la vitesse réelle mesurée entre 2 points température.")

for name, start, end in heating_starts:
    period_temps = [(ts, v) for ts, v in temp_data if start <= ts <= end]
    period_slopes = [(ts, v) for ts, v in slope_data if start <= ts <= end]

    if len(period_temps) < 2:
        continue

    # Calculate actual rate between consecutive temp readings
    print(f"\n  [{name}]")
    print(f"    {'Heure':>10} | {'Temp':>6} | {'Δt':>5} | {'Rate réelle':>12} | {'Slope VTherm':>13}")
    print(f"    {'-'*10}-+-{'-'*6}-+-{'-'*5}-+-{'-'*12}-+-{'-'*13}")

    for k in range(1, len(period_temps)):
        ts, temp = period_temps[k]
        prev_ts, prev_temp = period_temps[k-1]
        dt_min = mins(prev_ts, ts)
        if dt_min > 0 and temp != prev_temp:
            actual_rate = (temp - prev_temp) / (dt_min / 60)

            # Find closest slope reading
            closest_slope = None
            min_diff = float('inf')
            for sts, sv in period_slopes:
                diff = abs((sts - ts).total_seconds())
                if diff < min_diff:
                    min_diff = diff
                    closest_slope = sv

            slope_str = f"{closest_slope:.2f}" if closest_slope is not None else "N/A"
            print(f"    {ts.strftime('%H:%M:%S'):>10} | {temp:>5.1f}° | {dt_min:>4.0f}m | {actual_rate:>+10.2f}°/h | {slope_str:>12}")


# ═══════════════════════════════════════════════════════════════════════════
# 5. RECOMMANDATIONS FINALES
# ═══════════════════════════════════════════════════════════════════════════
print("\n\n" + "=" * 80)
print("5. RECOMMANDATIONS POUR LES CONSTANTES DE L'ALGORITHME")
print("=" * 80)

if valid_dead_times:
    med_dt = sorted(valid_dead_times)[len(valid_dead_times)//2]
    avg_dt = sum(valid_dead_times)/len(valid_dead_times)
else:
    med_dt = 10
    avg_dt = 10

avg_hp = sum(all_half_periods)/len(all_half_periods) if all_half_periods else 15

print(f"""
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                    DONNÉES MESURÉES vs CONSTANTES ALGO                     │
  ├─────────────────────────┬──────────────┬───────────────┬───────────────────┤
  │ Constante               │ Valeur actue │ Mesure réelle │ Recommandation    │
  ├─────────────────────────┼──────────────┼───────────────┼───────────────────┤
  │ DEFAULT_DEAD_TIME       │   10.0 min   │  {med_dt:>5.1f} min med│ ► {max(med_dt, 5):.0f} min           │
  │ DEAD_TIME_SAFETY_FACTOR │    1.5×      │     —         │ GARDER 1.5×       │
  │ THRESHOLD_SLOPE         │    0.10 °/h  │  p50=0.00     │ GARDER 0.10       │
  │ MAX_PROJECTION_DELTA    │    2.0°C     │  max proj=0.6 │ ► RÉDUIRE à 1.0°C │
  │ THRESHOLD_TARGET_DROP   │   -1.0°C     │ drop moy=-1.6 │ GARDER -1.0       │
  │ MIN_LIMIT_TIMEOUT       │    5.0 min   │  dt min={min(valid_dead_times) if valid_dead_times else '?':>4}  │ GARDER 5 min      │
  │ DELTA_TIME_CONTROL_LOOP │    2 min     │ ½-osc ≈{avg_hp:>3.0f}m  │ GARDER 2 min      │
  └─────────────────────────┴──────────────┴───────────────┴───────────────────┘

  JUSTIFICATIONS:

  a) DEFAULT_DEAD_TIME = {max(med_dt, 5):.0f} min
     Le dead time réel (AC→capteur) médian = {med_dt:.1f} min.
     Pour un changement de vitesse de ventilateur (effet plus subtil
     que l'allumage AC), 10 min reste approprié.

  b) MAX_PROJECTION_DELTA : 2.0 → 1.0°C
     La projection max observée est slope_max(3.58) × 10/60 = 0.60°C.
     Un clamp à 1.0°C offrirait une protection réelle (au lieu de 2.0
     qui n'est jamais atteint et ne protège de rien).

  c) THRESHOLD_SLOPE = 0.10 : CORRECT
     40% des samples sont < 0.05 (bruit), p75 = 0.15.
     Le seuil 0.10 sépare bien le bruit du signal réel.

  d) Oscillation de maintien : période ≈ {2*avg_hp:.0f} min
     Le DELTA_TIME_CONTROL_LOOP de 2 min permet de réagir à chaque
     demi-oscillation. OK pour la réactivité du fan controller.
     MAIS : les slopes en oscillation atteignent ±0.5-0.8 →
     l'algo ne doit PAS interpréter ces fluctuations comme
     des situations d'urgence (Zone A/B).

  e) Overshoot observé : +0.4°C moyen, +0.6°C max
     La Zone B (freinage) devrait se déclencher quand la projection
     dépasse target de > 0.3°C, soit slope > {0.3 * 60 / 10:.1f} °C/h en chauffe.
     Les slopes de chauffe initiale atteignent 2-3.5 → Zone B doit
     réagir agressivement quand on approche du target.
""")
