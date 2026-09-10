"""What the bot actually says.

Message text only - no sending, no Discord objects - so the wording can be
checked in tests and changed without touching the logic.

Times go out as Discord timestamps (<t:...:F>), which each reader's client
renders in their own timezone. The league spans several, and the whole point of
the original matcher was to stop people converting GMT in their heads.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from . import season
from .scheduling import Source

from .weeks import discord_time, from_iso, slot_datetime, week_saturday

FOOTER = "-# Times show in your own timezone."


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
        "Mark every time you could play — **🟢 ideal**, **🟡 fine**, or leave it "
        "**⚪ no**. We'll pick the best time you and your opponent both agree on.",
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
        "⏰ **Reminder — {} vs {}**".format(fixture["home_team"], fixture["away_team"]),
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


def fixture_confirmed(fixture, slot, referee_name=None):
    lines = [
        "# ⚽ Fixture confirmed",
        "**{}** vs **{}**  ·  #{}".format(
            fixture["home_team"], fixture["away_team"], fixture["id"]
        ),
        "",
        _slot_line(fixture["week"], slot),
    ]
    if referee_name:
        lines.append("Referee: **{}**".format(referee_name))
    else:
        lines.append("Referee: _still being assigned_")
    if fixture.get("schedule_source") == Source.AUTO_FALLBACK:
        lines += [
            "",
            "-# Allocated automatically from your team's saved timings, because "
            "the deadline passed without both managers submitting.",
        ]
    lines += ["", FOOTER]
    return "\n".join(lines)


def referee_offer(fixture, slot, referee_id=None):
    """A referee assignment, posted in a channel and addressed to one person.

    The buttons only work for whoever was actually offered it, so posting
    publicly is safe - and it reaches referees with DMs closed, who would
    otherwise never see the assignment at all.
    """
    lines = ["## 👨‍⚖️ Referee needed"]
    if referee_id:
        lines.append("<@{}> — can you take this one?".format(referee_id))
    lines += [
        "",
        "**{}** vs **{}**  ·  #{}".format(
            fixture["home_team"], fixture["away_team"], fixture["id"]
        ),
        _slot_line(fixture["week"], slot),
        "",
        "-# Only the referee named above can use these buttons.",
    ]
    return "\n".join(lines)


def referee_confirmed(fixture, slot, referee_id=None):
    """Posted when a referee is locked in, addressed to them by mention."""
    lines = ["## 👨‍⚖️ Referee confirmed"]
    if referee_id:
        lines.append("<@{}> is refereeing this one.".format(referee_id))
    lines += [
        "",
        "**{}** vs **{}**  ·  #{}".format(
            fixture["home_team"], fixture["away_team"], fixture["id"]
        ),
        _slot_line(fixture["week"], slot),
        "",
        FOOTER,
    ]
    return "\n".join(lines)


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

# Discord rejects a message over 2000 characters outright. Forty fixtures at
# ~55 characters a row clears that, so the board is split across messages -
# with headroom for the continuation marker and for team names longer than the
# ones tested.
BOARD_CHUNK = 1800

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


def board_row(fixture, slot, referee_name=None):
    """One fixture line: two badges, then the kickoff.

    Badges only, no team names - that is how the league's own posts read, and
    at forty fixtures the names are what pushes a gameweek over Discord's
    message limit. A team with no badge configured falls back to its name,
    since an empty side would leave the row meaningless.
    """
    home = season.label_for(fixture["home_team"])
    away = season.label_for(fixture["away_team"])
    when = (discord_time(slot_datetime(fixture["week"], slot), "F")
            if slot else "`TBD`")
    line = "{} *vs* {} @ {}".format(home, away, when)
    if referee_name:
        line += "  -# {}".format(referee_name)
    return line


def fixture_board(fixtures, week, slot_for, referee_names=None, gameweek=None,
                  deadline=None, mention=None, competition=None):
    """The public fixture announcement, grouped by division.

    Every fixture appears, scheduled or not. It is edited in place as times get
    decided, so one message is the whole gameweek's answer - which matters more
    than it sounds: managers with DMs closed never see a DM, and this is what
    they read instead.
    """
    referee_names = referee_names or {}

    by_league = {}
    for fixture in fixtures:
        key = fixture.get("league") or "??"
        by_league.setdefault(key, []).append(fixture)

    # "PRS SEASON 17 CLUBS GAMEWEEK 1:" - the competition sits between the
    # season and the gameweek, matching how the league titles its own posts.
    parts = ["PRS", season.SEASON_LABEL]
    if competition:
        parts.append(competition.upper())
    parts.append((gameweek.label if gameweek else "FIXTURES").upper())
    header = "**__{}:__**".format(" ".join(parts))
    if season.usable_emoji(season.SEASON_EMOJI):
        header = "{}  {}".format(header, season.SEASON_EMOJI)

    # The ping sits above the heading in a spoiler: it still notifies, but
    # collapses to a grey block instead of shouting at the top of the post.
    # A blank line after it keeps the heading clear of the spoiler block.
    lines = (["||{}||".format(mention), ""] if mention else []) + [header, ""]

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
                referee_names.get(fixture["referee_id"]),
            ))
        lines.append("")

    if not fixtures:
        lines.append("_No fixtures for this gameweek yet._")
        lines.append("")

    footer = []
    if deadline is not None:
        footer += [
            "**SCHEDULING DEADLINE:**",
            discord_time(deadline, "F"),
            "-# Not agreed by then and Officials set the time from your "
            "submitted timings.",
        ]
    footer.append("-# Times show in your own timezone. This post updates itself "
                  "as fixtures are agreed.")

    return _chunk(lines, footer=footer)


def availability_call_to_action(gameweek, deadline):
    """The message posted under the announcement, with the button on it.

    Public and button-driven on purpose. A DM only reaches managers who allow
    them; a button in a channel reaches everyone, and each person who clicks it
    gets their own private selector.
    """
    return "\n".join([
        "## 📋 Managers — submit your timings",
        "",
        "Press the button below to set when **your** team can play in "
        "**{}**. It opens privately, so only you see it.".format(gameweek.label),
        "",
        "Mark every time you could play — **🟢 ideal**, **🟡 fine**, or leave it "
        "**⚪ no**. We pick the best time you and your opponent both agree on, "
        "and the fixture list above fills in by itself.",
        "",
        "You can change your answers until {}.".format(discord_time(deadline, "F")),
        "",
        "-# Nothing to submit? That means your gameweek isn't open yet, or "
        "you're not registered as a manager — ask an Official.",
    ])


def _chunk(lines, footer=()):
    """Split rendered lines into message-sized pieces, never mid-row.

    Discord rejects a message over 2000 characters outright, and a full
    gameweek of twenty fixtures across five divisions clears that once club
    badges are in - a badge is ~28 characters and there are two per row.

    `footer` is kept whole and attached to the last message, or given one of
    its own if it will not fit. Chunking it with everything else split the
    deadline away from the line explaining it, which read like a mistake.
    """
    messages = []
    current = []
    length = 0
    for line in lines:
        if length + len(line) + 1 > BOARD_CHUNK and current:
            messages.append(current)
            current = ["-# …continued"]
            length = len(current[0])
        current.append(line)
        length += len(line) + 1
    if current:
        messages.append(current)

    footer = list(footer)
    if footer:
        tail = sum(len(line) + 1 for line in footer)
        if messages and length + tail <= BOARD_CHUNK:
            messages[-1] += footer
        else:
            messages.append(["-# …continued"] + footer)

    return ["\n".join(block) for block in messages] or [""]


def board_digest(bodies):
    """A fingerprint of what was published, to skip no-op edits."""
    return hashlib.sha256("\n".join(bodies).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# the staff dashboard (spec step 16)
# --------------------------------------------------------------------------

def dashboard_summary(buckets, week, waiting_on=None, reasons=None, asked=None):
    """The exception monitor from spec step 16.

    Counts first, then only what needs a human - and for each of those, who or
    what is blocking it. "Awaiting response: 2" tells staff nothing actionable;
    naming the manager who hasn't replied does.
    """
    waiting_on = waiting_on or {}
    reasons = reasons or {}
    asked = asked or {}

    counts = [
        ("🟢", len(buckets["confirmed"]), "fully confirmed"),
        ("🟡", len(buckets["awaiting"]), "awaiting response"),
        ("🟠", len(buckets["ref_needed"]), "ref needed"),
        ("🔴", len(buckets["no_valid_time"]), "no valid time"),
    ]
    lines = ["# Scheduling — week of {}".format(week), ""]
    lines += ["{} **{}** {}".format(icon, n, label) for icon, n, label in counts]

    if buckets["no_valid_time"]:
        lines += ["", "🔴 **No valid time** — needs scheduling by hand"]
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
            been_asked = asked.get(fixture["id"]) or []
            tail = "  -# {} already asked".format(len(been_asked)) if been_asked else ""
            lines.append("· **#{}** {} v {} — {}{}".format(
                fixture["id"], fixture["home_team"], fixture["away_team"],
                fixture["slot_key"] or "no time", tail))
        lines.append("-# Fix with `/refs assign fixture:<id> user:@ref`")

    if buckets["awaiting"]:
        lines += ["", "🟡 **Awaiting response**"]
        for fixture in buckets["awaiting"][:8]:
            missing = waiting_on.get(fixture["id"]) or []
            who = ", ".join("<@{}>".format(m) for m in missing) or "nobody"
            lines.append("· **#{}** {} v {} — waiting on {}".format(
                fixture["id"], fixture["home_team"], fixture["away_team"], who))

    for key in ("no_valid_time", "ref_needed", "awaiting"):
        if len(buckets[key]) > 8:
            lines.append("-# …and {} more {}".format(len(buckets[key]) - 8,
                                                     key.replace("_", " ")))

    if not any(buckets[k] for k in ("no_valid_time", "ref_needed", "awaiting")):
        lines += ["", "Nothing needs attention. ✅"]
    return "\n".join(lines)


def fixture_detail(fixture, history, slot=None):
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
    if fixture["referee_id"]:
        lines.append("Referee: <@{}>".format(fixture["referee_id"]))

    if history:
        lines += ["", "**Scheduling log**"]
        for entry in history[-15:]:
            detail = " — {}".format(entry["detail"]) if entry["detail"] else ""
            lines.append("`{}`  {}{}".format(entry["at"][11:16], entry["event"], detail))
    return "\n".join(lines)
