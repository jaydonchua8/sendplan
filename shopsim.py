"""A synthetic TikTok Shop, used only to test and measure the planner.

EVERYTHING HERE IS SYNTHETIC. No creator, handle, quota or reply rate in this
file comes from a real shop. The simulator enforces the outreach rules on its
own, without importing quota.py, so a planner mistake shows up as a refused
send instead of being hidden. It speaks the same two payload shapes the
planner reads: OutreachCapacityResponse and AutomationCreatorsResponse.
"""
import datetime as dt
import random
from collections import Counter, namedtuple

from sendplan import canonical

UTC = dt.timezone.utc
FIRST_RESET = dt.datetime(2026, 11, 1, 19, 0, tzinfo=UTC)   # a Sunday, 11:00 PT
FIRST_RUN = dt.datetime(2026, 11, 1, 20, 0, tzinfo=UTC)     # daily run time
WEEK = dt.timedelta(days=7)
GROUPS = ((0, 1), (2,), (3,), (4, 5, 6, 7))
PAGE = 500                                                  # ledger page_size maximum

Settings = namedtuple("Settings", "n mix limits bonus reply step_mult outside "
                      "dup_rate gone pre_invited pre_dm blocked no_level has_email "
                      "shop_daily_limit other_tc")
# n            real creators on the list
# mix          share of creators in each of GROUPS
# limits       weekly standard limit per group, None for no ceiling
# bonus        weekly bonus limit per group
# reply        chance a first contact gets a reply, per group
# step_mult    reply chance multiplier per sequence step
# outside      (low, high) units per day that other people in the shop use
#              from each limited group; part lands after the morning snapshot
# dup_rate     share of creators imported twice under another spelling
# gone         share whose imported handle no longer exists
# pre_invited  share already holding an invite for the product
# pre_dm       share already at the five-DM limit
# blocked      share who blocked the shop
# no_level     share imported without a creator level
# other_tc     (low, high) daily pool used by the shop's other automations

# Limits in TRAIN are the Lv 2 / Lv 3 / Lv 4-7 numbers a third-party help
# article lists for one US GMV band in November 2026. HELDOUT uses the next
# band up. All other numbers are mine.
TRAIN = Settings(n=20000, mix=(0.55, 0.25, 0.12, 0.08),
                 limits=(None, 2500, 750, 500), bonus=(0, 0, 0, 0),
                 reply=(0.06, 0.05, 0.04, 0.03), step_mult=(1.0, 0.6, 0.4, 0.3),
                 outside=(0, 12), dup_rate=0.10, gone=0.02, pre_invited=0.02,
                 pre_dm=0.01, blocked=0.005, no_level=0.03, has_email=0.55,
                 shop_daily_limit=2500, other_tc=(200, 900))
HELDOUT = Settings(n=26000, mix=(0.40, 0.30, 0.18, 0.12),
                   limits=(None, 3000, 1000, 800), bonus=(0, 0, 150, 0),
                   reply=(0.05, 0.05, 0.05, 0.04), step_mult=(1.0, 0.5, 0.5, 0.2),
                   outside=(5, 40), dup_rate=0.15, gone=0.04, pre_invited=0.03,
                   pre_dm=0.02, blocked=0.01, no_level=0.06, has_email=0.40,
                   shop_daily_limit=3000, other_tc=(0, 1500))


