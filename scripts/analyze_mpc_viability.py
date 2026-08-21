#!/usr/bin/env python3
"""Analyse approfondie de la viabilité du MPC shadow vs algo de prod.

Charge les données CSV du dossier data/ et produit:
- Statistiques globales sur les décisions live vs MPC shadow
- Analyse de la réactivité (temps pour atteindre la consigne)
- Distribution des vitesses de ventilateur
- Épisodes de divergence live/MPC
- Évaluation de l'agressivité du MPC
"""
import csv
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
MAIN_CSV = os.path.join(DATA_DIR, "vtherm_mpc_fan_data_01KK6PQT.csv")

FAN_ORDER = ["silent", "low", "med", "high", "superhigh"]


def fan_rank(mode: str) -> int:
    try:
        return FAN_ORDER.index(mode)
    except ValueError:
        return -1


def load_main_data():
    rows = []
    with open(MAIN_CSV, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                row["_ts"] = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                row["_current_temp"] = float(row["current_temp"])
                row["_target_temp"] = float(row["target_temp"])
                row["_current_error"] = float(row["current_error"])
                row["_effective_slope"] = float(row["effective_slope"])
                row["_vtherm_slope"] = float(row["vtherm_slope"])
                row["_minutes_since_change"] = float(row["minutes_since_change"])
                row["_mpc_shadow_cost"] = float(row["mpc_shadow_cost"]) if row.get("mpc_shadow_cost") else None
                row["_mpc_shadow_confidence"] = float(row["mpc_shadow_confidence"]) if row.get("mpc_shadow_confidence") else None
                row["_mpc_shadow_temp_10m"] = float(row["mpc_shadow_temp_10m"]) if row.get("mpc_shadow_temp_10m") else None
                row["_mpc_shadow_temp_30m"] = float(row["mpc_shadow_temp_30m"]) if row.get("mpc_shadow_temp_30m") else None
                row["_projected_temp"] = float(row["projected_temp"]) if row.get("projected_temp") else None
                row["_projected_error"] = float(row["projected_error"]) if row.get("projected_error") else None
            except (ValueError, KeyError):
                continue
            rows.append(row)
    return rows


def load_slope_data():
    slopes = {}
    for fname in os.listdir(DATA_DIR):
        if fname.startswith("mpc_") and fname.endswith("_slope.csv"):
            mode = fname.removeprefix("mpc_").removesuffix("_slope.csv")
            entries = []
            with open(os.path.join(DATA_DIR, fname), newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        entries.append({
                            "ts": datetime.fromisoformat(row["last_changed"].replace("Z", "+00:00")),
                            "slope": float(row["state"]),
                        })
                    except (ValueError, KeyError):
                        continue
            slopes[mode] = entries
    return slopes


def print_separator(title=""):
    print(f"\n{'=' * 70}")
    if title:
        print(f"  {title}")
        print(f"{'=' * 70}")


def analyze_basic_stats(rows):
    print_separator("1. STATISTIQUES GÉNÉRALES")
    ts_start = rows[0]["_ts"]
    ts_end = rows[-1]["_ts"]
    duration_h = (ts_end - ts_start).total_seconds() / 3600
    print(f"  Période      : {ts_start.strftime('%Y-%m-%d %H:%M')} → {ts_end.strftime('%Y-%m-%d %H:%M')}")
    print(f"  Durée         : {duration_h:.1f} h ({duration_h / 24:.1f} jours)")
    print(f"  Nombre points : {len(rows)}")
    print(f"  Intervalle    : ~{(duration_h * 60) / len(rows):.1f} min")

    hvac_modes = Counter(r["hvac_mode"] for r in rows)
    print(f"  Modes HVAC    : {dict(hvac_modes)}")

    phases = Counter(r["phase"] for r in rows)
    print(f"  Phases        : {dict(phases)}")

    temps = [r["_current_temp"] for r in rows]
    targets = list(set(r["_target_temp"] for r in rows))
    print(f"  Temp range    : {min(temps):.1f}°C → {max(temps):.1f}°C")
    print(f"  Consignes     : {sorted(targets)}")


def analyze_fan_distribution(rows):
    print_separator("2. DISTRIBUTION DES VITESSES")

    live_fans = Counter(r["current_fan"] for r in rows)
    mpc_fans = Counter(r["mpc_shadow_fan"] for r in rows if r.get("mpc_shadow_fan"))
    decided_fans = Counter(r["decided_fan"] for r in rows)

    print("  Ventilateur actuel (current_fan):")
    total = sum(live_fans.values())
    for mode in FAN_ORDER:
        cnt = live_fans.get(mode, 0)
        pct = (cnt / total * 100) if total else 0
        bar = "█" * int(pct / 2)
        print(f"    {mode:12s}: {cnt:5d} ({pct:5.1f}%) {bar}")

    print("\n  Décision live (decided_fan):")
    total = sum(decided_fans.values())
    for mode in FAN_ORDER:
        cnt = decided_fans.get(mode, 0)
        pct = (cnt / total * 100) if total else 0
        bar = "█" * int(pct / 2)
        print(f"    {mode:12s}: {cnt:5d} ({pct:5.1f}%) {bar}")

    print("\n  Recommandation MPC shadow (mpc_shadow_fan):")
    total = sum(mpc_fans.values())
    for mode in FAN_ORDER:
        cnt = mpc_fans.get(mode, 0)
        pct = (cnt / total * 100) if total else 0
        bar = "█" * int(pct / 2)
        print(f"    {mode:12s}: {cnt:5d} ({pct:5.1f}%) {bar}")

    # Aggressiveness index: mean rank
    live_mean_rank = sum(fan_rank(r["current_fan"]) for r in rows if fan_rank(r["current_fan"]) >= 0) / max(1, len(rows))
    mpc_mean_rank = sum(fan_rank(r["mpc_shadow_fan"]) for r in rows if r.get("mpc_shadow_fan") and fan_rank(r["mpc_shadow_fan"]) >= 0) / max(1, sum(1 for r in rows if r.get("mpc_shadow_fan")))
    decided_mean_rank = sum(fan_rank(r["decided_fan"]) for r in rows if fan_rank(r["decided_fan"]) >= 0) / max(1, len(rows))

    print(f"\n  Rang moyen vitesse (0=silent, 4=superhigh):")
    print(f"    Actuel       : {live_mean_rank:.2f}")
    print(f"    Décidé       : {decided_mean_rank:.2f}")
    print(f"    MPC shadow   : {mpc_mean_rank:.2f}")


def analyze_agreement(rows):
    print_separator("3. CONCORDANCE LIVE vs MPC SHADOW")

    match_counter = Counter(r.get("mpc_shadow_match", "n/a") for r in rows)
    total_valid = match_counter.get("yes", 0) + match_counter.get("no", 0)
    print(f"  Correspondances : {match_counter}")
    if total_valid > 0:
        print(f"  Taux d'accord  : {match_counter.get('yes', 0) / total_valid * 100:.1f}%")

    would_change = Counter(r.get("mpc_shadow_would_change", "n/a") for r in rows)
    print(f"  MPC voudrait changer: {would_change}")

    # When MPC disagrees, what direction?
    disagree_higher = 0
    disagree_lower = 0
    disagree_details = defaultdict(int)
    for r in rows:
        if r.get("mpc_shadow_match") == "no":
            live_rank = fan_rank(r["decided_fan"])
            mpc_rank = fan_rank(r.get("mpc_shadow_fan", ""))
            if mpc_rank > live_rank:
                disagree_higher += 1
            elif mpc_rank < live_rank:
                disagree_lower += 1
            disagree_details[f"{r['decided_fan']}→{r.get('mpc_shadow_fan', '?')}"] += 1

    print(f"\n  Quand le MPC désaccorde:")
    print(f"    MPC recommande PLUS haut : {disagree_higher}")
    print(f"    MPC recommande PLUS bas  : {disagree_lower}")
    print(f"    Détails des divergences:")
    for k, v in sorted(disagree_details.items(), key=lambda x: -x[1])[:10]:
        print(f"      {k}: {v} fois")


def analyze_reactivity(rows):
    print_separator("4. ANALYSE DE RÉACTIVITÉ / AGRESSIVITÉ")

    # Find episodes where error > 0.3 and track time to reach target
    episodes = []
    current_episode = None

    for i, r in enumerate(rows):
        error = r["_current_error"]
        if error > 0.3 and current_episode is None:
            current_episode = {
                "start_idx": i,
                "start_ts": r["_ts"],
                "start_temp": r["_current_temp"],
                "target_temp": r["_target_temp"],
                "max_error": error,
                "fan_modes_live": [],
                "fan_modes_mpc": [],
                "errors": [],
            }
        elif current_episode is not None:
            current_episode["max_error"] = max(current_episode["max_error"], error)
            current_episode["errors"].append(error)
            current_episode["fan_modes_live"].append(r["current_fan"])
            current_episode["fan_modes_mpc"].append(r.get("mpc_shadow_fan", "?"))

            if error <= 0.05 or i == len(rows) - 1:
                current_episode["end_idx"] = i
                current_episode["end_ts"] = r["_ts"]
                current_episode["duration_min"] = (r["_ts"] - current_episode["start_ts"]).total_seconds() / 60
                episodes.append(current_episode)
                current_episode = None

    print(f"  Nombre d'épisodes de rattrapage (error > 0.3°C) : {len(episodes)}")
    if episodes:
        durations = [e["duration_min"] for e in episodes]
        print(f"  Durée min/moy/max : {min(durations):.0f} / {sum(durations)/len(durations):.0f} / {max(durations):.0f} min")

        for j, ep in enumerate(episodes):
            print(f"\n  --- Épisode {j+1} ---")
            print(f"    Début       : {ep['start_ts'].strftime('%Y-%m-%d %H:%M')}")
            print(f"    Durée       : {ep['duration_min']:.0f} min")
            print(f"    Temp départ : {ep['start_temp']:.1f}°C → consigne {ep['target_temp']:.1f}°C")
            print(f"    Erreur max  : {ep['max_error']:.2f}°C")

            live_counter = Counter(ep["fan_modes_live"])
            mpc_counter = Counter(ep["fan_modes_mpc"])
            live_mean = sum(fan_rank(m) for m in ep["fan_modes_live"] if fan_rank(m) >= 0) / max(1, len(ep["fan_modes_live"]))
            mpc_mean = sum(fan_rank(m) for m in ep["fan_modes_mpc"] if fan_rank(m) >= 0) / max(1, len(ep["fan_modes_mpc"]))

            print(f"    Vitesses live : {dict(live_counter)} (rang moy={live_mean:.2f})")
            print(f"    Vitesses MPC  : {dict(mpc_counter)} (rang moy={mpc_mean:.2f})")

            if mpc_mean > live_mean:
                print(f"    → MPC est PLUS agressif (+{mpc_mean - live_mean:.2f} rangs)")
            elif mpc_mean < live_mean:
                print(f"    → MPC est MOINS agressif ({mpc_mean - live_mean:.2f} rangs)")
            else:
                print(f"    → MPC et live ont la même agressivité")


def analyze_comfort_quality(rows):
    print_separator("5. QUALITÉ DE CONFORT")

    errors = [r["_current_error"] for r in rows]
    abs_errors = [abs(e) for e in errors]

    print(f"  Erreur moyenne         : {sum(errors)/len(errors):+.3f}°C")
    print(f"  Erreur absolue moyenne : {sum(abs_errors)/len(abs_errors):.3f}°C")
    print(f"  Erreur max             : {max(errors):.3f}°C")
    print(f"  Erreur min             : {min(errors):.3f}°C")

    in_deadband = sum(1 for e in abs_errors if e <= 0.2)
    in_soft = sum(1 for e in errors if e > 0.3)
    in_hard = sum(1 for e in errors if e > 0.6)
    print(f"\n  Temps dans deadband (|err|≤0.2)  : {in_deadband / len(rows) * 100:.1f}%")
    print(f"  Temps en soft error (err>0.3)     : {in_soft / len(rows) * 100:.1f}%")
    print(f"  Temps en hard error (err>0.6)     : {in_hard / len(rows) * 100:.1f}%")

    # Fan change frequency
    changes = 0
    for i in range(1, len(rows)):
        if rows[i]["current_fan"] != rows[i-1]["current_fan"]:
            changes += 1
    duration_h = (rows[-1]["_ts"] - rows[0]["_ts"]).total_seconds() / 3600
    print(f"\n  Changements de vitesse : {changes} sur {duration_h:.1f}h ({changes / duration_h:.1f}/h)")


def analyze_mpc_predictions_accuracy(rows):
    print_separator("6. PRÉCISION DES PRÉDICTIONS MPC")

    # Compare mpc_shadow_temp_10m predictions with actual temperature 10 min later
    prediction_errors_10m = []
    prediction_errors_30m = []

    for i, r in enumerate(rows):
        if r["_mpc_shadow_temp_10m"] is None:
            continue
        # Find the actual temp ~10 min later
        target_ts_10 = r["_ts"].timestamp() + 600
        target_ts_30 = r["_ts"].timestamp() + 1800
        for j in range(i + 1, len(rows)):
            dt = rows[j]["_ts"].timestamp() - r["_ts"].timestamp()
            if abs(dt - 600) < 150:  # within 2.5 min of 10 min
                pred_err = r["_mpc_shadow_temp_10m"] - rows[j]["_current_temp"]
                prediction_errors_10m.append(pred_err)
                break
        if r["_mpc_shadow_temp_30m"] is not None:
            for j in range(i + 1, len(rows)):
                dt = rows[j]["_ts"].timestamp() - r["_ts"].timestamp()
                if abs(dt - 1800) < 150:
                    pred_err = r["_mpc_shadow_temp_30m"] - rows[j]["_current_temp"]
                    prediction_errors_30m.append(pred_err)
                    break

    if prediction_errors_10m:
        abs_errs = [abs(e) for e in prediction_errors_10m]
        print(f"  Prédictions à T+10min ({len(prediction_errors_10m)} comparaisons):")
        print(f"    Erreur moyenne      : {sum(prediction_errors_10m)/len(prediction_errors_10m):+.3f}°C")
        print(f"    Erreur abs moyenne  : {sum(abs_errs)/len(abs_errs):.3f}°C")
        print(f"    Erreur abs max      : {max(abs_errs):.3f}°C")
        print(f"    Erreur abs p90      : {sorted(abs_errs)[int(0.9*len(abs_errs))]:.3f}°C")
    else:
        print("  Aucune donnée de prédiction T+10m disponible")

    if prediction_errors_30m:
        abs_errs = [abs(e) for e in prediction_errors_30m]
        print(f"\n  Prédictions à T+30min ({len(prediction_errors_30m)} comparaisons):")
        print(f"    Erreur moyenne      : {sum(prediction_errors_30m)/len(prediction_errors_30m):+.3f}°C")
        print(f"    Erreur abs moyenne  : {sum(abs_errs)/len(abs_errs):.3f}°C")
        print(f"    Erreur abs max      : {max(abs_errs):.3f}°C")
        print(f"    Erreur abs p90      : {sorted(abs_errs)[int(0.9*len(abs_errs))]:.3f}°C")
    else:
        print("  Aucune donnée de prédiction T+30m disponible")


def analyze_slopes(slopes):
    print_separator("7. PROFILS DE PENTE EFFECTIVE PAR MODE")

    for mode in FAN_ORDER:
        if mode not in slopes or not slopes[mode]:
            print(f"  {mode:12s}: pas de données")
            continue
        vals = [e["slope"] for e in slopes[mode]]
        mean_val = sum(vals) / len(vals)
        min_val = min(vals)
        max_val = max(vals)
        print(f"  {mode:12s}: n={len(vals):4d}  moy={mean_val:+.3f}°C/h  min={min_val:+.3f}  max={max_val:+.3f}")


def analyze_time_in_error_by_fan(rows):
    print_separator("8. TEMPS EN ERREUR PAR VITESSE DE VENTILATEUR")

    mode_errors = defaultdict(list)
    for r in rows:
        mode_errors[r["current_fan"]].append(r["_current_error"])

    for mode in FAN_ORDER:
        if mode not in mode_errors:
            continue
        errs = mode_errors[mode]
        mean_err = sum(errs) / len(errs)
        pct_above_soft = sum(1 for e in errs if e > 0.3) / len(errs) * 100
        pct_above_hard = sum(1 for e in errs if e > 0.6) / len(errs) * 100
        print(f"  {mode:12s}: n={len(errs):5d}  err_moy={mean_err:+.3f}  soft%={pct_above_soft:5.1f}  hard%={pct_above_hard:5.1f}")


def analyze_mpc_vs_live_detail(rows):
    print_separator("9. PÉRIODES DE DIVERGENCE MAJEURE (MPC ≠ LIVE, >5 min)")

    divergence_start = None
    divergences = []

    for i, r in enumerate(rows):
        is_divergent = r.get("mpc_shadow_match") == "no"
        if is_divergent and divergence_start is None:
            divergence_start = i
        elif not is_divergent and divergence_start is not None:
            duration = (rows[i]["_ts"] - rows[divergence_start]["_ts"]).total_seconds() / 60
            if duration > 5:
                segment = rows[divergence_start:i]
                live_ranks = [fan_rank(r["decided_fan"]) for r in segment]
                mpc_ranks = [fan_rank(r.get("mpc_shadow_fan", "")) for r in segment if fan_rank(r.get("mpc_shadow_fan", "")) >= 0]
                divergences.append({
                    "start": rows[divergence_start]["_ts"],
                    "duration": duration,
                    "live_mean": sum(live_ranks) / max(len(live_ranks), 1),
                    "mpc_mean": sum(mpc_ranks) / max(len(mpc_ranks), 1),
                    "mean_error": sum(r["_current_error"] for r in segment) / len(segment),
                    "max_error": max(r["_current_error"] for r in segment),
                    "sample_reasons": list(set(r.get("reason", "") for r in segment))[:3],
                })
            divergence_start = None

    print(f"  Nombre de périodes de divergence (>5 min) : {len(divergences)}")
    for j, d in enumerate(divergences[:15]):  # top 15
        direction = "MPC PLUS haut" if d["mpc_mean"] > d["live_mean"] else "MPC PLUS bas"
        print(f"\n  --- Divergence {j+1} ---")
        print(f"    Début      : {d['start'].strftime('%Y-%m-%d %H:%M')}")
        print(f"    Durée      : {d['duration']:.0f} min")
        print(f"    Live rang  : {d['live_mean']:.2f}  vs  MPC rang : {d['mpc_mean']:.2f}  ({direction})")
        print(f"    Erreur moy : {d['mean_error']:+.3f}°C  max : {d['max_error']:+.3f}°C")
        print(f"    Raisons    : {d['sample_reasons'][:2]}")


def analyze_decision_reasons(rows):
    print_separator("10. DISTRIBUTION DES RAISONS DE DÉCISION")

    # Group reasons by prefix
    reason_groups = defaultdict(int)
    for r in rows:
        reason = r.get("reason", "Unknown")
        # Take the prefix before ':'
        prefix = reason.split(":")[0].strip() if ":" in reason else reason
        reason_groups[prefix] += 1

    total = sum(reason_groups.values())
    for k, v in sorted(reason_groups.items(), key=lambda x: -x[1]):
        print(f"    {k:40s}: {v:5d} ({v/total*100:5.1f}%)")


def analyze_hysteresis_blocking(rows):
    print_separator("11. ANALYSE DE L'HYSTÉRÉSIS / BLOCAGES MPC")

    # Count how often MPC is blocked by min_interval vs hysteresis
    min_interval_blocks = 0
    hysteresis_blocks = 0
    step_down_holds = 0
    other_blocks = 0

    for r in rows:
        if r.get("mpc_shadow_would_change") != "no":
            continue
        reason = r.get("mpc_shadow_reason", "")  # this doesn't exist, use mpc_shadow_match
        # The reason is embedded in mpc_shadow fields... we don't have it directly
        # But we can infer from mpc_shadow_match and would_change

    # Instead, analyze: when MPC says different fan but would_change=no
    blocked_count = 0
    for r in rows:
        mpc_fan = r.get("mpc_shadow_fan", "")
        live_fan = r.get("decided_fan", "")
        would_change = r.get("mpc_shadow_would_change", "n/a")
        if mpc_fan != live_fan and would_change == "no":
            blocked_count += 1

    agrees_and_no_change = sum(1 for r in rows if r.get("mpc_shadow_match") == "yes")
    disagrees = sum(1 for r in rows if r.get("mpc_shadow_match") == "no")
    would_change_yes = sum(1 for r in rows if r.get("mpc_shadow_would_change") == "yes")
    would_change_no = sum(1 for r in rows if r.get("mpc_shadow_would_change") == "no")

    print(f"  MPC agree avec live  : {agrees_and_no_change}")
    print(f"  MPC désaccorde       : {disagrees}")
    print(f"  MPC voudrait changer : {would_change_yes}")
    print(f"  MPC bloqué           : {would_change_no}")
    print(f"  MPC fan ≠ live et bloqué : {blocked_count}")


def analyze_speed_rampup_delay(rows):
    print_separator("12. ANALYSE DU DÉLAI DE MONTÉE EN VITESSE")

    # When error starts growing, how fast does the controller ramp up?
    ramp_events = []
    i = 0
    while i < len(rows) - 1:
        r = rows[i]
        # Detect moment where error crosses above 0.3
        if r["_current_error"] <= 0.3 and i + 1 < len(rows) and rows[i+1]["_current_error"] > 0.3:
            start_fan = r["current_fan"]
            start_rank = fan_rank(start_fan)
            start_ts = r["_ts"]
            # Track how long until fan goes up
            max_rank_reached = start_rank
            time_to_first_increase = None
            for j in range(i + 1, min(i + 60, len(rows))):  # look ahead ~2h
                jr = rows[j]
                jr_rank = fan_rank(jr["current_fan"])
                if jr_rank > start_rank and time_to_first_increase is None:
                    time_to_first_increase = (jr["_ts"] - start_ts).total_seconds() / 60
                max_rank_reached = max(max_rank_reached, jr_rank)
            ramp_events.append({
                "ts": start_ts,
                "start_fan": start_fan,
                "time_to_increase_min": time_to_first_increase,
                "max_rank_delta": max_rank_reached - start_rank,
                "error_at_start": rows[i+1]["_current_error"],
            })
        i += 1

    print(f"  Nombre d'événements de montée d'erreur : {len(ramp_events)}")
    if ramp_events:
        increased = [e for e in ramp_events if e["time_to_increase_min"] is not None]
        no_increase = [e for e in ramp_events if e["time_to_increase_min"] is None]
        print(f"  Ayant provoqué une augmentation de vitesse : {len(increased)}")
        print(f"  Sans augmentation (dans 2h)                : {len(no_increase)}")

        if increased:
            times = [e["time_to_increase_min"] for e in increased]
            print(f"  Délai première augmentation : min={min(times):.0f} moy={sum(times)/len(times):.0f} max={max(times):.0f} min")

        for j, ev in enumerate(ramp_events[:10]):
            delay_str = f"{ev['time_to_increase_min']:.0f} min" if ev['time_to_increase_min'] else "jamais"
            print(f"    [{ev['ts'].strftime('%m-%d %H:%M')}] fan={ev['start_fan']} err={ev['error_at_start']:.2f} → augmenté après {delay_str} (Δrank={ev['max_rank_delta']})")


def main():
    print("=" * 70)
    print("  ANALYSE DE VIABILITÉ MPC - VTherm MPC Fan")
    print("=" * 70)

    rows = load_main_data()
    slopes = load_slope_data()

    if not rows:
        print("ERREUR: Aucune donnée chargée")
        sys.exit(1)

    analyze_basic_stats(rows)
    analyze_fan_distribution(rows)
    analyze_agreement(rows)
    analyze_reactivity(rows)
    analyze_comfort_quality(rows)
    analyze_mpc_predictions_accuracy(rows)
    analyze_slopes(slopes)
    analyze_time_in_error_by_fan(rows)
    analyze_mpc_vs_live_detail(rows)
    analyze_decision_reasons(rows)
    analyze_hysteresis_blocking(rows)
    analyze_speed_rampup_delay(rows)

    print_separator("CONCLUSION PRÉLIMINAIRE")
    # Quick assessment
    mpc_rows = [r for r in rows if r.get("mpc_shadow_fan")]
    if mpc_rows:
        mpc_avg_rank = sum(fan_rank(r["mpc_shadow_fan"]) for r in mpc_rows if fan_rank(r["mpc_shadow_fan"]) >= 0) / len(mpc_rows)
        live_avg_rank = sum(fan_rank(r["current_fan"]) for r in rows if fan_rank(r["current_fan"]) >= 0) / len(rows)
        delta = mpc_avg_rank - live_avg_rank
        if abs(delta) < 0.1:
            print(f"  Le MPC shadow et l'algo live ont une agressivité similaire (Δ={delta:+.2f} rangs)")
            print(f"  → Le MPC ne résoudra probablement PAS le manque de réactivité")
        elif delta > 0:
            print(f"  Le MPC shadow est PLUS agressif que le live (Δ={delta:+.2f} rangs)")
            print(f"  → Le MPC pourrait améliorer la réactivité")
        else:
            print(f"  Le MPC shadow est MOINS agressif que le live (Δ={delta:+.2f} rangs)")
            print(f"  → Le MPC aggraverait le manque de réactivité")

    print()


if __name__ == "__main__":
    main()
