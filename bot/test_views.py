"""Offline checks on the Discord layer.

The callbacks need a live gateway, so they are not exercised here. What IS
checkable without a token is the part most likely to be quietly broken: that
every custom_id a button emits matches the template that routes clicks back to
it, that ids stay inside Discord's 100-character cap, and that a real day's
worth of slots builds a view inside the 5-row / 25-component budget.

A mismatch between an emitted id and its template produces a button that looks
fine and does nothing when clicked, which is exactly the sort of thing that
would otherwise surface mid-deadline.

Run with:  python -m bot.test_views
"""

from __future__ import annotations

import re
import sys

import discord

from bot.scheduling import Pref
from bot.selector import SelectorState
from bot.slots import Slot, clean_day, slot_key
from bot.views import (
    DYNAMIC_ITEMS, SCOPE_FIXTURE, SCOPE_REF_WEEK,
    ClearButton, DayButton, SlotButton, SubmitButton, Target, build_view,
)

FAILURES = []


def check(name, got, want):
    if got == want:
        print("  ok   {}".format(name))
    else:
        print("  FAIL {}\n         got:  {!r}\n         want: {!r}".format(name, got, want))
        FAILURES.append(name)


def make_slots(day, count, start=16 * 60, step=60):
    out = []
    for i in range(count):
        minutes = start + i * step
        hour = minutes // 60
        clock = "{}:{:02d} {}".format(hour % 12 or 12, minutes % 60,
                                      "PM" if hour >= 12 else "AM")
        out.append(Slot(key=slot_key(day, minutes), day=clean_day(day), day_index=0,
                        clock=clock, minutes=minutes, low_priority=False))
    return out


# Mirrors the real thing: three days, hourly, 4pm-10pm.
SLOTS = []
for index, day in enumerate(["Saturday", "Sunday", "Friday (Low Priority)"]):
    for slot in make_slots(day, 7):
        SLOTS.append(Slot(key=slot.key, day=slot.day, day_index=index, clock=slot.clock,
                          minutes=slot.minutes, low_priority=(index == 2)))

FIXTURE_TARGET = Target(SCOPE_FIXTURE, 1024)
WEEK_TARGET = Target(SCOPE_REF_WEEK, "2026-09-12")


# --------------------------------------------------------------------------
print("\nevery emitted custom_id matches the template that routes it back")
# --------------------------------------------------------------------------
def emitted_ids(target):
    slot = SLOTS[0]
    return [
        (SlotButton, SlotButton(target, slot, Pref.NO).custom_id),
        (DayButton, DayButton(target, 1, "Sunday").custom_id),
        (SubmitButton, SubmitButton(target).custom_id),
        (ClearButton, ClearButton(target).custom_id),
    ]


for target, what in ((FIXTURE_TARGET, "fixture"), (WEEK_TARGET, "ref week")):
    for cls, custom_id in emitted_ids(target):
        pattern = cls.__discord_ui_compiled_template__
        check("{:<12} {:<14} routes: {}".format(what, cls.__name__, custom_id),
              bool(pattern.fullmatch(custom_id)), True)

print("\nids stay inside Discord's 100-character cap")
for target, what in ((FIXTURE_TARGET, "fixture"), (WEEK_TARGET, "ref week")):
    longest = max(len(cid) for _, cid in emitted_ids(target))
    check("{} longest id is {} chars".format(what, longest), longest <= 100, True)

print("\nthe captured groups come back with the right values")
slot_id = SlotButton(FIXTURE_TARGET, SLOTS[0], Pref.NO).custom_id
match = SlotButton.__discord_ui_compiled_template__.fullmatch(slot_id)
check("scope", match["scope"], "fx")
check("ref", match["ref"], "1024")
check("slot", match["slot"], SLOTS[0].key)

week_slot_id = SlotButton(WEEK_TARGET, SLOTS[0], Pref.NO).custom_id
week_match = SlotButton.__discord_ui_compiled_template__.fullmatch(week_slot_id)
check("a dated week survives the id", week_match["ref"], "2026-09-12")
check("scope for refs", week_match["scope"], "rw")

print("\ntemplates don't overlap - one id must route to exactly one button type")
for _, custom_id in emitted_ids(FIXTURE_TARGET):
    hits = [c.__name__ for c in DYNAMIC_ITEMS
            if c.__discord_ui_compiled_template__.fullmatch(custom_id)]
    check("{} -> {}".format(custom_id, hits), len(hits), 1)

