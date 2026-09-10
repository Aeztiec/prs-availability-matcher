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
    """One line of the master list.

    The time is <t:...:t> - just the clock, localised by each reader's client.
    The day heading carries the date, so the row stays unambiguous even for a
    reader whose local day differs from the league's GMT one.
    """
    icon = STATUS_ICON.get(fixture["status"], "·")
    when = discord_time(slot_datetime(fixture["week"], slot), "t") if slot else "—"
    who = referee_name or ("<@{}>".format(fixture["referee_id"])
                           if fixture["referee_id"] else "—")
    source = SOURCE_SHORT.get(fixture.get("schedule_source"), "")
    tail = "  -# {}".format(source) if source else ""
    return "{} **{}** v **{}** · {} · ref {}{}".format(
        icon, fixture["home_team"], fixture["away_team"], when, who, tail
    )


def fixture_board(fixtures, week, slot_for, referee_names=None):
    """The whole week as one or more message bodies.

    Returns a list of strings: one per message, so a long week can be posted as
    several rather than silently truncated.
    """
    referee_names = referee_names or {}
    scheduled = [f for f in fixtures if f["slot_key"]]
    unscheduled = [f for f in fixtures if not f["slot_key"]]

    # Group the scheduled ones by day, in slot order.
    by_day = {}
    for fixture in scheduled:
        slot = slot_for(fixture["slot_key"])
        if not slot:
            unscheduled.append(fixture)
            continue
        by_day.setdefault((slot.day_index, slot.day), []).append((slot, fixture))

    saturday = week_saturday(week)
    header = "# Fixtures — week of {}".format(saturday.strftime("%d %B %Y"))

    lines = [header, ""]
    for (day_index, day), rows in sorted(by_day.items()):
        rows.sort(key=lambda pair: pair[0].minutes)
        offset = {"friday": -1, "saturday": 0, "sunday": 1}.get(day.lower(), 0)
        date = saturday + timedelta(days=offset)
        lines.append("**{} {}**".format(day, date.strftime("%d %b")))
        for slot, fixture in rows:
            lines.append(board_row(
                fixture, slot, referee_names.get(fixture["referee_id"])
            ))
        lines.append("")

    if unscheduled:
        lines.append("**Not yet scheduled**")
        for fixture in unscheduled:
            lines.append("{} **{}** v **{}**".format(
                STATUS_ICON.get(fixture["status"], "·"),
                fixture["home_team"], fixture["away_team"],
            ))
        lines.append("")

    if not scheduled and not unscheduled:
        lines.append("_No fixtures for this week yet._")
        lines.append("")

    lines.append("-# ✅ confirmed · 🟠 needs a ref · 🔴 needs scheduling · "
                 "🟡 awaiting managers")
    lines.append("-# Times show in your own timezone.")

    return _chunk(lines, header)


def _chunk(lines, header):
    """Split rendered lines into message-sized pieces, never mid-row."""
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
