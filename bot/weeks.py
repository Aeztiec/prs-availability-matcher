"""Deadlines and match weekends.

Everything here is UTC, on purpose. The timings sheet's canonical column is
GMT+0, so the bot works in the same frame and only converts at the edges - when
showing a manager a Discord timestamp, which their own client localises. Any
other arrangement invites an off-by-an-hour bug every time the clocks change.

A "week" is the Saturday of the match weekend, as YYYY-MM-DD. It groups
fixtures for the dashboard, for referee availability, and for the conflict
checks, so it has to be derived the same way every time.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

SATURDAY = 5  # weekday() index

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})")
_DAY_TIME = re.compile(r"^([a-z]+)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.I)


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(moment):
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def from_iso(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_deadline(text, now=None):
    """Read a staff-typed deadline as an aware UTC datetime.

    Accepts '2026-09-11 18:00' and 'Friday 18:00' / 'fri 6pm'. A bare weekday
    means the next one strictly in the future, so typing 'Friday' on a Friday
    gets next Friday rather than a deadline that has already passed.

    Raises ValueError with something a human can act on.
    """
    now = now or utcnow()
    text = (text or "").strip()
    if not text:
        raise ValueError("Give a deadline, e.g. 'Friday 18:00' or '2026-09-11 18:00'.")

    iso = _ISO.match(text)
    if iso:
        year, month, day, hour, minute = (int(g) for g in iso.groups())
        if hour > 23 or minute > 59:
            raise ValueError("'{}' isn't a real time.".format(text))
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)

    named = _DAY_TIME.match(text)
    if named:
        word, hour, minute, meridiem = named.groups()
        weekday = WEEKDAYS.get(word.lower())
        if weekday is None:
            raise ValueError(
                "'{}' isn't a day I recognise. Try 'Friday 18:00'.".format(word)
            )
        hour = int(hour)
        minute = int(minute or 0)
        if meridiem:
            hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
        if hour > 23 or minute > 59:
            raise ValueError("'{}' isn't a real time.".format(text))
        ahead = (weekday - now.weekday()) % 7
        candidate = (now + timedelta(days=ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    raise ValueError(
        "Couldn't read '{}' as a deadline. Use 'Friday 18:00' or "
        "'2026-09-11 18:00' (UTC).".format(text)
    )


def week_of(moment):
    """The match weekend a deadline belongs to: the next Saturday on or after it.

    Friday fixtures sit the day *before* that Saturday, matching how the
    website anchors its three days, so a Friday deadline and a Friday kickoff
    still belong to the same weekend.
    """
    ahead = (SATURDAY - moment.weekday()) % 7
    return (moment + timedelta(days=ahead)).date().isoformat()


def week_saturday(week):
    return datetime.fromisoformat(week).replace(tzinfo=timezone.utc)


def slot_datetime(week, slot):
    """The real UTC moment a slot falls on, for Discord timestamps.

    Saturday is the anchor; Sunday is the day after and Friday the day before,
    so the three days stay in one weekend instead of drifting apart.
    """
    offsets = {"friday": -1, "saturday": 0, "sunday": 1}
    offset = offsets.get(slot.day.strip().lower(), 0)
    base = week_saturday(week) + timedelta(days=offset)
    return base + timedelta(minutes=slot.minutes)


def discord_time(moment, style="F"):
    """<t:...:F> - rendered in each reader's own timezone by their client."""
    return "<t:{}:{}>".format(int(moment.timestamp()), style)


def reminders_due(deadline, now, offsets_hours, already_sent):
    """Which reminder offsets should fire now.

    A reminder fires once its moment has passed and stays eligible until sent,
    so a bot that was asleep at the 12-hour mark still sends it on waking
    rather than skipping it silently.
    """
    due = []
    for hours in offsets_hours:
        label = "{}h".format(hours)
        if label in already_sent:
            continue
        if now >= deadline - timedelta(hours=hours) and now < deadline:
            due.append(label)
    return due
