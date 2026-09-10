"""Tests for the database layer and the selector's logic.

Run with:  python -m bot.test_store
"""

from __future__ import annotations

import os
import sys
import tempfile

from bot.db import OFFER_ACCEPTED, OFFER_DECLINED, Store
from bot.scheduling import Pref, Source, Status, schedule_from_preferences
from bot.selector import SelectorState, describe_choice, fits_on_one_message, rows_needed
from bot.slots import Slot, clean_day, slot_key

FAILURES = []


def check(name, got, want):
    if got == want:
        print("  ok   {}".format(name))
    else:
        print("  FAIL {}\n         got:  {!r}\n         want: {!r}".format(name, got, want))
        FAILURES.append(name)


def make_slots(*specs):
    out = []
    for index, (day, minutes) in enumerate(specs):
        hour = minutes // 60
        clock = "{}:{:02d} {}".format(hour % 12 or 12, minutes % 60, "PM" if hour >= 12 else "AM")
        out.append(Slot(key=slot_key(day, minutes), day=clean_day(day), day_index=index,
                        clock=clock, minutes=minutes,
                        low_priority="low priority" in day.lower()))
    return out


SLOTS = make_slots(("Saturday", 17 * 60), ("Saturday", 18 * 60),
                   ("Sunday", 17 * 60), ("Sunday", 18 * 60))

WEEK = "2026-09-12"
tmp = tempfile.mkdtemp()
store = Store(os.path.join(tmp, "test.db"))

