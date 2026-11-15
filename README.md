# MycoLoop backend — Phase 0–2 scaffold (with 3/4/5/6/7 hooks)

A **contract-first, safety-constrained modular monolith** for closed-loop mycology.
This is the real backend the 9-phase plan describes, not a UI mock: FastAPI +
Pydantic v2 + SQLAlchemy 2 + Alembic + MQTT 5, with a simulated chamber that
speaks the same wire contract as a Mycodo or ESPHome node.

```
optimizer recommends → safety validates → human approves → control executes → hardware watchdog protects
```

Nothing in this codebase lets an optimizer touch a relay. That invariant is a
test, not a comment (`test_optimizer_module_has_no_direct_hardware_path`).

---

## Run it in 60 seconds (zero external services)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,postgres]"

pytest -q                      # 75 tests, all exit criteria
make dev                       # API + simulated chamber on :8000
python scripts/demo.py         # seeds 6 completed cycles + a live one
```

Then open:

| URL | What it is |
|---|---|
| `http://localhost:8000/console` | operator console — **fully self-contained**, no CDN, no fonts, no chart library |
| `http://localhost:8000/docs` | OpenAPI 3.1 interactive docs (49 paths) |
| `http://localhost:8000/contracts/asyncapi.yaml` | MQTT 5 contract |
| `http://localhost:8000/contracts/schemas.json` | JSON Schema for every contract |
| `http://localhost:8000/healthz` | live device/command counters |

Default keys (change them before anything touches hardware):
`X-API-Key: dev-operator-key` (operator), `dev-admin-key` (admin, token rotation).

---

## What is actually implemented

| Phase | Status | Evidence |
|---|---|---|
| **0 — Contracts, simulation, safety envelope** | **complete** | `contracts/asyncapi.yaml`, generated `openapi.json`, 44 JSON Schemas, `safety-envelope.yaml`, `state-machine.md`, `failure-modes.md`, `hil-test-plan.md`, simulated chamber + device client |
| **1 — Domain model & backend skeleton** | **complete** | 25 tables, Alembic `0001`+`0002` (upgrade/downgrade verified), API-key auth, device tokens, Docker Compose, seed script, full cycle through the API |
| **2 — Device gateway & command bus** | **complete** | MQTT 5 + loopback HAL, registry, idempotency keys, ack journal, retries with backoff, TTL expiry, heartbeat/offline detection, backend last-will |
| **3 — Telemetry quality** | **complete** | raw → clean → quarantine, calibration, dedupe per boot, range/plausibility/saturation checks, drift heuristic, sensor health, stage aggregates, all five exit questions answerable |
| **4 — Safe control engine** | **substantial** | recipe engine + versioning, setpoint scheduler loop, hysteresis temp/RH control, PI control for CO₂, per-actuator cooldown, runtime limits, interlocks, manual override, dry-run, staleness gate, device-side watchdog |
| **5 — Biology ledger** | **complete** | strict stage machine, append-only events (DB-enforced), harvest/flush/yield, contamination, annotations, reconstructable timeline |
| **6 — Vision pipeline** | **ingestion + queries** | frame/inference store, model registry, human corrections, active-learning queue, all four exit queries. Inference itself runs on the edge and posts results |
| **7 — Optimizer** | **gate complete, statistics stubbed** | trials, search space, objective, grounded recommendation, safety + contamination-risk constraints, human approval, rollback, full audit. `SearchBackend` protocol with a documented heuristic backend |
| **8 — Hardening & ops** | **partial** | append-only DB guards, audit everywhere, health endpoint, CI (SQLite + Postgres/Timescale jobs), Dockerfile with healthcheck. Prometheus/OTel/Grafana, backup drills and the 14-day soak are **not** built |

### Deliberately not done

* **Optuna / Ax / BoTorch / PyMC** — `HeuristicSearch` is a deterministic
  half-step toward the best observed region. It is labelled as such in every
  recommendation (`expected_effect.model`). Swap it via `optimizer.set_backend()`;
  the safety, approval and audit machinery around it is already proven.
* **YOLO / SAM 2 / ONNX inference** — the backend stores and queries features; it
  does not run models. That is the edge's job.
* **Observability stack, OTA, chaos automation, soak test** — Phase 8 proper.
* **Alembic on Timescale** is written and guarded, but only exercised on SQLite
  here; the CI `postgres` job runs the same suite against `timescale/timescaledb:latest-pg16`.

---

## Architecture

```
Operator console / REST ──┐
                          ▼
        ┌─────────────────────────────────────────┐
        │      FastAPI modular monolith           │
        │  devices   gateway   ingestion          │
        │  biology   control   vision             │
        │  optimizer audit     simulation         │
        └───────┬──────────────────────┬──────────┘
                ▼                      ▼
      PostgreSQL + TimescaleDB    MessageBus (HAL)
      (SQLite in dev/test)        ├─ LoopbackBus (in-process)
                                  └─ MqttBus (MQTT 5, paho)
                                          ▼
                                   Edge controller
                                   Mycodo / ESPHome / SimDevice
                                   (own watchdog + interlocks)
```

**Module boundaries.** Each bounded context owns its service + router. Modules
call each other's functions, never each other's tables. The optimizer cannot
import the bus; it reaches hardware only through `control.apply_recipe` after a
recorded human approval.

**Two abstraction seams** make the whole thing testable without hardware:
`MessageBus` (loopback ↔ MQTT) and the database URL (SQLite ↔ Postgres/Timescale).
Tests substitute only those two things — never the logic under test.

