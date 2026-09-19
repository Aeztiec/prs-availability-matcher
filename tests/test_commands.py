"""Smoke tests for the slash commands.

The command logic is thin, but Discord rejects a whole command (or a form) over
limits like a 46-character label, and nothing else in the suite would notice.
These check every definition against Discord's limits and build the result form
the way a real invocation does.

Run with:  python -m tests.test_commands
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import types

from discord import app_commands

from bot.client import PRSBot
from bot.commands import register
from bot.commands.results import RESULT_GUIDE, STARTERS, ResultForm
from bot.db import Store
from bot.domain import discipline, season
from bot.domain.players import Players
from bot.domain.scheduling import Status
from bot.ui import notify

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("  {}   {}".format("ok  " if ok else "FAIL", name))
    if not ok:
        print("         got:  {!r}\n         want: {!r}".format(got, want))
        FAILURES.append(name)


tmp = tempfile.mkdtemp()
bot = PRSBot(store=Store(os.path.join(tmp, "commands.db")))
bot.load_timings()
register(bot)
top_level = bot.tree.get_commands()

print("the command tree")
check("every command group is registered",
      sorted(c.name for c in top_level),
      ["availability", "fixture", "gw", "managers", "ref", "refs", "result", "test"])
check("under Discord's 100 global commands", len(top_level) <= 100, True)


def walk(command, path=""):
    """Every leaf command with its full name."""
    name = "{} {}".format(path, command.name).strip()
    if isinstance(command, app_commands.Group):
        for child in command.commands:
            yield from walk(child, name)
    else:
        yield name, command


leaves = dict(walk_item for c in top_level for walk_item in walk(c))
name_ok = re.compile(r"^[a-z0-9_-]{1,32}$")
bad = [n for n in leaves if not all(name_ok.match(part) for part in n.split())]
check("names are lowercase, 32 characters at most", bad, [])
check("every command has a description of 100 characters or less",
      [n for n, c in leaves.items() if not 1 <= len(c.description) <= 100], [])
check("every option description is 100 characters or less",
      [(n, p.name) for n, c in leaves.items() for p in c.parameters
       if not 1 <= len(p.description) <= 100], [])
check("every game option offers autocomplete",
      [n for n, c in leaves.items() for p in c.parameters
       if p.name == "game" and not p.autocomplete], [])
sizes = {c.name: len(json.dumps(c.to_dict(bot.tree))) for c in top_level}
check("each command's definition is under Discord's 8000 character cap",
      [n for n, size in sizes.items() if size >= 8000], [])

print("\nthe game picker")
gw = season.gameweek("GW1")
bot.open_gameweeks = lambda now=None: [gw]      # the real one depends on today's date
slot = bot.slots[0]
for home, away in (("ARSENAL", "MANCHESTER CITY"), ("REAL MADRID", "FC BARCELONA")):
    fid = bot.store.create_fixture(
        bot.timings.competition.key, gw.week, home, away, 1, 2, "2026-09-16T23:00:00Z",
        Status.SCHEDULED, gameweek="GW1", league="PL")
    bot.store.set_schedule(fid, slot.key, "MANUAL", Status.SCHEDULED)
bot.store.create_fixture(bot.timings.competition.key, gw.week, "AS ROMA", "JUVENTUS", 1, 2,
                         "2026-09-16T23:00:00Z", Status.WAITING_FOR_AVAILABILITY,
                         gameweek="GW1", league="SA")
complete = leaves["result"]._params["game"].autocomplete
everything = asyncio.run(complete(None, ""))
check("lists the open games", len(everything), 3)
check("each choice is a readable name and a numeric value",
      all(c.name and len(c.name) <= 100 and c.value.isdigit() for c in everything), True)
check("no choice shows a fixture number", any("#" in c.name for c in everything), False)
check("typing filters by team", len(asyncio.run(complete(None, "arsenal"))), 1)
check("a game with no kickoff is left out when one is needed",
      len(asyncio.run(leaves["refs assign"]._params["game"].autocomplete(None, ""))), 2)

print("\nthe result form")
players = Players([
    {"USERNAME": "gunner{}".format(i), "CLUB": "ARSENAL", "ROLE": "PLAYER",
     "C": "B", "WAGE": "$100,000"} for i in range(9)
] + [
    {"USERNAME": "citizen{}".format(i), "CLUB": "MANCHESTER CITY", "ROLE": "PLAYER",
     "C": "B", "WAGE": "$100,000"} for i in range(3)
])
fixture = bot.store.fixtures()[0]
bot.store.add_referee(11, "Discord Nick", roblox="moh1d")
bot.store.add_referee(12, "citizen0")
bot.store.claim_referee(fixture["id"], 11, "REF")
bot.store.claim_referee(fixture["id"], 12, "AR")


async def build_form(pens=None):
    return ResultForm(bot, players, fixture, 2, 1, pens)


form = asyncio.run(build_form())
components = form.to_components()
check("a note plus four boxes, Discord's limit of five",
      [c["type"] for c in components], [10, 18, 18, 18, 18])
check("box labels are short and clean",
      [c["label"] for c in components[1:]],
      ["ARS stats", "MCI stats", "MOTM & mentions", "Officiating team"])
check("every label fits Discord's 45 characters",
      all(len(c["label"]) <= 45 for c in components[1:]), True)
check("the title fits too", len(form.title) <= 45, True)
check("the guide fits a text display", len(RESULT_GUIDE) <= 4000, True)
home_box = components[1]["component"]["value"].splitlines()
check("the home box lists starters, then BENCH, then the rest",
      (home_box[STARTERS], len(home_box)), ("BENCH", 10))
check("the away box has no BENCH when everyone starts",
      "BENCH" in components[2]["component"]["value"], False)
crew = components[4]["component"]["value"].splitlines()
check("officials are pre-filled with the referee first",
      crew[0], "moh1d - Main Referee [Full 90']")
check("a Discord name that matches the sheet uses the sheet's spelling",
      crew[1], "citizen0 - Assistant Referee [Full 90']")

print("\nsubmitting a result: it is stored, and the next game's referee is told")
sent = []


async def fake_post(channel, content=None, view=None, mention_users=True, embed=None):
    sent.append((content, embed.description if embed else None))
    return object()


async def fake_channel(week):
    return object()


bot.post = fake_post
bot.officials_channel = fake_channel


class Response:
    def __init__(self):
        self.messages = []
        self.deferred = False

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))

    async def defer(self, **kwargs):
        self.deferred = True

    def is_done(self):
        return self.deferred or bool(self.messages)


class Followup:
    def __init__(self):
        self.messages = []

    async def send(self, *args, **kwargs):
        self.messages.append((args, kwargs))


def submit(target, home_text, away_text="", officials_text=""):
    form = ResultForm(bot, players, target, 2, 1, None)
    form.home._value, form.away._value = home_text, away_text
    form.motm._value, form.officials._value = "", officials_text
    interaction = types.SimpleNamespace(
        response=Response(), followup=Followup(), channel=object(),
        user=types.SimpleNamespace(id=99))
    asyncio.run(form.on_submit(interaction))
    return interaction


second = bot.store.create_fixture(
    bot.timings.competition.key, "2026-09-26", "ARSENAL", "MANCHESTER CITY", 1, 2,
    "2026-09-23T23:00:00Z", Status.SCHEDULED, gameweek="GW2", league="PL")
bot.store.set_schedule(second, slot.key, "MANUAL", Status.SCHEDULED)
bot.store.claim_referee(second, 12, "REF")

done = submit(fixture, "gunner0 rc\ngunner1 g g", "citizen0",
              "moh1d - Main Referee [Full 90']\ncitizen0 - Assistant Referee [Full 90']")
check("the result is saved", bot.store.result(fixture["id"])["home_score"], 2)
check("the players and their cards are saved",
      sorted((p["username"], p["reds"], p["goals"]) for p in bot.store.result_players("ARSENAL")),
      [("gunner0", 1, 0), ("gunner1", 0, 2)])
check("the result was posted, then the referee was told",
      [content for content, _ in sent], [None, "<@12>"])
check("the notice names the player and why",
      "gunner0" in sent[-1][1] and "red card in GW1" in sent[-1][1], True)
report = done.followup.messages[0][0][0]
check("staff see who is suspended and that the officials were told",
      "Suspended for their next game" in report and "have been told" in report, True)
check("the next game's suspension is on record",
      [x["username"] for x in discipline.suspended_for(bot.store, bot.store.fixture(second))],
      ["gunner0"])

asyncio.run(bot.announce_suspensions(bot.store.fixture(second), [12]))
check("a referee who claims that game is pinged as well", sent[-1][0], "<@12>")
sent.clear()
asyncio.run(bot.announce_suspensions(bot.store.fixture(fixture["id"]), [11]))
check("a game with nobody suspended posts nothing", sent, [])

served = submit(bot.store.fixture(second), "gunner0 g\ngunner1", "citizen0")
check("listing the suspended player is flagged to staff",
      "Played while suspended" in served.followup.messages[0][0][0], True)

print("\nthe referee leaderboard")
rows = bot.store.referee_leaderboard()
check("games count per person, however the name was typed",
      [(r["name"], r["games"], r["as_referee"], r["as_assistant"]) for r in rows],
      [("moh1d", 1, 1, 0), ("citizen0", 1, 0, 1)])
board = notify.referee_leaderboard(rows, {11: "moh1d", 12: "citizen0"})
check("tied referees share first place",
      board.description.splitlines()[0], "\U0001F947 **moh1d** - 1 game (1 as referee)")
check("and the assistant is counted too",
      board.description.splitlines()[1], "\U0001F947 **citizen0** - 1 game (1 as assistant)")
check("no games yet says so", "No games counted" in notify.referee_leaderboard([]).description, True)

print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all command checks passed")
