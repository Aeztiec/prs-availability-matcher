"""Tests for the Season 17 calendar and gameweek gating.

The fixture list is transcribed by hand from the published schedule, so the
structural invariants are checked rather than trusted: every team plays exactly
once a gameweek, all forty play seven games, and every shorthand code resolves
to a team that actually exists in the timings sheet. Those three between them
catch almost any transcription slip.

Run with:  python -m tests.test_season
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone

from bot.domain import sheet as av

from bot.domain import season
from bot.db import Store
from bot.domain.slots import build_slots
from bot.domain.weeks import slot_datetime, uk_time

FAILURES = []


def check(name, got, want):
    if got == want:
        print("  ok   {}".format(name))
    else:
        print("  FAIL {}\n         got:  {!r}\n         want: {!r}".format(name, got, want))
        FAILURES.append(name)


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
print("\nthe calendar")
# --------------------------------------------------------------------------
check("ten entries: 7 gameweeks plus the knockouts", len(season.ALL), 10)
check("GW1 plays Friday 18 Sep", season.gameweek("GW1").friday, utc(2026, 9, 18))
check("every schedule date is a Friday",
      {gw.friday.strftime("%A") for gw in season.ALL}, {"Friday"})
check("gameweeks are a week apart",
      {(season.ALL[i + 1].friday - season.ALL[i].friday).days for i in range(6)}, {7})

print("\ndeadlines land at the start of Thursday, the moment Wednesday ends")
# Stated in UK local time, so the raw UTC instant is an hour earlier than
# the naive "Thursday 00:00" reading whenever the date falls in BST - GW1
# plays in September, so its deadline is really Wednesday 23:00 UTC.
check("GW1 deadline (raw UTC, an hour ahead of GMT because of BST)",
      season.gameweek("GW1").deadline, utc(2026, 9, 16, 23, 0))
check("but reads as Thursday 00:00 in the UK's own clock",
      uk_time(season.gameweek("GW1").deadline), (utc(2026, 9, 17, 0, 0), "BST"))
check("every deadline is a Thursday in UK local time",
      {uk_time(gw.deadline)[0].strftime("%A") for gw in season.ALL}, {"Thursday"})
check("a deadline is always before its gameweek",
      all(gw.deadline < gw.friday for gw in season.ALL), True)

print("\nthe week key is the Saturday, matching the rest of the bot")
check("GW1 week", season.gameweek("GW1").week, "2026-09-19")
check("all weeks are Saturdays",
      {datetime.fromisoformat(gw.week).strftime("%A") for gw in season.ALL}, {"Saturday"})

print("\nlookup")
check("case-insensitive", season.gameweek("gw3").key, "GW3")
check("whitespace tolerated", season.gameweek("  GW4 ").key, "GW4")
try:
    season.gameweek("GW99")
    check("unknown gameweek rejected", "no error", "an error")
except LookupError as error:
    check("unknown gameweek explains itself", "no gameweek" in str(error), True)

# --------------------------------------------------------------------------
print("\nthe fixture list is structurally sound")
# --------------------------------------------------------------------------
problems = season.validate([t.country for t in av.build_sheet(
    av.load_sheet(av.find_competition("S17_Clubs"))).teams])
check("validates against the real timings sheet", problems, [])

for gw in season.ALL:
    if not gw.has_fixtures:
        continue
    codes = [c for row in gw.fixtures for c in row[:2]]
    check("{}: 20 fixtures".format(gw.key), len(gw.fixtures), 20)
    check("{}: every team plays exactly once".format(gw.key),
          (len(codes), len(set(codes))), (40, 40))

print("\nall forty teams play seven games")
played = Counter()
for gw in season.ALL:
    for home, away, _ in gw.fixtures:
        played[home] += 1
        played[away] += 1
check("forty teams", len(played), 40)
check("seven each", set(played.values()), {7})

print("\neach league keeps to itself")
by_league = {}
for gw in season.ALL:
    for home, away, league in gw.fixtures:
        by_league.setdefault(league, set()).update({home, away})
check("five leagues", sorted(by_league), ["BL", "L1", "LL", "PL", "SA"])
check("eight teams per league", {len(v) for v in by_league.values()}, {8})
check("no team appears in two leagues",
      sum(len(v) for v in by_league.values()), 40)

print("\nno pairing repeats within the season")
pairings = Counter()
for gw in season.ALL:
    for home, away, _ in gw.fixtures:
        pairings[frozenset((home, away))] += 1
check("every pairing at most once", max(pairings.values()), 1)

# --------------------------------------------------------------------------
print("\nthe UEFA League Phase, alongside the domestic fixtures")
# --------------------------------------------------------------------------
uefa_weeks = [gw for gw in season.ALL if gw.uefa_fixtures]
check("GW1 through GW5 have a UEFA round", [g.key for g in uefa_weeks],
      ["GW1", "GW2", "GW3", "GW4", "GW5"])
check("GW6 and GW7 have none",
      [bool(season.gameweek(k).uefa_fixtures) for k in ("GW6", "GW7")], [False, False])

for gw in uefa_weeks:
    codes = [c for row in gw.uefa_fixtures for c in row[:2]]
    check("{}: 20 UEFA fixtures".format(gw.key), len(gw.uefa_fixtures), 20)
    check("{}: every team plays exactly once in UEFA too".format(gw.key),
          (len(codes), len(set(codes))), (40, 40))
    check("{}: every row tagged UEFA_LEAGUE".format(gw.key),
          {row[2] for row in gw.uefa_fixtures}, {season.UEFA_LEAGUE})

check("UEFA is deliberately not one of the five domestic leagues",
      season.UEFA_LEAGUE in season.LEAGUES, False)

print("\nsome pairings repeat across both competitions in the same week")
# The source list isn't shy about it - Real Sociedad v Barcelona in GW3 is
# both that week's La Liga fixture and that week's UEFA one. Not a
# transcription slip: db.fixture_for() has to tell the two apart by league,
# see the test below, or opening a gameweek would silently skip the second.
repeats = {
    gw.key: {frozenset(row[:2]) for row in gw.fixtures}
             & {frozenset(row[:2]) for row in gw.uefa_fixtures}
    for gw in uefa_weeks
}
repeats = {k: v for k, v in repeats.items() if v}
check("at least one gameweek really does repeat a pairing", bool(repeats), True)

# --------------------------------------------------------------------------
print("\nthe kickoff floor is respected")
# --------------------------------------------------------------------------
sheet = av.build_sheet(av.load_sheet(av.find_competition("S17_Clubs")))
slots = build_slots(sheet)
gw1 = season.gameweek("GW1")
earliest = min(slot_datetime(gw1.week, s) for s in slots)
check("GW1's earliest slot is after the season opens",
      earliest >= season.KICKOFF_FLOOR, True)
check("the floor is the published Thursday 17:30 UK time (16:30 UTC in BST)",
      season.KICKOFF_FLOOR, utc(2026, 9, 17, 16, 30))
check("which reads as 17:30 BST on the UK's own clock",
      uk_time(season.KICKOFF_FLOOR), (utc(2026, 9, 17, 17, 30), "BST"))

# A hypothetical earlier gameweek must have its illegal slots filtered out.
too_early = [s for s in slots
             if slot_datetime("2026-09-12", s) < season.KICKOFF_FLOOR]
check("an earlier week would have slots before the floor", len(too_early) > 0, True)

# --------------------------------------------------------------------------
print("\nwhat's open, and when")
# --------------------------------------------------------------------------
before_season = utc(2026, 9, 10, 12)
check("before the season starts, GW1 is current",
      season.current(before_season).key, "GW1")

print("\na gameweek stays current until its Sunday is over")
check("Saturday of GW1", season.current(utc(2026, 9, 19, 20)).key, "GW1")
check("Sunday evening of GW1", season.current(utc(2026, 9, 20, 22)).key, "GW1")
check("Monday after rolls to GW2", season.current(utc(2026, 9, 21, 9)).key, "GW2")

print("\npast the end of the calendar it clamps rather than crashing")
check("after the final", season.current(utc(2027, 1, 1)).key, "F")

# --------------------------------------------------------------------------
print("\nteam codes")
# --------------------------------------------------------------------------
check("forty codes", len(season.TEAM_CODES), 40)
check("codes are unique to teams",
      len(set(season.TEAM_CODES.values())), 40)
check("lookup works", season.team_name("ars"), "ARSENAL")
check("an accented name resolves", season.team_name("ATM"), "ATLÉTICO MADRID")
try:
    season.team_name("ZZZ")
    check("unknown code rejected", "no error", "an error")
except LookupError as error:
    check("unknown code explains itself", "unknown team code" in str(error), True)

print("\nvalidate() actually catches a broken calendar")
original = dict(season.TEAM_CODES)
season.TEAM_CODES["ARS"] = "ARSNEAL FC"          # a typo
broken = season.validate([t.country for t in sheet.teams])
check("a mistyped team name is reported", any("ARSNEAL" in p for p in broken), True)
season.TEAM_CODES.clear()
season.TEAM_CODES.update(original)
check("restored", season.validate([t.country for t in sheet.teams]), [])

# --------------------------------------------------------------------------
print("\nopening a gameweek is recorded and idempotent")
# --------------------------------------------------------------------------
store = Store(os.path.join(tempfile.mkdtemp(), "season.db"))
check("nothing open initially", store.opened_gameweeks(), set())
store.open_gameweek("GW2", opened_by=123)
check("recorded", store.opened_gameweeks(), {"GW2"})
store.open_gameweek("GW2", opened_by=456)
check("opening twice is not two rows", store.opened_gameweeks(), {"GW2"})
store.close_gameweek("GW2")
check("closed", store.opened_gameweeks(), set())

print("\nfixtures carry their gameweek")
fid = store.create_fixture("S17_Clubs", "2026-09-19", "ARSENAL", "MANCHESTER CITY",
                           1, 2, "2026-09-16T23:59:00Z", "WAITING_FOR_AVAILABILITY",
                           gameweek="GW1")
check("stored", store.fixture(fid)["gameweek"], "GW1")
check("filterable", [f["id"] for f in store.fixtures(gameweek="GW1")], [fid])
check("other gameweeks empty", store.fixtures(gameweek="GW2"), [])
check("found by pairing",
      store.fixture_for("GW1", "ARSENAL", "MANCHESTER CITY")["id"], fid)
check("reversed pairing is a different fixture",
      store.fixture_for("GW1", "MANCHESTER CITY", "ARSENAL"), None)

print("\nthe same pairing can exist twice in a gameweek - once per competition")
# What the real GW3 fixture list does with Real Sociedad v Barcelona: a
# domestic fixture and a UEFA one, same two teams, same week.
domestic_fid = store.create_fixture("S17_Clubs", "2026-09-19", "REAL SOCIEDAD", "BARCELONA",
                                    3, 4, "2026-09-16T23:59:00Z", "WAITING_FOR_AVAILABILITY",
                                    gameweek="GW1", league="LL")
uefa_fid = store.create_fixture("S17_Clubs", "2026-09-19", "REAL SOCIEDAD", "BARCELONA",
                                3, 4, "2026-09-16T23:59:00Z", "WAITING_FOR_AVAILABILITY",
                                gameweek="GW1", league="UEFA")
check("two distinct fixtures, not a duplicate", domestic_fid != uefa_fid, True)
check("league picks out the domestic one",
      store.fixture_for("GW1", "REAL SOCIEDAD", "BARCELONA", league="LL")["id"],
      domestic_fid)
check("league picks out the UEFA one",
      store.fixture_for("GW1", "REAL SOCIEDAD", "BARCELONA", league="UEFA")["id"],
      uefa_fid)
check("without a league it just finds a match, same as before",
      store.fixture_for("GW1", "REAL SOCIEDAD", "BARCELONA") is not None, True)

print("\nmanager mapping")
check("nobody set yet", store.manager_of("ARSENAL"), None)
store.set_manager("ARSENAL", 555)
check("set", store.manager_of("ARSENAL"), 555)
store.set_manager("ARSENAL", 666)
check("reassigning replaces", store.manager_of("ARSENAL"), 666)
check("listed", store.managers(), {"ARSENAL": 666})

# --------------------------------------------------------------------------
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all season tests passed")
