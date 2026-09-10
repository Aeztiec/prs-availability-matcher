"""Read the server's club badges and print the TEAM_EMOJI block to paste in.

Emoji are matched by name to the three-letter codes in season.TEAM_CODES, so
uploading a badge named after a team's code is all that is needed - ARS for
Arsenal, BVB for Dortmund, and so on.

    python -m bot.sync_emoji            show what matches and what is missing
    python -m bot.sync_emoji --write    update season.py in place

Full "<:name:id>" strings are required in the file rather than ":name:"
shortcodes, because a bot cannot resolve a shortcode - it would post as
literal text. Reading them from the server is the only reliable way to get
the ids right.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys

import discord

from . import config, season

log = logging.getLogger("sync_emoji")

SEASON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "season.py")


def render(matched):
    """The TEAM_EMOJI literal, grouped by division in fixture-list order."""
    by_league = {}
    for code, team in season.TEAM_CODES.items():
        if team not in matched:
            continue
        league = next(
            (lg for rows in season.FIXTURES.values()
             for h, a, lg in rows if code in (h, a)),
            "??",
        )
        by_league.setdefault(league, []).append((code, team))

    lines = ["TEAM_EMOJI = {"]
    for league in list(season.LEAGUES) + sorted(k for k in by_league
                                                if k not in season.LEAGUES):
        rows = by_league.get(league)
        if not rows:
            continue
        lines.append("    # {}".format(season.LEAGUES.get(league, league)))
        for _code, team in sorted(rows, key=lambda pair: pair[1]):
            lines.append('    "{}": "{}",'.format(team, matched[team]))
    lines.append("}")
    return "\n".join(lines)


def render_leagues(matched):
    """The LEAGUE_EMOJI literal, for badges named after a league code."""
    lines = ["LEAGUE_EMOJI = {"]
    for code in season.LEAGUES:
        if code in matched:
            lines.append('    "{}": "{}",  # {}'.format(
                code, matched[code], season.LEAGUES[code]))
    lines.append("}")
    return "\n".join(lines)


def write_into_season(block, pattern_name="TEAM_EMOJI"):
    with open(SEASON_FILE, encoding="utf-8") as handle:
        text = handle.read()
    pattern = re.compile(r"^{} = \{{.*?^\}}".format(pattern_name), re.S | re.M)
    if not pattern.search(text):
        raise SystemExit("couldn't find the {} block in season.py".format(pattern_name))
    with open(SEASON_FILE, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(pattern.sub(lambda _: block, text, count=1))
    print("Updated {}".format(os.path.relpath(SEASON_FILE)))


class Reader(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents(guilds=True, emojis=True))
        self.found = {}
        self.failure = None

    async def on_ready(self):
        try:
            guild = self.get_guild(config.GUILD_ID)
            if guild is None:
                self.failure = ("Can't see guild {} - is the bot still in the "
                                "server?".format(config.GUILD_ID))
            else:
                self.found = {e.name: str(e) for e in await guild.fetch_emojis()}
        finally:
            await self.close()


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    logging.basicConfig(level=logging.ERROR)

    gaps = config.missing()
    if gaps:
        raise SystemExit("Missing config: {}".format(", ".join(gaps)))

    client = Reader()

    async def run():
        try:
            await asyncio.wait_for(client.start(config.TOKEN), timeout=60)
        except asyncio.TimeoutError:
            client.failure = "timed out connecting to Discord"
        finally:
            if not client.is_closed():
                await client.close()

    asyncio.run(run())
    if client.failure:
        raise SystemExit(client.failure)

    by_code = {name.upper(): value for name, value in client.found.items()}
    matched = {}
    missing = []
    for code, team in season.TEAM_CODES.items():
        if code.upper() in by_code:
            matched[team] = by_code[code.upper()]
        else:
            missing.append(code)

    # Division badges, matched the same way - an emoji named PL, BL, LL, SA, L1.
    leagues = {code: by_code[code.upper()]
               for code in season.LEAGUES if code.upper() in by_code}

    print("{} custom emoji in the server, {} matched a team code.".format(
        len(client.found), len(matched)))
    if missing:
        print("")
        print("No emoji named after these codes yet ({}):".format(len(missing)))
        print("  " + ", ".join(sorted(missing)))
        print("-# Upload one named after the code and re-run this.")

    if not matched:
        return 0

    block = render(matched)
    league_block = render_leagues(leagues) if leagues else None
    if "--write" in argv:
        write_into_season(block)
        if league_block:
            write_into_season(league_block, "LEAGUE_EMOJI")
            print("Also wrote {} division badge(s).".format(len(leagues)))
    else:
        print("")
        print("Paste this over TEAM_EMOJI in season.py, or re-run with --write:")
        print("")
        print(block)
        if league_block:
            print("")
            print(league_block)
    if not leagues:
        print("")
        print("-# No division badges found. Upload one named after a league "
              "code ({}) to get them beside the headings.".format(
                  ", ".join(season.LEAGUES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
