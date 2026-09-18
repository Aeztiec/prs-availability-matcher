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
from datetime import timedelta

import availability as av

from . import notify, referees, season
from .db import Store
from .fallback import Timings
from .orchestrator import Action, advance, dashboard
from .scheduling import Pref, Source, Status
from .selector import SelectorState, describe_choice
from .weeks import from_iso, to_iso, uk_time

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


def say_embed(embed, prefix="    "):
    """Print a notify.BoardEmbed the way Discord would show the real thing:
    title, description, fields, then footer."""
    if embed.title:
        say(prefix + "[ {} ]".format(embed.title))
    for line in embed.description.splitlines():
        say(prefix + line)
    for name, value, _ in embed.fields:
        first, *rest = "{}: {}".format(name, value).split(chr(10))
        say(prefix + first)
        for extra in rest:
            say(prefix + "  " + extra)
    if embed.footer:
        say(prefix + "-# " + embed.footer)


def expect(label, got, want):
    ok = got == want
    say("    {} {}".format("PASS" if ok else "FAIL", label))
    if not ok:
        say("         got  {!r}".format(got))
        say("         want {!r}".format(want))
        PROBLEMS.append(label)


def show_post(where, embed, content=None):
    """Print an embed the way it would appear in a channel."""
    say("")
    say("    ┌─ Posted in {} ".format(where) + "─" * max(0, 50 - len(where)))
    if content:
        say("    │ " + content)
    say_embed(embed, prefix="    │ ")
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

# A deactivated referee, to show they are correctly refused a claim.
store.add_referee(9099, "Ref Dormant")
store.set_referee_active(9099, False)


rule("SETUP  ·  what staff does once")
say("")
say("    Competition   : {}".format(timings.title))
say("    Gameweek      : {} (plays {})".format(gw.key, gw.friday.strftime("%a %d %b")))
_deadline_local, _deadline_label = uk_time(gw.deadline)
say("    Deadline      : {} {} (Wednesday's close)".format(
    _deadline_local.strftime("%a %d %b %H:%M"), _deadline_label))
say("    Slots offered : {} across {}".format(
    len(timings.slots), ", ".join(sorted({s.day for s in timings.slots}))))
say("    Referees      : {}".format(", ".join(REFS.values())))
say("")
say("    Staff would run:  /managers set  (x40)   then  /refs register  (x3)")
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
    """Exactly what the Submit button does, minus the clicking - one weekly
    submission, logged against whichever fixture prompted it here."""
    state = SelectorState(timings.slots, saved=picks)
    week = store.fixture(fixture_id)["week"]
    store.save_weekly_submission(week, manager_id, state.as_dict(), submitted=True)
    store.mark_weekly_submitted(week, manager_id, [fixture_id])
    return state


def slot_of(key):
    return next((s for s in timings.slots if s.key == key), None)


def open_referee_count():
    """Fixtures still missing a role of any kind - main ref or an assistant."""
    open_count = 0
    for f in store.fixtures(week=gw.week):
        if not f["slot_key"]:
            continue
        roles = [r["role"] for r in store.fixture_referees(f["id"])]
        if referees.next_open_role(roles) is not None:
            open_count += 1
    return open_count


def unreffed_count():
    """Fixtures with no main referee at all - the one that actually blocks play."""
    return sum(
        1 for f in store.fixtures(week=gw.week)
        if f["slot_key"] and not any(
            r["role"] == referees.ROLE_REF for r in store.fixture_referees(f["id"])
        )
    )


def show_ref_board():
    rosters = {f["id"]: store.fixture_referees(f["id"]) for f in store.fixtures(week=gw.week)}
    say("")
    say("    ┌─ Posted in #referees " + "─" * 40)
    bodies = notify.referee_board(store.fixtures(week=gw.week), gw.week, slot_of,
                                  rosters=rosters, gameweek=gw)
    for body in bodies:
        say_embed(body, prefix="    │ ")
        say("    │ ")
    say_embed(notify.referee_claim_prompt(open_referee_count()), prefix="    │ ")
    say("    └" + "─" * 60)


def claim_referee(fixture_id, referee_id, and_assistant=None):
    """A referee claims the open game; the roster claims come first-come-first-served."""
    fixture = store.fixture(fixture_id)
    role = referees.claim(store, fixture, referee_id)
    say("    {} clicks 'I'll ref this' -> {}.".format(REFS[referee_id], role))
    if and_assistant:
        referees.claim(store, store.fixture(fixture_id), and_assistant)
        say("    {} clicks 'I'll ref this' -> AR.".format(REFS[and_assistant]))


