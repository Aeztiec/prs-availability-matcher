"""The /result command: post a finished game's score, stats and officials."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.commands.checks import staff_only
from bot.commands.picker import game_picker
from bot.domain import discipline, referees, season
from bot.domain.players import Players
from bot.ui import notify, results
from bot.ui.ref_views import matchup
from bot.ui.render import to_discord_embed

log = logging.getLogger("prsbot")

STARTERS = 7

RESULT_GUIDE = chr(10).join([
    "### Result form",
    "One player per line, codes after the name: `vzcadc g g a`",
    "",
    "- `g` goal, `og` own goal, `a` assist",
    "- `yc` yellow (twice is a second yellow), `rc` red",
    "- `on75` / `off75` sub on / off (minute required)",
    "- `inj` injury",
    "- `ps` / `pm` shootout penalty scored / missed",
    "- `g3` three goals",
    "",
    "**Lineups:** pre-filled. Reorder to match who started, keep substitutes "
    "under `BENCH` and remove anyone who did not play.",
    "",
    "**MOTM:** best first (🏆 🥇 🥈 🥉). Optional note after a dash.",
])

OFFICIAL_ROLE = {referees.ROLE_REF: "Main Referee", referees.ROLE_AR: "Assistant Referee"}


def starting_lines(players, club):
    """The club's sheet players: the first seven as starters, then a BENCH line
    and the rest. Staff prune it down to who actually played."""
    roster = players.roster(club)
    lines = roster[:STARTERS]
    if roster[STARTERS:]:
        lines += ["BENCH"] + roster[STARTERS:]
    return chr(10).join(lines)


def roblox_name(players, row):
    """The Roblox username staff registered a referee with, else their Discord
    name if it matches someone on the player sheet, else the Discord name."""
    if row.get("roblox"):
        return row["roblox"]
    match = players.find(row["name"])
    return match["username"] if match else row["name"]


def registered_referees(store):
    """{lowercase name: discord id} for every registered referee, by Roblox
    name and by Discord name, so a result's officials add up under one person."""
    known = {}
    for row in store.referees(active_only=False):
        for name in (row.get("roblox"), row["name"]):
            if name:
                known.setdefault(name.lower(), row["discord_id"])
    return known


async def suspension_follow_up(bot, fixture):
    """After a result is stored: tell the next game's referees who is suspended
    for it, and return the lines staff should see.

    The suspensions are worked out from every stored result, not just this one,
    so what the referees are told is always the full list for that game.
    """
    store = bot.store
    group = discipline.competition_group(fixture.get("league"))
    fixtures = {f["id"]: f for f in store.fixtures()}
    lines = []

    by_next = {}
    for club in (fixture["home_team"], fixture["away_team"]):
        following = discipline.next_game(store, club, fixture)
        for username, standing in discipline.standings(store, club, group).items():
            if fixture["id"] in standing.triggers and (standing.game or standing.carries):
                by_next.setdefault(following["id"] if following else None, []).append(
                    {"username": username, "club": club, "standing": standing})

    for next_id, items in by_next.items():
        lines.append("**Suspended for their next game**")
        lines += notify.suspension_rows(items, fixtures)
        if next_id is None:
            lines.append("-# Their club has no next game in the calendar yet.")
            continue
        upcoming = fixtures[next_id]
        crew = [r["referee_id"] for r in store.fixture_referees(next_id)]
        if crew:
            await bot.announce_suspensions(upcoming, crew)
            lines.append("-# The officials of {} have been told.".format(
                matchup(upcoming)))
        else:
            lines.append("-# No referee has claimed {} yet. Whoever does will be told.".format(
                matchup(upcoming)))

    breaches = discipline.breaches(store, fixture)
    if breaches:
        lines.append("**Played while suspended**")
        lines += ["{} | {}".format(season.label_for(b["club"]), b["username"]) for b in breaches]
    return lines


class ResultForm(discord.ui.Modal):
    """One box per section. The team boxes start filled with that club's
    players from the sheet and Officials with whoever claimed the game, so
    staff delete and add rather than type everything."""

    def __init__(self, bot, players, fixture, home_score, away_score, pens):
        code_home = season.team_code(fixture["home_team"])
        code_away = season.team_code(fixture["away_team"])
        super().__init__(title="Result: {} vs {}".format(
            code_home or "HOME", code_away or "AWAY")[:45])
        self.bot = bot
        self.players = players
        self.fixture = fixture
        self.scores = (home_score, away_score)
        self.pens = pens

        # The how-to sits as a note at the top of the form, so the box labels
        # stay clean.
        self.add_item(discord.ui.TextDisplay(RESULT_GUIDE))

        self.home = self._box("{} stats".format(code_home or fixture["home_team"].title()),
                              starting_lines(players, fixture["home_team"]), "username g g a")
        self.away = self._box("{} stats".format(code_away or fixture["away_team"].title()),
                              starting_lines(players, fixture["away_team"]), "username g g a")
        self.motm = self._box("MOTM & mentions",
                              placeholder="username - short note (optional)")

        names = {r["discord_id"]: roblox_name(players, r)
                 for r in bot.store.referees(active_only=False)}
        crew = [names[r["referee_id"]] + " - " + OFFICIAL_ROLE[r["role"]] + " [Full 90']"
                for r in bot.store.fixture_referees(fixture["id"])
                if r["referee_id"] in names]
        self.officials = self._box("Officiating team", chr(10).join(crew),
                                   "username - Main Referee [Full 90']")

    def _box(self, label, default="", placeholder=None):
        item = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, default=default[:4000],
            required=False, max_length=4000, placeholder=placeholder)
        self.add_item(discord.ui.Label(text=label[:45], component=item))
        return item

    async def on_submit(self, interaction):
        text, problems, data = results.build_full(
            self.fixture, self.scores[0], self.scores[1], self.home.value,
            self.away.value, self.motm.value, self.officials.value, self.players,
            pens=self.pens)
        if problems:
            await interaction.response.send_message(
                embed=to_discord_embed(notify.BoardEmbed(
                    title="Fix these first",
                    description=chr(10).join(problems),
                    footer="Nothing was posted. Run /result again.")),
                ephemeral=True)
            return
        # Stored first: the suspensions and the referee leaderboard are worked
        # out from what is saved, and a corrected result replaces the old one.
        known = registered_referees(self.bot.store)
        officials = [dict(o, referee_id=known.get(o["name"].lower())) for o in data["officials"]]
        self.bot.store.save_result(
            self.fixture["id"], self.scores[0], self.scores[1], self.pens,
            interaction.user.id, data["players"], officials)

        posted = await self.bot.post(
            interaction.channel,
            embed=to_discord_embed(notify.BoardEmbed(description=text)),
            mention_users=False)
        await interaction.response.defer(ephemeral=True)
        lines = ["Result posted." if posted else
                 "⚠️ Couldn't post here. Check my permissions. The result is saved."]
        lines += await suspension_follow_up(self.bot, self.fixture)
        await interaction.followup.send(chr(10).join(lines), ephemeral=True)

    async def on_error(self, interaction, error):
        log.error("result form failed", exc_info=error)
        message = "That result couldn't be posted. It has been logged."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def setup(bot):
    tree = bot.tree
    game_autocomplete, resolve_game = game_picker(bot)
    players = Players.load()

    @tree.command(name="result",
                  description="Post a game's result: pick the game, score, then fill in the form")
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
            ResultForm(bot, players, record, home_score, away_score,
                       None if home_pens is None else (home_pens, away_pens)))

    result.autocomplete("game")(game_autocomplete())
