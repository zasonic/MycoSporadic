"""Control module (Phase 4): deterministic control *before* any AI.

Recipe engine -> setpoint scheduler -> threshold controller with hysteresis ->
safety interlocks -> gateway -> device. The optimizer is not in this path; it
can only hand a recommendation to a human, who approves it into a recipe
version, which then flows through here like any other recipe.

``dry_run`` computes and logs the exact same decision without dispatching, which
is how a new recipe gets reviewed before it is allowed to touch a relay.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Principal, require_operator
from ..config import Settings, get_settings
from ..db import get_session, session_scope
from ..models import Actuator, Device, GrowCycle, Recipe, RecipeVersion, TelemetryClean
from ..quality import CORE_FIELDS
from ..safety import ActuatorRuntime, evaluate_actuators, load_envelope, validate_setpoints
from ..schemas import (
    ApprovalRequest,
    CommandCreate,
    CommandKind,
    RecipeCreate,
    RecipeVersionRead,
    Setpoints,
    Stage,
    as_utc,
    utcnow,
)
from . import audit, biology
from .gateway import Gateway, get_gateway

log = logging.getLogger(__name__)
router = APIRouter(tags=["control"])

_runtimes: dict[str, ActuatorRuntime] = {}
_last_tick: dict[str, float] = {}
ACTUATOR_NAMES = ("heater", "humidifier", "fan", "light")
_dispatch_memo: dict[str, dict[str, tuple[Any, float]]] = {}
_co2_integral: dict[str, float] = {}
_device_clock: dict[str, float] = {}
_last_uptime: dict[str, int] = {}

HYSTERESIS_TEMP_C = 0.3
HYSTERESIS_RH_PCT = 1.0


def runtime_for(device_id: str) -> ActuatorRuntime:
    return _runtimes.setdefault(device_id, ActuatorRuntime())


def reset_state() -> None:
    """Clear per-device controller memory (cooldowns, runtime, last modulation).

    Called on app bootstrap in tests and available for a controlled hot-restart;
    in production this state is intentionally process-lived so a cooldown cannot
    be bypassed by restarting a request.
    """
    _runtimes.clear()
    _last_tick.clear()
    _dispatch_memo.clear()
    _co2_integral.clear()
    _device_clock.clear()
    _last_uptime.clear()


def active_setpoints(session: Session, device_id: str) -> Setpoints:
    """Active recipe for the device's live cycle, else the device's last applied setpoints."""
    cycle = biology.active_cycle_for_device(session, device_id)
    if cycle is not None:
        rv = session.get(RecipeVersion, cycle.recipe_version_id)
        if rv is not None:
            return Setpoints.model_validate(rv.setpoints)
    device = session.get(Device, device_id)
    stored = (device.meta or {}).get("active_setpoints") if device else None
    if stored:
        return Setpoints.model_validate(stored)
    return Setpoints(temp_c=19, humidity_pct=92, co2_ceiling_ppm=900, fan_pct=45, light_pct=60)


def _device_time(session: Session, device_id: str) -> tuple[float, float]:
    """Interlock timing must follow the *process*, not the wall clock.

    Uses the device's reported uptime so a simulator running at 8x (or an edge
    node reporting late) gets identical cooldown/runtime semantics. Falls back to
    wall clock when no health report has arrived yet.
    """
    from ..models import DeviceHealthRow

    health = session.scalars(
        select(DeviceHealthRow).where(DeviceHealthRow.device_id == device_id)
        .order_by(DeviceHealthRow.ts.desc(), DeviceHealthRow.id.desc()).limit(1)
    ).first()
    wall = time.monotonic()
    if health is not None:
        uptime = float(health.uptime_s)
        prev = _last_uptime.get(device_id)
        if prev is None or uptime < prev:      # first report or device reboot
            _device_clock.setdefault(device_id, uptime)
            dt = 1.0
        else:
            dt = max(uptime - prev, 0.0)
            _device_clock[device_id] = _device_clock.get(device_id, 0.0) + dt
        _last_uptime[device_id] = uptime
        _last_tick[device_id] = wall
        return _device_clock[device_id], max(dt, 0.001)
    dt = wall - _last_tick.get(device_id, wall - 1.0)
    _last_tick[device_id] = wall
    _device_clock[device_id] = _device_clock.get(device_id, 0.0) + max(dt, 0.0)
    return _device_clock[device_id], max(dt, 0.001)


def latest_clean(session: Session, device_id: str) -> TelemetryClean | None:
    return session.scalars(
        select(TelemetryClean).where(TelemetryClean.device_id == device_id)
        .order_by(TelemetryClean.ts.desc(), TelemetryClean.id.desc()).limit(1)
    ).first()


def remember_setpoints(session: Session, device_id: str, sp: Setpoints) -> None:
    device = session.get(Device, device_id)
    if device is None:
        return
    meta = dict(device.meta or {})
    meta["active_setpoints"] = sp.model_dump()
    device.meta = meta


# ---------------------------------------------------------------------------
# recipe engine
# ---------------------------------------------------------------------------
def create_recipe(session: Session, payload: RecipeCreate, actor: str) -> RecipeVersion:
    verdict = validate_setpoints(payload.setpoints)
    if not verdict.ok:
        audit.record(session, "safety", "recipe.vetoed", payload.recipe_id,
                     violations=[v.model_dump() for v in verdict.violations])
        session.commit()  # keep the veto auditable, then refuse
        raise HTTPException(422,
                            detail={"message": "setpoints outside safety envelope",
                                    "violations": [v.model_dump() for v in verdict.violations]})
    recipe = session.get(Recipe, payload.recipe_id)
    if recipe is None:
        recipe = Recipe(recipe_id=payload.recipe_id, name=payload.name, description=payload.description)
        session.add(recipe)
        session.flush()
    last = session.scalars(
        select(RecipeVersion).where(RecipeVersion.recipe_id == payload.recipe_id)
        .order_by(RecipeVersion.version.desc()).limit(1)
    ).first()
    version = RecipeVersion(
        recipe_id=payload.recipe_id, version=(last.version + 1) if last else 1,
        stage=payload.stage.value, setpoints=payload.setpoints.model_dump(),
        created_by=actor, approved_by=actor, change_reason=payload.change_reason,
        parent_version_id=last.recipe_version_id if last else None,
    )
    session.add(version)
    audit.record(session, actor, "recipe.version", payload.recipe_id,
                 version=version.version, setpoints=version.setpoints)
    session.flush()
    return version


def apply_recipe(session: Session, gw: Gateway, device_id: str, recipe_version_id: str, actor: str,
                 *, dry_run: bool = False, reason: str = "recipe applied") -> dict[str, Any]:
    rv = session.get(RecipeVersion, recipe_version_id)
    if rv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown recipe version")
    sp = Setpoints.model_validate(rv.setpoints)
    verdict = validate_setpoints(sp)
    if not verdict.ok:
        audit.record(session, "safety", "recipe.apply_vetoed", recipe_version_id,
                     violations=[v.model_dump() for v in verdict.violations])
        session.commit()
        return {"applied": False, "dry_run": dry_run, "safety": verdict.model_dump()}

    if dry_run:
        audit.record(session, actor, "control.dry_run", device_id,
                     recipe_version_id=recipe_version_id, setpoints=sp.model_dump())
        session.commit()
        return {"applied": False, "dry_run": True, "would_apply": sp.model_dump(), "safety": verdict.model_dump()}

    result = gw.dispatcher.dispatch(
        session, device_id,
        CommandCreate(kind=CommandKind.RECIPE,
                      payload={"recipe_version_id": recipe_version_id, "setpoints": sp.model_dump()},
                      idempotency_key=f"recipe:{recipe_version_id}:{device_id}", ttl_s=120),
        actor,
    )
    remember_setpoints(session, device_id, sp)
    audit.record(session, actor, "control.apply_recipe", device_id,
                 recipe_version_id=recipe_version_id, command_id=result.command.command_id, reason=reason)
    session.commit()
    return {"applied": True, "dry_run": False, "dispatch": result.model_dump()}


def manual_override(session: Session, gw: Gateway, device_id: str, actuator: str, state: bool,
                    actor: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Operator override — still subject to interlocks, always audited."""
    env = load_envelope()
    row = latest_clean(session, device_id)
    temp = row.temperature_c if row else None
    rh = row.humidity_pct if row else None
    if state and actuator == "heater" and temp is not None and temp >= env.max_temp_c:
        audit.record(session, "safety", "override.vetoed", device_id, actuator=actuator, temp_c=temp)
        session.commit()
        return {"applied": False, "veto": "over_temp_interlock"}
    if state and actuator == "humidifier" and rh is not None and rh >= env.max_humidity_pct:
        audit.record(session, "safety", "override.vetoed", device_id, actuator=actuator, humidity_pct=rh)
        session.commit()
        return {"applied": False, "veto": "over_humidity_interlock"}

    audit.record(session, actor, "control.manual_override", device_id,
                 actuator=actuator, state=state, dry_run=dry_run)
    if dry_run:
        session.commit()
        return {"applied": False, "dry_run": True, "would_apply": {actuator: state}}

    result = gw.dispatcher.dispatch(
        session, device_id,
        CommandCreate(kind=CommandKind.ACTUATOR, payload={"actuator": actuator, "state": state},
                      idempotency_key=f"override:{device_id}:{actuator}:{state}:{int(time.time())}", ttl_s=60),
        actor,
    )
    session.commit()
    return {"applied": True, "dispatch": result.model_dump()}


