"""SQLite persistence: people, sessions, and high scores.

Schema
------
people        : one row per enrolled person (name + all-time high score)
sessions      : one row per processed clip
attempts      : one row per juggle streak within a session (per person)

High score is denormalized onto ``people.high_score`` for cheap reads by the
Home Assistant publisher, and is recomputed transactionally whenever an attempt
is recorded.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Iterable, Optional

# Reserved profile that catches every juggle session we can't attribute to a
# named person. All streaks default here unless re-attributed via MQTT.
UNKNOWN_NAME = "Unknown Juggler"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    high_score  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_path        TEXT NOT NULL,
    started_at       REAL NOT NULL,
    frames           INTEGER NOT NULL DEFAULT 0,
    fps              REAL NOT NULL DEFAULT 0,
    duration_s       REAL NOT NULL DEFAULT 0,
    frigate_event_id TEXT             -- Frigate event ID for async FaceID re-attribution
);
CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    person_id   INTEGER REFERENCES people(id) ON DELETE SET NULL,
    track_id    INTEGER,
    count       INTEGER NOT NULL,
    ended_reason TEXT,                  -- 'hand' | 'ground' | 'lost' | 'end'
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS faceid_labels (
    event_id    TEXT PRIMARY KEY,
    person_name TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        # Migrate older DBs that predate the duration_s column.
        self._ensure_column("sessions", "duration_s", "REAL NOT NULL DEFAULT 0")
        # Migrate older DBs that predate FaceID async re-attribution.
        self._ensure_column("sessions", "frigate_event_id", "TEXT")
        # High-score replay clip: path to the most-recent high-score video for
        # this person, and when it was captured (epoch). Overwritten in place
        # whenever the record is beaten, so only the latest is ever kept.
        self._ensure_column("people", "high_clip", "TEXT")
        self._ensure_column("people", "high_clip_at", "REAL")
        self.conn.commit()
        # Guarantee the catch-all profile exists so it shows alongside other
        # people in the database and HA sensors.
        self.unknown_person_id = self.add_person(UNKNOWN_NAME)

    def _ensure_column(self, table: str, col: str, decl: str) -> None:
        cols = [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")]
        if col not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            self.conn.commit()

    # ---- people / enrollment -------------------------------------------
    def add_person(self, name: str) -> int:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO people(name, created_at) VALUES (?, ?)",
            (name, time.time()),
        )
        self.conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = self.conn.execute(
            "SELECT id FROM people WHERE name = ?", (name,)
        ).fetchone()
        return int(row["id"])

    def list_people(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, name, high_score, high_clip, high_clip_at "
            "FROM people ORDER BY high_score DESC, name"
        ).fetchall()

    def person_name(self, person_id: Optional[int]) -> str:
        if person_id is None:
            return "Unknown"
        row = self.conn.execute(
            "SELECT name FROM people WHERE id = ?", (person_id,)
        ).fetchone()
        return row["name"] if row else "Unknown"

    def set_high_clip(self, person_id: int, path: str, ts: float) -> None:
        """Record the path + timestamp of a person's most-recent high-score clip."""
        self.conn.execute(
            "UPDATE people SET high_clip = ?, high_clip_at = ? WHERE id = ?",
            (path, float(ts), person_id),
        )
        self.conn.commit()

    def remember_faceid_label(self, event_id: str, person_name: str) -> None:
        """Persist the latest nonempty FaceID label received for an event."""
        if not event_id or not person_name.strip():
            return
        self.conn.execute(
            "INSERT INTO faceid_labels(event_id, person_name, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(event_id) DO UPDATE SET "
            "person_name=excluded.person_name, updated_at=excluded.updated_at",
            (event_id, person_name.strip(), time.time()),
        )
        self.conn.commit()

    def faceid_label(self, event_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT person_name FROM faceid_labels WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return str(row["person_name"]) if row else None

    def backfill_unknown_attempts(self) -> tuple[int, int]:
        """One-time: credit historical unattributed (NULL) attempts to the
        Unknown Juggler profile and recompute its high score.

        Returns (reassigned_count, new_high_score). Idempotent — after the first
        run there are no NULL-person attempts left to move. Does NOT create a
        replay video for the historical best (its source clip may already be
        gone); new sessions generate videos normally."""
        uid = self.unknown_person_id
        cur = self.conn.execute(
            "UPDATE attempts SET person_id = ? WHERE person_id IS NULL", (uid,)
        )
        reassigned = cur.rowcount or 0
        row = self.conn.execute(
            "SELECT MAX(count) AS m FROM attempts WHERE person_id = ?", (uid,)
        ).fetchone()
        high = int(row["m"]) if row and row["m"] is not None else 0
        self.conn.execute(
            "UPDATE people SET high_score = ? WHERE id = ?", (high, uid)
        )
        self.conn.commit()
        return reassigned, high

    # ---- sessions / attempts -------------------------------------------
    def start_session(self, clip_path: str, fps: float,
                      frigate_event_id: Optional[str] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions(clip_path, started_at, fps, frigate_event_id) "
            "VALUES (?, ?, ?, ?)",
            (clip_path, time.time(), fps, frigate_event_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def lookup_session_by_event_id(self, frigate_event_id: str) -> Optional[int]:
        """Return the session_id for a Frigate event, or None if not found.

        Used by the FaceID MQTT handler to locate which session to re-attribute
        after Frigate's async face recognition fires a sub_label update."""
        row = self.conn.execute(
            "SELECT id FROM sessions WHERE frigate_event_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (frigate_event_id,),
        ).fetchone()
        return int(row["id"]) if row else None

    def reassign_session_by_event(
        self, frigate_event_id: str, person_name: str
    ) -> Optional[dict]:
        """Re-attribute all Unknown Juggler attempts in a session to a named person.

        Called when FaceID publishes a sub_label for a Frigate event that was
        already processed and bucketed to Unknown Juggler. Looks up the session
        by event ID, finds the target person by name (creating them if needed),
        moves every Unknown attempt in that session to the target, and
        recomputes high scores for both. Returns a summary dict or None if the
        session/person could not be found or there was nothing to move::

            {
              "session_id":    int,
              "person_name":   str,
              "moved_count":   int,   # number of attempt rows moved
              "best_count":    int,   # highest juggle count moved
              "new_high":      bool,  # whether this beat the person's record
            }
        """
        session_id = self.lookup_session_by_event_id(frigate_event_id)
        if session_id is None:
            return None

        uid = self.unknown_person_id
        # Find attempts in this session still assigned to Unknown Juggler.
        rows = self.conn.execute(
            "SELECT id, count FROM attempts "
            "WHERE session_id = ? AND person_id = ?",
            (session_id, uid),
        ).fetchall()
        if not rows:
            return None

        # Resolve (or create) the target person.
        pid = self.add_person(person_name)

        best_count = max(int(r["count"]) for r in rows)
        moved = len(rows)
        attempt_ids = [int(r["id"]) for r in rows]

        self.conn.execute(
            f"UPDATE attempts SET person_id = ? "
            f"WHERE id IN ({','.join('?' * moved)})",
            [pid] + attempt_ids,
        )

        # Recompute high scores for both Unknown and the target person.
        def _recompute(person_id: int) -> int:
            row = self.conn.execute(
                "SELECT MAX(count) AS hi FROM attempts WHERE person_id = ?",
                (person_id,),
            ).fetchone()
            hi = int(row["hi"]) if row and row["hi"] is not None else 0
            self.conn.execute(
                "UPDATE people SET high_score = ? WHERE id = ?", (hi, person_id)
            )
            return hi

        _recompute(uid)
        new_person_high = _recompute(pid)
        self.conn.commit()

        return {
            "session_id": session_id,
            "person_name": person_name,
            "moved_count": moved,
            "best_count": best_count,
            "new_high": new_person_high == best_count,
        }

    def finish_session(self, session_id: int, frames: int,
                       duration_s: float = 0.0) -> None:
        self.conn.execute(
            "UPDATE sessions SET frames = ?, duration_s = ? WHERE id = ?",
            (frames, float(duration_s), session_id),
        )
        self.conn.commit()

    @staticmethod
    def _clip_attributions(conn: sqlite3.Connection,
                           clip_names: Iterable[str]) -> dict[str, list[str]]:
        """Return the latest session's attributed people for each clip basename."""
        wanted = {os.path.basename(name) for name in clip_names}
        if not wanted:
            return {}

        latest: dict[str, int] = {}
        for row in conn.execute(
            "SELECT id, clip_path FROM sessions ORDER BY started_at DESC, id DESC"
        ):
            name = os.path.basename(row["clip_path"])
            if name in wanted and name not in latest:
                latest[name] = int(row["id"])

        if not latest:
            return {}
        placeholders = ",".join("?" for _ in latest)
        rows = conn.execute(
            "SELECT DISTINCT a.session_id, COALESCE(p.name, ?) AS person "
            "FROM attempts a LEFT JOIN people p ON p.id = a.person_id "
            f"WHERE a.session_id IN ({placeholders}) ORDER BY person",
            [UNKNOWN_NAME, *latest.values()],
        )
        people_by_session: dict[int, list[str]] = {
            session_id: [] for session_id in latest.values()
        }
        for row in rows:
            people_by_session[int(row["session_id"])].append(str(row["person"]))
        return {
            name: people_by_session[session_id]
            for name, session_id in latest.items()
        }

    def clip_attributions(self, clip_names: Iterable[str]) -> dict[str, list[str]]:
        return self._clip_attributions(self.conn, clip_names)

    @classmethod
    def clip_attributions_from_path(
        cls, path: str, clip_names: Iterable[str]
    ) -> dict[str, list[str]]:
        """Read clip attributions through a connection owned by this thread."""
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return cls._clip_attributions(conn, clip_names)
        finally:
            conn.close()

    def abort_session(self, session_id: int) -> None:
        """Discard a session and everything it recorded.

        Used when a clip's processing is cancelled mid-run: streaks that already
        completed were committed by :meth:`record_attempt` (and may have bumped
        a person's denormalized ``high_score``), so simply stopping would leave
        phantom scores from an unwanted clip. This deletes the session (its
        attempts cascade away via the FK) and then recomputes the high score of
        every person the session touched from their *remaining* attempts."""
        rows = self.conn.execute(
            "SELECT DISTINCT person_id FROM attempts "
            "WHERE session_id = ? AND person_id IS NOT NULL",
            (session_id,),
        ).fetchall()
        pids = [int(r["person_id"]) for r in rows]
        # ON DELETE CASCADE (PRAGMA foreign_keys=ON) removes the attempts too.
        self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        for pid in pids:
            row = self.conn.execute(
                "SELECT MAX(count) AS hi FROM attempts WHERE person_id = ?",
                (pid,),
            ).fetchone()
            hi = int(row["hi"]) if row and row["hi"] is not None else 0
            self.conn.execute(
                "UPDATE people SET high_score = ? WHERE id = ?", (hi, pid)
            )
        self.conn.commit()

    def process_time_stats(self) -> tuple[float, float, int]:
        """Return (last_seconds, avg_seconds, count) over timed sessions."""
        last_row = self.conn.execute(
            "SELECT duration_s FROM sessions WHERE duration_s > 0 "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        agg = self.conn.execute(
            "SELECT AVG(duration_s) AS a, COUNT(*) AS c "
            "FROM sessions WHERE duration_s > 0"
        ).fetchone()
        last = float(last_row["duration_s"]) if last_row else 0.0
        avg = float(agg["a"]) if agg and agg["a"] is not None else 0.0
        count = int(agg["c"]) if agg else 0
        return round(last, 1), round(avg, 1), count

    def record_attempt(
        self,
        session_id: int,
        person_id: Optional[int],
        track_id: Optional[int],
        count: int,
        ended_reason: str,
    ) -> bool:
        """Record a completed juggle streak. Returns True if it's a new high score."""
        self.conn.execute(
            "INSERT INTO attempts(session_id, person_id, track_id, count, "
            "ended_reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, person_id, track_id, count, ended_reason, time.time()),
        )
        new_high = False
        if person_id is not None:
            row = self.conn.execute(
                "SELECT high_score FROM people WHERE id = ?", (person_id,)
            ).fetchone()
            if row and count > int(row["high_score"]):
                self.conn.execute(
                    "UPDATE people SET high_score = ? WHERE id = ?",
                    (count, person_id),
                )
                new_high = True
        self.conn.commit()
        return new_high

    def high_scores(self) -> dict[str, int]:
        return {
            r["name"]: int(r["high_score"]) for r in self.list_people()
        }

    def close(self) -> None:
        self.conn.close()



