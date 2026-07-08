"""Command-line interface.

    python -m juggle_tracker.cli enroll  --name "Kid1"   [--images DIR | --webcam]
    python -m juggle_tracker.cli process CLIP.mp4        [--debug-video out.mp4]
    python -m juggle_tracker.cli watch                    # batch worker: watch inbox
    python -m juggle_tracker.cli scores                   # print the scoreboard
    python -m juggle_tracker.cli record  --seconds 30     # grab a clip from RTSP
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time

from .config import load_config


def _enroll(args) -> int:
    import cv2
    from .db import Database
    from .identity import FaceEngine

    cfg = load_config(args.config)
    db = Database(cfg.database.path)

    people = db.list_people()
    if len(people) >= int(cfg.identity.get("max_people", 4)) and \
            args.name not in [p["name"] for p in people]:
        print(f"Max people ({cfg.identity.get('max_people')}) reached.", file=sys.stderr)
        return 1

    face = FaceEngine(pack=cfg.models.get("face_pack", "buffalo_s"))
    pid = db.add_person(args.name)
    added = 0

    if args.images:
        paths = []
        for ext in ("*.jpg", "*.jpeg", "*.png"):
            paths += glob.glob(os.path.join(args.images, ext))
        for p in sorted(paths):
            img = cv2.imread(p)
            if img is None:
                continue
            emb = face.best_single(img)
            if emb is not None:
                db.add_embedding(pid, emb)
                added += 1
        print(f"Enrolled {args.name}: {added} embeddings from {len(paths)} images.")
    else:
        # Webcam capture: press SPACE to grab a shot, Q to finish.
        cap = cv2.VideoCapture(args.webcam_index)
        if not cap.isOpened():
            print("Cannot open webcam. Use --images DIR instead.", file=sys.stderr)
            return 1
        print("Webcam enrollment: SPACE = capture, Q = quit. Aim for 10-20 shots, "
              "varied angles/distances.")
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            disp = frame.copy()
            cv2.putText(disp, f"{args.name}: {added} shots (SPACE=grab Q=quit)",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.imshow("enroll", disp)
            key = cv2.waitKey(1) & 0xFF
            if key == ord(" "):
                emb = face.best_single(frame)
                if emb is not None:
                    db.add_embedding(pid, emb)
                    added += 1
                    print(f"  captured ({added})")
                else:
                    print("  no face found, try again")
            elif key in (ord("q"), 27):
                break
        cap.release()
        cv2.destroyAllWindows()
        print(f"Enrolled {args.name}: {added} embeddings.")

    db.close()
    return 0 if added > 0 else 2


def _process(args) -> int:
    from .pipeline import Pipeline, move_to_processed

    cfg = load_config(args.config)
    pipe = Pipeline(cfg)
    t0 = time.time()
    res = pipe.process(args.clip, debug_video=args.debug_video)
    dt = time.time() - t0
    print(f"\nProcessed {os.path.basename(args.clip)}: {res.frames} frames in "
          f"{dt:.1f}s ({res.frames / dt:.1f} fps analysis)")
    if res.streaks:
        print("Streaks:")
        for s in res.streaks:
            print(f"  {s['person']:<12} {s['count']:>3} juggles  (ended: {s['reason']})")
    else:
        print("No juggle streaks detected.")
    for nh in res.new_high_scores:
        print(f"  *** NEW HIGH SCORE: {nh['person']} = {nh['score']} ***")
    if args.move:
        move_to_processed(cfg, args.clip)
    pipe.close()
    return 0


def _watch(args) -> int:
    from .pipeline import Pipeline, move_to_processed

    cfg = load_config(args.config)
    inbox = cfg.capture.inbox_dir
    os.makedirs(inbox, exist_ok=True)
    print(f"Watching {inbox} for new clips (Ctrl-C to stop)...")
    pipe = Pipeline(cfg)
    try:
        while True:
            clips = []
            for ext in ("*.mp4", "*.mkv", "*.mov"):
                clips += glob.glob(os.path.join(inbox, ext))
            for clip in sorted(clips):
                # Wait until the file stops growing (finished recording).
                if not _is_stable(clip):
                    continue
                print(f"\n-> {os.path.basename(clip)}")
                try:
                    res = pipe.process(clip)
                    for s in res.streaks:
                        print(f"   {s['person']}: {s['count']} ({s['reason']})")
                    move_to_processed(cfg, clip)
                except Exception as exc:  # keep the worker alive
                    print(f"   ERROR processing {clip}: {exc}", file=sys.stderr)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopping watcher.")
    finally:
        pipe.close()
    return 0


def _is_stable(path: str, checks: int = 2, delay: float = 1.0) -> bool:
    try:
        last = -1
        for _ in range(checks):
            size = os.path.getsize(path)
            if size == last:
                return True
            last = size
            time.sleep(delay)
        return os.path.getsize(path) == last
    except OSError:
        return False


def _scores(args) -> int:
    from .db import Database

    cfg = load_config(args.config)
    db = Database(cfg.database.path)
    rows = db.list_people()
    if not rows:
        print("No people enrolled yet. Run: enroll --name \"Kid1\"")
        return 0
    print(f"{'Rank':<5}{'Name':<16}{'High Score':>10}")
    print("-" * 31)
    for i, r in enumerate(rows, 1):
        print(f"{i:<5}{r['name']:<16}{r['high_score']:>10}")
    db.close()
    return 0


def _record(args) -> int:
    from .capture import record_clip

    cfg = load_config(args.config)
    os.makedirs(cfg.capture.inbox_dir, exist_ok=True)
    out = os.path.join(cfg.capture.inbox_dir, f"clip_{int(time.time())}.mp4")
    secs = args.seconds or int(cfg.capture.get("clip_seconds", 30))
    print(f"Recording {secs}s to {out} ...")
    record_clip(cfg.camera.rtsp_main, out, secs)
    print("Done.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="juggle_tracker", description=__doc__)
    p.add_argument("--config", default=None, help="Path to config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("enroll", help="Enroll a known person's face")
    pe.add_argument("--name", required=True)
    pe.add_argument("--images", help="Directory of face images (instead of webcam)")
    pe.add_argument("--webcam-index", type=int, default=0)
    pe.set_defaults(func=_enroll)

    pp = sub.add_parser("process", help="Process a single clip")
    pp.add_argument("clip")
    pp.add_argument("--debug-video", help="Write an annotated debug video here")
    pp.add_argument("--move", action="store_true", help="Move clip to processed/ after")
    pp.set_defaults(func=_process)

    pw = sub.add_parser("watch", help="Batch worker: watch the inbox folder")
    pw.add_argument("--interval", type=float, default=5.0)
    pw.set_defaults(func=_watch)

    ps = sub.add_parser("scores", help="Print the high-score board")
    ps.set_defaults(func=_scores)

    pr = sub.add_parser("record", help="Record a clip from the camera RTSP")
    pr.add_argument("--seconds", type=int, default=0)
    pr.set_defaults(func=_record)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
