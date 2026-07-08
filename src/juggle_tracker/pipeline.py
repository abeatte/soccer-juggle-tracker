"""Pipeline: process one clip end-to-end.

For each frame:
  1. detect + track persons and the ball (every frame — the ball is fast)
  2. every ``person_stride`` frames also run pose + face identity
  3. feed (ball, keypoints of the active juggler) to the JuggleCounter
  4. persist completed streaks, update high scores, publish to Home Assistant
  5. (optional) draw a debug overlay video

"One kid juggling at a time" simplifies step 3: we attribute the ball to the
person nearest the ball, and use that person's pose + resolved identity.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

from .capture import FrameSource
from .config import Config
from .db import Database
from .detect import Detector
from .ha_mqtt import HAPublisher
from .identity import (FaceEngine, Gallery, IdentityResolver, assign_face_to_track)
from .juggle import JuggleCounter
from .pose import PoseEstimator, person_center


@dataclass
class ClipResult:
    clip: str
    frames: int
    streaks: list[dict]
    new_high_scores: list[dict]


def _nearest_person_to_ball(persons, ball_xy):
    if not persons or ball_xy is None:
        return None
    bx, by = ball_xy
    best, best_d = None, float("inf")
    for p in persons:
        cx, cy = p.centroid
        d = ((cx - bx) ** 2 + (cy - by) ** 2) ** 0.5
        if d < best_d:
            best_d, best = d, p
    return best


def _match_pose_to_track(poses, persons):
    """Map each PersonBox.track_id -> keypoints by nearest torso center."""
    out = {}
    for p in persons:
        cx, cy = p.centroid
        best, best_d = None, float("inf")
        for kp in poses:
            c = person_center(kp)
            if c is None:
                continue
            d = ((c[0] - cx) ** 2 + (c[1] - cy) ** 2) ** 0.5
            if d < best_d:
                best_d, best = d, kp
        if best is not None:
            out[p.track_id] = best
    return out


class Pipeline:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.database.path)
        self.detector = Detector(
            cfg.models.detector,
            person_conf=float(cfg.models.get("person_conf", 0.35)),
            ball_conf=float(cfg.models.get("ball_conf", 0.20)),
            torch_threads=int(cfg.processing.get("torch_threads", 0)),
        )
        self.pose = PoseEstimator(cfg.models.pose)
        # Identity is optional — only if people are enrolled.
        ids, names, embs = self.db.load_gallery()
        self.has_identity = len(ids) > 0
        self.face: Optional[FaceEngine] = None
        self.resolver: Optional[IdentityResolver] = None
        if self.has_identity:
            self.face = FaceEngine(
                pack=cfg.models.get("face_pack", "buffalo_s"),
                det_conf=float(cfg.models.get("face_conf", 0.45)),
            )
            gallery = Gallery(
                ids, names, embs,
                threshold=float(cfg.identity.get("match_threshold", 0.42)),
            )
            self.resolver = IdentityResolver(
                gallery=gallery,
                vote_min_frames=int(cfg.identity.get("vote_min_frames", 3)),
            )
        self.ha = HAPublisher(cfg)

    # ------------------------------------------------------------------
    def process(self, clip_path: str, debug_video: Optional[str] = None) -> ClipResult:
        src = FrameSource(
            clip_path,
            roi=self.cfg.roi,
            infer_long_edge=int(self.cfg.processing.get("infer_long_edge", 960)),
        )
        stride = max(1, int(self.cfg.processing.get("person_stride", 3)))
        session_id = self.db.start_session(clip_path, src.fps)

        writer = None
        counter: Optional[JuggleCounter] = None
        streaks: list[dict] = []
        new_highs: list[dict] = []
        active_track: Optional[int] = None
        last_poses: dict[int, dict] = {}
        n_frames = 0

        for frame in src:
            n_frames += 1
            img = frame.image
            h, w = img.shape[:2]
            if counter is None:
                counter = JuggleCounter(
                    frame_height=h,
                    smooth_window=int(self.cfg.juggle.get("smooth_window", 5)),
                    min_arc_px=float(self.cfg.juggle.get("min_arc_px", 18)),
                    contact_radius_px=float(self.cfg.juggle.get("contact_radius_px", 90)),
                    ground_y_frac=float(self.cfg.juggle.get("ground_y_frac", 0.92)),
                    valid_keypoints=set(self.cfg.juggle.get("valid_keypoints", [])) or None,
                    illegal_keypoints=set(self.cfg.juggle.get("illegal_keypoints", [])) or None,
                )

            det = self.detector.detect_track(img)
            ball_xy = det.ball.xy if det.ball else None

            # Pose + identity on stride frames.
            if frame.index % stride == 0 and det.persons:
                poses = self.pose.estimate(img)
                last_poses = _match_pose_to_track(poses, det.persons)
                if self.has_identity and self.face is not None:
                    for emb, fc in self.face.embeddings(img):
                        pid, _ = self.resolver.gallery.match(emb)
                        tid = assign_face_to_track(fc, det.persons)
                        if tid is not None:
                            self.resolver.observe(tid, pid)

            # Attribute the ball to the nearest person (one-kid scenario).
            active = _nearest_person_to_ball(det.persons, ball_xy)
            if active is not None:
                active_track = active.track_id
            kps = last_poses.get(active_track) if active_track is not None else None

            event = counter.update(frame.index, ball_xy, kps)
            if event is not None and event.count > 0:
                self._record(session_id, active_track, event, streaks, new_highs)

            if debug_video:
                writer = self._draw(
                    img, det, kps, counter, active_track, writer, debug_video
                )

        # Flush trailing streak.
        if counter is not None:
            ev = counter.flush(n_frames)
            if ev is not None and ev.count > 0:
                self._record(session_id, active_track, ev, streaks, new_highs)

        self.db.finish_session(session_id, n_frames)
        src.release()
        if writer is not None:
            writer.release()

        # Publish to Home Assistant.
        self.ha.sync_all(self.db.high_scores())
        self.ha.publish_session(streaks)
        for nh in new_highs:
            self.ha.fire_new_high_score(nh["person"], nh["score"])

        return ClipResult(clip_path, n_frames, streaks, new_highs)

    # ------------------------------------------------------------------
    def _record(self, session_id, track_id, event, streaks, new_highs):
        pid = self.resolver.resolve(track_id) if self.resolver else None
        is_high = self.db.record_attempt(
            session_id, pid, track_id, event.count, event.ended_reason
        )
        name = self.db.person_name(pid)
        streaks.append(
            {
                "person": name,
                "count": event.count,
                "reason": event.ended_reason,
                "start_frame": event.start_frame,
                "end_frame": event.end_frame,
            }
        )
        if is_high:
            new_highs.append({"person": name, "score": event.count})

    def _draw(self, img, det, kps, counter, active_track, writer, path):
        vis = img.copy()
        for p in det.persons:
            x0, y0, x1, y1 = (int(v) for v in p.xyxy)
            color = (0, 255, 0) if p.track_id == active_track else (160, 160, 160)
            cv2.rectangle(vis, (x0, y0), (x1, y1), color, 2)
            cv2.putText(vis, f"#{p.track_id}", (x0, y0 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        if det.ball:
            bx, by = (int(v) for v in det.ball.xy)
            cv2.circle(vis, (bx, by), max(4, int(det.ball.r)), (0, 128, 255), 2)
        if kps:
            for _, (x, y, c) in kps.items():
                if c >= 0.2:
                    cv2.circle(vis, (int(x), int(y)), 3, (255, 0, 255), -1)
        cv2.putText(vis, f"streak: {counter.current_streak}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)
        if writer is None:
            h, w = vis.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(path, fourcc, 25.0, (w, h))
        writer.write(vis)
        return writer

    def close(self):
        self.ha.close()
        self.db.close()


def move_to_processed(cfg: Config, clip_path: str) -> str:
    dst_dir = cfg.capture.processed_dir
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, os.path.basename(clip_path))
    shutil.move(clip_path, dst)
    return dst
