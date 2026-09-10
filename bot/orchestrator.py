"""Deciding what to do with a fixture right now.

This is the loop from the spec's pseudocode, and it is deliberately separate
from both Discord and the scheduling maths:

    scheduling.py  decides WHICH slot            (pure)
    orchestrator.py decides WHETHER to decide    (this file)
    runner.py      does it on a timer            (Discord)

Keeping the sequencing here means the awkward cases - one manager submitted,
neither did, the deadline slipped past while the bot was down - can be tested
directly, which is where the real risk lives. Scheduling the wrong fixture at
the wrong moment is worse than picking a slightly worse slot.

Nothing here sends a message. It returns an Outcome describing what should
happen, and the caller does the talking. That way a test can assert "this
would have DM'd both managers" without a gateway.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .scheduling import Status, schedule_from_preferences, schedule_from_sheet
from .weeks import from_iso, reminders_due, utcnow


class Action:
    WAIT = "WAIT"                    # deadline is not here and someone is missing
    REMIND = "REMIND"                # nudge whoever hasn't answered
    SCHEDULED = "SCHEDULED"          # a slot was chosen
    NO_VALID_TIME = "NO_VALID_TIME"  # nothing works; staff must step in
    SKIP = "SKIP"                    # already scheduled, nothing to do


@dataclass
class Outcome:
    action: str
    fixture_id: int
    decision: object = None
    reminders: list = field(default_factory=list)
    notify: list = field(default_factory=list)   # discord ids to message
    detail: str = ""

    @property
    def scheduled(self):
        return self.action == Action.SCHEDULED


def _missing_managers(store, fixture):
    """Managers who have not pressed submit yet."""
    missing = []
    for manager_id in (fixture["home_manager_id"], fixture["away_manager_id"]):
        record = store.submission(fixture["id"], manager_id)
        if not record or not record["submitted"]:
            missing.append(manager_id)
    return missing


def _context(store, fixture):
    """Load and busy sets for the engine's conflict checks."""
    load = store.slot_load(fixture["week"], fixture["competition"])
    busy = store.busy_slots(
        fixture["week"],
        [fixture["home_team"], fixture["away_team"]],
        competition=fixture["competition"],
        ignore_fixture=fixture["id"],
    )
    return load, busy


def advance(store, timings, fixture, now=None, rng=None):
    """Decide and apply the next step for one fixture.

    Order matters. Both-submitted is checked before the deadline so a fixture
    that got its answers in on time is scheduled from real preferences even if
    the runner only gets round to it after the deadline - the fallback is for
    silence, not for lateness on our side.
    """
    now = now or utcnow()
    fixture_id = fixture["id"]

    if fixture["slot_key"]:
        return Outcome(Action.SKIP, fixture_id, detail="already scheduled")

    deadline = from_iso(fixture["deadline"])
    missing = _missing_managers(store, fixture)
    load, busy = _context(store, fixture)

    if not missing:
        home = store.submission(fixture_id, fixture["home_manager_id"])["slots"]
        away = store.submission(fixture_id, fixture["away_manager_id"])["slots"]
        decision = schedule_from_preferences(
            timings.slots, home, away, load=load, busy=busy, rng=rng
        )
        return _apply(store, fixture, decision)

    if now >= deadline:
        # Silence, or a half-answer. Either way we use what the teams already
        # told us in the sheet rather than one manager's preferences - letting
        # the sole responder pick unopposed would reward not replying.
        return _apply(store, fixture, _fallback(timings, fixture, load, busy, rng),
                      detail="deadline passed; {} did not submit".format(
                          ", ".join(str(m) for m in missing)))

    labels = reminders_due(
        deadline, now, _reminder_offsets(store),
        already_sent=_reminders_sent(fixture),
    )
    if labels:
        return Outcome(Action.REMIND, fixture_id, reminders=labels, notify=missing,
                       detail="reminding {} manager(s)".format(len(missing)))

    return Outcome(Action.WAIT, fixture_id, notify=missing,
                   detail="waiting on {} manager(s)".format(len(missing)))


def _reminder_offsets(store):
    from . import config
    return getattr(config, "REMINDERS", (12, 2))


def _reminders_sent(fixture):
    sent = set()
    if fixture.get("reminded_12h"):
        sent.add("12h")
    if fixture.get("reminded_2h"):
        sent.add("2h")
    return sent


def _fallback(timings, fixture, load, busy, rng):
    """Schedule from the teams' stored sheet availability.

    A team that is on the sheet but has submitted nothing there has no
    availability at all, which is treated as no valid time rather than as
    "free whenever" - inventing availability would put a fixture in front of
    people who never agreed to it.
    """
    try:
        home_free = timings.availability(fixture["home_team"])
        away_free = timings.availability(fixture["away_team"])
    except LookupError as error:
        from .scheduling import Decision
        return Decision(reason="team not found in the timings sheet: {}".format(error))
    return schedule_from_sheet(
        timings.slots, home_free, away_free, load=load, busy=busy, rng=rng
    )


def _apply(store, fixture, decision, detail=""):
    fixture_id = fixture["id"]
    everyone = [fixture["home_manager_id"], fixture["away_manager_id"]]

    if not decision.scheduled:
        store.set_status(fixture_id, Status.NEEDS_MANUAL_SCHEDULING, decision.reason)
        return Outcome(Action.NO_VALID_TIME, fixture_id, decision=decision,
                       detail=detail or decision.reason)

    store.set_schedule(fixture_id, decision.slot.key, decision.source, decision.status)
    if detail:
        store.note(fixture_id, "fallback used", detail)
    return Outcome(Action.SCHEDULED, fixture_id, decision=decision,
                   notify=everyone, detail=detail or decision.reason)


def run_once(store, timings, week=None, competition=None, now=None, rng=None):
    """Advance every unscheduled fixture. What the timer calls.

    Fixtures are handled oldest first so that when several are scheduled in the
    same pass, the earlier ones' slots count towards the later ones' conflict
    checks - otherwise a batch could all pile into the same slot.
    """
    outcomes = []
    for fixture in store.fixtures(week=week, competition=competition):
        if fixture["slot_key"]:
            continue
        if fixture["status"] == Status.NEEDS_MANUAL_SCHEDULING:
            continue  # staff have been told; don't churn on it every minute
        outcomes.append(advance(store, timings, fixture, now=now, rng=rng))
    return outcomes


def dashboard(store, week=None, competition=None):
    """The exception monitor from the spec's step 16."""
    fixtures = store.fixtures(week=week, competition=competition)
    buckets = {
        "confirmed": [], "awaiting": [], "ref_needed": [], "no_valid_time": [],
    }
    for fixture in fixtures:
        if fixture["status"] == Status.FULLY_CONFIRMED:
            buckets["confirmed"].append(fixture)
        elif fixture["status"] == Status.NEEDS_MANUAL_SCHEDULING:
            buckets["no_valid_time"].append(fixture)
        elif fixture["status"] == Status.NEEDS_MANUAL_REF or (
            fixture["slot_key"] and not fixture["referee_id"]
        ):
            buckets["ref_needed"].append(fixture)
        else:
            buckets["awaiting"].append(fixture)
    return buckets
