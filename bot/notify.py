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

from .text import plural
from .weeks import discord_time, from_iso, slot_datetime, week_saturday

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


def _game(fixture):
    """'<badge> vs <badge>' - a game the way the boards show it."""
    return "{} vs {}".format(season.label_for(fixture["home_team"]),
                             season.label_for(fixture["away_team"]))


LEGEND_LINE = ("**How to mark each time slot:**" + chr(10) + "⚪ No" + chr(10)
               + "🟡 Fine" + chr(10) + "🟢 Ideal")


def reminder(fixture, deadline_iso, which, managers=()):
    """A public nudge for whoever still owes an answer: (content, embed).

    The mentions go in the message content because a mention inside an embed
    never notifies anyone. Posted in the channel rather than DM'd - a DM only
    reaches people who allow DMs from server members, and the ones who have
    not submitted are exactly the ones most likely to have them off.
    """
    urgency = {"12h": "in about 12 hours", "2h": "in about 2 hours"}.get(which, "soon")
    content = " ".join("<@{}>".format(m) for m in managers) or None
    embed = BoardEmbed(
        title="Reminder: submit your timings",
        description=(
            "{} still needs timings from {}.\n\n"
            "The deadline is {} ({}).\n\n"
            "Use the **Submit my timings** button on the fixture board, or run "
            "**/availability**."
        ).format(
            _game(fixture),
            "one of you" if len(managers) == 1 else "both managers",
            urgency, discord_time(from_iso(deadline_iso), "F"),
        ),
        footer=("Miss the deadline and Officials will allocate a time from your "
                "team's saved timings instead."),
    )
    return content, embed


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


# The competition tags (UEFA, and later UCL / UEL / UECL) and the domestic one
# are all shown at the same width so the badges after them start in the same
# column. That only works inside a code span: Discord draws those monospace, so
# equal character counts really are equal widths. The width is the longest
# competition tag actually on the board (never under 3), and DOMESTIC is cut
# down to fit it - DOME next to UEFA, DOM next to UCL/UEL.
MIN_TAG_WIDTH = 3


def tag_width(leagues):
    """How many letters every tag on a board gets: the longest competition
    code present (UEFA is 4, UCL/UEL are 3, UECL is 4), at least 3."""
    codes = [l for l in leagues if l and l not in season.LEAGUES]
    return max([MIN_TAG_WIDTH] + [len(c) for c in codes])


def league_tag(league, width=MIN_TAG_WIDTH):
    """`(DOME)`, `(UEFA)`, `(DOM)`, `(UCL)` ... - which competition a fixture
    is in, padded or cut to `width` letters so a board's tags line up."""
    if not league:
        return ""
    label = "DOMESTIC" if league in season.LEAGUES else league
    return "`({})` ".format(label[:width].ljust(width))


def referee_board_row(fixture, slot, week, roster=(), tag_letters=MIN_TAG_WIDTH):
    """One line: league tag, two badges, kickoff, then whoever has it so far.

    Mentions rather than names - unlike the compact fixture board, this one
    exists specifically to be claimed, so pinging whoever is already on it is
    the point rather than something to avoid.
    """
    home = season.label_for(fixture["home_team"])
    away = season.label_for(fixture["away_team"])
    when = discord_time(slot_datetime(week, slot), "F")
    tag = league_tag(fixture.get("league"), tag_letters)
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

    letters = tag_width(f.get("league") for f in fixtures if f["slot_key"])
    blocks = []
    for day in sorted(by_day):
        block = ["**__{} {} {}:__**  📅".format(
            day.strftime("%A"), day.day, day.strftime("%B"))]
        for moment, fixture, slot in sorted(by_day[day], key=lambda row: row[0]):
            block.append(referee_board_row(
                fixture, slot, week, rosters.get(fixture["id"]), letters))
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


# --------------------------------------------------------------------------
# the master fixture list (spec step 15)
# --------------------------------------------------------------------------

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
        badge = season.competition_emoji(key)
        if badge:
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
            "{legend}\n\n"
            "Once both managers have responded, the best mutually available "
            "time is selected automatically and the fixture list above "
            "updates."
        ).format(gameweek.label, legend=LEGEND_LINE),
        fields=[
            ("Deadline", discord_time(deadline, "F"), False),
        ],
        footer=("If you have nothing to submit, your gameweek may not be "
                "open yet, or you are not registered as a manager. Contact "
                "an Official if you believe this is incorrect."),
    )


# Discord sizes an embed to its widest line, so a short one comes out narrow.
# Invisible characters (braille blank, which Discord does not trim as
# whitespace) on the end of the last line stretch every embed to the full
# width. If embeds still look short, raise EMBED_PAD; if the blanks wrap onto
# a line of their own on your screen, lower it.
EMBED_PAD = 46
BLANK = "⠀"


def widen(description):
    """The description with invisible padding on its last line."""
    return (description or "") + BLANK * EMBED_PAD


