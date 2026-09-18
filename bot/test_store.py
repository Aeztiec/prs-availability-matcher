"""Tests for the database layer and the selector's logic.

Run with:  python -m bot.test_store
"""

from __future__ import annotations

import os
import sys
import tempfile

from bot.db import Store
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
print("\nsubmissions are per week, editable until submitted, and stored apart from the fixture")
# --------------------------------------------------------------------------
check("nothing submitted yet", store.weekly_submission(WEEK, 111), None)

state = SelectorState(SLOTS)
check("everything defaults to NO", set(state.as_dict().values()), {0})

check("first click -> IDEAL", state.cycle("sat_1800"), Pref.IDEAL)
check("second click -> FINE", state.cycle("sat_1800"), Pref.FINE)
check("third click -> NO", state.cycle("sat_1800"), Pref.NO)
check("fourth click -> IDEAL again", state.cycle("sat_1800"), Pref.IDEAL)

state.cycle("sat_1700")          # IDEAL
state.cycle("sat_1700")          # FINE
store.save_weekly_submission(WEEK, 111, state.as_dict())
saved = store.weekly_submission(WEEK, 111)
check("picks persisted", saved["slots"], {"sat_1700": 1, "sat_1800": 2, "sun_1700": 0, "sun_1800": 0})
check("not yet flagged submitted", saved["submitted"], 0)
check("both_submitted is false", store.both_submitted(fid), False)

print("\nre-opening the selector restores what was saved")
reopened = SelectorState(SLOTS, saved=saved["slots"])
check("FINE restored", reopened.get("sat_1700"), Pref.FINE)
check("IDEAL restored", reopened.get("sat_1800"), Pref.IDEAL)
check("untouched stays NO", reopened.get("sun_1800"), Pref.NO)

print("\nediting after submitting keeps the submitted flag")
store.mark_weekly_submitted(WEEK, 111, [fid])
check("flag set", store.weekly_submission(WEEK, 111)["submitted"], 1)
check("logged against the fixture it covers", store.history(fid)[-1]["event"], "manager submitted")
reopened.cycle("sun_1800")
store.save_weekly_submission(WEEK, 111, reopened.as_dict())   # submitted defaults False
check("flag survives an edit", store.weekly_submission(WEEK, 111)["submitted"], 1)
check("edit was stored", store.weekly_submission(WEEK, 111)["slots"]["sun_1800"], 2)

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
store.save_weekly_submission(WEEK, 222, away.as_dict(), submitted=True)
check("both submitted", store.both_submitted(fid), True)

home_picks = store.weekly_submission(WEEK, 111)["slots"]
away_picks = store.weekly_submission(WEEK, 222)["slots"]
decision = schedule_from_preferences(SLOTS, home_picks, away_picks)
check("picked the shared IDEAL slot", decision.slot.key, "sat_1800")
store.set_schedule(fid, decision.slot.key, decision.source, decision.status)
check("fixture updated", store.fixture(fid)["slot_key"], "sat_1800")
check("source recorded", store.fixture(fid)["schedule_source"], Source.MANAGER_PREFERENCES)

# --------------------------------------------------------------------------
print("\na later week can carry forward a manager's last submitted picks")
# --------------------------------------------------------------------------
NEXT_WEEK = "2026-09-19"
check("nothing to carry forward before they've ever submitted",
      store.latest_weekly_submission(999, before_week=NEXT_WEEK), None)
check("the most recent submitted week comes back",
      store.latest_weekly_submission(111, before_week=NEXT_WEEK)["slots"],
      store.weekly_submission(WEEK, 111)["slots"])
check("nothing earlier than their only submission",
      store.latest_weekly_submission(111, before_week=WEEK), None)

unsubmitted_only = SelectorState(SLOTS, saved={"sat_1700": 1})
store.save_weekly_submission(NEXT_WEEK, 333, unsubmitted_only.as_dict())   # never submitted
check("a saved-but-not-submitted draft is not offered as a carry-forward",
      store.latest_weekly_submission(333, before_week="2026-09-26"), None)

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
check("111 manages ABC's second fixture too, with no fresh submission needed",
      store.weekly_submission(WEEK, 111)["slots"]["sat_1800"], 2)

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

check("is_active_referee true for an active ref", store.is_active_referee(901), True)
check("is_active_referee false for a deactivated one", store.is_active_referee(903), False)
check("is_active_referee false for an unknown id", store.is_active_referee(999), False)

store.claim_referee(fid, 901, "REF")
check("workload counted", store.ref_workload(WEEK), {901: 1})
check("roster has the referee",
      [(r["referee_id"], r["role"]) for r in store.fixture_referees(fid)], [(901, "REF")])

print("\na referee can't be committed to two games in the same slot")
store.set_schedule(second, "sat_1800", Source.MANAGER_PREFERENCES, Status.SCHEDULED)
check("committed in that slot", store.ref_committed_in_slot(WEEK, "sat_1800", 901), True)
check("not committed in a different slot",
      store.ref_committed_in_slot(WEEK, "sun_1700", 901), False)
check("excluding its own fixture clears it",
      store.ref_committed_in_slot(WEEK, "sat_1800", 901, exclude_fixture=fid), False)

print("\ndropping a referee removes them and says so")
check("drop returns True when someone was removed", store.drop_referee(fid, 901), True)
check("roster now empty", store.fixture_referees(fid), [])
check("drop returns False for someone not on it", store.drop_referee(fid, 901), False)

store.claim_referee(fid, 902, "REF")
store.set_status(fid, Status.FULLY_CONFIRMED)
check("final state", store.fixture(fid)["status"], Status.FULLY_CONFIRMED)

# --------------------------------------------------------------------------
print("\nthe scheduling log reads as a story")
# --------------------------------------------------------------------------
events = [h["event"] for h in store.history(fid)]
check("in order, nothing lost", events, [
    "fixture created", "manager submitted", "scheduled", "referee claimed",
    "referee dropped out", "referee claimed", "status -> FULLY_CONFIRMED",
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
print("\nreassigning a manager - team mapping vs. what's already snapshotted")
# --------------------------------------------------------------------------
store.set_manager("ABC FC", 601)
store.set_manager("XYZ FC", 602)
reassign_fid = store.create_fixture("S17_Clubs", WEEK, "ABC FC", "XYZ FC", 601, 602,
                                    "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)

check("set_manager alone leaves existing fixtures untouched",
      store.fixture(reassign_fid)["home_manager_id"], 601)
store.set_manager("ABC FC", 701)
check("the mapping moved on", store.manager_of("ABC FC"), 701)
check("but the fixture still remembers the old manager",
      store.fixture(reassign_fid)["home_manager_id"], 601)

store.reassign_fixture_managers("ABC FC", 701)
check("reassign_fixture_managers updates the home side",
      store.fixture(reassign_fid)["home_manager_id"], 701)
check("the away side is untouched", store.fixture(reassign_fid)["away_manager_id"], 602)

store.reassign_fixture_managers("XYZ FC", 702)
check("and it updates the away side too when that's the team",
      store.fixture(reassign_fid)["away_manager_id"], 702)

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
