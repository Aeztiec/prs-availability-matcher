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

from bot import season
from bot.scheduling import Pref
from bot.selector import SelectorState
from bot.slots import Slot, clean_day, slot_key
from bot.views import (
    DYNAMIC_ITEMS, SCOPE_FIXTURE,
    ClearButton, DayButton, OpenButton, SlotButton, SubmitButton, Target,
    build_message, build_view, opener,
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

WEEK = "2026-09-19"
WEEK_TARGET = Target(SCOPE_FIXTURE, WEEK)


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
        (OpenButton, OpenButton(target).custom_id),
    ]


for cls, custom_id in emitted_ids(WEEK_TARGET):
    pattern = cls.__discord_ui_compiled_template__
    check("{:<14} routes: {}".format(cls.__name__, custom_id),
          bool(pattern.fullmatch(custom_id)), True)

print("\nids stay inside Discord's 100-character cap")
longest = max(len(cid) for _, cid in emitted_ids(WEEK_TARGET))
check("longest id is {} chars".format(longest), longest <= 100, True)

print("\nthe captured groups come back with the right values")
slot_id = SlotButton(WEEK_TARGET, SLOTS[0], Pref.NO).custom_id
match = SlotButton.__discord_ui_compiled_template__.fullmatch(slot_id)
check("scope", match["scope"], "fx")
check("ref is the week key, not a fixture id", match["ref"], WEEK)
check("slot", match["slot"], SLOTS[0].key)

print("\ntemplates don't overlap - one id must route to exactly one button type")
for _, custom_id in emitted_ids(WEEK_TARGET):
    hits = [c.__name__ for c in DYNAMIC_ITEMS
            if c.__discord_ui_compiled_template__.fullmatch(custom_id)]
    check("{} -> {}".format(custom_id, hits), len(hits), 1)

# --------------------------------------------------------------------------
print("\nviews build inside Discord's component budget")
# --------------------------------------------------------------------------
class FakeStore:
    """A manager's fixtures for one week - Target now resolves everything
    (may_answer, closed, heading) against this instead of a single fixture."""

    def __init__(self, fixtures=None, submissions=None):
        self._fixtures = fixtures if fixtures is not None else [
            {"id": 1024, "home_team": "ABC FC", "away_team": "XYZ FC",
             "home_manager_id": 111, "away_manager_id": 222,
             "slot_key": None, "gameweek": None, "week": WEEK},
        ]
        # {(week, manager_id): {"slots": {...}, "submitted": bool}}
        self._submissions = submissions or {}

    def fixtures(self, week=None):
        return [f for f in self._fixtures if week is None or f["week"] == week]

    def weekly_submission(self, week, manager_id):
        return self._submissions.get((week, manager_id))

    def latest_weekly_submission(self, manager_id, before_week=None):
        candidates = [
            (w, record) for (w, mid), record in self._submissions.items()
            if mid == manager_id and record.get("submitted")
            and (before_week is None or w < before_week)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda pair: pair[0])[1]


state = SelectorState(SLOTS)
view, active_day = build_view(WEEK_TARGET, state)
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
view_sunday, day = build_view(WEEK_TARGET, state, "Sunday")
check("active day honoured", day, "Sunday")
labels = [i.item.label for i in view_sunday.children if i.custom_id.startswith("av:")]
check("7 Sunday slots", len(labels), 7)
check("labels carry the state emoji", all(l.startswith("⚪") for l in labels), True)

print("\nbutton labels reflect saved picks")
picked = SelectorState(SLOTS, saved={SLOTS[0].key: 2, SLOTS[1].key: 1})
view_picked, _ = build_view(WEEK_TARGET, picked)
slot_labels = {i.custom_id.rsplit(":", 1)[1]: i.item.label
               for i in view_picked.children if i.custom_id.startswith("av:")}
check("IDEAL shows green", slot_labels[SLOTS[0].key].startswith("🟢"), True)
check("FINE shows yellow", slot_labels[SLOTS[1].key].startswith("🟡"), True)
check("untouched shows white", slot_labels[SLOTS[2].key].startswith("⚪"), True)

print("\na 13-slot half-hourly day still fits (granularity 30)")
half_hourly = [Slot(key=s.key, day=s.day, day_index=0, clock=s.clock,
                    minutes=s.minutes, low_priority=False)
               for s in make_slots("Saturday", 13, step=30)]