# Discord's own hard cap on one embed's description. Set to the real
# number rather than a conservative margin below it: _chunk_description()
# always finishes a chunk at least one character short of this figure
# (see its docstring), so a chunk can never actually reach 4096 - there is
# no scenario where sitting further below it buys any extra safety, only
# an earlier, needless split into a second embed.
DESCRIPTION_CHUNK = 4096 - EMBED_PAD   # leaves room for widen()'s padding


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
    size = (len(embed.description) + EMBED_PAD + len(embed.title or "")
            + len(embed.footer or ""))
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

STATUS_LABEL = {
    "FULLY_CONFIRMED": "Fully confirmed",
    "SCHEDULED": "Scheduled, needs officials",
    "NEEDS_MANUAL_REF": "Needs a referee",
    "NEEDS_MANUAL_SCHEDULING": "Needs a kickoff time",
    "WAITING_FOR_AVAILABILITY": "Waiting for availability",
}

SOURCE_LABEL = {
    Source.MANAGER_PREFERENCES: "Both managers' picks",
    Source.AUTO_FALLBACK: "Automatic, from saved timings",
    "MANUAL": "Set by staff",
    Source.TEST: "Test mode",
}

FIELD_LIMIT = 1024   # Discord's cap on one embed field's value


def _rows_field(rows, shown=8):
    """Up to `shown` rows for a field value, with an "and N more" line, and
    never over Discord's per-field limit however long the rows get."""
    out, size = [], 0
    for row in rows[:shown]:
        if size + len(row) + 1 > FIELD_LIMIT - 40:
            break
        out.append(row)
        size += len(row) + 1
    if len(rows) > len(out):
        out.append("_...and {} more_".format(len(rows) - len(out)))
    return "\n".join(out)


def _when(fixture, slot_for):
    slot = slot_for(fixture["slot_key"]) if (slot_for and fixture["slot_key"]) else None
    if slot:
        return "{} {}".format(slot.day[:3], slot.label)
    return "no kickoff time yet"


def dashboard_summary(buckets, week, waiting_on=None, reasons=None, rosters=None,
                      slot_for=None):
    """The exception monitor: counts first, then only what needs a human,
    and for each of those who or what is blocking it. "Awaiting response: 2"
    tells staff nothing actionable; naming the manager who hasn't replied does.
    Games are named by their badges and kickoff, never by fixture number.
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
    embed = BoardEmbed(
        title="Scheduling: week of {}".format(week),
        description="\n".join("{} **{}** {}".format(icon, n, label)
                              for icon, n, label in counts),
    )

    if buckets["no_valid_time"]:
        rows = []
        for fixture in buckets["no_valid_time"]:
            row = _game(fixture)
            why = reasons.get(fixture["id"])
            rows.append("{} - {}".format(row, why) if why else row)
        embed.fields.append(("🔴 No valid time", _rows_field(rows)
                             + "\n_Fix by hand with /fixture set._", False))

    if buckets["ref_needed"]:
        rows = []
        for fixture in buckets["ref_needed"]:
            assistants = [r for r in (rosters.get(fixture["id"]) or [])
                          if r["role"] == ROLE_AR]
            tail = (" - {} already claimed".format(plural(len(assistants), "assistant"))
                    if assistants else "")
            rows.append("{} - {}{}".format(_game(fixture), _when(fixture, slot_for), tail))
        embed.fields.append(("🟠 Ref needed", _rows_field(rows)
                             + "\n_Claim it in the referee channel, or use /refs assign._",
                             False))

    if buckets["awaiting"]:
        rows = []
        for fixture in buckets["awaiting"]:
            missing = waiting_on.get(fixture["id"]) or []
            who = ", ".join("<@{}>".format(m) for m in missing) or "nobody"
            rows.append("{} - waiting on {}".format(_game(fixture), who))
        embed.fields.append(("🟡 Awaiting response", _rows_field(rows), False))

    if not any(buckets[k] for k in ("no_valid_time", "ref_needed", "awaiting")):
        embed.description += "\n\nNothing needs attention. ✅"
    return embed


def fixture_detail(fixture, history, slot=None, roster=()):
    """One game for staff: who is playing, where it stands, and the log of
    what the automation has done to it."""
    embed = BoardEmbed(
        title="Game details",
        description="{}\n**{}** vs **{}**".format(
            _game(fixture), fixture["home_team"], fixture["away_team"]),
    )
    kickoff = (discord_time(slot_datetime(fixture["week"], slot), "F")
               if slot else "Not set yet")
    embed.fields += [
        ("Status", STATUS_LABEL.get(fixture["status"], fixture["status"]), True),
        ("Kickoff", kickoff, True),
        ("Deadline", discord_time(from_iso(fixture["deadline"]), "F"), True),
    ]
    if slot:
        embed.fields.append(
            ("Chosen by", SOURCE_LABEL.get(fixture["schedule_source"],
                                           fixture["schedule_source"] or "-"), True))
    if roster:
        embed.fields.append(("Officials", _roster_line(roster), False))
    if history:
        rows = ["`{}` {}{}".format(
            entry["at"][11:16], entry["event"],
            ": {}".format(entry["detail"]) if entry["detail"] else "")
            for entry in history[-10:]]
        embed.fields.append(("Scheduling log", _rows_field(rows, shown=10), False))
    return embed
