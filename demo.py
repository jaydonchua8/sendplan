"""A one-minute walk through the planner on a small synthetic shop.

    python demo.py

Everything printed here comes from shopsim.py. No real shop, creator or API
is involved.
"""
import datetime as dt
import json
import os
from collections import Counter

import bench
from capacity import parse_capacity, parse_schedule
from ledger import reconcile
from quota import QUOTA_SEQUENCE, Policy, plan_day
from sendplan import Book
from shopsim import FIRST_RUN, TRAIN, Shop

SMALL = TRAIN._replace(n=3000, limits=(None, 300, 90, 60), shop_daily_limit=400,
                       other_tc=(20, 120), outside=(0, 4))
SCHEDULE = {d + "_maxCreators": 300 for d in ("Monday", "Tuesday", "Wednesday",
                                               "Thursday", "Friday")}
SCHEDULE.update(Saturday_maxCreators=80, Sunday_maxCreators=80)
NAMES = {(0, 1): "Lv 0-1", (2,): "Lv 2", (3,): "Lv 3", (4, 5, 6, 7): "Lv 4-7", None: "no unit"}


def show_tiers(cap, now):
    for level in (0, 2, 3, 4):
        group, rem = cap.remaining_for(level, now)
        print("   %-7s remaining this week: %s" % (NAMES[group], "no ceiling" if rem == float("inf") else rem))


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    print("1. Reading sample_capacity.json (OutreachCapacityResponse shape, synthetic)")
    with open(os.path.join(here, "sample_capacity.json")) as f:
        cap = parse_capacity(json.load(f))
    monday = dt.datetime(2026, 11, 2, 20, 0, tzinfo=dt.timezone.utc)
    print("   daily pool used: %d, quota resets in %d daily runs" % (cap.daily_used, cap.days_left(monday)))
    show_tiers(cap, monday)

    print("\n2. One day on a synthetic shop of %d creators" % SMALL.n)
    shop = Shop(SMALL, 42)
    book = Book(sequence=QUOTA_SEQUENCE)
    for handle, score, email, level in shop.rows:
        book.add(handle, score, email, level)
    print("   %d import rows became %d creator records, %d rejected"
          % (len(shop.rows), len(book.creators), len(book.rejected)))
    sched = parse_schedule(SCHEDULE)
    policy = Policy(shop_daily_limit=SMALL.shop_daily_limit)
    now = FIRST_RUN + dt.timedelta(days=1)             # Monday
    cap = parse_capacity(shop.capacity(now))
    show_tiers(cap, now)
    for name, pol in (("blind", policy._replace(quota_aware=False)), ("quota-aware", policy)):
        plan = plan_day(book, cap, sched, now, pol)
        by = Counter(NAMES[s.group] if s.units else "no unit" for s in plan.sends)
        print("   %-11s plan: %d sends, quota units by group %s, counters %s"
              % (name, len(plan.sends), dict(sorted((k, v) for k, v in by.items() if k != "no unit")),
                 dict(plan.skipped)))
    pages = shop.submit(plan.sends, now)
    print("   ledger: %d page(s), outcomes %s" % (len(pages), {k: v for k, v in pages[0]["summary"].items() if v}))
    reasons = Counter(r["skip_reason"] for p in pages for r in p["data"] if r["skip_reason"])
    print("   skip reasons: %s" % dict(reasons))
    print("   reconcile: %s" % dict(reconcile(book, plan, pages, now, cap, policy)))
    print("   reconcile again with the same pages: %s" % dict(reconcile(book, plan, pages, now, cap, policy)))

    print("\n3. Four weeks, same shop settings, seed 42")
    print("   %-32s %9s %8s %8s %14s" % ("planner", "delivered", "refused", "replies", "expected score"))
    for name, mode, pol in (("v1: flat cap, assumes delivered", "v1", None),
                            ("blind: flat cap, reads ledger", "blind", Policy(quota_aware=False)),
                            ("quota-aware", "aware", Policy())):
        r = bench.run(SMALL, 42, mode, pol, schedule=SCHEDULE)
        print("   %-32s %9d %8d %8d %14.1f" % (name, r["delivered"], r["refused"], r["replies"], r["expected_score"]))


if __name__ == "__main__":
    main()
