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
from .ball_tracker import BallTracker
from .db import Database
from .detect import Detector, ClassicalBallDetector
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


class ClipCancelled(Exception):
    """Raised inside the processing loop when an HA "delete" targets the clip
    that is currently being processed. ``process()`` catches it to roll back the
    session's partial DB/HA writes, then re-raises so the watcher removes the
    clip instead of archiving it to the processed dir."""


class _FfmpegH264Writer:
    """Minimal cv2.VideoWriter-style sink that encodes H.264 *directly* by
    piping raw BGR frames into an ffmpeg subprocess.

    OpenCV on this no-AVX2 box can only write mp4v, which browsers won't play —
    so the old path wrote mp4v then ran a second ffmpeg transcode to H.264. This
    collapses that into a single streaming encode (no mp4v intermediate, no temp
    file, no re-decode) and is also slightly higher quality (no mp4v generation
    loss). Exposes ``write(frame)`` and ``release()`` so it drops into the
    existing overlay code. The ``scale`` filter rounds odd dims down to even
    (libx264 + yuv420p requires even), and ``+faststart`` enables progressive
    web playback."""

    def __init__(self, path: str, width: int, height: int, fps: float = 25.0):
        self.path = path
        self.proc = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "rawvideo", "-pix_fmt", "bgr24",
             "-s", f"{int(width)}x{int(height)}", "-r", f"{fps:g}", "-i", "-",
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
             "-movflags", "+faststart", "-an", path],
            stdin=subprocess.PIPE,
        )

    def write(self, frame) -> None:
        try:
            self.proc.stdin.write(frame.tobytes())
        except (BrokenPipeError, ValueError, AttributeError):
            pass  # ffmpeg died; release() surfaces it via a missing/empty file

    def release(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=180)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


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
        # Classical fallback ball finder (used only when YOLO misses the ball).
        self.ball_fallback = ClassicalBallDetector(cfg.ball_fallback)
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
        # Let the publisher signal cancellation back into a running clip when HA
        # "delete" targets the in-flight file.
        self.ha.bind_worker(self)
        # Live status published to HA (idle / processing / cooldown + progress).
        self._cur_clip: Optional[str] = None
        # Basename of a clip the operator asked to cancel; the frame loop checks
        # it and raises ClipCancelled when it matches the clip in flight.
        self._cancel_clip: Optional[str] = None
        # Pixels below the tracked juggler's feet (bbox bottom) at/under which a
        # ball low-point counts as a floor touch. Dynamic ground follows the
        # player instead of a fixed frame line.
        self._ground_margin = float(cfg.juggle.get("ground_margin_px", 40.0))
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
    def request_cancel(self, name: str) -> bool:
        """Ask the worker to cancel the clip currently being processed.

        Returns True if ``name`` matches the in-flight clip's basename (the
        frame loop will raise ClipCancelled shortly and the watcher will delete
        the file), or False if nothing matching is in flight — in which case the
        caller should handle it as a plain queued-file delete."""
        cur = self._cur_clip
        if cur and os.path.basename(cur) == name:
            self._cancel_clip = name
            return True
        return False

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
        ball_tracker = self._new_ball_tracker()
        streaks: list[dict] = []
        new_highs: list[dict] = []
        active_track: Optional[int] = None
        last_poses: dict[int, dict] = {}
        n_frames = 0

        # Publish "processing" + reset progress (total frames from the clip header).
        total = src.frame_count if getattr(src, "frame_count", 0) > 0 else 0
        self._cur_clip = clip_path
        self._cancel_clip = None  # clear any stale request from a prior clip
        self._cur_total = total
        self._cur_frame = 0
        self._cur_pct = 0.0
        self.ha.publish_status("processing", progress=0.0, current=clip_path,
                               frame=0, total=total)

        for frame in src:
            # Operator asked (via HA) to cancel THIS clip: tear down cleanly,
            # roll back everything the partial run wrote, then bail so the
            # watcher deletes the file instead of archiving it.
            if self._cancel_clip is not None:
                src.release()
                if writer is not None:
                    writer.release()
                self.db.abort_session(session_id)
                self.ha.sync_all(self.db.list_people())
                self.ha.publish_status("idle", progress=0)
                print(f"  [cancel] aborted {os.path.basename(clip_path)} "
                      f"({n_frames} frames in)", flush=True)
                self._cur_clip = None
                self._cancel_clip = None
                raise ClipCancelled(clip_path)
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
                # Keep the inbox/processed queue sensors live during long runs
                # (a clip can take many minutes on this box).
                if frame.index % 250 == 0:
                    self.ha.publish_queues()
            if counter is None:
                counter = self._new_counter(h)

            det = self.detector.detect_track(img)
            ball_det = det.ball.xy if det.ball else None
            if ball_det is None and self.ball_fallback.enabled:
                ball_det = self.ball_fallback.detect(img, ball_tracker.last_xy)
            ball_xy, ball_bridged = ball_tracker.update(frame.index, ball_det)

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

            # Dynamic ground = the tracked juggler's feet (bbox bottom) + margin;
            # None when no one is detected -> counter uses the static fallback.
            ground_y = (float(active.xyxy[3]) + self._ground_margin
                        if active is not None else None)

            event = counter.update(frame.index, ball_xy, kps, ground_y=ground_y)
            if event is not None and event.count > 0:
                self._record(session_id, active_track, event, streaks, new_highs)

            if debug_video:
                writer = self._draw(
                    img, det, ball_xy, ball_bridged, kps, counter,
                    active_track, writer, debug_video, ground_y=ground_y
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
    def process_annotated_viewable(self, clip_path: str) -> ClipResult:
        """Process a clip AND always produce a browser-playable annotated replay
        of the whole clip, regardless of whether it set a record.

        The overlay is drawn during the normal detection pass (via the
        ``debug_video`` output of :meth:`process`), which now encodes H.264
        directly through an ffmpeg pipe — so this costs NO extra transcode and
        NOT a second inference pass. The result is written to a single
        overwritten file (<highscore_dir>/last_reprocessed.mp4) and its URL is
        published to HA (sensor.juggle_last_reprocessed)."""
        hs_dir = self.cfg.capture.get("highscore_dir", "highscores")
        os.makedirs(hs_dir, exist_ok=True)
        ts = time.time()
        final = os.path.join(hs_dir, "last_reprocessed.mp4")
        stage = os.path.join(hs_dir, f".reproc_{int(ts)}.mp4")
        # process() draws the overlay straight to `stage` as browser-playable
        # H.264 (via _FfmpegH264Writer) as it runs.
        try:
            res = self.process(clip_path, debug_video=stage)
        except BaseException:
            # Cancelled or errored mid-run: don't leave a partial overlay temp.
            if os.path.exists(stage):
                os.remove(stage)
            raise
        if os.path.exists(stage) and os.path.getsize(stage) > 0:
            os.replace(stage, final)  # atomic overwrite of the single file
            self.ha.publish_last_reprocessed(clip_path, ts)
            print(f"  [reprocess] annotated replay -> {final}", flush=True)
        else:
            # No overlay frames (empty clip / encode failed): hide the card.
            if os.path.exists(stage):
                os.remove(stage)
            print("  [reprocess] no overlay frames written (empty clip?)",
                  flush=True)
            self.ha.publish_reprocess_pending(clip_path, annotated=False)
        return res

    def _record(self, session_id, track_id, event, streaks, new_highs) -> None:
        """Attribute a completed streak to a person, persist it, and collect it
        for HA publishing. Called from process() when a streak ends."""
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

    def _draw(self, img, det, ball_xy, ball_bridged, kps, counter, active_track,
              writer, path, ground_y=None):
        vis = img.copy()
        h_img, w_img = vis.shape[:2]
        # ROI border — the analysis frame IS the ROI crop, so this hugs the edge
        # (a reminder of what's in play). Ground line = floor-touch boundary.
        cv2.rectangle(vis, (1, 1), (w_img - 2, h_img - 2), (200, 200, 200), 1)
        cv2.putText(vis, "ROI", (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                    (200, 200, 200), 1)
        # Ground line = floor-touch boundary: the tracked juggler's feet +
        # margin. Only drawn on frames where a juggler is detected.
        gy = int(ground_y) if ground_y is not None else None
        if gy is not None and 0 <= gy < h_img:
            cv2.line(vis, (0, gy), (w_img, gy), (0, 0, 255), 1)
            cv2.putText(vis, "ground", (4, max(12, gy - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)
        for p in det.persons:
            x0, y0, x1, y1 = (int(v) for v in p.xyxy)
            color = (0, 255, 0) if p.track_id == active_track else (160, 160, 160)
            cv2.rectangle(vis, (x0, y0), (x1, y1), color, 2)
            cv2.putText(vis, f"#{p.track_id}", (x0, y0 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        if det.ball:
            bx, by = (int(v) for v in det.ball.xy)
            cv2.circle(vis, (bx, by), max(4, int(det.ball.r)), (0, 128, 255), 2)
        elif ball_xy is not None:
            # No YOLO ball: either a bridged prediction (yellow) or a classical
            # fallback detection (magenta).
            bx, by = int(ball_xy[0]), int(ball_xy[1])
            color = (0, 255, 255) if ball_bridged else (255, 0, 255)
            label = "bridge" if ball_bridged else "cv"
            cv2.circle(vis, (bx, by), 8, color, 1)
            cv2.putText(vis, label, (bx + 10, by),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
        # Fallback search window (where the classical detector looks next frame).
        if self.ball_fallback.enabled and ball_xy is not None:
            sr = int(self.ball_fallback.search_radius)
            if sr > 0:
                cx, cy = int(ball_xy[0]), int(ball_xy[1])
                cv2.rectangle(vis, (cx - sr, cy - sr), (cx + sr, cy + sr),
                              (255, 255, 0), 1)
        if kps:
            for _, (x, y, c) in kps.items():
                if c >= 0.2:
                    cv2.circle(vis, (int(x), int(y)), 3, (255, 0, 255), -1)
        cv2.putText(vis, f"streak: {counter.current_streak}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)
        if writer is None:
            h, w = vis.shape[:2]
            # Encode H.264 directly via an ffmpeg pipe — no mp4v intermediate
            # and no separate transcode pass.
            writer = _FfmpegH264Writer(path, w, h, fps=25.0)
        writer.write(vis)
        return writer

    # ------------------------------------------------------------------
    def _new_ball_tracker(self) -> BallTracker:
        return BallTracker(
            max_bridge_frames=int(self.cfg.juggle.get("max_bridge_frames", 8))
        )

    def _new_counter(self, frame_height: int) -> JuggleCounter:
        """Build a JuggleCounter from config (shared by process + render pass)."""
        j = self.cfg.juggle
        return JuggleCounter(
            frame_height=frame_height,
            smooth_window=int(j.get("smooth_window", 5)),
            min_arc_px=float(j.get("min_arc_px", 18)),
            contact_radius_px=float(j.get("contact_radius_px", 90)),
            valid_keypoints=set(j.get("valid_keypoints", [])) or None,
            illegal_keypoints=set(j.get("illegal_keypoints", [])) or None,
            lost_frames_reset=int(j.get("lost_frames_reset", 15)),
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
        ball_tracker = self._new_ball_tracker()
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
                ball_det = det.ball.xy if det.ball else None
                if ball_det is None and self.ball_fallback.enabled:
                    ball_det = self.ball_fallback.detect(img, ball_tracker.last_xy)
                ball_xy, ball_bridged = ball_tracker.update(frame.index, ball_det)
                if frame.index % stride == 0 and det.persons:
                    poses = self.pose.estimate(img)
                    last_poses = _match_pose_to_track(poses, det.persons)
                active = _nearest_person_to_ball(det.persons, ball_xy)
                if active is not None:
                    active_track = active.track_id
                kps = last_poses.get(active_track) if active_track is not None else None
                ground_y = (float(active.xyxy[3]) + self._ground_margin
                            if active is not None else None)
                counter.update(frame.index, ball_xy, kps, ground_y=ground_y)
                writer = self._draw(
                    img, det, ball_xy, ball_bridged, kps, counter,
                    active_track, writer, out_path, ground_y=ground_y
                )
        finally:
            src.release()
            if writer is not None:
                writer.release()

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
                # _render_annotated encodes H.264 straight to `tmp` via the
                # ffmpeg pipe — no mp4v intermediate, no separate transcode.
                self._render_annotated(clip_path, tmp)
                if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
                    raise RuntimeError("annotated render produced no output")
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


def _safe_int(value, default: int = 0) -> int:
    """Coerce a config value to int, tolerating stray trailing text/comments
    (e.g. a hand-edited 'processed_retention_days: 14 $ ...'). Recovers a leading
    integer when possible, else returns ``default`` — never raises, so a
    malformed config value can't crash (and infinitely retry) the pipeline."""
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(str(value).strip().split()[0])
        except (TypeError, ValueError, IndexError):
            return default


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
    if os.path.abspath(clip_path) != os.path.abspath(dst):
        if os.path.exists(dst):
            # A reprocess runs against a COPY placed in the inbox while the
            # original stays in `processed`. The archived original is
            # authoritative, so discard the inbox copy rather than clobber it
            # (this also means deleting a queued reprocess never loses a clip).
            os.remove(clip_path)
        else:
            shutil.move(clip_path, dst)
    # Enforce retention on the processed folder (0 = keep forever).
    _prune_old_clips(dst_dir, _safe_int(cfg.capture.get("processed_retention_days", 0)))
    return dst


def move_to_failed(cfg: Config, clip_path: str) -> Optional[str]:
    """Quarantine a clip that crashed during processing so the watcher can't
    retry it forever (one bad clip would otherwise stall the whole queue).

    Moves the clip (and any sidecar .annotate marker) to capture.failed_dir
    (default: a 'juggle_failed' sibling of the processed dir). Never raises."""
    processed = cfg.capture.processed_dir
    failed_dir = cfg.capture.get("failed_dir") or os.path.join(
        os.path.dirname(processed.rstrip("/")) or ".", "juggle_failed")
    try:
        os.makedirs(failed_dir, exist_ok=True)
        dst = os.path.join(failed_dir, os.path.basename(clip_path))
        if os.path.exists(dst):
            os.remove(dst)  # overwrite an older quarantine of the same name
        shutil.move(clip_path, dst)
        marker = clip_path + ".annotate"
        if os.path.exists(marker):
            try:
                mdst = dst + ".annotate"
                if os.path.exists(mdst):
                    os.remove(mdst)
                shutil.move(marker, mdst)
            except OSError:
                pass
        return dst
    except Exception as exc:  # never let quarantine itself break the worker
        print(f"  [failed] could not quarantine "
              f"{os.path.basename(clip_path)}: {exc}", flush=True)
        return None
