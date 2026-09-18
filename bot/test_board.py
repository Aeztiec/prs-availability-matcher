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

import bot.notify as notify_module
from bot.db import Store
from bot.notify import (
    board_digest, board_row, dashboard_summary, fixture_board,
    fixture_picker,
)
from bot.orchestrator import dashboard
from bot.scheduling import Source, Status
from bot.slots import Slot, clean_day, slot_key

FAILURES = []
DISCORD_LIMIT = 2000
# An embed description's own limit, separate from (and much bigger than) the
# 2000-character cap on a plain message's content.
EMBED_DESC_LIMIT = 4096


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
            store.add_referee(900 + (n % 5), "Ref {}".format(900 + (n % 5)))
            store.claim_referee(fid, 900 + (n % 5), "REF")
            store.set_status(fid, Status.FULLY_CONFIRMED)
        ids.append(fid)
    return ids


def roster_of(store, fixtures):
    return {f["id"]: store.fixture_referees(f["id"]) for f in fixtures}


# --------------------------------------------------------------------------
print("\none row of the master list")
# --------------------------------------------------------------------------
store = fresh_store()
fid = build_week(store, 1)[0]
fixture = store.fixture(fid)
row = board_row(fixture, slot_for(fixture["slot_key"]))
check("names both teams", "TEAM 0 FC" in row and "OPPO 0 FC" in row, True)
check("uses the vs format", " *vs* " in row, True)
check("localised timestamp", "<t:" in row and ":F>" in row, True)

print("\nthe referee never appears on this board, staffed or not")
# That lives on the referee board and nowhere else - a manager should see the
# same row shape regardless of who, if anyone, is reffing their game.
check("no referee shown even once one is fully staffed", "-#" in row, False)

store = fresh_store()
fid = build_week(store, 1, confirmed=False)[0]
unreffed = board_row(store.fixture(fid), slot_for(store.fixture(fid)["slot_key"]))
check("nor when nobody has claimed it yet", "-#" in unreffed, False)

# --------------------------------------------------------------------------
print("\na realistic 40-fixture week fits Discord's limits")
# --------------------------------------------------------------------------
store = fresh_store()
build_week(store, 40)
bodies = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for)
check("every embed's description within its 4096-char limit",
      all(len(b.description) <= EMBED_DESC_LIMIT for b in bodies), True)
print("       (split into {} embed(s), longest description {} chars)".format(
    len(bodies), max(len(b.description) for b in bodies)))
check("all 40 fixtures present",
      sum(b.description.count(" *vs* ") for b in bodies), 40)

print("\nan 80-fixture week still splits rather than truncating")
store = fresh_store()
build_week(store, 80)
big = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for)
check("within limits", all(len(b.description) <= EMBED_DESC_LIMIT for b in big), True)
check("nothing dropped", sum(b.description.count(" *vs* ") for b in big), 80)

# --------------------------------------------------------------------------
print("\ngrouped by division, titled, with the deadline")
# --------------------------------------------------------------------------
from bot import season as _season

store = fresh_store()
build_week(store, 10)
gw1 = _season.gameweek("GW1")
grouped_embeds = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for,
                               gameweek=gw1, deadline=gw1.deadline)
grouped = "\n".join(b.description for b in grouped_embeds)
check("titled with the season and gameweek",
      "**PRS SEASON 17 GAMEWEEK 1**" in grouped, True)
check("grouped by division", "**__Premier League:__**" in grouped, True)
check("every division present",
      all("**__{}:__**".format(n) in grouped for n in _season.LEAGUES.values()), True)
check("divisions in a fixed order",
      grouped.index("Premier League:") < grouped.index("Bundesliga:"), True)
check("the deadline is a field on the last embed",
      any(name == "__Scheduling Deadline:__" for name, _, _ in grouped_embeds[-1].fields),
      True)
check("deadline and extension both stack full-width, not side by side",
      all(inline is False for _, _, inline in grouped_embeds[-1].fields), True)
