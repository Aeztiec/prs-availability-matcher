"""The timings sheet: which kickoff times each team said it can play.

Each competition's sheet is saved as one page in data/timings/, named
"<SEASON>_<COMPETITION>_timings.html" (for example S17_Clubs_timings.html). Every
team has a row of half-hourly kickoff times coloured green (free) or red (busy).
This module reads that page and answers "what slots exist" and "which are free
for this team" - the saved-timings fallback when a manager misses a deadline.

The filename is where the season and competition come from, so adding a
competition means dropping a file in; nothing here needs editing.
"""

from __future__ import annotations

import difflib
import glob
import html
import os
import re

from bot.paths import ROOT, TIMINGS_DIR

# Prefixed to the season in a competition's title: "PRS S17 Clubs".
LEAGUE = "PRS"

GREEN = "#00ff00"
RED = "#ff0000"

# Positions in the sheet, by 0-based grid index.
COL_PLAYER = 4      # column D - the player's username
COL_TEAM = 5        # column E - the country they play as
COL_FIRST = 7       # column F - first availability column
ROW_DAY = 2         # sheet row 2 - "Saturday" / "Sunday" / "Friday (Low Priority)"
ROW_EST = 3         # sheet row 3 - GMT-5 times
ROW_BST = 4         # sheet row 4 - GMT+1 times
ROW_UTC = 5         # sheet row 5 - GMT+0 times
ROW_FIRST_TEAM = 7  # grid row of the first team (sheet row 6)


# --------------------------------------------------------------------------
# Competitions - one saved sheet per file in timings/
# --------------------------------------------------------------------------

class Competition:
    """One timings/ file, with the season and competition read off its name."""

    def __init__(self, path):
        self.path = path
        self.key = os.path.basename(path)[: -len("_timings.html")]
        season, _, competition = self.key.partition("_")
        self.season = season
        self.competition = competition.replace("_", " ")

    @property
    def title(self):
        parts = [LEAGUE, self.season, self.competition]
        return " ".join(p for p in parts if p)

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<Competition {}>".format(self.key)


def competitions():
    """Every timings/<season>_<competition>_timings.html, in name order."""
    pattern = os.path.join(TIMINGS_DIR, "*_timings.html")
    return [Competition(p) for p in sorted(glob.glob(pattern))]


def season_key(competition):
    """Sort key for a season, numerically - so S9 comes before S10, not after.

    Seasons without a number in them sort below the numbered ones rather than
    blowing up, so an oddly named file can never hide a real season.
    """
    digits = re.search(r"\d+", competition.season)
    return (1, int(digits.group()), "") if digits else (0, 0, competition.season.lower())


def default_competition(found):
    """The one to show first: newest season, preferring Clubs.

    Clubs is the regular season and the usual thing people want. The
    international competition is named differently every time (WC, Euros, ...),
    so it is picked by being "not Clubs" rather than by name - which means a new
    name works without touching this.
    """
    if not found:
        return None
    newest = max(season_key(c) for c in found)
    in_season = [c for c in found if season_key(c) == newest]
    clubs = [c for c in in_season if c.competition.strip().lower() == "clubs"]
    return sorted(clubs or in_season, key=lambda c: c.key)[0]


def find_competition(key=None):
    """Resolve a competition by key, or return the default one."""
    found = competitions()
    if not found:
        raise LookupError(
            "no competitions in {}. Save a sheet there as "
            "<SEASON>_<COMPETITION>_timings.html, e.g. S17_Clubs_timings.html."
            .format(os.path.relpath(TIMINGS_DIR, ROOT))
        )
    if not key:
        return default_competition(found)

    needle = key.lower()
    for competition in found:
        if competition.key.lower() == needle:
            return competition
    close = [c for c in found if needle in c.key.lower()]
    if len(close) == 1:
        return close[0]
    raise LookupError(
        "no competition '{}'. Available: {}".format(
            key, ", ".join(c.key for c in found)
        )
    )


# --------------------------------------------------------------------------
# Sheet loading and parsing
# --------------------------------------------------------------------------

def load_sheet(competition):
    with open(competition.path, encoding="utf-8") as handle:
        return handle.read()


