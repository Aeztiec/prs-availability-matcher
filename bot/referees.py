"""Officiating a scheduled fixture: first come, first served.

A fixture takes at most one REF (the referee) and two AR (assistant/VAR)
claims. Whoever clicks first gets the open role - there is no availability to
submit beforehand and no ranked offer to wait on. The only checks are the ones
that would make a claim nonsensical: not a registered active referee, already
on this fixture, the fixture's roles are already full, or already officiating
another fixture at the same kickoff time.
"""

from __future__ import annotations

from .scheduling import Status

ROLE_REF = "REF"
ROLE_AR = "AR"
MAX_ASSISTANTS = 2

ROLE_LABEL = {ROLE_REF: "referee", ROLE_AR: "assistant"}


class ClaimError(Exception):
    """Why a claim or drop-out was refused, phrased so it can be shown as-is."""


def next_open_role(roles):
    """The role a new claim would take, or None if the fixture is fully staffed.

    `roles` is the list of role strings already claimed. The referee slot
    fills first; only once it is taken do assistant slots become claimable -
    a game with no referee at all is the one that actually can't be played.
    """
    if ROLE_REF not in roles:
        return ROLE_REF
    if roles.count(ROLE_AR) < MAX_ASSISTANTS:
        return ROLE_AR
    return None


def claim(store, fixture, referee_id):
    """Give the next open role on `fixture` to `referee_id`, or raise ClaimError."""
    if not store.is_active_referee(referee_id):
        raise ClaimError(
            "You're not registered as an active referee. Ask an Official to "
            "add you with `/refs register`."
        )
    if not fixture["slot_key"]:
        raise ClaimError("This fixture doesn't have a kickoff time yet.")
    if referee_id in (fixture["home_manager_id"], fixture["away_manager_id"]):
        raise ClaimError("You can't referee a fixture you manage.")

    rows = store.fixture_referees(fixture["id"])
    if any(r["referee_id"] == referee_id for r in rows):
        raise ClaimError("You're already on this fixture.")

    role = next_open_role([r["role"] for r in rows])
    if role is None:
        raise ClaimError("This fixture already has a full team of officials.")

    if store.ref_committed_in_slot(fixture["week"], fixture["slot_key"], referee_id,
                                   exclude_fixture=fixture["id"]):
        raise ClaimError("You're already officiating another game at that kickoff time.")

    store.claim_referee(fixture["id"], referee_id, role)
    if role == ROLE_REF:
        store.set_status(fixture["id"], Status.FULLY_CONFIRMED)
    return role


def drop(store, fixture_id, referee_id):
    """Take `referee_id` off a fixture they claimed, or raise ClaimError."""
    rows = store.fixture_referees(fixture_id)
    mine = next((r for r in rows if r["referee_id"] == referee_id), None)
    if not mine:
        raise ClaimError("You're not on this fixture.")

    store.drop_referee(fixture_id, referee_id)
    if mine["role"] == ROLE_REF:
        fixture = store.fixture(fixture_id)
        if fixture and fixture["status"] == Status.FULLY_CONFIRMED:
            store.set_status(fixture_id, Status.SCHEDULED, "referee dropped out")
    return mine["role"]
