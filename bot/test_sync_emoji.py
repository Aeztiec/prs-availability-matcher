"""Tests for rewriting the emoji blocks in season.py.

Worth having: an earlier version of write_into_season matched only a dict
closed by a brace in column zero, so on an empty one-liner - `LEAGUE_EMOJI =
{}` - the regex ran on to the *next* block's closing brace and deleted
GAMEWEEKS, TEAM_CODES and the whole fixture list with it. The bot then would
not import at all.

Run with:  python -m bot.test_sync_emoji
"""

from __future__ import annotations

import os
import sys
import tempfile

from bot import season, sync_emoji

FAILURES = []


def check(name, got, want):
    if got == want:
        print("  ok   {}".format(name))
    else:
        print("  FAIL {}\n         got:  {!r}\n         want: {!r}".format(name, got, want))
        FAILURES.append(name)


SAMPLE = '''"""A stand-in for season.py."""

SEASON = "S17"
SEASON_EMOJI = ""
LEAGUE_EMOJI = {}

GAMEWEEKS = [
    ("GW1", "Gameweek 1", "2026-09-18"),
]

TEAM_CODES = {
    "ARS": "ARSENAL",
    "BVB": "BORUSSIA DORTMUND",
}

TEAM_EMOJI = {
    "ARSENAL": "<:ARS:1>",
}

LEAGUES = {
    "PL": "Premier League",
}

FIXTURES = {
    "GW1": [("ARS", "BVB", "PL")],
}
'''


def rewrite(text, block, name):
    """Run write_into_season against a temporary copy and return the result."""
    path = os.path.join(tempfile.mkdtemp(), "season.py")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    original = sync_emoji.SEASON_FILE
    sync_emoji.SEASON_FILE = path
    try:
        sync_emoji.write_into_season(block, name)
    finally:
        sync_emoji.SEASON_FILE = original
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# --------------------------------------------------------------------------
print("\nreplacing an empty one-line dict")
# --------------------------------------------------------------------------
result = rewrite(SAMPLE, 'LEAGUE_EMOJI = {\n    "PL": "<:PL:9>",\n}', "LEAGUE_EMOJI")
check("the new block is in", '"PL": "<:PL:9>"' in result, True)
check("GAMEWEEKS survived", "GAMEWEEKS = [" in result, True)
check("TEAM_CODES survived", '"BVB": "BORUSSIA DORTMUND"' in result, True)
check("FIXTURES survived", 'FIXTURES = {' in result, True)
check("TEAM_EMOJI survived", '"ARSENAL": "<:ARS:1>"' in result, True)
check("the result still parses", bool(compile(result, "x", "exec")) or True, True)

print("\nreplacing a multi-line dict")
result = rewrite(SAMPLE, 'TEAM_EMOJI = {\n    "ARSENAL": "<:ARS:2>",\n}', "TEAM_EMOJI")
check("updated", '"<:ARS:2>"' in result, True)
check("old value gone", '"<:ARS:1>"' in result, False)
check("LEAGUES survived", '"PL": "Premier League"' in result, True)
check("LEAGUE_EMOJI untouched", "LEAGUE_EMOJI = {}" in result, True)

print("\nboth in sequence, the way --write does it")
step = rewrite(SAMPLE, 'TEAM_EMOJI = {\n    "ARSENAL": "<:ARS:3>",\n}', "TEAM_EMOJI")
step = rewrite(step, 'LEAGUE_EMOJI = {\n    "PL": "<:PL:4>",\n}', "LEAGUE_EMOJI")
for needed in ("GAMEWEEKS = [", "TEAM_CODES = {", "LEAGUES = {", "FIXTURES = {"):
    check("{} survived both writes".format(needed.split()[0]), needed in step, True)
check("both blocks applied", '"<:ARS:3>"' in step and '"<:PL:4>"' in step, True)

# --------------------------------------------------------------------------
print("\nit refuses to write something broken")
# --------------------------------------------------------------------------
try:
    rewrite(SAMPLE, "TEAM_EMOJI = {\n    oops(\n}", "TEAM_EMOJI")
    check("a syntax error is refused", "wrote it anyway", "SystemExit")
