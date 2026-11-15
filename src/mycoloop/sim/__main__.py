"""Run a simulated chamber as a separate process against a real broker.

    python -m mycoloop.sim --device chamber-01 --api http://localhost:8000 \
        --mqtt-host localhost --speed 4 --fault rh_saturation

Registers itself through the public API (so it gets a real device token), then
speaks the AsyncAPI contract over MQTT 5 exactly like a Mycodo/ESPHome node.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
import urllib.error
import urllib.request

from ..bus import MqttBus
from ..config import Settings
from ..safety import load_envelope
from .chamber import FAULTS, SimChamber
from .device import SimDevice


def register(api: str, key: str, device_id: str) -> str:
    body = json.dumps({"device_id": device_id, "name": f"Simulated chamber {device_id}",
                       "edge_stack": "simulated", "firmware": "sim-1.0.0"}).encode()
    req = urllib.request.Request(api.rstrip("/") + "/devices", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read())["token"]
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            print(f"device {device_id} already registered; requesting a token rotation via admin key")
            req = urllib.request.Request(api.rstrip("/") + f"/devices/{device_id}/token", data=b"", method="POST",
                                         headers={"X-API-Key": key})
            with urllib.request.urlopen(req, timeout=15) as res:
                return json.loads(res.read())["token"]
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mycoloop-sim")
    ap.add_argument("--device", default="chamber-01")
    ap.add_argument("--api", default="http://localhost:8000", help="backend URL for registration")
    ap.add_argument("--key", default="dev-operator-key")
    ap.add_argument("--mqtt-host", default="localhost")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--speed", type=float, default=4.0, help="simulated seconds per wall second")
    ap.add_argument("--noise", type=float, default=0.4)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--fault", action="append", default=[], choices=list(FAULTS))
    ap.add_argument("--no-register", action="store_true", help="use --token instead of registering")
    ap.add_argument("--token", default="sim-token")
    args = ap.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    settings = Settings(bus="mqtt", mqtt_host=args.mqtt_host, mqtt_port=args.mqtt_port,
                        mqtt_client_id=f"sim-{args.device}")
    token = args.token if args.no_register else register(args.api, args.key, args.device)

    bus = MqttBus(settings)
    bus.start()
    chamber = SimChamber(load_envelope(), noise=args.noise, seed=args.seed)
    dev = SimDevice(bus, args.device, token, chamber=chamber)
    for fault in args.fault:
        dev.set_fault(fault)
    dev.run(speed=args.speed)
    print(f"simulated chamber {args.device} streaming to mqtt://{args.mqtt_host}:{args.mqtt_port} "
          f"at {args.speed}x — Ctrl-C to stop")

    stop = {"flag": False}

    def handler(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    try:
        while not stop["flag"]:
            time.sleep(5)
            s = dev.snapshot()
            print(f"  t={s['uptime_s']:>7.1f}s  {s['temp_c']:>5.2f}C  {s['humidity_pct']:>5.1f}%  "
                  f"{s['co2_ppm']:>5d}ppm  watchdog={s['watchdog_remaining_s']:>5.1f}s  "
                  f"{'FAIL-SAFE' if s['fail_safe'] else 'armed'}  cmds={s['commands_received']}")
    finally:
        dev.stop()
        bus.stop()
        print("simulator stopped (offline status published)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
