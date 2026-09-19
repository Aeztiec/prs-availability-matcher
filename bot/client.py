"""The Discord client: connection, the scheduling runner, and the boards it keeps up to date."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import tasks

from bot import config
from bot.ui import notify
from bot.domain import referees, season
from bot.db import Store
from bot.ui.ref_views import REF_DYNAMIC_ITEMS, ClaimSelect, claim_options
from bot.domain.fallback import Timings
from bot.domain.orchestrator import Action, dashboard, run_once
from bot.domain.scheduling import Status
from bot.ui.selector import fits_on_one_message, SelectorState
from bot.domain.slots import within_kickoff_window
from bot.ui.views import DYNAMIC_ITEMS, availability_button
from bot.domain.weeks import slot_datetime, to_iso, utcnow, week_of
from bot.commands import register
from bot.ui.render import to_discord_embed

log = logging.getLogger("prsbot")


# How often the deadline runner wakes. Deadlines are hours away, so a few
# minutes of lag is irrelevant - and a short interval keeps each pass tiny.
TICK_MINUTES = 5


class PRSBot(discord.Client):
    def __init__(self, fast_sync=False, store=None):
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
        self.store = store or Store()
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
                    await self.post(channel, content, embed=to_discord_embed(embed))
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
        # of the post.
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
                embeds=[to_discord_embed(b) for b in group],
                allowed_mentions=pinging if index == 0 else quiet,
            ))
        sent.append(await channel.send(
            embed=to_discord_embed(prompt), view=view, allowed_mentions=quiet
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
                embeds = [to_discord_embed(b) for b in group]
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

            prompt_embed = to_discord_embed(prompt)
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
                embeds=[to_discord_embed(b) for b in group],
                allowed_mentions=pinging if index == 0 else quiet,
            ))
        self.store.set_board(week, channel.id, [m.id for m in sent],
                             notify.board_digest(bodies))

        if with_button:
            gw = next((g for g in season.ALL if g.week == week), None)
            if gw:
                await channel.send(
                    embed=to_discord_embed(notify.availability_call_to_action(gw, gw.deadline)),
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
                embeds = [to_discord_embed(b) for b in group]
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
        await sender(embed=to_discord_embed(embed), ephemeral=True)

    @tasks.loop(minutes=TICK_MINUTES)
    async def runner(self):
        try:
            await self.process()
        except Exception:                      # a bad pass must not kill the loop
            log.exception("scheduling pass failed")

    @runner.before_loop
    async def before_runner(self):
        await self.wait_until_ready()
