"""The scheduling engine: pick a time for a fixture.

Two independent paths, deliberately kept separate:

  MANAGER_PREFERENCES - both managers submitted in Discord. Score every slot
      from the two preference values and take the best mutually acceptable one.

  AUTO_FALLBACK - the deadline passed without both submissions. Read the two
      teams' stored availability out of the timings sheet and pick at random
      from whatever is valid.

Everything here is pure: it takes data in and returns a decision. No Discord,
no database, no clock. That is what makes it testable, and scheduling is the
part that must not be quietly wrong.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import IntEnum


class Pref(IntEnum):
    """What a manager said about a slot."""

    NO = 0
    FINE = 1
    IDEAL = 2

    @property
    def emoji(self):
        return {0: "⚪", 1: "🟡", 2: "🟢"}[int(self)]

    @property
    def word(self):
        return {0: "NO", 1: "FINE", 2: "IDEAL"}[int(self)]

    def cycled(self):
        """NO -> IDEAL -> FINE -> NO, the click order from the spec."""
        return {Pref.NO: Pref.IDEAL, Pref.IDEAL: Pref.FINE, Pref.FINE: Pref.NO}[self]


class Source(str):
    """How a fixture got its time - recorded so staff can tell at a glance."""

    MANAGER_PREFERENCES = "MANAGER_PREFERENCES"
    AUTO_FALLBACK = "AUTO_FALLBACK"


class Status(str):
    WAITING_FOR_AVAILABILITY = "WAITING_FOR_AVAILABILITY"
    SCHEDULED = "SCHEDULED"
    FULLY_CONFIRMED = "FULLY_CONFIRMED"
    NEEDS_MANUAL_REF = "NEEDS_MANUAL_REF"
    NEEDS_MANUAL_SCHEDULING = "NEEDS_MANUAL_SCHEDULING"


def combined_score(home, away):
    """Score a slot from both managers' preferences, or None if unusable.

    NO from either side is a veto, not a low score - a manager who said no does
    not get overruled by an enthusiastic opponent.

        IDEAL + IDEAL = 4      FINE + FINE  = 2
        IDEAL + FINE  = 3      NO   + any   = None
    """
    if home == Pref.NO or away == Pref.NO:
        return None
    return int(home) + int(away)


@dataclass
class Candidate:
    """A slot a fixture could legitimately be put in."""

    slot: object
    score: int          # 2..4 for the preference path; equal for fallback
    load: int = 0       # fixtures already scheduled in this slot
    low_priority: bool = False

    @property
    def rank(self):
        """Sort key. Lower is better.

        Order from the spec: highest combined preference, then fewest
        conflicts. Low-priority days (the sheet's "Friday (Low Priority)") lose
        ties too, since the sheet is telling us it would rather not.
        """
        return (-self.score, self.load, self.low_priority)


@dataclass
class Decision:
    """What the engine chose, and enough of why to write a log line."""

    slot: object = None
    source: str = None
    status: str = Status.NEEDS_MANUAL_SCHEDULING
    considered: list = field(default_factory=list)
    reason: str = ""

    @property
    def scheduled(self):
        return self.slot is not None


def _busy_slots(team_fixtures):
    """Slot keys where a team already has a fixture.

    A hard exclusion, not a ranking penalty: a team cannot be in two places at
    once, so these slots are not candidates at all.
    """
    return {key for key in team_fixtures}


def candidates_from_preferences(slots, home_prefs, away_prefs, load=None, busy=()):
    """Valid slots with their combined scores, best first."""
    load = load or {}
    busy = set(busy)
    found = []
    for slot in slots:
        if slot.key in busy:
            continue
        score = combined_score(
            Pref(home_prefs.get(slot.key, Pref.NO)),
            Pref(away_prefs.get(slot.key, Pref.NO)),
        )
        if score is None:
            continue
        found.append(
            Candidate(
                slot=slot,
                score=score,
                load=load.get(slot.key, 0),
                low_priority=slot.low_priority,
            )
        )
    found.sort(key=lambda c: c.rank)
    return found


def candidates_from_sheet(slots, home_free, away_free, load=None, busy=()):
    """Valid slots from stored sheet availability.

    The sheet has no notion of "ideal" - a cell is green or it isn't - so every
    candidate scores the same and the choice comes down to conflicts and then
    chance, exactly as the fallback is meant to.
    """
    load = load or {}
    busy = set(busy)
    found = []
    for slot in slots:
        if slot.key in busy:
            continue
        if not (home_free.get(slot.key) and away_free.get(slot.key)):
            continue
        found.append(
            Candidate(
                slot=slot,
                score=0,
                load=load.get(slot.key, 0),
                low_priority=slot.low_priority,
            )
        )
    found.sort(key=lambda c: c.rank)
    return found


def choose(candidates, rng=None):
    """Pick from ranked candidates, breaking a genuine tie at random.

    The random step matters: without it every fixture lands in whichever slot
    happens to sort first, which would pile the whole league onto one kickoff
    time and leave no referees for it.
    """
    if not candidates:
        return None
    rng = rng or random
    best = candidates[0].rank
    tied = [c for c in candidates if c.rank == best]
    return rng.choice(tied) if len(tied) > 1 else tied[0]


def schedule_from_preferences(slots, home_prefs, away_prefs, load=None, busy=(), rng=None):
    candidates = candidates_from_preferences(slots, home_prefs, away_prefs, load, busy)
    picked = choose(candidates, rng)
    if picked is None:
        return Decision(
            considered=candidates,
            reason="both managers submitted but no slot is acceptable to both",
        )
    return Decision(
        slot=picked.slot,
        source=Source.MANAGER_PREFERENCES,
        status=Status.SCHEDULED,
        considered=candidates,
        reason="best mutually acceptable slot, score {}".format(picked.score),
    )


def schedule_from_sheet(slots, home_free, away_free, load=None, busy=(), rng=None):
    candidates = candidates_from_sheet(slots, home_free, away_free, load, busy)
    picked = choose(candidates, rng)
    if picked is None:
        return Decision(
            considered=candidates,
            reason="deadline passed and the teams' sheet availability does not overlap",
        )
    return Decision(
        slot=picked.slot,
        source=Source.AUTO_FALLBACK,
        status=Status.SCHEDULED,
        considered=candidates,
        reason="random pick from {} valid slot(s) in the timings sheet".format(
            len(candidates)
        ),
    )
