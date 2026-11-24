"""Read the two payloads that bound a day's outreach.

Field names follow the schemas in Reacher's public OpenAPI document
(docs.reacherapp.com/openapi.json, read in November 2026):

  OutreachCapacityResponse  from GET /automations/outreach-capacity
  AutomationSchedule        the per-weekday caps inside an automation request

I only parse dictionaries here. Nothing in this project calls the API.
The parser is strict on purpose: a payload I do not understand raises
CapacityError, because guessing a quota means sending messages the platform
will refuse.
"""
import datetime as dt
import math
import re

UTC = dt.timezone.utc
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday")
MAX_PER_DAY = 10000          # maximum of <Weekday>_maxCreators in the schema
QUOTA_TYPES = ("PERIOD", "BONUS")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class CapacityError(ValueError):
    pass


def _get(d, k):
    if not isinstance(d, dict):
        raise CapacityError("expected an object, got %s" % type(d).__name__)
    if k not in d:
        raise CapacityError("missing field %r" % k)
    return d[k]


def _int(v, name, nullable=False):
    if v is None and nullable:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise CapacityError("%s must be a non-negative integer, got %r" % (name, v))
    return v


def _bool(v, name):
    if not isinstance(v, bool):
        raise CapacityError("%s must be true or false, got %r" % (name, v))
    return v


def _time(v, name, nullable=False):
    if v is None and nullable:
        return None
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise CapacityError("%s is not an ISO-8601 time: %r" % (name, v))
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


class Tier:
    """One creator-level group. remaining is math.inf (no ceiling), an
    integer, or None when the platform did not report it."""
    __slots__ = ("levels", "unlimited", "remaining")

    def __init__(self, levels, unlimited, remaining):
        self.levels, self.unlimited, self.remaining = levels, unlimited, remaining


def _tier(d):
    levels = _get(d, "creator_levels")
    if not isinstance(levels, list) or not levels:
        raise CapacityError("creator_levels must be a non-empty list")
    levels = tuple(_int(x, "creator_levels[]") for x in levels)
    if _bool(_get(d, "unlimited"), "unlimited"):
        return Tier(levels, True, math.inf)
    v = {k: _int(_get(d, k), k, nullable=True) for k in
         ("standard_limit", "standard_used", "bonus_limit", "bonus_used", "remaining")}
    remaining = v["remaining"]
    if remaining is not None and v["standard_limit"] is not None \
            and v["standard_used"] is not None:
        # Both pools are reported. If they disagree with `remaining`, believe
        # the smaller number.
        pools = max(0, v["standard_limit"] - v["standard_used"])
        if v["bonus_limit"] is not None:
            pools += max(0, v["bonus_limit"] - (v["bonus_used"] or 0))
        remaining = min(remaining, pools)
    return Tier(levels, False, remaining)


class Quota:
    __slots__ = ("quota_type", "used", "remaining", "resets_at", "tiered", "tiers")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw[k])


def _quota(d):
    if d is None:
        return None              # the schema says null means unknown, never zero
    qt = _get(d, "quota_type")
    if qt not in QUOTA_TYPES:
        raise CapacityError("unknown quota_type %r" % (qt,))
    used = _int(_get(d, "used"), "used")
    limit = _int(_get(d, "limit"), "limit", nullable=True)
    remaining = _int(_get(d, "remaining"), "remaining", nullable=True)
    unlimited = _bool(_get(d, "unlimited"), "unlimited")
    tiered = _bool(d.get("tiered", False), "tiered")
    tiers = d.get("tiers")
    if tiers is not None:
        if not isinstance(tiers, list):
            raise CapacityError("tiers must be a list or null")
        tiers = [_tier(t) for t in tiers]
        seen = [lv for t in tiers for lv in t.levels]
        if len(seen) != len(set(seen)):
            raise CapacityError("a creator level appears in two tiers")
    if unlimited or limit is None:
        remaining = math.inf      # limit null means no ceiling; 0 is a real zero
    elif remaining is None:
        remaining = max(0, limit - used)
    else:
        remaining = min(remaining, limit)
    return Quota(quota_type=qt, used=used, remaining=remaining,
                 resets_at=_time(_get(d, "resets_at"), "resets_at", nullable=True),
                 tiered=tiered, tiers=tiers)


class Capacity:
    __slots__ = ("shop_id", "daily_used", "daily_resets_at", "daily_throttled",
                 "weekly_throttled", "quota")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw[k])

    def stale(self, now):
        """True when the quota in this snapshot belongs to a period that has
        already ended, so its numbers say nothing about today."""
        q = self.quota
        return q is not None and q.resets_at is not None and q.resets_at <= now

    def period(self, now):
        """Identifier of the current quota period, or None if not known."""
        q = self.quota
        if q is None or q.resets_at is None or q.resets_at <= now:
            return None
        return q.resets_at.isoformat()

    def days_left(self, now):
        """Daily runs left before the quota resets, counting today. 1 if unknown."""
        if self.period(now) is None:
            return 1
        return max(1, math.ceil((self.quota.resets_at - now).total_seconds() / 86400))

    def remaining_for(self, level, now):
        """(group, remaining) for a creator level. remaining is math.inf, an
        integer, or None for unknown. A tiered shop reports unlimited=true at
        the top level, so the tiers are what count there."""
        q = self.quota
        if q is None or self.stale(now):
            return "unknown", None
        if not q.tiered:
            return "shop", q.remaining
        if q.tiers is None or level is None:
            return "unknown", None
        for t in q.tiers:
            if level in t.levels:
                return t.levels, t.remaining
        return "unknown", None


def parse_capacity(d):
    """dict shaped like OutreachCapacityResponse -> Capacity."""
    return Capacity(
        shop_id=_int(_get(d, "shop_id"), "shop_id"),
        daily_used=_int(_get(d, "daily_creators_used"), "daily_creators_used"),
        daily_resets_at=_time(_get(d, "daily_window_resets_at"), "daily_window_resets_at"),
        daily_throttled=_bool(_get(d, "daily_limit_throttled"), "daily_limit_throttled"),
        weekly_throttled=_bool(_get(d, "weekly_limit_throttled"), "weekly_limit_throttled"),
        quota=_quota(_get(d, "outreach_quota")))


class Schedule:
    """Per-weekday cap on creators reached, as in AutomationSchedule."""

    def __init__(self, caps, start_time=None):
        self.caps, self.start_time = tuple(caps), start_time

    def cap(self, date):
        return self.caps[date.weekday()]


def parse_schedule(d):
    """dict shaped like AutomationSchedule -> Schedule. The schema sets
    additionalProperties false, so an unknown key is an error here too.
    end_time and timezone are accepted and ignored, as the schema says the
    API does."""
    if not isinstance(d, dict):
        raise CapacityError("schedule must be an object")
    known = {w + "_maxCreators" for w in WEEKDAYS} | {"start_time", "end_time", "timezone"}
    extra = sorted(set(d) - known)
    if extra:
        raise CapacityError("unknown schedule field %r" % extra[0])
    caps = []
    for w in WEEKDAYS:
        v = _int(d.get(w + "_maxCreators", 0), w + "_maxCreators")
        if v > MAX_PER_DAY:
            raise CapacityError("%s_maxCreators is above %d" % (w, MAX_PER_DAY))
        caps.append(v)
    st = d.get("start_time")
    if st is not None and not _HHMM.match(str(st)):
        raise CapacityError("start_time must be HH:MM, got %r" % (st,))
    return Schedule(caps, st)
