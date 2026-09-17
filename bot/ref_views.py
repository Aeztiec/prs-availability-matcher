"""The claim menu under the referee board.

One persistent Select, keyed by week rather than by fixture - so a single
component can offer every open game at once instead of a message per fixture.
Same DynamicItem pattern as everywhere else: it survives a restart, and the
claimant's identity comes from the interaction rather than the component, so
nobody can claim on someone else's behalf by replaying it.
"""

from __future__ import annotations

import discord

from . import referees
from .weeks import slot_datetime

MAX_OPTIONS = 25  # Discord's cap on a single select menu


def claim_options(pending, slot_for, week):
    """Select options for the open fixtures, most urgent (no referee at all)
    first, then soonest kickoff. Discord allows at most 25 options."""
    ordered = sorted(pending, key=lambda item: (
        0 if not any(r["role"] == referees.ROLE_REF for r in item[1]) else 1,
        slot_datetime(week, slot_for(item[0]["slot_key"])),
    ))

    options = []
    for fixture, roster in ordered[:MAX_OPTIONS]:
        slot = slot_for(fixture["slot_key"])
        role = referees.next_open_role([r["role"] for r in roster])
        label = "#{} {} vs {} - {} ({} open)".format(
            fixture["id"], fixture["home_team"], fixture["away_team"],
            slot.label, referees.ROLE_LABEL[role].capitalize(),
        )
        options.append(discord.SelectOption(label=label[:100], value=str(fixture["id"])))

    if not options:
        return [discord.SelectOption(label="Nothing needs a referee right now",
                                     value="none")], True
    return options, False


class ClaimSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"refclaim:(?P<week>[\w-]+)",
):
    def __init__(self, week, options, disabled=False):
        self.week = week
        super().__init__(
            discord.ui.Select(
                custom_id="refclaim:{}".format(week),
                placeholder="Claim a game...",
                options=options,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        week = match["week"]
        client = interaction.client
        pending = client.fixtures_needing_referee(week=week)
        options, disabled = claim_options(pending, client.slot, week)
        return cls(week, options, disabled)

    async def callback(self, interaction):
        chosen = self.item.values[0]
        if chosen == "none":
            await interaction.response.defer()
            return

        store = interaction.client.store
        fixture = store.fixture(int(chosen))
        if not fixture:
            await interaction.response.send_message(
                "That fixture no longer exists.", ephemeral=True
            )
            return
        try:
            role = referees.claim(store, fixture, interaction.user.id)
        except referees.ClaimError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        await interaction.response.send_message(
            "You're in as **{}** for **#{}** ({} vs {}). Drop out any time "
            "with `/ref dropout`.".format(
                referees.ROLE_LABEL[role], fixture["id"],
                fixture["home_team"], fixture["away_team"]),
            ephemeral=True,
        )
        # The board and its menu are edited by id, not through this response -
        # they're separate messages from whatever this select is attached to
        # once the board has grown past one chunk.
        await interaction.client.refresh_ref_board(self.week)


REF_DYNAMIC_ITEMS = (ClaimSelect,)