check("says it updates itself, in the footer",
      "updates itself" in (grouped_embeds[-1].footer or ""), True)

print("\nan undecided fixture shows a placeholder rather than being dropped")
store = fresh_store()
build_week(store, 2)
store.create_fixture("S17_Clubs", WEEK, "LATE FC", "SLOW FC", 5, 6,
                     "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY,
                     league="PL")
mixed = "\n".join(b.description for b in
                  fixture_board(store.fixtures(week=WEEK), WEEK, slot_for))
check("shows a TBD placeholder", "`TBD`" in mixed, True)
check("and is still listed", "LATE FC" in mixed, True)

print("\nan empty week says so instead of rendering a bare heading")
check("empty week", "No fixtures for this gameweek yet" in
      "\n".join(b.description for b in fixture_board([], WEEK, slot_for)), True)

print("\na fixture whose slot the sheet no longer offers is not silently dropped")
store = fresh_store()
fid = store.create_fixture("S17_Clubs", WEEK, "OLD FC", "GONE FC", 7, 8,
                           "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.set_schedule(fid, "sat_1630", Source.MANAGER_PREFERENCES, Status.SCHEDULED)
orphan = "\n".join(b.description for b in
                   fixture_board(store.fixtures(week=WEEK), WEEK, slot_for))
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
store.add_referee(901, "Ref 901")
store.claim_referee(needs_ref, 901, "AR")   # an assistant claimed it, but no referee yet

stuck = store.create_fixture("S17_Clubs", WEEK, "S1 FC", "S2 FC", 41, 42,
                             "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.set_status(stuck, Status.NEEDS_MANUAL_SCHEDULING, "no overlapping availability")

waiting = store.create_fixture("S17_Clubs", WEEK, "W1 FC", "W2 FC", 51, 52,
                               "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY)
store.save_submission(waiting, 51, {"sat_1800": 2}, submitted=True)   # only home

buckets = dashboard(store, week=WEEK)
waiting_on = {f["id"]: store.unsubmitted_managers(f["id"]) for f in store.fixtures(week=WEEK)}
reasons = {stuck: "no overlapping availability"}
rosters = roster_of(store, store.fixtures(week=WEEK))
body = dashboard_summary(buckets, WEEK, waiting_on, reasons, rosters)

check("counts confirmed", "**3** fully confirmed" in body, True)
check("counts awaiting", "**1** awaiting response" in body, True)
check("counts ref needed", "**1** ref needed" in body, True)
check("counts no valid time", "**1** no valid time" in body, True)
check("names the manager who hasn't replied", "<@52>" in body, True)
check("does not chase the one who did", "<@51>" not in body, True)
check("gives the blocking reason", "no overlapping availability" in body, True)
check("says how many assistants were already claimed",
      "1 assistant(s) already claimed" in body, True)
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
check("its own embed, titled for the managers",
      cta.title, "Managers, submit your timings")
check("names the gameweek", "Gameweek 1" in cta.description, True)
check("says it opens privately", "privately" in cta.description, True)
check("explains the three states",
      all(w in cta.description for w in ("ideal", "fine", "no")), True)
check("the deadline is its own field", cta.fields[0][0], "Deadline")
check("stated as a localised timestamp", "<t:" in cta.fields[0][1], True)
check("tells a non-manager what to do, in the footer",
      "Contact an Official" in cta.footer, True)
check("fits an embed's limit", len(cta.description) <= EMBED_DESC_LIMIT, True)

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
real = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for,
                     gameweek=gw1, deadline=gw1.deadline)
check("every embed within the limit",
      all(len(b.description) <= EMBED_DESC_LIMIT for b in real), True)
check("all 20 rendered", sum(b.description.count(" *vs* ") for b in real), 20)
check("half still say TBD",
      sum(b.description.count("`TBD`") for b in real), 10)
print("       ({} embed(s), longest description {} chars)".format(
    len(real), max(len(b.description) for b in real)))

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
    badged = fixture_board(store.fixtures(week=WEEK), WEEK, slot_for,
                           gameweek=gw1, deadline=gw1.deadline)
    check("still within the limit with all 40 badges",
          all(len(b.description) <= EMBED_DESC_LIMIT for b in badged), True)
    check("nothing dropped when it splits",
          sum(b.description.count(" *vs* ") for b in badged), 20)
    check("badges actually rendered",
          "<:ARS:" in "\n".join(b.description for b in badged), True)
    print("       ({} embed(s), longest description {} chars)".format(
        len(badged), max(len(b.description) for b in badged)))
finally:
    _season.TEAM_EMOJI = real_emoji

print("\ndivision and season emoji")
real_league = dict(_season.LEAGUE_EMOJI)
real_season = _season.SEASON_EMOJI
_season.LEAGUE_EMOJI = {"PL": "<:PL:123>"}
_season.SEASON_EMOJI = "<:PRS:456>"
try:
    decorated = "\n".join(b.description for b in
                          fixture_board(store.fixtures(week=WEEK), WEEK, slot_for,
                                       gameweek=gw1, deadline=gw1.deadline))
    check("season emoji follows the heading",
          "GAMEWEEK 1**  <:PRS:456>" in decorated, True)
    check("league emoji follows its division",
          "**__Premier League:__**  <:PL:123>" in decorated, True)
    check("a division without one has no trailing space",
          "**__Bundesliga:__**\n" in decorated, True)
finally:
    _season.LEAGUE_EMOJI = real_league
    _season.SEASON_EMOJI = real_season

print("\nthe deadline fields and footer stay on the last embed, even split")
footer_store = fresh_store()
for n, (h, a, lg) in enumerate(_season.FIXTURES["GW1"]):
    fid = footer_store.create_fixture("S17_Clubs", WEEK, _season.team_name(h),
                                      _season.team_name(a), 1000 + n, 2000 + n,
                                      "2026-09-11T18:00:00Z",
                                      Status.WAITING_FOR_AVAILABILITY, league=lg)
    footer_store.set_schedule(fid, SLOTS[n % len(SLOTS)].key,
                              Source.MANAGER_PREFERENCES, Status.SCHEDULED)
saved_chunk = notify_module.DESCRIPTION_CHUNK
notify_module.DESCRIPTION_CHUNK = 500   # force a split with just 20 fixtures
try:
    split = fixture_board(footer_store.fixtures(week=WEEK), WEEK, slot_for,
                          gameweek=gw1, deadline=gw1.deadline)
    check("actually splits into more than one embed", len(split) > 1, True)
    check("only the last embed carries the deadline fields",
          all(e.fields == [] for e in split[:-1]), True)
    check("the last embed has the deadline field",
          any(name == "__Scheduling Deadline:__" for name, _, _ in split[-1].fields), True)
    check("only the last embed carries the footer",
          all(e.footer is None for e in split[:-1]), True)
    check("the last embed's footer explains the deadline",
          "Officials set the time" in split[-1].footer, True)
    check("and mentions the timezone",
          "your own timezone" in split[-1].footer, True)
finally:
    notify_module.DESCRIPTION_CHUNK = saved_chunk

print("\nthe TBD placeholder")
tbd_store = fresh_store()
tbd_store.create_fixture("S17_Clubs", WEEK, "WAITING FC", "PENDING FC", 61, 62,
                         "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY,
                         league="PL")
tbd = "\n".join(b.description for b in
                fixture_board(tbd_store.fixtures(week=WEEK), WEEK, slot_for))
check("undecided fixtures read TBD", "`TBD`" in tbd, True)
check("the old long-dash placeholder is gone", "to be decided" in tbd, False)

check("a team without a badge falls back to its name",
      _season.label_for("NO SUCH TEAM"), "NO SUCH TEAM")
check("a team with one shows only the badge",
      _season.label_for("ARSENAL"), _season.TEAM_EMOJI["ARSENAL"])

# --------------------------------------------------------------------------
print("\nthe fixture picker, shown when a manager has more than one game open")
# --------------------------------------------------------------------------
picker_store = fresh_store()
picker_ids = []
for n, (h, a, lg) in enumerate(_season.FIXTURES["GW1"][:6]):
    fid = picker_store.create_fixture(
        "S17_Clubs", WEEK, _season.team_name(h), _season.team_name(a),
        900 + n, 901 + n, "2026-09-11T18:00:00Z",
        Status.WAITING_FOR_AVAILABILITY, league=lg,
    )
    picker_ids.append(fid)

picker = fixture_picker(picker_store.fixtures(week=WEEK))
check("grouped by division, like the announcement",
      "**__Premier League:__**" in picker and "**__Bundesliga:__**" in picker, True)
check("uses the same vs style as the announcement", " *vs* " in picker, True)
check("gives the command for each fixture",
      all("`/availability fixture:{}`".format(fid) in picker for fid in picker_ids),
      True)
check("no plain bullet points left over from the old format", "· **" not in picker, True)
check("divisions in the same fixed order as the announcement",
      picker.index("Premier League:") < picker.index("Bundesliga:"), True)

print("\na single fixture still renders correctly")
solo = fixture_picker(picker_store.fixtures(week=WEEK)[:1])
check("one row, one division heading", solo.count("*vs*"), 1)

print("\nan empty list produces an empty string rather than a stray heading")
check("nothing to show", fixture_picker([]), "")

print("\na fixture with no league falls back to a catch-all group")
loose = picker_store.create_fixture(
    "S17_Clubs", WEEK, "TEAM Z FC", "TEAM Y FC", 950, 951,
    "2026-09-11T18:00:00Z", Status.WAITING_FOR_AVAILABILITY,
)
leagueless = fixture_picker([picker_store.fixture(loose)])
check("still lists the fixture", "TEAM Z FC" in leagueless, True)

print("\na manager with dozens of open fixtures never crashes the reply")
# What actually happened live: the same two test accounts were left managing
# every team across three still-open gameweeks (season.seed_managers only
# guarantees no shared manager within the one gameweek it is run for), so a
# real account ended up with 47 open fixtures. Discord rejects any message
# over 2000 characters outright, and fixture_picker had no cap at all - the
# interaction failed with a 400 and the manager got no reply whatsoever.
big_store = fresh_store()
big_fixtures = []
for week_n in range(3):                       # three open gameweeks worth
    for n, (h, a, lg) in enumerate(_season.FIXTURES["GW1"]):
        fid = big_store.create_fixture(
            "S17_Clubs", WEEK, _season.team_name(h), _season.team_name(a),
            800 + week_n * 100 + n, 700, "2026-09-11T18:00:00Z",
            Status.WAITING_FOR_AVAILABILITY, league=lg,
        )
        big_fixtures.append(big_store.fixture(fid))
check("reproduces the live scale", len(big_fixtures), 60)

big_picker = fixture_picker(big_fixtures)
full_reply = ("You have more than one fixture open. Run the command under "
             "the one you want to set:\n\n" + big_picker)
check("stays comfortably under Discord's 2000-character limit",
      len(full_reply) <= DISCORD_LIMIT, True)
check("says how many were left off", "more" in big_picker, True)
check("still names at least one real fixture", "`/availability fixture:" in big_picker, True)

print("\nthe cap holds even at absurd scale")
huge = big_fixtures * 5                        # 300 fixtures, one account
huge_reply = ("You have more than one fixture open. Run the command under "
             "the one you want to set:\n\n" + fixture_picker(huge))
check("still under the limit at 300 fixtures", len(huge_reply) <= DISCORD_LIMIT, True)

# --------------------------------------------------------------------------
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all board and dashboard tests passed")