# --------------------------------------------------------------------------
print("\nfixtures")
# --------------------------------------------------------------------------
fid = store.create_fixture("S17_Clubs", WEEK, "ABC FC", "XYZ FC", 111, 222,
                           "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
check("fixture id assigned", fid, 1)
row = store.fixture(fid)
check("teams stored", (row["home_team"], row["away_team"]), ("ABC FC", "XYZ FC"))
check("starts waiting", row["status"], Status.WAITING_FOR_AVAILABILITY)
check("no slot yet", row["slot_key"], None)
check("creation is logged", store.history(fid)[0]["event"], "fixture created")

# --------------------------------------------------------------------------
print("\nsubmissions are editable until submitted, and stored apart from the fixture")
# --------------------------------------------------------------------------
check("nothing submitted yet", store.submission(fid, 111), None)

state = SelectorState(SLOTS)
check("everything defaults to NO", set(state.as_dict().values()), {0})

check("first click -> IDEAL", state.cycle("sat_1800"), Pref.IDEAL)
check("second click -> FINE", state.cycle("sat_1800"), Pref.FINE)
check("third click -> NO", state.cycle("sat_1800"), Pref.NO)
check("fourth click -> IDEAL again", state.cycle("sat_1800"), Pref.IDEAL)

state.cycle("sat_1700")          # IDEAL
state.cycle("sat_1700")          # FINE
store.save_submission(fid, 111, state.as_dict())
saved = store.submission(fid, 111)
check("picks persisted", saved["slots"], {"sat_1700": 1, "sat_1800": 2, "sun_1700": 0, "sun_1800": 0})
check("not yet flagged submitted", saved["submitted"], 0)
check("both_submitted is false", store.both_submitted(fid), False)

print("\nre-opening the selector restores what was saved")
reopened = SelectorState(SLOTS, saved=saved["slots"])
check("FINE restored", reopened.get("sat_1700"), Pref.FINE)
check("IDEAL restored", reopened.get("sat_1800"), Pref.IDEAL)
check("untouched stays NO", reopened.get("sun_1800"), Pref.NO)

print("\nediting after submitting keeps the submitted flag")
store.mark_submitted(fid, 111)
check("flag set", store.submission(fid, 111)["submitted"], 1)
reopened.cycle("sun_1800")
store.save_submission(fid, 111, reopened.as_dict())          # submitted defaults False
check("flag survives an edit", store.submission(fid, 111)["submitted"], 1)
check("edit was stored", store.submission(fid, 111)["slots"]["sun_1800"], 2)

# --------------------------------------------------------------------------
print("\nan all-NO submission is refused")
# --------------------------------------------------------------------------
empty = SelectorState(SLOTS)
check("blocked", empty.blocking_problem() is not None, True)
check("explains the fallback", "saved timings" in empty.blocking_problem(), True)
check("a single pick unblocks it",
      SelectorState(SLOTS, saved={"sun_1700": 1}).blocking_problem(), None)

# --------------------------------------------------------------------------
print("\nboth managers submitted -> the engine can run")
# --------------------------------------------------------------------------
away = SelectorState(SLOTS, saved={"sat_1800": 2, "sun_1700": 1})
store.save_submission(fid, 222, away.as_dict(), submitted=True)
check("both submitted", store.both_submitted(fid), True)

home_picks = store.submission(fid, 111)["slots"]
away_picks = store.submission(fid, 222)["slots"]
decision = schedule_from_preferences(SLOTS, home_picks, away_picks)
check("picked the shared IDEAL slot", decision.slot.key, "sat_1800")
store.set_schedule(fid, decision.slot.key, decision.source, decision.status)
check("fixture updated", store.fixture(fid)["slot_key"], "sat_1800")
check("source recorded", store.fixture(fid)["schedule_source"], Source.MANAGER_PREFERENCES)

# --------------------------------------------------------------------------
print("\nconflict context for the engine")
# --------------------------------------------------------------------------
check("slot load counts the scheduled fixture", store.slot_load(WEEK), {"sat_1800": 1})
check("ABC is busy then", store.busy_slots(WEEK, ["ABC FC"]), {"sat_1800"})
check("an uninvolved team is free", store.busy_slots(WEEK, ["OTHER FC"]), set())
check("rescheduling ignores its own slot",
      store.busy_slots(WEEK, ["ABC FC"], ignore_fixture=fid), set())

second = store.create_fixture("S17_Clubs", WEEK, "ABC FC", "DEF FC", 111, 333,
                              "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
both_ideal = {s.key: Pref.IDEAL for s in SLOTS}
avoid = schedule_from_preferences(
    SLOTS, both_ideal, both_ideal,
    load=store.slot_load(WEEK),
    busy=store.busy_slots(WEEK, ["ABC FC", "DEF FC"]),
)
check("ABC's second fixture avoids its first slot", avoid.slot.key != "sat_1800", True)

# --------------------------------------------------------------------------
print("\nreferees and fair workload")
# --------------------------------------------------------------------------
store.add_referee(901, "Ref One")
store.add_referee(902, "Ref Two")
store.add_referee(903, "Ref Three")
check("three active refs", len(store.referees()), 3)
store.set_referee_active(903, False)
check("deactivated ref hidden", [r["discord_id"] for r in store.referees()], [901, 902])
check("still listed when asked for all", len(store.referees(active_only=False)), 3)

store.save_ref_availability(901, WEEK, {"sat_1800": 2}, submitted=True)
check("ref availability round-trips", store.ref_availability(901, WEEK)["slots"], {"sat_1800": 2})
check("missing week is None", store.ref_availability(901, "2099-01-01"), None)

store.set_referee(fid, 901)
check("workload counted", store.ref_workload(WEEK), {901: 1})

print("\noffers: a decline never comes back to the same ref")
store.offer(fid, 901)
check("asked so far", store.refs_already_asked(fid), {901})
store.resolve_offer(fid, 901, OFFER_DECLINED)
store.offer(fid, 902)
check("both now asked", store.refs_already_asked(fid), {901, 902})
store.resolve_offer(fid, 902, OFFER_ACCEPTED)
store.set_referee(fid, 902)
store.set_status(fid, Status.FULLY_CONFIRMED)
check("final state", store.fixture(fid)["status"], Status.FULLY_CONFIRMED)

# --------------------------------------------------------------------------
print("\nthe scheduling log reads as a story")
# --------------------------------------------------------------------------
events = [h["event"] for h in store.history(fid)]
check("in order, nothing lost", events, [
    "fixture created", "manager submitted", "scheduled", "referee assigned",
    "referee offered", "referee declined", "referee offered", "referee accepted",
    "referee assigned", "status -> FULLY_CONFIRMED",
])

# --------------------------------------------------------------------------
print("\nreminders fire once each")
# --------------------------------------------------------------------------
check("not reminded yet", store.fixture(second)["reminded_12h"], 0)
store.mark_reminded(second, "12h")
check("12h marked", store.fixture(second)["reminded_12h"], 1)
check("2h untouched", store.fixture(second)["reminded_2h"], 0)

# --------------------------------------------------------------------------
print("\nlisting and filtering")
# --------------------------------------------------------------------------
check("two fixtures this week", len(store.fixtures(week=WEEK)), 2)
check("filter by status", [f["id"] for f in store.fixtures(status=Status.FULLY_CONFIRMED)], [fid])
check("other weeks empty", store.fixtures(week="2099-01-01"), [])

# --------------------------------------------------------------------------
print("\nDiscord's component budget")
# --------------------------------------------------------------------------
check("7 slots need 2 rows", rows_needed(7), 2)
check("15 slots need 3 rows", rows_needed(15), 3)
check("a real hourly day fits", fits_on_one_message(SelectorState(SLOTS)), True)
too_many = make_slots(*[("Saturday", 600 + 30 * i) for i in range(20)])
check("20 slots in a day does not fit",
      fits_on_one_message(SelectorState(too_many)), False)

print("\nsummaries")
check("describe_choice reads plainly",
      describe_choice(SelectorState(SLOTS, saved={"sat_1800": 2, "sun_1700": 1})),
      "Saturday 18:00 IDEAL, Sunday 17:00 FINE")
check("nothing chosen says so", describe_choice(SelectorState(SLOTS)), "nothing offered")
check("day summary hides NO slots",
      SelectorState(SLOTS, saved={"sat_1800": 2}).day_summary("Saturday"), "18:00 🟢")
check("empty day shows a dash",
      SelectorState(SLOTS, saved={"sat_1800": 2}).day_summary("Sunday"), "-")

# --------------------------------------------------------------------------
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all store and selector tests passed")
