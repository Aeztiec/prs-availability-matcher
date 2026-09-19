"""The player sheet: who plays for which club.

Read from data/players.csv (columns C, USERID, USERNAME, CLUB, ROLE, WAGE). It
holds real user IDs and wages, so it is git-ignored - the bot reads it from
disk and it never goes to the public repo.
"""

from __future__ import annotations

import csv
import difflib
import os

from bot.paths import PLAYERS_CSV as DEFAULT_PATH
FREE_AGENT = "FREE AGENT"
CLASS_ORDER = ["X", "S", "A", "B", "C"]   # best to worst


def _wage(row):
    digits = "".join(ch for ch in (row.get("WAGE") or "") if ch.isdigit())
    return int(digits) if digits else 0


def _key(name):
    """How usernames are compared: no @, no case."""
    return (name or "").strip().lstrip("@").lower()


class Players:
    def __init__(self, rows):
        self.rows = [r for r in rows if r.get("USERNAME")]
        self._by_name = {_key(r["USERNAME"]): r for r in self.rows}

    @classmethod
    def load(cls, path=None):
        path = path or DEFAULT_PATH
        if not os.path.exists(path):
            return cls([])
        with open(path, encoding="utf-8-sig", newline="") as handle:
            return cls(list(csv.DictReader(handle)))

    def find(self, name):
        """{'username', 'club', 'role'} for a username, or None."""
        row = self._by_name.get(_key(name))
        if not row:
            return None
        return {"username": row["USERNAME"], "club": row["CLUB"], "role": row["ROLE"]}

    def suggest(self, name, limit=3):
        """Close matches for a mistyped username."""
        close = difflib.get_close_matches(_key(name), list(self._by_name), n=limit, cutoff=0.6)
        return [self._by_name[k]["USERNAME"] for k in close]

    def roster(self, club):
        """Usernames of the players signed to a club, best class first (X, S,
        A, B, C), then highest wage, then A to Z. The first seven are who
        usually starts, the rest the bench."""
        def order(row):
            return (CLASS_ORDER.index(row.get("C")) if row.get("C") in CLASS_ORDER
                    else len(CLASS_ORDER), -_wage(row), row["USERNAME"].lower())
        return [r["USERNAME"] for r in sorted(
            (r for r in self.rows if r["CLUB"] == club and r["ROLE"] == "PLAYER"),
            key=order)]
