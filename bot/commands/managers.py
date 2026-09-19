"""Staff commands for team managers: /managers set and list."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.ui import notify
from bot.domain import season

from bot.commands.checks import staff_only
from bot.ui.render import to_discord_embed

log = logging.getLogger("prsbot")


def setup(bot):
    tree = bot.tree
    store = bot.store

    managers_group = app_commands.Group(name="managers", description="Staff: team managers")

    @managers_group.command(name="set", description="Say who manages a team")
    @app_commands.describe(team="Team name as in the timings sheet")
    @staff_only()
    async def managers_set(interaction, team: str, user: discord.User):
        try:
            resolved = bot.timings.find_team(team).country
        except LookupError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        store.set_manager(resolved, user.id)
        await interaction.response.send_message(
            "{} manages **{}**.".format(user.mention, resolved), ephemeral=True
        )

    @managers_group.command(name="list", description="Every team and its manager")
    @staff_only()
    async def managers_list(interaction, missing_only: bool = False):
        known = store.managers()
        league_of_code = {}
        for games in season.FIXTURES.values():
            for home, away, league in games:
                league_of_code.setdefault(home, league)
                league_of_code.setdefault(away, league)
        sections = []
        for league, league_name in season.LEAGUES.items():
            rows = []
            for code, name in sorted(season.TEAM_CODES.items(), key=lambda kv: kv[1]):
                if league_of_code.get(code) != league:
                    continue
                who = known.get(name)
                if missing_only and who:
                    continue
                rows.append("{} - {}".format(
                    season.label_for(name) if season.usable_emoji(season.TEAM_EMOJI.get(name))
                    else name,
                    "<@{}>".format(who) if who else "**nobody**"))
            if rows:
                badge = season.LEAGUE_EMOJI.get(league)
                head = "__**{}:**__".format(league_name)
                if season.usable_emoji(badge):
                    head += " " + badge
                sections.append(head + chr(10) + chr(10).join(rows))
        lines = [(chr(10) * 2).join(sections)] if sections else []
        embed = notify.BoardEmbed(
            title="Team managers",
            description=("\n".join(lines[:40]) if lines else "Every team is covered. ✅"),
        )
        await interaction.response.send_message(embed=to_discord_embed(embed), ephemeral=True)

    tree.add_command(managers_group)
