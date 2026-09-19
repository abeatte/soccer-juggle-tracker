# machinetelemetry → Home Assistant

A small daemon that publishes **live host telemetry** from the box (the 2012
MacBook Pro running Ubuntu that hosts Home Assistant, Frigate, and the juggle
worker) to Home Assistant over MQTT — using the **same MQTT-discovery pattern**
as the juggle tracker's `ha_mqtt.py`, so entities auto-appear with no HA YAML.

> **Not to be confused with** [`../deploy/box-telemetry.sh`](../deploy/box-telemetry.sh),
> which is a *one-shot, read-only* probe you run once to tune the juggle config.
> This is a *continuous* publisher that surfaces CPU/temp/fan/Frigate stats in HA.

## What appears in HA
A single device **"Frigate Box (MacBook Pro)"** with:

| Entity | Source |
|---|---|
| CPU Usage (%) | `/proc/stat` delta |
| CPU Temperature (°C) | `coretemp` hwmon |
| Fan Speed (rpm) | `applesmc` hwmon |
| Load Average 1m / 5m / 15m | `/proc/loadavg` |
| Memory Used (%) | `/proc/meminfo` |
| Disk Used (%) | `/` filesystem |
| Uptime (h) | `/proc/uptime` |
| Frigate CPU (%) / Memory (MB) | `docker stats frigate` (optional) |

## Design (mirrors `ha_mqtt.py`)
- Pure-stdlib reads of `/proc` + `/sys/class/hwmon` every `INTERVAL` seconds.
- Retained HA **discovery** configs → `homeassistant/sensor/frigate_box/<key>/config`.
- One retained JSON blob → `frigate_box/state`; each sensor reads `value_json.<key>`.
- Availability topic `frigate_box/availability` + MQTT Last-Will → HA shows the
  device **offline** if the box or daemon dies.

## Deploy (run ON the box, from the repo root)
Reuses the repo's shared `.venv` (created by `./setup.sh`) — `paho-mqtt` is
already a project dependency, so there's nothing extra to install.

```bash
./setup.sh                       # if you haven't already created .venv
machinetelemetry/install.sh
nano machinetelemetry/machine-telemetry.env    # set MQTT_PASS, confirm MQTT_USER
systemctl --user start machinetelemetry.service
systemctl --user status machinetelemetry.service
```

### MQTT account
Reuse an existing broker login. Frigate authenticates as `mqtt_broker`; the
juggle tracker uses `mqtt_user`. Set `MQTT_USER`/`MQTT_PASS` in the env file to
whichever real account you have the password for. `MQTT_HOST=127.0.0.1` works on
the box because the broker's 1883 port is published on the host.

### Frigate container stats (optional)
`DOCKER_CONTAINERS=frigate` adds Frigate's CPU%/mem. The service user must be in
the `docker` group (`sudo usermod -aG docker $USER`, then re-login). Leave blank
to disable — everything else still works.

## Verify
```bash
journalctl --user -u machinetelemetry.service -f
```
In HA: **Settings → Devices & Services → MQTT → Frigate Box (MacBook Pro)** — the
sensors appear within one publish cycle (~15 s). Raw feed:
```bash
mosquitto_sub -h 127.0.0.1 -u mqtt_broker -P '***' -t 'frigate_box/#' -v
```

## Example: high-temperature alert
This box thermally saturates (~90–99 °C) until the clean + repaste. Add an HA
automation (Settings → Automations → edit in YAML):

```yaml
alias: Frigate box overheating
trigger:
  - platform: numeric_state
    entity_id: sensor.frigate_box_cpu_temperature
    above: 95
    for: "00:02:00"
action:
  - service: notify.notify
    data:
      title: "🔥 Frigate box hot"
      message: >
        CPU {{ states('sensor.frigate_box_cpu_temperature') }}°C,
        fan {{ states('sensor.frigate_box_fan_speed') }} rpm,
        load {{ states('sensor.frigate_box_load_average_1m') }}.
mode: single
```

(Confirm the exact auto-generated entity IDs in HA after the device appears.)

## Uninstall
```bash
machinetelemetry/install.sh uninstall
```
