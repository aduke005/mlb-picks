import math
import sys
from collections import defaultdict

from analyze_xlsx import iter_rows, load_shared_strings, load_sheets
import zipfile


def num(value):
    try:
        if value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def pct(n):
    return "n/a" if n is None else f"{n * 100:.1f}%"


def mean(values):
    vals = [v for v in values if v is not None and math.isfinite(v)]
    return None if not vals else sum(vals) / len(vals)


def outcome_rate(rows):
    vals = [num(r.get("Outcome")) for r in rows if num(r.get("Outcome")) is not None]
    return None if not vals else sum(vals) / len(vals)


def bucket_chance(v, step):
    if v is None:
        return "missing"
    lo = math.floor(v / step) * step
    hi = lo + step
    return f"{lo:.2f}-{hi:.2f}"


def rank_bucket(v):
    if v is None:
        return "missing"
    if v <= 5:
        return "001-005"
    if v <= 10:
        return "006-010"
    if v <= 20:
        return "011-020"
    if v <= 40:
        return "021-040"
    if v <= 80:
        return "041-080"
    return "081+"


def rows_for_sheet(zf, sheet_name, path, shared):
    rows = [r for r in iter_rows(zf, path, shared) if any(str(v).strip() for v in r)]
    if not rows:
        return []
    header = [str(v).strip() for v in rows[0]]
    out = []
    for row in rows[1:]:
        possible_header = [str(v).strip() for v in row]
        if possible_header[:2] == ["Source", "Date"]:
            header = possible_header
            continue
        item = {header[i]: row[i] if i < len(row) else "" for i in range(len(header))}
        item["_sheet"] = sheet_name
        if item.get("Date") == "Date":
            continue
        if item.get("Player") in ("", "Player"):
            continue
        out.append(item)
    return out


def print_grouped(title, groups, min_count=8):
    print(f"\n{title}")
    for key, rows in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if len(rows) < min_count:
            continue
        print(f"{key:>12}  n={len(rows):4d}  rate={pct(outcome_rate(rows))}")


def quantile(values, proportion):
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    return vals[round((len(vals) - 1) * proportion)]


def factor_edges(rows, factors):
    print("\nFactor edges, top quartile vs bottom quartile")
    for factor in factors:
        vals = [(num(r.get(factor)), r) for r in rows if num(r.get(factor)) is not None]
        if len(vals) < 40:
            continue
        low_cut = quantile([value for value, _ in vals], 0.25)
        high_cut = quantile([value for value, _ in vals], 0.75)
        if low_cut is None or high_cut is None or math.isclose(low_cut, high_cut):
            print(f"{factor:18} no usable variation")
            continue
        low = [r for value, r in vals if value <= low_cut]
        high = [r for value, r in vals if value >= high_cut]
        print(
            f"{factor:18} low avg={mean([num(r.get(factor)) for r in low]):.3f} "
            f"rate={pct(outcome_rate(low))} | high avg={mean([num(r.get(factor)) for r in high]):.3f} "
            f"rate={pct(outcome_rate(high))}"
        )


def source_analysis(label, rows, chance_col, factors, include_rank=True):
    if not rows:
        return
    print(f"\n\n### {label} ###")
    print(f"Rows: {len(rows)}  Outcome rate: {pct(outcome_rate(rows))}")
    by_rank_bucket = defaultdict(list)
    by_chance = defaultdict(list)
    by_hand = defaultdict(list)
    by_order = defaultdict(list)
    by_capture_run = defaultdict(list)
    for r in rows:
        if include_rank:
            rank = num(r.get("Rank"))
            by_rank_bucket[rank_bucket(rank)].append(r)
        by_chance[bucket_chance(num(r.get(chance_col)), 0.02 if chance_col.startswith("HR") else 0.05)].append(r)
        by_hand[r.get("Pitcher Hand") or "missing"].append(r)
        order = num(r.get("Batting Order"))
        by_order[int(order) if order else "missing"].append(r)
        capture_run = num(r.get("Capture Run"))
        if capture_run is not None:
            by_capture_run[int(capture_run)].append(r)

    if include_rank:
        print_grouped("By rank bucket", by_rank_bucket)
    print_grouped("By chance bucket", by_chance)
    print_grouped("By pitcher hand", by_hand)
    print_grouped("By batting order", by_order)
    if by_capture_run:
        print_grouped("By capture run", by_capture_run, min_count=1)
    factor_edges(rows, factors)

    print("\nCalibration")
    cal_groups = defaultdict(list)
    for r in rows:
        cal_groups[bucket_chance(num(r.get(chance_col)), 0.02 if chance_col.startswith("HR") else 0.05)].append(r)
    for key, group in sorted(cal_groups.items()):
        if len(group) < 5:
            continue
        avg_chance = mean([num(r.get(chance_col)) for r in group])
        print(f"{key:>12}  n={len(group):4d}  pred={pct(avg_chance)}  actual={pct(outcome_rate(group))}")


