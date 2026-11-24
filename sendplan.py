"""sendplan: creator outreach state and the first, quota-blind planner.

Book holds one record per canonical creator handle. Book.plan() is the v1
planner that only knows flat daily caps. It is kept as the baseline. The
planner that reads TikTok's weekly outreach quota is in quota.py.
Standard library only. See README.md.
"""
import json
import re
import unicodedata
from collections import namedtuple

Step = namedtuple("Step", "channel wait_days")
# units: weekly quota units this send is expected to consume (0 or 1).
# group: the creator-level group whose quota it draws on, or None.
Send = namedtuple("Send", "key handle channel step kind due units group",
                  defaults=(0, None))

DEFAULT_SEQUENCE = (Step("dm", 0), Step("collab", 3), Step("email", 4))

_URL = re.compile(r"^(?:https?://)?(?:www\.|m\.|vm\.)?tiktok\.com/@?([^/?#\s]+)")
_INVISIBLE = re.compile(r"[​-‏⁠﻿\s]")
_VALID = re.compile(r"^[a-z0-9_.]{2,24}$")


def canonical(handle):
    """Reduce the ways a TikTok handle gets typed to one key. '' if unusable."""
    s = unicodedata.normalize("NFKC", str(handle))
    s = _INVISIBLE.sub("", s).lower()
    m = _URL.match(s)
    if m:
        s = m.group(1)
    s = s.lstrip("@").rstrip(".,;")
    return s if _VALID.match(s) else ""


def _record(handle, score=0.0, email=None, level=None):
    return {"handle": handle, "score": float(score), "email": email,
            "step": 0, "last": None, "stopped": None,
            "level": level,     # TikTok creator level 0..7, None if unknown
            "dm": 0,            # DMs sent while the creator has not replied
            "charged": None,    # id of the quota period that last counted them
            "tries": 0,         # consecutive attempts the platform refused
            "hold": None,       # epoch seconds before which not to retry
            "rec": None}        # [day, step] of the last reconciled attempt


class Book:
    """Outreach state for one brand: who has been sent what, and who answered."""

    def __init__(self, sequence=DEFAULT_SEQUENCE, key=canonical):
        self.sequence = tuple(Step(*s) for s in sequence)
        self.key = key
        self.creators = {}   # key -> record dict
        self.rejected = []   # handles that could not be keyed

    # ---- loading -------------------------------------------------------
    def add(self, handle, score=0.0, email=None, level=None):
        """Add a creator. A second row for the same key merges into the first."""
        k = self.key(handle)
        if not k:
            self.rejected.append(handle)
            return None
        c = self.creators.get(k)
        if c is None:
            self.creators[k] = _record(handle, score, email, level)
        else:
            c["score"] = max(c["score"], float(score))
            c["email"] = c["email"] or email
            if c["level"] is None:
                c["level"] = level
        return k

    def stop(self, handle, reason="replied"):
        """A reply or opt-out ends the sequence. Unknown handles are remembered
        too, so a creator imported later is never contacted."""
        k = self.key(handle)
        if not k:
            return False
        c = self.creators.setdefault(k, _record(handle))
        if c["stopped"] is None:
            c["stopped"] = reason
        return True

    # ---- planning ------------------------------------------------------
    def _next(self, c):
        """Index of the next step this creator can receive, or None."""
        i = c["step"]
        while i < len(self.sequence):
            if self.sequence[i].channel == "email" and not c["email"]:
                i += 1          # no address on file, skip the email step
                continue
            return i
        return None

    def plan(self, day, channel_caps, total_cap, new_share=0.0):
        """Pick today's sends. Does not change state.

        Follow-ups that are due go first, most overdue first. First contacts
        fill what is left, best score first. new_share reserves that fraction
        of total_cap for first contacts so a follow-up backlog cannot starve
        them. Ties break on the key, so the same input gives the same plan.
        """
        follow, new = [], []
        for k, c in self.creators.items():
            if c["stopped"] is not None or c["last"] == day:
                continue
            i = self._next(c)
            if i is None:
                continue
            if c["last"] is None:
                new.append((-c["score"], k, i))
            else:
                due = c["last"] + self.sequence[i].wait_days
                if day >= due:
                    follow.append((due, k, i))
        follow.sort()
        new.sort()

        left = dict(channel_caps)
        out = []

        def take(items, kind, budget):
            used = 0
            for pri, k, i in items:
                if used >= budget:
                    break
                ch = self.sequence[i].channel
                if left.get(ch, 0) <= 0:
                    continue
                left[ch] -= 1
                used += 1
                due = pri if kind == "followup" else day
                out.append(Send(k, self.creators[k]["handle"], ch, i, kind, due))
            return used

        reserve = min(int(total_cap * new_share), len(new))
        n_follow = take(follow, "followup", total_cap - reserve)
        take(new, "new", total_cap - n_follow)
        return out

    def apply(self, sends, day):
        """Record that these sends went out. Applying a plan twice is a no-op
        the second time, so a retried job cannot double-send."""
        done = 0
        for s in sends:
            c = self.creators.get(s.key)
            if c is None or c["stopped"] is not None or c["step"] > s.step:
                continue
            c["step"] = s.step + 1
            c["last"] = day
            done += 1
        return done

    # ---- persistence ---------------------------------------------------
    def dumps(self):
        return json.dumps({"sequence": [list(s) for s in self.sequence],
                           "creators": self.creators,
                           "rejected": self.rejected}, sort_keys=True)

    @classmethod
    def loads(cls, text, key=canonical):
        d = json.loads(text)
        b = cls(sequence=d["sequence"], key=key)
        b.creators = d["creators"]
        for c in b.creators.values():       # files written by v1 lack these
            for f, v in _record("").items():
                c.setdefault(f, v)
        b.rejected = d["rejected"]
        return b
