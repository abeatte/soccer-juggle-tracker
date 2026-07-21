# Home Assistant integration

The tracker publishes results over **MQTT** using HA's **MQTT discovery**, so
entities appear automatically — no `configuration.yaml` editing required.

## Prerequisites

- The **MQTT integration** installed in Home Assistant, pointed at a broker
  (the Mosquitto add-on is easiest). You have this if you already run Matter/Thread
  gear through HA, but any broker works.
- Put the broker host/user/password into `config.yaml` under `home_assistant`.

## Entities created

For each enrolled person, on the first processed clip:

| Entity | Example | Meaning |
|---|---|---|
| `sensor.<person>_juggle_high_score` | `sensor.kid1_juggle_high_score` | All-time best streak (unit: juggles) |

The high-score sensor also carries a **`video_url`** attribute (and `updated`)
pointing at that kid's most-recent high-score replay clip — see
[High-score replay videos](#high-score-replay-videos) below.

There is also a reserved **`sensor.unknown_juggler_juggle_high_score`** (icon
`mdi:help-circle-outline`). Any session the tracker can't attribute to an
enrolled kid is credited to this **Unknown Juggler** catch-all — it accrues its
own score, high score, and replay video exactly like an enrolled profile. It has
no enrolled face, so it never steals a match from a real kid and never counts
against `identity.max_people`.

Plus two raw topics:

- `juggle_tracker/last_session` (retained JSON) — summary of the most recent clip:
  `{"ts": ..., "results": [{"person","count","reason","start_frame","end_frame"}]}`
- `juggle_tracker/event/new_high_score` (JSON) — fired when someone beats their
  record: `{"person","score","ts"}`

Device: everything is grouped under a **Soccer Juggle Tracker** device in HA.

## Dashboard card

A simple Lovelace card showing the leaderboard:

```yaml
type: entities
title: ⚽ Juggle High Scores
entities:
  - entity: sensor.kid1_juggle_high_score
  - entity: sensor.kid2_juggle_high_score
  - entity: sensor.kid3_juggle_high_score
  - entity: sensor.kid4_juggle_high_score
  - entity: sensor.unknown_juggler_juggle_high_score   # catch-all for unattributed runs
```

## High-score replay videos

When a kid beats their record, the tracker saves a short **replay clip** of that
run and Home Assistant can play it — either inline in a card or via a link.

- **One clip per kid**, at `<highscore_dir>/<slug>.mp4` (e.g. `artie.mp4`). It is
  **overwritten in place** each time the record is beaten, so only the latest is
  kept and the old clip is discarded automatically.
- By default the clip is an **annotated overlay** (ball tracking, pose dots, live
  streak counter) rendered as a quick second pass — and *only* for record-setting
  clips, so the extra CPU/heat on the old laptop is paid rarely. Set
  `capture.annotate_highscore: false` to just copy the raw clip instead.
- The annotated overlay is written by OpenCV as `mp4v` (the only codec available
  on the no-AVX2 box) and then **transcoded to H.264 with the system `ffmpeg`**
  (`libx264`) so it plays inline in the HA dashboard / browsers. If the transcode
  ever fails, it falls back to saving the raw (already-H.264) clip so a record is
  never lost. `raw` clips (`annotate_highscore: false`) skip the transcode.
- The tracker publishes the clip's URL as the **`video_url`** attribute on that
  kid's `sensor.<person>_juggle_high_score` entity.

### Step 1 — serve the clips to HA (`www` bind-mount)

Same idea as the shared inbox: HA is in Docker, so bind-mount the host
`highscore_dir` into HA's `www/` folder. Files under `www/` are served at
`/local/...`, which is exactly what an HTML5 `<video>`/iframe needs.

```yaml
# docker-compose.yml for Home Assistant — add one volume line:
services:
  homeassistant:
    volumes:
      - /srv/juggle_inbox:/media/juggle_inbox          # (existing) inbox
      - /srv/juggle_highscores:/config/www/juggle:ro   # <-- add this (read-only)
```

Recreate the HA container so the mount takes effect. Clips then load at:

```
http://<ha-host>:8123/local/juggle/<slug>.mp4     e.g. .../local/juggle/artie.mp4
```

> `/config/www` is served by HA at `/local/`. A one-time HA restart is needed
> after first adding the `www` folder/mount, but new/overwritten clips inside it
> appear without a restart.

### Step 2 — point the tracker at the same host dir

In `config.yaml`:

```yaml
capture:
  highscore_dir: "/srv/juggle_highscores"
  save_highscore_video: true
  annotate_highscore: true          # false = raw clip, no second pass
home_assistant:
  # Browser-reachable base URL for the clips (matches the mount above).
  media_base_url: "http://192.168.0.139:8123/local/juggle"
```

Permissions mirror the inbox — the native tracker writes the clips, the HA
container only reads them (mounted `:ro`):

```bash
sudo mkdir -p /srv/juggle_highscores
sudo chown -R $USER:$USER /srv/juggle_highscores
sudo chmod 755 /srv/juggle_highscores
```

### Step 3a — embedded video window (per kid)

Each kid's clip has a **stable path** (`/local/juggle/<slug>.mp4`). A plain
`iframe` card with that fixed URL gets **cached by the browser** — when the file
is overwritten with a new record the iframe keeps showing the old clip (or a
black frame). The sensor's `video_url` attribute already carries a
`?v=<timestamp>` cache-buster that changes on every new record, but the native
`iframe` card can't read an entity attribute, so it never sees the new URL.

Drive the URL from that attribute with
[`config-template-card`](https://github.com/iantrich/config-template-card)
(install via HACS). It re-renders when the listed entity changes, so the iframe
`src` (and thus the video) reloads whenever a record is beaten — no manual
refresh, no stale/black clip:

```yaml
type: custom:config-template-card
entities:
  - sensor.artie_juggle_high_score      # re-render trigger
card:
  type: iframe
  aspect_ratio: 56%                     # 16:9
  title: Artie — best juggle run
  # origin-relative URL + ?v= buster, straight off the sensor attribute
  url: >-
    ${ states['sensor.artie_juggle_high_score'].attributes.video_url }
```

Add a **`visibility`** condition on the wrapper so the card only appears once
that kid actually has a replay — the tracker publishes `video_url` as `""` until
a clip exists, so `attribute video_url != ""` is an exact "has a replay" test
(a single string value, **not** a YAML list — HA rejects a list here):

```yaml
type: custom:config-template-card
entities:
  - sensor.artie_juggle_high_score
card:
  type: iframe
  aspect_ratio: 56%
  title: Artie — best juggle run
  url: >-
    ${ states['sensor.artie_juggle_high_score'].attributes.video_url }
visibility:
  - condition: state
    entity: sensor.artie_juggle_high_score
    attribute: video_url
    state_not: ""
```

The **Unknown Juggler** catch-all works the same way — use
`sensor.unknown_juggler_juggle_high_score`.

On the **Sections** dashboard, put each wrapper in its own `grid` section; a
section whose only card is hidden collapses, so profiles without a replay simply
don't show.

> **No-HACS alternative:** a Markdown card with an HTML5 `<video>` also picks up
> the `?v=` buster —
> `<video controls preload="metadata" width="100%" src="{{ state_attr('sensor.artie_juggle_high_score','video_url') }}"></video>`
> — but some HA versions' Markdown sanitizer strips `<video>`; if it renders
> blank, use `config-template-card` above.
>
> `video_url` is only reliably present after the worker has published at least
> once (restart `juggle-tracker.service` after upgrading).

### Step 3b — Markdown links (auto-lists every kid)

This card reads the `video_url` attribute (which includes a `?v=` cache-buster)
off the sensors, so it always links the freshest clip and needs no per-kid
editing — the **Unknown Juggler** is included automatically since it also matches
`_juggle_high_score`:

```yaml
type: markdown
title: ⚽ High-score replays
content: >
  {% set kids = states.sensor
       | selectattr('entity_id','search','_juggle_high_score')
       | sort(attribute='state', reverse=true) | list %}
  {% if kids | length == 0 %}No scores yet.{% else %}
  {%- for k in kids %}
  - **{{ k.attributes.friendly_name | replace(' Juggle High Score','') }}** —
  {{ k.state }} juggles{% if k.attributes.get('video_url') %} · [▶ watch]({{ k.attributes.get('video_url') }}){% endif %}
  {%- endfor %}
  {% endif %}
```

## Reset buttons (clear a high score + replay)

For every profile (each kid **and** Unknown Juggler) the tracker also publishes
a **button** entity via MQTT discovery:

| Entity | Example |
|---|---|
| `button.reset_<person>_high_score` | `button.reset_artie_high_score` |

Pressing it tells the worker to **zero that person's high score, delete their
replay clip, and re-publish** (score → 0, `video_url` → `""`) so the sensor and
any iframe/link update immediately. It only works while
`juggle-tracker.service` is running (the button shows *unavailable* when the
worker is offline). History (`attempts`) is left intact — the score just starts
climbing again from 0.

Add them to a dashboard however you like, e.g. a button per kid with a
confirmation prompt (the confirmation is a Lovelace `tap_action` feature — no
tracker change needed):

```yaml
type: horizontal-stack
cards:
  - type: button
    entity: button.reset_artie_high_score
    name: Reset Artie
    icon: mdi:trophy-broken
    tap_action:
      action: perform-action
      perform_action: button.press
      target:
        entity_id: button.reset_artie_high_score
      confirmation:
        text: Reset Artie's high score and delete the replay?
  - type: button
    entity: button.reset_unknown_juggler_high_score
    name: Reset Unknown
    icon: mdi:trophy-broken
    tap_action:
      action: perform-action
      perform_action: button.press
      target:
        entity_id: button.reset_unknown_juggler_high_score
      confirmation:
        text: Reset the Unknown Juggler high score and delete the replay?
```

> Under the hood: each button publishes the person's slug to
> `juggle_tracker/reset/set`, which the worker is subscribed to. The exact
> `button.` entity IDs may be device-prefixed (like your sensors) — grab them
> from Developer Tools → States (filter `reset`).

## Calibration — edit detection settings from HA

With `calibration.enabled: true` (default), the tracker publishes the key
detection parameters as **editable `number` entities** plus two buttons, all
grouped under the Soccer Juggle Tracker device (as `config` entities, so they
sit in the device's *Configuration* section):

| Entity (number) | Config key |
|---|---|
| Juggle Ball Confidence | `models.ball_conf` |
| Juggle Person Confidence | `models.person_conf` |
| Juggle Face Confidence | `models.face_conf` |
| Juggle Inference Resolution | `processing.infer_long_edge` |
| Juggle Person/Pose Stride | `processing.person_stride` |
| Juggle Contact Radius (px) | `juggle.contact_radius_px` |
| Juggle Min Arc Height (px) | `juggle.min_arc_px` |
| Juggle Ground Line (frac) | `juggle.ground_y_frac` |
| Juggle Smoothing Window | `juggle.smooth_window` |
| Juggle Lost-Ball Frames | `juggle.lost_frames_reset` |
| Juggle Ball Bridge Frames | `juggle.max_bridge_frames` |
| Juggle Face Match Threshold | `identity.match_threshold` |
| Juggle Identity Vote Frames | `identity.vote_min_frames` |
| Juggle Ball CV Sensitivity | `ball_fallback.hough_param2` (lower = more circles) |
| Juggle Ball CV Max Radius | `ball_fallback.max_radius` |
| Juggle Ball CV Search Radius | `ball_fallback.search_radius` |
| Juggle Ball CV Min Radius | `ball_fallback.min_radius` |
| Juggle Ball CV Edge Threshold | `ball_fallback.hough_param1` (Canny high threshold) |
| Juggle Ball CV Accumulator (dp) | `ball_fallback.dp` (Hough inverse accumulator resolution) |
| Juggle Max People | `identity.max_people` |
| Juggle Torch Threads | `processing.torch_threads` (0 = auto) |
| Juggle Thermal Max Temp (C) | `thermal.max_temp_c` (pause above this) |
| Juggle Thermal Resume Temp (C) | `thermal.resume_temp_c` (resume below this) |

There is also one **switch** in the same config section:

| Entity (switch) | Config key |
|---|---|
| Juggle Ball CV Fallback | `ball_fallback.enabled` (turn the classical fallback on/off) |

The **Ball CV** numbers only take effect when the **Juggle Ball CV Fallback**
switch is ON (i.e. `ball_fallback.enabled: true`) — a classical OpenCV
Hough-circle detector that runs only on frames where YOLO misses the ball
(complements it on blurry/small-ball frames). The switch honors the same
Apply & Restart flow as the numbers, so you no longer need to hand-edit
`calibration_overrides.yaml` to toggle it.

Plus buttons **Apply Calibration & Restart** and **Revert Calibration to
Defaults**.

**Workflow:** the numbers are seeded from your current config. Edit any of them
→ the value is saved to `calibration_overrides.yaml` (deep-merged over
`config.yaml`, so your commented config stays pristine) but **does not take
effect yet**. Press **Apply Calibration & Restart** to restart the worker so the
new values load. **Revert Calibration to Defaults** deletes the overrides file
and restarts (back to `config.yaml`).

> Model thresholds (`ball_conf`, etc.) are read once at worker startup, which is
> why applying requires a restart. Nudge several values, then Apply once. The
> restart uses `systemctl --user restart` (unit from `calibration.service_name`);
> if that fails it exits and systemd's `Restart=always` relaunches it.

Dashboard example (verify the exact `number.` / `button.` IDs in Developer Tools
→ States — filter `juggle` — as they may be device-prefixed):

```yaml
type: entities
title: 🎛️ Calibration
entities:
  - entity: number.juggle_ball_confidence
  - entity: number.juggle_contact_radius_px
  - entity: number.juggle_min_arc_height_px
  - entity: number.juggle_ground_line_frac
  - entity: number.juggle_person_pose_stride
  - entity: number.juggle_inference_resolution
  - type: divider
  - entity: button.apply_calibration_restart
  - entity: button.revert_calibration_to_defaults
```

> ROI (play-area box) and keypoint ("dots") toggles are **not** here yet — those
> get the visual editor in Phase 2.

## Queues & reprocessing

The worker publishes two queue sensors (updated every watch cycle) and a
reprocess control:

| Entity | What |
|---|---|
| `sensor.juggle_inbox_queue` | Count of clips waiting in the inbox (state), with a `files` attribute listing them |
| `sensor.juggle_processed_count` | Count of already-processed clips (state) + `files` attribute (newest first, capped at 100) |
| `select.juggle_reprocess_file` | Dropdown of processed filenames (refreshed each cycle) |
| `button.reprocess_selected_clip` | Re-runs the selected clip |
| `switch.juggle_annotate_on_reprocess` | When ON, the next reprocess also writes a viewable annotated replay |
| `sensor.juggle_last_reprocessed` | Name of the last annotated reprocess (state) + `video_url` attribute |

**Reprocess** moves the chosen processed clip back into the inbox, so the normal
watcher re-runs it **end-to-end with the current (possibly just-calibrated)
config** — taking every action a fresh run does (updating high scores, writing
the replay video, publishing to HA). Pick a file in the select, then press the
button.

**Annotate On Reprocess** (checkbox): flip this ON *before* pressing Reprocess to
also get a full annotated replay of that clip — ball/pose/streak overlay, ROI
border, ground line — regardless of whether it beats a record. The overlay is
drawn in the same detection pass (no second inference run — just one extra
ffmpeg transcode), so it's cheap enough for this box. The result overwrites a
single file, `last_reprocessed.mp4`, and its URL lands on
`sensor.juggle_last_reprocessed`. The toggle is captured at button-press time,
so changing it afterwards won't affect an already-queued clip, and it resets to
`capture.reprocess_annotate_default` on restart.

Dashboard example (verify device-prefixed IDs in Developer Tools → States):

```yaml
type: entities
title: 📥 Queue & Reprocess
entities:
  - entity: sensor.juggle_inbox_queue
  - entity: sensor.juggle_processed_count
  - type: divider
  - entity: select.juggle_reprocess_file
  - entity: switch.juggle_annotate_on_reprocess
  - entity: button.reprocess_selected_clip
```

Show the annotated replay in its own window. Use `config-template-card` (HACS)
so the iframe URL comes from the sensor's `video_url` attribute — this both
hides it until a reprocess has produced a clip and picks up the `?v=`
cache-buster so an overwritten replay actually reloads (a plain `iframe` with a
fixed URL would show the cached/old clip):

```yaml
type: custom:config-template-card
entities:
  - sensor.juggle_last_reprocessed
card:
  type: iframe
  aspect_ratio: 56%
  title: 🎬 Last Annotated Reprocess
  url: >-
    ${ states['sensor.juggle_last_reprocessed'].attributes.video_url }
visibility:
  - condition: state
    entity: sensor.juggle_last_reprocessed
    state_not: unknown
  - condition: state
    entity: sensor.juggle_last_reprocessed
    state_not: unavailable
  - condition: state
    entity: sensor.juggle_last_reprocessed
    attribute: video_url
    state_not: ""
```

> `last_reprocessed` isn't seeded on startup, so before the **first** reprocess
> the sensor is `unknown` and its `video_url` attribute is absent — the two
> `state_not` guards hide the card until then. Once a reprocess has run the
> worker keeps `video_url` **always present**: it publishes `""` the moment a
> clip is queued (so the card hides while an annotated run is in flight, and
> stays hidden for an annotate-off run that produces no replay) and the real
> URL when an annotated run finishes. That's why the third condition —
> `attribute: video_url, state_not: ""` — is the one that actually shows the
> card only when a fresh replay exists. All three are ANDed (each `state_not`
> is a single string; a YAML list under one `state_not` is rejected by HA).

List the actual inbox filenames with a Markdown card reading the attribute:

```yaml
type: markdown
title: 📥 Inbox
content: >
  {% set f = state_attr('sensor.juggle_inbox_queue','files') or [] %}
  **{{ f | length }} waiting**
  {% for name in f %}
  - {{ name }}
  {% endfor %}
```

> Typical calibration loop: tweak the Calibration numbers → **Apply & Restart** →
> pick a clip in **Reprocess File** → **Reprocess Selected Clip** → watch the new
> count + replay. Re-running always uses the latest config.

## New-high-score announcement (TTS)

Trigger off the event topic and speak it on a media player:

```yaml
alias: Juggle new high score announce
trigger:
  - platform: mqtt
    topic: juggle_tracker/event/new_high_score
action:
  - variables:
      person: "{{ trigger.payload_json.person }}"
      score: "{{ trigger.payload_json.score }}"
  - service: tts.google_translate_say   # or your TTS engine
    data:
      entity_id: media_player.kitchen_speaker
      message: "New juggling record! {{ person }} just hit {{ score }} juggles!"
mode: queued
```

> Because this is **batch** processing, the announcement fires a few minutes
> after the session ends (when the clip finishes processing), not the instant the
> record is set.
## Auto-recording clips into the shared `inbox/`

The batch worker processes whatever lands in its `inbox/`. To feed it
automatically, have Home Assistant record a clip when the RLC-810A reports a
person and write it into that folder.

### The key fact: HA is in Docker, the tracker is native

On your box the tracker runs **natively** (systemd) but **Home Assistant runs in
a Docker container**. A container can only write to host paths that are
**bind-mounted** into it. So the wiring is: pick one host directory, mount it
into the HA container, and point the tracker's `inbox_dir` at the same host path.

```
  HA container ──writes──▶ /media/juggle_inbox   (path INSIDE the container)
                                 │  (bind mount)
  host dir     ◀─────────────────┘  /srv/juggle_inbox   (path ON the host)
                                 │
  tracker (native) ──reads──▶ capture.inbox_dir = /srv/juggle_inbox
```

### Step 1 — pick a host dir and bind-mount it into HA

Choose e.g. `/srv/juggle_inbox` on the host. Add it to however you launch HA:

```yaml
# docker-compose.yml for Home Assistant — add one volume line:
services:
  homeassistant:
    # ...existing config...
    volumes:
      - /srv/juggle_inbox:/media/juggle_inbox   # <-- add this
```

Or with `docker run`: `-v /srv/juggle_inbox:/media/juggle_inbox`. Recreate the HA
container so the mount takes effect. (`/media/...` is convenient because HA
already allows writes there.)

### Step 2 — point the tracker at the same host dir

In `config.yaml`:

```yaml
capture:
  inbox_dir: "/srv/juggle_inbox"
```

### Step 3 — HA automation to record on person detection

```yaml
alias: Record yard clip on person
trigger:
  - platform: state
    entity_id: binary_sensor.front_yard_person    # RLC-810A person sensor
    to: "on"
action:
  - service: camera.record
    target:
      entity_id: camera.front_yard_clear           # main stream = full detail
    data:
      duration: 30
      lookback: 4
      # This path is INSIDE the HA container (Step 1's mount target).
      filename: "/media/juggle_inbox/clip_{{ now().strftime('%Y%m%d_%H%M%S') }}.mp4"
mode: single
```

(The ready-made version with a debounce is automation #2 in
[`homeassistant/packages/juggle_tracker.yaml`](../homeassistant/packages/juggle_tracker.yaml)
— just set the entity IDs and the `filename` path.)

### Step 4 — permissions

The HA container writes the file as its own user (often root); the native tracker
reads and then **moves** it to `processed/`. Make sure the tracker's user can
write in `/srv/juggle_inbox`:

```bash
sudo chown -R $USER:$USER /srv/juggle_inbox
sudo chmod 775 /srv/juggle_inbox
```

If HA writes root-owned files the tracker can't move, either run HA with a
matching `PUID/PGID`, or add a group both share and `chmod g+w`.

### Verify end-to-end

```bash
python -m juggle_tracker.cli doctor        # 'inbox dir' should be PASS (writable)
# trip the camera, then:
ls -l /srv/juggle_inbox                     # a clip_*.mp4 should appear
journalctl --user -u juggle-tracker.service -f   # watch it get processed + moved
```

### Alternative — tracker records from RTSP itself

If you'd rather not share a folder, trigger the tracker to pull its own clip:
call `python -m juggle_tracker.cli record --seconds 30` from an HA
`shell_command` (or a cron) fired by the person sensor. Simpler folder story, but
HA already has the stream, so the shared-inbox route above is usually cleaner.

## Sanity check the MQTT path

With the broker reachable, process any clip once; the entities should appear
under **Settings → Devices & Services → MQTT → Soccer Juggle Tracker**. If they
don't:

- Run `python -m juggle_tracker.cli doctor` — the MQTT check confirms broker
  reachability with your configured creds.
- Confirm `home_assistant.enabled: true` and broker creds in `config.yaml`.
- `mosquitto_sub -h HOST -u USER -P PASS -t 'homeassistant/#' -v` and re-process a
  clip — you should see the discovery config messages.
