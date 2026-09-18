"""Offline checks on the referee claim menu.

Same reasoning as test_views.py: the callback needs a gateway, but a custom_id
that doesn't match its own routing template produces a select that looks fine
and does nothing when used. Here that would mean a referee picking a game and
never actually being added to it, so it is worth pinning down without Discord.

Run with:  python -m bot.test_ref_views
"""

from __future__ import annotations

import sys

from bot.ref_views import REF_DYNAMIC_ITEMS, ClaimSelect, MAX_OPTIONS, claim_options
from bot.slots import Slot, clean_day, slot_key
from bot import season

FAILURES = []


def check(name, got, want):
    if got == want:
        print("  ok   {}".format(name))
    else:
        print("  FAIL {}\n         got:  {!r}\n         want: {!r}".format(name, got, want))
        FAILURES.append(name)


WEEK = "2026-09-19"


def slot(day, day_index, hour):
    minutes = hour * 60
    return Slot(key=slot_key(day, minutes), day=clean_day(day), day_index=day_index,
               clock="{}:00 PM".format(hour - 12 if hour > 12 else hour), minutes=minutes,
               low_priority=False)


SAT_1800 = slot("Saturday", 0, 18)
SUN_1700 = slot("Sunday", 1, 17)
SLOTS = {SAT_1800.key: SAT_1800, SUN_1700.key: SUN_1700}


def slot_for(key):
    return SLOTS[key]


def fixture(fid, slot_key, home="ABC FC", away="XYZ FC"):
    return {"id": fid, "home_team": home, "away_team": away, "slot_key": slot_key}


print("\nclaim_options: no-referee-at-all games sort before assistant-only ones")
pending = [
    (fixture(2, SAT_1800.key), [{"referee_id": 900, "role": "REF"}]),  # only needs AR
    (fixture(1, SUN_1700.key), []),                                    # needs everything
]
options, disabled = claim_options(pending, slot_for, WEEK)
check("fixture 1 (fully open) listed first",
      [o.value for o in options], ["1", "2"])
check("not disabled when there's something to claim", disabled, False)
check("label names the open role", "(Referee open)" in options[0].label, True)
check("the other option needs an assistant", "(Assistant open)" in options[1].label, True)
check("label stays within Discord's 100-char cap",
      all(len(o.label) <= 100 for o in options), True)
check("value is the fixture id", options[0].value, "1")

print("\nsame urgency ties break by kickoff time")
early = fixture(3, SAT_1800.key)   # 18:00
late = fixture(4, SUN_1700.key)    # next day
tied = [(late, []), (early, [])]
tied_options, _ = claim_options(tied, slot_for, WEEK)
check("earlier kickoff sorts first", [o.value for o in tied_options], ["3", "4"])

print("\nan empty roster produces a single disabled placeholder")
empty_options, empty_disabled = claim_options([], slot_for, WEEK)
check("one placeholder option", len(empty_options), 1)
check("disabled so it can't be picked", empty_disabled, True)
check("placeholder value is the sentinel", empty_options[0].value, "none")

print("\na real team shows its short code, not the full sheet name")
real = [(fixture(5, SAT_1800.key, season.team_name("FRA"), season.team_name("RBL")), [])]
real_options, _ = claim_options(real, slot_for, WEEK)
check("plain short codes, not the full names",
      "FRA vs RBL" in real_options[0].label, True)
check("the full sheet names are gone from the label",
      "EINTRACHT" not in real_options[0].label, True)

print("\na team with no known code just keeps its name")
check("unmapped team falls back to its full name",
      "ABC FC vs XYZ FC" in options[0].label, True)

print("\nmore than 25 open games is capped, not rejected")
many = [(fixture(100 + n, SAT_1800.key), []) for n in range(30)]
capped, capped_disabled = claim_options(many, slot_for, WEEK)
check("capped at Discord's limit", len(capped), MAX_OPTIONS)
check("still enabled", capped_disabled, False)

# --------------------------------------------------------------------------
print("\nthe select's custom_id routes back to itself")
# --------------------------------------------------------------------------
select = ClaimSelect(WEEK, options, disabled)
check("custom_id carries the week", select.custom_id, "refclaim:2026-09-19")
check("routes", bool(ClaimSelect.__discord_ui_compiled_template__.fullmatch(select.custom_id)),
      True)
check("captured week survives the round trip",
      ClaimSelect.__discord_ui_compiled_template__.fullmatch(select.custom_id)["week"],
      "2026-09-19")
check("a non-matching id is rejected",
      bool(ClaimSelect.__discord_ui_compiled_template__.fullmatch("refclaim:")), False)

print("\nit's registered as a dynamic item")
check("exactly one dynamic item for referees", REF_DYNAMIC_ITEMS, (ClaimSelect,))

print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all referee view checks passed")
