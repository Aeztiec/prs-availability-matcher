"""The post for a finished game: score, per-player stats, MOTM, officials.

Pure text in, text out (no discord objects), like notify.py. Staff type one
player per line with short tokens after the name, a BENCH line splits the
starters from the bench:

    vzcadc g g a        two goals and an assist
    BENCH
    gawa sub yc         a bench player who came on and was booked

Everything is checked against the player sheet, so a mistyped username or a
player on the wrong team is reported instead of posted. Players are shown by
username, never pinged.
"""

from __future__ import annotations

import re

from . import season

# What each token shows. Unicode for now; swap any of these for a custom emoji
# (e.g. "<:RC:123...>") once it is uploaded to the server.
STAT_EMOJI = {
    "goal": "⚽",
    "assist": "👁️",
    "yellow": "🟨",
    "red": "🟥",
    "sub": "🔁",       # shown as "🔁 '75 ON" / "🔁 '75 OFF"; see parse_line
    "pen_scored": "🟢",
    "pen_missed": "🔴",
}

TOKENS = {
    "g": "goal", "goal": "goal", "goals": "goal",
    "a": "assist", "ast": "assist", "assist": "assist", "assists": "assist",
    "yc": "yellow", "yellow": "yellow",
    "rc": "red", "red": "red",
    "ps": "pen_scored", "pm": "pen_missed",
}

TOKEN_HELP = ("g goal, a assist, yc yellow, rc red, on75 / off75 subbed on or off at "
              "75', ps scored pen, pm missed pen (g3 = three goals)")
SUB_HELP = ("A sub needs the minute and ON or OFF, like on75 or off60 "
            "(added time works too: on90+3).")

# MOTM gets the trophy, the next three mentions the medals, in the order typed.
PLACINGS = ["🏆", "🥇", "🥈", "🥉"]
MAX_MENTIONS = 10
DESCRIPTION_LIMIT = 4000   # an embed description holds 4096

_TOKEN = re.compile(r"^(\d*)([a-z]+)(\d*)$")
_MINUTE = r"(\d{1,3}(?:\+\d{1,2})?)"
_SUB_A = re.compile(r"^(on|off)" + _MINUTE + "$", re.IGNORECASE)   # on75
_SUB_B = re.compile(r"^" + _MINUTE + r"(on|off)$", re.IGNORECASE)  # 75on
_BENCH = re.compile(r"^bench:?$", re.IGNORECASE)


def esc(text):
    """Usernames like _gawa must not turn into italics."""
    return re.sub(r"([\\_*~`|>])", r"\\\1", text)


def parse_line(line):
    """('name', [stat keys...], [bad tokens]) for one 'name tok tok' line."""
    parts = line.split()
    if not parts:
        return None
    name = parts[0].lstrip("@")
    keys, bad = [], []
    for raw in parts[1:]:
        sub = _SUB_A.match(raw)
        if sub:
            keys.append("sub:{}:{}".format(sub.group(1).lower(), sub.group(2)))
            continue
        sub = _SUB_B.match(raw)
        if sub:
            keys.append("sub:{}:{}".format(sub.group(2).lower(), sub.group(1)))
            continue
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


def show(key):
    """The emoji (and for a sub the minute and direction) a stat key prints as."""
    if key.startswith("sub:"):
        _, direction, minute = key.split(":")
        return "{} '{} {}".format(STAT_EMOJI["sub"], minute, direction.upper())
    return STAT_EMOJI[key]


def _row(team, text):
    return "{} | {}".format(season.label_for(team), text)


def _stat_order(entry):
    """Most goals first, then most assists. Python's sort is stable, so
    everyone level keeps the order staff typed them in."""
    keys = entry[1]
    return (-keys.count("goal"), -keys.count("assist"))


def competition_name(fixture, competition=None):
    league = fixture.get("league")
    return (competition or season.LEAGUES.get(league) or league or "").upper()


def header(fixture, competition=None, round_name=None):
    """'<logo> **| PREMIER LEAGUE | GAMEWEEK 1**'"""
    if not round_name:
        gw = next((g for g in season.ALL if g.key == fixture.get("gameweek")), None)
        round_name = gw.label if gw else "Match"
    text = "**| {} | {}**".format(competition_name(fixture, competition), round_name.upper())
    logo = season.competition_emoji(fixture.get("league"))
    return "{} {}".format(logo, text) if logo else text


def score_line(fixture, home_score, away_score, pens=None):
    """'<home> **5 - 1** <away> **[5 - 4 ON PENS]**': the away badge sits
    right after the score, and any shootout follows it."""
    line = "{} **{} - {}** {}".format(
        season.label_for(fixture["home_team"]), home_score, away_score,
        season.label_for(fixture["away_team"]))
    if pens:
        line += " **[{} - {} ON PENS]**".format(*pens)
    return line


def build(fixture, home_score, away_score, stats_home, stats_away, motm,
          officials, players, competition=None, round_name=None, pens=None):
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

    def team_block(text, club):
        starters, bench = [], []
        target = starters
        for line in lines_of(text):
            if _BENCH.match(line):
                target = bench
                continue
            name, keys, bad = parse_line(line)
            if bad:
                problems.append("**{}**: I don't know {}. {}".format(
                    name, ", ".join("`{}`".format(b) for b in bad),
                    SUB_HELP if any(b.lower() == "sub" for b in bad)
                    else "Use " + TOKEN_HELP + "."))
                continue
            record = check(name, club)
            if record:
                target.append((record["username"], keys))
        rows = []
        for group, label in ((starters, None), (bench, "**BENCH:**")):
            if not group:
                continue
            if label:
                rows.append(label)
            for name, keys in sorted(group, key=_stat_order):
                rows.append(_row(club, " ".join(
                    [esc(name)] + [show(k) for k in keys])))
        return rows

    home_rows = team_block(stats_home, home)
    away_rows = team_block(stats_away, away)

    motm_rows = []
    mentions = lines_of(motm)
    if len(mentions) > MAX_MENTIONS:
        problems.append("MOTM & mentions takes at most {} names.".format(MAX_MENTIONS))
    for place, line in enumerate(mentions[:MAX_MENTIONS]):
        who, _, note = line.partition(" - ")
        record = check(who.split()[0].lstrip("@") if who.split() else "")
        if record and record["club"] not in (home, away):
            problems.append("**{}** doesn't play for {} or {}.".format(
                record["username"], home.title(), away.title()))
        elif record:
            medal = " " + PLACINGS[place] if place < len(PLACINGS) else ""
            motm_rows.append(_row(record["club"], esc(record["username"]) + medal
                                  + (" - " + esc(note.strip()) if note.strip() else "")))

    if problems:
        return None, problems

    badge = season.SEASON_EMOJI if season.usable_emoji(season.SEASON_EMOJI) else "•"
    parts = [
        header(fixture, competition, round_name),
        score_line(fixture, home_score, away_score, pens),
        "**STATISTICS:**" + ("\n" + "\n".join(home_rows) if home_rows else ""),
    ]
    if away_rows:
        parts.append("\n".join(away_rows))
    if motm_rows:
        parts.append("**MOTM & MENTIONS:**\n" + "\n".join(motm_rows))
    crew = [l.lstrip("@") for l in lines_of(officials)]
    if crew:
        parts.append("**OFFICIATING TEAM:**\n" + "\n".join(
            "{} | {}".format(badge, esc(line)) for line in crew))

    text = "\n\n".join(parts)
    if len(text) > DESCRIPTION_LIMIT:
        return None, ["That result is too long to post ({} characters). Trim the "
                      "MOTM notes or the bench.".format(len(text))]
    return text, []
