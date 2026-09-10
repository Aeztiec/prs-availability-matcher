"""Dress rehearsal: run a whole gameweek and print every message it would send.

Touches no real data and connects to nothing. It uses a throwaway database and
fake Discord ids, drives the same code the live bot does, and prints the DMs,
the fixture board and the staff dashboard exactly as they would appear.

The point is to see the workflow end to end before showing it to forty people -
and to check it, not just eyeball it. Every scenario asserts its expected
outcome, so this exits non-zero if the automation makes a wrong decision.

    python -m bot.rehearse           full transcript
    python -m bot.rehearse --quiet   just the verdict
"""

from __future__ import annotations

import os
import random
import sys
import tempfile

import availability as av

from . import notify, referees, season
from .db import Store
from .fallback import Timings
from .orchestrator import Action, advance, dashboard
from .scheduling import Pref, Source, Status
from .selector import SelectorState, describe_choice
from .weeks import from_iso, to_iso

QUIET = "--quiet" in sys.argv
PROBLEMS = []

# Deterministic, so a rehearsal reads the same twice running.
RNG = random.Random(20260910)


def say(*parts):
    if not QUIET:
        print(*parts)


def rule(title):
    say("")
    say("=" * 72)
    say("  " + title)
    say("=" * 72)


def expect(label, got, want):
    ok = got == want
    say("    {} {}".format("PASS" if ok else "FAIL", label))
    if not ok:
        say("         got  {!r}".format(got))
        say("         want {!r}".format(want))
        PROBLEMS.append(label)


def show_dm(who, body):
    say("")
    say("    ┌─ DM to {} ".format(who) + "─" * max(0, 50 - len(who)))
    for line in body.splitlines():
        say("    │ " + line)
    say("    └" + "─" * 60)


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------

timings = Timings(None)
gw = season.gameweek("GW1")
store = Store(os.path.join(tempfile.mkdtemp(), "rehearsal.db"))

# Fake managers and referees. Real Discord ids are 17-19 digits; these are
# obviously not, which keeps a rehearsal from ever messaging a real person.
MANAGERS = {}
NEXT_ID = [1000]


def manager_for(team):
    if team not in MANAGERS:
        NEXT_ID[0] += 1
        MANAGERS[team] = NEXT_ID[0]
        store.set_manager(team, MANAGERS[team])
    return MANAGERS[team]


REFS = {}
for n, name in enumerate(["Ref Alice", "Ref Bo", "Ref Cass"], start=1):
    REFS[9000 + n] = name
    store.add_referee(9000 + n, name)
    # Registering a referee is not enough - they have to submit availability,
    # or they are not a candidate for anything. Worth seeing: the first run of
    # this rehearsal left it out and every fixture came back NEEDS_MANUAL_REF.
    store.save_ref_availability(
        9000 + n, gw.week,
        {slot.key: int(Pref.IDEAL) for slot in timings.slots}, submitted=True,
    )

# One extra referee who registered but never filled the selector in, to show
# that they are correctly never offered a game.
store.add_referee(9099, "Ref Dormant")


rule("SETUP  ·  what staff does once")
say("")
say("    Competition   : {}".format(timings.title))
say("    Gameweek      : {} — plays {}".format(gw.key, gw.friday.strftime("%a %d %b")))
say("    Deadline      : {} (the Wednesday before)".format(
    gw.deadline.strftime("%a %d %b %H:%M UTC")))
say("    Slots offered : {} across {}".format(
    len(timings.slots), ", ".join(sorted({s.day for s in timings.slots}))))
say("    Referees      : {}".format(", ".join(REFS.values())))
say("")
say("    Staff would run:  /managers set  (x40)   then  /refs add  (x3)")
say("                      then  /gw open GW1")


# --------------------------------------------------------------------------
# three fixtures, covering the three ways a gameweek goes
# --------------------------------------------------------------------------

