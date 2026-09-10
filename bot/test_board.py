"""Tests for the master fixture list and the staff dashboard.

The length checks matter most. Discord rejects a message over 2000 characters
outright, so a 40-fixture week that renders to one oversized body would fail to
post at all - and it would only fail once the league got big, which is exactly
when nobody has time to debug it.

Run with:  python -m bot.test_board
"""

from __future__ import annotations

import os
import random
import sys
import tempfile

from bot.db import Store
from bot.notify import (
    BOARD_CHUNK, board_digest, board_row, dashboard_summary, fixture_board,
)
from bot.orchestrator import dashboard
from bot.scheduling import Source, Status
from bot.slots import Slot, clean_day, slot_key

FAILURES = []
DISCORD_LIMIT = 2000


def check(name, got, want):
    if got == want:
        print("  ok   {}".format(name))
    else:
        print("  FAIL {}\n         got:  {!r}\n         want: {!r}".format(name, got, want))
        FAILURES.append(name)


WEEK = "2026-09-12"


def make_slots():
    out = []
    for day_index, day in enumerate(["Saturday", "Sunday", "Friday (Low Priority)"]):
        for hour in range(16, 23):
            minutes = hour * 60
            out.append(Slot(
                key=slot_key(day, minutes), day=clean_day(day), day_index=day_index,
                clock="{}:00 PM".format(hour % 12 or 12), minutes=minutes,
                low_priority=(day_index == 2),
            ))
    return out


SLOTS = make_slots()
BY_KEY = {s.key: s for s in SLOTS}


def slot_for(key):
    return BY_KEY.get(key)


tmp = tempfile.mkdtemp()


def fresh_store():
    return Store(os.path.join(tmp, "board{}.db".format(random.randint(0, 10**9))))


def build_week(store, count, confirmed=True):
    """`count` fixtures spread across the week's slots."""
    ids = []
    for n in range(count):
        slot = SLOTS[(n % 3) * 7 + (n // 3) % 7]   # interleave the three days
        fid = store.create_fixture(
            "S17_Clubs", WEEK, "TEAM {} FC".format(n), "OPPO {} FC".format(n),
            1000 + n, 2000 + n, "2026-09-11T18:00:00Z",
            Status.WAITING_FOR_AVAILABILITY,
            league=["PL", "BL", "LL", "SA", "L1"][n % 5],
        )
        store.set_schedule(fid, slot.key, Source.MANAGER_PREFERENCES, Status.SCHEDULED)
        if confirmed:
            store.set_referee(fid, 900 + (n % 5))
            store.set_status(fid, Status.FULLY_CONFIRMED)
        ids.append(fid)
    return ids


# --------------------------------------------------------------------------
print("\none row of the master list")
# --------------------------------------------------------------------------
store = fresh_store()
fid = build_week(store, 1)[0]
fixture = store.fixture(fid)
row = board_row(fixture, slot_for(fixture["slot_key"]), referee_name="Ref One")
check("names both teams", "TEAM 0 FC" in row and "OPPO 0 FC" in row, True)
check("uses the vs format", " *vs* " in row, True)
check("localised timestamp", "<t:" in row and ":F>" in row, True)
check("names the referee", "Ref One" in row, True)

print("\nan unreffed fixture shows a dash rather than a blank")
store = fresh_store()
fid = build_week(store, 1, confirmed=False)[0]
unreffed = board_row(store.fixture(fid), slot_for(store.fixture(fid)["slot_key"]))
check("no ref shown until one is assigned", "-#" in unreffed, False)

# --------------------------------------------------------------------------
print("\na realistic 40-fixture week fits Discord's limits")
# --------------------------------------------------------------------------
store = fresh_store()
build_week(store, 40)
bodies = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for)
check("every body within Discord's 2000 chars",
      all(len(b) <= DISCORD_LIMIT for b in bodies), True)
check("longest body", max(len(b) for b in bodies) <= DISCORD_LIMIT, True)
print("       (split into {} message(s), longest {} chars)".format(
    len(bodies), max(len(b) for b in bodies)))
check("all 40 fixtures present",
      sum(b.count(" *vs* ") for b in bodies), 40)
check("every continuation message still fits",
      all(len(b) <= DISCORD_LIMIT for b in bodies[1:]), True)

print("\nan 80-fixture week still splits rather than truncating")
store = fresh_store()
build_week(store, 80)
big = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for)
check("within limits", all(len(b) <= DISCORD_LIMIT for b in big), True)
check("nothing dropped", sum(b.count(" *vs* ") for b in big), 80)

# --------------------------------------------------------------------------
print("\ngrouped by division, titled, with the deadline")
# --------------------------------------------------------------------------
from bot import season as _season

