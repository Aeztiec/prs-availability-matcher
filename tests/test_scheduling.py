"""Tests for the scheduling engine.

Run with:  python -m tests.test_scheduling
"""

from __future__ import annotations

import random
import sys

from bot.domain.scheduling import (
    Pref,
    Source,
    Status,
    combined_score,
    schedule_from_preferences,
    schedule_from_sheet,
    schedule_randomly,
)
from bot.domain.slots import Slot, clean_day, parse_clock, slot_key, within_kickoff_window

FAILURES = []


def check(name, got, want):
    if got == want:
        print("  ok   {}".format(name))
    else:
        print("  FAIL {}\n         got:  {!r}\n         want: {!r}".format(name, got, want))
        FAILURES.append(name)


def make_slots(*specs):
    """('Saturday', 1080, False) -> Slot, in the given order."""
    out = []
    for index, (day, minutes, low) in enumerate(specs):
        hour = minutes // 60
        clock = "{}:{:02d} {}".format(hour % 12 or 12, minutes % 60, "PM" if hour >= 12 else "AM")
        out.append(Slot(
            key=slot_key(day, minutes), day=clean_day(day), day_index=index,
            clock=clock, minutes=minutes, low_priority=low,
        ))
    return out


# --------------------------------------------------------------------------
print("\nthe scoring table from the spec")
# --------------------------------------------------------------------------
check("IDEAL + IDEAL = 4", combined_score(Pref.IDEAL, Pref.IDEAL), 4)
check("IDEAL + FINE  = 3", combined_score(Pref.IDEAL, Pref.FINE), 3)
check("FINE  + IDEAL = 3", combined_score(Pref.FINE, Pref.IDEAL), 3)
check("FINE  + FINE  = 2", combined_score(Pref.FINE, Pref.FINE), 2)
check("NO vetoes IDEAL", combined_score(Pref.NO, Pref.IDEAL), None)
check("IDEAL vetoed by NO", combined_score(Pref.IDEAL, Pref.NO), None)
check("NO + NO invalid", combined_score(Pref.NO, Pref.NO), None)

print("\nthe click cycle: NO -> IDEAL -> FINE -> NO")
check("NO -> IDEAL", Pref.NO.cycled(), Pref.IDEAL)
check("IDEAL -> FINE", Pref.IDEAL.cycled(), Pref.FINE)
check("FINE -> NO", Pref.FINE.cycled(), Pref.NO)
check("four clicks returns to start", Pref.NO.cycled().cycled().cycled(), Pref.NO)

# --------------------------------------------------------------------------
print("\npicking the best mutually acceptable slot")
# --------------------------------------------------------------------------
slots = make_slots(
    ("Saturday", 17 * 60, False),   # sat_1700
    ("Saturday", 18 * 60, False),   # sat_1800
    ("Saturday", 19 * 60, False),   # sat_1900
    ("Sunday", 14 * 60, False),     # sun_1400
)

# The worked example from the spec: 17:00=3, 18:00=4, 19:00=3, sun 14:00=2.
home = {"sat_1700": Pref.FINE, "sat_1800": Pref.IDEAL, "sat_1900": Pref.IDEAL,
        "sun_1400": Pref.FINE}
away = {"sat_1700": Pref.IDEAL, "sat_1800": Pref.IDEAL, "sat_1900": Pref.FINE,
        "sun_1400": Pref.FINE}

decision = schedule_from_preferences(slots, home, away)
check("chose the score-4 slot", decision.slot.key, "sat_1800")
check("source recorded", decision.source, Source.MANAGER_PREFERENCES)
check("status recorded", decision.status, Status.SCHEDULED)
check("scores as the spec says",
      [(c.slot.key, c.score) for c in decision.considered],
      [("sat_1800", 4), ("sat_1700", 3), ("sat_1900", 3), ("sun_1400", 2)])

print("\na slot missing from a submission counts as NO, not as available")
sparse = schedule_from_preferences(slots, {"sat_1800": Pref.IDEAL}, away)
check("only the shared slot is valid", sparse.slot.key, "sat_1800")
check("nothing else considered", len(sparse.considered), 1)

print("\nno overlap at all")
none_shared = schedule_from_preferences(
    slots,
    {"sat_1700": Pref.IDEAL, "sat_1800": Pref.NO},
    {"sat_1700": Pref.NO, "sat_1800": Pref.IDEAL},
)
check("nothing scheduled", none_shared.scheduled, False)
check("flagged for staff", none_shared.status, Status.NEEDS_MANUAL_SCHEDULING)
check("reason explains it", "acceptable to both" in none_shared.reason, True)

# --------------------------------------------------------------------------
print("\ntie-breaks")
# --------------------------------------------------------------------------
both_ideal = {s.key: Pref.IDEAL for s in slots}

# Equal scores, different loads -> the emptier slot wins, so refs stay free.
loaded = schedule_from_preferences(
    slots, both_ideal, both_ideal,
    load={"sat_1700": 4, "sat_1800": 4, "sat_1900": 0, "sun_1400": 4},
)
check("fewest already-scheduled fixtures wins", loaded.slot.key, "sat_1900")

# A higher score still beats a lighter load - preference comes first.
score_beats_load = schedule_from_preferences(
    slots,
    {"sat_1700": Pref.FINE, "sat_1800": Pref.IDEAL},
    {"sat_1700": Pref.FINE, "sat_1800": Pref.IDEAL},
    load={"sat_1700": 0, "sat_1800": 9},
)
check("score outranks load", score_beats_load.slot.key, "sat_1800")

# Low-priority days lose an otherwise exact tie.
with_friday = make_slots(("Friday (Low Priority)", 18 * 60, True),
                         ("Saturday", 18 * 60, False))
