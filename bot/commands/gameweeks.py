"""Staff commands for gameweeks: /gw list, open and close."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.ui import notify
from bot.domain import season
from bot.ui.text import plural
from bot.domain.orchestrator import run_once_randomly
from bot.domain.weeks import uk_time, utcnow

from bot.commands.checks import staff_only
from bot.ui.render import to_discord_embed

log = logging.getLogger("prsbot")


def setup(bot):
    tree = bot.tree
    store = bot.store

    gw_group = app_commands.Group(name="gw", description="Gameweeks")

    @gw_group.command(name="list", description="The season calendar and what's open")
    async def gw_list(interaction):
        now = utcnow()
        here = season.current(now)
        opened = store.opened_gameweeks()
        lines = []
        for gw in season.ALL:
            marks = []
            if here and gw.key == here.key:
                marks.append("**current**")
            if gw.key in opened:
                marks.append("opened early")
            if not gw.has_fixtures:
                marks.append("no fixtures yet")
            count = len(store.fixtures(gameweek=gw.key))
            if count:
                marks.append("{} created".format(count))
            local, label = uk_time(gw.deadline)
            lines.append("`{:<4}` {}: plays {}, deadline {} {}{}".format(
                gw.key, gw.label, gw.friday.strftime("%a %d %b"),
                local.strftime("%a %d %b %H:%M"), label,
                "  ·  " + ", ".join(marks) if marks else "",
            ))
        embed = notify.BoardEmbed(
            title="{} calendar".format(season.SEASON),
            description="\n".join(lines),
            footer=("Managers can set availability for the current gameweek, "
                    "plus any Officials have opened early."),
        )
        await interaction.response.send_message(embed=to_discord_embed(embed), ephemeral=True)

    @gw_group.command(name="open",
                      description="Create a gameweek's fixtures and post the announcement")
    @app_commands.describe(
        gameweek="e.g. GW2",
        count="Only create the first N fixtures. For testing; default is all 20.",
        test="TESTING ONLY: instantly give every fixture a random kickoff time, "
             "skipping availability entirely",
    )
    @staff_only()
    async def gw_open(interaction, gameweek: str, count: int = None, test: bool = False):
        await interaction.response.defer(ephemeral=True)
        try:
            gw = season.gameweek(gameweek)
        except LookupError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        if not gw.has_fixtures:
            await interaction.followup.send(
                "{} has no fixture list yet. Add it to season.py once the draw "
                "is made.".format(gw.key),
                ephemeral=True,
            )
            return

        manager_of = {}
        missing = []
        for code, name in season.TEAM_CODES.items():
            discord_id = store.manager_of(name)
            if discord_id:
                manager_of[name] = discord_id
            elif any(code in (h, a) for h, a, _ in gw.fixtures):
                missing.append("{} ({})".format(code, name))
        if missing:
            await interaction.followup.send(
                embed=to_discord_embed(notify.BoardEmbed(
                    title="{} can't open yet".format(gw.label),
                    description=("These teams have no manager registered, so their "
                                 "fixtures can't be created:" + chr(10) * 2
                                 + ", ".join(sorted(missing))),
                    footer="Add them with /managers set.",
                )),
                ephemeral=True,
            )
            return

        store.open_gameweek(gw.key, interaction.user.id)
        made, skipped = await bot.create_gameweek_fixtures(
            gw, manager_of, opened_by=interaction.user.id, limit=count
        )

        scheduled = 0
        if test:
            # Not gated on `made`: fixtures from an earlier /gw open (or an
            # earlier test:True run) are skipped as duplicates above, but if
            # they're still sitting TBD this should fill them in too.
            outcomes = run_once_randomly(store, bot.timings, week=gw.week)
            scheduled = len(outcomes)
            log.warning("TEST MODE: randomly scheduled %d fixture(s) for %s by %s",
                       scheduled, gw.week, interaction.user)

        # The announcement is the primary channel, not the DMs. Plenty of
        # managers have DMs from server members switched off, and for them a DM
        # is simply never delivered - so the fixture list and the button that
        # opens the selector both go in the channel where everyone can see them.
        # In test mode there's nothing left to submit, so the button is skipped.
        posted = None
        try:
            posted = await bot.publish_board(gw.week, interaction.channel, with_button=not test)
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning("could not post the announcement: %s", error)

        deadline_local, deadline_label = uk_time(gw.deadline)
        parts = ["{} created. Deadline: {} {}.".format(
            plural(len(made), "fixture").capitalize(), deadline_local.strftime("%a %d %b %H:%M"), deadline_label)]
        if test:
            parts.append("-# TEST MODE: {} given a random kickoff time - "
                         "availability was skipped entirely.".format(
                             plural(scheduled, "fixture")))
        if posted:
            parts.append("Announcement posted here" +
                         ("." if test else " with the **Submit my timings** button.") +
                         " It updates itself as fixtures are agreed.")
        else:
            parts.append("⚠️ Couldn't post the announcement in this channel. "
                         "Check my permissions, then run `/fixture board`.")
        if count:
            parts.append("-# Limited to the first {} of {} fixtures. Run again "
                         "without `count` to create the rest.".format(
                             count, len(gw.fixtures) + len(gw.uefa_fixtures)))
        if skipped:
            parts.append("Skipped {}: {}".format(len(skipped), ", ".join(skipped[:8])))
        await interaction.followup.send(
            embed=to_discord_embed(notify.BoardEmbed(
                title="{} opened".format(gw.label), description=chr(10).join(parts))),
            ephemeral=True,
        )

    @gw_group.command(name="close", description="Stop managers scheduling this gameweek")
    @staff_only()
    async def gw_close(interaction, gameweek: str):
        try:
            gw = season.gameweek(gameweek)
        except LookupError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        store.close_gameweek(gw.key)
        await interaction.response.send_message(
            "{} is no longer open early. Existing fixtures are untouched; the "
            "current gameweek is always open.".format(gw.key),
            ephemeral=True,
        )

    tree.add_command(gw_group)
