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

Because each kid's clip has a **stable path**, the card URL is fixed. An
`iframe` card renders the browser's native video player inline:

```yaml
type: iframe
url: http://192.168.0.139:8123/local/juggle/artie.mp4   # one per kid
aspect_ratio: 56%    # 16:9
title: Artie — best juggle run
```

Pair each with its score in a stack:

```yaml
type: vertical-stack
cards:
  - type: entity
    entity: sensor.artie_juggle_high_score
    name: Artie
  - type: iframe
    url: http://192.168.0.139:8123/local/juggle/artie.mp4
    aspect_ratio: 56%
```

> Since the file keeps the same name when overwritten, a browser may show a
> cached older clip — hard-refresh (Ctrl/Cmd-Shift-R) if needed. The Markdown
> card below avoids this by using the `?v=` cache-busted `video_url` attribute.

### Step 3b — Markdown links (auto-lists every kid)

This card reads the `video_url` attribute (which includes a `?v=` cache-buster)
off the sensors, so it always links the freshest clip and needs no per-kid
editing:

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
  {{ k.state }} juggles{% if k.attributes.video_url %} · [▶ watch]({{ k.attributes.video_url }}){% endif %}
  {%- endfor %}
  {% endif %}
```

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