view_dense, _ = build_view(Target(SCOPE_FIXTURE, WEEK), SelectorState(half_hourly))
dense_counts = {}
for item in view_dense.children:
    dense_counts[item.row] = dense_counts.get(item.row, 0) + 1
check("13 slots + submit + clear", len(view_dense.children), 15)
check("still within 25", len(view_dense.children) <= 25, True)
check("still at most 5 per row", max(dense_counts.values()) <= 5, True)
check("still at most 5 rows", max(dense_counts) <= 4, True)

# --------------------------------------------------------------------------
print("\nonly a manager on one of the week's fixtures may answer")
# --------------------------------------------------------------------------
check("home manager may", WEEK_TARGET.may_answer(FakeStore(), 111), True)
check("away manager may", WEEK_TARGET.may_answer(FakeStore(), 222), True)
check("a stranger may not", WEEK_TARGET.may_answer(FakeStore(), 999), False)

print("\na manager who has no fixtures that week at all is closed, with a reason")
check("closed with a reason",
      "no fixtures" in WEEK_TARGET.closed(FakeStore(), 999), True)
check("open while unscheduled", WEEK_TARGET.closed(FakeStore(), 111), None)

print("\nonce every one of a manager's fixtures that week is scheduled, it closes")
scheduled_store = FakeStore(fixtures=[
    {"id": 1024, "home_team": "ABC FC", "away_team": "XYZ FC",
     "home_manager_id": 111, "away_manager_id": 222,
     "slot_key": "sat_1800", "gameweek": None, "week": WEEK},
])
check("closed with a reason",
      "already scheduled" in WEEK_TARGET.closed(scheduled_store, 111), True)

print("\none submission covers every fixture a manager has that week")
multi_store = FakeStore(fixtures=[
    {"id": 1024, "home_team": "ABC FC", "away_team": "XYZ FC",
     "home_manager_id": 111, "away_manager_id": 222,
     "slot_key": None, "gameweek": None, "week": WEEK},
    {"id": 1025, "home_team": "DEF FC", "away_team": "GHI FC",
     "home_manager_id": 111, "away_manager_id": 333,
     "slot_key": None, "gameweek": None, "week": WEEK},
])
check("still open with two fixtures to answer for",
      multi_store.fixtures(week=WEEK).__len__(), 2)
check("both list the shared manager",
      WEEK_TARGET.may_answer(multi_store, 111), True)
check("heading lists both of their fixtures",
      ("ABC FC vs XYZ FC" in WEEK_TARGET.heading(multi_store, 111)
       and "DEF FC vs GHI FC" in WEEK_TARGET.heading(multi_store, 111)), True)
check("each fixture on its own line, not comma-packed, no fixture numbers",
      WEEK_TARGET.heading(multi_store, 111),
      "**__Your fixtures:__** 📋\nABC FC vs XYZ FC\nDEF FC vs GHI FC")
check("a single-fixture manager gets the plain vs heading, no bold header",
      WEEK_TARGET.heading(multi_store, 222), "ABC FC vs XYZ FC")
check("only their fixtures are listed, not the other manager's",
      "DEF FC" in WEEK_TARGET.heading(multi_store, 222), False)

print("\neach row carries a DOM/UEFA tag, same as the referee board")
tagged_store = FakeStore(fixtures=[
    {"id": 1024, "home_team": "ABC FC", "away_team": "XYZ FC", "league": "PL",
     "home_manager_id": 111, "away_manager_id": 222,
     "slot_key": None, "gameweek": None, "week": WEEK},
    {"id": 1025, "home_team": "DEF FC", "away_team": "GHI FC", "league": "UCL",
     "home_manager_id": 111, "away_manager_id": 333,
     "slot_key": None, "gameweek": None, "week": WEEK},
])
tagged_heading = WEEK_TARGET.heading(tagged_store, 111)
check("a domestic league gets tagged DOM",
      "`(DOM)` ABC FC vs XYZ FC" in tagged_heading, True)
check("a competition fixture is tagged with its own code",
      "`(UCL)` DEF FC vs GHI FC" in tagged_heading, True)
