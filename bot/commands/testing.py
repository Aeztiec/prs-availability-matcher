"""Staff commands that fake activity, for testing the flow end to end."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.domain import referees, season
from bot.ui.text import plural
from bot.domain.orchestrator import run_once_randomly

from bot.commands.checks import staff_only

log = logging.getLogger("prsbot")


def setup(bot):
    tree = bot.tree
    store = bot.store

    # Everything here fakes real activity so a handful of people can exercise
    # the whole flow without forty real managers and several real referees.
    # Kept in its own group rather than scattered flags, so it's never a
    # question of whether a command is "the real one" or "the test one".
    test_group = app_commands.Group(name="test", description="Staff: fake activity for testing")

    @test_group.command(name="managers",
                        description="TEST ONLY: spread every team across testers, round-robin")
    @app_commands.describe(
        user1="Tester to assign teams to", user2="Second tester (optional)",
        user3="Third tester (optional)",
    )
    @staff_only()
    async def test_managers(interaction, user1: discord.User,
                            user2: discord.User = None, user3: discord.User = None):
        # Every team, not just unmanaged ones - a partial prior run (or a real
        # /managers set someone forgot about) would otherwise leave the spread
        # lopsided instead of even across all 40.
        testers = [u for u in (user1, user2, user3) if u is not None]
        assigned = []
        for index, (code, name) in enumerate(sorted(season.TEAM_CODES.items())):
            tester = testers[index % len(testers)]
            store.set_manager(name, tester.id)
            # Also rewrite fixtures that already exist for this team - without
            # this, re-running the command after /gw open leaves the OLD
            # manager id trapped on those fixtures, still blocking them from
            # refereeing a game they no longer actually manage.
            store.reassign_fixture_managers(name, tester.id)
            assigned.append("{} → {}".format(code, tester.display_name))

        shown = ", ".join(assigned[:15]) + (", ..." if len(assigned) > 15 else "")
        await interaction.response.send_message(
            "Set **{}**, spread evenly across {}. This overwrites any "
            "existing manager for every team, including on fixtures already "
            "created - so a previous manager is fully freed up (e.g. to "
            "referee) rather than left blocked on their old games.\n-# {}".format(
                plural(len(assigned), "team"), ", ".join(t.mention for t in testers), shown),
            ephemeral=True,
        )

    @test_group.command(name="referees",
                        description="TEST ONLY: register testers as refs and claim every open game")
    @app_commands.describe(
        user1="Tester to register and claim games as",
        user2="Second tester (optional)", user3="Third tester (optional)",
    )
    @staff_only()
    async def test_referees(interaction, user1: discord.User,
                            user2: discord.User = None, user3: discord.User = None):
        testers = [u for u in (user1, user2, user3) if u is not None]
        for tester in testers:
            store.add_referee(tester.id, tester.display_name)

        claimed, skipped, weeks = 0, 0, set()
        for fixture, _ in bot.fixtures_needing_referee():
            for _ in range(len(testers) * 3):  # up to 3 roles, each tester gets a fair shot
                roster = store.fixture_referees(fixture["id"])
                if referees.next_open_role([r["role"] for r in roster]) is None:
                    break
                tester = testers[(claimed + skipped) % len(testers)]
                try:
                    referees.claim(store, fixture, tester.id)
                    claimed += 1
                    weeks.add(fixture["week"])
                except referees.ClaimError:
                    skipped += 1

        for week in weeks:
            await bot.refresh_ref_board(week)
            await bot.refresh_board(week)
        await interaction.response.send_message(
            "Registered {} as referees and made **{}** across open "
            "games ({} skipped, usually a kickoff clash).".format(
                ", ".join(t.mention for t in testers), plural(claimed, "claim"), skipped),
            ephemeral=True,
        )

    @test_group.command(name="schedule",
                        description="TEST ONLY: instantly give every unscheduled fixture a random time")
    @app_commands.describe(
        week="Saturday of the weekend, YYYY-MM-DD - required, so this can't "
             "land on the live gameweek by accident",
    )
    @staff_only()
    async def test_schedule(interaction, week: str):
        await interaction.response.defer(ephemeral=True)
        outcomes = run_once_randomly(store, bot.timings, week=week)
        log.warning("TEST MODE: randomly scheduled %d fixture(s) for %s by %s",
                   len(outcomes), week, interaction.user)
        await bot.process(week=week)
        await bot.show_dashboard(interaction, week, followup=True)

    tree.add_command(test_group)
