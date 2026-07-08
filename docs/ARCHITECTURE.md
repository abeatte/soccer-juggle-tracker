# Architecture

## Design driver: the hardware

The deployment target is a **2016 Intel MacBook Pro running Ubuntu**, already
running Home Assistant and Matter in Docker. Consequences:

- **No ML-usable GPU** (Intel integrated graphics, no CUDA) → PyTorch is
  **CPU-only**.
- Because it's an **Intel** CPU on Linux, **OpenVINO** (Intel's inference runtime)
  recovers a large chunk of speed — commonly ~2–3x over stock torch-CPU. Export
  once with `tools/export_openvino.py` and point config at the exported dirs.
- Even accelerated, the realtime pipeline (person-detect + pose + ball-track +
  face, every frame, fast enough to catch a ball mid-arc) is marginal on this
  CPU — **too slow to count juggles reliably in real time**, since a ball contact
  reverses in 2–3 frames.
- The box must stay responsive for HA/Matter, so the pipeline **caps CPU threads**
  (`processing.torch_threads`, default: leave 2 cores free) and the systemd unit
  runs at low CPU/IO priority.

## The batch (offline) decision

We **decouple capture from compute**:

```
                       full framerate (~25fps)              slower-than-realtime, accuracy preserved
  camera ──motion──▶ record short clip ──▶ inbox/ ──▶ batch worker ──▶ SQLite ──▶ MQTT ──▶ Home Assistant
```

The *source* clip is full framerate, so no fast ball contacts are lost — only the
*analysis* is slow, which is acceptable. Scores appear a few minutes after a
session rather than instantly. To stay **as close to real time as possible**:

- **nano** models (`yolo11n`, `yolo11n-pose`, InsightFace `buffalo_s`)
- frame **downscale** to `infer_long_edge` (default 960px long edge)
- **ROI crop** to the play area
- **person_stride**: run the expensive person-pose + face stages every Nth frame,
  but **always** detect the ball every frame (it's the fast object)
- ByteTrack interpolates person tracks between stride frames

## Frame pipeline

Per frame (`pipeline.Pipeline.process`):

1. **detect + track** — one YOLO call returns person boxes (COCO cls 0, tracked
   with ByteTrack → stable `track_id`) and the sports ball (cls 32; highest-conf
   detection is taken).
2. **pose + identity** (every `person_stride` frames) — YOLO-pose gives 17
   keypoints per person; InsightFace embeds any visible faces and matches them to
   the enrolled gallery; a vote locks each `track_id` to a `person_id`.
3. **attribute the ball** — in the one-kid-at-a-time case, the ball is assigned to
   the person nearest it; that person's keypoints + identity are the active ones.
4. **juggle state machine** (`juggle.JuggleCounter`) — see below.
5. **persist + publish** — completed streaks go to SQLite; high scores update; the
   HA publisher pushes per-person sensors and fires a `new_high_score` event.

## Juggle state machine

Signal: the ball's **image-y** centroid over time. In image coordinates y grows
downward, so the bottom of each arc (ball meets foot/knee) is a **local maximum**
in y.

- A **contact** = smoothed vertical velocity flips descending(+) → ascending(−),
  with a minimum arc height (`min_arc_px`) since the last contact to reject jitter.
- At each contact, the **nearest confident body keypoint** to the ball is found:
  - foot/ankle/knee/shoulder/head (`valid_keypoints`, `_is_footish`) → **+1**
  - wrist/elbow (`illegal_keypoints`) → **reset** (reason `hand`)
  - keypoint too far (`> contact_radius_px`) → ambiguous, ignored
- If the contact's low point is at/below the calibrated ground line
  (`ground_y_frac`) → **reset** (reason `ground`).
- If the ball goes missing for `lost_frames_reset` frames → streak ends (`lost`).
- End of clip flushes any streak in progress (`end`).

Completed streaks become `attempts` rows; the max per person is the high score.

## Persistence

SQLite (`db.Database`): `people`, `face_embeds`, `sessions`, `attempts`. High
score is denormalized onto `people.high_score` for cheap HA reads and recomputed
transactionally on each recorded attempt.

## Home Assistant

`ha_mqtt.HAPublisher` uses **MQTT discovery** so a
`sensor.<person>_juggle_high_score` entity appears automatically per enrolled
person. A retained `last_session` topic carries the latest clip summary, and a
`new_high_score` event topic can drive TTS/notifications. See
[`HOME_ASSISTANT.md`](HOME_ASSISTANT.md).

## Known accuracy limits (the multi-week part)

- Motion blur on the ball at arc extremes (mitigated by full-framerate capture +
  low ball-conf threshold).
- Foot-vs-thigh contacts share few COCO keypoints (ankle/knee/hip only) — thigh
  contacts are approximated.
- Face ID at yard distance is unreliable per-frame; voting + track continuity
  carry identity, but a kid who never shows their face stays "Unknown".
- Two balls / two kids simultaneously is out of scope for v0.1 (one-at-a-time).

These are tuned against **your real footage** — drop sample clips in `inbox/` and
process with `--debug-video` to watch the overlay and adjust config thresholds.
