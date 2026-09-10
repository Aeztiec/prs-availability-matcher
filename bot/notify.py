"""What the bot actually says.

Message text only - no sending, no Discord objects - so the wording can be
checked in tests and changed without touching the logic.

Times go out as Discord timestamps (<t:...:F>), which each reader's client
renders in their own timezone. The league spans several, and the whole point of
the original matcher was to stop people converting GMT in their heads.
"""

from __future__ import annotations

from .scheduling import Source
from .weeks import discord_time, from_iso, slot_datetime

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


def dashboard_summary(buckets, week):
    """The exception monitor. Counts first, then only what needs a human."""
    counts = [
        ("🟢", len(buckets["confirmed"]), "fully confirmed"),
        ("🟡", len(buckets["awaiting"]), "awaiting response"),
        ("🟠", len(buckets["ref_needed"]), "ref needed"),
        ("🔴", len(buckets["no_valid_time"]), "no valid time"),
    ]
    lines = ["# Scheduling — week of {}".format(week), ""]
    lines += ["{} **{}** {}".format(icon, n, label) for icon, n, label in counts]

    for key, icon, title in (
        ("no_valid_time", "🔴", "No valid time"),
        ("ref_needed", "🟠", "Ref needed"),
        ("awaiting", "🟡", "Awaiting response"),
    ):
        if not buckets[key]:
            continue
        lines += ["", "{} **{}**".format(icon, title)]
        for fixture in buckets[key][:10]:
            detail = fixture["slot_key"] or "unscheduled"
            lines.append("· #{}  {} vs {}  —  {}".format(
                fixture["id"], fixture["home_team"], fixture["away_team"], detail
            ))
        if len(buckets[key]) > 10:
            lines.append("· …and {} more".format(len(buckets[key]) - 10))

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
