"""Export the Phase 0 contracts to build artefacts.

    python scripts/export_contracts.py [--out contracts/generated]

Writes openapi.json, asyncapi.yaml, safety-envelope.yaml and one JSON Schema per
Pydantic contract, so frontend/edge/CI codegen never reads Python.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mycoloop import schemas  # noqa: E402
from mycoloop.config import CONTRACTS_DIR  # noqa: E402
from mycoloop.main import create_app  # noqa: E402

CONTRACT_NAMES = [
    "Telemetry", "TelemetryBatch", "DeviceHealth", "DeviceStatusMessage",
    "CommandCreate", "CommandRead", "CommandAck", "DispatchResult",
    "DeviceCreate", "DeviceRead", "SensorRead", "CalibrationUpdate",
    "Setpoints", "SafetyEnvelope", "SafetyVerdict", "Violation",
    "StrainCreate", "SubstrateBatchCreate", "RecipeCreate", "RecipeVersionRead",
    "GrowCycleCreate", "GrowCycleRead", "StageTransition", "StageEventRead",
    "HarvestCreate", "HarvestRead", "ContaminationCreate", "AnnotationCreate", "CycleTimeline",
    "PhenotypeFeatures", "VisionFrameCreate", "VisionInferenceRead", "HumanLabelCreate", "ModelRegister",
    "SearchSpace", "OptimizerTrialCreate", "OptimizerTrialRead", "RecommendationRead", "ApprovalRequest",
    "AuditRead", "IngestResult", "TelemetryCleanRead", "QuarantineRead", "DriftReport",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "contracts" / "generated"))
    args = parser.parse_args()
    out = Path(args.out)
    (out / "schemas").mkdir(parents=True, exist_ok=True)

    app = create_app()
    (out / "openapi.json").write_text(json.dumps(app.openapi(), indent=2) + "\n")
    for name in ("asyncapi.yaml", "safety-envelope.yaml", "state-machine.md", "failure-modes.md", "hil-test-plan.md"):
        shutil.copyfile(CONTRACTS_DIR / name, out / name)

    for name in CONTRACT_NAMES:
        model = getattr(schemas, name)
        (out / "schemas" / f"{name}.json").write_text(json.dumps(model.model_json_schema(), indent=2) + "\n")

    index = {
        "openapi": "openapi.json",
        "asyncapi": "asyncapi.yaml",
        "safety_envelope": "safety-envelope.yaml",
        "schemas": sorted(f"schemas/{n}.json" for n in CONTRACT_NAMES),
    }
    (out / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    print(f"exported {len(CONTRACT_NAMES)} schemas + openapi + asyncapi -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
