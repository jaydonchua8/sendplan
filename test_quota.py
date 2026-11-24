"""Tests for capacity.py, quota.py, ledger.py and the synthetic shop."""
import copy
import datetime as dt
import json
import math
import os
import unittest

from capacity import CapacityError, parse_capacity, parse_schedule
from ledger import LedgerError, parse_ledger, reconcile
from quota import (QUOTA_SEQUENCE, Policy, SequenceError, plan_day,
                   validate_sequence)
from sendplan import Book, Step
from shopsim import FIRST_RUN, TRAIN, Shop
import bench

UTC = dt.timezone.utc
HERE = os.path.dirname(os.path.abspath(__file__))
MON = dt.datetime(2026, 11, 2, 20, 0, tzinfo=UTC)        # a Monday
EVERY_DAY = parse_schedule({d + "_maxCreators": 100 for d in
                            ("Monday", "Tuesday", "Wednesday", "Thursday",
                             "Friday", "Saturday", "Sunday")})


def sample():
    with open(os.path.join(HERE, "sample_capacity.json")) as f:
        return json.load(f)


def cap_with(lv4=3, lv2=50, **top):
    d = sample()
    d["daily_creators_used"] = 0
    for t in d["outreach_quota"]["tiers"]:
        if t["creator_levels"] == [4, 5, 6, 7]:
            t.update(standard_limit=500, standard_used=500 - lv4, remaining=lv4)
        if t["creator_levels"] == [2]:
            t.update(standard_limit=2500, standard_used=2500 - lv2, remaining=lv2)
    d.update(top)
    return d


def book_of(levels, **kw):
    b = Book(sequence=QUOTA_SEQUENCE)
    for i, lv in enumerate(levels):
        b.add("creator_%02d" % i, 0.9 - i / 100.0, kw.get("email"), lv)
    return b


def ledger_for(plan, outcome="invite_submitted", reason=None, respell=False):
    rows = []
    for s in plan.sends:
        o = outcome
        if o == "invite_submitted" and s.channel != "tc":
            o = "outreach_sent"
        rows.append({"creator_handle": ("@" + s.handle.upper()) if respell else s.handle,
                     "outcome": o, "status": "x", "detail": "x",
                     "skip_reason": reason, "invitation_id": None})
    return {"data": rows, "summary": {}, "pagination": {}}


