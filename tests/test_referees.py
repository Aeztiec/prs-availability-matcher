"""Tests for first-come-first-served referee claiming.

Claiming is deliberately simple - no ranking, no fairness - so what is worth
pinning down is the handful of things that would make a claim nonsensical: an
inactive referee, a fixture with no kickoff yet, a manager claiming their own
game, a full roster, or being claimed twice into the same kickoff slot.

Run with:  python -m tests.test_referees
"""

from __future__ import annotations

import os
import random
import sys
import tempfile

from bot.db import Store
from bot.domain.referees import ROLE_AR, ROLE_REF, ClaimError, claim, drop, next_open_role
from bot.domain.scheduling import Source, Status

FAILURES = []


def check(name, got, want):
    if got == want:
        print("  ok   {}".format(name))
    else:
        print("  FAIL {}\n         got:  {!r}\n         want: {!r}".format(name, got, want))
        FAILURES.append(name)


def check_raises(name, fn, message_contains=None):
    try:
        fn()
    except ClaimError as error:
        ok = message_contains is None or message_contains in str(error)
        check(name, ok, True)
        return
    check(name, "no error raised", "ClaimError")


SLOT = "sat_1800"
WEEK = "2026-09-12"
tmp = tempfile.mkdtemp()


def fresh_store():
    return Store(os.path.join(tmp, "ref{}.db".format(random.randint(0, 10**9))))


def seeded_fixture(store, home_id=111, away_id=222, slot=SLOT):
    """A scheduled fixture, ready to be claimed."""
    fid = store.create_fixture("S17_Clubs", WEEK, "ABC FC", "XYZ FC", home_id, away_id,
                               "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
    store.set_schedule(fid, slot, Source.MANAGER_PREFERENCES, Status.SCHEDULED)
    return fid


# --------------------------------------------------------------------------
print("\nnext_open_role: referee first, then up to two assistants")
# --------------------------------------------------------------------------
check("empty roster needs a referee", next_open_role([]), ROLE_REF)
check("referee filled, needs an assistant", next_open_role([ROLE_REF]), ROLE_AR)
check("one assistant in, still room for another",
      next_open_role([ROLE_REF, ROLE_AR]), ROLE_AR)
check("full roster has nothing open",
      next_open_role([ROLE_REF, ROLE_AR, ROLE_AR]), None)
check("assistants alone don't count as a referee",
      next_open_role([ROLE_AR, ROLE_AR]), ROLE_REF)

# --------------------------------------------------------------------------
print("\nclaiming: first come, first served")
# --------------------------------------------------------------------------
store = fresh_store()
store.add_referee(1, "Ref One")
fid = seeded_fixture(store)

check("first claim takes the referee slot", claim(store, store.fixture(fid), 1), ROLE_REF)
check("fixture fully confirmed", store.fixture(fid)["status"], Status.FULLY_CONFIRMED)
check("roster shows the referee",
      [(r["referee_id"], r["role"]) for r in store.fixture_referees(fid)], [(1, ROLE_REF)])

store.add_referee(2, "Ref Two")
check("second claim becomes an assistant", claim(store, store.fixture(fid), 2), ROLE_AR)
store.add_referee(3, "Ref Three")
check("third claim, second assistant", claim(store, store.fixture(fid), 3), ROLE_AR)

print("\na full roster refuses a fourth claim")
store.add_referee(4, "Ref Four")
check_raises("fourth claim refused",
            lambda: claim(store, store.fixture(fid), 4), "full team")

# --------------------------------------------------------------------------
print("\nwho can't claim")
# --------------------------------------------------------------------------
store = fresh_store()
fid = seeded_fixture(store)

check_raises("unregistered referee", lambda: claim(store, store.fixture(fid), 999),
            "not registered")

store.add_referee(5, "Ref Five")
store.set_referee_active(5, False)
check_raises("deactivated referee", lambda: claim(store, store.fixture(fid), 5),
            "not registered")

store.add_referee(6, "Ref Six")
unscheduled = store.create_fixture("S17_Clubs", WEEK, "H FC", "A FC", 700, 701,
                                   "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
check_raises("no kickoff time yet", lambda: claim(store, store.fixture(unscheduled), 6),
            "kickoff time")

store.add_referee(111, "Home Manager")
check_raises("a manager cannot referee their own fixture",
            lambda: claim(store, store.fixture(fid), 111), "manage")

store.add_referee(7, "Ref Seven")
claim(store, store.fixture(fid), 7)
check_raises("already on this fixture", lambda: claim(store, store.fixture(fid), 7),
            "already on")

# --------------------------------------------------------------------------
print("\nnobody can be in two places at once")
# --------------------------------------------------------------------------
store = fresh_store()
store.add_referee(8, "Ref Eight")
a = seeded_fixture(store, home_id=101, away_id=102, slot=SLOT)
b = seeded_fixture(store, home_id=103, away_id=104, slot=SLOT)
claim(store, store.fixture(a), 8)
check_raises("same slot, different fixture, refused",
            lambda: claim(store, store.fixture(b), 8), "another game")

c = seeded_fixture(store, home_id=105, away_id=106, slot="sun_1700")
check("a different slot is fine", claim(store, store.fixture(c), 8), ROLE_REF)

# --------------------------------------------------------------------------
print("\ndropping out")
# --------------------------------------------------------------------------
store = fresh_store()
store.add_referee(9, "Ref Nine")
store.add_referee(10, "Ref Ten")
fid = seeded_fixture(store)
claim(store, store.fixture(fid), 9)
claim(store, store.fixture(fid), 10)

check("dropping the referee returns its role", drop(store, fid, 9), ROLE_REF)
check("status reverts once the referee slot empties",
      store.fixture(fid)["status"], Status.SCHEDULED)
check("the assistant is untouched",
      [r["referee_id"] for r in store.fixture_referees(fid)], [10])

check_raises("dropping someone not on the fixture",
            lambda: drop(store, fid, 999), "not on this fixture")

print("\nthe referee slot re-opens after a drop, and confirms again once refilled")
claim(store, store.fixture(fid), 9)
check("re-claimed", store.fixture(fid)["status"], Status.FULLY_CONFIRMED)

# --------------------------------------------------------------------------
print("\nthe log tells the story")
# --------------------------------------------------------------------------
events = [h["event"] for h in store.history(fid)]
check("claim, claim, drop, status change, claim, status change all present",
      [e for e in events if "referee" in e or e.startswith("status")],
      ["referee claimed", "status -> FULLY_CONFIRMED", "referee claimed",
       "referee dropped out", "status -> SCHEDULED", "referee claimed",
       "status -> FULLY_CONFIRMED"])

# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
print("\nfinding the games someone is officiating")
# --------------------------------------------------------------------------
mine = fresh_store()
mine.add_referee(7, "Ref Seven")
first = seeded_fixture(mine)
second = seeded_fixture(mine, slot="sun_1700")
other = seeded_fixture(mine, slot="sat_1900")
mine.claim_referee(first, 7, ROLE_REF)
mine.claim_referee(second, 7, ROLE_AR)
found = mine.fixtures_officiated_by(7)
check("every game they are on, none they are not", sorted(f["id"] for f in found), sorted([first, second]))
check("each comes with the role they hold", {f["id"]: f["role"] for f in found}, {first: ROLE_REF, second: ROLE_AR})
check("someone on nothing gets an empty list", mine.fixtures_officiated_by(999), [])
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all referee tests passed")
