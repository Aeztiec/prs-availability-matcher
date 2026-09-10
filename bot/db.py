"""SQLite storage for fixtures, submissions, referees and the scheduling log.

Deliberately plain: no ORM, explicit SQL, one short-lived connection per call.
A league week is a few dozen fixtures, so there is nothing here worth the
complexity of async database access - the queries finish in microseconds.

Two things are load-bearing:

* Manager submissions live in their own table, not on the fixture. Managers can
  edit right up to the deadline, and keeping the raw response separate means
  rescheduling never loses what they actually said.
* Nothing is ever overwritten silently. Every decision appends to `log`, so
  when a fixture ends up somewhere surprising you can read back exactly what
  the automation did and when.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "fixtures.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS fixtures (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    competition      TEXT    NOT NULL,
    week             TEXT    NOT NULL,
    home_team        TEXT    NOT NULL,
    away_team        TEXT    NOT NULL,
    home_manager_id  INTEGER NOT NULL,
    away_manager_id  INTEGER NOT NULL,
    deadline         TEXT    NOT NULL,
    status           TEXT    NOT NULL,
    slot_key         TEXT,
    schedule_source  TEXT,
    referee_id       INTEGER,
    reminded_12h     INTEGER NOT NULL DEFAULT 0,
    reminded_2h      INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS submissions (
    fixture_id   INTEGER NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    manager_id   INTEGER NOT NULL,
    slots        TEXT    NOT NULL,
    submitted    INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (fixture_id, manager_id)
);

CREATE TABLE IF NOT EXISTS referees (
    discord_id  INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ref_availability (
    referee_id  INTEGER NOT NULL REFERENCES referees(discord_id) ON DELETE CASCADE,
    week        TEXT    NOT NULL,
    slots       TEXT    NOT NULL,
    submitted   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (referee_id, week)
);

CREATE TABLE IF NOT EXISTS ref_offers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id    INTEGER NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    referee_id    INTEGER NOT NULL,
    state         TEXT    NOT NULL,
    offered_at    TEXT    NOT NULL,
    responded_at  TEXT
);

CREATE TABLE IF NOT EXISTS log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id  INTEGER,
    at          TEXT    NOT NULL,
    event       TEXT    NOT NULL,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS ix_fixtures_week   ON fixtures(week, competition);
CREATE INDEX IF NOT EXISTS ix_fixtures_status ON fixtures(status);
CREATE INDEX IF NOT EXISTS ix_log_fixture     ON log(fixture_id, id);
"""

OFFER_OFFERED = "OFFERED"
OFFER_ACCEPTED = "ACCEPTED"
OFFER_DECLINED = "DECLINED"
OFFER_SUPERSEDED = "SUPERSEDED"