class CapacityParsing(unittest.TestCase):
    def test_sample_payload(self):
        c = parse_capacity(sample())
        self.assertEqual(c.daily_used, 400)
        self.assertEqual(c.remaining_for(0, MON), ((0, 1), math.inf))
        self.assertEqual(c.remaining_for(2, MON), ((2,), 1400))
        self.assertEqual(c.remaining_for(3, MON), ((3,), 480))
        self.assertEqual(c.remaining_for(6, MON), ((4, 5, 6, 7), 290))
        self.assertEqual(c.days_left(MON), 6)

    def test_tiered_shop_ignores_top_level_unlimited(self):
        # the top level says unlimited=true, the tier says 290 left
        c = parse_capacity(sample())
        self.assertTrue(sample()["outreach_quota"]["unlimited"])
        self.assertEqual(c.remaining_for(4, MON)[1], 290)

    def test_null_quota_is_unknown_not_zero(self):
        d = sample()
        d["outreach_quota"] = None
        c = parse_capacity(d)
        self.assertEqual(c.remaining_for(4, MON), ("unknown", None))
        self.assertIsNone(c.period(MON))

    def test_untiered_limit_zero_is_a_real_zero_and_null_is_unlimited(self):
        d = sample()
        q = d["outreach_quota"]
        q.update(tiered=False, tiers=None, unlimited=False, limit=0, used=0, remaining=0)
        self.assertEqual(parse_capacity(d).remaining_for(4, MON), ("shop", 0))
        q.update(limit=None, remaining=None)
        self.assertEqual(parse_capacity(d).remaining_for(4, MON), ("shop", math.inf))
        q.update(limit=200, used=150, remaining=None)
        self.assertEqual(parse_capacity(d).remaining_for(4, MON), ("shop", 50))

    def test_disagreeing_numbers_take_the_smaller(self):
        d = sample()
        d["outreach_quota"]["tiers"][3].update(standard_limit=500, standard_used=450,
                                               remaining=290)
        self.assertEqual(parse_capacity(d).remaining_for(4, MON)[1], 50)

    def test_tier_with_null_remaining_is_unknown(self):
        d = sample()
        d["outreach_quota"]["tiers"][3]["remaining"] = None
        self.assertEqual(parse_capacity(d).remaining_for(4, MON), ((4, 5, 6, 7), None))

    def test_level_outside_every_tier_and_missing_tiers_are_unknown(self):
        d = sample()
        self.assertEqual(parse_capacity(d).remaining_for(9, MON), ("unknown", None))
        self.assertEqual(parse_capacity(d).remaining_for(None, MON), ("unknown", None))
        d["outreach_quota"]["tiers"] = None
        self.assertEqual(parse_capacity(d).remaining_for(2, MON), ("unknown", None))

    def test_stale_snapshot_is_unknown(self):
        c = parse_capacity(sample())
        later = dt.datetime(2026, 11, 9, 20, 0, tzinfo=UTC)   # after resets_at
        self.assertTrue(c.stale(later))
        self.assertEqual(c.remaining_for(2, later), ("unknown", None))

    def test_malformed_payloads_raise(self):
        def broken(f):
            d = sample()
            f(d)
            return d
        cases = [
            lambda d: d.pop("daily_creators_used"),
            lambda d: d.update(daily_creators_used=-1),
            lambda d: d.update(daily_creators_used=True),
            lambda d: d.update(daily_creators_used="400"),
            lambda d: d.update(daily_limit_throttled="no"),
            lambda d: d.update(daily_window_resets_at="tomorrow"),
            lambda d: d["outreach_quota"].update(quota_type="MONTHLY"),
            lambda d: d["outreach_quota"].pop("resets_at"),
            lambda d: d["outreach_quota"].update(tiers="none"),
            lambda d: d["outreach_quota"]["tiers"][0].update(creator_levels=[]),
            lambda d: d["outreach_quota"]["tiers"][1].update(creator_levels=[2, 3]),
            lambda d: d["outreach_quota"]["tiers"][1].pop("remaining"),
        ]
        for i, f in enumerate(cases):
            with self.assertRaises(CapacityError, msg="case %d" % i):
                parse_capacity(broken(f))
        with self.assertRaises(CapacityError):
            parse_capacity([])

    def test_schedule(self):
        s = parse_schedule({"Monday_maxCreators": 7, "start_time": "09:30",
                            "end_time": "17:00", "timezone": "America/Chicago"})
        self.assertEqual(s.cap(MON.date()), 7)
        self.assertEqual(s.cap(MON.date() + dt.timedelta(days=1)), 0)
        for bad in ({"Monday_maxCreators": 10001}, {"Monday_maxCreators": -1},
                    {"Monday_maxCreators": 1.5}, {"Mon_maxCreators": 5},
                    {"start_time": "25:00"}, {"start_time": "9am"}):
            with self.assertRaises(CapacityError, msg=bad):
                parse_schedule(bad)
        with self.assertRaises(CapacityError):
            parse_schedule(None)


