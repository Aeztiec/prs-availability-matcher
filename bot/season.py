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
