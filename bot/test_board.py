"""Tests for the master fixture list and the staff dashboard.

The length checks matter most. Discord rejects a message over 2000 characters
outright, so a 40-fixture week that renders to one oversized body would fail to
post at all - and it would only fail once the league got big, which is exactly
when nobody has time to debug it.

Run with:  python -m bot.test_board
"""

from __future__ import annotations

import os
import random
import sys
import tempfile

from bot.db import Store
from bot.notify import (
    BOARD_CHUNK, board_digest, board_row, dashboard_summary, fixture_board,
)
from bot.orchestrator import dashboard
from bot.scheduling import Source, Status
from bot.slots import Slot, clean_day, slot_key

FAILURES = []
DISCORD_LIMIT = 2000


def check(name, got, want):
    if got == want:
        print("  ok   {}".format(name))
    else:
        print("  FAIL {}\n         got:  {!r}\n         want: {!r}".format(name, got, want))
        FAILURES.append(name)


WEEK = "2026-09-12"


def make_slots():
    out = []
    for day_index, day in enumerate(["Saturday", "Sunday", "Friday (Low Priority)"]):
        for hour in range(16, 23):
            minutes = hour * 60
            out.append(Slot(
                key=slot_key(day, minutes), day=clean_day(day), day_index=day_index,
                clock="{}:00 PM".format(hour % 12 or 12), minutes=minutes,
                low_priority=(day_index == 2),
            ))
    return out


SLOTS = make_slots()
BY_KEY = {s.key: s for s in SLOTS}


def slot_for(key):
    return BY_KEY.get(key)


tmp = tempfile.mkdtemp()


def fresh_store():
    return Store(os.path.join(tmp, "board{}.db".format(random.randint(0, 10**9))))


def build_week(store, count, confirmed=True):
    """`count` fixtures spread across the week's slots."""
    ids = []
    for n in range(count):
        slot = SLOTS[(n % 3) * 7 + (n // 3) % 7]   # interleave the three days
        fid = store.create_fixture(
            "S17_Clubs", WEEK, "TEAM {} FC".format(n), "OPPO {} FC".format(n),
            1000 + n, 2000 + n, "2026-09-11T18:00:00Z",
            Status.WAITING_FOR_AVAILABILITY,
        )
        store.set_schedule(fid, slot.key, Source.MANAGER_PREFERENCES, Status.SCHEDULED)
        if confirmed:
            store.set_referee(fid, 900 + (n % 5))
            store.set_status(fid, Status.FULLY_CONFIRMED)
        ids.append(fid)
    return ids


# --------------------------------------------------------------------------
print("\none row of the master list")
# --------------------------------------------------------------------------
store = fresh_store()
fid = build_week(store, 1)[0]
fixture = store.fixture(fid)
row = board_row(fixture, slot_for(fixture["slot_key"]), referee_name="Ref One")
check("names both teams", "TEAM 0 FC" in row and "OPPO 0 FC" in row, True)
check("confirmed icon", row.startswith("✅"), True)
check("localised timestamp", "<t:" in row and ":t>" in row, True)
check("referee named", "Ref One" in row, True)
check("says who chose it", "managers" in row, True)

print("\nan unreffed fixture shows a dash rather than a blank")
store = fresh_store()
fid = build_week(store, 1, confirmed=False)[0]
unreffed = board_row(store.fixture(fid), slot_for(store.fixture(fid)["slot_key"]))
check("dash for no ref", "ref —" in unreffed, True)
check("orange icon", unreffed.startswith("🟠"), True)

# --------------------------------------------------------------------------
print("\na realistic 40-fixture week fits Discord's limits")
# --------------------------------------------------------------------------
store = fresh_store()
build_week(store, 40)
bodies = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for)
check("every body within Discord's 2000 chars",
      all(len(b) <= DISCORD_LIMIT for b in bodies), True)
check("longest body", max(len(b) for b in bodies) <= DISCORD_LIMIT, True)
print("       (split into {} message(s), longest {} chars)".format(
    len(bodies), max(len(b) for b in bodies)))
check("all 40 fixtures present",
      sum(b.count(" v ") for b in bodies), 40)
check("continuation marked when split",
      all("continued" in b for b in bodies[1:]), True)

print("\nan 80-fixture week still splits rather than truncating")
store = fresh_store()
build_week(store, 80)
big = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for)
check("within limits", all(len(b) <= DISCORD_LIMIT for b in big), True)
check("nothing dropped", sum(b.count(" v ") for b in big), 80)

# --------------------------------------------------------------------------
print("\ngrouping and dates")
# --------------------------------------------------------------------------
store = fresh_store()
build_week(store, 6)
grouped = "\n".join(fixture_board(store.fixtures(week=WEEK), WEEK, slot_for))
check("week heading has the date", "12 September 2026" in grouped, True)
check("Saturday dated", "Saturday 12 Sep" in grouped, True)
check("Sunday is the day after", "Sunday 13 Sep" in grouped, True)
check("Friday is the day before", "Friday 11 Sep" in grouped, True)
check("Saturday comes before Sunday",
      grouped.index("Saturday 12 Sep") < grouped.index("Sunday 13 Sep"), True)
