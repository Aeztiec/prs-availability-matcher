"""Tests for suspensions.

Run with:  python -m tests.test_discipline
"""

from __future__ import annotations

import os
import sys
import tempfile

from bot.db import Store
from bot.domain import discipline
from bot.domain.discipline import DOMESTIC, RED, SECOND_YELLOW, UEFA, YELLOWS, evaluate

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("  {}   {}".format("ok  " if ok else "FAIL", name))
    if not ok:
        print("         got:  {!r}\n         want: {!r}".format(got, want))
        FAILURES.append(name)


G = [1, 2, 3, 4, 5]

print("a red card")
s = evaluate(G, {1}, {1: (0, 1)}, {1})
check("misses the next game", (s.game, s.kind, s.triggers), (2, RED, [1]))
s = evaluate(G, {1, 2}, {1: (0, 1)}, {1})
check("and only that one: served", (s.game, s.carries, s.played_while_suspended), (None, False, []))
s = evaluate(G, {1, 2}, {1: (0, 1)}, {1, 2})
check("listed in the suspension game is a breach", s.played_while_suspended, [2])
s = evaluate([1], {1}, {1: (0, 1)}, {1})
check("no next game yet: it carries over", (s.game, s.carries, s.kind), (None, True, RED))

print("\nyellow cards")
s = evaluate(G, {1, 2}, {1: (1, 0), 2: (1, 0)}, {1, 2})
check("yellows in two consecutive games: miss the next",
      (s.game, s.kind, s.triggers), (3, YELLOWS, [1, 2]))
s = evaluate(G, {1, 2, 3}, {1: (1, 0), 3: (1, 0)}, {1, 3})
check("a clean game in between breaks the run", (s.game, s.carries), (None, False))
s = evaluate(G, {1, 2}, {1: (1, 0)}, {1, 2})
check("one yellow alone is nothing", (s.game, s.carries), (None, False))
s = evaluate(G, {1, 2, 3, 4}, {1: (1, 0), 2: (1, 0), 4: (1, 0)}, {1, 2, 4})
check("serving the ban starts the count again: a lone yellow after it is fine",
      (s.game, s.carries), (None, False))
s = evaluate(G, {1, 2, 3, 4, 5}, {1: (1, 0), 2: (1, 0), 4: (1, 0), 5: (1, 0)}, {1, 2, 4, 5})
check("but two more in a row after it is another ban",
      (s.carries, s.kind, s.triggers), (True, YELLOWS, [4, 5]))
s = evaluate(G, {1}, {1: (2, 0)}, {1})
check("two yellows in one game is a sending off",
      (s.game, s.kind), (2, SECOND_YELLOW))

print("\nnothing to work out yet")
s = evaluate(G, set(), {}, set())
check("no results, no suspension", (s.game, s.carries), (None, False))

print("\nthe competitions are separate")
check("a PL game is domestic", discipline.competition_group("PL"), DOMESTIC)
check("a game made by hand is domestic", discipline.competition_group(None), DOMESTIC)
check("UEFA is its own competition", discipline.competition_group("UEFA"), UEFA)
check("so are the later rounds", discipline.competition_group("UCL"), UEFA)

store = Store(os.path.join(tempfile.mkdtemp(), "discipline.db"))
COMP = "S17_Clubs"


def fixture(week, league, gameweek, home="ARSENAL", away="MANCHESTER CITY"):
    return store.create_fixture(COMP, week, home, away, 1, 2, "2026-09-16T23:00:00Z",
                                "SCHEDULED", gameweek=gameweek, league=league)


def result(fid, players, home="ARSENAL", away="MANCHESTER CITY"):
    rows = [{"username": u, "club": c, "starter": True, "goals": 0, "assists": 0,
             "yellows": y, "reds": r} for u, c, y, r in players]
    store.save_result(fid, 1, 0, None, 9, rows, [])


dom1 = fixture("2026-09-19", "PL", "GW1")
uefa1 = fixture("2026-09-19", "UEFA", "GW1")
dom2 = fixture("2026-09-26", "PL", "GW2")
uefa2 = fixture("2026-09-26", "UEFA", "GW2")
dom3 = fixture("2026-10-03", "PL", "GW3")

result(dom1, [("striker", "ARSENAL", 0, 1)])
print("\na domestic red card")
check("the next domestic game is missed",
      [(x["username"], x["club"]) for x in discipline.suspended_for(store, store.fixture(dom2))],
      [("striker", "ARSENAL")])
check("the UEFA game is not",
      discipline.suspended_for(store, store.fixture(uefa1)), [])
check("nor the UEFA game after it",
      discipline.suspended_for(store, store.fixture(uefa2)), [])
check("the game after the suspension is clear",
      discipline.suspended_for(store, store.fixture(dom3)), [])
check("the club's next domestic game is found",
      discipline.next_game(store, "ARSENAL", store.fixture(dom1))["id"], dom2)
check("and its next UEFA game is a different one",
      discipline.next_game(store, "ARSENAL", store.fixture(uefa1))["id"], uefa2)

print("\nyellows in consecutive domestic games, a UEFA game in between")
result(uefa1, [("winger", "ARSENAL", 1, 0)])            # a UEFA yellow
result(dom2, [("winger", "ARSENAL", 1, 0)])             # and a domestic one
check("a UEFA yellow does not join a domestic run",
      discipline.suspended_for(store, store.fixture(dom3)), [])
result(dom2, [("winger", "ARSENAL", 1, 0), ("striker", "ARSENAL", 0, 0)])
result(dom1, [("striker", "ARSENAL", 0, 1), ("winger", "ARSENAL", 1, 0)])
check("yellows in GW1 and GW2 domestic: out for GW3",
      [x["username"] for x in discipline.suspended_for(store, store.fixture(dom3))], ["winger"])
check("shown as pending for staff",
      sorted((x["username"], x["group"]) for x in discipline.current(store)),
      [("winger", DOMESTIC)])

print("\nplaying while suspended")
result(dom2, [("striker", "ARSENAL", 0, 0), ("winger", "ARSENAL", 1, 0)])
check("the suspended striker was listed in GW2: flagged",
      [x["username"] for x in discipline.breaches(store, store.fixture(dom2))], ["striker"])
result(dom2, [("winger", "ARSENAL", 1, 0)])
check("left out, no breach", discipline.breaches(store, store.fixture(dom2)), [])

print("\ncorrecting a result corrects the suspension")
result(dom1, [("winger", "ARSENAL", 1, 0)])             # the red was a mistake
check("striker is no longer suspended for GW2",
      discipline.suspended_for(store, store.fixture(dom2)), [])

print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all discipline checks passed")
