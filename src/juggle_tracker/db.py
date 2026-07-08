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
    fps         REAL NOT NULL DEFAULT 0
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
            "SELECT id, name, high_score FROM people ORDER BY high_score DESC, name"
        ).fetchall()

    def person_name(self, person_id: Optional[int]) -> str:
        if person_id is None:
            return "Unknown"
        row = self.conn.execute(
            "SELECT name FROM people WHERE id = ?", (person_id,)
        ).fetchone()
        return row["name"] if row else "Unknown"

    # ---- sessions / attempts -------------------------------------------
    def start_session(self, clip_path: str, fps: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions(clip_path, started_at, fps) VALUES (?, ?, ?)",
            (clip_path, time.time(), fps),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_session(self, session_id: int, frames: int) -> None:
        self.conn.execute(
            "UPDATE sessions SET frames = ? WHERE id = ?", (frames, session_id)
        )
        self.conn.commit()

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
