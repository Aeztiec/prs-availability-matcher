"""SQLite storage for fixtures, submissions, referees and the scheduling log.

Deliberately plain: no ORM, explicit SQL, one short-lived connection per call.
A league week is a few dozen fixtures, so there is nothing here worth the
complexity of async database access - the queries finish in microseconds.

Two things are load-bearing:

* A manager's availability is submitted once per week, not once per fixture -
  someone managing more than one team answers the same "what times can you
  play" question once, and it applies to every fixture of theirs that week.
  It lives in its own table keyed by (week, manager), not on the fixture, so
  rescheduling never loses what they actually said.
* Nothing is ever overwritten silently. Every decision appends to `log`, so
  when a fixture ends up somewhere surprising you can read back exactly what
  the automation did and when.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone

from bot.paths import DB_PATH, LEGACY_DB_PATH

DEFAULT_PATH = DB_PATH

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
    referee_id       INTEGER,  -- unused: officiating now lives in fixture_referees
    reminded_12h     INTEGER NOT NULL DEFAULT 0,
    reminded_2h      INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL
);

-- One submission per manager per week, not per fixture - a manager who
-- manages more than one team answers "what times can you play this week"
-- once, and the orchestrator applies it to every one of their fixtures that
-- share the week.
CREATE TABLE IF NOT EXISTS weekly_submissions (
    week         TEXT    NOT NULL,
    manager_id   INTEGER NOT NULL,
    slots        TEXT    NOT NULL,
    submitted    INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (week, manager_id)
);

CREATE TABLE IF NOT EXISTS referees (
    discord_id  INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    tier        INTEGER NOT NULL DEFAULT 1,
    roblox      TEXT
);

-- First-come-first-served officiating. A fixture takes at most one REF claim
-- and two AR (assistant/VAR) claims - see referees.next_open_role. Claiming is
-- public and instant: there is no availability to submit and no offer to wait on.
CREATE TABLE IF NOT EXISTS fixture_referees (
    fixture_id  INTEGER NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    referee_id  INTEGER NOT NULL,
    role        TEXT    NOT NULL,
    claimed_at  TEXT    NOT NULL,
    PRIMARY KEY (fixture_id, referee_id)
);

CREATE TABLE IF NOT EXISTS log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id  INTEGER,
    at          TEXT    NOT NULL,
    event       TEXT    NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS managers (
    team        TEXT    PRIMARY KEY,
    discord_id  INTEGER NOT NULL,
    set_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS open_gameweeks (
    gameweek   TEXT    PRIMARY KEY,
    opened_by  INTEGER,
    opened_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS boards (
    week        TEXT    PRIMARY KEY,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    digest      TEXT,
    updated_at  TEXT    NOT NULL
);

-- The public, self-updating referee board for a week: every scheduled
-- fixture grouped by day, plus a trailing claim-menu message. Same shape as
-- `boards`, and the same reason for it - editing in place beats reposting.
CREATE TABLE IF NOT EXISTS ref_boards (
    week        TEXT    PRIMARY KEY,
    channel_id  INTEGER NOT NULL,
    message_ids TEXT    NOT NULL,
    digest      TEXT,
    updated_at  TEXT    NOT NULL
);


CREATE INDEX IF NOT EXISTS ix_fixtures_week   ON fixtures(week, competition);
CREATE INDEX IF NOT EXISTS ix_fixtures_status ON fixtures(status);
CREATE INDEX IF NOT EXISTS ix_log_fixture     ON log(fixture_id, id);
"""


