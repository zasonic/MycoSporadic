# MycoSporadic grow-controller frontend

Responsive React frontend preview for MycoSporadic, based on the companion cultivation-controller planning documents. This is a standalone demo, not a replacement for the existing `/console` operator interface.

## Run

```sh
npm ci
npm run dev
```

Run these commands from `frontend/`. Use Node.js 22.12 or newer. The development server uses port 5000, independently of the Python backend on port 8000.

## Build

```sh
npm run check
npm run build
```

The static frontend is emitted to `dist/public`.

## Scope

The interface includes three demonstration zones, reversible manual output controls, editable target bands, simulated charts, session activity, a parts calculator based on the supplied CSV, commissioning checks, and per-zone YAML export.

All readings are sample data. No hardware commands are sent. Changes are stored in React state and reset on refresh. This frontend does not currently call the repository's FastAPI backend. The included Express development scaffold does not provide live telemetry or control.

Live integration must use the existing backend's authenticated, safety-constrained API and preserve its command validation, approval requirements, telemetry quality gates, and independent watchdog design. Browser output toggles must not be connected directly to relays or bypass those controls.

The original standalone ESPHome template is embedded in `client/src/firmware.json`. YAML exports substitute the selected zone name and session target bands into a copy of that template. It is not a validated implementation of the backend's MQTT contract, actuator inventory, or fail-safe behavior. Validate generated YAML in ESPHome and reconcile it with `src/mycoloop/contracts/` before considering any deployment to hardware.

Parts data in `client/src/parts.json` is copied from the supplied 15 Nov 2026 planning snapshot and has not been independently reverified. Optional and alternative items are excluded from the estimated total.

## Review notes

- No Python backend, database schema, existing console, or hardware control logic is modified.
- `FLOWS.md` records browser regression checks performed on the preview.
- Charts, setpoints, manual output controls, commissioning progress, and activity are session-only demonstration state.
- The frontend loads Satoshi from Fontshare; its sans-serif fallback remains available if the font service is blocked.
