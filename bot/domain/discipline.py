"""Suspensions, worked out from the results that have been posted.

The rules, per competition:

  * A red card means the player misses their club's next game in that
    competition.
  * A second yellow in one game counts as a red.
  * Yellow cards in two consecutive games of a competition mean the player
    misses the next one. A game with no yellow breaks the run, and serving a
    suspension starts the count again.

"Competition" here is domestic or UEFA: a domestic red (or yellows) is served
in the next domestic game and leaves the UEFA game alone, and the other way
round. Every UEFA round is treated as one competition.

Nothing is stored about a suspension. It is recomputed from the saved results
each time, so submitting a corrected result quietly corrects the suspensions
that came from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import season

DOMESTIC = "DOMESTIC"
UEFA = "UEFA"

RED = "RED"
SECOND_YELLOW = "SECOND_YELLOW"
YELLOWS = "YELLOWS"


def competition_group(league):
    """Which competition a fixture counts as. A fixture with no league (one
    made by hand) is treated as domestic."""
    if not league or league in season.LEAGUES:
        return DOMESTIC
    return UEFA


@dataclass
class Standing:
    """Where one player stands after the games that have results.

    game      the fixture they miss, or None
    kind      RED, SECOND_YELLOW or YELLOWS while a suspension is pending or live
    triggers  the fixture(s) whose cards caused it
    carries   True when the ban is real but the club's next game is not in the
              calendar yet
    played_while_suspended  fixtures where they were listed despite the ban
    """

    game: int | None = None
    kind: str | None = None
    triggers: list = field(default_factory=list)
    carries: bool = False
    played_while_suspended: list = field(default_factory=list)


def evaluate(games, resulted, cards, appeared):
    """Work a player's discipline through their club's games in one competition.

    games     fixture ids, in the order they are played
    resulted  fixture ids that have a posted result
    cards     {fixture_id: (yellows, reds)}
    appeared  fixture ids the player was listed in
    """
    ban = None            # (kind, triggers) while the next game is a suspension
    yellow_in = None      # fixture of the previous game's single yellow
    violations = []

    for fixture_id in games:
        if fixture_id not in resulted:
            if ban:
                return Standing(fixture_id, ban[0], ban[1], False, violations)
            return Standing(played_while_suspended=violations)

        yellows, reds = cards.get(fixture_id, (0, 0))
        if ban:
            # This game is the suspension. Being listed in it is a breach.
            if fixture_id in appeared:
                violations.append(fixture_id)
            ban, yellow_in = None, None
            continue

        if reds:
            ban = (RED, [fixture_id])
        elif yellows >= 2:
            ban = (SECOND_YELLOW, [fixture_id])
        elif yellows == 1:
            if yellow_in is not None:
                ban = (YELLOWS, [yellow_in, fixture_id])
            else:
                yellow_in = fixture_id
        else:
            yellow_in = None
        if ban:
            yellow_in = None

    if ban:
        return Standing(None, ban[0], ban[1], True, violations)
    return Standing(played_while_suspended=violations)


# --------------------------------------------------------------------------
# Working from the database
# --------------------------------------------------------------------------

def club_games(store, club, group):
    """A club's fixtures in one competition, in the order they are played."""
    games = [f for f in store.fixtures()
             if club in (f["home_team"], f["away_team"])
             and competition_group(f.get("league")) == group]
    return sorted(games, key=lambda f: (f["week"], f["id"]))


def _histories(store, club):
    """{username: ({fid: (yellows, reds)}, {fid, ...})} from the stored results."""
    people = {}
    for row in store.result_players(club):
        cards, appeared = people.setdefault(row["username"], ({}, set()))
        cards[row["fixture_id"]] = (row["yellows"], row["reds"])
        appeared.add(row["fixture_id"])
    return people


def standings(store, club, group):
    """{username: Standing} for everyone with a card on record at this club in
    this competition. Players with clean sheets are left out - they cannot be
    suspended."""
    games = club_games(store, club, group)
    ids = [g["id"] for g in games]
    resulted = store.resulted_fixture_ids()
    out = {}
    for username, (cards, appeared) in _histories(store, club).items():
        mine = {fid: c for fid, c in cards.items() if fid in ids}
        if not any(y or r for y, r in mine.values()):
            continue
        out[username] = evaluate(ids, resulted, mine, appeared & set(ids))
    return out


def suspended_for(store, fixture):
    """[{username, club, standing}] for everyone who must miss this fixture."""
    group = competition_group(fixture.get("league"))
    found = []
    for club in (fixture["home_team"], fixture["away_team"]):
        for username, standing in sorted(standings(store, club, group).items()):
            if standing.game == fixture["id"]:
                found.append({"username": username, "club": club, "standing": standing})
    return found


def next_game(store, club, fixture):
    """The club's next fixture in the same competition after `fixture`, or None."""
    group = competition_group(fixture.get("league"))
    games = club_games(store, club, group)
    ids = [g["id"] for g in games]
    if fixture["id"] not in ids:
        return None
    following = games[ids.index(fixture["id"]) + 1:]
    return following[0] if following else None


def breaches(store, fixture):
    """[{username, club}] listed in this fixture's result while suspended."""
    group = competition_group(fixture.get("league"))
    found = []
    for club in (fixture["home_team"], fixture["away_team"]):
        for username, standing in sorted(standings(store, club, group).items()):
            if fixture["id"] in standing.played_while_suspended:
                found.append({"username": username, "club": club})
    return found


def current(store):
    """Everyone suspended for a game still to come, as
    [{username, club, group, standing}] ordered by club."""
    clubs = sorted({row["club"] for row in store.result_players()})
    found = []
    for club in clubs:
        for group in (DOMESTIC, UEFA):
            for username, standing in sorted(standings(store, club, group).items()):
                if standing.game is not None or standing.carries:
                    found.append({"username": username, "club": club,
                                  "group": group, "standing": standing})
    return found