def truthy(value):
    numeric = num(value)
    if numeric is not None:
        return math.isfinite(numeric) and numeric != 0
    return str(value).strip().lower() in {"true", "yes"}


def model_selection(row):
    selected_by = str(row.get("Selected By") or "").strip().lower()
    if selected_by in {"current", "classic", "both"}:
        return selected_by
    in_current = truthy(row.get("In Current Top"))
    in_classic = truthy(row.get("In Classic Top"))
    if in_current and in_classic:
        return "both"
    if in_current:
        return "current"
    if in_classic:
        return "classic"
    return "unknown"


def dedupe_prediction_rows(rows):
    seen = set()
    unique = []
    duplicates = 0
    for row in rows:
        prediction_id = str(row.get("Prediction ID") or "").strip()
        game_pk = str(row.get("Game PK") or "").strip()
        batter_id = str(row.get("Batter ID") or "").strip()
        if game_pk and batter_id:
            # Stable across re-exports even when the export generated a new ID.
            identity = ("game-batter", game_pk, batter_id)
        else:
            # Legacy exports predate stable game/batter IDs. Pitcher and venue
            # keep doubleheaders and same-name players distinct.
            natural = (
                str(row.get("Date") or "").strip(),
                str(row.get("Player") or "").strip().lower(),
                str(row.get("Team") or "").strip().lower(),
                str(row.get("Pitcher") or "").strip().lower(),
                str(row.get("Venue") or "").strip().lower(),
            )
            if all(natural):
                identity = ("legacy", *natural)
            elif prediction_id:
                identity = ("prediction", prediction_id)
            else:
                # Do not collapse incomplete rows that lack a usable identity.
                identity = ("row", len(unique) + duplicates)
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        unique.append(row)
    return unique, duplicates


def model_comparison(picks):
    has_metadata = any(
        model_selection(row) != "unknown"
        or num(row.get("Current Rank")) is not None
        or num(row.get("Classic Rank")) is not None
        for row in picks
    )
    if not has_metadata:
        print("\nCurrent vs classic: metadata not available in these saved rows")
        return

    groups = defaultdict(list)
    current_ranks = defaultdict(list)
    classic_ranks = defaultdict(list)
    for row in picks:
        groups[model_selection(row)].append(row)
        current_rank = num(row.get("Current Rank"))
        classic_rank = num(row.get("Classic Rank"))
        if current_rank is not None:
            current_ranks[rank_bucket(current_rank)].append(row)
        if classic_rank is not None:
            classic_ranks[rank_bucket(classic_rank)].append(row)

    print("\nCurrent vs classic saved-pick results")
    for key in ("current", "classic", "both", "unknown"):
        group = groups.get(key, [])
        if not group:
            continue
        print(
            f"{key:>12}  n={len(group):4d}  rate={pct(outcome_rate(group))} "
            f"current rank pred={pct(mean([num(r.get('Rank Probability')) or num(r.get('Current Chance')) for r in group]))} "
            f"current display={pct(mean([num(r.get('Display Probability')) or num(r.get('Current Chance')) or num(r.get('Chance')) or num(r.get('HR Chance')) for r in group]))} "
            f"classic rank pred={pct(mean([num(r.get('Classic Rank Probability')) or num(r.get('Classic Chance')) for r in group]))}"
        )

    print_grouped("Saved picks by current-model rank", current_ranks, min_count=1)
    print_grouped("Saved picks by classic-model rank", classic_ranks, min_count=1)


