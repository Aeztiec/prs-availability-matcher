"""The /availability command: a manager sets their timings for the week."""

from __future__ import annotations

import logging


from bot.ui.views import SCOPE_FIXTURE, Target, open_selector


log = logging.getLogger("prsbot")


def setup(bot):
    tree = bot.tree
    store = bot.store

    @tree.command(description="Set your availability for the week")
    async def availability(interaction):
        week, mine = bot.my_open_week(interaction.user.id)
        if not mine:
            open_now = ", ".join(gw.key for gw in bot.open_gameweeks()) or "none"
            await interaction.response.send_message(
                "You have no fixtures waiting on availability.\n"
                "-# Open gameweeks: {}. Later ones open closer to the time, or "
                "when Officials unlock them.".format(open_now),
                ephemeral=True,
            )
            return
        # One selector for the whole week - it covers every fixture just
        # found above, even if that's more than one team's game.
        await open_selector(interaction, store, Target(SCOPE_FIXTURE, week),
                            bot.offerable_slots(mine[0]["gameweek"]))
