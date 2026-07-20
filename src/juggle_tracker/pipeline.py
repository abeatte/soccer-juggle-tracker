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
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

from .capture import FrameSource
from .config import Config
from .db import Database
from .detect import Detector
from .ha_mqtt import HAPublisher, _slug
from .identity import (FaceEngine, Gallery, IdentityResolver, assign_face_to_track)
from .juggle import JuggleCounter
from .pose import PoseEstimator, person_center
from .thermal import ThermalGuard


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
        # Live status published to HA (idle / processing / cooldown + progress).
        self._cur_clip: Optional[str] = None
        self._cur_total: int = 0
        self._cur_frame: int = 0
        self._cur_pct: Optional[float] = None
        # Reflect persisted timing stats in HA immediately on startup.
        self.ha.publish_timing(*self.db.process_time_stats())
        # Announce every profile (enrolled + the Unknown catch-all) up front so
        # their sensors exist right after a service restart, not only after the
        # next clip. Retained + idempotent.
        self.ha.sync_all(self.db.list_people())
        self.thermal = ThermalGuard.from_config(
            cfg.raw,
            on_pause=self._on_thermal_pause,
        )

    def _on_thermal_pause(self, t: float) -> None:
        print(f"  [thermal] {t:.0f}C >= limit; pausing to cool...", flush=True)
        self.ha.publish_status(
            "cooldown", progress=self._cur_pct, current=self._cur_clip,
            frame=self._cur_frame, total=self._cur_total, temp_c=t,
        )

    # ------------------------------------------------------------------
    def process(self, clip_path: str, debug_video: Optional[str] = None) -> ClipResult:
        src = FrameSource(
            clip_path,
            roi=self.cfg.roi,
            infer_long_edge=int(self.cfg.processing.get("infer_long_edge", 960)),
        )
        stride = max(1, int(self.cfg.processing.get("person_stride", 3)))
        session_id = self.db.start_session(clip_path, src.fps)
        t0 = time.perf_counter()

        writer = None
        counter: Optional[JuggleCounter] = None
        streaks: list[dict] = []
        new_highs: list[dict] = []
        active_track: Optional[int] = None
        last_poses: dict[int, dict] = {}
        n_frames = 0

        # Publish "processing" + reset progress (total frames from the clip header).
        total = src.frame_count if getattr(src, "frame_count", 0) > 0 else 0
        self._cur_clip = clip_path
        self._cur_total = total
        self._cur_frame = 0
        self._cur_pct = 0.0
        self.ha.publish_status("processing", progress=0.0, current=clip_path,
                               frame=0, total=total)

        for frame in src:
            n_frames += 1
            img = frame.image
            h, w = img.shape[:2]
            # Thermal safety: check every ~2s of video and block if overheating.
            if frame.index % 50 == 0:
                self._cur_frame = frame.index
                self._cur_pct = (round(100.0 * frame.index / self._cur_total, 1)
                                 if self._cur_total else None)
                self.ha.publish_status("processing", progress=self._cur_pct,
                                       current=self._cur_clip, frame=frame.index,
                                       total=self._cur_total)
                self.thermal.maybe_wait()
            if counter is None:
                counter = self._new_counter(h)

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

        self.db.finish_session(session_id, n_frames, time.perf_counter() - t0)
        src.release()
        if writer is not None:
            writer.release()

        # Capture a replay clip for any new personal record BEFORE publishing,
        # so the sensor attributes carry the fresh video URL.
        if new_highs:
            self._capture_highscore_videos(clip_path, new_highs)

        # Publish to Home Assistant.
        self.ha.sync_all(self.db.list_people())
        self.ha.publish_session(streaks)
        for nh in new_highs:
            self.ha.fire_new_high_score(nh["person"], nh["score"])

        # Back to idle (progress 0) now the clip is done.
        self.ha.publish_status("idle", progress=0)
        self._cur_clip = None
        # Publish last + average processing time (persisted across restarts).
        self.ha.publish_timing(*self.db.process_time_stats())

        return ClipResult(clip_path, n_frames, streaks, new_highs)

    # ------------------------------------------------------------------
    def _record(self, session_id, track_id, event, streaks, new_highs):
        pid = self.resolver.resolve(track_id) if self.resolver else None
        if pid is None:
            # Couldn't attribute to an enrolled kid -> the catch-all profile,
            # so unattributed juggles still accrue a score/high-score/video.
            pid = self.db.unknown_person_id
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
            new_highs.append({"person": name, "score": event.count, "pid": pid})

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

    # ------------------------------------------------------------------
    def _new_counter(self, frame_height: int) -> JuggleCounter:
        """Build a JuggleCounter from config (shared by process + render pass)."""
        j = self.cfg.juggle
        return JuggleCounter(
            frame_height=frame_height,
            smooth_window=int(j.get("smooth_window", 5)),
            min_arc_px=float(j.get("min_arc_px", 18)),
            contact_radius_px=float(j.get("contact_radius_px", 90)),
            ground_y_frac=float(j.get("ground_y_frac", 0.92)),
            valid_keypoints=set(j.get("valid_keypoints", [])) or None,
            illegal_keypoints=set(j.get("illegal_keypoints", [])) or None,
        )

    def _render_annotated(self, clip_path: str, out_path: str) -> None:
        """Second pass over a clip that writes ONLY an annotated overlay video.

        Re-runs detection/pose/juggle-counting purely for the overlay (ball
        circle, pose keypoints, active-track box, live streak counter). It does
        NOT touch the database or publish scores — that already happened in the
        first pass. Reuses the already-loaded models. Runs only for clips that
        set a new record, so the cost is paid rarely. Honors the thermal guard."""
        src = FrameSource(
            clip_path,
            roi=self.cfg.roi,
            infer_long_edge=int(self.cfg.processing.get("infer_long_edge", 960)),
        )
        stride = max(1, int(self.cfg.processing.get("person_stride", 3)))
        writer = None
        counter: Optional[JuggleCounter] = None
        active_track: Optional[int] = None
        last_poses: dict[int, dict] = {}
        try:
            for frame in src:
                img = frame.image
                h = img.shape[0]
                if frame.index % 50 == 0:
                    self.thermal.maybe_wait()
                if counter is None:
                    counter = self._new_counter(h)
                det = self.detector.detect_track(img)
                ball_xy = det.ball.xy if det.ball else None
                if frame.index % stride == 0 and det.persons:
                    poses = self.pose.estimate(img)
                    last_poses = _match_pose_to_track(poses, det.persons)
                active = _nearest_person_to_ball(det.persons, ball_xy)
                if active is not None:
                    active_track = active.track_id
                kps = last_poses.get(active_track) if active_track is not None else None
                counter.update(frame.index, ball_xy, kps)
                writer = self._draw(
                    img, det, kps, counter, active_track, writer, out_path
                )
        finally:
            src.release()
            if writer is not None:
                writer.release()

    def _transcode_h264(self, src: str, dst: str) -> None:
        """Transcode ``src`` to browser-playable H.264/yuv420p via system ffmpeg.

        OpenCV on this no-AVX2 box can only write mp4v, which the HA dashboard /
        Chrome won't play inline. The system ffmpeg (``libx264``) produces a
        widely-playable file; ``veryfast`` keeps CPU/heat down and the overlay
        clip is only 640px so it's quick. ``+faststart`` moves the moov atom to
        the front for progressive web playback. Raises (``check=True``) on
        failure so the caller falls back to the raw clip."""
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", "-an", dst],
            check=True,
        )

    def _capture_highscore_videos(self, clip_path: str,
                                  new_highs: list[dict]) -> None:
        """Save/refresh the per-person high-score replay clip.

        One file per person (``<highscore_dir>/<slug>.mp4``) is overwritten in
        place via an atomic rename, so only the most-recent record clip is kept
        and the previous one is discarded. When ``annotate_highscore`` is set we
        do a quick annotated second pass; otherwise the raw clip is copied."""
        cap = self.cfg.capture
        if not bool(cap.get("save_highscore_video", True)):
            return
        hs_dir = cap.get("highscore_dir", "highscores")
        annotate = bool(cap.get("annotate_highscore", True))
        os.makedirs(hs_dir, exist_ok=True)
        ts = time.time()
        tmp = os.path.join(hs_dir, f".render_{int(ts)}.mp4")

        try:
            if annotate:
                # Reflect the extra work in HA's worker-state sensor.
                self.ha.publish_status("rendering", current=clip_path)
                raw = os.path.join(hs_dir, f".raw_{int(ts)}.mp4")
                try:
                    self._render_annotated(clip_path, raw)
                    if not os.path.exists(raw) or os.path.getsize(raw) == 0:
                        raise RuntimeError("annotated render produced no output")
                    # OpenCV can only emit mp4v on this (no-AVX2/no-H.264) box,
                    # which browsers / the HA dashboard won't play inline. The
                    # system ffmpeg (libx264) transcodes the small overlay clip
                    # to widely-playable H.264.
                    self._transcode_h264(raw, tmp)
                finally:
                    if os.path.exists(raw):
                        os.remove(raw)
            else:
                # Raw HA-recorded clip is already H.264 — just copy it.
                shutil.copyfile(clip_path, tmp)
        except Exception as exc:  # fall back to the raw clip; never lose a record
            print(f"  [highscore] annotate failed ({exc}); saving raw clip",
                  flush=True)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
                shutil.copyfile(clip_path, tmp)
            except Exception as exc2:
                print(f"  [highscore] could not save clip: {exc2}", flush=True)
                return

        try:
            for nh in new_highs:
                pid = nh.get("pid")
                if pid is None:
                    continue
                slug = _slug(nh["person"])
                final = os.path.join(hs_dir, f"{slug}.mp4")
                stage = final + ".part"
                shutil.copyfile(tmp, stage)
                os.replace(stage, final)  # atomic overwrite -> old clip replaced
                self.db.set_high_clip(pid, final, ts)
                print(f"  [highscore] {nh['person']}: saved replay -> {final}",
                      flush=True)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def close(self):
        self.ha.close()
        self.db.close()


def _prune_old_clips(directory: str, retention_days: int) -> None:
    """Delete video files older than ``retention_days`` from ``directory``.

    ``retention_days <= 0`` disables pruning (keep forever). Only touches video
    files; ignores other files and any I/O errors so it can never break the
    worker."""
    if retention_days <= 0:
        return
    cutoff = time.time() - retention_days * 86400
    exts = (".mp4", ".mkv", ".mov", ".avi")
    removed = 0
    try:
        entries = os.listdir(directory)
    except OSError:
        return
    for name in entries:
        if not name.lower().endswith(exts):
            continue
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    if removed:
        print(f"  [retention] removed {removed} clip(s) older than "
              f"{retention_days}d from {directory}", flush=True)


def move_to_processed(cfg: Config, clip_path: str) -> str:
    dst_dir = cfg.capture.processed_dir
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, os.path.basename(clip_path))
    shutil.move(clip_path, dst)
    # Enforce retention on the processed folder (0 = keep forever).
    _prune_old_clips(dst_dir, int(cfg.capture.get("processed_retention_days", 0) or 0))
    return dst
