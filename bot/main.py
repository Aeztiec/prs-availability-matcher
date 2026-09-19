"""The Discord bot: slash commands and the deadline runner.

Thin by design: bot/client.py is the connection, bot/commands/ holds the
slash commands, and the deciding happens in bot/domain, which is tested
without a gateway.

    python -m bot.main              run the bot
    python -m bot.main --check      load everything and exit, without connecting
    python -m bot.main --fast-sync  register commands in the guild too, so new
                                    ones appear immediately instead of waiting
                                    on global propagation. Shows every command
                                    twice; a normal restart clears that.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile

from bot import config
from bot.client import PRSBot
from bot.commands import register
from bot.db import Store
from bot.domain import season
from bot.domain.players import Players
from bot.paths import DB_PATH, PLAYERS_CSV


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    argv = argv if argv is not None else sys.argv[1:]

    if "--check" in argv:
        # Everything except connecting: catches config gaps, a bad sheet and
        # an over-budget selector without touching Discord.
        with tempfile.TemporaryDirectory() as scratch:
            bot = PRSBot(store=Store(os.path.join(scratch, "check.db")))
            bot.load_timings()
            register(bot)
            problems = season.validate([t.country for t in bot.timings.sheet.teams])
            players = Players.load()
            print("timings   : {} ({} slots)".format(bot.timings.title, len(bot.slots)))
            print("players   : {}".format(
                "{} on the sheet".format(len(players.rows)) if players.rows
                else "MISSING - {} (needed for /result)".format(PLAYERS_CSV)))
            print("database  : {}".format(DB_PATH))
            print("commands  : {}".format(
                ", ".join(sorted(c.name for c in bot.tree.get_commands()))))
            print("season    : {}".format("ok" if not problems else "; ".join(problems)))
            gaps = config.missing()
            print("config    : {}".format("ok" if not gaps else "MISSING " + ", ".join(gaps)))
        return 1 if gaps or problems else 0

    gaps = config.missing()
    if gaps:
        print("Missing config: {}. See .env.example.".format(", ".join(gaps)),
              file=sys.stderr)
        return 1

    PRSBot(fast_sync="--fast-sync" in argv).run(config.TOKEN, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
