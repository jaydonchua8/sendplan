"""Plan one day of outreach against TikTok's weekly quota.

Rules the planner models, and where each comes from (see README for links):

  * A weekly quota per creator-level group, read from the `tiers` of an
    OutreachCapacityResponse. Unlimited groups cost nothing.
  * A creator counts against the quota once per period, even if they get a
    target collab invite and a DM. So a follow-up in the same period as the
    first contact is free, and a follow-up after the reset costs a new unit.
    (The second half is my reading of the once-per-week rule.)
  * At most five DMs to a creator who has not replied.
  * The daily pool counts creators invited by target collab. DMs do not count.
  * A null quota means unknown. It is never treated as zero.
"""
import math
from collections import Counter, namedtuple

from sendplan import Send, Step

QUOTA_SEQUENCE = (Step("tc", 0), Step("dm", 3), Step("dm", 4), Step("email", 3))
DM_LIMIT = 5                 # DMs to a creator before they reply
MAX_FOLLOW_UPS = 3           # follow_ups maxItems in the automation request
MAX_INITIAL_MESSAGES = 5     # messages maxItems in the automation request

Policy = namedtuple("Policy", "quota_aware margin reserve paid_followups "
                    "shop_daily_limit unknown initial_messages max_tries",
                    defaults=(True, 0.0, 0.0, "first", None, "daily_only", 1, 3))
# quota_aware      False ignores the weekly quota (the blind baseline).
# margin           share of each group's remaining quota left unspent, as a
#                  buffer against invites sent outside this planner.
# reserve          share of the follow-ups that will need a new quota unit
#                  later this period to hold back from first contacts.
# paid_followups   "first", "last" or "never": where follow-ups that cost a
#                  new unit rank against first contacts.
# shop_daily_limit ceiling of the shop's daily target collab pool, or None.
# unknown          "daily_only" plans on the daily cap alone when the quota is
#                  unknown, "hold" sends only what cannot cost a unit.
# initial_messages DMs sent together with the invite (0 to 5).
# max_tries        refused attempts before a creator is dropped.

Plan = namedtuple("Plan", "sends skipped warnings period")


class SequenceError(ValueError):
    pass


def validate_sequence(sequence, initial_messages=1):
    """Reject sequences the public automation request could not express."""
    if not 0 <= initial_messages <= MAX_INITIAL_MESSAGES:
        raise SequenceError("initial_messages must be 0 to %d" % MAX_INITIAL_MESSAGES)
    for i, s in enumerate(sequence):
        if s.channel not in ("tc", "dm", "email"):
            raise SequenceError("unknown channel %r" % (s.channel,))
        if s.channel == "tc" and i != 0:
            raise SequenceError("the target collab invite must be the first step")
        if isinstance(s.wait_days, bool) or not isinstance(s.wait_days, int) \
                or s.wait_days < 0:
            raise SequenceError("wait_days must be a non-negative integer")
    if sum(1 for s in sequence[1:] if s.channel == "dm") > MAX_FOLLOW_UPS:
        raise SequenceError("more than %d follow-up DMs" % MAX_FOLLOW_UPS)


def next_step(sequence, c):
    """Index of the next step this creator can receive, or None. Skips the
    email step without an address and DM steps once the DM limit is reached."""
    i = c["step"]
    while i < len(sequence):
        ch = sequence[i].channel
        if (ch == "email" and not c["email"]) or (ch == "dm" and c["dm"] >= DM_LIMIT):
            i += 1
            continue
        return i
    return None


def plan_day(book, cap, schedule, now, policy=Policy()):
    """Pick today's sends. Pure: it does not change the book.

    Order: follow-ups that cost nothing (most overdue first), then follow-ups
    that cost a unit and first contacts in the order policy.paid_followups
    names. First contacts go best score first. Ties break on the key.
    """
    seq = book.sequence
    day, now_ts = now.date().toordinal(), int(now.timestamp())
    slots = schedule.cap(now.date())
    period = cap.period(now)
    left_days = cap.days_left(now)
    warnings, skipped = [], Counter()

    tc_left = math.inf
    if policy.shop_daily_limit is not None:
        tc_left = max(0, policy.shop_daily_limit - cap.daily_used)
    if cap.daily_throttled:
        tc_left = 0
        warnings.append("daily_limit_throttled is set: no target collab invites today")
    if policy.quota_aware:
        if cap.stale(now):
            warnings.append("capacity snapshot is from a finished period: quota unknown")
        elif cap.quota is None:
            warnings.append("outreach_quota is null: quota unknown")
        if cap.weekly_throttled:
            warnings.append("weekly_limit_throttled is set: only sends that cost no unit")

    free_follow, paid_follow, new = [], [], []
    upcoming = Counter()             # group -> paid follow-ups due later this period
    remaining = {}                   # group -> units reported by the snapshot
    for k, c in book.creators.items():
        if c["stopped"] is not None or c["last"] == day:
            continue
        if c["hold"] is not None and c["hold"] > now_ts:
            skipped["held_until_reset"] += 1
            continue
        i = next_step(seq, c)
        if i is None:
            continue
        ch = seq[i].channel
        group = None
        if policy.quota_aware and ch != "email" \
                and not (period is not None and c["charged"] == period):
            g, rem = cap.remaining_for(c["level"], now)
            if rem is None:
                if policy.unknown == "hold":
                    skipped["quota_unknown"] += 1
                    continue
                group = "unknown"
            elif rem != math.inf:
                group = g
                remaining[g] = rem
            if cap.weekly_throttled and rem != math.inf:
                skipped["weekly_throttled"] += 1
                continue
        if c["last"] is None:
            new.append((-c["score"], k, i, group))
            continue
        due = c["last"] + seq[i].wait_days
        if day < due:
            if group not in (None, "unknown") and due < day + left_days:
                upcoming[group] += 1
            continue
        if group in (None, "unknown"):
            free_follow.append((due, k, i, group))
        else:
            paid_follow.append((due, k, i, group))
    free_follow.sort(key=lambda t: t[:2])
    paid_follow.sort(key=lambda t: t[:2])
    new.sort(key=lambda t: t[:2])

    usable = {g: r - math.ceil(policy.margin * r) for g, r in remaining.items()}
    for_new = {g: max(0, usable[g] - math.ceil(policy.reserve * upcoming[g]))
               for g in usable}
    spent = Counter()
    out = []

    def take(items, kind, budget):
        nonlocal tc_left
        for pri, k, i, group in items:
            if len(out) >= slots:
                return
            ch = seq[i].channel
            if ch == "tc" and tc_left <= 0:
                skipped["daily_tc_pool"] += 1
                continue
            units = 0
            if group == "unknown":
                skipped["planned_without_quota"] += 1
            elif group is not None:
                if spent[group] >= budget[group]:
                    skipped["tier_quota"] += 1
                    continue
                spent[group] += 1
                units = 1
            if ch == "tc":
                tc_left -= 1
            out.append(Send(k, book.creators[k]["handle"], ch, i, kind,
                            pri if kind == "followup" else day, units,
                            None if group == "unknown" else group))

    take(free_follow, "followup", usable)
    if policy.paid_followups == "first":
        take(paid_follow, "followup", usable)
        take(new, "new", for_new)
    elif policy.paid_followups == "last":
        take(new, "new", for_new)
        take(paid_follow, "followup", usable)
    elif policy.paid_followups == "never":
        skipped["paid_followup_off"] += len(paid_follow)
        take(new, "new", for_new)
    else:
        raise ValueError("paid_followups must be first, last or never")
    return Plan(out, +skipped, warnings, period)