# ---- 1. both managers reply ----------------------------------------------
rule("FIXTURE 1  ·  both managers reply  (the normal case)")

fid1, home1, away1 = create("ARS", "MCI")
fixture = store.fixture(fid1)
say("")
say("    {} vs {}   fixture #{}".format(home1, away1, fid1))
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
say("")
say("    The call for a referee goes up in #referees. First come, first served -")
say("    up to one referee and two assistants/VARs per game.")
claim_referee(fid1, 9001, and_assistant=9002)
expect("fully confirmed once the referee slot is claimed",
      store.fixture(fid1)["status"], Status.FULLY_CONFIRMED)


# ---- 2. only one replies -------------------------------------------------
rule("FIXTURE 2  ·  only one manager replies  (deadline fallback)")

fid2, home2, away2 = create("ASM", "LIL")
say("")
say("    {} vs {}   fixture #{}".format(home2, away2, fid2))
say("    {} submits. {} never replies.".format(home2, away2))
submit(fid2, MANAGERS[home2], {"sun_2200": Pref.IDEAL})

before = advance(store, timings, store.fixture(fid2),
                 now=gw.deadline - timedelta(hours=15), rng=RNG)
say("    Before the deadline  -> {} ({})".format(before.action, before.detail))
expect("waits, does not schedule early", before.action, Action.WAIT)

fixture = store.fixture(fid2)
reminder_content, reminder_embed = notify.reminder(
    fixture, fixture["deadline"], "2h", managers=[MANAGERS[away2]])
show_post("#availability", reminder_embed, content=reminder_content)

after = advance(store, timings, store.fixture(fid2),
                now=gw.deadline + timedelta(minutes=1), rng=RNG)
say("")
say("    Deadline passes  -> {}".format(after.action))
say("    {}".format(after.detail))
expect("falls back to the timings sheet", after.decision.source, Source.AUTO_FALLBACK)
expect("did NOT just take the one reply's pick",
       after.decision.slot.key != "sun_2200", True)

fixture = store.fixture(fid2)
say("")
claim_referee(fid2, 9002)
say("    {} has to pull out, and uses /ref dropout.".format(REFS[9002]))
referees.drop(store, fid2, 9002)
expect("back to needing a referee", store.fixture(fid2)["status"], Status.SCHEDULED)
say("    {} claims it instead.".format(REFS[9003]))
claim_referee(fid2, 9003)


# ---- 3. nobody replies and the sheet can't help --------------------------
rule("FIXTURE 3  ·  nobody replies and the sheet can't help  (needs staff)")

fid3, home3, away3 = create("TOT", "NEW")
say("")
say("    {} vs {}   fixture #{}".format(home3, away3, fid3))
say("    Neither manager replies, and {} has nothing on the timings sheet.".format(away3))

stuck = advance(store, timings, store.fixture(fid3),
                now=gw.deadline + timedelta(minutes=1), rng=RNG)
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
claim_referee(fid3, 9001)


# --------------------------------------------------------------------------
rule("THE REFEREE BOARD  ·  /refs board")
show_ref_board()
expect("every fixture has a referee, even if not every assistant slot is filled",
      unreffed_count(), 0)
expect("but every fixture still has at least one assistant slot open",
      open_referee_count(), 3)


# --------------------------------------------------------------------------
rule("THE FIXTURE BOARD  ·  /fixture board")
for body in notify.fixture_board(store.fixtures(week=gw.week), gw.week, slot_of):
    say("")
    say_embed(body)

rule("THE STAFF DASHBOARD  ·  /fixture list")
buckets = dashboard(store, week=gw.week)
waiting_on = {f["id"]: store.unsubmitted_managers(f["id"])
              for f in store.fixtures(week=gw.week)}
rosters = {f["id"]: store.fixture_referees(f["id"]) for f in store.fixtures(week=gw.week)}
reasons = {}
for f in store.fixtures(week=gw.week):
    for entry in reversed(store.history(f["id"])):
        if entry["event"].startswith("status -> NEEDS_MANUAL") and entry["detail"]:
            reasons[f["id"]] = entry["detail"]
            break
say("")
say_embed(notify.dashboard_summary(buckets, gw.week, waiting_on, reasons,
                                   rosters, slot_for=slot_of))

rule("THE AUDIT TRAIL  ·  /fixture show 2")
say("")
say_embed(notify.fixture_detail(store.fixture(fid2), store.history(fid2),
                                slot_of(store.fixture(fid2)["slot_key"]),
                                store.fixture_referees(fid2)))


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
print("Rehearsal passed: the full workflow behaved correctly end to end.")