class QuotaPlanning(unittest.TestCase):
    def test_tier_budget_holds_and_unlimited_levels_fill_the_rest(self):
        b = book_of([4] * 10 + [0] * 5)
        p = plan_day(b, parse_capacity(cap_with(lv4=3)), EVERY_DAY, MON)
        lv4 = [s for s in p.sends if s.group == (4, 5, 6, 7)]
        self.assertEqual([s.key for s in lv4], ["creator_00", "creator_01", "creator_02"])
        self.assertTrue(all(s.units == 1 for s in lv4))
        self.assertEqual(len(p.sends), 8)
        self.assertEqual(p.skipped["tier_quota"], 7)
        self.assertEqual(sum(s.units for s in p.sends), 3)

    def test_blind_policy_ignores_the_quota(self):
        b = book_of([4] * 10)
        p = plan_day(b, parse_capacity(cap_with(lv4=3)), EVERY_DAY, MON,
                     Policy(quota_aware=False))
        self.assertEqual(len(p.sends), 10)

    def test_followup_is_free_in_the_same_period_and_paid_after_reset(self):
        b = book_of([4, 4])
        cap = parse_capacity(cap_with(lv4=2))
        p = plan_day(b, cap, EVERY_DAY, MON)
        reconcile(b, p, ledger_for(p), MON, cap, Policy())
        thu = MON + dt.timedelta(days=3)                   # same period
        cap0 = parse_capacity(cap_with(lv4=0))
        p2 = plan_day(b, cap0, EVERY_DAY, thu)
        self.assertEqual([(s.channel, s.units) for s in p2.sends], [("dm", 0), ("dm", 0)])
        # skip Thursday; the DM is still due after Sunday's reset
        nxt = cap_with(lv4=1)
        nxt["outreach_quota"]["resets_at"] = "2026-11-15T19:00:00Z"
        mon2 = MON + dt.timedelta(days=7)
        p3 = plan_day(b, parse_capacity(nxt), EVERY_DAY, mon2)
        self.assertEqual([(s.channel, s.units) for s in p3.sends], [("dm", 1)])
        self.assertEqual(p3.skipped["tier_quota"], 1)

    def test_margin_holds_units_back(self):
        b = book_of([2] * 60)
        p = plan_day(b, parse_capacity(cap_with(lv2=50)), EVERY_DAY, MON, Policy(margin=0.1))
        self.assertEqual(len(p.sends), 45)

    def _mid_sequence_book(self):
        # 4 creators got the invite last Friday (previous period). Their DM is
        # due today for two of them and on Wednesday for the other two.
        b = book_of([2] * 10)
        fri = (MON - dt.timedelta(days=3)).date().toordinal()
        for i in range(4):
            c = b.creators["creator_%02d" % i]
            c.update(step=1, last=fri if i < 2 else fri + 2, dm=1,
                     charged="2026-11-01T19:00:00+00:00")
        return b

    def test_reserve_keeps_units_for_followups_due_later_this_period(self):
        b = self._mid_sequence_book()
        cap = parse_capacity(cap_with(lv2=5))
        kinds = lambda p: sorted(s.kind for s in p.sends)
        self.assertEqual(kinds(plan_day(b, cap, EVERY_DAY, MON)),
                         ["followup"] * 2 + ["new"] * 3)
        self.assertEqual(kinds(plan_day(b, cap, EVERY_DAY, MON, Policy(reserve=1.0))),
                         ["followup"] * 2 + ["new"])

    def test_paid_followups_last_and_never(self):
        b = self._mid_sequence_book()
        cap = parse_capacity(cap_with(lv2=5))
        kinds = lambda p: [s.kind for s in p.sends]
        self.assertEqual(kinds(plan_day(b, cap, EVERY_DAY, MON, Policy(paid_followups="last"))),
                         ["new"] * 5)
        p = plan_day(b, cap, EVERY_DAY, MON, Policy(paid_followups="never"))
        self.assertEqual(kinds(p), ["new"] * 5)
        self.assertEqual(p.skipped["paid_followup_off"], 2)
        with self.assertRaises(ValueError):
            plan_day(b, cap, EVERY_DAY, MON, Policy(paid_followups="sometimes"))

    def test_dm_limit_moves_on_to_email(self):
        b = book_of([0, 0], email="a@example.test")
        b.creators["creator_01"]["email"] = None
        fri = (MON - dt.timedelta(days=3)).date().toordinal()
        for c in b.creators.values():
            c.update(step=1, last=fri, dm=5)
        p = plan_day(b, parse_capacity(cap_with()), EVERY_DAY, MON)
        self.assertEqual([(s.key, s.channel, s.step) for s in p.sends],
                         [("creator_00", "email", 3)])

    def test_daily_pool_limits_invites_but_not_dms(self):
        b = book_of([0] * 6)
        fri = (MON - dt.timedelta(days=3)).date().toordinal()
        for i in range(2):
            b.creators["creator_%02d" % i].update(step=1, last=fri, dm=1)
        cap = parse_capacity(cap_with(daily_creators_used=997))
        p = plan_day(b, cap, EVERY_DAY, MON, Policy(shop_daily_limit=1000))
        self.assertEqual(sorted(s.channel for s in p.sends), ["dm", "dm", "tc", "tc", "tc"])
        self.assertEqual(p.skipped["daily_tc_pool"], 1)

    def test_throttle_flags(self):
        b = book_of([0, 4])
        p = plan_day(b, parse_capacity(cap_with(daily_limit_throttled=True)), EVERY_DAY, MON)
        self.assertEqual(p.sends, [])
        self.assertTrue(any("daily_limit_throttled" in w for w in p.warnings))
        p = plan_day(b, parse_capacity(cap_with(weekly_limit_throttled=True)), EVERY_DAY, MON)
        self.assertEqual([s.key for s in p.sends], ["creator_00"])   # Lv 0 has no ceiling
        self.assertEqual(p.skipped["weekly_throttled"], 1)

    def test_unknown_quota_policies(self):
        b = book_of([4] * 5)
        d = cap_with()
        d["outreach_quota"] = None
        cap = parse_capacity(d)
        p = plan_day(b, cap, EVERY_DAY, MON)
        self.assertEqual(len(p.sends), 5)                 # null is not zero
        self.assertEqual(p.skipped["planned_without_quota"], 5)
        self.assertTrue(any("null" in w for w in p.warnings))
        p = plan_day(b, cap, EVERY_DAY, MON, Policy(unknown="hold"))
        self.assertEqual(p.sends, [])
        self.assertEqual(p.skipped["quota_unknown"], 5)

    def test_stale_snapshot_warns_and_does_not_trust_old_numbers(self):
        b = book_of([4] * 5)
        later = MON + dt.timedelta(days=7)
        p = plan_day(b, parse_capacity(cap_with(lv4=3)), EVERY_DAY, later,
                     Policy(unknown="hold"))
        self.assertEqual(p.sends, [])
        self.assertTrue(any("finished period" in w for w in p.warnings))

    def test_creator_without_level_in_a_tiered_shop(self):
        b = book_of([None, 0])
        cap = parse_capacity(cap_with())
        self.assertEqual(len(plan_day(b, cap, EVERY_DAY, MON).sends), 2)
        p = plan_day(b, cap, EVERY_DAY, MON, Policy(unknown="hold"))
        self.assertEqual([s.key for s in p.sends], ["creator_01"])

    def test_weekday_cap(self):
        b = book_of([0] * 5)
        sched = parse_schedule({"Monday_maxCreators": 2})
        cap = parse_capacity(cap_with())
        self.assertEqual(len(plan_day(b, cap, sched, MON).sends), 2)
        self.assertEqual(plan_day(b, cap, sched, MON + dt.timedelta(days=1)).sends, [])

    def test_plan_is_pure_and_deterministic(self):
        b = book_of([4, 2, 0, 3, None] * 4)
        cap = parse_capacity(cap_with())
        before = b.dumps()
        self.assertEqual(plan_day(b, cap, EVERY_DAY, MON), plan_day(b, cap, EVERY_DAY, MON))
        self.assertEqual(b.dumps(), before)

    def test_sequence_validation(self):
        validate_sequence(QUOTA_SEQUENCE)
        bad = [[Step("dm", 0), Step("tc", 1)],
               [Step("tc", 0)] + [Step("dm", 1)] * 4,
               [Step("sms", 0)], [Step("tc", -1)], [Step("tc", 1.5)]]
        for seq in bad:
            with self.assertRaises(SequenceError, msg=seq):
                validate_sequence(seq)
        with self.assertRaises(SequenceError):
            validate_sequence(QUOTA_SEQUENCE, initial_messages=6)


