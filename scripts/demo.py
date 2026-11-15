"""Seed a running MycoLoop API with a believable history, then a live cycle.

    python scripts/demo.py --api http://localhost:8000

Gives the optimizer real completed cycles to learn from and the console
something honest to display. Idempotent-ish: existing entities are reused.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

STAGES = ["COLONIZING", "COLONIZATION_COMPLETE", "PINNING_INITIATED", "PINNING_ACTIVE",
          "MATURATION", "HARVEST_READY"]


class Client:
    def __init__(self, base: str, key: str) -> None:
        self.base, self.key = base.rstrip("/"), key

    def call(self, method: str, path: str, body=None, ok=(200, 201)):
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-API-Key": self.key},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                payload = json.loads(res.read() or "null")
                return res.status, payload
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = raw
            if e.code in ok:
                return e.code, payload
            return e.code, payload

    def post(self, path, body=None, ok=(200, 201)):
        return self.call("POST", path, body, ok)

    def get(self, path):
        return self.call("GET", path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--key", default="dev-operator-key")
    ap.add_argument("--device", default="chamber-01")
    ap.add_argument("--cycles", type=int, default=6)
    args = ap.parse_args()
    c = Client(args.api, args.key)

    for _ in range(30):
        try:
            if c.get("/healthz")[0] == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        print("API not reachable at", args.api)
        return 1
    print("API is up")

    status, body = c.post(f"/sim/devices/{args.device}")
    print(f"simulator {args.device}: {status}")

    for sid, name, species in [("blue-oyster", "Blue Oyster", "Pleurotus ostreatus"),
                               ("pink-oyster", "Pink Oyster", "Pleurotus djamor"),
                               ("lions-mane", "Lion's Mane", "Hericium erinaceus")]:
        c.post("/strains", {"strain_id": sid, "name": name, "species": species}, ok=(201, 409))
    for bid in ("SB-101", "SB-102", "SB-103"):
        c.post("/substrate-batches", {"batch_id": bid, "mass_kg": 2.5}, ok=(201, 409))

    setpoints = {"temp_c": 19, "humidity_pct": 92, "co2_ceiling_ppm": 900, "fan_pct": 45, "light_pct": 60}
    st, rv = c.post("/recipes", {"recipe_id": "R-07", "name": "Oyster fruiting",
                                 "stage": "PINNING_ACTIVE", "setpoints": setpoints})
    if st != 201:
        rv = c.get("/recipes?recipe_id=R-07")[1][0]
    print("recipe version:", rv["recipe_version_id"])

    strains = ["blue-oyster", "pink-oyster", "lions-mane"]
    yields = [412, 468, 390, 521, 445, 502]
    for i in range(args.cycles):
        cid = f"GC-HIST-{i+1:03d}"
        st, cycle = c.post("/grow-cycles", {
            "cycle_id": cid, "strain_id": strains[i % 3], "substrate_batch_id": f"SB-10{(i % 3) + 1}",
            "device_id": args.device, "recipe_version_id": rv["recipe_version_id"],
            "notes": "historical cycle for optimizer training",
        }, ok=(201, 409))
        if st != 201:
            continue
        for stage in STAGES:
            c.post(f"/grow-cycles/{cid}/transitions", {"to_stage": stage, "reason": "demo history"})
        c.post(f"/grow-cycles/{cid}/harvests", {"grams": yields[i % len(yields)], "flush": 1, "quality": "A"})
        c.post(f"/grow-cycles/{cid}/transitions", {"to_stage": "FLUSH_COMPLETE", "reason": "demo history"})
        if i == 3:
            c.post(f"/grow-cycles/{cid}/contamination", {"kind": "trichoderma", "severity": "localized",
                                                        "notes": "second flush lost"})
        print(f"  history {cid}: {yields[i % len(yields)]} g")

    st, live = c.post("/grow-cycles", {
        "cycle_id": "GC-LIVE-001", "strain_id": "blue-oyster", "substrate_batch_id": "SB-101",
        "device_id": args.device, "recipe_version_id": rv["recipe_version_id"], "notes": "live cycle",
    }, ok=(201, 409))
    if st == 201:
        for stage in ["COLONIZING", "COLONIZATION_COMPLETE", "PINNING_INITIATED", "PINNING_ACTIVE"]:
            c.post("/grow-cycles/GC-LIVE-001/transitions", {"to_stage": stage, "reason": "demo live cycle"})
    print("live cycle: GC-LIVE-001 @ PINNING_ACTIVE")

    c.post("/vision/models", {"model_version": "yolo-mushroom-v0.3", "family": "yolo",
                              "dataset": "M18K RGB-D + chamber corrections",
                              "metrics": {"mAP50": 0.81}, "edge_runtime": "onnxruntime"}, ok=(201, 409))
    for i, (pins, contam, conf) in enumerate([(38, 0.04, 0.93), (44, 0.08, 0.88), (52, 0.78, 0.71), (61, 0.06, 0.64)]):
        c.post("/vision/frames", {
            "cycle_id": "GC-LIVE-001", "device_id": args.device, "model_version": "yolo-mushroom-v0.3",
            "confidence": conf, "storage_uri": f"file:///frames/demo-{i}.jpg",
            "features": {"colonization_percent": 96.0, "pin_count": pins, "pin_density": 12.5,
                         "cap_area_estimate": 88.0, "cap_volume_estimate": 41.0, "maturity_score": 0.42,
                         "aborted_pin_count": 2, "growth_rate_24h": 18.4,
                         "contamination_probability": contam,
                         "contamination_type": "trichoderma" if contam > 0.7 else "none",
                         "wetness_score": 0.6, "dryness_score": 0.2, "condensation_score": 0.35},
        })
    print("vision frames ingested (1 contamination alert, 2 in the active-learning queue)")

    c.post(f"/sim/devices/{args.device}/tick?n=20")
    st, rec = c.post(f"/optimizer/recommendations?recipe_id=R-07&device_id={args.device}",
                     {"temp_min": 16, "temp_max": 21, "rh_min": 88, "rh_max": 94,
                      "co2_max": 1200, "fae_min": 40, "fae_max": 75})
    if st == 201:
        print(f"recommendation {rec['recommendation_id']} pending · confidence {rec['confidence']} "
              f"· violations {len(rec['violations'])}")
    print("\nOpen", args.api + "/console")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
