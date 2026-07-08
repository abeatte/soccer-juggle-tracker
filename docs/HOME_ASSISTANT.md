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

## Auto-recording clips on person detection

The batch worker just processes whatever lands in `inbox/`. To feed it
automatically, have HA record a clip when the RLC-810A reports a person and drop
it where the worker sees it.

Option A — **HA records the stream** (works entirely inside HA):

```yaml
alias: Record yard clip on person
trigger:
  - platform: state
    entity_id: binary_sensor.front_yard_person   # RLC-810A person sensor
    to: "on"
action:
  - service: camera.record
    target:
      entity_id: camera.front_yard_fluent          # or _clear (main stream)
    data:
      duration: 30
      lookback: 4
      # Write into a folder the tracker's inbox/ points at (bind-mount or symlink).
      filename: "/media/juggle_inbox/clip_{{ now().strftime('%Y%m%d_%H%M%S') }}.mp4"
mode: single
```

Then make the tracker's `capture.inbox_dir` the same folder HA writes to (a
symlink or a shared Docker bind-mount). Since HA and the tracker run on the same
MacBook, point both at one directory.

Option B — **tracker records from RTSP** on an HA webhook/MQTT nudge: call
`python -m juggle_tracker.cli record --seconds 30` from an HA `shell_command` or a
cron triggered by the person sensor. Option A is simpler if HA already has the
camera stream.

## Sanity check the MQTT path

With the broker reachable, process any clip once; the entities should appear
under **Settings → Devices & Services → MQTT → Soccer Juggle Tracker**. If they
don't:

- Confirm `home_assistant.enabled: true` and broker creds in `config.yaml`.
- `mosquitto_sub -h HOST -u USER -P PASS -t 'homeassistant/#' -v` and re-process a
  clip — you should see the discovery config messages.
