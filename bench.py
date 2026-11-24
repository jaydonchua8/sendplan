"""Measure the planners on the synthetic shop in shopsim.py.

Tunes three policy settings on TRAIN (seeds 101-103), then reports every
planner on HELDOUT, which uses different generator settings and seeds
901-903 and was not looked at while tuning. Prints the tables quoted in
README.md and writes bench_results.json, including every per-seed run.
All data is synthetic.

    python bench.py            48 simulated months, roughly one CPU-second each
    python bench.py --resume   reuse the runs already in bench_results.json
"""
import datetime as dt
import json
import os
import sys

from capacity import parse_capacity, parse_schedule
from ledger import reconcile
from quota import QUOTA_SEQUENCE, Policy, plan_day
from sendplan import Book
from shopsim import FIRST_RUN, HELDOUT, TRAIN, Shop

SCHEDULE = {"Monday_maxCreators": 2000, "Tuesday_maxCreators": 2000,
            "Wednesday_maxCreators": 2000, "Thursday_maxCreators": 2000,
            "Friday_maxCreators": 2000, "Saturday_maxCreators": 500,
            "Sunday_maxCreators": 500, "start_time": "20:00"}
DAYS = 28
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench_results.json")
TRAIN_SEEDS, HELDOUT_SEEDS = (101, 102, 103), (901, 902, 903)


def run(settings, seed, mode, policy=None, days=DAYS, schedule=SCHEDULE):
    """One simulated month. mode "v1" is the first planner as I shipped it:
    flat daily cap and every planned send assumed delivered. Any other mode
    uses plan_day() with the given policy and reconciles against the ledger."""
    shop = Shop(settings, seed)
    book = Book(sequence=QUOTA_SEQUENCE)
    for handle, score, email, level in shop.rows:
        book.add(handle, score, email, level)
    sched = parse_schedule(schedule)
    if policy is not None:
        policy = policy._replace(shop_daily_limit=settings.shop_daily_limit)
    follow = on_time = 0
    for d in range(days):
        now = FIRST_RUN + dt.timedelta(days=d)
        day = now.date().toordinal()
        cap = parse_capacity(shop.capacity(now))
        if mode == "v1":
            n = sched.cap(now.date())
            sends = book.plan(day, {"tc": n, "dm": n, "email": n}, n)
            shop.submit(sends, now)
            book.apply(sends, day)
        else:
            plan = plan_day(book, cap, sched, now, policy)
            sends = plan.sends
            reconcile(book, plan, shop.submit(sends, now), now, cap, policy)
        for s in sends:
            if s.kind == "followup":
                follow += 1
                on_time += s.due == day
        for handle in shop.pop_replies():
            book.stop(handle)
    shop.close(FIRST_RUN + dt.timedelta(days=days))
    st = shop.stats
    return {"attempts": st["attempts"], "delivered": st["delivered"],
            "refused": st["refused"],
            "refused_quota": st["refused:creator_tier_quota_exhausted"],
            "replies": st["replies"], "replies_by_group": list(shop.replies_by_group),
            "replies_lv2_up": sum(shop.replies_by_group[1:]),
            "reply_score": round(shop.reply_score, 2),
            "expected_score": round(shop.expected_score, 2),
            "unused_units": sum(shop.unused), "unused_by_group": list(shop.unused),
            "units": st["units"], "repeated_step": st["repeated_step"],
            "sent_after_reply": st["sent_after_reply"],
            "followups_planned": follow,
            "followups_on_time_pct": round(100.0 * on_time / max(follow, 1), 1)}


