"""Phase 4: the setpoint scheduler must actually close the loop, not just store a recipe."""
from __future__ import annotations

from conftest import OPERATOR

from mycoloop import db
from mycoloop.modules.control import ControlLoop


def test_control_loop_drives_the_chamber_toward_the_recipe(client, device, biology, app, settings):
    device.chamber.state.temp_c = 15.0
    device.chamber.state.humidity_pct = 78.0
    device.tick()

    loop = ControlLoop(app.state.gateway, settings)
    results = loop.run_once()
    assert len(results) == 1
    r = results[0]
    assert r["device_id"] == "chamber-01"
    assert r["proposed"]["heater"] is True
    assert r["proposed"]["humidifier"] is True
    assert any(d["actuator"] == "heater" for d in r["dispatched"])
    assert device.chamber.state.heater is True

    for _ in range(40):
        device.tick()
    assert device.chamber.state.temp_c > 17.0, "heater command did not actually warm the chamber"

    audit = client.get("/audit", headers=OPERATOR, params={"actor": "control-loop"}).json()
    assert audit, "the autonomous loop must be attributed in the audit log"


def test_control_loop_ignores_devices_without_a_live_cycle(client, device, app, settings):
    device.tick()
    loop = ControlLoop(app.state.gateway, settings)
    assert loop.run_once() == []  # no grow cycle yet -> nothing to control


def test_control_loop_respects_dry_run(client, device, biology, app, settings):
    settings.dry_run = True
    device.chamber.state.temp_c = 15.0
    device.tick()
    before = len(device.received_commands)
    loop = ControlLoop(app.state.gateway, settings)
    r = loop.run_once()[0]
    assert r["dry_run"] is True
    assert r["dispatched"] == []
    assert len(device.received_commands) == before
    settings.dry_run = False


def test_control_state_exposes_envelope_and_loop(client, device, biology):
    body = client.get("/devices/chamber-01/control/state", headers=OPERATOR).json()
    assert body["envelope"]["watchdog_timeout_s"] == 60
    assert body["control_loop"]["enabled"] is True
    assert body["setpoints"]["temp_c"] == 19


def test_co2_has_a_real_source_sink_balance(device):
    """A chamber with no CO2 source would look falsely healthy at any fan speed."""
    device.chamber.state.co2_ppm = 600.0
    device.chamber.state.fan_pct = 20
    for _ in range(20):
        device.tick()
    assert device.chamber.state.co2_ppm > 600.0
    peak = device.chamber.state.co2_ppm
    device.chamber.state.fan_pct = 100
    for _ in range(60):
        device.tick()
    assert device.chamber.state.co2_ppm < peak
    assert device.chamber.state.co2_ppm < 600.0


def test_fan_speed_modulation_reaches_the_device(client, device, biology, app, settings):
    """A change of fan *speed* with the fan already ON must still be dispatched."""
    device.chamber.state.co2_ppm = 2400.0
    device.chamber.state.fan_pct = 45
    device.tick()
    loop = ControlLoop(app.state.gateway, settings)
    r = loop.run_once()[0]
    assert r["proposed"]["fan_pct"] > 45
    assert any(d["actuator"] == "fan/light" for d in r["dispatched"])
    assert device.chamber.state.fan_pct == r["allowed"]["fan_pct"]


def test_proportional_fae_clears_a_large_co2_error(client, device, biology, app, settings):
    device.chamber.state.co2_ppm = 2400.0
    device.tick()
    loop = ControlLoop(app.state.gateway, settings)
    loop.run_once()
    assert device.chamber.state.fan_pct >= 90
    start = device.chamber.state.co2_ppm
    for _ in range(60):
        device.tick()
    # removal now exceeds respiration: the error is actively being cleared
    assert device.chamber.state.co2_ppm < start - 500, "proportional FAE must actually clear the error"


def test_heater_stops_at_the_setpoint_instead_of_overshooting(client, device, biology, app, settings):
    device.chamber.state.temp_c = 17.0
    device.tick()
    loop = ControlLoop(app.state.gateway, settings)
    loop.run_once()
    assert device.chamber.state.heater is True
    # cross the setpoint, then let the loop run again immediately (inside cooldown)
    device.chamber.state.temp_c = 19.5
    device.tick()
    loop.run_once()
    assert device.chamber.state.heater is False, "cooldown must never hold a heater on past target"


def test_backend_heartbeat_keeps_a_quiet_device_armed(client, device, biology, app, settings):
    """A stable chamber sends no commands; the heartbeat must still arm the watchdog."""
    for _ in range(55):
        device.tick()
    assert device.chamber.state.fail_safe is False
    assert device.chamber.state.watchdog_remaining_s < 10
    app.state.gateway.publish_heartbeat()
    device.tick()
    assert device.chamber.state.watchdog_remaining_s > 50
    assert device.chamber.state.fail_safe is False


def test_sweep_publishes_the_contracted_backend_status(client, device, app):
    from mycoloop import topics

    app.state.gateway._last_heartbeat = 0.0
    client.post("/gateway/sweep", headers=OPERATOR)
    beats = [m for m in device.bus.published if m.topic == topics.BACKEND_STATUS]
    assert beats and beats[-1].json()["state"] == "online"
    assert beats[-1].retain is True


def test_controller_trusts_reported_state_not_its_own_last_command(client, device, biology, app, settings):
    """After a device reboot the backend must re-assert, not assume."""
    from mycoloop.models import Actuator
    from mycoloop import db

    device.chamber.state.temp_c = 15.0
    device.tick()
    # backend mirror claims the heater is already ON, device reports OFF
    with db.session_scope() as s:
        s.get(Actuator, "chamber-01:heater").state = True
    device.chamber.state.heater = False
    device.tick()

    loop = ControlLoop(app.state.gateway, settings)
    r = loop.run_once()[0]
    assert r["observed"]["heater"] is False
    assert any(d["actuator"] == "heater" and d["state"] is True for d in r["dispatched"])
    assert device.chamber.state.heater is True


def test_stale_telemetry_forces_fail_safe(client, device, biology, app, settings):
    """Acting on a minutes-old reading is how a controller cooks a chamber."""
    from datetime import timedelta
    from mycoloop import db
    from mycoloop.models import TelemetryClean
    from mycoloop.schemas import utcnow
    from sqlalchemy import select

    device.tick()
    with db.session_scope() as s:
        row = s.scalars(select(TelemetryClean).order_by(TelemetryClean.id.desc()).limit(1)).first()
        row.ts = utcnow() - timedelta(seconds=settings.telemetry_stale_s + 10)

    loop = ControlLoop(app.state.gateway, settings)
    r = loop.run_once()[0]
    assert r["telemetry_fresh"] is False
    assert r["sensors_valid"] is False
    assert r["allowed"]["heater"] is False
    assert r["allowed"]["fan_pct"] == 100
    assert "sensor_invalid_fail_safe" in r["vetoes"]
    audit = client.get("/audit", headers=OPERATOR, params={"action": "control.stale_telemetry"}).json()
    assert len(audit) == 1
