"""Time-split rolling display calibration for hit and HR audit workbooks.

The scorer's saved probability remains the ranking probability. This script
tests a monotone log-odds offset using only slates completed before each target
date, so calibration can be evaluated without look-ahead.
"""

import argparse
import datetime as dt
import math
import os
import zipfile
from collections import defaultdict

from audit_stats import (
    load_shared_strings,
    load_sheets,
    mean,
    num,
    outcome_rate,
    rows_for_sheet,
    rows_matching,
)


def excel_date(value):
    return dt.date(1899, 12, 30) + dt.timedelta(days=int(num(value)))


def clip_prob(value):
    return min(0.9999, max(0.0001, float(value)))


def apply_offset(probability, offset):
    p = clip_prob(probability)
    odds = p / (1.0 - p)
    shifted = odds * math.exp(offset)
    return shifted / (1.0 + shifted)


def natural_key(row):
    game_pk = str(row.get("Game PK") or "").strip()
    batter_id = str(row.get("Batter ID") or "").strip()
    if game_pk and batter_id:
        return (row.get("Date"), game_pk, batter_id)
    return (
        row.get("Date"),
        str(row.get("Player") or "").strip().lower(),
        str(row.get("Team") or "").strip().lower(),
        str(row.get("Pitcher") or "").strip().lower(),
        str(row.get("Venue") or "").strip().lower(),
    )


def load_audits(workbooks, label):
    deduped = {}
    for workbook in workbooks:
        with zipfile.ZipFile(workbook) as zf:
            shared = load_shared_strings(zf)
            sheets = {
                name: rows_for_sheet(zf, name, path, shared)
                for name, path in load_sheets(zf)
            }
        rows = rows_matching(sheets, f"{label} Analysis", prefer="v2")
        for row in rows:
            if (row.get("Source") or "").lower() != "audit":
                continue
            if num(row.get("Outcome")) is None:
                continue
            # Later workbook arguments intentionally win for overlapping rows.
            deduped[natural_key(row)] = row
    return list(deduped.values())


def raw_offset(rows, chance_col):
    pairs = [
        (num(row.get(chance_col)), num(row.get("Outcome")))
        for row in rows
        if num(row.get(chance_col)) is not None
        and num(row.get("Outcome")) is not None
    ]
    if not pairs:
        return 0.0
    target = sum(outcome for _, outcome in pairs)

    def residual(offset):
        return sum(apply_offset(probability, offset) for probability, _ in pairs) - target

    lo, hi = -4.0, 4.0
    for _ in range(70):
        mid = (lo + hi) / 2.0
        if residual(mid) > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0


def fitted_offset(rows, chance_col, prior_rows):
    offset = raw_offset(rows, chance_col)
    n = sum(
        1
        for row in rows
        if num(row.get(chance_col)) is not None
        and num(row.get("Outcome")) is not None
    )
    return offset * n / (n + max(0, prior_rows))


def metrics(rows, chance_col, offset=0.0):
    pairs = [
        (apply_offset(num(row.get(chance_col)), offset), num(row.get("Outcome")))
        for row in rows
        if num(row.get(chance_col)) is not None and num(row.get("Outcome")) is not None
    ]
    brier = mean([(p - y) ** 2 for p, y in pairs])
    logloss = mean([-(y * math.log(p) + (1 - y) * math.log(1 - p)) for p, y in pairs])
    return {
        "n": len(pairs),
        "pred": mean([p for p, _ in pairs]),
        "actual": mean([y for _, y in pairs]),
        "brier": brier,
        "logloss": logloss,
    }


def pct(value):
    return "n/a" if value is None else f"{value * 100:.2f}%"


def run_model(label, rows, chance_col, window, min_slates, prior_rows):
    by_date = defaultdict(list)
    for row in rows:
        by_date[excel_date(row.get("Date"))].append(row)
    dates = sorted(by_date)
    evaluated = []
    daily = []

    for index, target_date in enumerate(dates):
        prior_dates = dates[max(0, index - window):index]
        if len(prior_dates) < min_slates:
            continue
        training = [row for day in prior_dates for row in by_date[day]]
        offset = fitted_offset(training, chance_col, prior_rows)
        target_rows = by_date[target_date]
        evaluated.extend((row, offset) for row in target_rows)
        day_metrics = metrics(target_rows, chance_col, offset)
        daily.append((target_date, offset, day_metrics))

    if not evaluated:
        print(f"\n{label}: not enough slates for rolling evaluation")
        return

    raw_rows = [row for row, _ in evaluated]
    raw = metrics(raw_rows, chance_col)
    calibrated_pairs = [
        (apply_offset(num(row.get(chance_col)), offset), num(row.get("Outcome")))
        for row, offset in evaluated
    ]
    calibrated = {
        "n": len(calibrated_pairs),
        "pred": mean([p for p, _ in calibrated_pairs]),
        "actual": mean([y for _, y in calibrated_pairs]),
        "brier": mean([(p - y) ** 2 for p, y in calibrated_pairs]),
        "logloss": mean([
            -(y * math.log(p) + (1 - y) * math.log(1 - p))
            for p, y in calibrated_pairs
        ]),
    }

    latest_training_dates = dates[-window:]
    latest_training = [row for day in latest_training_dates for row in by_date[day]]
    latest_offset = fitted_offset(latest_training, chance_col, prior_rows)

    print(f"\n### {label} rolling display calibration ###")
    print(f"Unique audit rows: {len(rows)}  Slates: {len(dates)}")
    print(
        f"Out-of-sample rows: {raw['n']} | raw pred={pct(raw['pred'])} "
        f"actual={pct(raw['actual'])} brier={raw['brier']:.5f} logloss={raw['logloss']:.5f}"
    )
    print(
        f"Rolling calibrated: pred={pct(calibrated['pred'])} "
        f"actual={pct(calibrated['actual'])} brier={calibrated['brier']:.5f} "
        f"logloss={calibrated['logloss']:.5f}"
    )
    print(
        f"Latest shadow offset: {latest_offset:+.4f} "
        f"from {len(latest_training_dates)} prior slates; do not deploy without forward validation"
    )
    print("Recent targets:")
    for target_date, offset, result in daily[-8:]:
        print(
            f"  {target_date} offset={offset:+.4f} "
            f"pred={pct(result['pred'])} actual={pct(result['actual'])} n={result['n']}"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workbooks", nargs="+", help="XLSX files in chronological order")
    parser.add_argument("--window", type=int, default=20, help="prior slates in each rolling fit")
    parser.add_argument("--min-slates", type=int, default=10)
    parser.add_argument("--prior-rows", type=int, default=1000, help="shrink offset toward zero")
    args = parser.parse_args()

    missing = [path for path in args.workbooks if not os.path.exists(path)]
    if missing:
        raise SystemExit(f"missing workbook: {missing[0]}")

    hit_rows = load_audits(args.workbooks, "Hit")
    hr_rows = load_audits(args.workbooks, "HR")
    run_model("Hit", hit_rows, "Chance", args.window, args.min_slates, args.prior_rows)
    run_model("HR", hr_rows, "HR Chance", args.window, args.min_slates, args.prior_rows)


if __name__ == "__main__":
    main()
