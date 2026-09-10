"""Choosing a referee for a scheduled fixture.

Same shape as scheduling.py: the ranking is pure and the store-backed helpers
around it only gather inputs. Fairness is the thing worth testing - a system
that keeps picking the same two willing referees will burn them out, and that
failure is invisible until they stop volunteering.

Order of elimination, from the spec's step 12:

    active referees
      -> who said they can do that slot
      -> minus anyone already committed to that slot
      -> minus anyone already asked about this fixture
      -> ranked by workload, then by how keen they were
      -> ties broken at random
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .scheduling import Pref, Status


@dataclass
class RefCandidate:
    referee_id: int
    name: str
    workload: int      # games already assigned or offered this week
    preference: Pref   # what they said about this slot

    @property
    def rank(self):
        """Lower is better.

        Workload comes before preference on purpose: spreading games matters
        more than a referee's mild preference between two times they already
        said they could do.
        """
        return (self.workload, -int(self.preference))


def eligible(referees, availability, slot_key, workloads, busy_in_slot=(),
             already_asked=(), excluded=()):
    """Referees who could take this fixture, best first.

    `availability` is {referee_id: {slot_key: pref}}. A referee with no
    submission for the week is not a candidate - silence is not consent, the
    same rule the manager and sheet paths use.
    """
    busy_in_slot = set(busy_in_slot)
    already_asked = set(already_asked)
    excluded = set(excluded)

    found = []
    for referee in referees:
        referee_id = referee["discord_id"]
        if referee_id in busy_in_slot or referee_id in already_asked:
            continue
        if referee_id in excluded:
            continue
        said = (availability.get(referee_id) or {}).get(slot_key, Pref.NO)
        if Pref(int(said)) == Pref.NO:
            continue
        found.append(RefCandidate(
            referee_id=referee_id,
            name=referee["name"],
            workload=workloads.get(referee_id, 0),
            preference=Pref(int(said)),
        ))
    found.sort(key=lambda c: c.rank)
    return found


def choose(candidates, rng=None):
    """Pick from ranked candidates, breaking a real tie at random.

    Without the random step the same referee is picked every week - they sort
    first, so they get every game until their workload rises above everyone
    else's, then the next one does.
    """
    if not candidates:
        return None
    rng = rng or random
    best = candidates[0].rank
    tied = [c for c in candidates if c.rank == best]
    return rng.choice(tied) if len(tied) > 1 else tied[0]


# --------------------------------------------------------------------------
# store-backed helpers
# --------------------------------------------------------------------------

def gather(store, fixture, slot_key):
    """Everything eligible() needs, read from the database."""
    week = fixture["week"]
    competition = fixture["competition"]

    referees = store.referees()
    availability = {}
    for referee in referees:
        record = store.ref_availability(referee["discord_id"], week)
        if record and record["submitted"]:
            availability[referee["discord_id"]] = record["slots"]

    assigned = store.ref_workload(week, competition)
    pending = store.ref_pending_count(week, competition)
    workloads = {
        referee["discord_id"]: assigned.get(referee["discord_id"], 0)
                               + pending.get(referee["discord_id"], 0)
        for referee in referees
    }

    return {
        "referees": referees,
        "availability": availability,
        "workloads": workloads,
        "busy_in_slot": store.ref_slot_assignments(week, competition).get(slot_key, set()),
        "already_asked": store.refs_already_asked(fixture["id"]),
        # A referee should not officiate a fixture they are managing.
        "excluded": {fixture["home_manager_id"], fixture["away_manager_id"]},
    }


def next_referee(store, fixture, slot_key, rng=None):
    """The referee to offer this fixture to, or None if nobody is left."""
    inputs = gather(store, fixture, slot_key)
    return choose(eligible(slot_key=slot_key, **inputs), rng=rng)


def offer(store, fixture, slot_key, rng=None):
    """Record an offer to the best remaining referee.

    Returns the candidate, or None having flagged the fixture for staff.
    """
    candidate = next_referee(store, fixture, slot_key, rng=rng)
    if candidate is None:
        store.set_status(fixture["id"], Status.NEEDS_MANUAL_REF,
                         "no available referee left to ask")
        return None
    store.offer(fixture["id"], candidate.referee_id)
    return candidate


def accept(store, fixture_id, referee_id):
    """A referee said yes. Any other outstanding offer is withdrawn."""
    store.resolve_offer(fixture_id, referee_id, "ACCEPTED")
    store.withdraw_offers(fixture_id)
    store.set_referee(fixture_id, referee_id)
    store.set_status(fixture_id, Status.FULLY_CONFIRMED)
    return store.fixture(fixture_id)


def decline(store, fixture_id, referee_id, slot_key, rng=None):
    """A referee said no. Ask the next one.

    Returns the next candidate, or None if the fixture now needs a human.
    `refs_already_asked` includes the decliner, so nobody is asked twice.
    """
    store.resolve_offer(fixture_id, referee_id, "DECLINED")
    fixture = store.fixture(fixture_id)
    return offer(store, fixture, slot_key, rng=rng)
