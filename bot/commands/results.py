"""The /result command: post a finished game's score, stats and officials."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.ui import notify, results
from bot.domain import referees, season
from bot.domain.players import Players

from bot.commands.checks import staff_only
from bot.commands.picker import game_picker
from bot.ui.render import to_discord_embed

log = logging.getLogger("prsbot")


def setup(bot):
    tree = bot.tree
    store = bot.store
    game_autocomplete, resolve_game = game_picker(bot)

    players = Players.load()

    STARTERS = 7

    RESULT_GUIDE = chr(10).join([
        "### Result form",
        "One player per line, codes after the name: `vzcadc g g a`",
        "",
        "- `g` goal, `a` assist",
        "- `yc` yellow, `rc` red",
        "- `on75` / `off75` sub on / off (minute required)",
        "- `ps` / `pm` shootout penalty scored / missed",
        "- `g3` three goals",
        "",
        "**Lineups:** pre-filled. Reorder to match who started, keep substitutes "
        "under `BENCH` and remove anyone who did not play.",
        "",
        "**MOTM:** best first (🏆 🥇 🥈 🥉). Optional note after a dash.",
    ])

    def starting_lines(club):
        """The club's sheet players: the first seven as starters, then a BENCH
        line and the rest. Staff prune it down to who actually played."""
        roster = players.roster(club)
        lines = roster[:STARTERS]
        if roster[STARTERS:]:
            lines += ["BENCH"] + roster[STARTERS:]
        return chr(10).join(lines)

    OFFICIAL_ROLE = {referees.ROLE_REF: "Main Referee", referees.ROLE_AR: "Assistant Referee"}

    class ResultForm(discord.ui.Modal):
        """One box per section. The team boxes start filled with that club's
        players from the sheet and Officials with whoever claimed the game, so
        staff delete and add rather than type everything."""

        def __init__(self, fixture, home_score, away_score, pens):
            super().__init__(title="Result: {} vs {}".format(
                season.team_code(fixture["home_team"]) or "HOME",
                season.team_code(fixture["away_team"]) or "AWAY")[:45])
            self.fixture = fixture
            self.scores = (home_score, away_score)
            self.pens = pens

            # The how-to sits as a note at the top of the form, so the box
            # labels stay clean.
            self.add_item(discord.ui.TextDisplay(RESULT_GUIDE))

            def box(label, default="", placeholder=None):
                item = discord.ui.TextInput(
                    style=discord.TextStyle.paragraph, default=default[:4000],
                    required=False, max_length=4000, placeholder=placeholder)
                self.add_item(discord.ui.Label(text=label[:45], component=item))
                return item

            def code(team):
                return season.team_code(team) or team.title()

            self.home = box("{} stats".format(
                code(fixture["home_team"])), starting_lines(fixture["home_team"]),
                "username g g a")
            self.away = box("{} stats".format(
                code(fixture["away_team"])), starting_lines(fixture["away_team"]),
                "username g g a")
            self.motm = box("MOTM & mentions",
                            placeholder="username - short note (optional)")
            def roblox_name(row):
                """The Roblox username staff registered them with, else their
                Discord name if it matches someone on the sheet, else the
                Discord name as is."""
                if row.get("roblox"):
                    return row["roblox"]
                match = players.find(row["name"])
                return match["username"] if match else row["name"]

            names = {r["discord_id"]: roblox_name(r)
                     for r in store.referees(active_only=False)}
            crew = [names[r["referee_id"]] + " - " + OFFICIAL_ROLE[r["role"]] + " [Full 90']"
                    for r in store.fixture_referees(fixture["id"])
                    if r["referee_id"] in names]
            self.officials = box("Officiating team", chr(10).join(crew),
                                 "username - Main Referee [Full 90']")

        async def on_submit(self, interaction):
            text, problems = results.build(
                self.fixture, self.scores[0], self.scores[1], self.home.value,
                self.away.value, self.motm.value, self.officials.value, players,
                pens=self.pens)
            if problems:
                await interaction.response.send_message(
                    embed=to_discord_embed(notify.BoardEmbed(
                        title="Fix these first",
                        description=chr(10).join(problems),
                        footer="Nothing was posted. Run /result again.")),
                    ephemeral=True)
                return
            posted = await bot.post(
                interaction.channel,
                embed=to_discord_embed(notify.BoardEmbed(description=text)),
                mention_users=False)
            await interaction.response.send_message(
                "Result posted." if posted else
                "⚠️ Couldn't post here. Check my permissions.", ephemeral=True)

    @tree.command(name="result", description="Post a game's result: pick the game, score, then fill in the form")
    @app_commands.describe(
        game="The game - pick from the list as you type",
        home_score="Home team's goals",
        away_score="Away team's goals",
        home_pens="Home team's penalty shootout goals, if there was one",
        away_pens="Away team's penalty shootout goals, if there was one",
    )
    @staff_only()
    async def result(interaction, game: str, home_score: app_commands.Range[int, 0, 99],
                     away_score: app_commands.Range[int, 0, 99],
                     home_pens: app_commands.Range[int, 0, 99] = None,
                     away_pens: app_commands.Range[int, 0, 99] = None):
        record = await resolve_game(interaction, game)
        if record is None:
            return
        if (home_pens is None) != (away_pens is None):
            await interaction.response.send_message(
                "Give both penalty scores, or neither.", ephemeral=True)
            return
        if not players.rows:
            await interaction.response.send_message(
                "The player sheet (data/players.csv) is missing, so I can't check "
                "usernames.", ephemeral=True)
            return
        await interaction.response.send_modal(
            ResultForm(record, home_score, away_score,
                       None if home_pens is None else (home_pens, away_pens)))

    result.autocomplete("game")(game_autocomplete())
