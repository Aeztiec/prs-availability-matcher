"""Point every team at a couple of test accounts, so a gameweek can be run for real.

Testing the live commands needs a manager on both sides of every fixture, and
there is rarely a spare forty people. This assigns one account to every home
team in a gameweek and the other to every away team, which guarantees each
fixture in that gameweek has two different managers - assigning teams to
accounts at random would sooner or later put the same person on both sides,
and the scheduler would then be comparing someone's availability with itself.

The assignment only holds for the gameweek you name. Later gameweeks reshuffle
the pairings, so re-run it for whichever gameweek you are about to test.

    python -m bot.seed_managers GW1 537728003577348149 1394452450173386895
    python -m bot.seed_managers --clear

Ids are arguments rather than hardcoded, so nobody's Discord id ends up
committed to a public repository.
"""

from __future__ import annotations

import sys

from . import season
from .db import Store


def clear(store):
    with store._connect() as conn:
        conn.execute("DELETE FROM managers")
    print("Cleared every team-to-manager mapping.")


def seed(store, gameweek_key, home_id, away_id):
    gw = season.gameweek(gameweek_key)
    if not gw.has_fixtures:
        raise SystemExit("{} has no fixture list yet.".format(gw.key))

    assigned = {}
    for home_code, away_code, _league in gw.fixtures:
        assigned[season.team_name(home_code)] = home_id
        assigned[season.team_name(away_code)] = away_id

    for team, discord_id in assigned.items():
        store.set_manager(team, discord_id)

    # Every other team gets the home account, so /gw open does not refuse on a
    # team that is not in this gameweek.
    for name in season.TEAM_CODES.values():
        if name not in assigned:
            store.set_manager(name, home_id)

    print("Seeded {} teams for {}.".format(len(store.managers()), gw.key))
    print("")
    print("  home teams -> {}".format(home_id))
    print("  away teams -> {}".format(away_id))
    print("")
    print("{}'s fixtures, as they will be created:".format(gw.key))
    for home_code, away_code, league in gw.fixtures:
        print("  {:<3} {:<22} v {:<22} {}".format(
            league, season.team_name(home_code), season.team_name(away_code),
            "OK" if assigned[season.team_name(home_code)]
                    != assigned[season.team_name(away_code)] else "SAME PERSON",
        ))

    same = [
        (h, a) for h, a, _ in gw.fixtures
        if assigned[season.team_name(h)] == assigned[season.team_name(a)]
    ]
    if same:
        raise SystemExit("\nBROKEN: {} fixture(s) have the same manager on both "
                         "sides: {}".format(len(same), same))
    print("")
    print("Every fixture has two different managers. Safe to run /gw open {}.".format(gw.key))


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    store = Store()

    if "--clear" in argv:
        clear(store)
        return 0

    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 1

    gameweek_key, home_id, away_id = argv
    try:
        home_id, away_id = int(home_id), int(away_id)
    except ValueError:
        print("The two ids must be numeric Discord user ids.", file=sys.stderr)
        return 1
    if home_id == away_id:
        print("Give two different accounts, or every fixture would have the "
              "same manager on both sides.", file=sys.stderr)
        return 1

    seed(store, gameweek_key, home_id, away_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