class Reconcile(unittest.TestCase):
    def setUp(self):
        self.cap = parse_capacity(cap_with(lv4=5))
        self.b = book_of([4, 4, 4])
        self.p = plan_day(self.b, self.cap, EVERY_DAY, MON)

    def rec(self, pages, policy=Policy(), now=MON):
        return reconcile(self.b, self.p, pages, now, self.cap, policy)

    def test_success_advances_and_is_idempotent(self):
        pages = ledger_for(self.p, respell=True)     # ledger spells handles differently
        self.assertEqual(self.rec(pages)["sent"], 3)
        c = self.b.creators["creator_00"]
        self.assertEqual((c["step"], c["dm"], c["charged"]),
                         (1, 1, "2026-11-08T19:00:00+00:00"))
        snapshot = self.b.dumps()
        again = self.rec(pages)
        self.assertEqual(again["already_reconciled"], 3)
        self.assertNotIn("sent", again)
        self.assertEqual(self.b.dumps(), snapshot)

    def test_missing_row_stays_pending(self):
        pages = ledger_for(self.p)
        pages["data"] = pages["data"][:1]
        r = self.rec(pages)
        self.assertEqual((r["sent"], r["missing"]), (1, 2))
        tue = MON + dt.timedelta(days=1)
        again = plan_day(self.b, self.cap, EVERY_DAY, tue)
        self.assertEqual([s.key for s in again.sends], ["creator_01", "creator_02"])

    def test_not_found_stops_the_creator(self):
        self.rec(ledger_for(self.p, "skipped", "creator_not_found"))
        self.assertEqual(self.b.creators["creator_00"]["stopped"], "creator_not_found")
        self.assertEqual(plan_day(self.b, self.cap, EVERY_DAY, MON + dt.timedelta(days=1)).sends, [])

    def test_quota_refusal_waits_for_the_reset(self):
        r = self.rec(ledger_for(self.p, "skipped", "creator_tier_quota_exhausted"))
        self.assertEqual(r["held_until_reset"], 3)
        self.assertEqual(self.b.creators["creator_00"]["tries"], 0)
        sat = MON + dt.timedelta(days=5)
        p = plan_day(self.b, self.cap, EVERY_DAY, sat)
        self.assertEqual((p.sends, p.skipped["held_until_reset"]), ([], 3))
        nxt = cap_with(lv4=5)
        nxt["outreach_quota"]["resets_at"] = "2026-11-15T19:00:00Z"
        sun = dt.datetime(2026, 11, 8, 20, 0, tzinfo=UTC)
        self.assertEqual(len(plan_day(self.b, parse_capacity(nxt), EVERY_DAY, sun).sends), 3)

    def test_daily_limit_refusal_retries_tomorrow(self):
        self.rec(ledger_for(self.p, "skipped", "blocked_by_outreach_limit"))
        tue = MON + dt.timedelta(days=1)
        self.assertEqual(len(plan_day(self.b, self.cap, EVERY_DAY, tue).sends), 3)

    def test_previously_invited_advances_without_a_charge(self):
        self.rec(ledger_for(self.p, "previously_invited"))
        c = self.b.creators["creator_00"]
        self.assertEqual((c["step"], c["charged"], c["dm"]), (1, None, 0))

    def test_five_message_limit_closes_dm_steps(self):
        b = book_of([0], email="a@example.test")
        fri = (MON - dt.timedelta(days=3)).date().toordinal()
        b.creators["creator_00"].update(step=1, last=fri, dm=1)
        p = plan_day(b, self.cap, EVERY_DAY, MON)
        r = reconcile(b, p, ledger_for(p, "skipped", "five_message_limit_hit"),
                      MON, self.cap, Policy())
        self.assertEqual(r["dm_closed"], 1)
        nxt = plan_day(b, self.cap, EVERY_DAY, MON + dt.timedelta(days=1))
        self.assertEqual([(s.channel, s.step) for s in nxt.sends], [("email", 3)])

    def test_unknown_codes_retry_then_give_up(self):
        now = MON
        for attempt in range(3):
            p = plan_day(self.b, self.cap, EVERY_DAY, now)
            self.assertEqual(len(p.sends), 3, attempt)
            r = reconcile(self.b, p, ledger_for(p, "skipped", "a_code_added_later"),
                          now, self.cap, Policy())
            self.assertEqual((r["retry"], r["unrecognised_code"]), (3, 3))
            now += dt.timedelta(days=1)
        self.assertEqual(r["gave_up"], 3)
        self.assertEqual(self.b.creators["creator_00"]["stopped"], "max_tries")
        self.assertEqual(self.b.creators["creator_00"]["step"], 0)   # never marked sent

    def test_outcome_unknown_is_not_treated_as_sent(self):
        r = self.rec(ledger_for(self.p, "unknown"))
        self.assertEqual(r["retry"], 3)
        self.assertNotIn("unrecognised_code", r)
        self.assertEqual(self.b.creators["creator_00"]["step"], 0)

    def test_bad_rows_duplicates_and_strangers(self):
        pages = ledger_for(self.p)
        pages["data"] += [{"creator_handle": None, "outcome": "skipped"}, "junk",
                          dict(pages["data"][0], outcome="skipped",
                               skip_reason="creator_not_found"),
                          {"creator_handle": "nobody_here", "outcome": "outreach_sent"}]
        r = self.rec(pages)
        self.assertEqual((r["sent"], r["malformed"], r["duplicate_row"], r["unplanned_row"]),
                         (3, 2, 1, 1))
        self.assertIsNone(self.b.creators["creator_00"]["stopped"])   # first row won

    def test_pages_and_bad_payloads(self):
        full = ledger_for(self.p)
        pages = [dict(full, data=full["data"][:2]), dict(full, data=full["data"][2:])]
        self.assertEqual(self.rec(pages)["sent"], 3)
        for bad in ({}, {"data": None}, [{"data": []}, "x"], "nope"):
            with self.assertRaises(LedgerError, msg=bad):
                parse_ledger(bad)

    def test_reply_then_ledger_does_not_restart_the_creator(self):
        self.b.stop("@CREATOR_00")
        r = self.rec(ledger_for(self.p))
        self.assertEqual((r["sent"], r["already_reconciled"]), (2, 1))

    def test_state_survives_a_round_trip_and_loads_v1_files(self):
        self.rec(ledger_for(self.p))
        c = Book.loads(self.b.dumps())
        thu = MON + dt.timedelta(days=3)
        self.assertEqual(plan_day(c, self.cap, EVERY_DAY, thu),
                         plan_day(self.b, self.cap, EVERY_DAY, thu))
        old = json.dumps({"sequence": [["tc", 0]], "rejected": [], "creators": {
            "a_one": {"handle": "a_one", "score": 0.5, "email": None, "step": 0,
                      "last": None, "stopped": None}}})
        v1 = Book.loads(old)
        self.assertEqual(len(plan_day(v1, self.cap, EVERY_DAY, MON).sends), 1)