def variant(rng, h):
    return rng.choice(["@" + h, h.upper(), " " + h + " ", "tiktok.com/@" + h,
                       "https://www.tiktok.com/@" + h + "?lang=en", h])


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Shop:
    def __init__(self, settings, seed):
        self.s, self.rng = settings, random.Random(seed)
        rng, s = self.rng, settings
        self.creators, self.owner, self.rows, self.score = [], {}, [], {}
        for i in range(s.n):
            g = rng.choices(range(4), weights=s.mix)[0]
            level = rng.choice(GROUPS[g])
            h = "cr%05d_%s" % (i, rng.choice(["fit", "home", "beauty", "tech", "pets"]))
            c = {"g": g, "level": level, "handle": h, "replied": False,
                 "charged": -1, "dm": 5 if rng.random() < s.pre_dm else 0,
                 "invited": rng.random() < s.pre_invited,
                 "blocked": rng.random() < s.blocked, "steps": set()}
            self.creators.append(c)
            self.owner[h] = i
            # the brand's score: bigger creators score higher, with noise
            score = round(min(1.0, max(0.0, 0.2 + 0.2 * g + rng.gauss(0, 0.12))), 4)
            self.score[i] = score
            email = "c%d@example.test" % i if rng.random() < s.has_email else None
            known = None if rng.random() < s.no_level else level
            if rng.random() < s.gone:
                self.rows.append(("old%05d_x" % i, score, email, known))
                continue                      # this handle resolves to nobody
            self.rows.append((h, score, email, known))
            if rng.random() < s.dup_rate:
                self.rows.append((variant(rng, h), score, None, None))
        rng.shuffle(self.rows)
        self.week = 0
        self.std_used, self.bonus_used = [0] * 4, [0] * 4
        self.shop_used = 0
        self.today, self.tc_today, self.late_outside = None, 0, [0] * 4
        self.replies = []
        self.stats = Counter()
        self.unused = [0] * 4                 # units left at each reset, summed
        self.replies_by_group = [0] * 4
        self.reply_score = 0.0
        self.expected_score = 0.0
        self.sent_by_group = [0] * 4

    # ---- quota bookkeeping ----------------------------------------------
    def _left(self, g):
        if self.s.limits[g] is None:
            return None
        return (self.s.limits[g] - self.std_used[g]) + (self.s.bonus[g] - self.bonus_used[g])

    def _use(self, g):
        if self.std_used[g] < self.s.limits[g]:
            self.std_used[g] += 1
        else:
            self.bonus_used[g] += 1
        self.shop_used += 1

    def _outside(self, g, n):
        for _ in range(min(n, self._left(g))):
            self._use(g)
            self.stats["outside_units"] += 1

    def _roll(self, now):
        week = (now - FIRST_RESET) // WEEK
        while self.week < week:
            for g in range(4):
                if self.s.limits[g] is not None:
                    self.unused[g] += self._left(g)
            self.std_used, self.bonus_used, self.shop_used = [0] * 4, [0] * 4, 0
            self.week += 1
        if self.today != now.date():
            self.today = now.date()
            self.tc_today = self.rng.randint(*self.s.other_tc)
            for g in range(4):
                if self.s.limits[g] is not None:
                    n = self.rng.randint(*self.s.outside)
                    early = self.rng.randint(0, n)
                    self._outside(g, early)           # before the snapshot
                    self.late_outside[g] = n - early  # after it, before the sends

    # ---- the two payloads -----------------------------------------------
    def capacity(self, now):
        """OutreachCapacityResponse as of this morning."""
        self._roll(now)
        tiers = []
        for g in range(4):
            lim = self.s.limits[g]
            tiers.append({"creator_levels": list(GROUPS[g]), "unlimited": lim is None,
                          "standard_limit": lim,
                          "standard_used": None if lim is None else self.std_used[g],
                          "bonus_limit": None if lim is None else self.s.bonus[g],
                          "bonus_used": None if lim is None else self.bonus_used[g],
                          "remaining": self._left(g)})
        midnight = dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(0), UTC)
        return {"shop_id": 101, "daily_creators_used": self.tc_today,
                "daily_window_resets_at": _iso(midnight),
                "concurrent_automations_running": 2, "max_concurrent_automations": 5,
                "daily_limit_throttled": False, "weekly_limit_throttled": False,
                "outreach_quota": {
                    "quota_type": "PERIOD", "used": self.shop_used, "limit": None,
                    "remaining": None, "unlimited": True,
                    "resets_at": _iso(FIRST_RESET + WEEK * (self.week + 1)),
                    "gmv_tier": "synthetic", "gmv_level_rule": None,
                    "tiered": True, "tiers": tiers}}

    def submit(self, sends, now):
        """Try each send in order. Returns ledger pages shaped like
        AutomationCreatorsResponse, at most 500 rows each."""
        self._roll(now)
        for g in range(4):
            if self.late_outside[g]:
                self._outside(g, self.late_outside[g])
                self.late_outside[g] = 0
        rng, st, rows = self.rng, self.stats, []
        for s in sends:
            st["attempts"] += 1
            outcome, reason = self._one(s)
            if outcome == "skipped":
                st["refused"] += 1
                st["refused:" + reason] += 1
            elif outcome == "previously_invited":
                st["refused"] += 1
                st["refused:previously_invited"] += 1
            rows.append({"creator_handle": s.handle, "outcome": outcome,
                         "status": outcome.replace("_", " ").title(),
                         "detail": "synthetic", "skip_reason": reason,
                         "invitation_id": "tc%07d" % st["attempts"]
                         if outcome == "invite_submitted" else None})
        summary = Counter(r["outcome"] for r in rows)
        pages = []
        for p in range(0, max(len(rows), 1), PAGE):
            pages.append({"data": rows[p:p + PAGE],
                          "summary": dict({k: summary.get(k, 0) for k in (
                              "invite_submitted", "outreach_sent", "previously_invited",
                              "invite_removed", "dm_blocked", "skipped", "unknown")},
                              total=len(rows)),
                          "pagination": {"page": p // PAGE + 1, "page_size": PAGE,
                                         "total": len(rows)}})
        return pages

    def _one(self, s):
        i = self.owner.get(canonical(s.handle))
        if i is None:
            return "skipped", "creator_not_found"
        c, st = self.creators[i], self.stats
        if c["replied"]:
            st["sent_after_reply"] += 1
            return "skipped", "already_connected"
        if c["blocked"]:
            return "skipped", "creator_blocked_shop"
        if s.step in c["steps"]:
            st["repeated_step"] += 1
        if s.channel == "tc":
            if c["invited"]:
                return "previously_invited", None
            if self.tc_today >= self.s.shop_daily_limit:
                return "skipped", "blocked_by_outreach_limit"
        if s.channel == "dm" and c["dm"] >= 5:
            return "skipped", "five_message_limit_hit"
        g = c["g"]
        if s.channel != "email" and self.s.limits[g] is not None \
                and c["charged"] != self.week:
            if self._left(g) <= 0:
                return "skipped", "creator_tier_quota_exhausted"
            self._use(g)
            st["units"] += 1
        if s.channel != "email":
            c["charged"] = self.week
        if s.channel == "tc":
            self.tc_today += 1
            c["invited"] = True
            c["dm"] += 1                      # one message goes with the invite
        elif s.channel == "dm":
            c["dm"] += 1
        c["steps"].add(s.step)
        st["delivered"] += 1
        self.sent_by_group[g] += 1
        p = self.s.reply[g] * self.s.step_mult[min(s.step, len(self.s.step_mult) - 1)]
        # Expected value of this send. Summed, it estimates the same thing as
        # reply_score with far less seed-to-seed noise, so tuning uses it.
        self.expected_score += p * self.score.get(i, 0.0)
        if self.rng.random() < p:
            c["replied"] = True
            self.replies.append(variant(self.rng, c["handle"]))
            self.replies_by_group[g] += 1
            self.reply_score += self.score.get(i, 0.0)
            st["replies"] += 1
        return ("invite_submitted" if s.channel == "tc" else "outreach_sent"), None

    def close(self, now):
        """Roll forward to `now` so the last period's unused units are counted."""
        self._roll(now)

    def pop_replies(self):
        out, self.replies = self.replies, []
        return out
