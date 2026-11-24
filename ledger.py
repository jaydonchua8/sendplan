"""Reconcile a plan with what the platform reports happened.

Input is one or more pages shaped like AutomationCreatorsResponse, the
per-creator outcome ledger at GET /automations/{automation_id}/creators in
Reacher's public OpenAPI document. Each row has creator_handle, outcome,
status, detail, skip_reason and invitation_id. The spec says to branch on
skip_reason and not to parse detail, so that is what this does.

The spec lists the codes but does not define most of them. The action I take
for each code below is my reading of its name, and is the first thing I would
want corrected by someone who knows the real semantics.
"""
from collections import Counter, namedtuple

OUTCOMES = ("invite_submitted", "outreach_sent", "previously_invited",
            "invite_removed", "dm_blocked", "skipped", "unknown")
SENT = ("invite_submitted", "outreach_sent")

# skip_reason -> what to do with the creator
STOP = ("creator_not_found", "creator_ineligible", "performance_criteria_not_met",
        "already_accepted_invite", "conflicting_invitation", "shop_blocked_creator",
        "creator_blocked_shop", "follower_threshold_exceeded",
        "agency_sample_request_blocked", "sample_request_expired",
        "max_send_attempts_reached", "tiktok_contact_restricted",
        "already_connected", "creator_level_not_targeted", "live_auction_ineligible")
WAIT_FOR_RESET = ("creator_tier_quota_exhausted",)
TRY_TOMORROW = ("blocked_by_outreach_limit",)   # could be the daily pool
STEP_DONE = ("previously_invited", "previously_messaged")
DM_CLOSED = ("five_message_limit_hit",)
SKIP_REASONS = STOP + WAIT_FOR_RESET + TRY_TOMORROW + STEP_DONE + DM_CLOSED + ("unknown",)

Row = namedtuple("Row", "handle outcome skip_reason invitation_id")


class LedgerError(ValueError):
    pass


def parse_ledger(pages):
    """One page (dict) or a list of pages -> (rows, malformed row count).
    A page without a `data` list is an error. A bad row is counted and
    skipped, so one broken row does not block the other 499 on the page."""
    if isinstance(pages, dict):
        pages = [pages]
    rows, bad = [], 0
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise LedgerError("ledger page has no data list")
        for r in page["data"]:
            if not isinstance(r, dict) or not isinstance(r.get("creator_handle"), str) \
                    or not isinstance(r.get("outcome"), str):
                bad += 1
                continue
            rows.append(Row(r["creator_handle"], r["outcome"], r.get("skip_reason"),
                            r.get("invitation_id")))
    return rows, bad


def reconcile(book, plan, pages, now, cap, policy):
    """Update the book from the ledger. Returns a Counter of what happened.

    Only a row that says the send was submitted advances the sequence. A
    planned send with no row is left pending and is planned again later.
    Running this twice with the same input changes nothing the second time.
    """
    rows, bad = parse_ledger(pages)
    res = Counter()
    if bad:
        res["malformed"] = bad
    by_key = {}
    for r in rows:
        k = book.key(r.handle)
        if k in by_key:
            res["duplicate_row"] += 1       # the first row for a creator wins
        else:
            by_key[k] = r
    day, now_ts = now.date().toordinal(), int(now.timestamp())
    period = cap.period(now)
    reset_ts = int(cap.quota.resets_at.timestamp()) if period else now_ts + 86400
    planned = set()
    for s in plan.sends:
        planned.add(s.key)
        c = book.creators.get(s.key)
        r = by_key.get(s.key)
        if c is None:
            continue
        if r is None:
            res["missing"] += 1
            continue
        if c["rec"] == [day, s.step] or c["step"] > s.step or c["stopped"] is not None:
            res["already_reconciled"] += 1
            continue
        c["rec"] = [day, s.step]
        reason = r.skip_reason if r.outcome == "skipped" else None
        if r.outcome in SENT:
            c["step"], c["last"], c["tries"], c["hold"] = s.step + 1, day, 0, None
            if s.channel == "tc":
                c["dm"] += policy.initial_messages
            elif s.channel == "dm":
                c["dm"] += 1
            if s.channel != "email" and period is not None:
                c["charged"] = period
            res["sent"] += 1
        elif r.outcome == "previously_invited" or reason in STEP_DONE:
            # The invite or message already exists. Do not send it again, and
            # wait the normal delay before the next step.
            c["step"], c["last"], c["tries"] = s.step + 1, day, 0
            res["step_already_done"] += 1
        elif r.outcome in ("invite_removed", "dm_blocked") or reason in STOP:
            c["stopped"] = reason or r.outcome
            res["stopped"] += 1
        elif reason in DM_CLOSED:
            c["dm"] = max(c["dm"], 5)       # next_step() now skips DM steps
            res["dm_closed"] += 1
        elif reason in WAIT_FOR_RESET:
            # Out of quota is not the creator's fault, so it does not count
            # as a failed try. Come back after the reset.
            c["hold"] = reset_ts
            res["held_until_reset"] += 1
        elif reason in TRY_TOMORROW:
            c["hold"] = now_ts + 43200
            res["held_until_tomorrow"] += 1
        else:
            # outcome "unknown", skip_reason "unknown", or a code added after
            # I read the spec. Treat as not sent and try again tomorrow.
            if r.outcome not in OUTCOMES or \
                    (r.outcome == "skipped" and reason not in SKIP_REASONS):
                res["unrecognised_code"] += 1
            c["hold"] = now_ts + 43200
            c["tries"] += 1
            res["retry"] += 1
            if c["tries"] >= policy.max_tries:
                c["stopped"] = "max_tries"
                res["gave_up"] += 1
    res["unplanned_row"] = sum(1 for k in by_key if k not in planned)
    return +res
