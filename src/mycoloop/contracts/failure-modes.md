# Failure mode table (Phase 0 deliverable)

Every row is implemented and exercised by the test suite. "Proven by" is the
test that fails if the behaviour regresses.

| Failure | Detection | Safe response | Layer | Proven by |
|---|---|---|---|---|
| Sensor disconnect | missing core fields | fail-safe actuation, quarantine, sensor health decay | quality + control + device | `test_missing_sensor_is_flagged_and_scored`, `test_invalid_sensors_force_fail_safe_actuation` |
| Invalid CO₂ reading | plausible range 350–5000 ppm | field quarantined (never zero-filled), conservative ventilation | ingestion + device | `test_out_of_range_co2_is_quarantined` |
| Implausible spike | Δ > 1500 ppm / 8 °C / 25 % per sample | field quarantined, baseline not poisoned | quality | `test_implausible_jump_is_quarantined` |
| Humidity sensor saturation | ≥ 99.5 % for 3 samples | field invalidated, humidifier disabled | quality + control | `test_humidity_saturation_is_detected_after_streak` |
| Sensor drift | sustained deviation from device baseline | `drift_suspected` flag + health score, human review | quality | `test_humidity_saturation_is_detected_after_streak` |
| Duplicate telemetry | unique `(device_id, seq)` | rejected, counted, raw row untouched | ingestion | `test_duplicate_seq_is_rejected` |
| Heater stuck on | relay feedback mismatch / runtime limit | NACK the command, runtime interlock, fail-safe supply cut | device + safety | `test_device_reports_failure_for_welded_relay`, `test_fail_safe_cuts_power_to_a_welded_relay` |
| Humidifier stuck on | runtime limit + RH divergence | runtime interlock, over-humidity interlock | safety | `test_interlocks_override_the_proposal` |
| Fan failure | tachometer 0 rpm / CO₂ rise | command NACK, CO₂ purge override | device + safety | `test_interlocks_override_the_proposal` |
| Power interruption | heartbeat gap (no telemetry) | offline detection, command TTL expiry, device restores safe state | gateway | `test_power_loss_creates_a_heartbeat_gap`, `test_partitioned_device_is_offline_and_command_expires` |
| Backend crash | watchdog timeout on device | device fail-safe: heater/humidifier off, fan 100 %, light off | device hardware | `test_hardware_watchdog_fires_when_backend_goes_silent`, `test_backend_crash_partition_leaves_device_safe` |
| Broker partition | publish refused | command stays `queued`, retried with same id, then `expired` | gateway | `test_retry_republishes_same_command_id_and_device_dedupes` |
| Duplicate command delivery | `command_id` in device cache | idempotent replay ack, no second actuation | device | same as above |
| Reused idempotency key, different payload | payload fingerprint mismatch | **409**, never a silent overwrite | gateway | `test_idempotency_key_reuse_with_different_payload_conflicts` |
| Unsafe setpoint | envelope validation | command persisted as `rejected_safety`, nothing published | safety | `test_safety_veto_is_persisted_and_never_published` |
| Optimizer proposes unsafe params | envelope + contamination risk | approval blocked with **409**, audited | optimizer + safety | `test_unsafe_recommendation_cannot_be_approved` |
| Malformed bus payload | Pydantic validation | rejected + `bus.invalid_payload` audit, handler survives | gateway | `test_malformed_telemetry_is_audited_and_rejected` |
| Unknown command ack | no such command | audited, ignored | gateway | `test_unknown_command_ack_is_audited_not_crashed` |
| Ledger tampering | DB trigger | UPDATE/DELETE raises | database | `test_ledger_is_append_only_at_the_database` |

## Distinction that matters for the soak test

A **veto** is the safety layer working. An **unsafe actuation** is a veto that
failed to hold. Counting vetoes as failures (a common dashboard bug) makes the
system look unsafe precisely when it is behaving correctly. The audit log keeps
them separate: `actor="safety"` events are interventions, not incidents.
