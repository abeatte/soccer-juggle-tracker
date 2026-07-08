"""Identity: manual enrollment + face-recognition assist.

Uses InsightFace (ONNX, CPU) to compute 512-d face embeddings. Enrolled people
have their embeddings stored in SQLite. At analysis time we compute face
embeddings on frames where a face is visible, match against the enrolled gallery
by cosine similarity, and vote over several frames to lock a *track* to a
*person*. Faces at yard distance are often too small to read every frame, so
voting + track continuity carries the identity through the rest of the session.

If no confident match is found, the track stays "Unknown" and its attempts are
still recorded (person_id NULL) but do not update any high score.
"""
from __future__ import annotations

import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


def _normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


class FaceEngine:
    """Thin wrapper over InsightFace FaceAnalysis (detection + embedding)."""

    def __init__(self, pack: str = "buffalo_s", det_conf: float = 0.45):
        from insightface.app import FaceAnalysis

        self.app = FaceAnalysis(name=pack, providers=["CPUExecutionProvider"])
        # ctx_id < 0 forces CPU; det_size kept modest for speed.
        self.app.prepare(ctx_id=-1, det_size=(640, 640))
        self.det_conf = det_conf

    def embeddings(self, image: np.ndarray) -> list[tuple[np.ndarray, tuple[float, float]]]:
        """Return [(embedding, face_center_xy), ...] for faces in the image."""
        faces = self.app.get(image)
        out = []
        for f in faces:
            if getattr(f, "det_score", 1.0) < self.det_conf:
                continue
            emb = _normalize(np.asarray(f.normed_embedding, dtype=np.float32))
            x0, y0, x1, y1 = f.bbox
            out.append((emb, ((x0 + x1) / 2.0, (y0 + y1) / 2.0)))
        return out

    def best_single(self, image: np.ndarray) -> Optional[np.ndarray]:
        """Largest/most-confident single embedding — used during enrollment."""
        faces = self.app.get(image)
        if not faces:
            return None
        faces.sort(key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        return _normalize(np.asarray(faces[-1].normed_embedding, dtype=np.float32))


class Gallery:
    """Enrolled embeddings + cosine matcher."""

    def __init__(self, person_ids: list[int], names: list[str], embeds: np.ndarray,
                 threshold: float = 0.42):
        self.person_ids = np.asarray(person_ids, dtype=np.int64)
        self.names = names
        self.embeds = embeds if embeds.size else np.zeros((0, 512), np.float32)
        self.threshold = threshold

    def match(self, emb: np.ndarray) -> tuple[Optional[int], float]:
        """Return (person_id, similarity) for the best gallery match, or (None, sim)."""
        if self.embeds.shape[0] == 0:
            return None, 0.0
        sims = self.embeds @ emb  # both L2-normalized -> cosine similarity
        j = int(np.argmax(sims))
        best = float(sims[j])
        if best >= self.threshold:
            return int(self.person_ids[j]), best
        return None, best


@dataclass
class IdentityResolver:
    """Votes face matches per track to lock a stable person identity."""

    gallery: Gallery
    vote_min_frames: int = 3
    _votes: dict[int, Counter] = field(default_factory=lambda: defaultdict(Counter))
    _locked: dict[int, int] = field(default_factory=dict)

    def observe(self, track_id: int, person_id: Optional[int]) -> None:
        if person_id is not None:
            self._votes[track_id][person_id] += 1
            top, n = self._votes[track_id].most_common(1)[0]
            if n >= self.vote_min_frames:
                self._locked[track_id] = top

    def resolve(self, track_id: int) -> Optional[int]:
        """Best-known person_id for a track (locked, else current plurality)."""
        if track_id in self._locked:
            return self._locked[track_id]
        votes = self._votes.get(track_id)
        if votes:
            return votes.most_common(1)[0][0]
        return None


def assign_face_to_track(
    face_center: tuple[float, float],
    persons,  # list[PersonBox]
) -> Optional[int]:
    """Return the track_id of the person box whose head region contains the face."""
    fx, fy = face_center
    best_tid = None
    best_area = float("inf")
    for p in persons:
        x0, y0, x1, y1 = p.xyxy
        if x0 <= fx <= x1 and y0 <= fy <= y1:
            area = (x1 - x0) * (y1 - y0)
            if area < best_area:  # smallest containing box = most specific
                best_area = area
                best_tid = p.track_id
    return best_tid
