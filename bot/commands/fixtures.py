"""Staff commands for fixtures: /fixture list, show, set and board."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.ui import notify
from bot.ui.ref_views import matchup
from bot.domain import discipline
from bot.domain.scheduling import Status

from bot.commands.checks import staff_only
from bot.commands.picker import game_picker
from bot.ui.render import to_discord_embed

log = logging.getLogger("prsbot")


def setup(bot):
    tree = bot.tree
    store = bot.store
    game_autocomplete, resolve_game = game_picker(bot)

    # Split by who uses them: /ref is what a referee runs on themselves,
    # /refs is staff administering referees as a group - easy to tell apart
    # in the picker, and the descriptions say so too.
    fixture_group = app_commands.Group(name="fixture", description="Staff: fixture scheduling")

    @fixture_group.command(name="list", description="Scheduling status for a week")
    @app_commands.describe(week="Saturday of the weekend, YYYY-MM-DD. Defaults to the next one.")
    @staff_only()
    async def fixture_list(interaction, week: str = None):
        await bot.show_dashboard(interaction, week or bot.current_week())

    @fixture_group.command(name="show", description="One game, with its scheduling log")
    @app_commands.describe(game="The game - pick from the list as you type")
    @staff_only()
    async def fixture_show(interaction, game: str):
        record = await resolve_game(interaction, game)
        if record is None:
            return
        fixtures = {f["id"]: f for f in store.fixtures()}
        suspended = notify.suspension_rows(discipline.suspended_for(store, record), fixtures)
        await interaction.response.send_message(
            embed=to_discord_embed(notify.fixture_detail(
                record, store.history(record["id"]),
                bot.slot(record["slot_key"]), store.fixture_referees(record["id"]),
                suspended=suspended)),
            ephemeral=True,
        )

    fixture_show.autocomplete("game")(game_autocomplete())

    @fixture_group.command(name="suspensions",
                           description="Players suspended for a game still to come")
    @staff_only()
    async def fixture_suspensions(interaction):
        fixtures = {f["id"]: f for f in store.fixtures()}
        await interaction.response.send_message(
            embed=to_discord_embed(notify.suspension_list(
                discipline.current(store), fixtures, fixtures.get)),
            ephemeral=True,
        )

    @fixture_group.command(name="set", description="Set a kickoff time by hand")
    @app_commands.describe(
        game="The game - pick from the list as you type",
        slot="Slot key, e.g. sat_1800",
    )
    @staff_only()
    async def fixture_set(interaction, game: str, slot: str):
        record = await resolve_game(interaction, game)
        if record is None:
            return
        chosen = bot.slot(slot)
        if not chosen:
            await interaction.response.send_message(
                "Unknown slot `{}`. Valid: {}".format(
                    slot, ", ".join(s.key for s in bot.slots)
                ),
                ephemeral=True,
            )
            return
        fixture_id = record["id"]
        store.set_schedule(fixture_id, chosen.key, "MANUAL", Status.SCHEDULED)
        store.note(fixture_id, "set by hand", "{} by {}".format(chosen.key, interaction.user.id))
        await interaction.response.defer(ephemeral=True)
        await bot.refresh_board(record["week"])
        await interaction.followup.send(
            "**{}** set to {}. The fixture board has been updated.".format(
                matchup(record), chosen),
            ephemeral=True,
        )

    fixture_set.autocomplete("game")(game_autocomplete())

    @fixture_group.command(name="board",
                           description="Publish the week's fixture board here, or stop updating it")
    @app_commands.describe(
        week="Saturday of the weekend, YYYY-MM-DD. Defaults to the next one.",
        stop="Stop updating the board instead of (re)publishing it",
    )
    @staff_only()
    async def fixture_board_cmd(interaction, week: str = None, stop: bool = False):
        week = week or bot.current_week()

        if stop:
            if not store.board(week):
                await interaction.response.send_message(
                    "No board published for the week of {}.".format(week), ephemeral=True
                )
                return
            store.forget_board(week)
            await interaction.response.send_message(
                "Stopped updating the board for {}. The message is still there; "
                "delete it by hand if you want it gone.".format(week),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        existing = store.board(week)
        try:
            await bot.publish_board(week, interaction.channel)
        except (discord.Forbidden, discord.HTTPException) as error:
            await interaction.followup.send(
                "Couldn't post here: {}".format(error), ephemeral=True
            )
            return
        note = ""
        if existing:
            note = ("\n-# The previous board for this week is no longer updated - "
                    "delete it if you don't want it lying around.")
        await interaction.followup.send(
            "Published the week of {} here. It'll update itself as fixtures "
            "change.{}".format(week, note),
            ephemeral=True,
        )

    tree.add_command(fixture_group)
