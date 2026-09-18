"""The Discord bot: slash commands and the deadline runner.

Thin by design. Commands validate input, call into orchestrator/db, and report;
the deciding happens in modules that are tested without a gateway.

    python -m bot.main              run the bot
    python -m bot.main --check      load everything and exit, without connecting
    python -m bot.main --fast-sync  register commands in the guild too, so new
                                    ones appear immediately instead of waiting
                                    on global propagation. Shows every command
                                    twice; a normal restart clears that.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys

import discord
from discord import app_commands
from discord.ext import tasks

from . import config, notify, referees, results, season
from .db import Store
from .ref_views import (
    REF_DYNAMIC_ITEMS, ClaimSelect, claim_options, game_name, matchup, staff_game_name,
)
from .text import plural
from .fallback import Timings
from .players import Players
from .orchestrator import Action, dashboard, run_once, run_once_randomly
from .scheduling import Status
from .selector import fits_on_one_message, SelectorState
from .slots import within_kickoff_window
from .views import (
    DYNAMIC_ITEMS, SCOPE_FIXTURE, Target, availability_button,
    opener, open_selector,
)
from .weeks import (
    from_iso, parse_deadline, slot_datetime, to_iso, uk_time, utcnow, week_of,
)

log = logging.getLogger("prsbot")

MAX_TIER = 5   # referee tiers run 1..MAX_TIER; they will limit which games a referee can take


def _discord_embed(board_embed):
    """Turn a notify.BoardEmbed - plain, testable data - into the real
    discord.Embed object the API actually wants. Kept to this one spot so
    nothing else in this module needs to know discord.Embed's shape."""
    embed = discord.Embed(description=notify.widen(board_embed.description),
                          color=discord.Color(season.EMBED_COLOR))
    if board_embed.title:
        embed.title = board_embed.title
    if board_embed.footer:
        embed.set_footer(text=board_embed.footer)
    for name, value, inline in board_embed.fields:
        embed.add_field(name=name, value=value, inline=inline)
    return embed

# How often the deadline runner wakes. Deadlines are hours away, so a few
# minutes of lag is irrelevant - and a short interval keeps each pass tiny.
TICK_MINUTES = 5