QUIET = TRAIN._replace(n=1500, limits=(None, 120, 40, 25), outside=(0, 0), gone=0.0,
                       pre_invited=0.0, pre_dm=0.0, blocked=0.0, no_level=0.0,
                       shop_daily_limit=10000, other_tc=(0, 0))
SMALL = {d + "_maxCreators": 150 for d in ("Monday", "Tuesday", "Wednesday", "Thursday",
                                             "Friday", "Saturday", "Sunday")}


class AgainstTheSyntheticShop(unittest.TestCase):
    def test_no_refusals_when_nobody_else_uses_the_quota(self):
        # With no outside usage and clean data, the planner's model of the
        # rules and the simulator's must agree on every single send.
        for policy in (Policy(), Policy(reserve=1.0), Policy(paid_followups="last")):
            r = bench.run(QUIET, 5, "aware", policy, schedule=SMALL)
            self.assertEqual(r["refused"], 0, policy)
            self.assertEqual(r["attempts"], r["delivered"])
            self.assertEqual((r["repeated_step"], r["sent_after_reply"]), (0, 0))
            self.assertGreater(r["units"], 0)

    def test_blind_planner_gets_refused_in_the_same_shop(self):
        r = bench.run(QUIET, 5, "blind", Policy(quota_aware=False), schedule=SMALL)
        self.assertGreater(r["refused_quota"], 0)
        self.assertEqual((r["repeated_step"], r["sent_after_reply"]), (0, 0))

    def test_messy_shop_never_repeats_a_step_or_writes_after_a_reply(self):
        messy = TRAIN._replace(n=1500, limits=(None, 120, 40, 25), outside=(0, 6))
        r = bench.run(messy, 6, "aware", Policy(margin=0.05), schedule=SMALL)
        self.assertEqual((r["repeated_step"], r["sent_after_reply"]), (0, 0))
        self.assertGreater(r["refused"], 0)           # gone handles, blocks, drift

    def test_simulator_never_exceeds_a_tier_limit(self):
        shop = Shop(QUIET, 9)
        b = Book(sequence=QUOTA_SEQUENCE)
        for h, score, email, level in shop.rows:
            b.add(h, score, email, level)
        cap = parse_capacity(shop.capacity(FIRST_RUN))
        sched = parse_schedule({"Sunday_maxCreators": 10000})
        p = plan_day(b, cap, sched, FIRST_RUN, Policy(quota_aware=False))
        shop.submit(p.sends, FIRST_RUN)
        after = parse_capacity(shop.capacity(FIRST_RUN))
        for level, limit in ((2, 120), (3, 40), (4, 25)):
            self.assertEqual(after.remaining_for(level, FIRST_RUN)[1], 0)
        self.assertEqual(shop.stats["units"], 120 + 40 + 25)

    def test_simulator_payload_matches_the_parser_on_every_day(self):
        shop = Shop(QUIET, 3)
        for d in range(15):
            now = FIRST_RUN + dt.timedelta(days=d)
            c = parse_capacity(copy.deepcopy(shop.capacity(now)))
            self.assertEqual(c.days_left(now), 7 - d % 7)


if __name__ == "__main__":
    unittest.main()