# --------------------------------------------------------------------------
print("\nviews build inside Discord's component budget")
# --------------------------------------------------------------------------
class FakeStore:
    def fixture(self, fixture_id):
        return {"id": fixture_id, "home_team": "ABC FC", "away_team": "XYZ FC",
                "home_manager_id": 111, "away_manager_id": 222, "slot_key": None}


state = SelectorState(SLOTS)
view, active_day = build_view(FakeStore(), FIXTURE_TARGET, state)
check("opens on the first day", active_day, "Saturday")
check("3 day tabs + 7 slots + submit + clear", len(view.children), 12)
check("within the 25-component cap", len(view.children) <= 25, True)
rows = {item.row for item in view.children}
check("uses at most 5 rows", max(rows) <= 4, True)
counts = {}
for item in view.children:
    counts[item.row] = counts.get(item.row, 0) + 1
check("no row exceeds 5 buttons", max(counts.values()) <= 5, True)

print("\nswitching day shows that day's slots")
view_sunday, day = build_view(FakeStore(), FIXTURE_TARGET, state, "Sunday")
check("active day honoured", day, "Sunday")
labels = [i.item.label for i in view_sunday.children if i.custom_id.startswith("av:")]
check("7 Sunday slots", len(labels), 7)
check("labels carry the state emoji", all(l.startswith("⚪") for l in labels), True)

print("\nbutton labels reflect saved picks")
picked = SelectorState(SLOTS, saved={SLOTS[0].key: 2, SLOTS[1].key: 1})
view_picked, _ = build_view(FakeStore(), FIXTURE_TARGET, picked)
slot_labels = {i.custom_id.rsplit(":", 1)[1]: i.item.label
               for i in view_picked.children if i.custom_id.startswith("av:")}
check("IDEAL shows green", slot_labels[SLOTS[0].key].startswith("🟢"), True)
check("FINE shows yellow", slot_labels[SLOTS[1].key].startswith("🟡"), True)
check("untouched shows white", slot_labels[SLOTS[2].key].startswith("⚪"), True)

print("\na 13-slot half-hourly day still fits (granularity 30)")
half_hourly = [Slot(key=s.key, day=s.day, day_index=0, clock=s.clock,
                    minutes=s.minutes, low_priority=False)
               for s in make_slots("Saturday", 13, step=30)]
view_dense, _ = build_view(FakeStore(), Target(SCOPE_FIXTURE, 1), SelectorState(half_hourly))
dense_counts = {}
for item in view_dense.children:
    dense_counts[item.row] = dense_counts.get(item.row, 0) + 1
check("13 slots + submit + clear", len(view_dense.children), 15)
check("still within 25", len(view_dense.children) <= 25, True)
check("still at most 5 per row", max(dense_counts.values()) <= 5, True)
check("still at most 5 rows", max(dense_counts) <= 4, True)

# --------------------------------------------------------------------------
print("\nonly the fixture's own managers may answer")
# --------------------------------------------------------------------------
class RefStore(FakeStore):
    def referees(self):
        return [{"discord_id": 901, "name": "Ref One"}]


check("home manager may", FIXTURE_TARGET.may_answer(FakeStore(), 111), True)
check("away manager may", FIXTURE_TARGET.may_answer(FakeStore(), 222), True)
check("a stranger may not", FIXTURE_TARGET.may_answer(FakeStore(), 999), False)
check("registered ref may", WEEK_TARGET.may_answer(RefStore(), 901), True)
check("unregistered ref may not", WEEK_TARGET.may_answer(RefStore(), 902), False)

print("\nan already-scheduled fixture is closed to edits")
class ScheduledStore(FakeStore):
    def fixture(self, fixture_id):
        row = super().fixture(fixture_id)
        row["slot_key"] = "sat_1800"
        return row


check("closed with a reason",
      "already scheduled" in FIXTURE_TARGET.closed(ScheduledStore()), True)
check("open while unscheduled", FIXTURE_TARGET.closed(FakeStore()), None)
check("ref weeks never close", WEEK_TARGET.closed(FakeStore()), None)

# --------------------------------------------------------------------------
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all view-layer checks passed")
