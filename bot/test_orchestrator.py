"""Tests for deadlines, weekends and the fixture sequencing.

Covers the spec's three response cases - both managers, one, neither - plus the
awkward ones it doesn't mention: the deadline slipping past while the bot was
down, a team missing from the sheet, and a batch of fixtures scheduled in one
pass all trying to take the same slot.

Run with:  python -m bot.test_orchestrator
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from bot.db import Store
from bot.orchestrator import Action, advance, dashboard, run_once
from bot.scheduling import Pref, Source, Status
from bot.slots import Slot, clean_day, slot_key
from bot.weeks import (
    discord_time, parse_deadline, reminders_due, slot_datetime, to_iso, week_of,
)

FAILURES = []


def check(name, got, want):
    if got == want:
        print("  ok   {}".format(name))
    else:
        print("  FAIL {}\n         got:  {!r}\n         want: {!r}".format(name, got, want))
        FAILURES.append(name)


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def make_slots(*specs):
    out = []
    for index, (day, minutes) in enumerate(specs):
        hour = minutes // 60
        clock = "{}:{:02d} {}".format(hour % 12 or 12, minutes % 60,
                                      "PM" if hour >= 12 else "AM")
        out.append(Slot(key=slot_key(day, minutes), day=clean_day(day), day_index=index,
                        clock=clock, minutes=minutes,
                        low_priority="low priority" in day.lower()))
    return out


SLOTS = make_slots(("Saturday", 17 * 60), ("Saturday", 18 * 60),
                   ("Sunday", 17 * 60), ("Sunday", 18 * 60))


class FakeTimings:
    """Stands in for the sheet, so fallback behaviour is controllable."""

    def __init__(self, availability=None):
        self.slots = SLOTS
        self._availability = availability or {}

    def availability(self, team):
        if team not in self._availability:
            raise LookupError("no team called '{}'".format(team))
        return self._availability[team]


WEEK = "2026-09-12"          # a Saturday
DEADLINE = utc(2026, 9, 11, 18)
BEFORE = utc(2026, 9, 10, 9)      # >12h out, so no reminder is due yet
AFTER = utc(2026, 9, 11, 19)

tmp = tempfile.mkdtemp()


def fresh_store():
    path = os.path.join(tmp, "orch{}.db".format(random.randint(0, 10**9)))
    return Store(path)


def new_fixture(store, home="ABC FC", away="XYZ FC", home_id=111, away_id=222):
    return store.create_fixture("S17_Clubs", WEEK, home, away, home_id, away_id,
                                to_iso(DEADLINE), Status.WAITING_FOR_AVAILABILITY)


# --------------------------------------------------------------------------
print("\ndeadline parsing")
# --------------------------------------------------------------------------
now = utc(2026, 9, 8, 12)     # a Tuesday
check("ISO with space", parse_deadline("2026-09-11 18:00", now), DEADLINE)
check("ISO with T", parse_deadline("2026-09-11T18:00", now), DEADLINE)
check("named day", parse_deadline("Friday 18:00", now), DEADLINE)
check("short day", parse_deadline("fri 18:00", now), DEADLINE)
check("12-hour clock", parse_deadline("Friday 6pm", now), DEADLINE)
check("bare hour", parse_deadline("Friday 18", now), DEADLINE)

print("\na weekday already gone today rolls to next week")
friday_evening = utc(2026, 9, 11, 20)
check("Friday 18:00 asked on Friday 20:00 -> next Friday",
      parse_deadline("Friday 18:00", friday_evening), utc(2026, 9, 18, 18))
check("later today is still today",
      parse_deadline("Friday 22:00", friday_evening), utc(2026, 9, 11, 22))

print("\nbad input explains itself")
for bad, expect in [("", "Give a deadline"), ("whenever", "Couldn't read"),
                    ("Frunday 18:00", "isn't a day I recognise"),
                    ("Friday 99:00", "isn't a real time")]:
    try:
        parse_deadline(bad, now)
        check("{!r} rejected".format(bad), "no error", "an error")
    except ValueError as error:
        check("{!r} -> {}".format(bad, expect), expect in str(error), True)

# --------------------------------------------------------------------------
print("\nmatch weekends")
# --------------------------------------------------------------------------
check("Friday deadline -> that Saturday", week_of(DEADLINE), "2026-09-12")
check("Saturday is its own week", week_of(utc(2026, 9, 12, 10)), "2026-09-12")
check("Sunday rolls to next Saturday", week_of(utc(2026, 9, 13, 10)), "2026-09-19")
check("Monday -> that Saturday", week_of(utc(2026, 9, 7, 10)), "2026-09-12")

print("\nslots land on real dates, with Friday before Saturday")
sat = slot_datetime(WEEK, SLOTS[1])          # Saturday 18:00
check("Saturday 18:00", sat, utc(2026, 9, 12, 18))
check("Sunday is the day after", slot_datetime(WEEK, SLOTS[3]), utc(2026, 9, 13, 18))
friday_slot = make_slots(("Friday (Low Priority)", 18 * 60))[0]
check("Friday is the day before", slot_datetime(WEEK, friday_slot), utc(2026, 9, 11, 18))
check("discord timestamp format", discord_time(sat), "<t:{}:F>".format(int(sat.timestamp())))

# --------------------------------------------------------------------------
print("\nreminders fire once, and survive the bot being asleep")
# --------------------------------------------------------------------------
check("nothing due 3 days out", reminders_due(DEADLINE, utc(2026, 9, 8), (12, 2), set()), [])
check("12h due", reminders_due(DEADLINE, utc(2026, 9, 11, 7), (12, 2), set()), ["12h"])
check("both due if 12h was missed",
      reminders_due(DEADLINE, utc(2026, 9, 11, 17), (12, 2), set()), ["12h", "2h"])
check("already-sent skipped",
      reminders_due(DEADLINE, utc(2026, 9, 11, 17), (12, 2), {"12h"}), ["2h"])
check("nothing after the deadline",
      reminders_due(DEADLINE, AFTER, (12, 2), set()), [])

# --------------------------------------------------------------------------
print("\nspec step 7: both managers respond")
# --------------------------------------------------------------------------
store = fresh_store()
fid = new_fixture(store)
store.save_submission(fid, 111, {"sat_1800": 2, "sun_1700": 1}, submitted=True)
store.save_submission(fid, 222, {"sat_1800": 2, "sun_1700": 2}, submitted=True)

outcome = advance(store, FakeTimings(), store.fixture(fid), now=BEFORE)
check("scheduled", outcome.action, Action.SCHEDULED)
check("took the score-4 slot", outcome.decision.slot.key, "sat_1800")
check("source is managers", outcome.decision.source, Source.MANAGER_PREFERENCES)
check("both managers notified", sorted(outcome.notify), [111, 222])
check("persisted", store.fixture(fid)["slot_key"], "sat_1800")

print("\nlate submissions still beat the fallback")
store = fresh_store()
fid = new_fixture(store)
store.save_submission(fid, 111, {"sat_1700": 2}, submitted=True)
store.save_submission(fid, 222, {"sat_1700": 2}, submitted=True)
late = advance(store, FakeTimings(), store.fixture(fid), now=AFTER)
check("uses preferences even after the deadline", late.decision.source,
      Source.MANAGER_PREFERENCES)
check("not the fallback", late.decision.slot.key, "sat_1700")

print("\nboth submitted but nothing overlaps -> staff")
store = fresh_store()
fid = new_fixture(store)
store.save_submission(fid, 111, {"sat_1700": 2, "sat_1800": 0}, submitted=True)
store.save_submission(fid, 222, {"sat_1700": 0, "sat_1800": 2}, submitted=True)
clash = advance(store, FakeTimings(), store.fixture(fid), now=BEFORE)
check("flagged", clash.action, Action.NO_VALID_TIME)
check("status set", store.fixture(fid)["status"], Status.NEEDS_MANUAL_SCHEDULING)

# --------------------------------------------------------------------------
print("\nspec step 8: only one manager responds")
# --------------------------------------------------------------------------
sheet = {"ABC FC": {"sat_1700": True, "sat_1800": True, "sun_1700": False, "sun_1800": False},
         "XYZ FC": {"sat_1700": False, "sat_1800": True, "sun_1700": True, "sun_1800": False}}
store = fresh_store()
fid = new_fixture(store)
store.save_submission(fid, 111, {"sun_1800": 2}, submitted=True)   # only home replied

before = advance(store, FakeTimings(sheet), store.fixture(fid), now=BEFORE)
check("before the deadline it waits", before.action, Action.WAIT)
check("waiting on the away manager", before.notify, [222])
check("nothing scheduled yet", store.fixture(fid)["slot_key"], None)

after = advance(store, FakeTimings(sheet), store.fixture(fid), now=AFTER,
                rng=random.Random(0))
check("at the deadline it falls back", after.decision.source, Source.AUTO_FALLBACK)
check("used the sheet overlap, not the one reply", after.decision.slot.key, "sat_1800")
check("the sole responder's own pick was not honoured",
      after.decision.slot.key != "sun_1800", True)
check("why is logged",
      any("did not submit" in h.get("detail") or "" for h in store.history(fid)), True)

# --------------------------------------------------------------------------
print("\nspec step 9: neither responds")
# --------------------------------------------------------------------------
store = fresh_store()
fid = new_fixture(store)
silent = advance(store, FakeTimings(sheet), store.fixture(fid), now=AFTER,
                 rng=random.Random(0))
check("falls back", silent.decision.source, Source.AUTO_FALLBACK)
check("only the shared sheet slot", silent.decision.slot.key, "sat_1800")

print("\na team missing from the sheet is flagged, not guessed")
store = fresh_store()
fid = new_fixture(store, home="GHOST FC")
ghost = advance(store, FakeTimings(sheet), store.fixture(fid), now=AFTER)
check("flagged for staff", ghost.action, Action.NO_VALID_TIME)
check("reason names the sheet", "timings sheet" in ghost.decision.reason, True)

print("\na team with nothing on the sheet is not treated as free whenever")
store = fresh_store()
fid = new_fixture(store)
blank = {"ABC FC": {k.key: False for k in SLOTS}, "XYZ FC": sheet["XYZ FC"]}
nothing = advance(store, FakeTimings(blank), store.fixture(fid), now=AFTER)
check("no valid time", nothing.action, Action.NO_VALID_TIME)

# --------------------------------------------------------------------------
print("\nreminders through advance()")
# --------------------------------------------------------------------------
store = fresh_store()
fid = new_fixture(store)
due = advance(store, FakeTimings(sheet), store.fixture(fid), now=utc(2026, 9, 11, 7))
check("reminder outcome", due.action, Action.REMIND)
check("which reminder", due.reminders, ["12h"])
check("both managers chased", sorted(due.notify), [111, 222])
store.mark_reminded(fid, "12h")
again = advance(store, FakeTimings(sheet), store.fixture(fid), now=utc(2026, 9, 11, 8))
check("not repeated", again.action, Action.WAIT)

# --------------------------------------------------------------------------
print("\nalready-scheduled fixtures are left alone")
# --------------------------------------------------------------------------
store = fresh_store()
fid = new_fixture(store)
store.set_schedule(fid, "sat_1800", Source.MANAGER_PREFERENCES, Status.SCHEDULED)
check("skipped", advance(store, FakeTimings(sheet), store.fixture(fid), now=AFTER).action,
      Action.SKIP)

# --------------------------------------------------------------------------
print("\na batch in one pass does not all pile into the same slot")
# --------------------------------------------------------------------------
store = fresh_store()
everyone_free = {}
ids = []
for n in range(4):
    home, away = "H{} FC".format(n), "A{} FC".format(n)
    everyone_free[home] = {s.key: True for s in SLOTS}
    everyone_free[away] = {s.key: True for s in SLOTS}
    ids.append(new_fixture(store, home=home, away=away,
                           home_id=1000 + n, away_id=2000 + n))

outcomes = run_once(store, FakeTimings(everyone_free), week=WEEK, now=AFTER,
                    rng=random.Random(5))
check("all four scheduled", [o.action for o in outcomes], [Action.SCHEDULED] * 4)
chosen = [store.fixture(i)["slot_key"] for i in ids]
check("spread across slots, not stacked", len(set(chosen)), 4)

print("\nrun_once leaves flagged fixtures alone instead of churning")
store = fresh_store()
fid = new_fixture(store)
store.set_status(fid, Status.NEEDS_MANUAL_SCHEDULING, "no overlap")
check("not reprocessed", run_once(store, FakeTimings(sheet), week=WEEK, now=AFTER), [])

# --------------------------------------------------------------------------
print("\nthe staff dashboard buckets fixtures by what needs a human")
# --------------------------------------------------------------------------
store = fresh_store()
confirmed = new_fixture(store, home="C1", away="C2", home_id=1, away_id=2)
store.set_schedule(confirmed, "sat_1800", Source.MANAGER_PREFERENCES, Status.SCHEDULED)
store.set_referee(confirmed, 901)
store.set_status(confirmed, Status.FULLY_CONFIRMED)

needs_ref = new_fixture(store, home="R1", away="R2", home_id=3, away_id=4)
store.set_schedule(needs_ref, "sun_1700", Source.AUTO_FALLBACK, Status.SCHEDULED)

stuck = new_fixture(store, home="S1", away="S2", home_id=5, away_id=6)
store.set_status(stuck, Status.NEEDS_MANUAL_SCHEDULING, "no overlap")

waiting = new_fixture(store, home="W1", away="W2", home_id=7, away_id=8)

buckets = dashboard(store, week=WEEK)
check("confirmed", [f["id"] for f in buckets["confirmed"]], [confirmed])
check("needs a ref", [f["id"] for f in buckets["ref_needed"]], [needs_ref])
check("no valid time", [f["id"] for f in buckets["no_valid_time"]], [stuck])
check("still awaiting", [f["id"] for f in buckets["awaiting"]], [waiting])

# --------------------------------------------------------------------------
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all orchestrator tests passed")