def fmt_mean(rows, field):
    value = mean([num(r.get(field)) for r in rows])
    return "n/a" if value is None else f"{value:.3f}"


HIT_RECENCY_ARMS = (
    ("A current 0.15", "In Current Top", "Rank Probability"),
    ("B 0.05 unshrunk", "In Recent 0.05 Unshrunk Top", "Recent 0.05 Unshrunk Chance"),
    ("C 0.15 shrunk", "In Recent 0.15 Shrunk Top", "Recent 0.15 Shrunk Chance"),
    ("D zero recent", "In Recent 0 Top", "Recent 0 Chance"),
)


def brier_score(rows, chance_col):
    pairs = [
        (num(row.get(chance_col)), num(row.get("Outcome")))
        for row in rows
        if num(row.get(chance_col)) is not None
        and num(row.get("Outcome")) is not None
    ]
    return mean([(probability - outcome) ** 2 for probability, outcome in pairs])


def fmt_number(value, places):
    return "n/a" if value is None else f"{value:.{places}f}"


def has_value(value):
    return value is not None and str(value).strip() != ""


def hit_recency_arm_comparison(picks, shadows):
    tracked, _ = dedupe_prediction_rows([*picks, *shadows])
    membership_fields = [membership for _, membership, _ in HIT_RECENCY_ARMS]
    eligible = [
        row for row in tracked
        if all(has_value(row.get(field)) for field in membership_fields)
    ]

    print("\nHit recent-form arm comparison")
    print("All arms use the same pure top-10 policy; Arm A is the displayed list.")
    if not eligible:
        print("No completed rows with explicit arm membership yet.")
        return

    arm_rows = {
        label: [row for row in eligible if truthy(row.get(membership))]
        for label, membership, _ in HIT_RECENCY_ARMS
    }
    arm_chances = {label: chance for label, _, chance in HIT_RECENCY_ARMS}

    for label, _, chance_col in HIT_RECENCY_ARMS:
        group = arm_rows[label]
        dates = {str(row.get("Date") or "") for row in group}
        predicted = mean([num(row.get(chance_col)) for row in group])
        brier = brier_score(group, chance_col)
        picks_per_slate = len(group) / len(dates) if dates else None
        prefix = (
            f"{label:18} n={len(group):4d} slates={len(dates):3d} "
            f"picks/slate={picks_per_slate:.1f} " if picks_per_slate is not None else
            f"{label:18} n={len(group):4d} slates={len(dates):3d} picks/slate=n/a "
        )
        result = (
            f"pred={pct(predicted)} actual={pct(outcome_rate(group))} "
            f"brier={brier:.5f}" if brier is not None else
            f"pred={pct(predicted)} actual={pct(outcome_rate(group))} brier=n/a"
        )
        print(prefix + result)

    base_label = HIT_RECENCY_ARMS[0][0]
    base_by_date = defaultdict(list)
    for row in arm_rows[base_label]:
        base_by_date[str(row.get("Date") or "")].append(row)

    print("Paired slate-level differences versus Arm A (positive hit delta / Brier gain is better)")
    for label, _, chance_col in HIT_RECENCY_ARMS[1:]:
        comparison_by_date = defaultdict(list)
        for row in arm_rows[label]:
            comparison_by_date[str(row.get("Date") or "")].append(row)
        common_dates = sorted(set(base_by_date) & set(comparison_by_date))
        hit_deltas = []
        brier_gains = []
        for date in common_dates:
            base_rate = outcome_rate(base_by_date[date])
            comparison_rate = outcome_rate(comparison_by_date[date])
            base_brier = brier_score(base_by_date[date], arm_chances[base_label])
            comparison_brier = brier_score(comparison_by_date[date], chance_col)
            if base_rate is not None and comparison_rate is not None:
                hit_deltas.append(comparison_rate - base_rate)
            if base_brier is not None and comparison_brier is not None:
                brier_gains.append(base_brier - comparison_brier)
        print(
            f"{label:18} common slates={len(common_dates):3d} "
            f"hit delta={pct(mean(hit_deltas))} brier gain={fmt_number(mean(brier_gains), 5)}"
        )