except SystemExit as error:
    check("a syntax error is refused", "would not parse" in str(error), True)

# A file whose TEAM_EMOJI dict closes on an indented brace: the multi-line
# pattern then runs past it and swallows GAMEWEEKS, which is exactly the bug
# this guard is the backstop for.
SPANNING = '''TEAM_EMOJI = {
    "ARSENAL": "<:ARS:1>",
  }
GAMEWEEKS = [
    ("GW1", "Gameweek 1", "2026-09-18"),
]
TEAM_CODES = {
    "ARS": "ARSENAL",
}
LEAGUES = {
    "PL": "Premier League",
}
FIXTURES = {
    "GW1": [],
}
'''
try:
    rewrite(SPANNING, 'TEAM_EMOJI = {\n    "ARSENAL": "<:ARS:2>",\n}', "TEAM_EMOJI")
    check("swallowing another block is refused", "wrote it anyway", "SystemExit")
except SystemExit as error:
    check("swallowing another block is refused",
          "went missing" in str(error), True)

try:
    rewrite("SEASON = 1\n", "NOPE = {\n}", "NOPE")
    check("a missing block is reported", "wrote it anyway", "SystemExit")
except SystemExit as error:
    check("a missing block is reported", "couldn't find" in str(error), True)

# --------------------------------------------------------------------------
print("\nthe real season.py is intact and complete")
# --------------------------------------------------------------------------
check("gameweeks", len(season.GAMEWEEKS), 10)
check("team codes", len(season.TEAM_CODES), 40)
check("fixture lists", len(season.FIXTURES), 7)
# Badges are optional and come and go as emoji are uploaded or deleted -
# Discord caps a server at 50, so the divisions get dropped to make room for
# forty clubs. Assert only that whatever is present is valid, never a count.
check("every division badge present is a real reference",
      [c for c, e in season.LEAGUE_EMOJI.items() if not season.usable_emoji(e)], [])
check("no division is listed that isn't a real division",
      [c for c in season.LEAGUE_EMOJI if c not in season.LEAGUES], [])


# --------------------------------------------------------------------------
print("")
print("badges that no longer exist are treated as absent")
# --------------------------------------------------------------------------
check("a real reference is usable",
      season.usable_emoji("<:ARS:1547634005745344554>"), True)
check("blank is not", season.usable_emoji(""), False)
check("None is not", season.usable_emoji(None), False)
check("a bare shortcode is not - a bot cannot resolve one",
      season.usable_emoji(":ARS:"), False)
check("a stray word is not", season.usable_emoji("ARS"), False)

saved_team = dict(season.TEAM_EMOJI)
saved_league = dict(season.LEAGUE_EMOJI)
try:
    season.TEAM_EMOJI = {"ARSENAL": ":ARS:", "CHELSEA": ""}
    check("an unresolvable badge falls back to the name",
          season.label_for("ARSENAL"), "ARSENAL")
    check("a blank badge falls back too", season.label_for("CHELSEA"), "CHELSEA")
    season.TEAM_EMOJI = {"ARSENAL": "<:ARS:1>"}
    check("a usable badge is used", season.label_for("ARSENAL"), "<:ARS:1>")
finally:
    season.TEAM_EMOJI = saved_team
    season.LEAGUE_EMOJI = saved_league

print("")
print("the real season.py holds only usable references")
bad_teams = [t for t, e in season.TEAM_EMOJI.items() if not season.usable_emoji(e)]
bad_leagues = [c for c, e in season.LEAGUE_EMOJI.items() if not season.usable_emoji(e)]
check("no unusable team badges", bad_teams, [])
check("no unusable division badges", bad_leagues, [])
check("the season badge is either usable or empty",
      season.SEASON_EMOJI == "" or season.usable_emoji(season.SEASON_EMOJI), True)

# --------------------------------------------------------------------------
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all sync_emoji tests passed")
