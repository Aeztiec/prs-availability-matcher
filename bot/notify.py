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
    """The opening DM: what the fixture is and by when."""
    return "\n".join([
        "# {} vs {}".format(fixture["home_team"], fixture["away_team"]),
        "Fixture **#{}** needs a kickoff time.".format(fixture["id"]),
        "",
        "Mark every time you could play — **🟢 ideal**, **🟡 fine**, or leave it "
        "**⚪ no**. We'll pick the best time you and your opponent both agree on.",
        "",
        "Deadline: {}".format(discord_time(from_iso(deadline_iso))),
        "",
        "Use **/availability** to open the selector. You can change your answers "
        "until the deadline.",
        "",
        "-# If you don't reply, we'll fall back to your team's saved timings and "
        "allocate a time from those.",
    ])


def reminder(fixture, deadline_iso, which):
    urgency = {"12h": "in about 12 hours", "2h": "in about 2 hours"}.get(
        which, "soon"
    )
    return "\n".join([
        "⏰ **Reminder — fixture #{}**".format(fixture["id"]),
        "{} vs {}".format(fixture["home_team"], fixture["away_team"]),
        "",
        "You haven't submitted your availability yet. The deadline is {} ({}).".format(
            urgency, discord_time(from_iso(deadline_iso))
        ),
        "",
        "Run **/availability** to do it now.",
        "",
        "-# Miss the deadline and we'll allocate a time from your team's saved "
        "timings instead.",
    ])


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


def referee_offer(fixture, slot):
    return "\n".join([
        "# 👨‍⚖️ Referee assignment",
        "**{}** vs **{}**  ·  #{}".format(
            fixture["home_team"], fixture["away_team"], fixture["id"]
        ),
        "",
        _slot_line(fixture["week"], slot),
        "",
        "Can you take this one?",
        "",
        FOOTER,
    ])


def referee_confirmed(fixture, slot):
    return "\n".join([
        "# 👨‍⚖️ You're refereeing",
        "**{}** vs **{}**  ·  #{}".format(
            fixture["home_team"], fixture["away_team"], fixture["id"]
        ),
        "",
        _slot_line(fixture["week"], slot),
        "",
        FOOTER,
    ])


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
    """One fixture line: "HOME vs AWAY @ <time>".

    An unscheduled fixture gets a placeholder rather than being left out, so
    the announcement is the full list from the moment it is posted and managers
    can see their own game on it before a time exists.
    """
    home = season.label_for(fixture["home_team"])
    away = season.label_for(fixture["away_team"])
    if slot:
        when = discord_time(slot_datetime(fixture["week"], slot), "F")
    else:
        when = "`  ——  to be decided  ——  `"
    line = "{} **vs** {} @ {}".format(home, away, when)
    if referee_name:
        line += "  -# ref {}".format(referee_name)
    elif slot and not fixture["referee_id"]:
        line += "  -# ref tbc"
    return line


def fixture_board(fixtures, week, slot_for, referee_names=None, gameweek=None,
                  deadline=None):
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

    title = "PRS {} {}".format(
        season.SEASON, (gameweek.label if gameweek else "Fixtures").upper()
    )
    header = "# {}:".format(title.upper())
    lines = [header, ""]

    order = list(season.LEAGUES) + sorted(k for k in by_league if k not in season.LEAGUES)
    for key in order:
        rows = by_league.get(key)
        if not rows:
            continue
        lines.append("## __{}__:".format(season.LEAGUES.get(key, key)))
        # Scheduled first, in kickoff order, then the undecided ones.
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

    if deadline is not None:
        lines.append("**SCHEDULING DEADLINE:**")
        lines.append(discord_time(deadline, "F"))
        lines.append("-# If both managers haven't agreed by then, Officials set "
                     "the time from your submitted timings.")
    lines.append("-# Times show in your own timezone. Updated automatically as "
                 "fixtures are agreed.")

    return _chunk(lines, header)


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


def _chunk(lines, header):
    """Split rendered lines into message-sized pieces, never mid-row.

    Discord rejects a message over 2000 characters outright, and a full
    gameweek of twenty fixtures across five divisions clears that once the
    division headings are in.
    """
    messages = []
    current = []
    length = 0
    for line in lines:
        if length + len(line) + 1 > BOARD_CHUNK and current:
            messages.append("\n".join(current))
            current = ["-# …continued"]
            length = len(current[0])
        current.append(line)
        length += len(line) + 1
    if current:
        messages.append("\n".join(current))
    return messages


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
