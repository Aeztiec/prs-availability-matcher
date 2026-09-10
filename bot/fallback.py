"""Bridge between the timings sheet and the scheduling engine.

This is the fallback path's data source: when a manager misses the deadline we
fall back to what their team already told us in the sheet. The engine wants
{slot_key: bool}; the sheet gives green/red cells. This turns one into the
other, using the same slot vocabulary the Discord selector is built from so the
two paths can never disagree about what a slot is.
"""

from __future__ import annotations

import availability as av

from .slots import build_slots, sheet_columns_for


class Timings:
    """A parsed competition sheet, ready to answer availability questions."""

    def __init__(self, competition_key=None, granularity=None):
        self.competition = av.find_competition(competition_key)
        self.sheet = av.build_sheet(av.load_sheet(self.competition))
        kwargs = {} if granularity is None else {"granularity": granularity}
        self.slots = build_slots(self.sheet, **kwargs)
        self._columns = sheet_columns_for(self.sheet, self.slots)

    @property
    def title(self):
        return self.competition.title

    def team_names(self):
        return [(t.country, t.player) for t in self.sheet.teams]

    def find_team(self, name):
        """Resolve a team by country or player name. Raises LookupError."""
        return self.sheet.find(name)

    def has_submitted(self, name):
        return self.find_team(name).has_submitted

    def availability(self, name):
        """{slot_key: True/False} for one team, over the offered slots.

        A blank cell is False. That is deliberate: the fallback is already
        acting without the manager's input, so it should never invent an
        availability the team never claimed.
        """
        team = self.find_team(name)
        return {
            key: team.availability.get(col) == "free"
            for key, col in self._columns.items()
        }
