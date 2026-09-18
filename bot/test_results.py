"""Tests for the result post and the player sheet lookup.

Run with:  python -m bot.test_results
"""

from __future__ import annotations

import sys

from bot import season
from bot.players import Players
from bot.results import build, header, parse_line

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("  {}   {}".format("ok  " if ok else "FAIL", name))
    if not ok:
        print("         got:  {!r}\n         want: {!r}".format(got, want))
        FAILURES.append(name)


SHEET = Players([
    {"USERNAME": "vzcadc", "CLUB": "FC PORTO", "ROLE": "PLAYER"},
    {"USERNAME": "_gawa", "CLUB": "FC PORTO", "ROLE": "PLAYER"},
    {"USERNAME": "danielfly", "CLUB": "FC PORTO", "ROLE": "PLAYER"},
    {"USERNAME": "FelipeF", "CLUB": "PARIS SAINT-GERMAIN", "ROLE": "PLAYER"},
    {"USERNAME": "om_ena", "CLUB": "PARIS SAINT-GERMAIN", "ROLE": "PLAYER"},
    {"USERNAME": "nobody_fc", "CLUB": "ARSENAL", "ROLE": "PLAYER"},
    {"USERNAME": "boss", "CLUB": "FC PORTO", "ROLE": "MANAGER"},
])
FIXTURE = {"home_team": "FC PORTO", "away_team": "PARIS SAINT-GERMAIN",
           "league": "UEFA", "competition": "S17_Clubs", "gameweek": "GW1"}

print("the player sheet")
check("finds a username in any case", SHEET.find("VZCADC")["club"], "FC PORTO")
check("ignores a leading @", SHEET.find("@_gawa")["username"], "_gawa")
check("unknown is None", SHEET.find("ghost"), None)
check("roster is players only, A to Z",
      SHEET.roster("FC PORTO"), ["_gawa", "danielfly", "vzcadc"])
check("suggests a close match", SHEET.suggest("vzcad"), ["vzcadc"])

print("\nparsing a line")
check("goals, assist", parse_line("vzcadc g g a"), ("vzcadc", ["goal", "goal", "assist"], []))
check("counts either side", parse_line("x g3 2a")[1], ["goal"] * 3 + ["assist"] * 2)
check("keeper, sub, cards", parse_line("x gk sub yc rc")[1],
      ["gk", "sub", "yellow", "red"])
check("unknown token is flagged", parse_line("x banana")[2], ["banana"])
check("blank line is nothing", parse_line("   "), None)

print("\nbuilding the post")
text, problems = build(
    FIXTURE, 5, 0, "vzcadc g g\n_gawa g a\ndanielfly gk", "FelipeF\nom_ena rc",
    "danielfly sub", "_gawa\nFelipeF", "moh1d\nj5rdi", SHEET, round_name="Round of 16 Leg 1")
check("no problems", problems, [])
check("heading names the season, round and competition",
      text.splitlines()[0].endswith("**| PRS SEASON 17 CLUBS | ROUND OF 16 LEG 1 | UEFA**"), True)
check("score line", "**5 - 0**" in text, True)
check("goals shown", "vzcadc ⚽ ⚽" in text, True)
check("underscore usernames are escaped, not italic", "\\_gawa" in text, True)
check("red card shown", "om\\_ena 🟥" in text, True)
check("sections present", all(h in text for h in
      ("**STATISTICS**", "**SUBS**", "**MOTM & MENTIONS**", "**OFFICIALS**")), True)
check("MOTM gets the trophy, next the gold medal", "\\_gawa 🏆" in text and "FelipeF 🥇" in text, True)
check("nobody is pinged", "<@" in text, False)

print("\nthings that stop the post")
_, wrong_team = build(FIXTURE, 1, 0, "nobody_fc g", "", "", "", "", SHEET)
check("a player on the wrong team", wrong_team,
      ["**nobody_fc** plays for Arsenal, not Fc Porto."])
_, unknown = build(FIXTURE, 1, 0, "vzcad g", "", "", "", "", SHEET)
check("a mistyped name gets a suggestion",
      "did you mean vzcadc" in unknown[0], True)
_, bad_token = build(FIXTURE, 1, 0, "vzcadc banana", "", "", "", "", SHEET)
check("a bad token says what is allowed", "`banana`" in bad_token[0], True)
_, too_many = build(FIXTURE, 1, 0, "", "", "", "_gawa\nvzcadc\ndanielfly\nFelipeF\nom_ena", "", SHEET)
check("more than four MOTM names", too_many[0], "MOTM & mentions takes at most 4 names.")

print("\nheading without overrides")
check("league game uses gameweek and league",
      header({"league": "PL", "competition": "S17_Clubs", "gameweek": "GW2"}),
      season.LEAGUE_EMOJI["PL"] + " **| PRS SEASON 17 CLUBS | GAMEWEEK 2 | PL**")

print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all result tests passed")
