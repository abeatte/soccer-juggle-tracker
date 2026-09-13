# Frigate

Frigate watches the configured Reolink streams, detects people in the yard,
records the source video, and publishes detection events to Mosquitto. The
Home Assistant package consumes those events to request a full-framerate clip
in `/srv/juggle_inbox`; Frigate itself does not run the juggle counter.

## Setup

1. Start Mosquitto first from `../mosquitto` so the external
   `mosquitto_default` network exists.
2. Edit `config/config.yml` and replace the camera RTSP URLs and MQTT
   credentials. The default config is tuned for CPU detection on the target
   Ivy Bridge host; use a Coral only after changing the detector settings and
   passing through the USB device.
3. Confirm `/dev/dri/renderD128` exists or remove that device mapping if this
   host has no usable VA-API device. Create storage and start Frigate:

   ```bash
   mkdir -p config storage
   docker compose up -d
   ```

4. Open `http://<host-ip>:8971`, complete Frigate authentication, and verify
   the `front_yard` camera is receiving frames. The host must be able to reach
   the camera RTSP endpoint.

## Verify and inspect logs

```bash
docker compose ps
docker compose logs -f frigate
docker exec frigate python3 -c 'print("Frigate container is running")'
```

Use the Frigate UI's **Review** and **Explore** pages to verify person events
and recordings. In Home Assistant, the corresponding camera/person entities
should change state. On MQTT, inspect `frigate/#` with `mosquitto_sub` from the
Mosquitto folder.

## Stop and update

```bash
docker compose pull
docker compose up -d
docker compose down
```

Keep `config/` and `storage/`; they contain the service configuration and
recordings/snapshots.