def propose_actuators(sp: Setpoints, row: TelemetryClean | None, co2_integral: float = 0.0) -> dict[str, Any]:
    """Threshold control with hysteresis for temp/RH, PI control for CO2.

    Temperature and humidity are slow, forgiving processes: bang-bang with a
    deadband is appropriate and keeps relay wear low. CO2 has a hard ceiling and
    a continuous disturbance (respiration), so pure proportional control leaves
    a permanent steady-state offset above the ceiling — hence the integral term.
    """
    if row is None:
        return {"heater": False, "humidifier": False, "fan": True, "light": sp.light_pct > 0,
                "fan_pct": sp.fan_pct, "light_pct": sp.light_pct}
    temp, rh, co2 = row.temperature_c, row.humidity_pct, row.co2_ppm
    heater = temp is not None and temp < sp.temp_c - HYSTERESIS_TEMP_C
    humid = rh is not None and rh < sp.humidity_pct - HYSTERESIS_RH_PCT
    fan_pct = sp.fan_pct
    if co2 is not None and co2 > sp.co2_ceiling_ppm:
        # proportional fresh-air response: a fixed +25% bump cannot clear a large
        # CO2 error, it just parks the chamber above the ceiling forever.
        error = co2 - sp.co2_ceiling_ppm
        fan_pct = int(min(100, sp.fan_pct + max(10.0, error / 15.0 + co2_integral / 400.0)))
    return {"heater": bool(heater), "humidifier": bool(humid), "fan": fan_pct > 0, "light": sp.light_pct > 0,
            "fan_pct": fan_pct, "light_pct": sp.light_pct}


