import datetime as dt
import sys
import zipfile
from collections import defaultdict

from audit_stats import load_shared_strings, load_sheets, mean, num, outcome_rate, rows_for_sheet, rows_matching


def excel_date(value):
    n = num(value)
    if n is None:
        return str(value)
    return (dt.date(1899, 12, 30) + dt.timedelta(days=int(n))).isoformat()


def pct(value):
    return "n/a" if value is None else f"{value * 100:.1f}%"


def rank_bucket(rank):
    r = num(rank)
    if r is None:
        return "missing"
    if r <= 5:
        return "01-05"
    if r <= 10:
        return "06-10"
    if r <= 20:
        return "11-20"
    if r <= 40:
        return "21-40"
    return "41+"


def by_bucket(rows, key_fn):
    groups = defaultdict(list)
    for row in rows:
        groups[key_fn(row)].append(row)
    return groups


def print_groups(title, groups):
    print(f"  {title}")
    for key in sorted(groups):
        rows = groups[key]
        if len(rows) < 3:
            continue
        print(f"    {key}: n={len(rows)} rate={pct(outcome_rate(rows))}")


def summarize(label, rows, chance_col):
    rows = [r for r in rows if num(r.get("Outcome")) is not None]
    print(f"\n### {label}")
    print(f"Rows={len(rows)} overall={pct(outcome_rate(rows))}")

    for date, day_rows in sorted(by_bucket(rows, lambda r: excel_date(r.get("Date"))).items()):
        picks = [r for r in day_rows if (r.get("Source") or "").lower() == "pick"]
        audits = [r for r in day_rows if (r.get("Source") or "").lower() == "audit"]
        top10 = [r for r in day_rows if (num(r.get("Rank")) or 999) <= 10]
        print(
            f"{date}: all {len(day_rows)} {pct(outcome_rate(day_rows))} | "
            f"audit {len(audits)} {pct(outcome_rate(audits))} | "
            f"pick {len(picks)} {pct(outcome_rate(picks))} | "
            f"top10 {len(top10)} {pct(outcome_rate(top10))} | "
            f"pick pred {pct(mean([num(r.get(chance_col)) for r in picks]))}"
        )
        if picks:
            orders = {
                key: (len(group), pct(outcome_rate(group)))
                for key, group in sorted(by_bucket(picks, lambda r: int(num(r.get("Batting Order")) or 0)).items())
            }
            print(f"  pick batting order: {orders}")
            print(
                "  picks: "
                + "; ".join(
                    f"#{int(num(r.get('Rank')) or 0)} {r.get('Player')} BO{int(num(r.get('Batting Order')) or 0)} "
                    f"{r.get('Result')} pred={float(num(r.get(chance_col)) or 0):.2f}"
                    for r in picks[:12]
                )
            )

    print_groups("rank buckets", by_bucket(rows, lambda r: rank_bucket(r.get("Rank"))))
    print_groups("batting order", by_bucket(rows, lambda r: int(num(r.get("Batting Order")) or 0)))


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: python deep_breakdown.py workbook.xlsx")

    with zipfile.ZipFile(sys.argv[1]) as zf:
        shared = load_shared_strings(zf)
        all_rows = {name: rows_for_sheet(zf, name, path, shared) for name, path in load_sheets(zf)}

    summarize("Hit", rows_matching(all_rows, "Hit Analysis"), "Chance")
    summarize("HR", rows_matching(all_rows, "HR Analysis"), "HR Chance")


if __name__ == "__main__":
    main()
