"""The Discord bot: slash commands and the deadline runner.

Thin by design. Commands validate input, call into orchestrator/db, and report;
the deciding happens in modules that are tested without a gateway.

    python -m bot.main            run the bot
    python -m bot.main --check    load everything and exit, without connecting
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord
from discord import app_commands
from discord.ext import tasks

from . import config, notify, referees
from .db import OFFER_ACCEPTED, OFFER_DECLINED, Store
from .ref_views import REF_DYNAMIC_ITEMS, offer_view
from .fallback import Timings
from .orchestrator import Action, dashboard, run_once
from .scheduling import Status
from .selector import fits_on_one_message, SelectorState
from .views import (
    DYNAMIC_ITEMS, SCOPE_FIXTURE, SCOPE_REF_WEEK, Target, opener, open_selector,
)
from .weeks import from_iso, parse_deadline, to_iso, utcnow, week_of

log = logging.getLogger("prsbot")

# How often the deadline runner wakes. Deadlines are hours away, so a few
# minutes of lag is irrelevant - and a short interval keeps each pass tiny.
TICK_MINUTES = 5


class PRSBot(discord.Client):
    def __init__(self, sync_guild=False):
        self.sync_guild = sync_guild
        # Only `guilds`, which is not privileged. Message content and members
        # are never needed - everything is slash commands and buttons - but
        # without `guilds` the role objects behind a STAFF_ROLE_ID check cannot
        # be resolved, and the check would silently fail open or closed.
        super().__init__(intents=discord.Intents(guilds=True))
        self.tree = app_commands.CommandTree(self)
        self.store = Store()
        self.timings = None
        self.slots = []

    # ------------------------------------------------------------- lifecycle
    async def setup_hook(self):
        self.load_timings()
        for item in DYNAMIC_ITEMS + REF_DYNAMIC_ITEMS:
            self.add_dynamic_items(item)
        register(self)
        # Global, not guild-scoped: guild commands are unavailable in DMs, and
        # /availability has to work where the manager is being messaged.
        await self.tree.sync()
        log.info("commands synced globally")
        if self.sync_guild and config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("also synced to guild %s for immediate testing", config.GUILD_ID)
        self.runner.start()

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

    # ------------------------------------------------------------ messaging
    async def dm(self, user_id, content, view=None):
        """Best-effort DM. Managers with DMs closed must not stall the runner."""
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            await user.send(content, view=view) if view else await user.send(content)
            return True
        except (discord.Forbidden, discord.HTTPException, discord.NotFound) as error:
            log.warning("could not DM %s: %s", user_id, error)
            return False

    async def on_availability_submitted(self, fixture_id):
        """Called by the submit button - schedule the moment both are in."""
        if self.store.both_submitted(fixture_id):
            await self.process(week=self.store.fixture(fixture_id)["week"])

    # -------------------------------------------------------------- runner
    async def process(self, week=None):
        """One scheduling pass, then say what happened."""
        for outcome in run_once(self.store, self.timings, week=week):
            fixture = self.store.fixture(outcome.fixture_id)
            if outcome.action == Action.SCHEDULED:
                slot = self.slot(fixture["slot_key"])
                for manager_id in outcome.notify:
                    await self.dm(manager_id, notify.fixture_confirmed(fixture, slot))
            elif outcome.action == Action.REMIND:
                for which in outcome.reminders:
                    for manager_id in outcome.notify:
                        await self.dm(manager_id, notify.reminder(
                            fixture, fixture["deadline"], which))
                    self.store.mark_reminded(fixture["id"], which)
            elif outcome.action == Action.NO_VALID_TIME:
                log.warning("fixture %s needs manual scheduling: %s",
                            fixture["id"], outcome.detail)

        # Anything with a time but no referee - including fixtures scheduled
        # before a restart, whose offer never went out.
        for fixture in self.store.fixtures_awaiting_referee(week=week):
            slot = self.slot(fixture["slot_key"])
            if slot:
                await self.offer_referee(fixture, slot)

    def slot(self, key):
        return next((s for s in self.slots if s.key == key), None)

    async def offer_referee(self, fixture, slot, attempts=12):
        """Ask referees in ranked order until one is reachable.

        A referee who cannot be DM'd is recorded as declined rather than left
        pending - otherwise the fixture would sit waiting on an offer that was
        never delivered, and the sweep above would never retry it.
        """
        for _ in range(attempts):
            candidate = referees.offer(self.store, fixture, slot.key)
            if candidate is None:
                log.warning("fixture %s needs a referee by hand", fixture["id"])
                return None
            sent = await self.dm(
                candidate.referee_id, notify.referee_offer(fixture, slot),
                view=offer_view(fixture["id"]),
            )
            if sent:
                return candidate
            self.store.resolve_offer(fixture["id"], candidate.referee_id, OFFER_DECLINED)
            self.store.note(fixture["id"], "referee unreachable",
                            str(candidate.referee_id))
        return None

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
    """Staff role if configured, otherwise anyone who can manage the server.

    interaction.permissions comes from the interaction payload rather than the
    member cache, so it is trustworthy even for a guild the bot has not cached.
    """
    if config.STAFF_ROLE_ID:
        role_ids = {r.id for r in getattr(interaction.user, "roles", []) or []}
        return config.STAFF_ROLE_ID in role_ids
    try:
        return bool(interaction.permissions.manage_guild)
    except AttributeError:
        return False


def staff_only():
    async def predicate(interaction):
        if is_staff(interaction):
            return True
        await interaction.response.send_message(
            "That's a staff command.", ephemeral=True
        )
        return False
    return app_commands.check(predicate)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def register(bot):
    tree = bot.tree
    store = bot.store

    fixture_group = app_commands.Group(name="fixture", description="Fixture scheduling")
    refs_group = app_commands.Group(name="refs", description="Referees")

    # ------------------------------------------------------------- managers
    @tree.command(description="Set your availability for a fixture")
    @app_commands.describe(fixture="Fixture number, if you have more than one")
    async def availability(interaction, fixture: int = None):
        mine = [
            f for f in store.fixtures()
            if interaction.user.id in (f["home_manager_id"], f["away_manager_id"])
            and not f["slot_key"]
        ]
        if fixture is not None:
            mine = [f for f in mine if f["id"] == fixture]
        if not mine:
            await interaction.response.send_message(
                "You have no fixtures waiting on availability.", ephemeral=True
            )
            return
        if len(mine) > 1:
            listing = "\n".join(
                "· **#{}** {} vs {}".format(f["id"], f["home_team"], f["away_team"])
                for f in mine
            )
            await interaction.response.send_message(
                "You have a few open. Pick one with `/availability fixture:<number>`:"
                "\n\n" + listing,
                ephemeral=True,
            )
            return
        await open_selector(interaction, store, Target(SCOPE_FIXTURE, mine[0]["id"]),
                            bot.slots)

    # ------------------------------------------------------------- fixtures
    @fixture_group.command(name="create", description="Create a fixture and DM both managers")
    @app_commands.describe(
        home="Home team, as named in the timings sheet",
        away="Away team, as named in the timings sheet",
        home_manager="Home team's manager",
        away_manager="Away team's manager",
        deadline="e.g. 'Friday 18:00' or '2026-09-11 18:00' (UTC)",
    )
    @staff_only()
    async def fixture_create(interaction, home: str, away: str,
                             home_manager: discord.User, away_manager: discord.User,
                             deadline: str):
        await interaction.response.defer(ephemeral=True)

        # Fail before creating anything: a fixture whose teams aren't in the
        # sheet can never use the fallback, which is most of its safety net.
        problems = []
        resolved = {}
        for label, name in (("home", home), ("away", away)):
            try:
                resolved[label] = bot.timings.find_team(name).country
            except LookupError as error:
                problems.append("{}: {}".format(label, error))
        try:
            when = parse_deadline(deadline)
        except ValueError as error:
            problems.append(str(error))
            when = None
        if resolved.get("home") and resolved.get("home") == resolved.get("away"):
            problems.append("A team can't play itself.")
        if home_manager.id == away_manager.id:
            problems.append("Both managers are the same person.")

        if problems:
            await interaction.followup.send(
                "Couldn't create that fixture:\n" + "\n".join("· " + p for p in problems),
                ephemeral=True,
            )
            return

        fixture_id = store.create_fixture(
            bot.timings.competition.key, week_of(when),
            resolved["home"], resolved["away"],
            home_manager.id, away_manager.id, to_iso(when),
            Status.WAITING_FOR_AVAILABILITY,
        )
        fixture = store.fixture(fixture_id)

        reached = []
        for user in (home_manager, away_manager):
            ok = await bot.dm(
                user.id, notify.ask_for_availability(fixture, to_iso(when)),
                view=opener(Target(SCOPE_FIXTURE, fixture_id)),
            )
            reached.append("{} {}".format("✅" if ok else "⚠️", user.mention))
            if not ok:
                store.note(fixture_id, "could not DM", str(user.id))

        await interaction.followup.send(
            "Created **#{}** — {} vs {}, deadline {}.\nDMs: {}".format(
                fixture_id, resolved["home"], resolved["away"],
                when.strftime("%a %d %b %H:%M UTC"), "  ".join(reached),
            ),
            ephemeral=True,
        )

    @fixture_group.command(name="list", description="Scheduling status for a week")
    @app_commands.describe(week="Saturday of the weekend, YYYY-MM-DD. Defaults to the next one.")
    @staff_only()
    async def fixture_list(interaction, week: str = None):
        week = week or week_of(utcnow())
        buckets = dashboard(store, week=week)
        await interaction.response.send_message(
            notify.dashboard_summary(buckets, week), ephemeral=True
        )

    @fixture_group.command(name="show", description="One fixture, with its scheduling log")
    @staff_only()
    async def fixture_show(interaction, fixture: int):
        record = store.fixture(fixture)
        if not record:
            await interaction.response.send_message(
                "No fixture #{}.".format(fixture), ephemeral=True
            )
            return
        await interaction.response.send_message(
            notify.fixture_detail(record, store.history(fixture),
                                  bot.slot(record["slot_key"])),
            ephemeral=True,
        )

    @fixture_group.command(name="run", description="Run a scheduling pass now")
    @staff_only()
    async def fixture_run(interaction, week: str = None):
        await interaction.response.defer(ephemeral=True)
        await bot.process(week=week)
        buckets = dashboard(store, week=week or week_of(utcnow()))
        await interaction.followup.send(
            notify.dashboard_summary(buckets, week or week_of(utcnow())), ephemeral=True
        )

    @fixture_group.command(name="set", description="Set a kickoff time by hand")
    @app_commands.describe(slot="Slot key, e.g. sat_1800")
    @staff_only()
    async def fixture_set(interaction, fixture: int, slot: str):
        record = store.fixture(fixture)
        if not record:
            await interaction.response.send_message(
                "No fixture #{}.".format(fixture), ephemeral=True
            )
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
        store.set_schedule(fixture, chosen.key, "MANUAL", Status.SCHEDULED)
        store.note(fixture, "set by hand", "{} by {}".format(chosen.key, interaction.user.id))
        record = store.fixture(fixture)
        for manager_id in (record["home_manager_id"], record["away_manager_id"]):
            await bot.dm(manager_id, notify.fixture_confirmed(record, chosen))
        await interaction.response.send_message(
            "#{} set to {}.".format(fixture, chosen), ephemeral=True
        )

    # ------------------------------------------------------------ referees
    @refs_group.command(name="add", description="Register a referee")
    @staff_only()
    async def refs_add(interaction, user: discord.User):
        store.add_referee(user.id, user.display_name)
        await interaction.response.send_message(
            "{} added as a referee.".format(user.mention), ephemeral=True
        )

    @refs_group.command(name="remove", description="Deactivate a referee")
    @staff_only()
    async def refs_remove(interaction, user: discord.User):
        store.set_referee_active(user.id, False)
        await interaction.response.send_message(
            "{} deactivated.".format(user.mention), ephemeral=True
        )

    @refs_group.command(name="list", description="Registered referees and their load")
    @staff_only()
    async def refs_list(interaction, week: str = None):
        week = week or week_of(utcnow())
        workload = store.ref_workload(week)
        rows = store.referees()
        if not rows:
            await interaction.response.send_message(
                "No referees registered. Add one with `/refs add`.", ephemeral=True
            )
            return
        lines = ["**Referees — week of {}**".format(week), ""]
        for ref in rows:
            got = store.ref_availability(ref["discord_id"], week)
            lines.append("· <@{}> — {} game(s){}".format(
                ref["discord_id"], workload.get(ref["discord_id"], 0),
                "" if (got and got["submitted"]) else "  ⚠️ no availability submitted",
            ))
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @refs_group.command(name="assign", description="Assign a referee by hand")
    @staff_only()
    async def refs_assign(interaction, fixture: int, user: discord.User):
        record = store.fixture(fixture)
        if not record:
            await interaction.response.send_message(
                "No fixture #{}.".format(fixture), ephemeral=True
            )
            return
        if not record["slot_key"]:
            await interaction.response.send_message(
                "#{} has no kickoff time yet.".format(fixture), ephemeral=True
            )
            return
        store.add_referee(user.id, user.display_name)
        store.withdraw_offers(fixture)
        store.set_referee(fixture, user.id)
        store.set_status(fixture, Status.FULLY_CONFIRMED,
                         "referee set by hand by {}".format(interaction.user.id))
        record = store.fixture(fixture)
        slot = bot.slot(record["slot_key"])
        await bot.dm(user.id, notify.referee_confirmed(record, slot))
        for manager_id in (record["home_manager_id"], record["away_manager_id"]):
            await bot.dm(manager_id, notify.fixture_confirmed(
                record, slot, referee_name=user.display_name))
        await interaction.response.send_message(
            "{} assigned to #{}.".format(user.mention, fixture), ephemeral=True
        )

    @refs_group.command(name="availability", description="Set your referee availability")
    async def refs_availability(interaction, week: str = None):
        if not any(r["discord_id"] == interaction.user.id for r in store.referees()):
            await interaction.response.send_message(
                "You're not registered as a referee. Ask staff to add you.",
                ephemeral=True,
            )
            return
        week = week or week_of(utcnow())
        await open_selector(interaction, store, Target(SCOPE_REF_WEEK, week), bot.slots)

    tree.add_command(fixture_group)
    tree.add_command(refs_group)

    @tree.command(description="Check the bot's configuration and data")
    @staff_only()
    async def status(interaction):
        week = week_of(utcnow())
        await interaction.response.send_message("\n".join([
            "**{}**".format(bot.timings.title),
            "Slots offered: {} across {}".format(
                len(bot.slots), ", ".join(sorted({s.day for s in bot.slots}))
            ),
            "Teams in the sheet: {}".format(len(bot.timings.sheet.teams)),
            "Fixtures this week: {}".format(len(store.fixtures(week=week))),
            "Referees: {}".format(len(store.referees())),
            "Runner: every {} min".format(TICK_MINUTES),
        ]), ephemeral=True)


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

    PRSBot(sync_guild="--sync-guild" in argv).run(config.TOKEN, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
