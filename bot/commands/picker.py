"""Picking a game by name: the autocomplete list and turning a pick back into a fixture."""

from __future__ import annotations

import logging

from discord import app_commands

from bot.ui.ref_views import staff_game_name

log = logging.getLogger("prsbot")


def game_picker(bot):
    """(game_autocomplete, resolve_game) for staff commands that take a game."""
    store = bot.store

    def game_autocomplete(needs_time=False):
        """Autocomplete for staff commands: games in the open gameweeks named
        by teams, kickoff and league - staff never have to look up or type a
        fixture number. Typing filters by team code or full team name."""
        async def complete(interaction, current: str):
            try:
                choices = await find(current)
                log.info("game autocomplete for %r: %d choices", current, len(choices))
                return choices
            except Exception:
                log.exception("game autocomplete failed")
                return []

        async def find(current):
            weeks = {gw.week for gw in bot.open_gameweeks()}
            rows = [f for f in store.fixtures()
                    if f["week"] in weeks and (f["slot_key"] or not needs_time)]

            def order(f):
                slot = bot.slot(f["slot_key"]) if f["slot_key"] else None
                return (f["week"], slot.day_index if slot else 9,
                        slot.minutes if slot else 0, f["id"])

            needle = current.lower()
            choices = []
            for f in sorted(rows, key=order):
                slot = bot.slot(f["slot_key"]) if f["slot_key"] else None
                name = staff_game_name(f, slot)
                haystack = "{} {} {}".format(name, f["home_team"], f["away_team"]).lower()
                if needle in haystack:
                    choices.append(app_commands.Choice(name=name[:100], value=str(f["id"])))
            return choices[:25]
        return complete

    async def resolve_game(interaction, value):
        """The fixture a picked game refers to, or None after telling the
        user what to do instead."""
        try:
            record = store.fixture(int(value))
        except ValueError:
            record = None
            message = "Pick a game from the list that appears as you type."
        else:
            message = "That game no longer exists."
        if record is None:
            await interaction.response.send_message(message, ephemeral=True)
        return record

    return game_autocomplete, resolve_game
