"""What the bot actually says.

Message text only - no sending, no Discord objects - so the wording can be
checked in tests and changed without touching the logic.

Times go out as Discord timestamps (<t:...:F>), which each reader's client
renders in their own timezone. The league spans several, and the whole point of
the original matcher was to stop people converting GMT in their heads.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import timedelta

from . import season
from .referees import MAX_ASSISTANTS, ROLE_AR, ROLE_REF
from .scheduling import Source

from .weeks import discord_time, from_iso, slot_datetime, week_saturday

FOOTER = "-# Times show in your own timezone."


@dataclass
class BoardEmbed:
    """Plain data for one Discord embed - no discord.Embed here, so this can
    still be built and checked without a gateway, same as everything else in
    this module. The caller (main.py) turns it into a real embed only when it
    actually sends something.

    `fields` is a list of (name, value, inline) triples.
    """
    description: str
    title: str = None
    fields: list = field(default_factory=list)
    footer: str = None


def _slot_line(week, slot):
    moment = slot_datetime(week, slot)
    return "{}  ({} {} GMT)".format(discord_time(moment), slot.day, slot.label)


def ask_for_availability(fixture, deadline_iso):
    """The opening post for a single fixture: what it is and by when.

    Used by /fixture create for a one-off. A whole gameweek is announced by
    fixture_board instead, which covers all twenty at once.
    """
    return "\n".join([
        "# {} vs {}".format(fixture["home_team"], fixture["away_team"]),
        "Fixture **#{}** needs a kickoff time.".format(fixture["id"]),
        "",
        "Mark every time you could play as **🟢 ideal**, **🟡 fine**, or leave it "
        "**⚪ no**. We will pick the best time you and your opponent both agree on.",
        "",
        "Deadline: {}".format(discord_time(from_iso(deadline_iso))),
        "",
        "Press the button below, or use **/availability**. You can change your "
        "answers until the deadline.",
        "",
        "-# If neither of you replies, Officials will allocate a time from your "
        "teams' saved timings.",
    ])


def reminder(fixture, deadline_iso, which, managers=()):
    """A public nudge, mentioning whoever still owes an answer.

    Posted in the channel rather than DM'd. A DM only reaches people who allow
    DMs from server members, and the ones who have not submitted are exactly
    the ones most likely to have them off.
    """
    urgency = {"12h": "in about 12 hours", "2h": "in about 2 hours"}.get(which, "soon")
    who = " ".join("<@{}>".format(m) for m in managers)
    lines = [
        "⏰ **Reminder: {} vs {}**".format(fixture["home_team"], fixture["away_team"]),
    ]
    if who:
        lines.append(who)
    lines += [
        "",
        "Still no timings from {} of you. The deadline is {} ({}).".format(
            "one" if len(managers) == 1 else "both",
            urgency, discord_time(from_iso(deadline_iso)),
        ),
        "",
        "Use the **Submit my timings** button on the fixture post, or "
        "**/availability**.",
        "",
        "-# Miss the deadline and Officials will allocate a time from your "
        "team's saved timings instead.",
    ]
    return "\n".join(lines)


def fixture_confirmed(fixture, slot, roster=()):
    lines = [
        "# ⚽ Fixture confirmed",
        "**{}** vs **{}**  ·  #{}".format(
            fixture["home_team"], fixture["away_team"], fixture["id"]
        ),
        "",
        _slot_line(fixture["week"], slot),
        _roster_line(roster),
    ]
    if fixture.get("schedule_source") == Source.AUTO_FALLBACK:
        lines += [
            "",
            "-# Allocated automatically from your team's saved timings, because "
            "the deadline passed without both managers submitting.",
        ]
    lines += ["", FOOTER]
    return "\n".join(lines)


def _roster_line(roster):
    """Referee: @X   ·   Assistants: @Y, _open_ - always both roles shown."""
    ref = next((r for r in roster if r["role"] == ROLE_REF), None)
    ars = [r for r in roster if r["role"] == ROLE_AR]
    ar_names = ["<@{}>".format(a["referee_id"]) for a in ars]
    ar_names += ["_open_"] * (MAX_ASSISTANTS - len(ar_names))
    return "Referee: {}   ·   Assistants: {}".format(
        "<@{}>".format(ref["referee_id"]) if ref else "_open_",
        ", ".join(ar_names),
    )


def league_tag(league):
    """(DOM <league emoji>) or (UEFA <uefa emoji>) - which competition a
    fixture is in, and for a domestic one which league.

    Plain parentheses rather than a code span: Discord doesn't render custom
    emoji inside backticks, and the emoji is the point. A league with no
    usable emoji just shows the bare word.
    """
    if not league:
        return ""
    domestic = league in season.LEAGUES
    emoji = season.LEAGUE_EMOJI.get(league) if domestic else season.UEFA_TAG_EMOJI
    label = "DOM" if domestic else "UEFA"
    if season.usable_emoji(emoji):
        label = "{} {}".format(label, emoji)
    return "({}) ".format(label)


def referee_board_row(fixture, slot, week, roster=()):
    """One line: league tag, two badges, kickoff, then whoever has it so far.

    Mentions rather than names - unlike the compact fixture board, this one
    exists specifically to be claimed, so pinging whoever is already on it is
    the point rather than something to avoid.
    """
    home = season.label_for(fixture["home_team"])
    away = season.label_for(fixture["away_team"])
    when = discord_time(slot_datetime(week, slot), "F")
    tag = league_tag(fixture.get("league"))
    who = " ".join("<@{}>".format(r["referee_id"]) for r in roster) if roster else "_open_"
    return "{}{} *vs* {} @ {} {}".format(tag, home, away, when, who)


def referee_board(fixtures, week, slot_for, rosters=None, gameweek=None,
                  competition=None):
    """The public, self-updating referee board: every day that has a kickoff
    time yet, one embed per day, all sent in one message.

    A fixture still marked TBD is left off entirely - there's nothing to claim
    until it has a time. The claim prompt is a separate embed below this one
    (see referee_claim_prompt); this board only ever shows state, never a
    call to action.

    Mentioning the referee role, if configured, is the caller's job, not
    this function's - a mention inside an embed is just inert text, it does
    not notify anyone, so it has to go in the message's plain content instead.
    """
    rosters = rosters or {}

    by_day = {}
    for fixture in fixtures:
        if not fixture["slot_key"]:
            continue
        slot = slot_for(fixture["slot_key"])
        if not slot:
            continue
        moment = slot_datetime(week, slot)
        by_day.setdefault(moment.date(), []).append((moment, fixture, slot))

    # The embed's own title, not a bolded description line - see fixture_board.
    parts = ["PRS", season.SEASON_LABEL]
    if competition:
        parts.append(competition.upper())
    parts.append((gameweek.label if gameweek else "FIXTURES").upper())
    title = "{}:".format(" ".join(parts))
    if season.usable_emoji(season.SEASON_EMOJI):
        title = "{} {}".format(title, season.SEASON_EMOJI)

    footer_text = ("Times show in your own timezone. This board updates "
                  "itself as games are claimed.")

    if not by_day:
        return [BoardEmbed(
            title=title,
            description="_No fixtures have a kickoff time yet._",
            footer=footer_text,
        )]

    blocks = []
    for day in sorted(by_day):
        block = ["**__{} {} {}:__**  📅".format(
            day.strftime("%A"), day.day, day.strftime("%B"))]
        for moment, fixture, slot in sorted(by_day[day], key=lambda row: row[0]):
            block.append(referee_board_row(
                fixture, slot, week, rosters.get(fixture["id"])))
        block.append("")
        blocks.append(block)

    chunks = []
    for block in blocks:
        chunks.extend(_chunk_description(block, REFEREE_EMBED_CHUNK))
    embeds = [BoardEmbed(title=title if i == 0 else None, description=d)
             for i, d in enumerate(chunks)]
    embeds[-1].footer = footer_text
    return embeds


def referee_claim_prompt(open_count):
    """The embed under the board that carries the claim menu."""
    if open_count == 0:
        return BoardEmbed(
            title="Referees",
            description="Every game above is fully staffed. ✅",
        )
    noun = "game" if open_count == 1 else "games"
    verb = "needs" if open_count == 1 else "need"
    return BoardEmbed(
        title="Referees, claim a game",
        description=(
            "Pick an open game from the menu below to take it. First come, "
            "first served - one referee and up to two assistants per game."
        ),
        footer="{} {} still {} officiating.".format(open_count, noun, verb),
    )


def no_valid_time(fixture, reason):
    return "\n".join([
        "🔴 **Fixture #{} could not be scheduled**".format(fixture["id"]),
        "{} vs {}".format(fixture["home_team"], fixture["away_team"]),
        "",
        reason,
        "",
        "Someone needs to sort this one out by hand.",
    ])


# --------------------------------------------------------------------------
# the master fixture list (spec step 15)
# --------------------------------------------------------------------------

STATUS_ICON = {
    "FULLY_CONFIRMED": "✅",
    "SCHEDULED": "🟠",
    "NEEDS_MANUAL_REF": "🟠",
    "NEEDS_MANUAL_SCHEDULING": "🔴",
    "WAITING_FOR_AVAILABILITY": "🟡",
}

SOURCE_SHORT = {
    Source.MANAGER_PREFERENCES: "managers",
    Source.AUTO_FALLBACK: "auto",
    "MANUAL": "staff",
}


def board_row(fixture, slot):
    """One fixture line: two badges, then the kickoff.

    Badges only, no team names - that is how the league's own posts read, and
    at forty fixtures the names are what pushes a gameweek over Discord's
    message limit. A team with no badge configured falls back to its name,
    since an empty side would leave the row meaningless.

    Never shows the referee, even once one is assigned - that lives on the
    referee board and nowhere else, so a manager scanning this one sees the
    same shape whether a game is fully staffed or not.
    """
    home = season.label_for(fixture["home_team"])
    away = season.label_for(fixture["away_team"])
    when = (discord_time(slot_datetime(fixture["week"], slot), "F")
            if slot else "`TBD`")
    return "{} *vs* {} @ {}".format(home, away, when)


def fixture_board(fixtures, week, slot_for,
                  gameweek=None, deadline=None, competition=None):
    """The public fixture announcement, grouped by division, as one or more
    embeds (more than one only if a huge gameweek genuinely needs it - see
    _chunk_description). All attached to a single message.

    Every fixture appears, scheduled or not. It is edited in place as times get
    decided, so one message is the whole gameweek's answer - which matters more
    than it sounds: managers with DMs closed never see a DM, and this is what
    they read instead.

    Mentioning the announce role, if configured, is the caller's job, not this
    function's - a mention inside an embed is just inert text, it does not
    notify anyone, so it has to go in the message's plain content instead.
    """
    by_league = {}
    for fixture in fixtures:
        key = fixture.get("league") or "??"
        by_league.setdefault(key, []).append(fixture)

    # "PRS SEASON 17 CLUBS GAMEWEEK 1:" - the competition sits between the
    # season and the gameweek, matching how the league titles its own posts.
    # The embed's own title, not a bolded description line - Discord already
    # renders a title bigger and bolder than anything markdown can do inside
    # the body.
    parts = ["PRS", season.SEASON_LABEL]
    if competition:
        parts.append(competition.upper())
    parts.append((gameweek.label if gameweek else "FIXTURES").upper())
    title = "{}:".format(" ".join(parts))
    if season.usable_emoji(season.SEASON_EMOJI):
        title = "{} {}".format(title, season.SEASON_EMOJI)

    lines = []

    order = list(season.LEAGUES) + sorted(k for k in by_league if k not in season.LEAGUES)
    for key in order:
        rows = by_league.get(key)
        if not rows:
            continue
        # The badge goes after the colon, and only if it is a usable
        # custom-emoji reference. A division with none - or with a stale id
        # left behind after its emoji was deleted - just renders its name;
        # Discord prints a dead reference as raw text otherwise.
        division = "**__{}:__**".format(season.LEAGUES.get(key, key))
        badge = season.LEAGUE_EMOJI.get(key)
        if season.usable_emoji(badge):
            division = "{}  {}".format(division, badge)
        lines.append(division)


        def sort_key(fixture):
            slot = slot_for(fixture["slot_key"]) if fixture["slot_key"] else None
            if not slot:
                return (1, 0, 0)
            return (0, slot.day_index, slot.minutes)

        for fixture in sorted(rows, key=sort_key):
            lines.append(board_row(
                fixture, slot_for(fixture["slot_key"]) if fixture["slot_key"] else None,
            ))
        lines.append("")

    if not fixtures:
        lines.append("_No fixtures for this gameweek yet._")
        lines.append("")

    chunks = _chunk_description(lines)
    embeds = [BoardEmbed(title=title if i == 0 else None, description=d)
             for i, d in enumerate(chunks)]

    if deadline is not None:
        embeds[-1].fields = [
            ("Scheduling Deadline", discord_time(deadline, "F"), False),
            ("Scheduling Extension", discord_time(deadline + timedelta(days=7), "F"), False),
        ]
    embeds[-1].footer = (
        "If a fixture isn't agreed by the deadline, Officials schedule it "
        "from your submitted timings - the extension is the latest it can "
        "still be moved to if postponed. Times show in your own timezone, "
        "and this board updates itself as fixtures are agreed."
    )
    return embeds


def availability_call_to_action(gameweek, deadline):
    """The board posted under the announcement, with the button on it.

    Its own embed, and its own message - a button is attached to the message,
    not the embed, and keeping it separate from the announcement means that
    board can keep being edited in place without Discord ever touching this
    one or dropping its component.

    Public and button-driven on purpose. A DM only reaches managers who allow
    them; a button in a channel reaches everyone, and each person who clicks it
    gets their own private selector.
    """
    return BoardEmbed(
        title="Managers, submit your timings",
        description=(
            "Use the button below to submit the times **{}** works for "
            "your team. The selector opens privately, so only you can see "
            "your responses.\n\n"
            "**How to mark each time slot:** ⚪ No · 🟡 Fine · 🟢 Ideal\n\n"
            "Once both managers have responded, the best mutually available "
            "time is selected automatically and the fixture list above "
            "updates."
        ).format(gameweek.label),
        fields=[
            ("Deadline", discord_time(deadline, "F"), False),
        ],
        footer=("If you have nothing to submit, your gameweek may not be "
                "open yet, or you are not registered as a manager. Contact "
                "an Official if you believe this is incorrect."),
    )


# Discord's own hard cap on one embed's description. Set to the real
# number rather than a conservative margin below it: _chunk_description()
# always finishes a chunk at least one character short of this figure
# (see its docstring), so a chunk can never actually reach 4096 - there is
# no scenario where sitting further below it buys any extra safety, only
# an earlier, needless split into a second embed.
DESCRIPTION_CHUNK = 4096


def _chunk_description(lines, limit=None):
    """Split rendered lines into embed-description-sized pieces, never mid-row.

    Each finished chunk's actual length is DESCRIPTION_CHUNK minus one at
    most: `length` tracks every line plus a trailing separator that the
    last line never actually gets once joined, so the real string is
    always one character shorter than the budget it was closed under.
    """
    limit = limit or DESCRIPTION_CHUNK
    chunks = []
    current = []
    length = 0
    for line in lines:
        if length + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current = []
            length = 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


# What the referee board keeps each embed under. Discord *stores* a
# description up to 4096 characters, but the client was observed silently
# not displaying the tail of a ~4000-character one (the board's Sunday
# section went missing on screen while the API held it in full) - so the
# referee board gets one embed per day instead of one long one. A day is
# only ever split further if it alone is bigger than this.
REFEREE_EMBED_CHUNK = 2500


# Discord's cap on the *combined* size of every embed attached to one
# message - title + description + every field's name and value + footer,
# summed across all of them - not just the 4096-character description limit
# on each embed individually. A board that needed two embeds under
# DESCRIPTION_CHUNK could still add up to more than this between them, and
# Discord rejects the whole send/edit outright rather than truncating - so
# group_embeds_for_messages() below keeps each message's embeds under this
# figure, splitting into more messages rather than risk that.
MESSAGE_EMBED_BUDGET = 5900


def _embed_size(embed):
    """Every character Discord counts toward an embed's share of the
    combined per-message limit."""
    size = len(embed.description) + len(embed.title or "") + len(embed.footer or "")
    for name, value, _ in embed.fields:
        size += len(name) + len(value)
    return size


def group_embeds_for_messages(embeds):
    """Split a list of BoardEmbeds into the batches they need to go out
    as separate messages in, so no single message's combined embed content
    can ever exceed Discord's real limit. Almost always just one batch -
    this only bites once a board is genuinely too big for DESCRIPTION_CHUNK
    alone to have caught.
    """
    groups = []
    current = []
    total = 0
    for embed in embeds:
        size = _embed_size(embed)
        if current and total + size > MESSAGE_EMBED_BUDGET:
            groups.append(current)
            current = []
            total = 0
        current.append(embed)
        total += size
    if current:
        groups.append(current)
    return groups or [[]]


def board_digest(bodies):
    """A fingerprint of what was published, to skip no-op edits.

    `bodies` is a mix of plain strings and BoardEmbed objects - whichever a
    given board's caller happens to pass - so each embed is flattened to its
    own text before hashing rather than assuming one shape or the other.
    """
    parts = []
    for body in bodies:
        if isinstance(body, BoardEmbed):
            parts.append(body.title or "")
            parts.append(body.description)
            parts.append(body.footer or "")
            parts += ["{}:{}".format(name, value) for name, value, _ in body.fields]
        else:
            parts.append(body)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# the staff dashboard (spec step 16)
# --------------------------------------------------------------------------

def dashboard_summary(buckets, week, waiting_on=None, reasons=None, rosters=None):
    """The exception monitor from spec step 16.

    Counts first, then only what needs a human - and for each of those, who or
    what is blocking it. "Awaiting response: 2" tells staff nothing actionable;
    naming the manager who hasn't replied does.
    """
    waiting_on = waiting_on or {}
    reasons = reasons or {}
    rosters = rosters or {}

    counts = [
        ("🟢", len(buckets["confirmed"]), "fully confirmed"),
        ("🟡", len(buckets["awaiting"]), "awaiting response"),
        ("🟠", len(buckets["ref_needed"]), "ref needed"),
        ("🔴", len(buckets["no_valid_time"]), "no valid time"),
    ]
    lines = ["# Scheduling: week of {}".format(week), ""]
    lines += ["{} **{}** {}".format(icon, n, label) for icon, n, label in counts]

    if buckets["no_valid_time"]:
        lines += ["", "🔴 **No valid time** (needs scheduling by hand)"]
        for fixture in buckets["no_valid_time"][:8]:
            lines.append("· **#{}** {} v {}".format(
                fixture["id"], fixture["home_team"], fixture["away_team"]))
            why = reasons.get(fixture["id"])
            if why:
                lines.append("  -# {}".format(why))
        lines.append("-# Fix with `/fixture set fixture:<id> slot:<slot>`")

    if buckets["ref_needed"]:
        lines += ["", "🟠 **Ref needed**"]
        for fixture in buckets["ref_needed"][:8]:
            assistants = [r for r in (rosters.get(fixture["id"]) or []) if r["role"] == ROLE_AR]
            tail = ("  -# {} assistant(s) already claimed".format(len(assistants))
                    if assistants else "")
            lines.append("· **#{}** {} v {}: {}{}".format(
                fixture["id"], fixture["home_team"], fixture["away_team"],
                fixture["slot_key"] or "no time", tail))
        lines.append("-# Claim it in the referee channel, or fix by hand with "
                     "`/refs assign fixture:<id> user:@ref`")

    if buckets["awaiting"]:
        lines += ["", "🟡 **Awaiting response**"]
        for fixture in buckets["awaiting"][:8]:
            missing = waiting_on.get(fixture["id"]) or []
            who = ", ".join("<@{}>".format(m) for m in missing) or "nobody"
            lines.append("· **#{}** {} v {}, waiting on {}".format(
                fixture["id"], fixture["home_team"], fixture["away_team"], who))

    for key in ("no_valid_time", "ref_needed", "awaiting"):
        if len(buckets[key]) > 8:
            lines.append("-# …and {} more {}".format(len(buckets[key]) - 8,
                                                     key.replace("_", " ")))

    if not any(buckets[k] for k in ("no_valid_time", "ref_needed", "awaiting")):
        lines += ["", "Nothing needs attention. ✅"]
    return "\n".join(lines)


def fixture_detail(fixture, history, slot=None, roster=()):
    lines = [
        "# Fixture #{}".format(fixture["id"]),
        "**{}** vs **{}**".format(fixture["home_team"], fixture["away_team"]),
        "",
        "Competition: `{}`  ·  week of {}".format(fixture["competition"], fixture["week"]),
        "Status: `{}`".format(fixture["status"]),
        "Deadline: {}".format(discord_time(from_iso(fixture["deadline"]))),
    ]
    if slot:
        lines.append("Kickoff: {}".format(_slot_line(fixture["week"], slot)))
        lines.append("Chosen by: `{}`".format(fixture["schedule_source"]))
    if roster:
        lines.append(_roster_line(roster))

    if history:
        lines += ["", "**Scheduling log**"]
        for entry in history[-15:]:
            detail = ": {}".format(entry["detail"]) if entry["detail"] else ""
            lines.append("`{}`  {}{}".format(entry["at"][11:16], entry["event"], detail))
    return "\n".join(lines)