def analyze(label, rows, chance_col, factors, compare_hit_recency=False):
    print(f"\n\n##### {label} #####")
    rows = [r for r in rows if num(r.get("Outcome")) is not None]
    raw_picks = [r for r in rows if (r.get("Source") or "").lower() == "pick"]
    raw_shadows = [r for r in rows if (r.get("Source") or "").lower() == "shadow"]
    picks, pick_duplicate_count = dedupe_prediction_rows(raw_picks)
    shadows, shadow_duplicate_count = dedupe_prediction_rows(raw_shadows)
    duplicate_count = pick_duplicate_count + shadow_duplicate_count
    if duplicate_count:
        rows = [
            r for r in rows
            if (r.get("Source") or "").lower() not in {"pick", "shadow"}
        ] + picks + shadows
    print(f"All source rows: {len(rows)}  Pooled outcome rate: {pct(outcome_rate(rows))}")
    if duplicate_count:
        print(f"Removed repeated saved predictions by stable game/batter identity: {duplicate_count}")

    by_source = defaultdict(list)
    for row in rows:
        by_source[(row.get("Source") or "missing").lower()].append(row)
    print_grouped("By source", by_source, min_count=1)

    audits = [r for r in rows if (r.get("Source") or "").lower() == "audit"]
    source_analysis("Full-slate audit rows", audits, chance_col, factors)
    source_analysis("Saved pick rows (capture-run rank)", picks, chance_col, factors)
    source_analysis("Shadow/counterfactual rows", shadows, chance_col, factors, include_rank=False)
    if picks:
        model_comparison(picks)
    if compare_hit_recency:
        hit_recency_arm_comparison(picks, shadows)

    if picks and audits:
        print("\nPick vs audit factor means")
        for factor in [chance_col, *factors]:
            print(
                f"{factor:18} pick={fmt_mean(picks, factor)} "
                f"audit={fmt_mean(audits, factor)}"
            )


def rows_matching(all_rows, label, prefer=None):
    label_lower = label.lower()
    if label_lower.startswith("hit"):
        names = [
            name for name, rows in all_rows.items()
            if any("Chance" in row and "HR Chance" not in row for row in rows)
        ]
    elif label_lower.startswith("hr"):
        names = [
            name for name, rows in all_rows.items()
            if any("HR Chance" in row for row in rows)
        ]
    else:
        names = [name for name in all_rows if label_lower in name.lower()]
    if prefer:
        preferred = [name for name in names if prefer.lower() in name.lower()]
        if preferred:
            names = preferred
    return [row for name in names for row in all_rows[name]]


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: python audit_stats.py workbook.xlsx")
    with zipfile.ZipFile(sys.argv[1]) as zf:
        shared = load_shared_strings(zf)
        all_rows = {}
        for name, path in load_sheets(zf):
            all_rows[name] = rows_for_sheet(zf, name, path, shared)

    hr_rows = rows_matching(all_rows, "HR Analysis", prefer="v2")
    hit_rows = rows_matching(all_rows, "Hit Analysis", prefer="v2")

    analyze(
        "HR combined",
        hr_rows,
        "HR Chance",
        ["Single-trip HR Rate", "Power Adj", "Pitcher Risk", "Road Adj", "TTO Adj", "Game Env", "Park", "Recent Adj", "Contact Adj", "Handedness"],
    )
    analyze(
        "Hit combined",
        hit_rows,
        "Chance",
        ["Single-trip Hit Rate", "Pitcher Weakness", "Handedness", "Road Adj", "TTO Adj", "Game Env", "Park", "Recent Adj", "Contact Adj", "OPS Adj"],
        compare_hit_recency=True,
    )


if __name__ == "__main__":
    main()
