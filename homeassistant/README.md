# Home Assistant

Home Assistant provides the dashboard, MQTT-discovered juggle sensors, clip
recording automation, and optional notifications. It operates independently in
Docker and connects to the other folders through MQTT and the shared clip
directory.

## Setup

1. Install Docker Engine and the Compose plugin on the Ubuntu host.
2. Create the shared directories used by the tracker:

   ```bash
   sudo mkdir -p /srv/juggle_inbox /srv/juggle_highscores
   sudo chown -R "$USER":"$USER" /srv/juggle_inbox /srv/juggle_highscores
   ```

3. Review `configs/configuration.yaml`. Copy
   `packages/juggle_tracker.yaml` into `configs/packages/` and replace its
   `CHANGE_ME` entity IDs and notification services. Enable packages by adding
   this under `homeassistant:` in `configuration.yaml`:

   ```yaml
   packages: !include_dir_named packages
   ```

4. Create the package directory and required empty include files if this is a
   new Home Assistant configuration:

   ```bash
   mkdir -p configs/packages
   touch configs/automations.yaml configs/scripts.yaml configs/scenes.yaml
   cp packages/juggle_tracker.yaml configs/packages/
   ```

5. Configure the MQTT integration in Home Assistant to use the Mosquitto
   broker at `127.0.0.1:1883` when both use host networking. Start Home
   Assistant from this directory:

   ```bash
   docker compose up -d
   ```

6. Open `http://<host-ip>:8123`, finish onboarding, and add the dashboard from
   `dashboards/juggle-tracker-dashboard.yaml` as a manual Lovelace dashboard.

## Verify and inspect logs

```bash
docker compose ps
docker compose logs -f homeassistant
docker exec homeassistant ha core check
```

After the soccer worker processes a clip, check **Developer Tools -> States**
for `sensor.juggle_last_session` and the per-person high-score sensors. A
person detection should also trigger the package automation and create an MP4
under `/srv/juggle_inbox`.

## Stop and update

```bash
docker compose pull
docker compose up -d
docker compose down
```

Keep the `configs/` directory and the two shared host directories when
updating the container.