def parse_grid(source):
    """Flatten the HTML table into {(row, col): (text, background colour)}.

    Google's export uses colspan/rowspan for merged cells, so a merged cell is
    written into every grid position it covers.
    """
    styles = dict(re.findall(r"\.(s[0-9]+)\{([^}]*)\}", source))

    def background(class_attr):
        name = class_attr.split()[0] if class_attr else ""
        match = re.search(r"background-color:(#[0-9a-f]{6})", styles.get(name, ""))
        return match.group(1) if match else None

    start = source.find("<table")
    end = source.find("</table>", start)
    if start == -1 or end == -1:
        raise ValueError("no spreadsheet table found in the saved page")

    grid = {}
    for row_index, row_html in enumerate(
        re.findall(r"<tr[^>]*>(.*?)</tr>", source[start:end], re.S)
    ):
        col_index = 0
        for cell in re.finditer(r"<t[dh]([^>]*)>(.*?)</t[dh]>", row_html, re.S):
            attrs, inner = cell.group(1), cell.group(2)
            text = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
            text = re.sub(r"\s+", " ", text)
            colspan = re.search(r'colspan="?(\d+)', attrs)
            rowspan = re.search(r'rowspan="?(\d+)', attrs)
            class_attr = re.search(r'class="([^"]*)"', attrs)
            colspan = int(colspan.group(1)) if colspan else 1
            rowspan = int(rowspan.group(1)) if rowspan else 1
            colour = background(class_attr.group(1) if class_attr else "")
            while (row_index, col_index) in grid:
                col_index += 1
            for dr in range(rowspan):
                for dc in range(colspan):
                    grid[(row_index + dr, col_index + dc)] = (text, colour)
            col_index += colspan
    return grid


class Slot:
    """One kickoff time - a single column of one day's block."""

    def __init__(self, col, day, utc, est, bst):
        self.col = col
        self.day = day
        self.utc = utc
        self.est = est
        self.bst = bst

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<Slot {} {} GMT+0>".format(self.day, self.utc)


class Team:
    def __init__(self, country, player, row):
        self.country = country
        self.player = player
        self.row = row
        self.availability = {}

    @property
    def label(self):
        return "{} ({})".format(self.country, self.player) if self.player else self.country

    @property
    def has_submitted(self):
        return any(state is not None for state in self.availability.values())

    def is_free(self, slot):
        return self.availability.get(slot.col) == "free"


class Sheet:
    def __init__(self, slots, teams):
        self.slots = slots
        self.teams = teams

    def find(self, query):
        """Resolve a country or player name, tolerating case and typos."""
        needle = query.strip().lower()
        if not needle:
            raise LookupError("no team given")

        for team in self.teams:  # exact match on either name
            if needle in (team.country.lower(), team.player.lower()):
                return team

        prefix = [
            t for t in self.teams
            if t.country.lower().startswith(needle) or t.player.lower().startswith(needle)
        ]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise LookupError(
                "'{}' is ambiguous: {}".format(query, ", ".join(t.country for t in prefix))
            )

        contains = [
            t for t in self.teams
            if needle in t.country.lower() or needle in t.player.lower()
        ]
        if len(contains) == 1:
            return contains[0]
        if len(contains) > 1:
            raise LookupError(
                "'{}' is ambiguous: {}".format(query, ", ".join(t.country for t in contains))
            )

        # difflib is case-sensitive, so compare folded names and map back.
        names = {}
        for team in self.teams:
            names.setdefault(team.country.lower(), team.country)
            names.setdefault(team.player.lower(), team.player)
        close = difflib.get_close_matches(needle, list(names), n=3, cutoff=0.6)
        hint = (
            " Did you mean: {}?".format(", ".join(names[c] for c in close)) if close else ""
        )
        raise LookupError("no team called '{}'.{}".format(query, hint))


def build_sheet(source):
    grid = parse_grid(source)
    max_col = max(col for _, col in grid) + 1
    max_row = max(row for row, _ in grid) + 1

    # Availability columns are the ones carrying a GMT+0 time in row 5. The
    # narrow spacer columns between days carry no time, which is what separates
    # one day's block from the next.
    time_cols = [
        col for col in range(COL_FIRST, max_col)
        if re.match(r"^\d{1,2}:\d{2} ?(AM|PM)$", grid.get((ROW_UTC, col), ("", None))[0])
    ]

    blocks = []
    for col in time_cols:
        if blocks and col == blocks[-1][-1] + 1:
            blocks[-1].append(col)
        else:
            blocks.append([col])

    slots = []
    for block in blocks:
        day = next(
            (grid[(ROW_DAY, c)][0] for c in block if grid.get((ROW_DAY, c), ("", None))[0]),
            "Unknown day",
        )
        for col in block:
            slots.append(
                Slot(
                    col=col,
                    day=day,
                    utc=grid[(ROW_UTC, col)][0],
                    est=grid.get((ROW_EST, col), ("", None))[0],
                    bst=grid.get((ROW_BST, col), ("", None))[0],
                )
            )

    teams = []
    for row in range(ROW_FIRST_TEAM, max_row):
        country = grid.get((row, COL_TEAM), ("", None))[0]
        player = grid.get((row, COL_PLAYER), ("", None))[0]
        if not country and not player:
            continue
        team = Team(country or player, player, row)
        for slot in slots:
            colour = grid.get((row, slot.col), ("", None))[1]
            if colour == GREEN:
                team.availability[slot.col] = "free"
            elif colour == RED:
                team.availability[slot.col] = "busy"
            else:
                team.availability[slot.col] = None
        teams.append(team)

    if not slots or not teams:
        raise ValueError("the sheet did not contain the expected times or teams")
    return Sheet(slots, teams)
