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

from . import notify, season
from .selector import SelectorState, describe_choice

# --------------------------------------------------------------------------
# custom_id encoding
#
# Discord caps custom_id at 100 characters, so these stay terse. The owner is
# deliberately NOT encoded - it is read from the interaction, so one person
# cannot act on another's selector by replaying a custom_id.
#
#   av:fx:2026-09-19:sat_1800   slot button, manager's week of 2026-09-19
#   avd:fx:2026-09-19:1         switch to day index 1
#   avs:fx:2026-09-19           submit
#   avc:fx:2026-09-19           clear
#
# "ref" is a week key, not a fixture id - a manager answers once for every
# fixture they have that week, see Target below. The "fx" scope segment is a
# holdover from when referees also answered a weekly selector through this
# same machinery (scope "rw"). That path is gone - referees now claim games
# directly, see ref_views.py - but the segment stays in the id format so
# buttons already posted before this change keep routing.
# --------------------------------------------------------------------------

SCOPE_FIXTURE = "fx"

_REF = r"[\w.-]+"


class Target:
    """A manager's week: where a selector's answers are stored.

    One submission covers every fixture the manager has that week, not just
    one - someone managing more than one team marks their times once and it
    applies to all of them, instead of repeating the same picks per fixture.
    """

    def __init__(self, scope, ref):
        self.scope = scope
        self.ref = str(ref)   # the week key, e.g. "2026-09-19"

    def _my_fixtures(self, store, user_id, open_only=True):
        return [
            f for f in store.fixtures(week=self.ref)
            if user_id in (f["home_manager_id"], f["away_manager_id"])
            and (not open_only or not f["slot_key"])
        ]

    def load(self, store, user_id):
        record = store.weekly_submission(self.ref, user_id)
        if record:
            return record["slots"], bool(record["submitted"])
        # Nothing saved for this week yet - start from whatever they last
        # submitted instead of a blank selector. Never comes back as already
        # submitted: it's a starting point to confirm or adjust, not an
        # answer for this week until they press Submit again.
        previous = store.latest_weekly_submission(user_id, before_week=self.ref)
        if previous:
            return previous["slots"], False
        return {}, False

    def save(self, store, user_id, picks, submitted=False):
        store.save_weekly_submission(self.ref, user_id, picks, submitted=submitted)

    def mark_submitted(self, store, user_id):
        fixture_ids = [f["id"] for f in self._my_fixtures(store, user_id)]
        store.mark_weekly_submitted(self.ref, user_id, fixture_ids)

    def may_answer(self, store, user_id):
        """Only someone managing at least one fixture this week."""
        return bool(self._my_fixtures(store, user_id, open_only=False))

    def heading(self, store, user_id):
        def row(f):
            return "#{} {} vs {}".format(
                f["id"], season.label_for(f["home_team"]), season.label_for(f["away_team"]))

        fixtures = self._my_fixtures(store, user_id)
        if not fixtures:
            return "No fixtures"
        if len(fixtures) == 1:
            return row(fixtures[0])
        return "Your fixtures:\n" + "\n".join(row(f) for f in fixtures)

    def closed(self, store, user_id):
        """Why answering is no longer possible, or None."""
        if not self._my_fixtures(store, user_id, open_only=False):
            return "You have no fixtures this week."
        if not self._my_fixtures(store, user_id):
            return "All of your fixtures this week are already scheduled."
        return None

    def gameweek(self, store):
        """This week's gameweek key, for working out which slots are legal."""
        fixtures = store.fixtures(week=self.ref)
        return next((f["gameweek"] for f in fixtures if f["gameweek"]), None)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

LEGEND = "⚪ no  ·  🟡 fine  ·  🟢 ideal (click a time to cycle it)"


def _discord_embed(board_embed):
    """Turn a notify.BoardEmbed into the real discord.Embed the API wants.

    Duplicates main.py's helper of the same name rather than importing it -
    main.py imports this module, so the reverse import would cycle.
    """
    embed = discord.Embed(description=board_embed.description,
                          color=discord.Color(season.EMBED_COLOR))
    if board_embed.title:
        embed.title = board_embed.title
    if board_embed.footer:
        embed.set_footer(text=board_embed.footer)
    for name, value, inline in board_embed.fields:
        embed.add_field(name=name, value=value, inline=inline)
    return embed


def _resolve_active_day(state, active_day):
    days = state.days
    return active_day if active_day in days else (days[0] if days else None)


