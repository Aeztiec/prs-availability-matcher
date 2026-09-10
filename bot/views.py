"""The Discord shell around selector.py.

Every button here does the same three things: load the saved state, hand the
click to selector.py, save the result. No decisions are made in this file - it
draws things and forwards clicks - so the logic stays testable in test_store.py
where no gateway connection is needed.

Buttons are DynamicItems, which means their behaviour is recovered from the
custom_id rather than from a View object held in memory. A manager's selector
therefore keeps working after the bot restarts, which matters when the thing
runs on a small free VM and the deadline is hours away.
"""

from __future__ import annotations

import re

import discord

from .selector import SelectorState, describe_choice

# --------------------------------------------------------------------------
# custom_id encoding
#
# Discord caps custom_id at 100 characters, so these stay terse. The owner is
# deliberately NOT encoded - it is read from the interaction, so one person
# cannot act on another's selector by replaying a custom_id.
#
#   av:fx:1024:sat_1800      slot button, manager on fixture 1024
#   av:rw:2026-09-12:sat_1800   slot button, referee for that week
#   avd:fx:1024:1            switch to day index 1
#   avs:fx:1024              submit
#   avc:fx:1024              clear
# --------------------------------------------------------------------------

SCOPE_FIXTURE = "fx"
SCOPE_REF_WEEK = "rw"

_REF = r"[\w.-]+"


class Target:
    """Where a selector's answers are stored.

    Managers answer per fixture; referees answer per week. Both use the same
    buttons, so the difference is isolated here.
    """

    def __init__(self, scope, ref):
        self.scope = scope
        self.ref = str(ref)

    @property
    def is_fixture(self):
        return self.scope == SCOPE_FIXTURE

    def load(self, store, user_id):
        if self.is_fixture:
            record = store.submission(int(self.ref), user_id)
        else:
            record = store.ref_availability(user_id, self.ref)
        if not record:
            return {}, False
        return record["slots"], bool(record["submitted"])

    def save(self, store, user_id, picks, submitted=False):
        if self.is_fixture:
            store.save_submission(int(self.ref), user_id, picks, submitted=submitted)
        else:
            store.save_ref_availability(user_id, self.ref, picks, submitted=submitted)

    def mark_submitted(self, store, user_id):
        if self.is_fixture:
            store.mark_submitted(int(self.ref), user_id)
        else:
            store.mark_ref_submitted(user_id, self.ref)

    def may_answer(self, store, user_id):
        """Only the two managers of a fixture, or a registered referee."""
        if self.is_fixture:
            fixture = store.fixture(int(self.ref))
            if not fixture:
                return False
            return user_id in (fixture["home_manager_id"], fixture["away_manager_id"])
        return any(r["discord_id"] == user_id for r in store.referees())

    def heading(self, store):
        if self.is_fixture:
            fixture = store.fixture(int(self.ref))
            if not fixture:
                return "Fixture not found"
            return "#{} · {} vs {}".format(
                fixture["id"], fixture["home_team"], fixture["away_team"]
            )
        return "Referee availability · week of {}".format(self.ref)

    def closed(self, store):
        """Why answering is no longer possible, or None."""
        if not self.is_fixture:
            return None
        fixture = store.fixture(int(self.ref))
        if not fixture:
            return "That fixture no longer exists."
        if fixture["slot_key"]:
            return "This fixture is already scheduled for {}.".format(fixture["slot_key"])
        return None


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

LEGEND = "⚪ no  ·  🟡 fine  ·  🟢 ideal — click a time to cycle it"


def build_message(store, target, state, active_day=None):
    """The text shown above the buttons."""
    days = state.days
    active_day = active_day if active_day in days else (days[0] if days else None)
    lines = [
        "**{}**".format(target.heading(store)),
        "",
        "All times are **GMT+0**. {}".format(LEGEND),
        "",
    ]
    lines += state.summary_lines()
    if state.submitted:
        lines += ["", "✅ Submitted — you can still change it until the deadline."]
    return "\n".join(lines), active_day


def build_view(store, target, state, active_day=None):
    """A fresh View for the current state. Rebuilt on every click."""
    _, active_day = build_message(store, target, state, active_day)
    view = discord.ui.View(timeout=None)

    days = state.days
    if len(days) > 1:
        for index, day in enumerate(days):
            view.add_item(DayButton(
                target, index, day, active=(day == active_day), row=0,
            ))

    slots = state.slots_for(active_day) if active_day else []
    for position, slot in enumerate(slots):
        view.add_item(SlotButton(
            target, slot, state.get(slot.key), row=1 + position // 5,
        ))

    last_row = 1 + (max(len(slots) - 1, 0)) // 5 + 1
    view.add_item(SubmitButton(target, row=min(last_row, 4)))
    view.add_item(ClearButton(target, row=min(last_row, 4)))
    return view, active_day


async def refresh(interaction, store, target, state, active_day=None):
    content, active_day = build_message(store, target, state, active_day)
    view, _ = build_view(store, target, state, active_day)
    await interaction.response.edit_message(content=content, view=view)


async def open_selector(interaction, store, target, slots, ephemeral=True):
    """First render, in response to a command or a DM button."""
    saved, submitted = target.load(store, interaction.user.id)
    state = SelectorState(slots, saved=saved, submitted=submitted)
    content, active_day = build_message(store, target, state)
    view, _ = build_view(store, target, state, active_day)
    await interaction.response.send_message(content=content, view=view, ephemeral=ephemeral)


# --------------------------------------------------------------------------
# buttons
# --------------------------------------------------------------------------