def now():
    """UTC timestamp, second precision, sortable as text."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Store:
    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ---------------------------------------------------------------- log
    def note(self, fixture_id, event, detail=None):
        """Append to the scheduling log. Never deletes, never updates."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO log (fixture_id, at, event, detail) VALUES (?,?,?,?)",
                (fixture_id, now(), event, detail),
            )

    def history(self, fixture_id):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT at, event, detail FROM log WHERE fixture_id=? ORDER BY id",
                (fixture_id,),
            )]

    # ----------------------------------------------------------- fixtures
    def create_fixture(self, competition, week, home_team, away_team,
                       home_manager_id, away_manager_id, deadline, status):
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO fixtures
                   (competition, week, home_team, away_team, home_manager_id,
                    away_manager_id, deadline, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (competition, week, home_team, away_team, home_manager_id,
                 away_manager_id, deadline, status, now()),
            )
            fixture_id = cursor.lastrowid
        self.note(fixture_id, "fixture created",
                  "{} vs {}, deadline {}".format(home_team, away_team, deadline))
        return fixture_id

    def fixture(self, fixture_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM fixtures WHERE id=?", (fixture_id,)).fetchone()
            return dict(row) if row else None

    def fixtures(self, week=None, competition=None, status=None):
        sql = "SELECT * FROM fixtures WHERE 1=1"
        args = []
        for column, value in (("week", week), ("competition", competition), ("status", status)):
            if value is not None:
                sql += " AND {}=?".format(column)
                args.append(value)
        sql += " ORDER BY id"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def set_schedule(self, fixture_id, slot_key, source, status):
        with self._connect() as conn:
            conn.execute(
                "UPDATE fixtures SET slot_key=?, schedule_source=?, status=? WHERE id=?",
                (slot_key, source, status, fixture_id),
            )
        self.note(fixture_id, "scheduled", "{} via {}".format(slot_key, source))

    def set_status(self, fixture_id, status, detail=None):
        with self._connect() as conn:
            conn.execute("UPDATE fixtures SET status=? WHERE id=?", (status, fixture_id))
        self.note(fixture_id, "status -> {}".format(status), detail)

    def set_referee(self, fixture_id, referee_id):
        with self._connect() as conn:
            conn.execute("UPDATE fixtures SET referee_id=? WHERE id=?",
                         (referee_id, fixture_id))
        self.note(fixture_id, "referee assigned", str(referee_id))

    def mark_reminded(self, fixture_id, which):
        column = {"12h": "reminded_12h", "2h": "reminded_2h"}[which]
        with self._connect() as conn:
            conn.execute("UPDATE fixtures SET {}=1 WHERE id=?".format(column), (fixture_id,))
        self.note(fixture_id, "{} reminder sent".format(which))

    # -------------------------------------------------------- submissions
    def submission(self, fixture_id, manager_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM submissions WHERE fixture_id=? AND manager_id=?",
                (fixture_id, manager_id),
            ).fetchone()
        if not row:
            return None
        record = dict(row)
        record["slots"] = json.loads(record["slots"])
        return record

    def save_submission(self, fixture_id, manager_id, slots, submitted=False):
        """Upsert a manager's picks. Called on every button click, so it must
        stay cheap and must not clobber the `submitted` flag by accident."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO submissions (fixture_id, manager_id, slots, submitted, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(fixture_id, manager_id) DO UPDATE SET
                       slots=excluded.slots,
                       submitted=MAX(submissions.submitted, excluded.submitted),
                       updated_at=excluded.updated_at""",
                (fixture_id, manager_id, json.dumps(slots, sort_keys=True),
                 1 if submitted else 0, now()),
            )

    def mark_submitted(self, fixture_id, manager_id):
        with self._connect() as conn:
            conn.execute(
                "UPDATE submissions SET submitted=1, updated_at=? WHERE fixture_id=? AND manager_id=?",
                (now(), fixture_id, manager_id),
            )
        self.note(fixture_id, "manager submitted", str(manager_id))

    def both_submitted(self, fixture_id):
        fixture = self.fixture(fixture_id)
        if not fixture:
            return False
        return all(
            (self.submission(fixture_id, mid) or {}).get("submitted")
            for mid in (fixture["home_manager_id"], fixture["away_manager_id"])
        )

    # ----------------------------------------------------------- referees
    def add_referee(self, discord_id, name):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO referees (discord_id, name) VALUES (?,?)
                   ON CONFLICT(discord_id) DO UPDATE SET name=excluded.name, active=1""",
                (discord_id, name),
            )

    def set_referee_active(self, discord_id, active):
        with self._connect() as conn:
            conn.execute("UPDATE referees SET active=? WHERE discord_id=?",
                         (1 if active else 0, discord_id))

    def referees(self, active_only=True):
        sql = "SELECT * FROM referees"
        if active_only:
            sql += " WHERE active=1"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY name")]

    def save_ref_availability(self, referee_id, week, slots, submitted=False):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO ref_availability (referee_id, week, slots, submitted, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(referee_id, week) DO UPDATE SET
                       slots=excluded.slots,
                       submitted=MAX(ref_availability.submitted, excluded.submitted),
                       updated_at=excluded.updated_at""",
                (referee_id, week, json.dumps(slots, sort_keys=True),
                 1 if submitted else 0, now()),
            )

    def ref_availability(self, referee_id, week):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ref_availability WHERE referee_id=? AND week=?",
                (referee_id, week),
            ).fetchone()
        if not row:
            return None
        record = dict(row)
        record["slots"] = json.loads(record["slots"])
        return record

    def mark_ref_submitted(self, referee_id, week):
        with self._connect() as conn:
            conn.execute(
                "UPDATE ref_availability SET submitted=1, updated_at=? WHERE referee_id=? AND week=?",
                (now(), referee_id, week),
            )

    # ------------------------------------------------------- ref workload
    def ref_workload(self, week, competition=None):
        """referee_id -> fixtures already assigned this week.

        Feeds the fair-workload ranking, so the same two refs don't end up
        doing every game.
        """
        sql = ("SELECT referee_id, COUNT(*) AS n FROM fixtures "
               "WHERE week=? AND referee_id IS NOT NULL")
        args = [week]
        if competition:
            sql += " AND competition=?"
            args.append(competition)
        with self._connect() as conn:
            return {r["referee_id"]: r["n"] for r in conn.execute(sql + " GROUP BY referee_id", args)}

    def offer(self, fixture_id, referee_id):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO ref_offers (fixture_id, referee_id, state, offered_at) VALUES (?,?,?,?)",
                (fixture_id, referee_id, OFFER_OFFERED, now()),
            )
        self.note(fixture_id, "referee offered", str(referee_id))

    def resolve_offer(self, fixture_id, referee_id, state):
        with self._connect() as conn:
            conn.execute(
                """UPDATE ref_offers SET state=?, responded_at=?
                   WHERE fixture_id=? AND referee_id=? AND state=?""",
                (state, now(), fixture_id, referee_id, OFFER_OFFERED),
            )
        self.note(fixture_id, "referee {}".format(state.lower()), str(referee_id))

    def refs_already_asked(self, fixture_id):
        """Refs who have already been offered this fixture, so a decline never
        loops back round to the same person."""
        with self._connect() as conn:
            return {r["referee_id"] for r in conn.execute(
                "SELECT DISTINCT referee_id FROM ref_offers WHERE fixture_id=?", (fixture_id,)
            )}

    # ------------------------------------------------- scheduling context
    def slot_load(self, week, competition=None):
        """slot_key -> how many fixtures are already in it."""
        sql = ("SELECT slot_key, COUNT(*) AS n FROM fixtures "
               "WHERE week=? AND slot_key IS NOT NULL")
        args = [week]
        if competition:
            sql += " AND competition=?"
            args.append(competition)
        with self._connect() as conn:
            return {r["slot_key"]: r["n"] for r in conn.execute(sql + " GROUP BY slot_key", args)}

    def busy_slots(self, week, teams, competition=None, ignore_fixture=None):
        """Slots where any of `teams` already has a fixture this week.

        A hard exclusion for the engine: a team cannot play two games at once.
        `ignore_fixture` lets a fixture be rescheduled without treating its own
        current slot as a conflict.
        """
        if not teams:
            return set()
        placeholders = ",".join("?" * len(teams))
        sql = ("SELECT slot_key FROM fixtures WHERE week=? AND slot_key IS NOT NULL "
               "AND (home_team IN ({0}) OR away_team IN ({0}))".format(placeholders))
        args = [week] + list(teams) + list(teams)
        if competition:
            sql += " AND competition=?"
            args.append(competition)
        if ignore_fixture is not None:
            sql += " AND id<>?"
            args.append(ignore_fixture)
        with self._connect() as conn:
            return {r["slot_key"] for r in conn.execute(sql, args)}