def build_message(store, target, user_id, state, active_day=None):
    """The embed shown above the buttons - one field per day, so the current
    picks read as a compact row of cards rather than a wall of plain text."""
    active_day = _resolve_active_day(state, active_day)
    description = "{}\n\nAll times are **GMT+0**. {}".format(
        target.heading(store, user_id), LEGEND)
    if state.submitted:
        description += "\n\n✅ Submitted. You can still change it until the deadline."
    embed = notify.BoardEmbed(
        title="Set your availability",
        description=description,
        fields=[(day, state.day_summary(day), True) for day in state.days],
        footer=("Saved automatically - reused as your starting point the "
                "next time you submit."),
    )
    return embed, active_day


def build_view(target, state, active_day=None):
    """A fresh View for the current state. Rebuilt on every click."""
    active_day = _resolve_active_day(state, active_day)
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
    embed, active_day = build_message(store, target, interaction.user.id, state, active_day)
    view, _ = build_view(target, state, active_day)
    await interaction.response.edit_message(embed=_discord_embed(embed), view=view)


async def open_selector(interaction, store, target, slots, ephemeral=True):
    """First render, in response to a command or a DM button."""
    saved, submitted = target.load(store, interaction.user.id)
    state = SelectorState(slots, saved=saved, submitted=submitted)
    embed, active_day = build_message(store, target, interaction.user.id, state)
    view, _ = build_view(target, state, active_day)
    await interaction.response.send_message(embed=_discord_embed(embed), view=view,
                                            ephemeral=ephemeral)


# --------------------------------------------------------------------------
# buttons
# --------------------------------------------------------------------------

class _SelectorButton:
    """Shared plumbing: identify the target, check permission, load state."""

    def context(self, interaction):
        # offerable_slots(), not the raw slot list - a click rebuilding the
        # view must never let a slot back in that the window or the season's
        # kickoff floor already ruled out, even if the underlying sheet has
        # more than that on offer.
        store = interaction.client.store
        slots = interaction.client.offerable_slots(self.target.gameweek(store))
        return store, slots

    async def guard(self, interaction, target):
        store, _ = self.context(interaction)
        if not target.may_answer(store, interaction.user.id):
            await interaction.response.send_message(
                "This selector isn't yours to fill in.", ephemeral=True
            )
            return False
        reason = target.closed(store, interaction.user.id)
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
    template=r"av:(?P<scope>fx):(?P<ref>[\w.-]+):(?P<slot>\w+)",
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
        store = interaction.client.store
        slots = interaction.client.offerable_slots(target.gameweek(store))
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
    template=r"avd:(?P<scope>fx):(?P<ref>[\w.-]+):(?P<day>\d+)",
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
        store = interaction.client.store
        slots = interaction.client.offerable_slots(target.gameweek(store))
        state = SelectorState(slots)
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
    template=r"avs:(?P<scope>fx):(?P<ref>[\w.-]+)",
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
        # Target is a manager's week now, covering every fixture of theirs in
        # it - so the scheduling pass runs for the whole week, not one fixture.
        hook = getattr(interaction.client, "on_availability_submitted", None)
        if hook:
            await hook(self.target.ref)


class ClearButton(
    _SelectorButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"avc:(?P<scope>fx):(?P<ref>[\w.-]+)",
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
    template=r"avo:(?P<scope>fx):(?P<ref>[\w.-]+)",
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


class MyAvailabilityButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"avme",
):
    """One public button that opens the clicker's own selector.

    Not tied to a fixture: it looks up whoever pressed it and finds their open
    game. That is the whole point - a DM only reaches managers who allow DMs,
    whereas a button in a channel reaches everyone, and each person still gets
    a private response.
    """

    def __init__(self):
        super().__init__(
            discord.ui.Button(
                label="Submit my timings",
                style=discord.ButtonStyle.success,
                emoji="📋",
                custom_id="avme",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction):
        bot = interaction.client
        store = bot.store
        week, fixtures = bot.my_open_week(interaction.user.id)

        if not fixtures:
            managed = [t for t, uid in store.managers().items()
                       if uid == interaction.user.id]
            if not managed:
                await interaction.response.send_message(
                    "You're not registered as a manager yet. Ask an Official "
                    "to add you with `/managers set`.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "Nothing to submit right now. Either your fixture already has "
                "a time, or the next gameweek isn't open yet.\n"
                "-# You manage: {}".format(", ".join(sorted(managed))),
                ephemeral=True,
            )
            return

        # One selector for the whole week - it covers every fixture just
        # found above, even if that's more than one team's game.
        await open_selector(interaction, store, Target(SCOPE_FIXTURE, week),
                            bot.offerable_slots(fixtures[0]["gameweek"]))


def availability_button():
    view = discord.ui.View(timeout=None)
    view.add_item(MyAvailabilityButton())
    return view


DYNAMIC_ITEMS = DYNAMIC_ITEMS + (MyAvailabilityButton,)