friday_prefs = {s.key: Pref.IDEAL for s in with_friday}
avoid_friday = schedule_from_preferences(with_friday, friday_prefs, friday_prefs)
check("low-priority day loses the tie", avoid_friday.slot.key, "sat_1800")

print("\na genuine tie is broken at random, not by list order")
picks = set()
for seed in range(40):
    tied = schedule_from_preferences(
        slots, both_ideal, both_ideal, rng=random.Random(seed)
    )
    picks.add(tied.slot.key)
check("spreads across all four tied slots", picks, {s.key for s in slots})
check("same seed is reproducible",
      schedule_from_preferences(slots, both_ideal, both_ideal, rng=random.Random(7)).slot.key,
      schedule_from_preferences(slots, both_ideal, both_ideal, rng=random.Random(7)).slot.key)

print("\nteams already playing in a slot are excluded outright")
busy = schedule_from_preferences(
    slots, both_ideal, both_ideal, busy={"sat_1700", "sat_1800", "sat_1900"},
)
check("only the free slot remains", busy.slot.key, "sun_1400")
check("busy slots never considered",
      [c.slot.key for c in busy.considered], ["sun_1400"])

# --------------------------------------------------------------------------
print("\nthe fallback path (sheet availability, no ideal/fine distinction)")
# --------------------------------------------------------------------------
home_free = {"sat_1700": True, "sat_1800": True, "sat_1900": False, "sun_1400": True}
away_free = {"sat_1700": False, "sat_1800": True, "sat_1900": True, "sun_1400": True}

fallback = schedule_from_sheet(slots, home_free, away_free, rng=random.Random(1))
check("only slots green for both", sorted(c.slot.key for c in fallback.considered),
      ["sat_1800", "sun_1400"])
check("source marks it automatic", fallback.source, Source.AUTO_FALLBACK)
check("status recorded", fallback.status, Status.SCHEDULED)

print("\nthe fallback spreads its choice at random")
fallback_picks = {schedule_from_sheet(slots, home_free, away_free,
                                      rng=random.Random(s)).slot.key
                  for s in range(40)}
check("uses both valid slots", fallback_picks, {"sat_1800", "sun_1400"})

print("\nfallback with no overlap flags for staff")
no_overlap = schedule_from_sheet(
    slots, {"sat_1700": True}, {"sat_1800": True},
)
check("nothing scheduled", no_overlap.scheduled, False)
check("flagged", no_overlap.status, Status.NEEDS_MANUAL_SCHEDULING)
check("reason mentions the sheet", "sheet availability" in no_overlap.reason, True)

print("\nfallback respects load and busy too")
fb_loaded = schedule_from_sheet(
    slots, {k: True for k in ("sat_1700", "sat_1800", "sun_1400")},
    {k: True for k in ("sat_1700", "sat_1800", "sun_1400")},
    load={"sat_1700": 3, "sat_1800": 0, "sun_1400": 3},
)
check("emptiest slot wins", fb_loaded.slot.key, "sat_1800")

# --------------------------------------------------------------------------
print("\nrandom scheduling (testing only): ignores preferences entirely")
# --------------------------------------------------------------------------
random_picks = {schedule_randomly(slots, rng=random.Random(s)).slot.key
                for s in range(40)}
check("lands on more than one slot across enough tries", len(random_picks) > 1, True)
check("never picks outside the given slots",
      random_picks <= {s.key for s in slots}, True)

tagged = schedule_randomly(slots, rng=random.Random(0))
check("scheduled", tagged.scheduled, True)
check("tagged with the TEST source", tagged.source, Source.TEST)
check("status is SCHEDULED like any other path", tagged.status, Status.SCHEDULED)

print("\nrandom scheduling still respects a hard conflict")
constrained = schedule_randomly(
    slots, busy={"sat_1700", "sat_1800", "sun_1400"}, rng=random.Random(0),
)
check("only the one free slot is ever chosen", constrained.slot.key, "sat_1900")

print("\nno free slot left -> flagged, not silently skipped")
none_left = schedule_randomly(slots, busy={s.key for s in slots})
check("nothing scheduled", none_left.scheduled, False)
check("flagged for staff", none_left.status, Status.NEEDS_MANUAL_SCHEDULING)
check("reason says why", "no free slot" in none_left.reason, True)

# --------------------------------------------------------------------------
print("\nclock parsing")
# --------------------------------------------------------------------------
check("4:00 PM", parse_clock("4:00 PM"), 16 * 60)
check("12:00 PM is noon", parse_clock("12:00 PM"), 12 * 60)
check("12:30 AM is after midnight", parse_clock("12:30 AM"), 30)
check("11:30 AM", parse_clock("11:30 AM"), 11 * 60 + 30)
check("double space tolerated", parse_clock("12:00  PM"), 12 * 60)
check("junk rejected", parse_clock("later"), None)
check("slot keys are stable", slot_key("Friday (Low Priority)", 18 * 60), "fri_1800")

# --------------------------------------------------------------------------
print("\nthe daily kickoff window: 4pm-10pm GMT, nothing outside it")
# --------------------------------------------------------------------------
def at(minutes):
    return Slot(key="x", day="Saturday", day_index=0, clock="x", minutes=minutes,
               low_priority=False)


check("4pm itself is allowed", within_kickoff_window(at(16 * 60)), True)
check("10pm itself is allowed", within_kickoff_window(at(22 * 60)), True)
check("6pm, well inside, is allowed", within_kickoff_window(at(18 * 60)), True)
check("3:30pm is too early", within_kickoff_window(at(15 * 60 + 30)), False)
check("10:30pm is too late", within_kickoff_window(at(22 * 60 + 30)), False)
check("2am is nowhere near it", within_kickoff_window(at(2 * 60)), False)

# --------------------------------------------------------------------------
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all scheduling tests passed")
