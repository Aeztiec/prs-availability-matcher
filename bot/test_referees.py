"""Tests for referee allocation.

The fairness rules get the most attention, because an unfair allocator looks
like it works - it assigns a referee every time - and only shows up months
later when the same two people have done every game and stopped volunteering.

Run with:  python -m bot.test_referees
"""

from __future__ import annotations

import os
import random
import sys
import tempfile

from bot.db import OFFER_ACCEPTED, OFFER_DECLINED, OFFER_OFFERED, OFFER_SUPERSEDED, Store
from bot.referees import accept, choose, decline, eligible, gather, next_referee, offer
from bot.scheduling import Pref, Source, Status

FAILURES = []


def check(name, got, want):
    if got == want:
        print("  ok   {}".format(name))
    else:
        print("  FAIL {}\n         got:  {!r}\n         want: {!r}".format(name, got, want))
        FAILURES.append(name)


def refs(*ids):
    return [{"discord_id": i, "name": "Ref {}".format(i), "active": 1} for i in ids]


SLOT = "sat_1800"
WEEK = "2026-09-12"
tmp = tempfile.mkdtemp()


def fresh_store():
    return Store(os.path.join(tmp, "ref{}.db".format(random.randint(0, 10**9))))


def seeded_fixture(store, refs_available=None, workload_of=None, home_id=111, away_id=222):
    """A scheduled fixture plus referees who submitted availability."""
    fid = store.create_fixture("S17_Clubs", WEEK, "ABC FC", "XYZ FC", home_id, away_id,
                               "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
    store.set_schedule(fid, SLOT, Source.MANAGER_PREFERENCES, Status.SCHEDULED)
    for referee_id, picks in (refs_available or {}).items():
        store.add_referee(referee_id, "Ref {}".format(referee_id))
        store.save_ref_availability(referee_id, WEEK, picks, submitted=True)
    for referee_id, count in (workload_of or {}).items():
        for n in range(count):
            other = store.create_fixture("S17_Clubs", WEEK, "H{}".format(n), "A{}".format(n),
                                         900 + n, 950 + n, "2026-09-11T18:00:00Z",
                                         Status.WAITING_FOR_AVAILABILITY)
            store.set_schedule(other, "sun_1700", Source.AUTO_FALLBACK, Status.SCHEDULED)
            store.set_referee(other, referee_id)
    return fid


# --------------------------------------------------------------------------
print("\nwho is eligible at all")
# --------------------------------------------------------------------------
availability = {
    1: {SLOT: Pref.IDEAL},
    2: {SLOT: Pref.FINE},
    3: {SLOT: Pref.NO},
    4: {"sun_1700": Pref.IDEAL},      # available, but not for this slot
}
found = eligible(refs(1, 2, 3, 4, 5), availability, SLOT, workloads={})
check("only those who said fine or ideal", [c.referee_id for c in found], [1, 2])
check("a NO is excluded", 3 not in [c.referee_id for c in found], True)
check("another slot doesn't count", 4 not in [c.referee_id for c in found], True)
check("no submission at all is excluded", 5 not in [c.referee_id for c in found], True)

print("\nIDEAL outranks FINE when workload is level")
check("keenest first", [c.referee_id for c in found], [1, 2])

print("\nbut workload comes first")
loaded = eligible(refs(1, 2), availability, SLOT, workloads={1: 3, 2: 0})
check("the busy IDEAL ref loses to the free FINE ref",
      [c.referee_id for c in loaded], [2, 1])

print("\nhard exclusions")
check("already committed to that slot",
      [c.referee_id for c in eligible(refs(1, 2), availability, SLOT, {}, busy_in_slot={1})],
      [2])
check("already asked about this fixture",
      [c.referee_id for c in eligible(refs(1, 2), availability, SLOT, {}, already_asked={1})],
      [2])
check("a manager of the fixture cannot referee it",
      [c.referee_id for c in eligible(refs(1, 2), availability, SLOT, {}, excluded={1})],
      [2])
check("nobody left returns empty",
      eligible(refs(1), availability, SLOT, {}, already_asked={1}), [])

# --------------------------------------------------------------------------
print("\nties are broken at random, so one ref doesn't get everything")
# --------------------------------------------------------------------------
level = {i: {SLOT: Pref.IDEAL} for i in (1, 2, 3, 4)}
picks = {choose(eligible(refs(1, 2, 3, 4), level, SLOT, {}),
                rng=random.Random(seed)).referee_id
         for seed in range(40)}
check("spreads across all four", picks, {1, 2, 3, 4})
check("same seed reproducible",
      choose(eligible(refs(1, 2, 3, 4), level, SLOT, {}), rng=random.Random(9)).referee_id,
      choose(eligible(refs(1, 2, 3, 4), level, SLOT, {}), rng=random.Random(9)).referee_id)
check("empty candidate list -> None", choose([]), None)

# --------------------------------------------------------------------------
print("\nfairness over a season: 20 fixtures across 4 equally willing refs")
# --------------------------------------------------------------------------
store = fresh_store()
for referee_id in (1, 2, 3, 4):
    store.add_referee(referee_id, "Ref {}".format(referee_id))
    store.save_ref_availability(referee_id, WEEK, {"s{}".format(n): Pref.IDEAL
                                                   for n in range(20)}, submitted=True)
rng = random.Random(4)
tally = {}
for n in range(20):
    fid = store.create_fixture("S17_Clubs", WEEK, "H{}".format(n), "A{}".format(n),
                               800 + n, 850 + n, "2026-09-11T18:00:00Z",
                               Status.WAITING_FOR_AVAILABILITY)
    store.set_schedule(fid, "s{}".format(n), Source.MANAGER_PREFERENCES, Status.SCHEDULED)
    candidate = offer(store, store.fixture(fid), "s{}".format(n), rng=rng)
    accept(store, fid, candidate.referee_id)
    tally[candidate.referee_id] = tally.get(candidate.referee_id, 0) + 1

check("every ref used", sorted(tally), [1, 2, 3, 4])
check("evenly spread - 5 each", sorted(tally.values()), [5, 5, 5, 5])

# --------------------------------------------------------------------------
print("\noffering, accepting and declining end to end")
# --------------------------------------------------------------------------
store = fresh_store()
fid = seeded_fixture(store, refs_available={
    1: {SLOT: Pref.IDEAL}, 2: {SLOT: Pref.FINE}, 3: {SLOT: Pref.IDEAL},
})
first = offer(store, store.fixture(fid), SLOT, rng=random.Random(0))
check("someone was offered", first is not None, True)
check("offer recorded", store.refs_already_asked(fid), {first.referee_id})
check("fixture not confirmed yet", store.fixture(fid)["status"], Status.SCHEDULED)

second = decline(store, fid, first.referee_id, SLOT, rng=random.Random(0))
check("a different ref is asked next", second.referee_id != first.referee_id, True)
check("both now asked", store.refs_already_asked(fid),
      {first.referee_id, second.referee_id})

final = accept(store, fid, second.referee_id)
check("referee set", final["referee_id"], second.referee_id)
check("fully confirmed", final["status"], Status.FULLY_CONFIRMED)

print("\nthe log tells the story")
events = [h["event"] for h in store.history(fid)]
check("offer, decline, offer, accept all present",
      [e for e in events if "referee" in e],
      ["referee offered", "referee declined", "referee offered",
       "referee accepted", "referee assigned"])

# --------------------------------------------------------------------------
print("\neveryone declines -> flagged for staff, nobody asked twice")
# --------------------------------------------------------------------------
store = fresh_store()
fid = seeded_fixture(store, refs_available={1: {SLOT: Pref.IDEAL}, 2: {SLOT: Pref.FINE}})
asked = []
candidate = offer(store, store.fixture(fid), SLOT, rng=random.Random(1))
while candidate:
    asked.append(candidate.referee_id)
    candidate = decline(store, fid, candidate.referee_id, SLOT, rng=random.Random(1))

check("each ref asked exactly once", sorted(asked), [1, 2])
check("no repeats", len(asked), len(set(asked)))
check("flagged", store.fixture(fid)["status"], Status.NEEDS_MANUAL_REF)
check("reason logged",
      any("no available referee" in (h["detail"] or "") for h in store.history(fid)), True)

# --------------------------------------------------------------------------
print("\nno referees at all, or none available for that slot")
# --------------------------------------------------------------------------
store = fresh_store()
fid = seeded_fixture(store)
check("nobody registered -> flagged", offer(store, store.fixture(fid), SLOT), None)
check("status set", store.fixture(fid)["status"], Status.NEEDS_MANUAL_REF)

store = fresh_store()
fid = seeded_fixture(store, refs_available={1: {"sun_1700": Pref.IDEAL}})
check("registered but not for this slot -> flagged",
      offer(store, store.fixture(fid), SLOT), None)

print("\na ref who never submitted is not treated as available")
store = fresh_store()
fid = seeded_fixture(store)
store.add_referee(7, "Ref 7")
store.save_ref_availability(7, WEEK, {SLOT: Pref.IDEAL}, submitted=False)  # draft only
check("draft availability doesn't count", offer(store, store.fixture(fid), SLOT), None)

# --------------------------------------------------------------------------
print("\na manager cannot referee their own fixture")
# --------------------------------------------------------------------------
store = fresh_store()
fid = seeded_fixture(store, refs_available={111: {SLOT: Pref.IDEAL}}, home_id=111)
check("the home manager is not offered it", offer(store, store.fixture(fid), SLOT), None)

# --------------------------------------------------------------------------
print("\npending offers count towards workload")
# --------------------------------------------------------------------------
store = fresh_store()
for referee_id in (1, 2):
    store.add_referee(referee_id, "Ref {}".format(referee_id))
    store.save_ref_availability(referee_id, WEEK, {"a": Pref.IDEAL, "b": Pref.IDEAL},
                                submitted=True)
first_fid = store.create_fixture("S17_Clubs", WEEK, "H1", "A1", 501, 502,
                                 "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.set_schedule(first_fid, "a", Source.MANAGER_PREFERENCES, Status.SCHEDULED)
one = offer(store, store.fixture(first_fid), "a", rng=random.Random(0))

second_fid = store.create_fixture("S17_Clubs", WEEK, "H2", "A2", 503, 504,
                                  "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.set_schedule(second_fid, "b", Source.MANAGER_PREFERENCES, Status.SCHEDULED)
two = offer(store, store.fixture(second_fid), "b", rng=random.Random(0))
check("the second fixture goes to the other ref, not the one already asked",
      two.referee_id != one.referee_id, True)

print("\na ref already committed to a slot isn't offered another game in it")
store = fresh_store()
store.add_referee(1, "Ref 1")
store.save_ref_availability(1, WEEK, {SLOT: Pref.IDEAL}, submitted=True)
a = store.create_fixture("S17_Clubs", WEEK, "H1", "A1", 501, 502,
                         "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.set_schedule(a, SLOT, Source.MANAGER_PREFERENCES, Status.SCHEDULED)
store.set_referee(a, 1)
b = store.create_fixture("S17_Clubs", WEEK, "H2", "A2", 503, 504,
                         "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.set_schedule(b, SLOT, Source.MANAGER_PREFERENCES, Status.SCHEDULED)
check("same slot, so not offered", offer(store, store.fixture(b), SLOT), None)

print("\naccepting withdraws any other outstanding offer")
store = fresh_store()
fid = seeded_fixture(store, refs_available={1: {SLOT: Pref.IDEAL}, 2: {SLOT: Pref.IDEAL}})
c1 = offer(store, store.fixture(fid), SLOT, rng=random.Random(0))
store.offer(fid, 2 if c1.referee_id == 1 else 1)          # a second, racing offer
accept(store, fid, c1.referee_id)
with store._connect() as conn:
    states = sorted(r["state"] for r in conn.execute(
        "SELECT state FROM ref_offers WHERE fixture_id=?", (fid,)))
check("one accepted, the other superseded", states, [OFFER_ACCEPTED, OFFER_SUPERSEDED])

# --------------------------------------------------------------------------
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all referee tests passed")
