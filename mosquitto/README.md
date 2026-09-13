# Mosquitto

Mosquitto is the shared MQTT message bus. Frigate publishes camera events,
the soccer worker publishes scores and worker status, and Home Assistant
subscribes to both. The broker is deliberately independent of all three
applications.

## Setup

1. Install Docker Engine and the Compose plugin.
2. Review `mosquitto.conf`. It currently allows anonymous local-network
   connections for the existing setup. For a network-exposed broker, set
   `allow_anonymous false`, create `config/password.txt` with `mosquitto_passwd`,
   and uncomment `password_file`.
3. Start the broker from this directory:

   ```bash
   mkdir -p config data log
   docker compose up -d
   ```

The compose file creates the stable external network `mosquitto_default`.
Frigate joins that network using the service name `mosquitto`; the native
soccer worker and host-networked Home Assistant use `127.0.0.1:1883`.

## Verify and inspect logs

```bash
docker compose ps
docker compose logs -f mosquitto
mosquitto_sub -h 127.0.0.1 -p 1883 -t 'juggle_tracker/#' -v
```

The broker log is also persisted at `log/mosquitto.log`. The subscription
command should show discovery, worker state, session, and high-score messages
after the soccer worker starts or processes a clip.

## Stop and update

```bash
docker compose pull
docker compose up -d
docker compose down
```

Do not delete `data/` unless losing retained MQTT state is intentional.