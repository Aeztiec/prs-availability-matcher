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


def write_season_emoji(value):
    """Set SEASON_EMOJI, or clear it if that badge is gone from the server."""
    with open(SEASON_FILE, encoding="utf-8") as handle:
        text = handle.read()
    pattern = re.compile(r'^SEASON_EMOJI = ".*?"$', re.M)
    if not pattern.search(text):
        return False
    updated = pattern.sub('SEASON_EMOJI = "{}"'.format(value), text, count=1)
    if updated == text:
        return False
    with open(SEASON_FILE, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    return True


def write_into_season(block, pattern_name="TEAM_EMOJI"):
    with open(SEASON_FILE, encoding="utf-8") as handle:
        text = handle.read()
    # Two shapes to match: an empty one-liner, or a multi-line dict closed by a
    # brace in column zero. Matching only the latter is how an earlier version
    # of this ate everything between "LEAGUE_EMOJI = {}" and the next block's
    # closing brace - so the empty form is matched first and explicitly.
    empty = re.compile(r"^{} = \{{\s*\}}$".format(pattern_name), re.M)
    filled = re.compile(r"^{} = \{{\n.*?\n\}}$".format(pattern_name), re.S | re.M)

    for pattern in (empty, filled):
        if pattern.search(text):
            updated = pattern.sub(lambda _: block, text, count=1)
            break
    else:
        raise SystemExit(
            "couldn't find a {} block in season.py to replace".format(pattern_name)
        )

    # Refuse to write something that would not import - a corrupted season.py
    # takes the whole bot down, and this runs unattended from a shell.
    try:
        compile(updated, SEASON_FILE, "exec")
    except SyntaxError as error:
        raise SystemExit(
            "refusing to write: the result would not parse ({})".format(error)
        )
    for needed in ("GAMEWEEKS", "TEAM_CODES", "FIXTURES", "LEAGUES"):
        if "\n{} = ".format(needed) not in updated:
            raise SystemExit(
                "refusing to write: {} went missing from the result".format(needed)
            )

    with open(SEASON_FILE, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    print("Updated {} ({})".format(os.path.relpath(SEASON_FILE), pattern_name))


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

    # Both blocks are always written, even when empty. Writing only what
    # matched left stale entries behind: deleting a badge from the server -
    # which happens the moment the 50-emoji cap bites - left season.py
    # pointing at an id that no longer exists, and Discord renders a dead id
    # as raw text rather than nothing.
    block = render(matched)
    league_block = render_leagues(leagues)
    prs = by_code.get("PRS", "")

    if "--write" in argv:
        write_into_season(block)
        write_into_season(league_block, "LEAGUE_EMOJI")
        if write_season_emoji(prs):
            print("SEASON_EMOJI {}.".format("set" if prs else "cleared"))
        dropped = [t for t in season.TEAM_EMOJI if t not in matched]
        stale_leagues = [c for c in season.LEAGUE_EMOJI if c not in leagues]
        if dropped or stale_leagues:
            print("Dropped {} stale badge(s) whose emoji no longer exist: {}".format(
                len(dropped) + len(stale_leagues),
                ", ".join(sorted(stale_leagues) + sorted(dropped)[:6])))
        print("Now: {} team badge(s), {} division badge(s).".format(
            len(matched), len(leagues)))
    else:
        print("")
        print("Paste these over the blocks in season.py, or re-run with --write:")
        print("")
        print(block)
        print("")
        print(league_block)
        print("")
        print('SEASON_EMOJI = "{}"'.format(prs))

    if not leagues:
        print("")
        print("-# No division badges on the server. Headings will render "
              "without one, which is fine - upload an emoji named after a "
              "league code ({}) if you want them back.".format(
                  ", ".join(season.LEAGUES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