check("a fixture with no league at all gets no tag",
      WEEK_TARGET.heading(multi_store, 111).startswith(
          "**__Your fixtures:__** 📋\nABC FC vs XYZ FC"), True)

# --------------------------------------------------------------------------
print("\na fresh week carries forward the manager's last submitted picks")
# --------------------------------------------------------------------------
PREV_WEEK, NEXT_WEEK = "2026-09-12", "2026-09-26"
carried = {SLOTS[0].key: 2, SLOTS[1].key: 1}
history_store = FakeStore(submissions={
    (PREV_WEEK, 111): {"slots": carried, "submitted": True},
})
picks, submitted = Target(SCOPE_FIXTURE, NEXT_WEEK).load(history_store, 111)
check("the new week starts from what they last submitted", picks, carried)
check("but is not itself marked submitted - it's a starting point", submitted, False)

check("a manager with no history yet starts blank",
      Target(SCOPE_FIXTURE, NEXT_WEEK).load(history_store, 999), ({}, False))

print("\nthis week's own submission always wins over an older one")
current_and_history = FakeStore(submissions={
    (PREV_WEEK, 111): {"slots": carried, "submitted": True},
    (WEEK, 111): {"slots": {SLOTS[2].key: 2}, "submitted": True},
})
this_week_picks, this_week_submitted = WEEK_TARGET.load(current_and_history, 111)
check("this week's own picks, not carried forward", this_week_picks, {SLOTS[2].key: 2})
check("and it reports as actually submitted", this_week_submitted, True)

print("\nan unsubmitted draft from before is never carried forward")
draft_only = FakeStore(submissions={
    (PREV_WEEK, 111): {"slots": carried, "submitted": False},
})
check("still blank - a draft was never a real answer",
      Target(SCOPE_FIXTURE, NEXT_WEEK).load(draft_only, 111), ({}, False))

# --------------------------------------------------------------------------
print("\nthe selector's own message is an embed, like everything else the bot posts")
# --------------------------------------------------------------------------
badged_store = FakeStore(fixtures=[
    {"id": 7, "home_team": season.team_name("AST"), "away_team": season.team_name("MCI"),
     "home_manager_id": 111, "away_manager_id": 222,
     "slot_key": None, "gameweek": None, "week": WEEK},
])
picked_state = SelectorState(SLOTS, saved={SLOTS[0].key: 2})
embed, embed_day = build_message(badged_store, WEEK_TARGET, 111, picked_state)
check("titled generically - the fixtures themselves are in the body",
      embed.title, "Set your availability")
check("real team badges render in the body, not plain names",
      season.TEAM_EMOJI["ASTON VILLA"] in embed.description, True)
check("the GMT/legend note is still there", "GMT+0" in embed.description, True)
check("one field per day, matching the active-day resolution",
      [name for name, _, _ in embed.fields], picked_state.days)
check("defaults to the first day, same as build_view",
      embed_day, picked_state.days[0])
check("the footer explains that it's saved for next time",
      "next time" in embed.footer, True)

print("\na submitted state says so in the body")
submitted_state = SelectorState(SLOTS, saved={SLOTS[0].key: 2}, submitted=True)
submitted_embed, _ = build_message(badged_store, WEEK_TARGET, 111, submitted_state)
check("submitted note appended", "Submitted" in submitted_embed.description, True)

# --------------------------------------------------------------------------
print("\nthe DM opener button")
# --------------------------------------------------------------------------
# "avo:" must not be swallowed by the "av:" slot template - if it were, the DM
# button would route to a slot handler and cycle a nonexistent slot.
open_id = OpenButton(WEEK_TARGET).custom_id
check("opener id", open_id, "avo:fx:{}".format(WEEK))
check("does not match the slot template",
      bool(SlotButton.__discord_ui_compiled_template__.fullmatch(open_id)), False)
check("a slot id does not match the opener template",
      bool(OpenButton.__discord_ui_compiled_template__.fullmatch(
          "av:fx:{}:sat_1600".format(WEEK))), False)
view_dm = opener(WEEK_TARGET)
check("one button on the DM view", len(view_dm.children), 1)
check("labelled for a human", view_dm.children[0].item.label, "Set availability")

print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all view-layer checks passed (including the opener)")
