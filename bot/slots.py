"""The slot vocabulary both scheduling paths share.

There are two ways a fixture gets a time: managers picking preferences in
Discord, and the fallback matcher reading the timings sheet. They have to agree
on what a slot *is*, or the fallback can hand back a time the selector never
offered and vice versa. So the slots come from one place - the sheet - and the
Discord selector is built from the same list.

The sheet is half-hourly (13 slots a day across three days, 39 in total), which
is more buttons than Discord will put on one message and more clicking than a
manager will tolerate. GRANULARITY_MINUTES thins that down; at 60 it gives 7
slots a day, which fits comfortably and still covers the same window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Only offer slots on this boundary. 30 uses every slot in the sheet; 60 uses
# only the ones on the hour. Applied to BOTH paths, never just one.
GRANULARITY_MINUTES = 60

_TIME = re.compile(r"^(\d{1,2}):(\d{2})\s*(AM|PM)$", re.I)


def parse_clock(clock):
    """'6:30 PM' -> minutes since midnight, or None if unparseable."""
    match = _TIME.match(clock.strip())
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == "PM":
        hour += 12
    return hour * 60 + int(match.group(2))


def clean_day(day):
    """'Friday (Low Priority)' -> 'Friday'."""
    return re.sub(r"\s*\(low priority\)", "", day, flags=re.I).strip()


@dataclass(frozen=True)
class Slot:
    """One offerable kickoff time.

    `key` is the stable identifier - it goes in the database and in Discord
    button custom_ids, so it must not change shape once fixtures reference it.
    """

    key: str            # "sat_1800"
    day: str            # "Saturday"
    day_index: int      # position of the day in the sheet, for ordering
    clock: str          # "6:00 PM" as the sheet writes it
    minutes: int        # minutes since midnight, GMT+0
    low_priority: bool  # the sheet marked this day low priority

    @property
    def label(self):
        hour, minute = divmod(self.minutes, 60)
        return "{:02d}:{:02d}".format(hour, minute)

    def __str__(self):
        return "{} {}".format(self.day, self.label)


def slot_key(day, minutes):
    hour, minute = divmod(minutes, 60)
    return "{}_{:02d}{:02d}".format(clean_day(day)[:3].lower(), hour, minute)


def build_slots(sheet, granularity=GRANULARITY_MINUTES):
    """The offerable slots for a parsed timings sheet, in sheet order.

    Two sheet columns can collapse onto one slot only if the sheet itself is
    malformed, so keys are de-duplicated defensively rather than trusted.
    """
    days = []
    for sheet_slot in sheet.slots:
        if sheet_slot.day not in days:
            days.append(sheet_slot.day)

    slots = []
    seen = set()
    for sheet_slot in sheet.slots:
        minutes = parse_clock(sheet_slot.utc)
        if minutes is None or minutes % granularity:
            continue
        key = slot_key(sheet_slot.day, minutes)
        if key in seen:
            continue
        seen.add(key)
        slots.append(
            Slot(
                key=key,
                day=clean_day(sheet_slot.day),
                day_index=days.index(sheet_slot.day),
                clock=sheet_slot.utc,
                minutes=minutes,
                low_priority=bool(re.search(r"low priority", sheet_slot.day, re.I)),
            )
        )
    return slots


def sheet_columns_for(sheet, slots):
    """Map slot key -> the sheet column index it came from.

    The fallback path needs this to read a team's green/red cells for exactly
    the slots the selector offers, so the two paths stay aligned.
    """
    wanted = {(s.day, s.minutes): s.key for s in slots}
    columns = {}
    for sheet_slot in sheet.slots:
        minutes = parse_clock(sheet_slot.utc)
        key = wanted.get((clean_day(sheet_slot.day), minutes))
        if key is not None and key not in columns:
            columns[key] = sheet_slot.col
    return columns