class _SelectorButton:
    """Shared plumbing: identify the target, check permission, load state."""

    def context(self, interaction):
        store = interaction.client.store
        slots = interaction.client.slots
        return store, slots

    async def guard(self, interaction, target):
        store, _ = self.context(interaction)
        if not target.may_answer(store, interaction.user.id):
            await interaction.response.send_message(
                "This selector isn't yours to fill in.", ephemeral=True
            )
            return False
        reason = target.closed(store)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return False
        return True

    def state_for(self, interaction, target, slots):
        store, _ = self.context(interaction)
        saved, submitted = target.load(store, interaction.user.id)
        return SelectorState(slots, saved=saved, submitted=submitted)


class SlotButton(
    _SelectorButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"av:(?P<scope>fx|rw):(?P<ref>[\w.-]+):(?P<slot>\w+)",
):
    def __init__(self, target, slot, pref, row=1):
        self.target = target
        self.slot = slot
        super().__init__(
            discord.ui.Button(
                label="{} {}".format(pref.emoji, slot.label),
                style=discord.ButtonStyle.secondary,
                custom_id="av:{}:{}:{}".format(target.scope, target.ref, slot.key),
                row=row,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        target = Target(match["scope"], match["ref"])
        slots = interaction.client.slots
        slot = next((s for s in slots if s.key == match["slot"]), None)
        if slot is None:
            raise ValueError("unknown slot {}".format(match["slot"]))
        from .scheduling import Pref
        return cls(target, slot, Pref.NO)

    async def callback(self, interaction):
        if not await self.guard(interaction, self.target):
            return
        store, slots = self.context(interaction)
        state = self.state_for(interaction, self.target, slots)
        state.cycle(self.slot.key)
        self.target.save(store, interaction.user.id, state.as_dict())
        await refresh(interaction, store, self.target, state, self.slot.day)


class DayButton(
    _SelectorButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"avd:(?P<scope>fx|rw):(?P<ref>[\w.-]+):(?P<day>\d+)",
):
    def __init__(self, target, index, day, active=False, row=0):
        self.target = target
        self.index = index
        self.day = day
        super().__init__(
            discord.ui.Button(
                label=day,
                style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
                custom_id="avd:{}:{}:{}".format(target.scope, target.ref, index),
                row=row,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        target = Target(match["scope"], match["ref"])
        index = int(match["day"])
        state = SelectorState(interaction.client.slots)
        days = state.days
        day = days[index] if index < len(days) else (days[0] if days else "")
        return cls(target, index, day)

    async def callback(self, interaction):
        if not await self.guard(interaction, self.target):
            return
        store, slots = self.context(interaction)
        state = self.state_for(interaction, self.target, slots)
        await refresh(interaction, store, self.target, state, self.day)


class SubmitButton(
    _SelectorButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"avs:(?P<scope>fx|rw):(?P<ref>[\w.-]+)",
):
    def __init__(self, target, row=4):
        self.target = target
        super().__init__(
            discord.ui.Button(
                label="Submit availability",
                style=discord.ButtonStyle.success,
                custom_id="avs:{}:{}".format(target.scope, target.ref),
                row=row,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(Target(match["scope"], match["ref"]))

    async def callback(self, interaction):
        if not await self.guard(interaction, self.target):
            return
        store, slots = self.context(interaction)
        state = self.state_for(interaction, self.target, slots)

        problem = state.blocking_problem()
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        self.target.save(store, interaction.user.id, state.as_dict(), submitted=True)
        self.target.mark_submitted(store, interaction.user.id)
        state.submitted = True

        await refresh(interaction, store, self.target, state)
        await interaction.followup.send(
            "Saved: {}\n\nYou can change this any time before the deadline.".format(
                describe_choice(state)
            ),
            ephemeral=True,
        )
        hook = getattr(interaction.client, "on_availability_submitted", None)
        if hook and self.target.is_fixture:
            await hook(int(self.target.ref))


class ClearButton(
    _SelectorButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"avc:(?P<scope>fx|rw):(?P<ref>[\w.-]+)",
):
    def __init__(self, target, row=4):
        self.target = target
        super().__init__(
            discord.ui.Button(
                label="Clear all",
                style=discord.ButtonStyle.danger,
                custom_id="avc:{}:{}".format(target.scope, target.ref),
                row=row,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(Target(match["scope"], match["ref"]))

    async def callback(self, interaction):
        if not await self.guard(interaction, self.target):
            return
        store, slots = self.context(interaction)
        state = self.state_for(interaction, self.target, slots)
        state.clear()
        self.target.save(store, interaction.user.id, state.as_dict())
        await refresh(interaction, store, self.target, state)


DYNAMIC_ITEMS = (SlotButton, DayButton, SubmitButton, ClearButton)


class OpenButton(
    _SelectorButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"avo:(?P<scope>fx|rw):(?P<ref>[\w.-]+)",
):
    """Opens the selector from a DM.

    Managers get this on the fixture DM so they never have to find a slash
    command. It matters for a second reason: commands synced to a guild are not
    available in DMs, and a fresh global sync can take a while to propagate -
    this button works the moment the DM lands.
    """

    def __init__(self, target, label="Set availability"):
        self.target = target
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id="avo:{}:{}".format(target.scope, target.ref),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(Target(match["scope"], match["ref"]))

    async def callback(self, interaction):
        if not await self.guard(interaction, self.target):
            return
        store, slots = self.context(interaction)
        await open_selector(interaction, store, self.target, slots)


def opener(target, label="Set availability"):
    """A one-button View to attach to a DM."""
    view = discord.ui.View(timeout=None)
    view.add_item(OpenButton(target, label))
    return view


DYNAMIC_ITEMS = DYNAMIC_ITEMS + (OpenButton,)