store = fresh_store()
build_week(store, 10)
gw1 = _season.gameweek("GW1")
grouped = "\n".join(fixture_board(store.fixtures(week=WEEK), WEEK, slot_for,
                                 gameweek=gw1, deadline=gw1.deadline))
check("titled with the season and gameweek",
      "**__PRS SEASON 17 GAMEWEEK 1:__**" in grouped, True)
check("grouped by division", "**__Premier League:__**" in grouped, True)
check("every division present",
      all("**__{}:__**".format(n) in grouped for n in _season.LEAGUES.values()), True)
check("divisions in a fixed order",
      grouped.index("Premier League:") < grouped.index("Bundesliga:"), True)
check("the deadline is stated", "SCHEDULING DEADLINE" in grouped, True)
check("says it updates itself", "updates itself" in grouped, True)

print("\nan undecided fixture shows a placeholder rather than being dropped")
store = fresh_store()
build_week(store, 2)
store.create_fixture("S17_Clubs", WEEK, "LATE FC", "SLOW FC", 5, 6,
                     "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY,
                     league="PL")
mixed = "\n".join(fixture_board(store.fixtures(week=WEEK), WEEK, slot_for))
check("shows a TBD placeholder", "`TBD`" in mixed, True)
check("and is still listed", "LATE FC" in mixed, True)

print("\nan empty week says so instead of rendering a bare heading")
check("empty week", "No fixtures for this gameweek yet" in
      "\n".join(fixture_board([], WEEK, slot_for)), True)

print("\na fixture whose slot the sheet no longer offers is not silently dropped")
store = fresh_store()
fid = store.create_fixture("S17_Clubs", WEEK, "OLD FC", "GONE FC", 7, 8,
                           "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.set_schedule(fid, "sat_1630", Source.MANAGER_PREFERENCES, Status.SCHEDULED)
orphan = "\n".join(fixture_board(store.fixtures(week=WEEK), WEEK, slot_for))
check("still shown", "OLD FC" in orphan, True)

# --------------------------------------------------------------------------
print("\nthe digest skips no-op edits")
# --------------------------------------------------------------------------
store = fresh_store()
build_week(store, 5)
first = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for)
check("stable for identical content", board_digest(first),
      board_digest(fixture_board(store.fixtures(week=WEEK), WEEK, slot_for)))
