# Hardware-in-the-loop test plan (Phase 0 deliverable)

Goal: prove every device-facing behaviour **before** connecting a relay. The
bundled simulator is a separate actor on the same bus contract, so the same
tests run unchanged against Mycodo or an ESPHome node later.

## Level 0 — in-process (no services, runs in CI)

```bash
pytest -q          # 62 tests, loopback bus + SQLite + simulated chamber
```

| # | Scenario | Command | Expected |
|---|---|---|---|
| 1 | Register simulated device | `POST /devices` | 201 + one-time token |
| 2 | Stream telemetry | simulator tick | `telemetry_clean` rows attributed to cycle + stage |
| 3 | Inject sensor failure | `POST /sim/devices/{id}/faults?name=sensor_disconnect` | fields null, quarantine row, health decay |
| 4 | Inject stuck actuator | `...?name=heater_stuck` | actuator command NACKed `failed` |
| 5 | Force stage transition | `POST /grow-cycles/{id}/transitions` | append-only event, illegal ones 409 |
| 6 | Log contamination | `POST /grow-cycles/{id}/contamination` | terminal stage, `contaminated=true` |
| 7 | Log harvest + yield | `POST /grow-cycles/{id}/harvests` | `yield_g` accumulates |
| 8 | Backend crash → watchdog | `...?name=backend_crash` + 61 ticks | device `fail_safe`, fan 100 %, heater off |
| 9 | Power loss | `...?name=power_loss` | heartbeat gap, offline detection, TTL expiry |
| 10 | Dry run | `MYCOLOOP_DRY_RUN=1` + control tick | decisions logged, zero commands dispatched |

## Level 1 — real broker (docker compose)

```bash
docker compose up -d broker db
MYCOLOOP_BUS=mqtt MYCOLOOP_MQTT_HOST=localhost \
MYCOLOOP_DATABASE_URL=postgresql+psycopg://mycoloop:mycoloop@localhost:5432/mycoloop \
  uvicorn mycoloop.main:app --port 8000 &
python -m mycoloop.sim --device chamber-01 --api http://localhost:8000 --speed 4
```

Repeat scenarios 2–9. Additionally verify: retained status survives a backend
restart, last-will marks the device offline when the simulator is killed, QoS 1
redelivery does not double-actuate, and `telemetry_clean` is a Timescale
hypertable (`SELECT * FROM timescaledb_information.hypertables`).

## Level 2 — physical edge (Mycodo / ESPHome)

Gate: **do not pass this level until Level 1 is stable.**

1. Flash the edge with the envelope from `safety-envelope.yaml` and an
   independent hardware watchdog. Confirm the watchdog fires with the backend
   stopped and the network unplugged.
2. Verify the fail-safe cuts the relay board supply (the only defence against a
   welded relay), not just the GPIO signal.
3. Run 24 h with a resistive dummy load, logging every command/ack pair.
4. Confirm calibration offsets against a reference thermometer/hygrometer before
   any telemetry is allowed into `telemetry_clean`.
5. Only then connect real heaters/humidifiers, in dry-run mode, for one full
   cycle.

## Exit criteria

Level 0 green in CI, Level 1 green with a real broker, Level 2 signed off by a
human who watched the watchdog fire. No autonomous control before that.
