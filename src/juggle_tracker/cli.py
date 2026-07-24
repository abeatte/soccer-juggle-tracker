"""Command-line interface.

    python -m juggle_tracker.cli enroll  --name "Kid1"   [--images DIR | --webcam]
    python -m juggle_tracker.cli process CLIP.mp4        [--debug-video out.mp4]
    python -m juggle_tracker.cli watch                    # batch worker: watch inbox
    python -m juggle_tracker.cli scores                   # print the scoreboard
    python -m juggle_tracker.cli backfill-unknown         # credit old NULL streaks to Unknown
    python -m juggle_tracker.cli record  --seconds 30     # grab a clip from RTSP
    python -m juggle_tracker.cli doctor                   # preflight env checks
    python -m juggle_tracker.cli bench                     # model FPS + per-clip estimate
    python -m juggle_tracker.cli tune CLIPS_DIR [--write]  # auto-calibrate from labelled clips
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
    from .db import Database, UNKNOWN_NAME
    from .identity import FaceEngine

    cfg = load_config(args.config)
    db = Database(cfg.database.path)

    # The reserved catch-all profile is not an enrolled person and must not
    # count against the max_people cap.
    people = [p for p in db.list_people() if p["name"] != UNKNOWN_NAME]
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
    from .pipeline import Pipeline, move_to_processed, ClipCancelled, move_to_failed

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
    from .pipeline import Pipeline, move_to_processed, ClipCancelled, move_to_failed

    cfg = load_config(args.config)
    inbox = cfg.capture.inbox_dir
    os.makedirs(inbox, exist_ok=True)
    print(f"Watching {inbox} for new clips (Ctrl-C to stop)...")
    pipe = Pipeline(cfg)
    pipe.ha.publish_queues()
    try:
        while True:
            clips = []
            for ext in ("*.mp4", "*.mkv", "*.mov"):
                clips += glob.glob(os.path.join(inbox, ext))
            for clip in sorted(clips):
                # Wait until the file stops growing (finished recording).
                if not _is_stable(clip):
                    continue
                # A sidecar '<clip>.annotate' marker (dropped by an HA reprocess
                # with the "Annotate On Reprocess" switch on) forces a viewable
                # annotated replay for this clip.
                marker = clip + ".annotate"
                annotate = os.path.exists(marker)
                print(f"\n-> {os.path.basename(clip)}"
                      f"{'  [annotated replay]' if annotate else ''}")
                try:
                    if annotate:
                        res = pipe.process_annotated_viewable(clip)
                    else:
                        res = pipe.process(clip)
                    for s in res.streaks:
                        print(f"   {s['person']}: {s['count']} ({s['reason']})")
                    dst = move_to_processed(cfg, clip)
                    # Surface the just-finished clip in HA (live queue progress).
                    pipe.ha.publish_last_processed(dst, res.streaks)
                    # Drop the marker only after the clip is handled + moved, so
                    # a mid-run crash re-annotates on the retry.
                    if annotate:
                        try:
                            os.remove(marker)
                        except OSError:
                            pass
                except ClipCancelled:
                    # Operator deleted the in-flight clip via HA: remove it and
                    # its marker instead of archiving. process() already rolled
                    # back the session's partial DB/HA writes.
                    for p in (clip, marker):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                    print(f"-> cancelled + removed {os.path.basename(clip)}",
                          flush=True)
                except Exception as exc:  # keep the worker alive
                    print(f"   ERROR processing {clip}: {exc}", file=sys.stderr)
                    # Quarantine the offending clip so it isn't retried forever
                    # (one bad clip would otherwise stall the whole queue).
                    dst = move_to_failed(cfg, clip)
                    if dst:
                        print(f"   -> quarantined to {dst}", file=sys.stderr)
            # Refresh the inbox/processed queue sensors in HA each cycle.
            pipe.ha.publish_queues()
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


def _backfill_unknown(args) -> int:
    from .db import Database, UNKNOWN_NAME

    cfg = load_config(args.config)
    db = Database(cfg.database.path)
    reassigned, high = db.backfill_unknown_attempts()
    db.close()
    print(f"Backfill: reassigned {reassigned} unattributed attempt(s) to "
          f"'{UNKNOWN_NAME}'. High score is now {high}.")
    print("Restart juggle-tracker.service (or process any clip) to publish the "
          "updated score to Home Assistant.")
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


def _tune(args) -> int:
    from . import tune
    return tune.run(args)


def _dump_trace(args) -> int:
    """Run detection once per clip and write the per-frame observation trace
    (ball / keypoints / ground) that the counter is fed — for fast, ML-free
    replay via the ``eval`` command. Side-effect free (HA off, throwaway DB)."""
    from .pipeline import Pipeline
    from . import eval_harness as ev
    from .tune import _sandbox

    cfg = _sandbox(load_config(args.config))
    # Dumping needs no labels (labels are only used at eval time), so take every
    # clip under the source path.
    clips = _list_clip_files(args.source)
    if not clips:
        print(f"No clips found at {args.source}", file=sys.stderr)
        return 2
    out_dir = args.out or (args.source if os.path.isdir(args.source)
                           else os.path.dirname(os.path.abspath(args.source)))
    os.makedirs(out_dir, exist_ok=True)
    pipe = Pipeline(cfg)
    n = 0
    try:
        for clip in clips:
            base = os.path.splitext(os.path.basename(clip))[0]
            out = os.path.join(out_dir, base + ev.TRACE_SUFFIX)
            with open(out, "w", encoding="utf-8") as fh:
                # Meta header first, then one JSONL frame per sink call.
                import cv2
                cap = cv2.VideoCapture(clip)
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
                fps = float(cap.get(cv2.CAP_PROP_FPS)) or 0.0
                cap.release()
                fh.write(_json_meta(clip, h, fps) + "\n")
                pipe.process(clip, trace_sink=ev.make_trace_sink(fh))
            print(f"  wrote {out}")
            n += 1
    finally:
        pipe.close()
    print(f"Dumped {n} trace(s) to {out_dir}")
    return 0 if n else 2


def _eval(args) -> int:
    """Replay labelled traces through the counter (no ML) and score the
    longest-streak error. Optional ``--set juggle.min_contact_gap_frames=8``
    overrides let you A/B a parameter instantly against a whole trace set."""
    from . import eval_harness as ev
    from .tune import label_from_filename

    cfg = load_config(args.config)
    params = ev.counter_params_from_cfg(cfg)
    for kv in (args.set or []):
        key, _, val = kv.partition("=")
        leaf = key.split(".")[-1]
        if leaf not in params:
            print(f"  ! ignoring unknown --set key '{key}' "
                  f"(known: {', '.join(sorted(params))})", file=sys.stderr)
            continue
        params[leaf] = int(val) if leaf.endswith(("frames", "window")) else float(val)

    traces = sorted(glob.glob(os.path.join(args.traces, "*" + ev.TRACE_SUFFIX))) \
        if os.path.isdir(args.traces) else [args.traces]
    labelled: list[tuple[str, int]] = []
    for t in traces:
        # Trace name is "<clip-stem>.trace.jsonl"; strip the suffix for labels.
        stem = os.path.basename(t)
        if stem.endswith(ev.TRACE_SUFFIX):
            stem = stem[:-len(ev.TRACE_SUFFIX)]
        lbl = label_from_filename(stem)
        if lbl is None:
            print(f"  ! skipping (no count in name): {stem}", file=sys.stderr)
            continue
        labelled.append((t, lbl))
    if not labelled:
        print("No labelled traces found. Name clips like '5_juggles.mp4' or "
              "dump traces from such clips.", file=sys.stderr)
        return 2
    report = ev.evaluate(labelled, params)
    print(ev.format_report(report))
    return 0


def _list_clip_files(source: str) -> list:
    if os.path.isfile(source):
        return [source]
    out: list = []
    for ext in ("*.mp4", "*.mkv", "*.mov", "*.avi"):
        out += glob.glob(os.path.join(source, ext))
        out += glob.glob(os.path.join(source, ext.upper()))
    return sorted(set(out))


def _json_meta(clip: str, frame_height: int, fps: float) -> str:
    import json
    return json.dumps({"meta": {"clip": os.path.basename(clip),
                                "frame_height": int(frame_height),
                                "fps": float(fps)}})


def _bench(args) -> int:
    """Micro-benchmark the model stack to estimate real per-clip processing time."""
    import numpy as np
    from .detect import Detector
    from .pose import PoseEstimator
    from .thermal import read_package_temp_c

    cfg = load_config(args.config)
    size = int(cfg.processing.get("infer_long_edge", 640))
    iters = args.iters
    img = (np.random.rand(size, size, 3) * 255).astype("uint8")

    print(f"Benchmarking at {size}px, {iters} iters "
          f"(threads={cfg.processing.get('torch_threads', 0)})...")
    det = Detector(
        cfg.models.detector,
        person_conf=float(cfg.models.get("person_conf", 0.35)),
        ball_conf=float(cfg.models.get("ball_conf", 0.20)),
        torch_threads=int(cfg.processing.get("torch_threads", 0)),
    )
    pose = PoseEstimator(cfg.models.pose)

    def timeit(fn, n):
        for _ in range(2):      # warmup
            fn()
        t0 = time.time()
        for _ in range(n):
            fn()
        return n / (time.time() - t0)

    det_fps = timeit(lambda: det.detect_track(img), iters)
    pose_fps = timeit(lambda: pose.estimate(img), iters)

    fps = float(cfg.camera.get("fps", 25))
    stride = max(1, int(cfg.processing.get("person_stride", 4)))
    clip_s = int(cfg.capture.get("clip_seconds", 20))
    frames = int(fps * clip_s)
    # ball detect runs every frame; pose runs every stride-th frame.
    est_s = frames / det_fps + (frames / stride) / pose_fps

    print("\nResults (higher FPS = faster):")
    print(f"  detector (person+ball): {det_fps:5.1f} FPS")
    print(f"  pose (17 keypoints):    {pose_fps:5.1f} FPS")
    temp = read_package_temp_c()
    if temp is not None:
        print(f"  CPU package temp:       {temp:.0f} C")
    print(f"\nEstimated processing time for a {clip_s}s clip "
          f"({frames} frames @ {fps:.0f}fps, stride {stride}):")
    print(f"  ~{est_s:.0f}s  ({est_s / clip_s:.1f}x realtime)")
    print("\nTune: lower infer_long_edge or raise person_stride to speed up; "
          "watch accuracy with tools/eval.py.")
    return 0


# --- doctor / preflight --------------------------------------------------
_PASS, _WARN, _FAIL = "PASS", "WARN", "FAIL"
_SYM = {_PASS: "\u2713", _WARN: "!", _FAIL: "\u2717"}


def _c_ffmpeg():
    import shutil
    path = shutil.which("ffmpeg")
    if path:
        return _PASS, f"found at {path}"
    return _FAIL, "not found — install with: sudo apt-get install ffmpeg"


def _c_config():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if os.path.exists(os.path.join(root, "config.yaml")):
        return _PASS, "config.yaml present"
    return _WARN, "config.yaml missing — using config.example.yaml (copy + edit it)"


def _c_models(cfg):
    msgs = []
    level = _PASS
    for label, path in (("detector", cfg.models.detector), ("pose", cfg.models.pose)):
        if os.path.exists(path):
            kind = "OpenVINO" if path.endswith("_openvino_model") else "weights"
            msgs.append(f"{label}: {kind} ok")
        else:
            level = _FAIL
            msgs.append(f"{label}: MISSING ({path}) — run ./setup.sh")
    return level, "; ".join(msgs)


def _c_rtsp(cfg):
    url = cfg.camera.get("rtsp_main")
    if not url or "CHANGE_ME" in url:
        return _WARN, "rtsp_main not configured yet"
    try:
        import cv2
        os.environ.setdefault(
            "OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000"
        )
        cap = cv2.VideoCapture(url)
        opened = cap.isOpened()
        frame_ok = cap.read()[0] if opened else False
        cap.release()
        if frame_ok:
            return _PASS, "connected and read a frame"
        if opened:
            return _WARN, "opened but couldn't read a frame (slow stream?)"
        return _FAIL, "could not open stream (check IP/creds/RTSP enabled/network)"
    except ImportError:
        return _WARN, "opencv not installed yet — run ./setup.sh"
    except Exception as exc:  # noqa: BLE001
        return _FAIL, f"error: {exc}"


def _c_mqtt(cfg):
    ha = cfg.home_assistant
    if not ha.get("enabled", False):
        return _WARN, "home_assistant.enabled is false (skipping)"
    host = ha.get("mqtt_host", "127.0.0.1")
    port = int(ha.get("mqtt_port", 1883))
    if ha.get("mqtt_password") in (None, "", "CHANGE_ME"):
        return _WARN, f"MQTT password not set; would connect to {host}:{port}"
    try:
        import paho.mqtt.client as mqtt
        state = {"rc": None}
        cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if ha.get("mqtt_user"):
            cli.username_pw_set(ha.get("mqtt_user"), ha.get("mqtt_password"))

        def on_connect(client, userdata, flags, reason_code, properties=None):
            state["rc"] = reason_code

        cli.on_connect = on_connect
        cli.connect(host, port, keepalive=10)
        cli.loop_start()
        for _ in range(50):
            if state["rc"] is not None:
                break
            time.sleep(0.1)
        cli.loop_stop()
        cli.disconnect()
        rc = state["rc"]
        if rc is None:
            return _FAIL, f"no response from broker at {host}:{port}"
        if getattr(rc, "is_failure", False):
            return _FAIL, f"broker refused connection: {rc}"
        return _PASS, f"broker reachable at {host}:{port}"
    except ImportError:
        return _WARN, "paho-mqtt not installed yet — run ./setup.sh"
    except Exception as exc:  # noqa: BLE001
        return _FAIL, f"cannot reach broker at {host}:{port}: {exc}"


def _c_inbox(cfg):
    inbox = cfg.capture.inbox_dir
    if not os.path.isdir(inbox):
        return _WARN, f"{inbox} does not exist yet (created on first run)"
    if os.access(inbox, os.R_OK | os.W_OK):
        return _PASS, f"{inbox} readable + writable"
    return _FAIL, f"{inbox} not writable (check permissions / bind-mount UID)"


def _c_highscore(cfg):
    if not cfg.capture.get("save_highscore_video", True):
        return _WARN, "save_highscore_video is false (replay clips disabled)"
    d = cfg.capture.get("highscore_dir", "highscores")
    annotate = cfg.capture.get("annotate_highscore", True)
    if not os.path.isdir(d):
        return _WARN, f"{d} does not exist yet (created on first record)"
    if not os.access(d, os.R_OK | os.W_OK):
        return _FAIL, f"{d} not writable (tracker must write replay clips here)"
    # Annotated replays need the system ffmpeg's H.264 encoder for HA playback.
    if annotate:
        import subprocess
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                  capture_output=True, text=True, timeout=10)
            if "libx264" not in out.stdout:
                return _WARN, (f"{d} writable, but ffmpeg lacks libx264 — "
                               "annotated clips won't be H.264 (set "
                               "annotate_highscore: false or install libx264)")
        except Exception:
            return _WARN, f"{d} writable; could not verify ffmpeg libx264"
    return _PASS, f"{d} writable ({'annotated H.264' if annotate else 'raw clip'})"


def _c_webcam():
    if os.path.exists("/dev/video0"):
        return _PASS, "/dev/video0 present (live enrollment available)"
    return _WARN, "/dev/video0 absent — enroll with --images DIR instead"


def _doctor(args) -> int:
    cfg = load_config(args.config)
    checks = [
        ("config", _c_config()),
        ("ffmpeg", _c_ffmpeg()),
        ("models", _c_models(cfg)),
        ("camera (RTSP)", _c_rtsp(cfg)),
        ("home assistant (MQTT)", _c_mqtt(cfg)),
        ("inbox dir", _c_inbox(cfg)),
        ("highscore dir", _c_highscore(cfg)),
        ("webcam", _c_webcam()),
    ]
    print("Juggle Tracker preflight\n" + "=" * 60)
    worst_fail = False
    for name, (level, msg) in checks:
        print(f"  [{_SYM[level]}] {level:<4} {name:<22} {msg}")
        worst_fail = worst_fail or (level == _FAIL)
    print("=" * 60)
    if worst_fail:
        print("Result: FAIL — resolve the ✗ items above before running.")
        return 1
    print("Result: OK (warnings are non-blocking).")
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

    pbu = sub.add_parser("backfill-unknown",
                         help="One-time: credit historical unattributed streaks "
                              "to the Unknown Juggler profile")
    pbu.set_defaults(func=_backfill_unknown)

    pr = sub.add_parser("record", help="Record a clip from the camera RTSP")
    pr.add_argument("--seconds", type=int, default=0)
    pr.set_defaults(func=_record)

    pd = sub.add_parser("doctor", help="Preflight: check ffmpeg, RTSP, MQTT, models, webcam")
    pd.set_defaults(func=_doctor)

    pb = sub.add_parser("bench", help="Benchmark model FPS + estimate per-clip time")
    pb.add_argument("--iters", type=int, default=20)
    pb.set_defaults(func=_bench)

    ptune = sub.add_parser(
        "tune", help="Auto-calibrate detection params against labelled clips")
    from . import tune as _tune_mod
    _tune_mod.add_arguments(ptune)
    ptune.set_defaults(func=_tune)

    pdt = sub.add_parser(
        "dump-trace",
        help="Run detection once and dump per-frame counter traces (for `eval`)")
    pdt.add_argument("source", help="A clip file or a directory of clips")
    pdt.add_argument("--out", help="Directory to write .trace.jsonl files "
                                   "(default: alongside the clips)")
    pdt.set_defaults(func=_dump_trace)

    pev = sub.add_parser(
        "eval",
        help="Replay labelled traces through the counter (no ML) and score")
    pev.add_argument("traces", help="A .trace.jsonl file or a directory of them")
    pev.add_argument("--set", action="append", metavar="juggle.KEY=VALUE",
                     help="Override a counter param for this run (repeatable), "
                          "e.g. --set juggle.min_contact_gap_frames=8")
    pev.set_defaults(func=_eval)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