store.create_fixture("S17_Clubs", WEEK, "NEW FC", "EXTRA FC", 9, 10,
                     "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
check("changes when a fixture is added", board_digest(first) !=
      board_digest(fixture_board(store.fixtures(week=WEEK), WEEK, slot_for)), True)

# --------------------------------------------------------------------------
print("\nthe staff dashboard names who is blocking each fixture")
# --------------------------------------------------------------------------
store = fresh_store()
confirmed = build_week(store, 3)[0]

needs_ref = store.create_fixture("S17_Clubs", WEEK, "R1 FC", "R2 FC", 31, 32,
                                 "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.set_schedule(needs_ref, "sat_1800", Source.AUTO_FALLBACK, Status.SCHEDULED)
store.offer(needs_ref, 901)
store.resolve_offer(needs_ref, 901, "DECLINED")

stuck = store.create_fixture("S17_Clubs", WEEK, "S1 FC", "S2 FC", 41, 42,
                             "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.set_status(stuck, Status.NEEDS_MANUAL_SCHEDULING, "no overlapping availability")

waiting = store.create_fixture("S17_Clubs", WEEK, "W1 FC", "W2 FC", 51, 52,
                               "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.save_submission(waiting, 51, {"sat_1800": 2}, submitted=True)   # only home

buckets = dashboard(store, week=WEEK)
waiting_on = {f["id"]: store.unsubmitted_managers(f["id"]) for f in store.fixtures(week=WEEK)}
reasons = {stuck: "no overlapping availability"}
asked = {f["id"]: store.refs_already_asked(f["id"]) for f in store.fixtures(week=WEEK)}
body = dashboard_summary(buckets, WEEK, waiting_on, reasons, asked)

check("counts confirmed", "**3** fully confirmed" in body, True)
check("counts awaiting", "**1** awaiting response" in body, True)
check("counts ref needed", "**1** ref needed" in body, True)
check("counts no valid time", "**1** no valid time" in body, True)
check("names the manager who hasn't replied", "<@52>" in body, True)
check("does not chase the one who did", "<@51>" not in body, True)
check("gives the blocking reason", "no overlapping availability" in body, True)
check("says how many refs were already asked", "1 already asked" in body, True)
check("tells staff the fix for scheduling", "/fixture set" in body, True)
check("tells staff the fix for refs", "/refs assign" in body, True)
check("fits Discord's limit", len(body) <= DISCORD_LIMIT, True)

print("\na clean week says nothing needs attention")
store = fresh_store()
build_week(store, 4)
clean = dashboard_summary(dashboard(store, week=WEEK), WEEK)
check("all clear", "Nothing needs attention" in clean, True)
check("still shows the count", "**4** fully confirmed" in clean, True)

print("\na busy week truncates the lists but says how many are hidden")
store = fresh_store()
for n in range(15):
    fid = store.create_fixture("S17_Clubs", WEEK, "B{} FC".format(n), "C{} FC".format(n),
                               600 + n, 700 + n, "2026-09-11T18:00:00Z",
                               Status.WAITING_FOR_AVAILABILITY)
    store.set_status(fid, Status.NEEDS_MANUAL_SCHEDULING, "nothing overlaps")
busy = dashboard_summary(dashboard(store, week=WEEK), WEEK)
check("says how many more", "and 7 more" in busy, True)
check("still within the limit", len(busy) <= DISCORD_LIMIT, True)


# --------------------------------------------------------------------------
print("\nthe public call to action, for managers whose DMs are closed")
# --------------------------------------------------------------------------
from bot.notify import availability_call_to_action

cta = availability_call_to_action(gw1, gw1.deadline)
check("names the gameweek", "Gameweek 1" in cta, True)
check("says it opens privately", "privately" in cta, True)
check("explains the three states",
      all(w in cta for w in ("ideal", "fine", "no")), True)
check("states the deadline", "<t:" in cta, True)
check("tells a non-manager what to do", "Contact an Official" in cta, True)
check("fits Discord's limit", len(cta) <= DISCORD_LIMIT, True)

print("\nthe board tracks every message it occupies")
store = fresh_store()
build_week(store, 3)
store.set_board(WEEK, 111, [201, 202, 203], digest="abc")
check("all ids kept", store.board_message_ids(WEEK), [201, 202, 203])
check("first stays in message_id for compatibility",
      store.board(WEEK)["message_id"], 201)
store.set_board(WEEK, 111, 999)
check("a single id still works", store.board_message_ids(WEEK), [999])
check("no board -> no ids", store.board_message_ids("2099-01-01"), [])

print("\na real gameweek of 20 fixtures across 5 divisions fits")
store = fresh_store()
for n, (h, a, lg) in enumerate(_season.FIXTURES["GW1"]):
    fid = store.create_fixture("S17_Clubs", WEEK, _season.team_name(h),
                               _season.team_name(a), 1000 + n, 2000 + n,
                               "2026-09-11T18:00:00Z",
                               Status.WAITING_FOR_AVAILABILITY, league=lg)
    if n % 2 == 0:
        store.set_schedule(fid, SLOTS[n % len(SLOTS)].key,
                           Source.MANAGER_PREFERENCES, Status.SCHEDULED)
        store.add_referee(800 + n, "Referee Number {}".format(n))
        store.set_referee(fid, 800 + n)
real_names = {r["discord_id"]: r["name"] for r in store.referees(active_only=False)}
real = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for, real_names,
                     gameweek=gw1, deadline=gw1.deadline)
check("every message within the limit", all(len(b) <= DISCORD_LIMIT for b in real), True)
check("all 20 rendered", sum(b.count(" *vs* ") for b in real), 20)
check("half still say TBD",
      sum(b.count("`TBD`") for b in real), 10)
print("       ({} message(s), longest {} chars)".format(
    len(real), max(len(b) for b in real)))

# --------------------------------------------------------------------------
print("\nclub badges, and what they cost in message length")
# --------------------------------------------------------------------------
# A "<:CODE:1547614892981354567>" badge is ~28 characters, so two per row adds
# well over a thousand across a full gameweek. Worth pinning down: if all forty
# divisions get badges and that tips a message past 2000, Discord rejects it
# outright - and it would only happen once someone finished uploading them.
real_emoji = dict(_season.TEAM_EMOJI)
_season.TEAM_EMOJI = {name: "<:{}:1547614892981354567>".format(code)
                      for code, name in _season.TEAM_CODES.items()}
try:
    store = fresh_store()
    for n, (h, a, lg) in enumerate(_season.FIXTURES["GW1"]):
        fid = store.create_fixture("S17_Clubs", WEEK, _season.team_name(h),
                                   _season.team_name(a), 1000 + n, 2000 + n,
                                   "2026-09-11T18:00:00Z",
                                   Status.WAITING_FOR_AVAILABILITY, league=lg)
        store.set_schedule(fid, SLOTS[n % len(SLOTS)].key,
                           Source.MANAGER_PREFERENCES, Status.SCHEDULED)
        store.add_referee(700 + n, "Referee Longname {}".format(n))
        store.set_referee(fid, 700 + n)
    badge_names = {r["discord_id"]: r["name"] for r in store.referees(active_only=False)}
    badged = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for, badge_names,
                           gameweek=gw1, deadline=gw1.deadline)
    check("still within the limit with all 40 badges",
          all(len(b) <= DISCORD_LIMIT for b in badged), True)
    check("nothing dropped when it splits",
          sum(b.count(" *vs* ") for b in badged), 20)
    check("badges actually rendered", "<:ARS:" in "\n".join(badged), True)
    print("       ({} message(s), longest {} chars)".format(
        len(badged), max(len(b) for b in badged)))
finally:
    _season.TEAM_EMOJI = real_emoji

print("\ndivision and season emoji")
real_league = dict(_season.LEAGUE_EMOJI)
real_season = _season.SEASON_EMOJI
_season.LEAGUE_EMOJI = {"PL": "<:PL:123>"}
_season.SEASON_EMOJI = "<:PRS:456>"
try:
    decorated = "\n".join(fixture_board(store.fixtures(week=WEEK), WEEK, slot_for,
                                        gameweek=gw1, deadline=gw1.deadline))
    check("season emoji follows the heading, outside the underline",
          "GAMEWEEK 1:__**  <:PRS:456>" in decorated, True)
    check("league emoji follows its division",
          "**__Premier League:__**  <:PL:123>" in decorated, True)
    check("a division without one has no trailing space",
          "**__Bundesliga:__**\n" in decorated, True)
finally:
    _season.LEAGUE_EMOJI = real_league
    _season.SEASON_EMOJI = real_season

print("\nthe footer is never split away from its deadline")
footer_store = fresh_store()
for n, (h, a, lg) in enumerate(_season.FIXTURES["GW1"]):
    fid = footer_store.create_fixture("S17_Clubs", WEEK, _season.team_name(h),
                                      _season.team_name(a), 1000 + n, 2000 + n,
                                      "2026-09-11T18:00:00Z",
                                      Status.WAITING_FOR_AVAILABILITY, league=lg)
    footer_store.set_schedule(fid, SLOTS[n % len(SLOTS)].key,
                              Source.MANAGER_PREFERENCES, Status.SCHEDULED)
saved_badges = dict(_season.TEAM_EMOJI)
_season.TEAM_EMOJI = {name: "<:{}:1547614892981354567>".format(code)
                      for code, name in _season.TEAM_CODES.items()}
try:
    split = fixture_board(footer_store.fixtures(week=WEEK), WEEK, slot_for,
                          gameweek=gw1, deadline=gw1.deadline, mention="@everyone")
    check("splits into more than one message", len(split) > 1, True)
    tail = [b for b in split if "SCHEDULING DEADLINE" in b]
    check("the deadline block lands in exactly one message", len(tail), 1)
    check("its explanation is in the same message",
          "Officials set the time" in tail[0], True)
    check("and so is the timezone note", "your own timezone" in tail[0], True)
    check("all still within the limit",
          all(len(b) <= DISCORD_LIMIT for b in split), True)
finally:
    _season.TEAM_EMOJI = saved_badges

print("\nthe ping and the TBD placeholder")
tbd_store = fresh_store()
tbd_store.create_fixture("S17_Clubs", WEEK, "WAITING FC", "PENDING FC", 61, 62,
                         "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY,
                         league="PL")
tbd = "\n".join(fixture_board(tbd_store.fixtures(week=WEEK), WEEK, slot_for))
check("undecided fixtures read TBD", "`TBD`" in tbd, True)
check("the old long-dash placeholder is gone", "to be decided" in tbd, False)

pinged = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for,
                       gameweek=gw1, deadline=gw1.deadline, mention="@everyone")
check("the ping is spoilered on the first line",
      pinged[0].splitlines()[0], "||@everyone||")
check("a blank line separates it from the heading",
      pinged[0].splitlines()[1], "")
check("then the heading",
      pinged[0].splitlines()[2].startswith("**__PRS"), True)
check("only the first message carries it",
      all(not b.startswith("||") for b in pinged[1:]), True)
quiet = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for,
                      gameweek=gw1, deadline=gw1.deadline, mention=None)
check("no ping when unset", quiet[0].startswith("**__"), True)
check("a role mention works too",
      fixture_board(store.fixtures(week=WEEK), WEEK, slot_for,
                    mention="<@&123>")[0].splitlines()[0], "||<@&123>||")

check("a team without a badge falls back to its name",
      _season.label_for("NO SUCH TEAM"), "NO SUCH TEAM")
check("a team with one shows only the badge",
      _season.label_for("ARSENAL"), _season.TEAM_EMOJI["ARSENAL"])

# --------------------------------------------------------------------------
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all board and dashboard tests passed")