---

## The safety model, concretely

| Layer | What it can do | Where |
|---|---|---|
| Schema | reject malformed / physically impossible payloads (`extra="forbid"`) | `schemas.py` |
| Envelope | veto biologically unsafe setpoints **before** anything is published | `safety.validate_setpoints` |
| Interlocks | override any proposal: over-temp, over-humidity, CO₂ purge, min FAE, runtime limits, cooldown | `safety.evaluate_actuators` |
| Staleness gate | refuse to actuate on telemetry older than 30 s → fail-safe | `control.evaluate` |
| Human approval | an optimizer recommendation with violations returns **409** | `optimizer.approve` |
| Device watchdog | on backend silence: heater/humidifier off, fan 100 %, light off — **cuts the relay board supply**, the only defence against a welded relay | `sim/chamber.py` |
| Append-only ledger | UPDATE/DELETE on `audit_logs`, `stage_events`, `command_acks`, `telemetry_raw` raises at the DB | `db.install_append_only_guards` |

A **veto is the system working**; an unsafe actuation is a veto that failed to
hold. The audit log keeps them apart (`actor="safety"`), so a soak test does not
score successful interventions as incidents.

---

## Bugs this scaffold found in itself (and fixed)

These are the reasons Phase 0 exists. Each was caught by running the system, not
by reading it, and each now has a regression test.

1. **Cooldown blocked *stopping* a heater.** A blanket anti-flap window held the
   heater on past its setpoint → 25 °C overshoot. Cooldown now only limits
   *restarts*, and only for actuators that actually short-cycle (humidifier, fan).
2. **Fan speed changes were never dispatched.** The loop only sent commands when
   a boolean flipped, so CO₂ climbed forever with the fan "already on".
3. **The controller trusted its own last command.** After a device reboot it
   believed the heater was on while it was off. State now comes from what the
   device *reports*.
4. **`seq` dedupe killed telemetry after a reboot.** Unique `(device_id, seq)`
   rejected every reading once the counter restarted. Now `(device_id, boot_id,
   seq)` — migration `0002`.
5. **No staleness gate.** The engine happily acted on four-minute-old data.
6. **A healthy backend looked dead.** A stable chamber sends no commands, so the
   device watchdog fired. The gateway now publishes the contracted
   `farm/backend/status` heartbeat.
7. **CO₂ had no source term**, so any fan speed collapsed it to the outdoor floor
   and the chamber looked falsely healthy.
8. **An uninstalled optional sensor** flagged every reading as degraded.

---

## Layout

```
mycoloop/
├── src/mycoloop/
│   ├── config.py db.py models.py schemas.py auth.py safety.py quality.py topics.py
│   ├── bus/                 # MessageBus HAL: LoopbackBus | MqttBus
│   ├── modules/             # devices gateway ingestion biology control vision optimizer audit simulation
│   ├── sim/                 # chamber physics + device client + CLI
│   ├── contracts/           # asyncapi.yaml, safety-envelope.yaml, 3 markdown deliverables
│   ├── static/console.html  # self-contained operator console
│   └── main.py              # app factory + lifespan wiring
├── migrations/versions/     # 0001_initial, 0002_telemetry_boot_id
├── tests/                   # 75 tests across 7 files
├── scripts/                 # export_contracts.py, demo.py
├── deploy/mosquitto.conf
├── docker-compose.yml Dockerfile Makefile .env.example
└── .github/workflows/ci.yml # SQLite job + Postgres/Timescale job
```

## Common tasks

```bash
make test          # 75 tests
make contracts     # export openapi + asyncapi + 44 JSON Schemas
make demo          # seed a running API
make migrate       # alembic upgrade head
make revision m="add x"
make up            # postgres+timescale, mosquitto, redis, api
make sim           # simulated chamber on the real broker
```

Real MQTT, separate process:

```bash
docker compose up -d db broker
MYCOLOOP_BUS=mqtt MYCOLOOP_MQTT_HOST=localhost \
MYCOLOOP_DATABASE_URL=postgresql+psycopg://mycoloop:mycoloop@localhost:5432/mycoloop \
  uvicorn mycoloop.main:app --port 8000 &
python -m mycoloop.sim --device chamber-01 --api http://localhost:8000 --speed 4
python -m mycoloop.sim --device chamber-02 --fault heater_stuck --speed 8
```

## Known limitations (honest list)

* One live cycle per device: telemetry is attributed to the most recent
  non-terminal cycle. Multi-cycle-per-chamber needs an explicit assignment.
* Runtime/cooldown accounting follows reported device uptime; between health
  reports it falls back to wall clock.
* Drift detection is a baseline-deviation heuristic for human review, not a
  calibrated estimator. It never rewrites data.
* The humidifier cannot always reach 92 % RH at high fresh-air exchange — that
  tension is real physics, and exactly what the optimizer should explore.
* Loopback bus is single-process. Scale-out needs the MQTT transport plus a
  shared command store (the schema already supports it).
* Auth is API keys + device tokens. No OAuth, no RBAC beyond operator/admin.

## Next step

Phase 2 is stable and proven, which is the plan's gate for autonomous control.
The highest-value next work is **Phase 3 hardening on real sensors** (calibration
against a reference, drift validation) and **Phase 8 observability**, then
swapping `HeuristicSearch` for Optuna/BoTorch behind the existing protocol.
