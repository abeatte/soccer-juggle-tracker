"""SQLite persistence: people, face embeddings, sessions, and high scores.

Schema
------
people        : one row per enrolled person (name + all-time high score)
face_embeds   : one row per enrolled face embedding (multiple per person)
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

import numpy as np

# Reserved profile that catches every juggle session we can't attribute to an
# enrolled person. It's a normal `people` row but has NO face embeddings, so it
# can never be a match target — it only ever receives the `pid is None` fallback.
UNKNOWN_NAME = "Unknown Juggler"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    high_score  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS face_embeds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    embedding   BLOB NOT NULL,          -- float32 numpy bytes
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_path   TEXT NOT NULL,
    started_at  REAL NOT NULL,
    frames      INTEGER NOT NULL DEFAULT 0,
    fps         REAL NOT NULL DEFAULT 0,
    duration_s  REAL NOT NULL DEFAULT 0
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
        # High-score replay clip: path to the most-recent high-score video for
        # this person, and when it was captured (epoch). Overwritten in place
        # whenever the record is beaten, so only the latest is ever kept.
        self._ensure_column("people", "high_clip", "TEXT")
        self._ensure_column("people", "high_clip_at", "REAL")
        self.conn.commit()
        # Guarantee the catch-all profile exists so it shows alongside enrolled
        # kids. It has no embeddings, so the face gallery ignores it.
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

    def add_embedding(self, person_id: int, embedding: np.ndarray) -> None:
        emb = np.asarray(embedding, dtype=np.float32).tobytes()
        self.conn.execute(
            "INSERT INTO face_embeds(person_id, embedding, created_at) VALUES (?, ?, ?)",
            (person_id, emb, time.time()),
        )
        self.conn.commit()

    def load_gallery(self) -> tuple[list[int], list[str], np.ndarray]:
        """Return (person_ids, names, embeddings[N,D]) for all enrolled faces."""
        rows = self.conn.execute(
            "SELECT fe.person_id, p.name, fe.embedding "
            "FROM face_embeds fe JOIN people p ON p.id = fe.person_id"
        ).fetchall()
        if not rows:
            return [], [], np.zeros((0, 512), dtype=np.float32)
        ids = [int(r["person_id"]) for r in rows]
        names = [str(r["name"]) for r in rows]
        embs = np.stack(
            [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
        )
        return ids, names, embs

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
    def start_session(self, clip_path: str, fps: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions(clip_path, started_at, fps) VALUES (?, ?, ?)",
            (clip_path, time.time(), fps),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_session(self, session_id: int, frames: int,
                       duration_s: float = 0.0) -> None:
        self.conn.execute(
            "UPDATE sessions SET frames = ?, duration_s = ? WHERE id = ?",
            (frames, float(duration_s), session_id),
        )
        self.conn.commit()

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


def reassign_current_high(conn, source_id: int, target_id: int) -> Optional[dict]:
    """Move a person's *current* high-score attempt to another person.

    This fixes a misattribution — e.g. an "Unknown Juggler" record that was
    actually one of the enrolled kids. It re-points the source's single best
    attempt (its max ``count``, which is exactly the streak that set the source's
    denormalized ``high_score`` and whose replay clip is ``people.high_clip``) to
    ``target_id``, then recomputes ``high_score`` for BOTH people from their
    remaining attempts (same ``MAX(count)`` rule as :meth:`Database.abort_session`).

    It deliberately does NOT touch ``high_clip`` / the replay ``.mp4`` and does
    NOT commit: the caller reconciles the video file and commits, so the whole
    reassignment lands as one transaction. This keeps DB logic free of any
    filesystem/slug concerns (which live in the HA publisher) and unit-testable.

    Runs on whatever connection the caller supplies — the MQTT handler passes a
    short-lived per-thread connection (SQLite connections aren't shareable across
    threads), tests pass a temp DB. Never uses a module-global connection.

    Returns a summary dict, or ``None`` when the source/target id is unknown or
    the source has no attempt to move::

        {
          "moved_attempt_id": int,     # the attempts.id that was re-pointed
          "moved_count":      int,     # its juggle count (the score being moved)
          "src_new_high":     int,     # source high score after removal
          "tgt_old_high":     int,     # target high score before the move
          "tgt_new_high":     int,     # target high score after the move
          "src_high_clip":    str|None,# source's replay path before the move
          "src_high_clip_at": float|None,
        }
    """
    src = conn.execute(
        "SELECT high_score, high_clip, high_clip_at FROM people WHERE id = ?",
        (source_id,),
    ).fetchone()
    tgt = conn.execute(
        "SELECT high_score FROM people WHERE id = ?", (target_id,)
    ).fetchone()
    if src is None or tgt is None:
        return None
    # The source's current high score == its highest-count attempt, and that is
    # the attempt whose clip is stored in people.high_clip. Ties broken by most
    # recent so we move the freshest clip.
    best = conn.execute(
        "SELECT id, count FROM attempts WHERE person_id = ? "
        "ORDER BY count DESC, created_at DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    if best is None:
        return None
    moved_id = int(best["id"])
    moved_count = int(best["count"])
    tgt_old_high = int(tgt["high_score"])

    conn.execute(
        "UPDATE attempts SET person_id = ? WHERE id = ?", (target_id, moved_id)
    )

    def _recompute(pid: int) -> int:
        row = conn.execute(
            "SELECT MAX(count) AS hi FROM attempts WHERE person_id = ?", (pid,)
        ).fetchone()
        hi = int(row["hi"]) if row and row["hi"] is not None else 0
        conn.execute("UPDATE people SET high_score = ? WHERE id = ?", (hi, pid))
        return hi

    src_new_high = _recompute(source_id)
    tgt_new_high = _recompute(target_id)
    return {
        "moved_attempt_id": moved_id,
        "moved_count": moved_count,
        "src_new_high": src_new_high,
        "tgt_old_high": tgt_old_high,
        "tgt_new_high": tgt_new_high,
        "src_high_clip": src["high_clip"],
        "src_high_clip_at": src["high_clip_at"],
    }