class PRSBot(discord.Client):
    def __init__(self, fast_sync=False):
        # Testing only. Global commands can take up to an hour to reach a
        # client; a guild sync is instant. It makes every command appear twice
        # in this server until a normal restart, which clears the guild copies
        # again - so it is opt-in and self-undoing rather than the default.
        self.fast_sync = fast_sync
        # Only `guilds`, which is not privileged. Message content and members
        # are never needed - everything is slash commands and buttons - but
        # without `guilds` the role objects behind a STAFF_ROLE_ID check cannot
        # be resolved, and the check would silently fail open or closed.
        super().__init__(intents=discord.Intents(guilds=True))
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self.on_tree_error
        self.store = Store()
        self.timings = None
        self.slots = []

    # ------------------------------------------------------------- lifecycle
    async def setup_hook(self):
        self.load_timings()
        for item in DYNAMIC_ITEMS + REF_DYNAMIC_ITEMS:
            self.add_dynamic_items(item)
        register(self)
        # Global only. Guild-scoped commands do not exist in DMs, and
        # /availability has to work where the manager is being messaged.
        # Registering both scopes makes every command appear twice in the
        # server's picker, so guild copies are actively cleared below.
        await self.tree.sync()
        log.info("commands synced globally")
        if self.fast_sync and config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.warning("--fast-sync: also synced to guild %s. Commands will "
                        "appear TWICE here until you restart without the flag.",
                        config.GUILD_ID)
        else:
            await self.clear_guild_commands()
        self.runner.start()

    async def clear_guild_commands(self):
        """Remove guild-scoped duplicates of the global commands.

        Only acts if there are any, so a normal start costs one cheap read.
        Self-healing: whatever left stale copies behind, they go on next boot.
        """
        if not config.GUILD_ID:
            return
        guild = discord.Object(id=config.GUILD_ID)
        try:
            existing = await self.tree.fetch_commands(guild=guild)
        except discord.HTTPException as error:
            log.warning("could not read guild commands: %s", error)
            return
        if not existing:
            return
        self.tree.clear_commands(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("removed %d duplicate guild command(s): %s", len(existing),
                 ", ".join(sorted(c.name for c in existing)))

    def load_timings(self):
        """(Re)read the timings sheet and rebuild the slot vocabulary."""
        self.timings = Timings(config.COMPETITION)
        self.slots = self.timings.slots
        if not fits_on_one_message(SelectorState(self.slots)):
            # Caught here rather than when a manager opens a broken selector:
            # over the component budget, Discord rejects the message outright.
            raise RuntimeError(
                "a day has more slots than Discord will show at once; raise "
                "slots.GRANULARITY_MINUTES"
            )
        log.info("%s: %d slots", self.timings.title, len(self.slots))

    async def on_ready(self):
        log.info("logged in as %s (%s)", self.user, self.user.id)

    async def on_tree_error(self, interaction, error):
        """Keep expected refusals out of the log, and never leave a command
        silently unanswered."""
        if isinstance(error, app_commands.CheckFailure):
            # staff_only() has already explained itself to the user.
            log.info("refused %s for %s",
                     interaction.command.qualified_name if interaction.command else "?",
                     interaction.user)
            return
        log.exception("command %s failed",
                      interaction.command.qualified_name if interaction.command else "?")
        message = ("Something went wrong running that. It's been logged - "
                   "tell staff what you were doing.")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------ messaging
    async def channel_for(self, week, prefer=None):
        """Where to post about a week: the given channel, or the board's.

        Nothing is sent by DM. A large share of managers have "direct messages
        from server members" switched off, and a DM to them is never delivered
        and never bounces - so it fails silently, which is the worst way for a
        deadline notice to fail.
        """
        if prefer is not None:
            return prefer
        record = self.store.board(week)
        if not record:
            return None
        try:
            return self.get_channel(record["channel_id"]) or \
                await self.fetch_channel(record["channel_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            log.warning("board channel for %s unreachable: %s", week, error)
            return None

    async def post(self, channel, content=None, view=None, mention_users=True, embed=None):
        """Post to a channel, allowing user and role pings but never @everyone."""
        if channel is None:
            return None
        allowed = discord.AllowedMentions(
            everyone=False, roles=True, users=mention_users
        )
        try:
            return await channel.send(content=content, embed=embed, view=view,
                                      allowed_mentions=allowed)
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning("could not post in %s: %s", getattr(channel, "id", "?"), error)
            return None

    async def referee_channel(self, week):
        if config.REF_CHANNEL_ID:
            try:
                return self.get_channel(config.REF_CHANNEL_ID) or \
                    await self.fetch_channel(config.REF_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                log.warning("referee channel unreachable: %s", error)
        return await self.channel_for(week)

    async def on_availability_submitted(self, week):
        """Called by the submit button with the week just submitted for.

        A submission now covers every fixture the manager has that week, so
        there's no single fixture to check "both submitted" against -
        advance() already skips anything still missing a manager, so simply
        running the week's pass is cheap and correct even when most of it
        does nothing yet.
        """
        await self.process(week=week)

    # -------------------------------------------------------------- runner
    async def process(self, week=None):
        """One scheduling pass, then say what happened."""
        for outcome in run_once(self.store, self.timings, week=week):
            fixture = self.store.fixture(outcome.fixture_id)
            if outcome.action == Action.SCHEDULED:
                # No confirmation message is needed: the announcement the
                # managers already read updates itself at the end of this pass.
                pass
            elif outcome.action == Action.REMIND:
                channel = await self.channel_for(fixture["week"])
                for which in outcome.reminders:
                    content, embed = notify.reminder(
                        fixture, fixture["deadline"], which, managers=outcome.notify)
                    await self.post(channel, content, embed=_discord_embed(embed))
                    self.store.mark_reminded(fixture["id"], which)
            elif outcome.action == Action.NO_VALID_TIME:
                log.warning("fixture %s needs manual scheduling: %s",
                            fixture["id"], outcome.detail)

        # Keep any published fixture board and referee board in step with
        # whatever just changed - a newly scheduled kickoff, or a claim.
        for record in ([self.store.board(week)] if week else self.store.boards()):
            if record:
                await self.refresh_board(record["week"])
        for record in ([self.store.ref_board(week)] if week else self.store.ref_boards()):
            if record:
                await self.refresh_ref_board(record["week"])

    def slot(self, key):
        return next((s for s in self.slots if s.key == key), None)

    def current_week(self):
        """The week key commands default to: the current gameweek's weekend.

        Not the next calendar Saturday. Those differ whenever a gameweek is
        more than a few days out - today's calendar week is 2026-09-12 while
        GW1 plays the weekend of 2026-09-19 - and defaulting to the calendar
        week made /fixture list and /refs board quietly look at a weekend
        with nothing in it.
        """
        gw = season.current(utcnow())
        return gw.week if gw else week_of(utcnow())

    def fixtures_needing_referee(self, week=None):
        """(fixture, roster) pairs for scheduled fixtures not yet fully staffed."""
        fixtures = self.store.fixtures(week=week) if week else self.store.fixtures()
        pending = []
        for fixture in fixtures:
            if not fixture["slot_key"] or fixture["status"] == Status.NEEDS_MANUAL_SCHEDULING:
                continue
            roster = self.store.fixture_referees(fixture["id"])
            if referees.next_open_role([r["role"] for r in roster]) is not None:
                pending.append((fixture, roster))
        return pending

    # ----------------------------------------------------------- referee board
    def _claim_view(self, week, pending=None):
        if pending is None:
            pending = self.fixtures_needing_referee(week=week)
        options, disabled = claim_options(pending, self.slot, week)
        view = discord.ui.View(timeout=None)
        view.add_item(ClaimSelect(week, options, disabled))
        return view

    def ref_board_payload(self, week):
        """The board embeds, the claim-prompt embed, its view, and a digest
        of all of it together - so publish and refresh always agree on
        whether anything actually changed."""
        fixtures = self.store.fixtures(week=week)
        rosters = {f["id"]: self.store.fixture_referees(f["id"]) for f in fixtures}
        gw = next((g for g in season.ALL if g.week == week), None)
        bodies = notify.referee_board(
            fixtures, week, self.slot, rosters=rosters, gameweek=gw,
            competition=self.timings.competition.competition,
        )
        pending = self.fixtures_needing_referee(week=week)
        prompt = notify.referee_claim_prompt(len(pending))
        view = self._claim_view(week, pending)
        digest = notify.board_digest(bodies + [prompt])
        return bodies, prompt, view, digest

    async def publish_ref_board(self, week, channel):
        """Post the referee board (one message, more only if a genuinely
        huge week needs it), and the claim menu embed under it."""
        bodies, prompt, view, digest = self.ref_board_payload(week)
        # Spoilered so it still notifies the role without shouting at the top
        # of the post - same idea the old text board used.
        mention = ("<@&{}>".format(config.REFEREE_ROLE_ID)
                  if config.REFEREE_ROLE_ID else None)
        content = "||{}||".format(mention) if mention else None
        pinging = discord.AllowedMentions(everyone=False, roles=True, users=False)
        quiet = discord.AllowedMentions.none()
        groups = notify.group_embeds_for_messages(bodies)
        sent = []
        for index, group in enumerate(groups):
            sent.append(await channel.send(
                content=content if index == 0 else None,
                embeds=[_discord_embed(b) for b in group],
                allowed_mentions=pinging if index == 0 else quiet,
            ))
        sent.append(await channel.send(
            embed=_discord_embed(prompt), view=view, allowed_mentions=quiet
        ))
        self.store.set_ref_board(week, channel.id, [m.id for m in sent], digest)
        return sent[0]

    async def refresh_ref_board(self, week):
        """Edit the published referee board and its claim menu in place.

        Same shape as refresh_board, plus one more message: the claim prompt,
        which is re-edited every time because its options change with the
        roster, not just its text.
        """
        record = self.store.ref_board(week)
        if not record:
            return False
        bodies, prompt, view, digest = self.ref_board_payload(week)
        if digest == record.get("digest"):
            return False
        known = self.store.ref_board_message_ids(week)
        known_board, known_prompt = (known[:-1], known[-1]) if known else ([], None)
        try:
            channel = self.get_channel(record["channel_id"]) or \
                await self.fetch_channel(record["channel_id"])
            quiet = discord.AllowedMentions.none()
            groups = notify.group_embeds_for_messages(bodies)
            live = []
            for index, group in enumerate(groups):
                embeds = [_discord_embed(b) for b in group]
                if index < len(known_board):
                    message = await channel.fetch_message(known_board[index])
                    await message.edit(embeds=embeds, allowed_mentions=quiet)
                    live.append(message.id)
                else:
                    live.append((await channel.send(embeds=embeds, allowed_mentions=quiet)).id)

            for stale in known_board[len(groups):]:
                try:
                    await (await channel.fetch_message(stale)).delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

            prompt_embed = _discord_embed(prompt)
            if known_prompt:
                prompt_message = await channel.fetch_message(known_prompt)
                await prompt_message.edit(embed=prompt_embed, view=view)
                live.append(prompt_message.id)
            else:
                live.append((await channel.send(
                    embed=prompt_embed, view=view, allowed_mentions=quiet
                )).id)
        except (discord.NotFound, discord.Forbidden) as error:
            # Genuinely gone: nothing to edit, so forget it rather than
            # retrying forever against a message that no longer exists.
            log.warning("referee board for %s unreachable (%s); forgetting it", week, error)
            self.store.forget_ref_board(week)
            return False
        except discord.HTTPException as error:
            # A transient API failure (rate limit, a hiccup on Discord's
            # side) is not the same as "gone" - the board is still there,
            # so leave it tracked and let the next pass try again instead
            # of abandoning it over what might just be bad timing.
            log.warning("referee board for %s failed to update (%s); will retry next pass",
                       week, error)
            return False

        self.store.set_ref_board(week, record["channel_id"], live, digest)
        return True

    # -------------------------------------------------------------- gameweeks
    def open_gameweeks(self, now=None):
        """Gameweeks a manager may set availability for.

        Always the current one, plus any staff have opened early. A manager
        cannot sensibly say in September whether their squad is free in
        December, so the season is not opened all at once.
        """
        now = now or utcnow()
        here = season.current(now)
        keys = self.store.opened_gameweeks()
        if here:
            keys = keys | {here.key}
        return [gw for gw in season.ALL if gw.key in keys]

    def my_open_fixtures(self, user_id, now=None):
        """This manager's unscheduled fixtures in currently open gameweeks."""
        allowed = {gw.key for gw in self.open_gameweeks(now)}
        return [
            f for f in self.store.fixtures()
            if user_id in (f["home_manager_id"], f["away_manager_id"])
            and not f["slot_key"]
            and (f["gameweek"] is None or f["gameweek"] in allowed)
        ]

    def my_open_week(self, user_id, now=None):
        """The single week to open a manager's selector for, and every open
        fixture of theirs in it.

        A manager normally has exactly one open fixture, in one week. Someone
        managing more than one team can have several at once, all sharing a
        week - one selector covers all of them. Two gameweeks opened early is
        the only way to land in two different weeks simultaneously; that's
        rare enough that the simplest thing is to surface whichever is due
        soonest and let them come back for the other closer to its deadline.
        """
        mine = self.my_open_fixtures(user_id, now)
        if not mine:
            return None, []
        week = min(mine, key=lambda f: f["deadline"])["week"]
        return week, [f for f in mine if f["week"] == week]

    def offerable_slots(self, gameweek_key=None):
        """Slots legal for a gameweek: within the daily kickoff window, and
        honouring the season's kickoff floor.

        The window keeps a manager from marking themselves free at a time the
        league doesn't play; the floor keeps an early-opened gameweek from
        offering a time before the season has even started. /fixture set is
        the only path that can put a fixture anywhere the sheet knows about -
        this is what everything automatic is limited to.
        """
        slots = [slot for slot in self.slots if within_kickoff_window(slot)]
        if not gameweek_key:
            return slots
        try:
            gw = season.gameweek(gameweek_key)
        except LookupError:
            return slots
        return [
            slot for slot in slots
            if slot_datetime(gw.week, slot) >= season.KICKOFF_FLOOR
        ]

    async def create_gameweek_fixtures(self, gw, manager_of, opened_by=None, limit=None):
        """Create every fixture in a gameweek - domestic and UEFA both - and
        DM both managers.

        Idempotent: a pairing that already exists is skipped, so running this
        twice does not duplicate fixtures or spam twenty people again. A team
        with a UEFA fixture that week also has a domestic one, same as
        real life - the weekly submission covers both without asking twice.
        """
        made, skipped = [], []
        all_fixtures = gw.fixtures + gw.uefa_fixtures
        wanted = all_fixtures[:limit] if limit else all_fixtures
        for home_code, away_code, league in wanted:
            home = season.team_name(home_code)
            away = season.team_name(away_code)
            if self.store.fixture_for(gw.key, home, away, league=league):
                skipped.append("{} v {}".format(home_code, away_code))
                continue
            home_id = manager_of.get(home)
            away_id = manager_of.get(away)
            if not home_id or not away_id:
                skipped.append("{} v {} (no Discord id for a manager)".format(
                    home_code, away_code))
                continue
            fixture_id = self.store.create_fixture(
                self.timings.competition.key, gw.week, home, away,
                home_id, away_id, to_iso(gw.deadline),
                Status.WAITING_FOR_AVAILABILITY, gameweek=gw.key, league=league,
            )
            self.store.note(fixture_id, "gameweek opened",
                            "{} ({}) by {}".format(gw.key, league, opened_by))
            made.append(fixture_id)
        # Managers are told through the announcement and its button, posted by
        # the caller - not individually.
        return made, skipped

    # ---------------------------------------------------------- fixture board
    def board_bodies(self, week):
        fixtures = self.store.fixtures(week=week)
        gw = next((g for g in season.ALL if g.week == week), None)
        return notify.fixture_board(
            fixtures, week, self.slot,
            gameweek=gw, deadline=gw.deadline if gw else None,
            competition=self.timings.competition.competition,
        )

    async def publish_board(self, week, channel, with_button=True):
        """Post the fixture announcement (one message, one embed - more only
        if a huge gameweek genuinely needs it), and the button under it.

        The button is a separate message so the announcement can be edited
        freely without Discord dropping the components, and so the call to
        action stays at the bottom of the channel where people will see it.
        """
        bodies = self.board_bodies(week)
        # TEMP: @everyone ping disabled. Re-enable by swapping this back:
        # mention = config.ANNOUNCE_MENTION or None
        mention = None
        content = "||{}||".format(mention) if mention else None
        pinging = discord.AllowedMentions(everyone=True, roles=True, users=False)
        quiet = discord.AllowedMentions.none()
        groups = notify.group_embeds_for_messages(bodies)
        sent = []
        for index, group in enumerate(groups):
            sent.append(await channel.send(
                content=content if index == 0 else None,
                embeds=[_discord_embed(b) for b in group],
                allowed_mentions=pinging if index == 0 else quiet,
            ))
        self.store.set_board(week, channel.id, [m.id for m in sent],
                             notify.board_digest(bodies))

        if with_button:
            gw = next((g for g in season.ALL if g.week == week), None)
            if gw:
                await channel.send(
                    embed=_discord_embed(notify.availability_call_to_action(gw, gw.deadline)),
                    view=availability_button(),
                )
        return sent[0]

    async def refresh_board(self, week):
        """Edit the published board in place, if anything actually changed.

        Skipping unchanged edits matters: the runner wakes every few minutes and
        would otherwise rewrite the same message all day.
        """
        record = self.store.board(week)
        if not record:
            return False
        bodies = self.board_bodies(week)
        digest = notify.board_digest(bodies)
        if digest == record.get("digest"):
            return False
        known = self.store.board_message_ids(week)
        try:
            channel = self.get_channel(record["channel_id"]) or \
                await self.fetch_channel(record["channel_id"])

            # Mentions are suppressed on every edit: the initial post in
            # publish_board is the only thing allowed to ping.
            quiet = discord.AllowedMentions.none()
            groups = notify.group_embeds_for_messages(bodies)
            live = []
            for index, group in enumerate(groups):
                embeds = [_discord_embed(b) for b in group]
                if index < len(known):
                    message = await channel.fetch_message(known[index])
                    await message.edit(embeds=embeds, allowed_mentions=quiet)
                    live.append(message.id)
                else:
                    live.append((await channel.send(embeds=embeds, allowed_mentions=quiet)).id)

            for stale in known[len(groups):]:
                try:
                    await (await channel.fetch_message(stale)).delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        except (discord.NotFound, discord.Forbidden) as error:
            # Genuinely gone: nothing to edit, so forget it rather than
            # retrying forever against a message that no longer exists.
            log.warning("board for %s unreachable (%s); forgetting it", week, error)
            self.store.forget_board(week)
            return False
        except discord.HTTPException as error:
            # A transient API failure (rate limit, a hiccup on Discord's
            # side) is not the same as "gone" - the board is still there,
            # so leave it tracked and let the next pass try again instead
            # of abandoning it over what might just be bad timing.
            log.warning("board for %s failed to update (%s); will retry next pass",
                       week, error)
            return False

        self.store.set_board(week, record["channel_id"], live, digest)
        return True

    def dashboard_context(self, week):
        """The extra detail step 16 needs: who and why, not just how many."""
        fixtures = self.store.fixtures(week=week)
        waiting_on, reasons, rosters = {}, {}, {}
        for fixture in fixtures:
            fixture_id = fixture["id"]
            waiting_on[fixture_id] = self.store.unsubmitted_managers(fixture_id)
            rosters[fixture_id] = self.store.fixture_referees(fixture_id)
            for entry in reversed(self.store.history(fixture_id)):
                if entry["event"].startswith("status -> NEEDS_MANUAL") and entry["detail"]:
                    reasons[fixture_id] = entry["detail"]
                    break
        return waiting_on, reasons, rosters

    async def show_dashboard(self, interaction, week, followup=False):
        buckets = dashboard(self.store, week=week)
        waiting_on, reasons, rosters = self.dashboard_context(week)
        embed = notify.dashboard_summary(buckets, week, waiting_on, reasons, rosters,
                                         slot_for=self.slot)
        sender = interaction.followup.send if followup else \
            interaction.response.send_message
        await sender(embed=_discord_embed(embed), ephemeral=True)

    @tasks.loop(minutes=TICK_MINUTES)
    async def runner(self):
        try:
            await self.process()
        except Exception:                      # a bad pass must not kill the loop
            log.exception("scheduling pass failed")

    @runner.before_loop
    async def before_runner(self):
        await self.wait_until_ready()


# --------------------------------------------------------------------------
# permissions
# --------------------------------------------------------------------------

def is_staff(interaction):
    """Staff role if configured, otherwise anyone who can manage the server."""
    if interaction.guild is None:
        # Commands are registered globally so /availability works in DMs, which
        # means the staff ones are offered there too. There is no role or
        # permission to check in a DM, so they simply do not apply.
        return False
    if interaction.guild.owner_id == interaction.user.id:
        return True
    if config.STAFF_ROLE_ID:
        role_ids = {r.id for r in getattr(interaction.user, "roles", []) or []}
        return config.STAFF_ROLE_ID in role_ids
    try:
        perms = interaction.permissions
        return bool(perms.manage_guild or perms.administrator)
    except AttributeError:
        return False


def staff_refusal(interaction):
    """Why a staff command was refused, phrased so it can be acted on."""
    if interaction.guild is None:
        return ("Staff commands only work in the server, not in DMs. Discord "
                "gives me no roles or permissions to check here.\n"
                "-# `/availability` and `/ref dropout` do work in DMs.")
    if config.STAFF_ROLE_ID:
        return ("That's a staff command. It needs the configured staff role "
                "(`DISCORD_STAFF_ROLE_ID`).")
    return ("That's a staff command. It needs **Manage Server**, or set "
            "`DISCORD_STAFF_ROLE_ID` in .env to gate by role instead.")


def staff_only():
    async def predicate(interaction):
        if is_staff(interaction):
            return True
        await interaction.response.send_message(
            staff_refusal(interaction), ephemeral=True
        )
        return False
    return app_commands.check(predicate)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def register(bot):
    tree = bot.tree
    store = bot.store

    # Split by who uses them: /ref is what a referee runs on themselves,
    # /refs is staff administering referees as a group - easy to tell apart
    # in the picker, and the descriptions say so too.
    fixture_group = app_commands.Group(name="fixture", description="Staff: fixture scheduling")
    refs_group = app_commands.Group(name="refs", description="Staff: manage referees")
    ref_group = app_commands.Group(name="ref", description="For referees: your own assignments")
    # Everything here fakes real activity so a handful of people can exercise
    # the whole flow without forty real managers and several real referees.
    # Kept in its own group rather than scattered flags, so it's never a
    # question of whether a command is "the real one" or "the test one".
    test_group = app_commands.Group(name="test", description="Staff: fake activity for testing")

    # ------------------------------------------------------------- managers
    @tree.command(description="Set your availability for the week")
    async def availability(interaction):
        week, mine = bot.my_open_week(interaction.user.id)
        if not mine:
            open_now = ", ".join(gw.key for gw in bot.open_gameweeks()) or "none"
            await interaction.response.send_message(
                "You have no fixtures waiting on availability.\n"
                "-# Open gameweeks: {}. Later ones open closer to the time, or "
                "when Officials unlock them.".format(open_now),
                ephemeral=True,
            )
            return
        # One selector for the whole week - it covers every fixture just
        # found above, even if that's more than one team's game.
        await open_selector(interaction, store, Target(SCOPE_FIXTURE, week),
                            bot.offerable_slots(mine[0]["gameweek"]))

    # ------------------------------------------------------------- fixtures
    @fixture_group.command(name="list", description="Scheduling status for a week")
    @app_commands.describe(week="Saturday of the weekend, YYYY-MM-DD. Defaults to the next one.")
    @staff_only()
    async def fixture_list(interaction, week: str = None):
        await bot.show_dashboard(interaction, week or bot.current_week())

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

    @fixture_group.command(name="show", description="One game, with its scheduling log")
    @app_commands.describe(game="The game - pick from the list as you type")
    @staff_only()
    async def fixture_show(interaction, game: str):
        record = await resolve_game(interaction, game)
        if record is None:
            return
        await interaction.response.send_message(
            embed=_discord_embed(notify.fixture_detail(
                record, store.history(record["id"]),
                bot.slot(record["slot_key"]), store.fixture_referees(record["id"]))),
            ephemeral=True,
        )

    fixture_show.autocomplete("game")(game_autocomplete())

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

    # ------------------------------------------------------------ referees
    @refs_group.command(name="register", description="Register a referee, or deactivate one")
    @app_commands.describe(
        active="False deactivates them instead of registering",
        tier="Their referee tier (new referees start at 1)",
    )
    @staff_only()
    async def refs_register(interaction, user: discord.User, active: bool = True,
                            tier: app_commands.Range[int, 1, MAX_TIER] = None):
        if active:
            store.add_referee(user.id, user.display_name, tier)
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
        await interaction.response.send_message(embed=_discord_embed(embed), ephemeral=True)

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

    # --------------------------------------------------------------- ref (self-service)
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

    gw_group = app_commands.Group(name="gw", description="Gameweeks")

    @gw_group.command(name="list", description="The season calendar and what's open")
    async def gw_list(interaction):
        now = utcnow()
        here = season.current(now)
        opened = store.opened_gameweeks()
        lines = []
        for gw in season.ALL:
            marks = []
            if here and gw.key == here.key:
                marks.append("**current**")
            if gw.key in opened:
                marks.append("opened early")
            if not gw.has_fixtures:
                marks.append("no fixtures yet")
            count = len(store.fixtures(gameweek=gw.key))
            if count:
                marks.append("{} created".format(count))
            local, label = uk_time(gw.deadline)
            lines.append("`{:<4}` {}: plays {}, deadline {} {}{}".format(
                gw.key, gw.label, gw.friday.strftime("%a %d %b"),
                local.strftime("%a %d %b %H:%M"), label,
                "  ·  " + ", ".join(marks) if marks else "",
            ))
        embed = notify.BoardEmbed(
            title="{} calendar".format(season.SEASON),
            description="\n".join(lines),
            footer=("Managers can set availability for the current gameweek, "
                    "plus any Officials have opened early."),
        )
        await interaction.response.send_message(embed=_discord_embed(embed), ephemeral=True)

    @gw_group.command(name="open",
                      description="Create a gameweek's fixtures and post the announcement")
    @app_commands.describe(
        gameweek="e.g. GW2",
        count="Only create the first N fixtures. For testing; default is all 20.",
        test="TESTING ONLY: instantly give every fixture a random kickoff time, "
             "skipping availability entirely",
    )
    @staff_only()
    async def gw_open(interaction, gameweek: str, count: int = None, test: bool = False):
        await interaction.response.defer(ephemeral=True)
        try:
            gw = season.gameweek(gameweek)
        except LookupError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        if not gw.has_fixtures:
            await interaction.followup.send(
                "{} has no fixture list yet. Add it to season.py once the draw "
                "is made.".format(gw.key),
                ephemeral=True,
            )
            return

        manager_of = {}
        missing = []
        for code, name in season.TEAM_CODES.items():
            discord_id = store.manager_of(name)
            if discord_id:
                manager_of[name] = discord_id
            elif any(code in (h, a) for h, a, _ in gw.fixtures):
                missing.append("{} ({})".format(code, name))
        if missing:
            await interaction.followup.send(
                embed=_discord_embed(notify.BoardEmbed(
                    title="{} can't open yet".format(gw.label),
                    description=("These teams have no manager registered, so their "
                                 "fixtures can't be created:" + chr(10) * 2
                                 + ", ".join(sorted(missing))),
                    footer="Add them with /managers set.",
                )),
                ephemeral=True,
            )
            return

        store.open_gameweek(gw.key, interaction.user.id)
        made, skipped = await bot.create_gameweek_fixtures(
            gw, manager_of, opened_by=interaction.user.id, limit=count
        )

        scheduled = 0
        if test:
            # Not gated on `made`: fixtures from an earlier /gw open (or an
            # earlier test:True run) are skipped as duplicates above, but if
            # they're still sitting TBD this should fill them in too.
            outcomes = run_once_randomly(store, bot.timings, week=gw.week)
            scheduled = len(outcomes)
            log.warning("TEST MODE: randomly scheduled %d fixture(s) for %s by %s",
                       scheduled, gw.week, interaction.user)

        # The announcement is the primary channel, not the DMs. Plenty of
        # managers have DMs from server members switched off, and for them a DM
        # is simply never delivered - so the fixture list and the button that
        # opens the selector both go in the channel where everyone can see them.
        # In test mode there's nothing left to submit, so the button is skipped.
        posted = None
        try:
            posted = await bot.publish_board(gw.week, interaction.channel, with_button=not test)
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning("could not post the announcement: %s", error)

        deadline_local, deadline_label = uk_time(gw.deadline)
        parts = ["{} created. Deadline: {} {}.".format(
            plural(len(made), "fixture").capitalize(), deadline_local.strftime("%a %d %b %H:%M"), deadline_label)]
        if test:
            parts.append("-# TEST MODE: {} given a random kickoff time - "
                         "availability was skipped entirely.".format(
                             plural(scheduled, "fixture")))
        if posted:
            parts.append("Announcement posted here" +
                         ("." if test else " with the **Submit my timings** button.") +
                         " It updates itself as fixtures are agreed.")
        else:
            parts.append("⚠️ Couldn't post the announcement in this channel. "
                         "Check my permissions, then run `/fixture board`.")
        if count:
            parts.append("-# Limited to the first {} of {} fixtures. Run again "
                         "without `count` to create the rest.".format(
                             count, len(gw.fixtures) + len(gw.uefa_fixtures)))
        if skipped:
            parts.append("Skipped {}: {}".format(len(skipped), ", ".join(skipped[:8])))
        await interaction.followup.send(
            embed=_discord_embed(notify.BoardEmbed(
                title="{} opened".format(gw.label), description=chr(10).join(parts))),
            ephemeral=True,
        )

    @gw_group.command(name="close", description="Stop managers scheduling this gameweek")
    @staff_only()
    async def gw_close(interaction, gameweek: str):
        try:
            gw = season.gameweek(gameweek)
        except LookupError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        store.close_gameweek(gw.key)
        await interaction.response.send_message(
            "{} is no longer open early. Existing fixtures are untouched; the "
            "current gameweek is always open.".format(gw.key),
            ephemeral=True,
        )

    managers_group = app_commands.Group(name="managers", description="Staff: team managers")

    @managers_group.command(name="set", description="Say who manages a team")
    @app_commands.describe(team="Team name as in the timings sheet")
    @staff_only()
    async def managers_set(interaction, team: str, user: discord.User):
        try:
            resolved = bot.timings.find_team(team).country
        except LookupError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        store.set_manager(resolved, user.id)
        await interaction.response.send_message(
            "{} manages **{}**.".format(user.mention, resolved), ephemeral=True
        )

    @managers_group.command(name="list", description="Every team and its manager")
    @staff_only()
    async def managers_list(interaction, missing_only: bool = False):
        known = store.managers()
        league_of_code = {}
        for games in season.FIXTURES.values():
            for home, away, league in games:
                league_of_code.setdefault(home, league)
                league_of_code.setdefault(away, league)
        sections = []
        for league, league_name in season.LEAGUES.items():
            rows = []
            for code, name in sorted(season.TEAM_CODES.items(), key=lambda kv: kv[1]):
                if league_of_code.get(code) != league:
                    continue
                who = known.get(name)
                if missing_only and who:
                    continue
                rows.append("{} - {}".format(
                    season.label_for(name) if season.usable_emoji(season.TEAM_EMOJI.get(name))
                    else name,
                    "<@{}>".format(who) if who else "**nobody**"))
            if rows:
                badge = season.LEAGUE_EMOJI.get(league)
                head = "__**{}:**__".format(league_name)
                if season.usable_emoji(badge):
                    head += " " + badge
                sections.append(head + chr(10) + chr(10).join(rows))
        lines = [(chr(10) * 2).join(sections)] if sections else []
        embed = notify.BoardEmbed(
            title="Team managers",
            description=("\n".join(lines[:40]) if lines else "Every team is covered. ✅"),
        )
        await interaction.response.send_message(embed=_discord_embed(embed), ephemeral=True)

    # -------------------------------------------------------------------- test
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

    # ----------------------------------------------------------------- result
    players = Players.load()

    STARTERS = 7

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

            def box(label, default="", placeholder=None):
                item = discord.ui.TextInput(
                    label=label[:45], style=discord.TextStyle.paragraph,
                    default=default[:4000], required=False, max_length=4000,
                    placeholder=placeholder)
                self.add_item(item)
                return item

            self.home = box("{} stats".format(fixture["home_team"].title()),
                            starting_lines(fixture["home_team"]), "username g g a")
            self.away = box("{} stats".format(fixture["away_team"].title()),
                            starting_lines(fixture["away_team"]), "username g g a")
            self.motm = box("MOTM & mentions", placeholder="username - short note (optional)")
            names = {r["discord_id"]: r["name"] for r in store.referees(active_only=False)}
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
                    embed=_discord_embed(notify.BoardEmbed(
                        title="Fix these first",
                        description=chr(10).join(problems),
                        footer="Nothing was posted. Run /result again.")),
                    ephemeral=True)
                return
            posted = await bot.post(
                interaction.channel,
                embed=_discord_embed(notify.BoardEmbed(description=text)),
                mention_users=False)
            await interaction.response.send_message(
                "Result posted." if posted else
                "⚠️ Couldn't post here. Check my permissions.", ephemeral=True)

    @tree.command(name="result", description="Post a finished game's result")
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

    tree.add_command(gw_group)
    tree.add_command(managers_group)
    tree.add_command(fixture_group)
    tree.add_command(refs_group)
    tree.add_command(ref_group)
    tree.add_command(test_group)


# --------------------------------------------------------------------------

def main(argv=None):
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    argv = argv if argv is not None else sys.argv[1:]

    if "--check" in argv:
        # Everything except connecting: catches config gaps, a bad sheet and
        # an over-budget selector without touching Discord.
        bot = PRSBot()
        bot.load_timings()
        register(bot)
        print("timings   : {} ({} slots)".format(bot.timings.title, len(bot.slots)))
        print("database  : {}".format(bot.store.path))
        print("commands  : {}".format(
            ", ".join(sorted(c.name for c in bot.tree.get_commands()))
        ))
        gaps = config.missing()
        print("config    : {}".format("ok" if not gaps else "MISSING " + ", ".join(gaps)))
        return 1 if gaps else 0

    gaps = config.missing()
    if gaps:
        print("Missing config: {}. See .env.example.".format(", ".join(gaps)),
              file=sys.stderr)
        return 1

    PRSBot(fast_sync="--fast-sync" in argv).run(config.TOKEN, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
