"""Accept / decline buttons on a referee's assignment DM.

Same pattern as the availability selector: DynamicItems, so an offer sent
before a restart is still answerable afterwards, and the referee's identity
comes from the interaction rather than from the custom_id - so nobody can
accept a game on someone else's behalf by replaying a button.
"""

from __future__ import annotations

import discord

from . import notify, referees
from .db import OFFER_DECLINED, OFFER_OFFERED


def _offered_to(store, fixture_id, user_id):
    """Is this user the person currently being asked?"""
    with store._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM ref_offers WHERE fixture_id=? AND referee_id=? AND state=?",
            (fixture_id, user_id, OFFER_OFFERED),
        ).fetchone()
    return row is not None


class _OfferButton:
    """Shared checks. A plain mixin, not a DynamicItem subclass."""

    async def resolve(self, interaction):
        """Common checks. Returns (store, fixture, slot) or None."""
        store = interaction.client.store
        fixture = store.fixture(self.fixture_id)
        if not fixture:
            await interaction.response.send_message(
                "That fixture no longer exists.", ephemeral=True
            )
            return None
        if not _offered_to(store, self.fixture_id, interaction.user.id):
            # Either already answered, withdrawn, or never theirs to answer.
            await interaction.response.send_message(
                "This assignment isn't open for you any more — it may already "
                "have been answered or reassigned.",
                ephemeral=True,
            )
            return None
        slot = interaction.client.slot(fixture["slot_key"])
        return store, fixture, slot


class AcceptButton(
    _OfferButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ra:(?P<fixture>\d+)",
):
    def __init__(self, fixture_id):
        self.fixture_id = int(fixture_id)
        super().__init__(
            discord.ui.Button(
                label="Accept",
                style=discord.ButtonStyle.success,
                custom_id="ra:{}".format(fixture_id),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["fixture"])

    async def callback(self, interaction):
        resolved = await self.resolve(interaction)
        if not resolved:
            return
        store, fixture, slot = resolved

        fixture = referees.accept(store, self.fixture_id, interaction.user.id)
        await interaction.response.edit_message(
            content=notify.referee_confirmed(fixture, slot), view=None
        )
        # Managers were told a time already; this fills in the referee.
        for manager_id in (fixture["home_manager_id"], fixture["away_manager_id"]):
            await interaction.client.dm(
                manager_id,
                notify.fixture_confirmed(fixture, slot,
                                         referee_name=interaction.user.display_name),
            )


class DeclineButton(
    _OfferButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"rd:(?P<fixture>\d+)",
):
    def __init__(self, fixture_id):
        self.fixture_id = int(fixture_id)
        super().__init__(
            discord.ui.Button(
                label="Decline",
                style=discord.ButtonStyle.secondary,
                custom_id="rd:{}".format(fixture_id),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["fixture"])

    async def callback(self, interaction):
        resolved = await self.resolve(interaction)
        if not resolved:
            return
        store, fixture, slot = resolved

        store.resolve_offer(self.fixture_id, interaction.user.id, OFFER_DECLINED)
        await interaction.response.edit_message(
            content="No problem — we'll ask someone else for #{}.".format(self.fixture_id),
            view=None,
        )
        # Hand off to the client so the next offer goes out the same way the
        # first one did, including the unreachable-referee handling.
        await interaction.client.offer_referee(store.fixture(self.fixture_id), slot)


def offer_view(fixture_id):
    view = discord.ui.View(timeout=None)
    view.add_item(AcceptButton(fixture_id))
    view.add_item(DeclineButton(fixture_id))
    return view


REF_DYNAMIC_ITEMS = (AcceptButton, DeclineButton)