class Runs:
    """Per-seed results, kept in bench_results.json so a stopped benchmark
    can pick up where it left off with --resume."""

    def __init__(self, resume):
        self.runs = {}
        if resume and os.path.exists(OUT):
            with open(OUT) as f:
                self.runs = json.load(f).get("runs", {})
        self.fresh = 0

    def mean(self, name, settings, seeds, mode, policy=None):
        rs = []
        for seed in seeds:
            key = "%s seed=%d %s %s" % (name, seed, mode, label(policy) if policy else "-")
            if key not in self.runs:
                self.runs[key] = run(settings, seed, mode, policy)
                self.fresh += 1
                self.save({})
            rs.append(self.runs[key])
        out = {}
        for k in rs[0]:
            if isinstance(rs[0][k], list):
                out[k] = [round(sum(r[k][i] for r in rs) / len(rs), 1)
                          for i in range(len(rs[0][k]))]
            else:
                out[k] = round(sum(r[k] for r in rs) / len(rs), 1)
        out["expected_score_range"] = [min(r["expected_score"] for r in rs),
                                       max(r["expected_score"] for r in rs)]
        return out

    def save(self, summary):
        with open(OUT + ".tmp", "w") as f:
            json.dump(dict(summary, runs=self.runs), f, indent=1, sort_keys=True)
        os.replace(OUT + ".tmp", OUT)


def grid():
    for margin in (0.0, 0.1):
        for paid, reserve in (("first", 0.0), ("first", 1.0), ("last", 0.0),
                              ("last", 1.0), ("never", 0.0)):
            yield Policy(margin=margin, reserve=reserve, paid_followups=paid)


def label(p):
    if not p.quota_aware:
        return "quota_aware=False"
    return "margin=%.2f paid_followups=%s reserve=%.1f" % (p.margin, p.paid_followups, p.reserve)


ROW = "| %s | %d | %d | %d | %d | %.1f | %.1f | %d |"
HEAD = ("| planner | delivered | refused | replies | replies Lv 2+ | reply score | "
        "expected score | quota units unused |\n|---|---|---|---|---|---|---|---|")


def row(name, r):
    return ROW % (name, r["delivered"], r["refused"], r["replies"], r["replies_lv2_up"],
                  r["reply_score"], r["expected_score"], r["unused_units"])


def main():
    runs = Runs("--resume" in sys.argv)
    out = {"note": "synthetic data; tables are means over 3 seeds", "days": DAYS,
           "schedule": SCHEDULE, "train_seeds": TRAIN_SEEDS,
           "heldout_seeds": HELDOUT_SEEDS, "train": {}, "heldout": {}, "grid": []}

    print("1. Tuning on TRAIN (%d creators, seeds %s). Objective: expected reply score."
          % (TRAIN.n, TRAIN_SEEDS))
    best = None
    for p in grid():
        r = runs.mean("train", TRAIN, TRAIN_SEEDS, "aware", p)
        out["grid"].append({"policy": label(p), "expected_score": r["expected_score"],
                            "expected_score_range": r["expected_score_range"],
                            "reply_score": r["reply_score"], "refused": r["refused"],
                            "refused_quota": r["refused_quota"]})
        print("   %-48s expected %6.1f (seeds %.1f to %.1f)  realised %6.1f  refused %6.1f"
              % ((label(p), r["expected_score"]) + tuple(r["expected_score_range"])
                 + (r["reply_score"], r["refused"])))
        sys.stdout.flush()
        if best is None or r["expected_score"] > best[1]["expected_score"]:
            best = (p, r)
    tuned = best[0]
    out["tuned_policy"] = label(tuned)
    print("   picked: %s\n" % label(tuned))

    planners = (("v1: flat cap, assumes delivered", "v1", None),
                ("blind: flat cap, reads ledger", "blind", Policy(quota_aware=False)),
                ("quota-aware, default policy", "aware", Policy()),
                ("quota-aware, tuned on TRAIN", "aware", tuned))
    for title, settings, seeds, key in (("2. TRAIN", TRAIN, TRAIN_SEEDS, "train"),
                                        ("3. HELDOUT", HELDOUT, HELDOUT_SEEDS, "heldout")):
        print("%s (%d creators, %d days, mean of seeds %s)" % (title, settings.n, DAYS, seeds))
        print(HEAD)
        for name, mode, policy in planners:
            r = runs.mean(key, settings, seeds, mode, policy)
            out[key][name] = r
            print(row(name, r))
            sys.stdout.flush()
        print()
    print("%d runs simulated now, %d reused" % (runs.fresh, len(runs.runs) - runs.fresh))
    runs.save(out)


if __name__ == "__main__":
    main()