def evaluate(session: Session, gw: Gateway, device_id: str, settings: Settings, actor: str = "control-engine") -> dict[str, Any]:
    """One deterministic control tick: observe -> propose -> interlock -> dispatch.

    The current state comes from what the device *reports*, never from what the
    backend last commanded. A device reboot, a welded relay or a lost command
    otherwise leaves the backend believing an actuator is on when it is not —
    the classic way a "working" controller quietly stops controlling.
    """
    if session.get(Device, device_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    sp = active_setpoints(session, device_id)
    row = latest_clean(session, device_id)
    age_s = (utcnow() - as_utc(row.ts)).total_seconds() if row is not None else None
    fresh = age_s is not None and age_s <= settings.telemetry_stale_s
    sensors_ok = fresh and all(getattr(row, f) is not None for f in CORE_FIELDS)
    # PI integral with anti-windup and a release term, in device time.
    _now, _dt = _device_time(session, device_id)
    error = max(0.0, (row.co2_ppm - sp.co2_ceiling_ppm)) if row is not None and row.co2_ppm is not None else 0.0
    integral = _co2_integral.get(device_id, 0.0)
    integral = min(integral + error * _dt, 40000.0) if error > 0 else max(0.0, integral - 200.0 * _dt)
    _co2_integral[device_id] = integral
    proposed = propose_actuators(sp, row, co2_integral=integral)

    mirror = {a.kind: a.state for a in session.scalars(select(Actuator).where(Actuator.device_id == device_id))}
    reported = dict(row.actuators or {}) if row is not None else {}
    current = {name: bool(reported.get(name, mirror.get(name, False))) for name in ACTUATOR_NAMES}
    reported_pct = {
        "fan_pct": int(row.fan_pct) if row is not None and row.fan_pct is not None else sp.fan_pct,
        "light_pct": int(row.light_pct) if row is not None and row.light_pct is not None else sp.light_pct,
    }

    runtime = runtime_for(device_id)
    now_s, dt = _now, _dt
    runtime.observe(current, now_s, dt)

    allowed, vetoes = evaluate_actuators(
        proposed, temp_c=row.temperature_c if row else None,
        humidity_pct=row.humidity_pct if row else None, co2_ppm=row.co2_ppm if row else None,
        sensors_valid=sensors_ok, runtime=runtime, now_s=now_s,
    )

    dry_run = settings.dry_run
    dispatched: list[dict[str, Any]] = []
    memo = _dispatch_memo.setdefault(device_id, {})

    def may_dispatch(slot: str, target: Any, min_gap_s: float) -> bool:
        """Re-assert a divergent target, but do not spam the bus every tick."""
        prev = memo.get(slot)
        if prev is not None and prev[0] == target and (now_s - prev[1]) < min_gap_s:
            return False
        memo[slot] = (target, now_s)
        return True

    for name in ACTUATOR_NAMES:
        target = bool(allowed[name])
        actuator = session.get(Actuator, f"{device_id}:{name}")
        if actuator is not None:
            actuator.state = current[name]            # mirror reality, not intent
            actuator.runtime_s = runtime.runtime_s.get(name, 0.0)
            actuator.updated_at = utcnow()
        if target == current[name] or not may_dispatch(name, target, 15.0):
            continue
        if dry_run:
            audit.record(session, actor, "control.dry_run_actuator", device_id, actuator=name, state=target)
            continue
        result = gw.dispatcher.dispatch(
            session, device_id,
            CommandCreate(kind=CommandKind.ACTUATOR, payload={"actuator": name, "state": target},
                          idempotency_key=f"ctl:{device_id}:{name}:{target}:{int(now_s)}", ttl_s=45),
            actor,
        )
        dispatched.append({"actuator": name, "state": target, "command_id": result.command.command_id,
                           "safety_ok": result.safety.ok})

    # Fan/light are modulated, not just switched: a speed change with the boolean
    # unchanged still has to reach the device.
    pct = {"fan_pct": int(allowed["fan_pct"]), "light_pct": int(allowed["light_pct"])}
    if pct != reported_pct and may_dispatch("pct", tuple(sorted(pct.items())), 5.0):
        if dry_run:
            audit.record(session, actor, "control.dry_run_setpoint", device_id, **pct)
        else:
            result = gw.dispatcher.dispatch(
                session, device_id,
                CommandCreate(kind=CommandKind.SETPOINT, payload={**sp.model_dump(), **pct},
                              idempotency_key=f"ctl-pct:{device_id}:{pct['fan_pct']}:{pct['light_pct']}:{int(now_s)}",
                              ttl_s=45),
                actor,
            )
            dispatched.append({"actuator": "fan/light", "state": pct,
                               "command_id": result.command.command_id, "safety_ok": result.safety.ok})

    if not fresh:
        audit.record(session, "safety", "control.stale_telemetry", device_id,
                     age_s=round(age_s, 1) if age_s is not None else None,
                     limit_s=settings.telemetry_stale_s)
    if vetoes:
        audit.record(session, "safety", "control.veto", device_id, vetoes=vetoes, proposed=proposed, allowed=allowed)
    session.commit()
    return {
        "device_id": device_id, "setpoints": sp.model_dump(), "sensors_valid": sensors_ok,
        "telemetry_age_s": round(age_s, 1) if age_s is not None else None, "telemetry_fresh": fresh,
        "observed": {**current, **reported_pct}, "proposed": proposed, "allowed": allowed, "vetoes": vetoes,
        "dry_run": dry_run, "dispatched": dispatched, "telemetry_ts": row.ts if row else None,
        "device_time_s": round(now_s, 1), "co2_integral": round(integral, 1),
    }


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.post("/recipes", response_model=RecipeVersionRead, status_code=status.HTTP_201_CREATED)
def create_recipe_route(payload: RecipeCreate, session: Session = Depends(get_session),
                        principal: Principal = Depends(require_operator)) -> RecipeVersion:
    return create_recipe(session, payload, principal.actor)


@router.get("/recipes", response_model=list[RecipeVersionRead])
def list_recipe_versions(recipe_id: str | None = None, session: Session = Depends(get_session),
                         _: Principal = Depends(require_operator)) -> list[RecipeVersion]:
    stmt = select(RecipeVersion).order_by(RecipeVersion.created_at.desc())
    if recipe_id:
        stmt = stmt.where(RecipeVersion.recipe_id == recipe_id)
    return list(session.scalars(stmt))


@router.post("/devices/{device_id}/recipe/{recipe_version_id}")
def apply_recipe_route(device_id: str, recipe_version_id: str, body: ApprovalRequest | None = None,
                       dry_run: bool = False, session: Session = Depends(get_session),
                       principal: Principal = Depends(require_operator),
                       gw: Gateway = Depends(get_gateway)) -> dict[str, Any]:
    return apply_recipe(session, gw, device_id, recipe_version_id, principal.actor,
                        dry_run=dry_run, reason=(body.note if body else None) or "recipe applied")


@router.post("/devices/{device_id}/override")
def override_route(device_id: str, actuator: str, state: bool, dry_run: bool = False,
                   session: Session = Depends(get_session), principal: Principal = Depends(require_operator),
                   gw: Gateway = Depends(get_gateway)) -> dict[str, Any]:
    if actuator not in {"heater", "humidifier", "fan", "light"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown actuator")
    return manual_override(session, gw, device_id, actuator, state, principal.actor, dry_run=dry_run)


@router.post("/devices/{device_id}/control/evaluate")
def evaluate_route(device_id: str, session: Session = Depends(get_session),
                   principal: Principal = Depends(require_operator),
                   gw: Gateway = Depends(get_gateway),
                   settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return evaluate(session, gw, device_id, settings, principal.actor)


@router.get("/devices/{device_id}/control/state")
def control_state(device_id: str, session: Session = Depends(get_session),
                  _: Principal = Depends(require_operator)) -> dict[str, Any]:
    row = latest_clean(session, device_id)
    return {
        "device_id": device_id,
        "setpoints": active_setpoints(session, device_id).model_dump(),
        "envelope": load_envelope().model_dump(),
        "actuators": {a.kind: {"state": a.state, "runtime_s": round(a.runtime_s, 1)}
                      for a in session.scalars(select(Actuator).where(Actuator.device_id == device_id))},
        "control_loop": {"enabled": get_settings().control_loop_enabled,
                         "interval_s": get_settings().control_interval_s},
        "latest_telemetry": None if row is None else {
            "ts": row.ts, "temperature_c": row.temperature_c, "humidity_pct": row.humidity_pct,
            "co2_ppm": row.co2_ppm, "quality_flags": row.quality_flags, "quality_score": row.quality_score,
        },
    }


class ControlLoop:
    """Phase 4 setpoint scheduler: runs the deterministic controller on a timer.

    Without this the recipe is only a stored intention — the chamber drifts.
    Every tick goes through the same interlocks and the same command bus as a
    manual action, so there is exactly one actuation path to audit.
    """

    def __init__(self, gw: Gateway, settings: Settings) -> None:
        self.gw = gw
        self.settings = settings
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def devices_to_control(self, session: Session) -> list[str]:
        rows = session.scalars(
            select(Device).where(Device.status.in_(["online", "fail_safe"]))
        ).all()
        return [d.device_id for d in rows if biology.active_cycle_for_device(session, d.device_id) is not None]

    def run_once(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with session_scope() as session:
            device_ids = self.devices_to_control(session)
        for device_id in device_ids:
            try:
                with session_scope() as session:
                    out.append(evaluate(session, self.gw, device_id, self.settings, actor="control-loop"))
            except Exception:
                log.exception("control tick failed for %s", device_id)
        return out

    def start(self) -> None:
        if not (self.settings.background_workers and self.settings.control_loop_enabled):
            return
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="control-loop", daemon=True)
        self._thread.start()
        log.info("control loop started (%.1fs interval)", self.settings.control_interval_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.settings.control_interval_s):
            try:
                self.run_once()
            except Exception:
                log.exception("control loop iteration failed")
