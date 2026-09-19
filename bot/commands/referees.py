"""Referee commands: /refs (staff) and /ref (a referee on themselves)."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.ui import notify
from bot.domain import referees, season
from bot.ui.ref_views import game_name
from bot.domain.scheduling import Status

from bot.commands.checks import is_staff, staff_only
from bot.commands.picker import game_picker
from bot.ui.render import to_discord_embed

log = logging.getLogger("prsbot")
MAX_TIER = 5   # referee tiers run 1..MAX_TIER; they will limit which games a referee can take


def setup(bot):
    tree = bot.tree
    store = bot.store
    game_autocomplete, resolve_game = game_picker(bot)

    refs_group = app_commands.Group(name="refs", description="Staff: manage referees")

    ref_group = app_commands.Group(name="ref", description="For referees: your own assignments")

    @refs_group.command(name="register", description="Register a referee, or deactivate one")
    @app_commands.describe(
        active="False deactivates them instead of registering",
        tier="Their referee tier (new referees start at 1)",
        roblox="Their Roblox username, shown in results",
    )
    @staff_only()
    async def refs_register(interaction, user: discord.User, active: bool = True,
                            tier: app_commands.Range[int, 1, MAX_TIER] = None,
                            roblox: str = None):
        if active:
            store.add_referee(user.id, user.display_name, tier, roblox)
            current = next(r["tier"] for r in store.referees() if r["discord_id"] == user.id)
            await interaction.response.send_message(
                "{} registered as a referee (Tier {}).".format(user.mention, current),
                ephemeral=True,
            )
        else:
            store.set_referee_active(user.id, False)
            await interaction.response.send_message(
                "{} deactivated.".format(user.mention), ephemeral=True
            )

    @refs_group.command(name="tier", description="Change a referee's tier")
    @app_commands.describe(user="The referee", tier="Their new tier")
    @staff_only()
    async def refs_tier(interaction, user: discord.User,
                        tier: app_commands.Range[int, 1, MAX_TIER]):
        if not store.is_active_referee(user.id):
            await interaction.response.send_message(
                "{} isn't a registered referee. Add them with `/refs register`.".format(
                    user.mention), ephemeral=True)
            return
        store.set_referee_tier(user.id, tier)
        await interaction.response.send_message(
            "{} is now **Tier {}**.".format(user.mention, tier), ephemeral=True)

    @refs_group.command(name="list", description="Registered referees by tier")
    @staff_only()
    async def refs_list(interaction):
        rows = store.referees()
        if not rows:
            await interaction.response.send_message(
                "No referees registered. Add one with `/refs register`.", ephemeral=True
            )
            return
        sections = []
        for tier in sorted({r["tier"] for r in rows}):
            names = ["{} - <@{}>".format(season.SEASON_EMOJI, r["discord_id"])
                     for r in rows if r["tier"] == tier]
            sections.append("__**Tier {}:**__".format(tier) + chr(10) + chr(10).join(names))
        embed = notify.BoardEmbed(
            title="Referees", description=(chr(10) * 2).join(sections))
        await interaction.response.send_message(embed=to_discord_embed(embed), ephemeral=True)

    @refs_group.command(name="assign", description="Assign a referee by hand")
    @app_commands.describe(
        game="The game - pick from the list as you type",
        role="Which role to fill (default: next open one)",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="Referee", value=referees.ROLE_REF),
        app_commands.Choice(name="Assistant", value=referees.ROLE_AR),
    ])
    @staff_only()
    async def refs_assign(interaction, game: str, user: discord.User,
                          role: app_commands.Choice[str] = None):
        record = await resolve_game(interaction, game)
        if record is None:
            return
        if not record["slot_key"]:
            await interaction.response.send_message(
                "That game has no kickoff time yet.", ephemeral=True
            )
            return
        fixture_id = record["id"]
        name = game_name(record, bot.slot(record["slot_key"]))
        roster = store.fixture_referees(fixture_id)
        if any(r["referee_id"] == user.id for r in roster):
            await interaction.response.send_message(
                "{} is already on **{}**.".format(user.mention, name), ephemeral=True
            )
            return
        chosen_role = role.value if role else referees.next_open_role(
            [r["role"] for r in roster])
        if chosen_role is None:
            await interaction.response.send_message(
                "**{}** already has a full team of officials.".format(name),
                ephemeral=True,
            )
            return

        store.add_referee(user.id, user.display_name)
        store.claim_referee(fixture_id, user.id, chosen_role)
        if chosen_role == referees.ROLE_REF:
            store.set_status(fixture_id, Status.FULLY_CONFIRMED,
                             "referee set by hand by {}".format(interaction.user.id))
        await bot.refresh_ref_board(record["week"])
        await bot.refresh_board(record["week"])
        await interaction.response.send_message(
            "{} assigned as {} on **{}**. The referee board has been updated.".format(
                user.mention, referees.ROLE_LABEL[chosen_role], name),
            ephemeral=True,
        )

    refs_assign.autocomplete("game")(game_autocomplete(needs_time=True))

    @refs_group.command(name="board",
                        description="Publish the referee claim board here, or stop updating it")
    @app_commands.describe(
        week="Saturday of the weekend, YYYY-MM-DD. Defaults to the next one.",
        stop="Stop updating the board instead of (re)publishing it",
    )
    @staff_only()
    async def refs_board(interaction, week: str = None, stop: bool = False):
        week = week or bot.current_week()

        if stop:
            if not store.ref_board(week):
                await interaction.response.send_message(
                    "No referee board published for the week of {}.".format(week),
                    ephemeral=True,
                )
                return
            store.forget_ref_board(week)
            await interaction.response.send_message(
                "Stopped updating the referee board for {}. The message is still "
                "there; delete it by hand if you want it gone.".format(week),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        existing = store.ref_board(week)
        try:
            await bot.publish_ref_board(week, interaction.channel)
        except (discord.Forbidden, discord.HTTPException) as error:
            await interaction.followup.send(
                "Couldn't post here: {}".format(error), ephemeral=True
            )
            return
        note = ""
        if existing:
            note = ("\n-# The previous referee board for this week is no longer "
                    "updated - delete it if you don't want it lying around.")
        await interaction.followup.send(
            "Published the referee board for the week of {} here. It'll "
            "update itself as games get times and claims come in.{}".format(
                week, note),
            ephemeral=True,
        )

    @ref_group.command(name="dropout", description="Drop out of a game you're officiating")
    @app_commands.describe(
        game="The game to drop out of - pick from the list as you type",
        user="Staff only: drop someone else instead of yourself",
    )
    async def ref_dropout(interaction, game: str, user: discord.User = None):
        target = user or interaction.user
        if user is not None and user.id != interaction.user.id and not is_staff(interaction):
            await interaction.response.send_message(
                "You can only drop yourself. Staff can remove someone else with "
                "the `user` option.",
                ephemeral=True,
            )
            return

        record = await resolve_game(interaction, game)
        if record is None:
            return
        fixture_id = record["id"]
        try:
            role = referees.drop(store, fixture_id, target.id)
        except referees.ClaimError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        who = "You're" if target.id == interaction.user.id else "{} is".format(target.mention)
        await interaction.response.send_message(
            "{} off **{}** ({}). Someone else can claim it now.".format(
                who, game_name(record, bot.slot(record["slot_key"])),
                referees.ROLE_LABEL[role]),
            ephemeral=True,
        )
        await bot.refresh_ref_board(record["week"])
        await bot.refresh_board(record["week"])

    @ref_dropout.autocomplete("game")
    async def ref_dropout_games(interaction, current: str):
        """The games this person is actually on, named by teams and kickoff -
        so nobody has to know or type a fixture number."""
        chosen = getattr(interaction.namespace, "user", None)
        who = interaction.user.id
        if chosen is not None and chosen.id != who and is_staff(interaction):
            who = chosen.id
        choices = []
        for fixture in store.fixtures_officiated_by(who):
            slot = bot.slot(fixture["slot_key"]) if fixture["slot_key"] else None
            if slot is None:
                continue
            name = game_name(fixture, slot, fixture["role"])
            if current.lower() in name.lower():
                choices.append(app_commands.Choice(name=name[:100], value=str(fixture["id"])))
        return choices[:25]

    tree.add_command(refs_group)
    tree.add_command(ref_group)
