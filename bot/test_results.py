"""Tests for the result post and the player sheet lookup.

Run with:  python -m bot.test_results
"""

from __future__ import annotations

import sys

from bot import season
from bot.players import Players
from bot.results import build, header, parse_line, score_line

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("  {}   {}".format("ok  " if ok else "FAIL", name))
    if not ok:
        print("         got:  {!r}\n         want: {!r}".format(got, want))
        FAILURES.append(name)


def rows_of(text, section):
    """The lines under a bold section heading, up to the next blank line."""
    lines = text.splitlines()
    start = lines.index(section) + 1
    out = []
    for line in lines[start:]:
        if not line:
            break
        out.append(line)
    return out


SHEET = Players([
    {"USERNAME": "vzcadc", "CLUB": "FC PORTO", "ROLE": "PLAYER"},
    {"USERNAME": "_gawa", "CLUB": "FC PORTO", "ROLE": "PLAYER"},
    {"USERNAME": "danielfly", "CLUB": "FC PORTO", "ROLE": "PLAYER"},
    {"USERNAME": "benchguy", "CLUB": "FC PORTO", "ROLE": "PLAYER"},
    {"USERNAME": "FelipeF", "CLUB": "PARIS SAINT-GERMAIN", "ROLE": "PLAYER"},
    {"USERNAME": "om_ena", "CLUB": "PARIS SAINT-GERMAIN", "ROLE": "PLAYER"},
    {"USERNAME": "nobody_fc", "CLUB": "ARSENAL", "ROLE": "PLAYER"},
    {"USERNAME": "boss", "CLUB": "FC PORTO", "ROLE": "MANAGER"},
])
FIXTURE = {"home_team": "FC PORTO", "away_team": "PARIS SAINT-GERMAIN",
           "league": "PL", "competition": "S17_Clubs", "gameweek": "GW1"}
PORTO = season.label_for("FC PORTO")
PSG = season.label_for("PARIS SAINT-GERMAIN")

print("the player sheet")
check("finds a username in any case", SHEET.find("VZCADC")["club"], "FC PORTO")
check("ignores a leading @", SHEET.find("@_gawa")["username"], "_gawa")
check("unknown is None", SHEET.find("ghost"), None)
check("roster is players only, A to Z",
      SHEET.roster("FC PORTO"), ["_gawa", "benchguy", "danielfly", "vzcadc"])
check("suggests a close match", SHEET.suggest("vzcad"), ["vzcadc"])

print("\nparsing a line")
check("goals, assist", parse_line("vzcadc g g a"), ("vzcadc", ["goal", "goal", "assist"], []))
check("counts either side", parse_line("x g3 2a")[1], ["goal"] * 3 + ["assist"] * 2)
check("cards, sub, unused", parse_line("x yc rc sub nosub")[1],
      ["yellow", "red", "sub", "nosub"])
check("penalties", parse_line("x ps pm")[1], ["pen_scored", "pen_missed"])
check("unknown token is flagged", parse_line("x banana")[2], ["banana"])
check("blank line is nothing", parse_line("   "), None)

print("\nthe heading and score")
check("league game: logo, league name, gameweek, all bold",
      header(FIXTURE), season.LEAGUE_EMOJI["PL"] + " **| PREMIER LEAGUE | GAMEWEEK 1**")
check("overrides for a knockout",
      header(FIXTURE, "UEFA Champions League", "Final"),
      season.LEAGUE_EMOJI["PL"] + " **| UEFA CHAMPIONS LEAGUE | FINAL**")
check("score in bold between the badges",
      score_line(FIXTURE, 3, 3), "{} **3 - 3** {}".format(PORTO, PSG))
check("with a shootout", score_line(FIXTURE, 3, 3, (5, 4)),
      "{} **3 - 3 [5 - 4 ON PENS]** {}".format(PORTO, PSG))

print("\nbuilding the post")
text, problems = build(
    FIXTURE, 5, 0,
    "danielfly a\nvzcadc g g\n_gawa g a yc\nBENCH\nbenchguy sub",
    "FelipeF\nom_ena rc",
    "_gawa - hat trick\nFelipeF", "moh1d - Main Referee [Full 90']\nj5rdi - Assistant Referee [Full 90']",
    SHEET)
check("no problems", problems, [])
stats = text.split("**STATISTICS:**")[1]
check("statistics heading is bold", "**STATISTICS:**" in text, True)
check("most goals first, then assists, ties keep typed order",
      [r.split(" | ")[1] for r in rows_of(text, "**STATISTICS:**")[:3]],
      ["vzcadc ⚽ ⚽", "\\_gawa ⚽ 👁️ 🟨", "danielfly 👁️"])
check("bench has its own bold heading under the starters",
      rows_of(text, "**STATISTICS:**")[3:], ["**BENCH:**", "{} | benchguy 🔁".format(PORTO)])
check("team 2 is its own block with no bench when there is none",
      text.split("\n\n")[3].splitlines()[0], "{} | FelipeF".format(PSG))
check("underscore usernames are escaped, not italic", "\\_gawa" in text, True)
check("red card shown", "om\\_ena 🟥" in text, True)
check("mentions: team badge, name, optional note",
      rows_of(text, "**MOTM & MENTIONS:**"),
      ["{} | \\_gawa - hat trick".format(PORTO), "{} | FelipeF".format(PSG)])
check("officiating team keeps role and minutes",
      rows_of(text, "**OFFICIATING TEAM:**")[0].endswith("moh1d - Main Referee [Full 90']"), True)
check("nobody is pinged", "<@" in text, False)
check("no MOTM section when there are none",
      "MOTM" in build(FIXTURE, 1, 0, "vzcadc", "", "", "", SHEET)[0], False)

print("\nthings that stop the post")
_, wrong_team = build(FIXTURE, 1, 0, "nobody_fc g", "", "", "", SHEET)
check("a player on the wrong team", wrong_team,
      ["**nobody_fc** plays for Arsenal, not Fc Porto."])
_, unknown = build(FIXTURE, 1, 0, "vzcad g", "", "", "", SHEET)
check("a mistyped name gets a suggestion", "did you mean vzcadc" in unknown[0], True)
_, bad_token = build(FIXTURE, 1, 0, "vzcadc banana", "", "", "", SHEET)
check("a bad token says what is allowed", "`banana`" in bad_token[0], True)
_, mention_outsider = build(FIXTURE, 1, 0, "", "", "nobody_fc", "", SHEET)
check("MOTM must be from one of the two teams",
      mention_outsider, ["**nobody_fc** doesn't play for Fc Porto or Paris Saint-Germain."])

print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all result tests passed")
