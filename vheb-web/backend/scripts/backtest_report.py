#!/usr/bin/env python3
"""
Print a walk-forward backtest of the pricing model against whatever
history is currently in the Google Sheet -- the same thing the app's
Settings page shows, but handy from the command line.

Usage:
    python scripts/backtest_report.py --margin 7
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import billing_service


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--margin", type=float, default=None, help="Margin %% to test (default: Settings tab's default_margin_pct)")
    args = ap.parse_args()

    report = billing_service.get_backtest(args.margin)
    print(f"Backtested {report.months_tested} months")
    print(f"Never a loss: {report.never_a_loss}")
    print(f"Worst margin achieved: {report.worst_margin_pct}%")
    print(f"Average margin achieved: {report.average_margin_pct}%")
    print(f"Mean absolute error: Rs {report.mean_absolute_error}/unit")
    print()
    print(f"{'month':8} {'suggested':10} {'actual':10} {'covered?':9} {'margin %'}")
    for d in report.details:
        print(f"{d['month']:8} {d['suggested_price']:<10} {d['actual_cost_per_unit']:<10} "
              f"{str(d['would_have_covered_cost']):9} {d['margin_pct_achieved']}")


if __name__ == "__main__":
    main()