def create(home_code, away_code):
    home, away = season.team_name(home_code), season.team_name(away_code)
    fixture_id = store.create_fixture(
        timings.competition.key, gw.week, home, away,
        manager_for(home), manager_for(away), to_iso(gw.deadline),
        Status.WAITING_FOR_AVAILABILITY, gameweek=gw.key,
    )
    return fixture_id, home, away


def submit(fixture_id, manager_id, picks):
    """Exactly what the Submit button does, minus the clicking."""
    state = SelectorState(timings.slots, saved=picks)
    store.save_submission(fixture_id, manager_id, state.as_dict(), submitted=True)
    store.mark_submitted(fixture_id, manager_id)
    return state


def slot_of(key):
    return next((s for s in timings.slots if s.key == key), None)


def offer_referee(fixture_id, decline_first=True):
    """Ref allocation, with the first ref declining to exercise that path."""
    fixture = store.fixture(fixture_id)
    slot = slot_of(fixture["slot_key"])
    first = referees.offer(store, fixture, slot.key, rng=RNG)
    if first is None:
        say("    (no referee available — fixture flagged NEEDS_MANUAL_REF)")
        return None
    show_dm(REFS[first.referee_id], notify.referee_offer(fixture, slot))
    if decline_first:
        say("    {} clicks Decline.".format(REFS[first.referee_id]))
        nxt = referees.decline(store, fixture_id, first.referee_id, slot.key, rng=RNG)
        if nxt is None:
            say("    (nobody left — flagged for staff)")
            return None
        say("    Next up: {}".format(REFS[nxt.referee_id]))
        first = nxt
    say("    {} clicks Accept.".format(REFS[first.referee_id]))
    referees.accept(store, fixture_id, first.referee_id)
    return first


# ---- 1. both managers reply ----------------------------------------------
rule("FIXTURE 1  ·  both managers reply  (the normal case)")

fid1, home1, away1 = create("ARS", "MCI")
fixture = store.fixture(fid1)
say("")
say("    {} vs {}   fixture #{}".format(home1, away1, fid1))
show_dm("{} manager".format(home1),
        notify.ask_for_availability(fixture, to_iso(gw.deadline)))
say("    Both managers click [Set availability] and fill the selector in.")

home_picks = {"sat_1800": Pref.IDEAL, "sat_1830": Pref.IDEAL, "sun_1700": Pref.FINE}
away_picks = {"sat_1800": Pref.IDEAL, "sat_1900": Pref.FINE, "sun_1700": Pref.FINE}
say("")
say("    {} says : {}".format(home1, describe_choice(submit(fid1, MANAGERS[home1], home_picks))))
say("    {} says : {}".format(away1, describe_choice(submit(fid1, MANAGERS[away1], away_picks))))

outcome = advance(store, timings, store.fixture(fid1), now=from_iso(to_iso(gw.deadline)),
                  rng=RNG)
say("")
say("    -> {}  ({})".format(outcome.action, outcome.detail))
expect("scheduled from manager preferences", outcome.decision.source,
       Source.MANAGER_PREFERENCES)
expect("took the slot both called ideal", outcome.decision.slot.key, "sat_1800")

fixture = store.fixture(fid1)
show_dm("both managers", notify.fixture_confirmed(fixture, slot_of(fixture["slot_key"])))
chosen_ref = offer_referee(fid1)
expect("fully confirmed", store.fixture(fid1)["status"], Status.FULLY_CONFIRMED)
if chosen_ref:
    show_dm(REFS[chosen_ref.referee_id],
            notify.referee_confirmed(store.fixture(fid1), slot_of(fixture["slot_key"])))


# ---- 2. only one replies -------------------------------------------------
rule("FIXTURE 2  ·  only one manager replies  (deadline fallback)")

fid2, home2, away2 = create("ASM", "LIL")
say("")
say("    {} vs {}   fixture #{}".format(home2, away2, fid2))
say("    {} submits. {} never replies.".format(home2, away2))
submit(fid2, MANAGERS[home2], {"sun_2200": Pref.IDEAL})

before = advance(store, timings, store.fixture(fid2),
                 now=gw.deadline.replace(hour=9), rng=RNG)
