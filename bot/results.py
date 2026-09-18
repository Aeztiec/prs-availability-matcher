"""The post for a finished game: score, per-player stats, subs, MOTM, officials.

Pure text in, text out (no discord objects), like notify.py. Staff type one
player per line with short tokens after the name:

    vzcadc g g a        two goals and an assist
    danielfly gk sub    goalkeeper who came on
    gawa g3 rc          three goals and a red card

Everything is checked against the player sheet, so a mistyped username or a
player on the wrong team is reported instead of posted.
"""

from __future__ import annotations

import re

from . import season

# What each token shows. Unicode for now; swap any of these for a custom emoji
# (e.g. "<:RC:123...>") once it is uploaded to the server.
STAT_EMOJI = {
    "goal": "⚽",
    "assist": "👁️",
    "gk": "🧤",
    "sub": "🔁",
    "yellow": "🟨",
    "red": "🟥",
}

TOKENS = {
    "g": "goal", "goal": "goal", "goals": "goal",
    "a": "assist", "ast": "assist", "assist": "assist", "assists": "assist",
    "gk": "gk", "keeper": "gk",
    "sub": "sub",
    "yc": "yellow", "yellow": "yellow",
    "rc": "red", "red": "red",
}

TOKEN_HELP = "g goal, a assist, gk keeper, sub, yc yellow, rc red (g3 = three goals)"

# MOTM first, then the mentions in order.
PLACINGS = ["🏆", "🥇", "🥈", "🥉"]

_TOKEN = re.compile(r"^(\d*)([a-z]+)(\d*)$")


def esc(text):
    """Usernames like _gawa or a_b_c must not turn into italics."""
    return re.sub(r"([\\_*~`|>])", r"\\\1", text)


def parse_line(line):
    """('name', [emoji-keys...], [bad tokens]) for one 'name tok tok' line."""
    parts = line.split()
    if not parts:
        return None
    name = parts[0].lstrip("@")
    keys, bad = [], []
    for raw in parts[1:]:
        match = _TOKEN.match(raw.lower())
        if not match:
            bad.append(raw)
            continue
        lead, word, trail = match.groups()
        key = TOKENS.get(word)
        count = int(lead or trail or 1)
        if key is None or count > 9:
            bad.append(raw)
            continue
        keys.extend([key] * count)
    return name, keys, bad


def lines_of(text):
    return [l.strip() for l in (text or "").splitlines() if l.strip()]


def _emojis(keys):
    return " ".join(STAT_EMOJI[k] for k in keys)


def _row(team, name, extra=""):
    return "{} | {}{}".format(season.label_for(team), esc(name), " " + extra if extra else "")


def header(fixture, competition=None, round_name=None):
    """'<logo> **| PRS SEASON 17 CLUBS | GAMEWEEK 1 | UEFA**'"""
    league = fixture.get("league")
    ladder = (fixture.get("competition") or "").split("_", 1)
    division = (ladder[1] if len(ladder) > 1 else "Clubs").upper()
    if not round_name:
        gw = next((g for g in season.ALL if g.key == fixture.get("gameweek")), None)
        round_name = gw.label if gw else "Match"
    text = "| PRS {} {} | {} | {}".format(
        season.SEASON_LABEL, division, round_name.upper(),
        (competition or league or "").upper()).rstrip(" |")
    logo = season.competition_emoji(league)
    return "{} **{}**".format(logo, text) if logo else "**{}**".format(text)


def build(fixture, home_score, away_score, stats_home, stats_away, subs, motm,
          officials, players, competition=None, round_name=None):
    """(text, problems). text is None when there is anything to fix first."""
    home, away = fixture["home_team"], fixture["away_team"]
    problems = []

    def check(name, club_needed=None):
        record = players.find(name)
        if record is None:
            close = players.suggest(name)
            problems.append("**{}** isn't on the player sheet{}.".format(
                name, " (did you mean {}?)".format(", ".join(close)) if close else ""))
            return None
        if club_needed and record["club"] != club_needed:
            problems.append("**{}** plays for {}, not {}.".format(
                record["username"], record["club"].title(), club_needed.title()))
            return None
        return record

    def stat_rows(text, club):
        rows = []
        for line in lines_of(text):
            name, keys, bad = parse_line(line)
            if bad:
                problems.append("**{}**: I don't know {}. Use {}.".format(
                    name, ", ".join("`{}`".format(b) for b in bad), TOKEN_HELP))
                continue
            record = check(name, club)
            if record:
                rows.append(_row(club, record["username"], _emojis(keys)))
        return rows

    home_rows = stat_rows(stats_home, home)
    away_rows = stat_rows(stats_away, away)

    sub_rows = []
    for line in lines_of(subs):
        name, keys, bad = parse_line(line)
        record = check(name)
        if record and record["club"] not in (home, away):
            problems.append("**{}** doesn't play for {} or {}.".format(
                record["username"], home.title(), away.title()))
        elif record:
            sub_rows.append(_row(record["club"], record["username"], _emojis(keys) if not bad else ""))

    motm_rows = []
    placed = lines_of(motm)
    if len(placed) > len(PLACINGS):
        problems.append("MOTM & mentions takes at most {} names.".format(len(PLACINGS)))
    for emoji, line in zip(PLACINGS, placed):
        record = check(line.split()[0].lstrip("@"))
        if record and record["club"] in (home, away):
            motm_rows.append(_row(record["club"], record["username"], emoji))
        elif record:
            problems.append("**{}** doesn't play for {} or {}.".format(
                record["username"], home.title(), away.title()))

    if problems:
        return None, problems

    badge = season.SEASON_EMOJI if season.usable_emoji(season.SEASON_EMOJI) else "•"
    parts = [
        header(fixture, competition, round_name),
        "{} **{} - {}** {}".format(
            season.label_for(home), home_score, away_score, season.label_for(away)),
        "**STATISTICS**\n" + "\n".join(home_rows) if home_rows else "**STATISTICS**",
    ]
    if away_rows:
        parts.append("\n".join(away_rows))
    if sub_rows:
        parts.append("**SUBS**\n" + "\n".join(sub_rows))
    if motm_rows:
        parts.append("**MOTM & MENTIONS**\n" + "\n".join(motm_rows))
    names = [n.lstrip("@") for n in lines_of(officials)]
    if names:
        parts.append("**OFFICIALS**\n" + "\n".join(
            "{} | {}".format(badge, esc(n)) for n in names))
    return "\n\n".join(parts), []
