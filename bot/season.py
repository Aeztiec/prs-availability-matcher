"""The Season 17 calendar: gameweeks, deadlines and the fixture list.

Data, not logic. Everything a new season needs is in this file - change the
dates and the fixture lists and the rest of the bot follows.

Three rules from the league's own instructions are encoded here:

* A gameweek is played over the Friday, Saturday and Sunday of its week, which
  is exactly what the timings sheet offers. Weekday kickoffs are possible but
  are an exception, handled by a staff override rather than the selector.
* The scheduling deadline is the Wednesday before. If the two managers have
  not agreed by then, Officials set the time from the teams' sheet timings -
  which is the fallback path the bot already runs.
* Nothing may kick off before the season opens. KICKOFF_FLOOR is checked when
  a slot is offered, so an early gameweek cannot produce an illegal time.

Availability opens per gameweek rather than for the whole season: a manager
cannot sensibly know in September whether their squad is free in December.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

SEASON = "S17"

# How the season reads in a public post's heading: "PRS SEASON 17 GAMEWEEK 1:".
SEASON_LABEL = "SEASON 17"

# Optional emoji after the heading and after each division name, the way the
# league's own posts do it. Upload one named after the league code below (PL,
# BL, LL, SA, L1) and `python -m bot.sync_emoji --write` picks them up.
SEASON_EMOJI = ""
LEAGUE_EMOJI = {
}

# No match may start before this. From the league instructions: "You can
# schedule games to be played from Thursday, 17 September 2026 17:30 onwards".
KICKOFF_FLOOR = datetime(2026, 9, 17, 17, 30, tzinfo=timezone.utc)

# The deadline is the Wednesday before a gameweek's Friday. End of that day, so
# "by Wednesday" means the whole of Wednesday is still in play.
DEADLINE_WEEKDAY_OFFSET = -2          # Friday - 2 = Wednesday
DEADLINE_TIME = time(23, 59, tzinfo=timezone.utc)

# How many gameweeks past the current one managers may schedule into. The
# league allows two, but only with an Officials' unlock - so the default is
# zero and staff open a gameweek early when they choose to.
DEFAULT_ADVANCE = 0
MAX_ADVANCE = 2

# Each gameweek's Friday, from the published Season 17 schedule. The knockout
# rounds are listed too so the calendar is complete; their fixtures get added
# once the draws are made.
GAMEWEEKS = [
    ("GW1", "Gameweek 1", "2026-09-18"),
    ("GW2", "Gameweek 2", "2026-09-25"),
    ("GW3", "Gameweek 3", "2026-10-02"),
    ("GW4", "Gameweek 4", "2026-10-09"),
    ("GW5", "Gameweek 5", "2026-10-16"),
    ("GW6", "Gameweek 6", "2026-10-23"),
    ("GW7", "Gameweek 7", "2026-10-30"),
    ("QF", "Domestic Quarter-Final", "2026-11-06"),
    ("SF", "Domestic Semi-Final", "2026-11-13"),
    ("F", "Domestic Final", "2026-11-20"),
]

# Fixture-list shorthand -> the team's name in the timings sheet. Validated
# against the sheet at startup, so a typo here is a loud error rather than a
# fixture quietly created for a team that does not exist.
TEAM_CODES = {
    # Premier League
    "ARS": "ARSENAL",
    "AST": "ASTON VILLA",
    "CHE": "CHELSEA",
    "LIV": "LIVERPOOL",
    "MUN": "MANCHESTER UNITED",
    "MCI": "MANCHESTER CITY",
    "NEW": "NEWCASTLE UNITED",
    "TOT": "TOTTENHAM HOTSPUR",
    # Bundesliga
    "BAY": "BAYERN MUNICH",
    "B04": "BAYER LEVERKUSEN",
    "BVB": "BORUSSIA DORTMUND",
    "SCF": "FREIBURG",
    "FRA": "EINTRACHT FRANKFURT",
    "S04": "SCHALKE 04",
    "RBL": "RED BULL LEIPZIG",
    "VFB": "STUTTGART",
    # La Liga
    "SEV": "SEVILLA",
    "ATM": "ATLÉTICO MADRID",
    "FCB": "BARCELONA",
    "BET": "REAL BETIS",
    "RMA": "REAL MADRID",
    "RSO": "REAL SOCIEDAD",
    "VAL": "VALENCIA",
    "VIL": "VILLARREAL",
    # Serie A
    "ACM": "AC MILAN",
    "COM": "COMO",
    "ROM": "AS ROMA",
    "INT": "INTER MILAN",
    "JUV": "JUVENTUS",
    "LAZ": "LAZIO",
    "NAP": "NAPOLI",
    "ATA": "ATALANTA",
    # Ligue 1
    "PSG": "PARIS SAINT-GERMAIN",
    "OLL": "OLYMPIQUE LYONNAIS",
    "LIL": "LOSC LILLE",
    "RCL": "RC LENS",
    "OLM": "OLYMPIQUE DE MARSEILLE",
    "ASM": "AS MONACO",
    "RCS": "STRASBOURG",
    "OGC": "OGC NICE",
}

# The club badge shown beside a team in the public fixture announcement.
#
# Must be the full "<:name:id>" form, not ":name:" - a bot cannot resolve a
# shortcode, so ":ARS:" would post as literal text. The ids below were read
# from the server's own emoji, matched by the three-letter team code.
#
# Any team left out simply shows its name, so this can be filled in a division
# at a time. To add more: upload the emoji named after the team's code from
# TEAM_CODES above, then run  python -m bot.sync_emoji  to pick them up.
TEAM_EMOJI = {
    # Premier League
    "ARSENAL": "<:ARS:1547634005745344554>",
    "ASTON VILLA": "<:AST:1547634141615366214>",
    "CHELSEA": "<:CHE:1547634215443763302>",
    "LIVERPOOL": "<:LIV:1547634278370775050>",
    "MANCHESTER CITY": "<:MCI:1547634427977400470>",
    "MANCHESTER UNITED": "<:MUN:1547634355944292454>",
    "NEWCASTLE UNITED": "<:NEW:1547634516401848341>",
    "TOTTENHAM HOTSPUR": "<:TOT:1547634590468804738>",
    # Bundesliga
    "BAYER LEVERKUSEN": "<:B04:1547634741824458902>",
    "BAYERN MUNICH": "<:BAY:1547634685394554921>",
    "BORUSSIA DORTMUND": "<:BVB:1547634804168855562>",
    "EINTRACHT FRANKFURT": "<:FRA:1547635238522327242>",
    "FREIBURG": "<:SCF:1547635165092651060>",
    "RED BULL LEIPZIG": "<:RBL:1547635435701010462>",
    "SCHALKE 04": "<:S04:1547635300455415828>",
    "STUTTGART": "<:VFB:1547635524167147662>",
    # La Liga
    "ATLÉTICO MADRID": "<:ATM:1547635894515933214>",
    "BARCELONA": "<:FCB:1547635947804303460>",
    "REAL BETIS": "<:BET:1547636036543193209>",
    "REAL MADRID": "<:RMA:1547636173130702888>",
    "REAL SOCIEDAD": "<:RSO:1547636231926718535>",
    "SEVILLA": "<:SEV:1547635623580672140>",
    "VALENCIA": "<:VAL:1547636292022566962>",
    "VILLARREAL": "<:VIL:1547636346787467465>",
    # Serie A
    "AC MILAN": "<:ACM:1547636419072106496>",
    "AS ROMA": "<:ROM:1547636562571825194>",
    "ATALANTA": "<:ATA:1547636860216672266>",
    "COMO": "<:COM:1547636501997949038>",
    "INTER MILAN": "<:INT:1547636622944772177>",
    "JUVENTUS": "<:JUV:1547636678670418111>",
    "LAZIO": "<:LAZ:1547636749818138676>",
    "NAPOLI": "<:NAP:1547636798753083472>",
    # Ligue 1
    "AS MONACO": "<:ASM:1547637634006917171>",
    "LOSC LILLE": "<:LIL:1547637312362520596>",
    "OGC NICE": "<:OGC:1547637804375478403>",
    "OLYMPIQUE DE MARSEILLE": "<:OLM:1547637582341611620>",
    "OLYMPIQUE LYONNAIS": "<:OLL:1547637227675459634>",
    "PARIS SAINT-GERMAIN": "<:PSG:1547636925576257654>",
    "RC LENS": "<:RCL:1547637516474257549>",
    "STRASBOURG": "<:RCS:1547637703200342079>",
}

LEAGUES = {
    "PL": "Premier League",
    "BL": "Bundesliga",
    "LL": "La Liga",
    "SA": "Serie A",
    "L1": "Ligue 1",
}

# home, away, league - transcribed from the published gameweek fixture lists.
FIXTURES = {
    "GW1": [
        ("ARS", "MCI", "PL"), ("TOT", "NEW", "PL"), ("AST", "LIV", "PL"), ("MUN", "CHE", "PL"),
        ("BVB", "BAY", "BL"), ("VFB", "B04", "BL"), ("SCF", "S04", "BL"), ("FRA", "RBL", "BL"),
        ("VIL", "BET", "LL"), ("ATM", "VAL", "LL"), ("FCB", "SEV", "LL"), ("RSO", "RMA", "LL"),
        ("ACM", "JUV", "SA"), ("INT", "ROM", "SA"), ("LAZ", "COM", "SA"), ("NAP", "ATA", "SA"),
        ("OGC", "PSG", "L1"), ("RCS", "RCL", "L1"), ("ASM", "LIL", "L1"), ("OLL", "OLM", "L1"),
    ],
    "GW2": [
        ("ARS", "TOT", "PL"), ("AST", "MCI", "PL"), ("CHE", "NEW", "PL"), ("LIV", "MUN", "PL"),
        ("BVB", "SCF", "BL"), ("B04", "FRA", "BL"), ("BAY", "RBL", "BL"), ("S04", "VFB", "BL"),
        ("RSO", "VIL", "LL"), ("SEV", "RMA", "LL"), ("FCB", "VAL", "LL"), ("ATM", "BET", "LL"),
        ("JUV", "NAP", "SA"), ("LAZ", "ATA", "SA"), ("ROM", "COM", "SA"), ("INT", "ACM", "SA"),
        ("PSG", "OLL", "L1"), ("ASM", "OLM", "L1"), ("LIL", "RCL", "L1"), ("RCS", "OGC", "L1"),
    ],
    "GW3": [
        ("ARS", "AST", "PL"), ("CHE", "TOT", "PL"), ("MCI", "MUN", "PL"), ("NEW", "LIV", "PL"),
        ("BVB", "VFB", "BL"), ("S04", "BAY", "BL"), ("B04", "RBL", "BL"), ("SCF", "FRA", "BL"),
        ("RMA", "VIL", "LL"), ("RSO", "FCB", "LL"), ("ATM", "SEV", "LL"), ("BET", "VAL", "LL"),
        ("COM", "JUV", "SA"), ("ATA", "INT", "SA"), ("NAP", "ACM", "SA"), ("ROM", "LAZ", "SA"),
        ("PSG", "OLM", "L1"), ("OLL", "LIL", "L1"), ("ASM", "RCS", "L1"), ("OGC", "RCL", "L1"),
    ],
    "GW4": [
        ("ARS", "CHE", "PL"), ("MUN", "AST", "PL"), ("TOT", "LIV", "PL"), ("MCI", "NEW", "PL"),
        ("S04", "BVB", "BL"), ("RBL", "VFB", "BL"), ("BAY", "FRA", "BL"), ("B04", "SCF", "BL"),
        ("VIL", "FCB", "LL"), ("RMA", "ATM", "LL"), ("BET", "RSO", "LL"), ("VAL", "SEV", "LL"),
        ("JUV", "ATA", "SA"), ("NAP", "COM", "SA"), ("LAZ", "INT", "SA"), ("ACM", "ROM", "SA"),
        ("RCS", "PSG", "L1"), ("LIL", "OGC", "L1"), ("OLM", "RCL", "L1"), ("ASM", "OLL", "L1"),
    ],
    "GW5": [
        ("MUN", "ARS", "PL"), ("LIV", "CHE", "PL"), ("AST", "NEW", "PL"), ("TOT", "MCI", "PL"),
        ("BVB", "B04", "BL"), ("BAY", "SCF", "BL"), ("VFB", "FRA", "BL"), ("RBL", "S04", "BL"),
        ("VIL", "ATM", "LL"), ("FCB", "BET", "LL"), ("VAL", "RMA", "LL"), ("SEV", "RSO", "LL"),
        ("JUV", "LAZ", "SA"), ("ROM", "NAP", "SA"), ("ATA", "ACM", "SA"), ("COM", "INT", "SA"),
        ("PSG", "LIL", "L1"), ("OLM", "RCS", "L1"), ("OLL", "OGC", "L1"), ("RCL", "ASM", "L1"),
    ],
    "GW6": [
        ("LIV", "ARS", "PL"), ("NEW", "MUN", "PL"), ("CHE", "MCI", "PL"), ("AST", "TOT", "PL"),
        ("FRA", "BVB", "BL"), ("SCF", "RBL", "BL"), ("B04", "S04", "BL"), ("VFB", "BAY", "BL"),
        ("VIL", "VAL", "LL"), ("BET", "SEV", "LL"), ("ATM", "RSO", "LL"), ("RMA", "FCB", "LL"),
        ("JUV", "ROM", "SA"), ("ACM", "LAZ", "SA"), ("NAP", "INT", "SA"), ("ATA", "COM", "SA"),
        ("PSG", "ASM", "L1"), ("RCL", "OLL", "L1"), ("OLM", "OGC", "L1"), ("LIL", "RCS", "L1"),
    ],
    "GW7": [
        ("NEW", "ARS", "PL"), ("MCI", "LIV", "PL"), ("TOT", "MUN", "PL"), ("CHE", "AST", "PL"),
        ("RBL", "BVB", "BL"), ("FRA", "S04", "BL"), ("VFB", "SCF", "BL"), ("BAY", "B04", "BL"),
        ("SEV", "VIL", "LL"), ("VAL", "RSO", "LL"), ("BET", "RMA", "LL"), ("FCB", "ATM", "LL"),
        ("INT", "JUV", "SA"), ("COM", "ACM", "SA"), ("ROM", "ATA", "SA"), ("LAZ", "NAP", "SA"),
        ("RCL", "PSG", "L1"), ("OGC", "ASM", "L1"), ("OLL", "RCS", "L1"), ("OLM", "LIL", "L1"),
    ],
}


# --------------------------------------------------------------------------

class Gameweek:
    def __init__(self, key, label, friday):
        self.key = key
        self.label = label
        self.friday = datetime.fromisoformat(friday).replace(tzinfo=timezone.utc)

    @property
    def week(self):
        """The Saturday, which is how the rest of the bot keys a match weekend."""
        return (self.friday + timedelta(days=1)).date().isoformat()

    @property
    def deadline(self):
        """End of the Wednesday before."""
        day = (self.friday + timedelta(days=DEADLINE_WEEKDAY_OFFSET)).date()
        return datetime.combine(day, DEADLINE_TIME)

    @property
    def fixtures(self):
        return FIXTURES.get(self.key, [])

    @property
    def has_fixtures(self):
        return bool(self.fixtures)

    def __repr__(self):
        return "<{} {}>".format(self.key, self.friday.date())


ALL = [Gameweek(*row) for row in GAMEWEEKS]
BY_KEY = {gw.key: gw for gw in ALL}


def gameweek(key):
    """Look up a gameweek, case-insensitively. Raises LookupError."""
    found = BY_KEY.get((key or "").strip().upper())
    if not found:
        raise LookupError(
            "no gameweek '{}'. Known: {}".format(key, ", ".join(BY_KEY))
        )
    return found


def current(now):
    """The gameweek being played or next up.

    A gameweek stays current until its Sunday is over, so a Sunday-evening
    fixture is not orphaned by the calendar rolling forward mid-weekend.
    """
    for gw in ALL:
        if now < gw.friday + timedelta(days=3):
            return gw
    return ALL[-1] if ALL else None


def upcoming(now, advance=DEFAULT_ADVANCE):
    """The gameweeks a manager may set availability for right now."""
    here = current(now)
    if here is None:
        return []
    start = ALL.index(here)
    return ALL[start:start + 1 + max(0, min(advance, MAX_ADVANCE))]


def team_name(code):
    name = TEAM_CODES.get((code or "").strip().upper())
    if not name:
        raise LookupError("unknown team code '{}'".format(code))
    return name


def validate(sheet_team_names):
    """Check every code and fixture against the timings sheet.

    Returns a list of problems, empty if the calendar is sound. Run at startup:
    a fixture list that references a team the sheet does not have would create
    fixtures the fallback can never schedule.
    """
    problems = []
    known = set(sheet_team_names)

    for code, name in sorted(TEAM_CODES.items()):
        if name not in known:
            problems.append("code {} -> '{}' is not in the timings sheet".format(code, name))

    for key, rows in FIXTURES.items():
        seen = {}
        for home, away, league in rows:
            for code in (home, away):
                if code not in TEAM_CODES:
                    problems.append("{}: unknown code '{}'".format(key, code))
            if league not in LEAGUES:
                problems.append("{}: unknown league '{}'".format(key, league))
            if home == away:
                problems.append("{}: {} plays itself".format(key, home))
            for code in (home, away):
                if code in seen:
                    problems.append("{}: {} appears twice ({} and {})".format(
                        key, code, seen[code], "{} v {}".format(home, away)))
                else:
                    seen[code] = "{} v {}".format(home, away)

    for key in FIXTURES:
        if key not in BY_KEY:
            problems.append("fixtures for unknown gameweek '{}'".format(key))

    return problems


def league_of(gameweek_key, home_code, away_code):
    """Which league a fixture belongs to, or None if it isn't in the list."""
    for home, away, league in FIXTURES.get(gameweek_key, []):
        if (home, away) == (home_code, away_code):
            return league
    return None


def usable_emoji(emoji):
    """True if this looks like a real custom-emoji reference.

    Anything else - blank, a bare ":NAME:" shortcode a bot cannot resolve, or
    an id whose emoji has since been deleted - is treated as absent and not
    rendered. A badge that is present but wrong is worse than none at all:
    Discord prints a dead reference as raw text in the middle of the post.
    """
    return bool(emoji) and emoji.startswith("<") and emoji.endswith(">")


def label_for(team_name):
    """How a team appears in a public post.

    Just the badge when there is a usable one - the league's posts are two
    logos and a time, with no names. A team with no badge falls back to its
    name, since an empty side would leave the row meaningless.
    """
    emoji = TEAM_EMOJI.get(team_name)
    return emoji if usable_emoji(emoji) else team_name