say("    Before Wednesday  -> {} ({})".format(before.action, before.detail))
expect("waits, does not schedule early", before.action, Action.WAIT)

fixture = store.fixture(fid2)
show_dm("{} manager".format(away2),
        notify.reminder(fixture, fixture["deadline"], "2h"))

after = advance(store, timings, store.fixture(fid2),
                now=gw.deadline.replace(hour=23, minute=59), rng=RNG)
say("")
say("    Wednesday passes  -> {}".format(after.action))
say("    {}".format(after.detail))
expect("falls back to the timings sheet", after.decision.source, Source.AUTO_FALLBACK)
expect("did NOT just take the one reply's pick",
       after.decision.slot.key != "sun_2200", True)

fixture = store.fixture(fid2)
show_dm("both managers", notify.fixture_confirmed(fixture, slot_of(fixture["slot_key"])))
offer_referee(fid2, decline_first=False)


# ---- 3. nobody replies and the sheet can't help --------------------------
rule("FIXTURE 3  ·  nobody replies and the sheet can't help  (needs staff)")

fid3, home3, away3 = create("TOT", "NEW")
say("")
say("    {} vs {}   fixture #{}".format(home3, away3, fid3))
say("    Neither manager replies, and {} has nothing on the timings sheet.".format(away3))

stuck = advance(store, timings, store.fixture(fid3),
                now=gw.deadline.replace(hour=23, minute=59), rng=RNG)
say("")
say("    -> {}".format(stuck.action))
say("    {}".format(stuck.detail))
expect("flagged rather than guessed", stuck.action, Action.NO_VALID_TIME)
expect("status set for staff", store.fixture(fid3)["status"],
       Status.NEEDS_MANUAL_SCHEDULING)

say("")
say("    Staff fixes it:  /fixture set fixture:{} slot:sat_2000".format(fid3))
store.set_schedule(fid3, "sat_2000", "MANUAL", Status.SCHEDULED)
store.note(fid3, "set by hand", "sat_2000 by a staff member")
fixture = store.fixture(fid3)
show_dm("both managers", notify.fixture_confirmed(fixture, slot_of("sat_2000")))
offer_referee(fid3, decline_first=False)


# --------------------------------------------------------------------------
rule("THE FIXTURE BOARD  ·  /fixture publish")
names = {r["discord_id"]: r["name"] for r in store.referees(active_only=False)}
for body in notify.fixture_board(store.fixtures(week=gw.week), gw.week, slot_of, names):
    say("")
    for line in body.splitlines():
        say("    " + line)

rule("THE STAFF DASHBOARD  ·  /fixture list")
buckets = dashboard(store, week=gw.week)
waiting_on = {f["id"]: store.unsubmitted_managers(f["id"])
              for f in store.fixtures(week=gw.week)}
asked = {f["id"]: store.refs_already_asked(f["id"])
         for f in store.fixtures(week=gw.week)}
reasons = {}
for f in store.fixtures(week=gw.week):
    for entry in reversed(store.history(f["id"])):
        if entry["event"].startswith("status -> NEEDS_MANUAL") and entry["detail"]:
            reasons[f["id"]] = entry["detail"]
            break
say("")
for line in notify.dashboard_summary(buckets, gw.week, waiting_on, reasons,
                                     asked).splitlines():
    say("    " + line)

rule("THE AUDIT TRAIL  ·  /fixture show 2")
say("")
for line in notify.fixture_detail(store.fixture(fid2), store.history(fid2),
                                  slot_of(store.fixture(fid2)["slot_key"])).splitlines():
    say("    " + line)


# --------------------------------------------------------------------------
rule("VERDICT")
say("")
confirmed = len(buckets["confirmed"])
expect("all three fixtures ended up with a time and a referee", confirmed, 3)
expect("nothing left needing a human",
       len(buckets["no_valid_time"]) + len(buckets["ref_needed"]), 0)

print("")
if PROBLEMS:
    print("REHEARSAL FAILED: {}".format(", ".join(PROBLEMS)))
    sys.exit(1)
print("Rehearsal passed — the full workflow behaved correctly end to end.")