check("a legend is included", "confirmed" in grouped and "needs a ref" in grouped, True)

print("\nunscheduled fixtures get their own section")
store = fresh_store()
build_week(store, 2)
store.create_fixture("S17_Clubs", WEEK, "LATE FC", "SLOW FC", 5, 6,
                     "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
mixed = "\n".join(fixture_board(store.fixtures(week=WEEK), WEEK, slot_for))
check("section present", "Not yet scheduled" in mixed, True)
check("the unscheduled fixture is listed", "LATE FC" in mixed, True)

print("\nan empty week says so instead of rendering a bare heading")
check("empty week", "No fixtures for this week yet" in
      "\n".join(fixture_board([], WEEK, slot_for)), True)

print("\na fixture whose slot the sheet no longer offers is not silently dropped")
store = fresh_store()
fid = store.create_fixture("S17_Clubs", WEEK, "OLD FC", "GONE FC", 7, 8,
                           "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.set_schedule(fid, "sat_1630", Source.MANAGER_PREFERENCES, Status.SCHEDULED)
orphan = "\n".join(fixture_board(store.fixtures(week=WEEK), WEEK, slot_for))
check("still shown", "OLD FC" in orphan, True)

# --------------------------------------------------------------------------
print("\nthe digest skips no-op edits")
# --------------------------------------------------------------------------
store = fresh_store()
build_week(store, 5)
first = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for)
check("stable for identical content", board_digest(first),
      board_digest(fixture_board(store.fixtures(week=WEEK), WEEK, slot_for)))
store.create_fixture("S17_Clubs", WEEK, "NEW FC", "EXTRA FC", 9, 10,
                     "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
check("changes when a fixture is added", board_digest(first) !=
      board_digest(fixture_board(store.fixtures(week=WEEK), WEEK, slot_for)), True)

# --------------------------------------------------------------------------
print("\nthe staff dashboard names who is blocking each fixture")
# --------------------------------------------------------------------------
store = fresh_store()
confirmed = build_week(store, 3)[0]

needs_ref = store.create_fixture("S17_Clubs", WEEK, "R1 FC", "R2 FC", 31, 32,
                                 "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.set_schedule(needs_ref, "sat_1800", Source.AUTO_FALLBACK, Status.SCHEDULED)
store.offer(needs_ref, 901)
store.resolve_offer(needs_ref, 901, "DECLINED")

stuck = store.create_fixture("S17_Clubs", WEEK, "S1 FC", "S2 FC", 41, 42,
                             "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.set_status(stuck, Status.NEEDS_MANUAL_SCHEDULING, "no overlapping availability")

waiting = store.create_fixture("S17_Clubs", WEEK, "W1 FC", "W2 FC", 51, 52,
                               "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.save_submission(waiting, 51, {"sat_1800": 2}, submitted=True)   # only home

buckets = dashboard(store, week=WEEK)
waiting_on = {f["id"]: store.unsubmitted_managers(f["id"]) for f in store.fixtures(week=WEEK)}
reasons = {stuck: "no overlapping availability"}
asked = {f["id"]: store.refs_already_asked(f["id"]) for f in store.fixtures(week=WEEK)}
body = dashboard_summary(buckets, WEEK, waiting_on, reasons, asked)

check("counts confirmed", "**3** fully confirmed" in body, True)
check("counts awaiting", "**1** awaiting response" in body, True)
check("counts ref needed", "**1** ref needed" in body, True)
check("counts no valid time", "**1** no valid time" in body, True)
check("names the manager who hasn't replied", "<@52>" in body, True)
check("does not chase the one who did", "<@51>" not in body, True)
check("gives the blocking reason", "no overlapping availability" in body, True)
check("says how many refs were already asked", "1 already asked" in body, True)
check("tells staff the fix for scheduling", "/fixture set" in body, True)
check("tells staff the fix for refs", "/refs assign" in body, True)
check("fits Discord's limit", len(body) <= DISCORD_LIMIT, True)

print("\na clean week says nothing needs attention")
store = fresh_store()
build_week(store, 4)
clean = dashboard_summary(dashboard(store, week=WEEK), WEEK)
check("all clear", "Nothing needs attention" in clean, True)
check("still shows the count", "**4** fully confirmed" in clean, True)

print("\na busy week truncates the lists but says how many are hidden")
store = fresh_store()
for n in range(15):
    fid = store.create_fixture("S17_Clubs", WEEK, "B{} FC".format(n), "C{} FC".format(n),
                               600 + n, 700 + n, "2026-09-11T18:00:00Z",
                               Status.WAITING_FOR_AVAILABILITY)
    store.set_status(fid, Status.NEEDS_MANUAL_SCHEDULING, "nothing overlaps")
busy = dashboard_summary(dashboard(store, week=WEEK), WEEK)
check("says how many more", "and 7 more" in busy, True)
check("still within the limit", len(busy) <= DISCORD_LIMIT, True)

# --------------------------------------------------------------------------
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all board and dashboard tests passed")