def now():
    """UTC timestamp, second precision, sortable as text."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Store:
    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        if path == DB_PATH:
            self._adopt_legacy_database()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _adopt_legacy_database():
        """The database used to sit inside the package. Bring an existing one
        to its new home the first time; the old file is left as a backup."""
        if not os.path.exists(DB_PATH) and os.path.exists(LEGACY_DB_PATH):
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            shutil.copy2(LEGACY_DB_PATH, DB_PATH)

    def _migrate(self, conn):
        """Add columns that arrived after the first release.

        CREATE TABLE IF NOT EXISTS will not alter an existing table, so a
        database made before gameweeks existed needs the column adding rather
        than the schema silently not applying.
        """
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(fixtures)")}
        if "gameweek" not in columns:
            conn.execute("ALTER TABLE fixtures ADD COLUMN gameweek TEXT")
        if "league" not in columns:
            conn.execute("ALTER TABLE fixtures ADD COLUMN league TEXT")
        ref_columns = {r["name"] for r in conn.execute("PRAGMA table_info(referees)")}
        if "roblox" not in ref_columns:
            conn.execute("ALTER TABLE referees ADD COLUMN roblox TEXT")
        if "tier" not in ref_columns:
            conn.execute("ALTER TABLE referees ADD COLUMN tier INTEGER NOT NULL DEFAULT 1")
        board_columns = {r["name"] for r in conn.execute("PRAGMA table_info(boards)")}
        if "message_ids" not in board_columns:
            conn.execute("ALTER TABLE boards ADD COLUMN message_ids TEXT")

    @contextlib.contextmanager
    def _connect(self):
        """A connection for one unit of work: committed on success, rolled back
        on an error, and always closed (sqlite3's own `with` never closes, which
        leaves the file locked on Windows)."""
        conn = sqlite3.connect(self.path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                yield conn
        finally:
            conn.close()

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
                       home_manager_id, away_manager_id, deadline, status,
                       gameweek=None, league=None):
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO fixtures
                   (competition, week, home_team, away_team, home_manager_id,
                    away_manager_id, deadline, status, created_at, gameweek, league)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (competition, week, home_team, away_team, home_manager_id,
                 away_manager_id, deadline, status, now(), gameweek, league),
            )
            fixture_id = cursor.lastrowid
        self.note(fixture_id, "fixture created",
                  "{} vs {}, deadline {}".format(home_team, away_team, deadline))
        return fixture_id

    def fixture(self, fixture_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM fixtures WHERE id=?", (fixture_id,)).fetchone()
            return dict(row) if row else None

    def fixtures(self, week=None, competition=None, status=None, gameweek=None):
        sql = "SELECT * FROM fixtures WHERE 1=1"
        args = []
        for column, value in (("week", week), ("competition", competition),
                              ("status", status), ("gameweek", gameweek)):
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

    def mark_reminded(self, fixture_id, which):
        column = {"12h": "reminded_12h", "2h": "reminded_2h"}[which]
        with self._connect() as conn:
            conn.execute("UPDATE fixtures SET {}=1 WHERE id=?".format(column), (fixture_id,))
        self.note(fixture_id, "{} reminder sent".format(which))

    # -------------------------------------------------------- submissions
    def weekly_submission(self, week, manager_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM weekly_submissions WHERE week=? AND manager_id=?",
                (week, manager_id),
            ).fetchone()
        if not row:
            return None
        record = dict(row)
        record["slots"] = json.loads(record["slots"])
        return record

    def latest_weekly_submission(self, manager_id, before_week=None):
        """This manager's most recent submitted week, to prefill a fresh
        week's selector with.

        A manager's availability rarely changes week to week, so opening a
        selector that has nothing saved yet for the new week falls back to
        whatever they last actually submitted - one click confirms it again
        instead of re-entering every slot from scratch. Only ever reads a
        submitted answer, never an abandoned in-progress draft. Week keys
        sort correctly as text (they're ISO dates), so ORDER BY is enough.
        """
        sql = "SELECT * FROM weekly_submissions WHERE manager_id=? AND submitted=1"
        args = [manager_id]
        if before_week is not None:
            sql += " AND week<?"
            args.append(before_week)
        sql += " ORDER BY week DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(sql, args).fetchone()
        if not row:
            return None
        record = dict(row)
        record["slots"] = json.loads(record["slots"])
        return record

    def save_weekly_submission(self, week, manager_id, slots, submitted=False):
        """Upsert a manager's picks for a week. Called on every button click,
        so it must stay cheap and must not clobber the `submitted` flag by
        accident."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO weekly_submissions (week, manager_id, slots, submitted, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(week, manager_id) DO UPDATE SET
                       slots=excluded.slots,
                       submitted=MAX(weekly_submissions.submitted, excluded.submitted),
                       updated_at=excluded.updated_at""",
                (week, manager_id, json.dumps(slots, sort_keys=True),
                 1 if submitted else 0, now()),
            )

    def mark_weekly_submitted(self, week, manager_id, fixture_ids=()):
        """Flag a week's submission as final, and log it against every
        fixture it now covers - the audit trail lives per fixture, even
        though the submission itself does not."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE weekly_submissions SET submitted=1, updated_at=? "
                "WHERE week=? AND manager_id=?",
                (now(), week, manager_id),
            )
        for fixture_id in fixture_ids:
            self.note(fixture_id, "manager submitted", str(manager_id))

    # ----------------------------------------------------------- referees
    def add_referee(self, discord_id, name, tier=None, roblox=None):
        """Register (or re-activate) a referee. A new one starts at tier 1; an
        existing one keeps their tier unless one is given."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO referees (discord_id, name, tier, roblox) VALUES (?,?,?,?)
                   ON CONFLICT(discord_id) DO UPDATE SET name=excluded.name, active=1,
                   tier=COALESCE(?, tier), roblox=COALESCE(?, roblox)""",
                (discord_id, name, tier or 1, roblox, tier, roblox),
            )

    def set_referee_tier(self, discord_id, tier):
        with self._connect() as conn:
            conn.execute("UPDATE referees SET tier=? WHERE discord_id=?", (tier, discord_id))

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

    def is_active_referee(self, discord_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM referees WHERE discord_id=? AND active=1", (discord_id,)
            ).fetchone()
        return row is not None

    def claim_referee(self, fixture_id, referee_id, role):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO fixture_referees (fixture_id, referee_id, role, claimed_at)
                   VALUES (?,?,?,?)""",
                (fixture_id, referee_id, role, now()),
            )
        self.note(fixture_id, "referee claimed", "{} as {}".format(referee_id, role))

    def drop_referee(self, fixture_id, referee_id):
        """Remove one official from a fixture. Returns whether anything was removed."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM fixture_referees WHERE fixture_id=? AND referee_id=?",
                (fixture_id, referee_id),
            )
            removed = cursor.rowcount > 0
        if removed:
            self.note(fixture_id, "referee dropped out", str(referee_id))
        return removed

    def fixture_referees(self, fixture_id):
        """Officials on a fixture: the referee first, then assistants in claim order."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM fixture_referees WHERE fixture_id=?
                   ORDER BY CASE role WHEN 'REF' THEN 0 ELSE 1 END, claimed_at""",
                (fixture_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def fixtures_officiated_by(self, referee_id):
        """Every fixture this person is on, each with the `role` they hold."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT f.*, fr.role AS role FROM fixture_referees fr
                   JOIN fixtures f ON f.id = fr.fixture_id
                   WHERE fr.referee_id=? ORDER BY f.week, f.id""",
                (referee_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def ref_committed_in_slot(self, week, slot_key, referee_id, exclude_fixture=None):
        """Is this referee already officiating another fixture at this kickoff?

        A hard exclusion: nobody can be in two places at once, regardless of
        which role they hold on either fixture.
        """
        sql = ("SELECT 1 FROM fixture_referees fr JOIN fixtures f ON f.id = fr.fixture_id "
               "WHERE f.week=? AND f.slot_key=? AND fr.referee_id=?")
        args = [week, slot_key, referee_id]
        if exclude_fixture is not None:
            sql += " AND fr.fixture_id<>?"
            args.append(exclude_fixture)
        with self._connect() as conn:
            return conn.execute(sql, args).fetchone() is not None

    # ------------------------------------------------------- ref workload
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

    # ------------------------------------------------- published fixture board
    def board(self, week):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM boards WHERE week=?", (week,)).fetchone()
        return dict(row) if row else None

    def boards(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM boards ORDER BY week")]

    def set_board(self, week, channel_id, message_ids, digest=None):
        """Remember every message the board occupies, not just the first.

        A fully scheduled gameweek needs two messages once referee names are
        in, and editing only the first would leave the second half of the
        fixture list frozen at whatever it said when posted.
        """
        if isinstance(message_ids, int):
            message_ids = [message_ids]
        message_ids = list(message_ids)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO boards (week, channel_id, message_id, message_ids,
                                       digest, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(week) DO UPDATE SET
                       channel_id=excluded.channel_id,
                       message_id=excluded.message_id,
                       message_ids=excluded.message_ids,
                       digest=excluded.digest,
                       updated_at=excluded.updated_at""",
                (week, channel_id, message_ids[0], json.dumps(message_ids),
                 digest, now()),
            )

    def board_message_ids(self, week):
        """Every message id for a board, oldest first."""
        record = self.board(week)
        if not record:
            return []
        raw = record.get("message_ids")
        if raw:
            try:
                return [int(x) for x in json.loads(raw)]
            except (ValueError, TypeError):
                pass
        return [record["message_id"]]

    def forget_board(self, week):
        with self._connect() as conn:
            conn.execute("DELETE FROM boards WHERE week=?", (week,))

    # -------------------------------------------------- published referee board
    def ref_board(self, week):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM ref_boards WHERE week=?", (week,)).fetchone()
        return dict(row) if row else None

    def ref_boards(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM ref_boards ORDER BY week")]

    def set_ref_board(self, week, channel_id, message_ids, digest=None):
        if isinstance(message_ids, int):
            message_ids = [message_ids]
        message_ids = list(message_ids)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO ref_boards (week, channel_id, message_ids, digest, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(week) DO UPDATE SET
                       channel_id=excluded.channel_id,
                       message_ids=excluded.message_ids,
                       digest=excluded.digest,
                       updated_at=excluded.updated_at""",
                (week, channel_id, json.dumps(message_ids), digest, now()),
            )

    def ref_board_message_ids(self, week):
        """Every message id the referee board occupies, oldest first - the
        claim-menu message is always the last one."""
        record = self.ref_board(week)
        if not record:
            return []
        try:
            return [int(x) for x in json.loads(record["message_ids"])]
        except (ValueError, TypeError):
            return []

    def forget_ref_board(self, week):
        with self._connect() as conn:
            conn.execute("DELETE FROM ref_boards WHERE week=?", (week,))

    def unsubmitted_managers(self, fixture_id):
        """Managers on a fixture whose week they have not submitted yet.

        The dashboard needs names, not a count - "waiting on 1" doesn't tell
        staff who to chase.
        """
        fixture = self.fixture(fixture_id)
        if not fixture:
            return []
        missing = []
        for manager_id in (fixture["home_manager_id"], fixture["away_manager_id"]):
            record = self.weekly_submission(fixture["week"], manager_id)
            if not record or not record["submitted"]:
                missing.append(manager_id)
        return missing

    # ---------------------------------------------------- open gameweeks
    def open_gameweek(self, key, opened_by=None):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO open_gameweeks (gameweek, opened_by, opened_at)
                   VALUES (?,?,?)
                   ON CONFLICT(gameweek) DO UPDATE SET
                       opened_by=excluded.opened_by, opened_at=excluded.opened_at""",
                (key, opened_by, now()),
            )

    def close_gameweek(self, key):
        with self._connect() as conn:
            conn.execute("DELETE FROM open_gameweeks WHERE gameweek=?", (key,))

    def opened_gameweeks(self):
        """Gameweeks staff have opened early, beyond the current one."""
        with self._connect() as conn:
            return {r["gameweek"] for r in conn.execute(
                "SELECT gameweek FROM open_gameweeks")}

    def fixture_for(self, gameweek, home_team, away_team, league=None):
        """An existing fixture for this pairing in this gameweek, if any.

        Used to make opening a gameweek idempotent - running it twice must
        not create duplicate fixtures and DM everyone again. `league`
        narrows the match to one competition - the same two teams
        occasionally play each other twice in a gameweek, once
        domestically and once in the UEFA League Phase, and without it
        the second would look like a duplicate of the first and get
        silently skipped.
        """
        sql = "SELECT * FROM fixtures WHERE gameweek=? AND home_team=? AND away_team=?"
        args = [gameweek, home_team, away_team]
        if league is not None:
            sql += " AND league=?"
            args.append(league)
        with self._connect() as conn:
            row = conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ managers
    def set_manager(self, team, discord_id):
        """Map a sheet team name to a Discord user.

        The timings sheet has in-game usernames, not Discord ids, so this
        mapping has to be made once per team before a gameweek can be opened
        in bulk - there is nobody to DM otherwise.
        """
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO managers (team, discord_id, set_at) VALUES (?,?,?)
                   ON CONFLICT(team) DO UPDATE SET
                       discord_id=excluded.discord_id, set_at=excluded.set_at""",
                (team, discord_id, now()),
            )

    def manager_of(self, team):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT discord_id FROM managers WHERE team=?", (team,)
            ).fetchone()
        return row["discord_id"] if row else None

    def reassign_fixture_managers(self, team, discord_id):
        """Overwrite the manager snapshot on every existing fixture for `team`.

        set_manager() alone only affects fixtures created afterwards - a
        fixture freezes its manager id at creation on purpose, so a real
        mid-season manager change never quietly rewrites history. That
        freeze is exactly what breaks /test managers when it's re-run after
        fixtures already exist: the mapping table moves on, but the old
        manager id stays trapped on those fixtures, still blocking them from
        refereeing their own "old" game. Test-only: never call this from a
        real staff command.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE fixtures SET home_manager_id=? WHERE home_team=?",
                (discord_id, team),
            )
            conn.execute(
                "UPDATE fixtures SET away_manager_id=? WHERE away_team=?",
                (discord_id, team),
            )

    def managers(self):
        with self._connect() as conn:
            return {r["team"]: r["discord_id"]
                    for r in conn.execute("SELECT team, discord_id FROM managers")}
