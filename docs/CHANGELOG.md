# Change Log — pixIQ Yarn Cone Inspection

## Unreleased — 2026-07-31

### Changed
- **UV Inspection Algorithm Replaced**: `UVInspection` (`src/inspection/uv_inspection.py`) now uses a blue-channel radial-profile + angular-coverage approach instead of log(G/B) ratio + degree-2 polynomial baseline. Root cause: this camera has no yellow-green emission filter, so green sits near the sensor noise floor (mean ~10-20 vs blue ~40-80) — log(G/B) was dividing the strong channel by the weak one, and its deployed threshold had already drifted from the validated 0.024 to 0.77-0.8 in the field with good/bad cones barely separated. New algorithm validated directly against ~35-60 labeled Indorama good/bad cones across several batches, reaching 94-100% accuracy with 0 false positives. See `docs/08_uv_inspection.md` for full details.
- **`UVResult` field meanings repurposed** (names unchanged for API/UI compatibility): `radial_dip` now holds `depth_pct` (was log(G/B) max dip), `gb_ratio` now holds `coverage_pct` (was mean G/B monitoring ratio).
- **Config**: `uv_inspection.radial_dip_threshold` changed from `0.77` to `1.33` (new units — % of local baseline, not log-ratio). Added `uv_inspection.coverage_threshold` (`18.2`, % of circumference) — a cone is flagged if *either* threshold is crossed.

### Notes
- This is a smaller validation sample (~35-60 images) than the original log(G/B) calibration (1959 images) — treat current thresholds as a pilot calibration, recalibrate as more labeled cones accumulate (see `docs/08_uv_inspection.md` §8.9).
- Deployed directly on Indorama (trial mode) without a local-repo commit/push/pull round trip.

## Unreleased — 2026-05-28

### Added
- **Manifest-Driven Teach Capture Workflow**: Session-based teaching pipeline for one active teach module at a time (`stain`, `uv`, `tail`, `dimension`) with lifecycle tracking (OPEN → PENDING_REVIEW → UPLOADED). Located in `docs/fleet_managements/Workflow_Session_Manifest.md`.
- **Unified Teach Toggle** (`PUT /config/teach`): Single entry point for the entire capture lifecycle — toggle ON creates session + emits `set_capture_mode`, toggle OFF stops session + updates manifest. Accepts short module keys (`tail`, `stain`, `uv`, `dimension`) and `operator_id`.
- **Session Manifest (manifest.json)**: Tracks session metadata (ID, operator, device info, capture stats, blob prefix). `part.inspection_type` derived from `module.upper()`.
- **Auto-Stop at 200 Images**: Session automatically transitions to PENDING_REVIEW, resets `config.json` teach flag to `false`, and pauses runtime after the current PLC ack when `_installation_min_capture` is reached.
- **Teaching Progress Alerts**: Socket.IO `teaching_alert` events emitted per frame save for real-time UI progress tracking.
- **Rclone Upload Integration**: Replaced manual sequential file upload in `BlobUploader.upload_session()` with `rclone sync` subprocess using `sieger_azure` remote.
- **Teach Capture Override**: `_run_inspection_cycle()` rereads teach config each cone; active teach routes to `_run_capture_cycle()` Path A, saves module data, writes PLC result `0`, and bypasses normal inspection.
- **Runtime Pause Helper**: Manual teach OFF and auto-stop move the service to IDLE, flush buffers, stop camera acquisition, and write PLC `ips_status=3`.
- **Azure Config & Credentials Loading**: Environmental variables loaded from `.env` on API startup.

### Changed
- **Database Schema v6 → v7**: Added two columns to `capture_sessions` table (`operator_id`, `capture_status`). Migration runs automatically on startup.
- **`inspection_type` Completely Removed**: The redundant column has been removed from database schema, DDL definition, and migrations. Derived as `module.upper()` on manifest and API layer.
- **Config Teach Keys Renamed**: `stain_detection`→`stain`, `uv_inspection`→`uv`, `tail_inspection`→`tail`, `dimension_check`→`dimension` in `config.json`.
- **`POST /capture/sessions/start` and `POST /capture/sessions/stop` Deprecated**: Logic moved to `PUT /config/teach`. Endpoints remain but log deprecation warnings.
- **IDLE→CAPTURE Auto-Activation Removed**: `set_capture_mode` no longer transitions to CAPTURE state. Operator starts inspection normally; if inspection is already running, teach mode is picked up on the next cone cycle.
- **Inspection Service State**: Added session tracking (`teach_session_start_time`, `teach_capture_count`, `capture_session_id`, `capture_module`). Device identity derived from Jetson serial → manifest.
- **Image Save Routing**: `_run_capture_cycle()` Path A routes teach images to `captures/sessions/<module>/<session_id>/images/<sample_counter>.png`; legacy capture paths still coexist.
- **Session ID Format**: UUID4.hex (32 hex chars) for global uniqueness and timezone safety.
- **Cloud Upload Engine**: Transitioned from native Azure Blob SDK to system `rclone` sync engine via subprocess.
- **Azure Blob Prefix & Metadata Target**: Standardized uploaded path to mirror local directory structure.

### Fixed
- **SQLite Schema DDL Bug**: Patched the raw `_DDL` schema definition string in `src/db/schema.py` for `capture_sessions` table to match the exact v7 columns (`operator_id`, `capture_status`), preventing failures on fresh database installations.

### Backward Compatibility
- ✅ Zero breaking changes to inspection workflows
- ✅ Zero breaking changes to legacy capture (tube auto-teach, PLC handshake unchanged)
- ✅ Coexists with existing `captures/` folder structure
- ✅ Non-teach captures have zero overhead (single boolean check)
- ⚠️ **Breaking**: `PUT /config/teach` body keys changed from verbose (`tail_inspection`) to short (`tail`). Sieger-UI must update.

### Session Lifecycle
- **OPEN**: Session created, images being captured to `captures/sessions/<module>/<session_id>/images/`
- **PENDING_REVIEW**: Session stopped manually OR auto-stopped at 200 images. Ready for operator review and training decision.
- **UPLOADED**: Session images uploaded to Azure Blob Storage. Training dataset ready for cloud pipeline.

### Folder Structure
```
captures/
  sessions/
    tail/
      a1b2c3d4e5f6789012345678abcdef90/
        manifest.json
        images/
          <sample_counter>.png
          <sample_counter>.png
          ...
          <sample_counter>.png
    stain/
      ...
    uv/
      ...
  (legacy layout coexists)
  tail/
    2/
      camera_key/
        timestamp.png
```

### Testing
- Enable teach mode + start session: `PUT /config/teach {"tail": true, "operator_id": "OP-042"}`
- Monitor progress via Socket.IO `teaching_alert` events (e.g. `teaching_alert {count: 47, total: 200}`)
- Stop session (manual): `PUT /config/teach {"tail": false}`
- Stop session (auto): Let 200 cones pass; auto-stop updates manifest, resets config teach flag to false, pauses runtime to IDLE, and waits for UI `start_inspection`
- View images: `GET /capture/images?session_id=...&module=tail`
- Upload: `POST /cloud/upload {"module": "tail", "session_id": "..."}`

---

## Unreleased — 2026-05-14

### Added
- Mock PLC for testing scenarios, located in `testing/Mock_PLC/`. Simulates PLC behavior for development and debugging purposes.
- Extended waiting time on startup service logs to improve debugging visibility.

### Changed
- Deployment scripts (`scripts/bootstrap.sh`, `scripts/deploy.sh`) updated for enhanced error handling and reporting.
- Systemd service templates (`deploy/systemd/sieger-*.service`) now support dynamic variable injection for user and group configurations.
- System setup script (`scripts/system_setup.sh`): Step 4 now auto-installs CUDA runtime packages (libcuda, libcublas, cupti, nvtx, DLA) and registers dynamic linker path before TensorRT verification.

### Fixed
- ONNX compatibility issue in `export_tensorrt.py` for Jetson PyTorch nv24.05.
- Minor bugs in deployment health checks and JSON reporting.

### Deployment Automation
- **Bootstrap Script** (`scripts/bootstrap.sh`): Stage 1 minimal prerequisites installer with GitHub SSH setup, repository cloning, and phase-tracked JSON reporting (`/tmp/pixiq_bootstrap_report.json`).
- **Deploy Script** (`scripts/deploy.sh`): Stage 2 orchestration across 9 phases (preflight, tools, DVC, models, Python, config, TensorRT, services, health). Includes `--status` flag to display last deployment report. Full JSON reporting with deployment ID, phase tracking, error/warning aggregation.
- **Power Mode Coordination**: Detects current Jetson power mode vs target mode. Halts deployment with clear instructions if reboot is required. Idempotent reruns detect completed phases and skip them.
- **System Setup Enhancement** (`scripts/system_setup.sh`): Configurable `TARGET_POWER_MODE` variable (0=MAXN, 2=15W, 3=30W). Auto-detects operator user via `$SUDO_USER`. Pylon SDK auto-downloads from SharePoint. Moves `uv` to system-wide `/usr/local/bin`. `INSTALL_DIR` defaults to user home.

### Service Architecture
- **systemd Templates** (`deploy/systemd/sieger-*.service`): Replaced hardcoded paths/users with `{{PLACEHOLDER}}` variables (`DEPLOY_USER`, `DEPLOY_GROUP`, `WORKDIR`, `VENV_PYTHON`). Runtime substitution via `sed` in Phase 8.
- **Service Isolation**: Memory limits (sieger-inspection 8G, sieger-api 2G). GPU access groups for inspection service. CPU quota for API (50%).

### Python & Dependencies
- **pyproject.toml Restructure**: Removed deprecated jetson-ai index. Pinned `torch==2.4.0`, `torchvision==0.18.0` for JetPack 6. ONNX `>=1.16.0,<1.17.0`. Ultralytics `>=8.3.0,<8.4.0`. Added `[tool.uv]` environment constraint for aarch64 Linux. `[tool.uv.sources]` local wheel paths for Jetson Torch/TorchVision (downloaded at deploy time).
- **Wheel Download Integration**: Phase 5 pre-fetches Jetson Torch/TorchVision wheels from SharePoint if missing. Enables offline deployments.

### TensorRT Export
- **ONNX Compatibility Patch** (`scripts/export_tensorrt.py`): Jetson PyTorch nv24.05 lacks `dynamo` kwarg in `torch.onnx.export`. Implemented runtime patching to strip incompatible kwarg before calling upstream ONNX export.

### Observability
- **Multi-Phase Reporting**: Console table + detailed check results. JSON report (`/tmp/pixiq_deploy_report_<timestamp>.json`) with deployment ID, per-phase duration, error/warning counts, validation summary.
- **Operator Summary**: Clear "next actions" block (e.g., "Reboot required", "config.json has placeholders").
- **Idempotent Skipping**: Detects already-completed phases (models, engines, venv) and skips them on rerun.

---

## Unreleased — 2026-04-29

### Environment & Stability
- **Dependency Management**: Transitioned to `uv` for python dependencies.
- **Core Dump Fix**: Permanently removed `onnxruntime` from `pyproject.toml`. The system now runs exclusively on native TensorRT engines to avoid ARM64 memory architecture conflicts.
- **Warning Suppression**: Suppressed `matplotlib` 3D warnings in `src/inspection/__init__.py` and Ultralytics "task guessing" warnings in `src/inspection/yolo_detector.py`.

### Inference Optimization
- **TensorRT Pipeline**: All models exported to FP16 engines via `scripts/export_tensorrt.py`.
- **ARM64 Compatibility**: Globally disabled ONNX simplification (`simplify=False`) in `YOLODetector` to prevent C++ assertion failures during runtime.
- **Task Definition**: Explicitly set `task="detect"` for all YOLO instances.

### Critical Bug Fixes
- **UV Inspection**: Resolved a fatal `AttributeError` crash by correctly passing `cone_bbox` to the `UVResult` constructor instead of attempting an illegal assignment on a tuple. This enables proper stream cropping in the Web UI.
- **Camera Trigger Debounce**: Fixed SFNC portability issue where `LineDebouncerTime` write silently failed on ace classic cameras (UV/Tail). Implemented model-aware node selection (`LineDebouncerTimeAbs` vs `LineDebouncerTime`) to ensure proximity-sensor bounce filtering is active across all three stations.

## Unreleased — 2026-04-14

- **Cameras**: Updated Tail camera model from acA1920-40gc (ace classic) to a2A1920-40gc (ace 2). Added lens specs: VL 25mm, UV 16mm, Tail 25mm. Updated all docs, diagrams, README, CLAUDE.md, and source docstrings.
- **Diagrams**: Added `inspection_sequence.mmd` (capture sequence diagram) and `system_block_diagram.mmd` (full system block diagram) to `docs/diagrams/`. Corrected camera models and added lens specs in all `.mmd` files.
- **Docs overhaul**: Purged all stale x86/IDS references from documentation. Updated `project_context.md`, `01_system_overview.md`, `02_architecture.md`, `04_camera_capture.md`, `05_inspection_pipeline.md`, `10_rest_api.md`, `13_configuration.md`, `14_deployment.md`, `17_pixiq_setup.md`, `index.md`, `api_reference.md`, `frontend_v3_guide.md`. All docs now reflect pixIQ as primary platform: Jetson Orin NX 16GB, Basler GigE cameras (pypylon), TensorRT FP16, 192.168.1.0/24 subnet, Line1 hardware trigger. Removed all IDS Peak, RTX 3050, 192.168.2.x, and "x86 as primary" references.
- **Mermaid diagrams**: Replaced all ASCII art diagrams with inline Mermaid across `01_system_overview.md`, `02_architecture.md`, `04_camera_capture.md`, `05_inspection_pipeline.md`, `06_tube_pattern.md`, `07_stain_detection.md`, `11_socketio_service.md`, `14_deployment.md`, `project_context.md`. Embedded system block diagram and inspection sequence from `.mmd` files inline in system overview and project context. Updated `00_index.md` diagram index.

## Unreleased — 2026-04-08
- **README**: Rewritten to reflect Jetson Orin NX + Basler pixIQ target. Removed stale x86/IDS hardware table; added TensorRT, pypylon, Line1 trigger, LatestImageOnly, and 16GB shared-memory notes. Updated `src/camera/` description from IDS Peak to Basler pylon.

## v1.0.0 — 2026-04-02 — pixIQ Fork (Basler + Jetson Orin NX)

### Breaking Changes
- **Camera SDK**: Replaced IDS Peak SDK with Basler pypylon. All IDS code removed.
- **Camera models**: acA1920-40gc (UV) + a2A1920-40gc (Tail) + a2A2600-20gcPRO (VL)
- **Hardware trigger**: Line1 (was Line0 on IDS)
- **Grab strategy**: `GrabStrategy_LatestImageOnly` auto-discards stale frames (replaces manual drain loop)
- **Device discovery**: pypylon `TlFactory.CreateFirstDevice()` by IP — no DeviceManager needed
- **Image conversion**: `pylon.ImageFormatConverter` (Bayer→BGR8packed) replaces `ids_peak_ipl`
- **Buffer management**: pypylon manages buffers internally (`MaxNumBuffer = 5`)

### Camera Observability
- **Three-layer counter pipeline**: Line1 edges (Counter2) → FrameStart (Counter1) → delivered (software)
- **Leakage detection**: debounced triggers, transport drops, block ID gaps, buffer underruns
- **Per-frame tracking**: BlockID gap detection, skipped frame count from LatestImageOnly
- **Temperature monitoring**: `get_temperature()` — ace classic (TemperatureAbs) + ace 2 (DeviceTemperature)
- **Line status**: `get_line_status()` — read current trigger input state
- **ace classic vs ace 2**: automatic detection and correct counter node names
- **Socket.IO integration**: `camera_health` summary in every `send_image` event (no polling needed)
- **`check_cameras` enriched**: full stats + health level in `camera_status` response
- **Frontend spec**: Camera Diagnostics panel on Settings page, status bar on Inspect page

### TensorRT Inference
- **YOLO auto-detection**: `YOLODetector` auto-detects `.engine` alongside `.pt` — no config change needed
- **Export script**: `scripts/export_tensorrt.py` exports all 3 YOLO models to TensorRT FP16 on target device
- **~5x speedup**: ~80ms (PyTorch) → ~15ms (TensorRT FP16) per YOLO inference on Orin NX
- **Fallback**: Delete `.engine` file to fall back to PyTorch `.pt` automatically

### Documentation
- **Chapter 17: pixIQ Setup** (`docs/17_pixiq_setup.md`) — complete first-time setup guide: hardware overview, dual NIC layout, network config, camera IP assignment, TensorRT export, Jetson perf tuning, verification, troubleshooting
- **Network architecture diagram** (`docs/diagrams/pixiq_network.mmd`) — Mermaid diagram showing cameras, switch, PLC, HMI, pixIQ dual NIC, services, and inference stack

### System Setup
- **`scripts/system_setup.sh`** — one-shot Jetson Orin NX setup: system packages, GigE network tuning (rmem_max=16MB), pylon SDK, TensorRT verification, uv + pypylon from source, MAXN power mode, jetson_clocks persistence
- **Dual NIC support**: auto-detects factory LAN NIC vs internet NIC, applies optimizations only to factory NIC
- **Jumbo frames**: MTU 9000 on factory LAN NIC only (reduces packet count ~6x for GigE cameras), internet NIC keeps MTU 1500
- **Routing sanity check**: warns if factory NIC and default route share the same interface
- Idempotent — safe to re-run

### New Repo
- Forked from `cone-transport-system` v3.1.0 as independent repo
- Target platform: Jetson Orin NX 16GB (ARM64, JetPack 6.x)
- No IDS SDK dependency — `pypylon>=4.0.0` only
- Project name: `sieger-pixiq-cv`

---

## v3.1.0 — 2026-04-01 — Session-Based Authentication (inherited from cone-transport-system)

### New Features

#### Authentication Module (`src/auth/`)
- **Session-based auth** — no JWT. Tokens are random uuid4 strings stored in SQLite `sessions` table.
- **Password hashing** with bcrypt.
- **Shift-length session expiry** — configurable via `config.json` → `auth.session_hours` (default: 8h).
- **Role-based access**: `superAdmin`, `engineer`, `operator`.
- **Service permissions**: per-user boolean flags (`live`, `master`, `settings`, `report`, `activityLog`, `inspection`, `email`).
- **Activity logging** — login, logout, and action audit trail in SQLite `activity_log` table.

#### Auth Endpoints (`/auth/*`)
- `POST /auth/login` — returns session token + user info
- `POST /auth/logout` — invalidates session
- `GET /auth/me` — current user from token
- `GET /auth/users` — list all users (superAdmin only)
- `POST /auth/users` — create user (superAdmin only)
- `PUT /auth/users/{username}` — update user (superAdmin only)
- `DELETE /auth/users/{username}` — delete user (superAdmin only)
- `POST /auth/users/{username}/reset-password` — reset password (superAdmin only)
- `GET /auth/activity` — activity log (superAdmin/engineer)

#### FastAPI Dependencies
- `get_current_user` — require valid session (401 if missing/expired)
- `get_optional_user` — return user if token provided, else None (for mixed endpoints)
- `require_role("superAdmin")` — role gate (403 if wrong role)
- `require_service("settings")` — service permission gate

#### Database
- Schema v5 → v6 migration: adds `users`, `sessions`, `activity_log` tables
- Default admin seeded on first startup (username/password from `config.json` → `auth.admin_username`/`auth.admin_password`, defaults to `admin`/`admin`)

#### Design Decisions
- **No JWT** — single backend, single SQLite DB. Session tokens are simpler, instantly revocable, no secrets to manage.
- **Read endpoints stay public** — monitoring (inspect, analytics) works without login. Write endpoints (teaching, settings, user mgmt) require auth.
- **Inspection loop is auth-independent** — PLC C2C drives inspection, no HMI login needed.
- **Replaces old loginservice** (Node.js + MongoDB on port 5000). Auth now lives in the CV backend (FastAPI port 5002).

#### Config Write API (No More Manual config.json Editing)
- `PUT /config/tasks` — toggle inspection modules on/off (dimension, stain, tube, UV, tail). No restart.
- `PUT /config/teach` — toggle teach mode per module (stain, UV, tail, dimension). Auto-captures lossless numpy crops during inspection. No restart.
- `PUT /config/shift` — update shift_hours. No restart. (Moved from SQLite settings table to config.json.)
- `PUT /config/cameras` — change camera settings (ip, serial, exposure, timeout, trigger_debounce_us per camera). Restart required.
- `PUT /config/plc` — change PLC settings (host, port, unit_id, timeout, poll_interval, full register mapping for input/output/light groups). Restart required.
- `GET /analytics/hourly?date=YYYY-MM-DD` — hourly Good/Defect/Error counts for line chart.
- config.json is now developer-only. All operator/engineer changes go through UI → API → config.json on disk.

#### Teaching Workflow Changes
- **Teach toggle** (`inspection.teach` in config.json) replaces manual `POST /capture/start` / `POST /capture/stop`.
- Each module has two toggles: **Inspect** (on/off) and **Teach** (on/off). When both are ON, system saves crops instead of running inference.
- Tube teaching remains fully autonomous — no teach toggle needed.
- Data capture is automatic: teach ON → system saves lossless numpy crops during inspection → counter increments → alert at 200 images → operator triggers training from teaching page.

#### Analytics Enhancements
- `GET /analytics` → `per_material` now includes `defect_types` breakdown per material.
- `GET /analytics/hourly` — new endpoint for hourly line chart data.

#### Removed Endpoints
- `POST /capture/start` — replaced by `PUT /config/teach` toggle.
- `POST /capture/stop` — replaced by `PUT /config/teach` toggle.
- `GET /settings` — replaced by `GET /config` (shift_hours now in config.json).
- `PUT /settings` — replaced by `PUT /config/shift`.

#### Removed from HMI
- Lights / Illumination control — PLC handles lights directly
- Error proofing page — removed
- Email settings — not in v3 (future)
- Per-master defect selection — replaced by inspect + teach toggles

#### Stale Code Cleanup
- Removed unused imports (`RequestContext`, `Optional`, `field`)
- Removed dead `_get_setting_float()` and `_get_setting_str()` methods
- Removed stale `POST /capture/start` reference in docstring
- Marked SQLite `settings` table as legacy (no code reads it; kept for backward compatibility)

#### Documentation Updates
- Updated 9 docs chapters for consistency: `02_architecture`, `07_stain_detection`, `09_tail_inspection`, `10_rest_api`, `11_socketio_service`, `12_teaching`, `13_configuration`, `api_reference`, `frontend_v3_guide`
- Renamed `frontend_guide.md` → `frontend_v3_guide.md`
- Added `frontend_hmi_reference.md` — complete old HMI page-by-page reference (31 pages)

### Dependencies
- Added `bcrypt>=4.2.0`

---

## v3.0.0 — 2026-03-25 — Autonomous Teaching, material_id Global Truth, Cloud Training

### Breaking Changes

- **Removed RecipeStore / master_id** — `material_id` (PLC `material_no` as string) is now the single source of truth. No lookup table. `material_no=42` → template `42.npz`. All DB rows use `material_id` column (string). Migration required on existing installations.
- **Removed PUT /config** — config changes require service restart (poka-yoke for production). No live config mutation endpoint.

### New Features

#### Autonomous Tube Teaching
- Unknown `material_id` (no matching `.npz`) → system automatically enters teaching mode
- `result=0` written to PLC for teaching cones (no pass/fail verdict)
- Auto-captures 256×256 annular tube crops; `tube_min_capture=20` triggers background teach
- `TubeTeacher.teach(pre_cropped=True)` trains Color NN + FFT NN, computes `p99_self_distance * 1.5` threshold, saves into `.npz`
- Hot-loads new template without service restart
- Operator sees progress via `teaching_alert` socket.io events on HMI
- Manual retrigger: POST `/teaching/tube`, POST `/teaching/tube/extend`

#### Per-Pattern Tube Threshold
- Threshold is no longer a global scalar in config
- Each `.npz` file carries its own threshold: `p99(self_distances) * 1.5`
- Threshold multiplier configurable: `tube_teaching.threshold_multiplier`

#### Module-Specific Crop Saving
- Stain: 256×256 annular cone crop (VL frame) → `sieger_data/captures/stain/`
- UV: 256×256 annular cone crop (UV frame) → `sieger_data/captures/uv/`
- Tail: top 60% of tail frame → `sieger_data/captures/tail/`
- Dimension: full VL frame → `sieger_data/captures/dimension/`

#### Cloud Training Upload
- POST `/cloud/upload` uploads captured crops to Azure Blob Storage
- Container: `sieger-training`
- Blob path: `{module}/{session_id}/{filename}`
- Stain, UV, tail captures are cloud-training eligible
- Training runs on A100 cloud; model downloaded and reloaded on completion

#### result=0 for Teaching Cones
- Any cone inspected during tube auto-teaching writes `result=0` to PLC
- PLC receives ACK (cycle completes normally) but gets no pass/fail verdict
- Prevents false defect counts during template bootstrap

#### Global Dimension Specs
- Dimension tolerances are site-wide (not per-material)
- POST `/teaching/dimension` sets `pixels_per_mm`, `cone_diameter_mm`, `tube_diameter_mm`, tolerances
- Applied globally to all materials — one calibration per installation

### Infrastructure

- **systemd units** added to `deploy/systemd/`: `sieger-api.service`, `sieger-inspection.service`, `sieger-report.service`
- **Nginx config** added to `deploy/nginx/sieger.conf`
- **BOM documented**: Jetson Orin NX + ASUS NUC 14 as alternative to x86 desktop (hot/dusty environments). See `docs/deployment.md`.

---

# Change Log — Sieger V2 Yarn Cone Inspection

| 2026-03-21 | Production reliability fixes — 4 critical bugs + 4 warnings | Full codebase review (Gemini pre-scan + Opus deep review). Fixes: **C1** `_worker_loop` — added `try/except` inside `while` loop so any unhandled exception is logged and the loop continues (previously thread died silently → PLC deadlock). **C2** `_inspect_and_report` — wrapped steps 4–6 in `try/except`; `except` block sends `result=3` + `ack_complete()` before re-raising, guaranteeing PLC always receives ack even on pipeline crash. **C3** capture-mode skip path — fixed `defect_type=0` → `defect_type_code=0` and `write_ack()` → `ack_complete()` (both were wrong field/method names causing `TypeError`/`AttributeError` crash). **C4** UV YOLO failure — all failure paths now return `UVResult(detection_failed=True)` (skip, uv_code=None) instead of `has_mixup=False` (Good); added `_consecutive_detection_failures` counter that fires `logger.error()` after 5 consecutive misses. **W3** UV NaN guard — added `& (g > 0)` to valid pixel mask; previously `log(0/b)=-inf` → NaN through polyfit → `max_dip=NaN` → silent Good. **W2** `write_output()` return checked at all 4 call sites — logs error on partial write but always sends ack (stopping ack would be worse). **W5** inter-cycle camera reconnect — `_run_inspection_cycle` now calls `cam.health_check()` + `cam.reconnect()` for any disconnected camera during the inter-cone gap (before `cycle_start`); previously a GigE disconnect returned Error=3 forever until manual restart. **W6** PLC reconnect backoff — replaced raw `connect()` calls with `_try_plc_reconnect()` which uses exponential backoff 2s→30s (doubles on failure, resets on success) to prevent Modbus TCP retry spam on flaky networks. |
| 2026-03-21 | Config + startup fixes (production readiness) | **start_cv.sh `--follow`**: fixed wrong log filename (`sieger-inspection-service.log` → `${INSPECTION_LOG}`) — `--follow` was tailing a file that doesn't exist. **config.json `uv_inspection`**: removed unused legacy `gb_ratio_threshold` key, added explicit `radial_dip_threshold: 0.024` (code defaulted to 0.024 but threshold was not visible in config). **Pending on ghcl reconnect**: pull `weights/` (visible_yolo.pt, uv_yolo.pt, yarn_tail_v3.pt) and correct patchcore model path in config.json (currently `models/patchcore`, actual name TBD from ghcl). |
| 2026-03-21 | UV: Replaced G/B ratio with radial log(G/B) dip detection | Full algorithm redesign based on dataset analysis (1950 good + 9 defect images). Polymer mixup creates concentric fluorescence bands → local dip in radial profile. New pipeline: annular region → per-pixel log(G/B) → 100 radial bins → degree-2 polynomial baseline → `max_dip = max negative deviation`. Decision: `has_mixup = radial_dip > 0.024`. log(G/B) domain chosen over raw G/B (gap=0.018 vs 0.013) and Green-only. Scope clarified: UV detects polymer mixup ONLY (chemistry), not appearance defects. `UVResult` fields: `radial_dip` (decision), `gb_ratio` (monitoring). Config key: `uv_inspection.radial_dip_threshold=0.024`, `outer_margin=0.10`. |

| Date | Change | Details |
|------|--------|---------|
| 2026-02-10 | Created `start_cv.sh` | Startup script for CV services. Teaching API on port 5002 (avoids conflict with login on 5000), Inspection on 5004. PID tracking, log capture, mock mode support. |
| 2026-02-10 | Image storage restructured | Images now save directly to `Master/raw/{material_id}/{camera}/` during capture instead of flat `Master/{camera}/raw/`. No deferred sorting needed on stop. |
| 2026-02-10 | Send raw VL frame to frontend | Stopped sending the annotated composite (with cropped panels) during inspection. Frontend now receives the raw `vl_frame` instead of `annotated_frame`. |
| 2026-02-10 | Fixed PLC register map everywhere | Created `plc.json` with authoritative register map from PLC team spec. Fixed `config.json`, `_read_plc_config()`, and CLAUDE.md. Registers: trigger=1(40002), sample_counter=0(40001), material_no=8(40009), result=2(40003), ack=19(40020), lights=4-6(40005-07). |
| 2026-02-10 | PLC IP updated to 192.168.2.61 | Was 192.168.2.1, corrected in config.json and plc.json. IPS machine is at 192.168.2.62 on same /20 subnet. |
| 2026-02-10 | Redefined `ips_status` register (40016) | Now has clear values: 1=Active (inspect/capture/teaching), 2=Trial run, 3=Disabled. Written on start_inspection (1) and stop_inspection (3). |
| 2026-02-10 | Added `defect_type` register (40020), moved `ack` to 40021 | defect_type codes: 0=Good, 1=Stain, 2=Wrong Pattern, 3=Wrong Cone Dia, 4=Wrong Tube Dia, 5=Missing Tail, 6=Thread Mixup. Updated plc.json, config.json, client.py, data_types.py, inspection_service.py, CLAUDE.md. |
| 2026-02-10 | Added trial mode + c2c_start 3 modes | Trial = inspect without writing results/ack to PLC. Triggered by PLC `c2c_start=2` (40008) or frontend `trial: true`. c2c_start: 0=disabled, 1=normal, 2=trial. IPS echoes ips_status: 1=Active, 2=Trial, 3=Disabled (40016). Data capture: no results but writes ack (PLC needs it for next trigger). Capture also reads material_id from PLC and saves to Master/raw/{material_id}/{camera}/. |
| 2026-02-10 | Trial mode PLC writes | In trial mode, result/defect_type/ack writes are suppressed but `ips_status` is always written (2=Trial on start, 3=Disabled on stop). PLC needs `ips_status` to populate material data. Capture cycle skips PLC trigger wait (cameras fire on hardware Line0). PLC registers are still READ passively for material_no folder naming. |
| 2026-02-10 | PLC auto-reconnect + material_no=0 guard | Added PLC reconnect at start of inspection/capture cycles if connection was lost. material_no=0 from PLC (empty basket) no longer overwrites frontend material_id — only non-zero values are used. |
| 2026-02-10 | master.json teaching integration | Frontend saves `master.json` in teaching folder with `master_name`, `cone_dia`, `tube_dia`. `/tube` endpoint reads it, uses `master_name` as .npz class name, saves dimensions + master_name to DB. During inspection, `visible.py` uses `specs.master_name` for tube pattern verification instead of PLC material_id. Added `master_name` column to `materials` table with migration. |
| 2026-02-10 | Enhanced cycle logging + --follow flag | Inspection/capture cycles now log clear `═══ START/DONE ═══` summaries with all PLC values, results, and check statuses. `start_cv.sh --follow` tails logs live on screen. |
| 2026-02-10 | `/tube` schema simplified | Removed `material_id`, `inner_diameter`, `inner_tolerance` from POST body. Now just `{folder: "path"}`. Reads `materialid` and `masterid` from `master.json` in the folder. |
| 2026-02-10 | Max inscribed circle for all diameter/radius | All bbox-to-diameter conversions changed from `width` or `max(w,h)` to `min(w,h)` — the max inscribed circle in the YOLO bbox. Applied across: `dimension_check.py`, `api/main.py`, `find_radius.py`, `unwarp.py`, `uv_inspection.py`. |
| 2026-02-10 | `/retrain_all` API endpoint | Scans `Master/visible/*/master.json`, calls shared `_teach_folder()` helper (same code as `/tube`), reloads all templates after completion. |
| 2026-02-10 | Annular masking for tube pattern | Tube crops use `extract_annular_roi(frame, tube_det, inner_ratio=0.80)` — donut mask that zeros out corners AND inner hole. Applied in both teaching (`tube_teacher.py`) and inspection (`visible.py`). `inner_ratio=0.80` (hole_dia/tube_outer_dia). |
| 2026-02-10 | Bilateral filter bleeding fix | Re-apply zero mask after bilateral filter in `preprocess_pipeline.py`: `filtered[~(bgr_image > 0).any(axis=2)] = 0`. Prevents color bleeding into black regions (hole/corners). |
| 2026-02-10 | Polar sweet spot 10% angle crop | `crop_sweet_spot.py` now crops 10% from top and bottom (angle direction) in addition to inner/outer radius crop. Removes edge artifacts from polar warp boundaries. |
| 2026-02-10 | Teaching saves intermediate images | Teaching saves debug crops: `crops/annular/` (donut-masked YOLO crop), `crops/circular/` (find_radius tight crop), `crops/polar/` (LAB polar strip as BGR), `crops/cone/` (raw cone bbox). Permission errors logged as warnings (non-fatal). |
| 2026-02-10 | Per-dimension tolerances | Separate `cone_tolerance_mm` and `tube_tolerance_mm` in DB + master.json (`conetol`, `tubetol`). Falls back to `tolerance_mm=2.0` if 0. Rejection only when measured dia outside spec ± tolerance. |
| 2026-02-10 | defect_type 7 = No Material ID | When material_id not found in DB (not taught), cone is ejected as Defect (code 2) with defect_type=7. Previously returned Error (code 3). Updated plc.json, data_types, inspection_service, visible.py. |
| 2026-02-10 | Trial mode: always write `ips_status` | Fixed bug where trial mode suppressed ALL PLC writes including `ips_status`. PLC gates material data on `ips_status` — without it, `material_no=0` always. Now: `start_inspection` always writes `ips_status` (2=Trial, 1=Active), `stop_inspection` always writes `ips_status=3` (Disabled). Only result/defect_type/ack writes are suppressed in trial mode. |
| 2026-02-10 | Added DEBUG logging to all pipeline modules | Comprehensive `logger.debug()` calls added to all 9 inspection pipeline modules + inspection_service. Covers: PLC trigger/write, camera capture, YOLO detection, dimension check, stain detection, tube pattern matching, tail detection, state transitions. Config `console_level` set to DEBUG. pyModbusTCP noise silenced in logging_config.py. |
| 2026-02-11 | Documentation cleanup | Fixed port 5000→5002 across all docs (chapters 00-09, appendix, config.json, api/main.py docstring). Rewrote project.md for V2.0 architecture. Updated plan.md (all modules DONE). Fixed PLC register map in chapter 02 (was V1 layout). Updated chapter 04 REST API (simplified /tube schema, added /retrain_all). Fixed chapter 08 camera/PLC config (was placeholder IPs). Fixed chapter 09 deployment (PLC is pyModbusTCP not snap7, added start_cv.sh). Added deprecation notice to pipeline.md (V1 design doc). Wrote proper README.md. Fixed wrong IPs in inspection.md, chapters 05/08. |
| 2026-02-11 | Stain detector anomalib 2.2.0 fix | Removed `metadata=` parameter from `TorchInferencer` and `OpenVINOInferencer` calls — not supported in anomalib 2.2.0 (only `path` and `device`). Added `os.environ["TRUST_REMOTE_CODE"] = "1"` before model loading. |
| 2026-02-11 | Template auto-reload after teaching | Added `reload_templates` Socket.IO event to inspection_service.py. After `/tube` teaching succeeds, main.py connects to inspection service (port 5004) via socketio client and emits `reload_templates`. Inspector reloads `.npz` files without restart. |
| 2026-02-11 | TRUST_REMOTE_CODE in start_cv.sh | Added `export TRUST_REMOTE_CODE=1` to start_cv.sh so both CV services inherit it. Required by anomalib PatchCore model loading (timm backbone). |
| 2026-02-11 | Polar unwarp for stain inference | PatchCore stain model was trained on polar-unwrapped texture strips but inference passed circular-masked crop. Added `src/inspection/polar_unwarp.py` (copied `find_geometry` + `unwarp_cone` from `training/patchcore/unwarp.py`). `visible.py` Step 4 now: YOLO crop → polar unwarp → radial crop → PatchCore. Requires both cone AND tube detections. Removed unused `cone_masked` / `extract_circular_roi` call. |
| 2026-02-11 | Material ID 6 conflict resolved | YELLOW_SOLID and ROSE_ZEEBRA both mapped to material_id=6. Deleted YELLOW_SOLID folder and .npz template. `/retrain_all` fixed DB to ROSE_ZEEBRA. Also removed orphaned TestCotton-150.npz template. |
| 2026-02-11 | Camera buffer count reduced | `BUFFER_COUNT` in `camera.py` changed from 10 to 1. SDK enforces minimum 3, so `max(min_required, 1) = 3` buffers allocated. Reduces stale frame window on startup. |
| 2026-02-11 | Removed all per-cycle camera flushes | Removed `flush_buffers()` calls from `_run_inspection_cycle()` and `_run_capture_cycle()`. FIFO buffer ordering matches PLC trigger ordering (both sequential, 1:1 gated by ack handshake). Flushing risked discarding the correct frame when PLC releases the next cone immediately after ack (rejection mode). First ~3 cycles after start may have stale frames (draining startup buffers). |
| 2026-02-11 | Tail inspection disabled | Set `tail_inspection: false` in config.json — tail YOLO model not reliable yet. |
| 2026-02-11 | Trigger timing analysis document | Created `docs/trigger_timing_analysis.md` with measured cycle timings: Trigger→Capture ~1.3s, Capture→Ack ~2.2s, Ack→NextTrigger ~3.0s, Total ~6.6s. Key insight: PLC trigger (register 40002) and camera proximity sensor (Line0) are independent systems — cannot determine cone physical position from register timing. |
| 2026-02-12 | Camera stop/start acquisition | Added `stop_acquisition()` and `start_acquisition()` methods to Camera class. `stop_acquisition()` stops SDK acquisition + flushes buffers with `DiscardAll`. `start_acquisition()` restarts. Called on stop_inspection/start_inspection respectively. Prevents stale frame accumulation during idle (like V1's full camera close/reopen but lighter). |
| 2026-02-12 | Switched to `capture_latest()` | All 3 cameras now use `capture_latest()` (drains stale frames, keeps freshest) instead of plain `capture()`. With cycle_start gating ensuring 1 cone in-flight, the latest frame is always the correct one. |
| 2026-02-12 | Added `cycle_start` register (40010) | New output register at address 9. Vision sets `cycle_start=1` at start of each inspection/capture cycle. PLC reads it, clears to 0, then triggers when ready. Prevents PLC from triggering before vision is ready. Solves N+1 frame shift problem together with stop/start acquisition and capture_latest. |
| 2026-02-12 | VL image overlay — 640x480 cone crop | Report image changed from 1280x720 full frame to 640x480 YOLO cone crop. Text overlay drawn on small image (readable): result label, per-check status (Stain/Pattern/ConeDia/TubeDia/Tail/Mixup), tube pattern color nearest + distance, pattern nearest + distance. Thumbnails saved to `Master/inspection-log/` at 640x480 with overlay. |
| 2026-02-13 | Per-camera timeout handling | `capture_part()` catches `TimeoutError` per camera independently — one camera timeout doesn't block others. Missing cameras return `None`, inspection pipeline skips them. Black frame with red "EMPTY" text sent to frontend for timed-out cameras. |
| 2026-02-13 | Per-camera configurable timeouts | Each camera uses its own timeout from config.json: VL=2000ms, UV=1600ms, Tail=1000ms. `capture_part()` no longer takes a shared timeout — uses `camera.timeout` per camera. |
| 2026-02-13 | Skip inspection for empty cameras | `vl_code`/`uv_code`/`tail_code` default to `None` (skipped) instead of `3` (Error). `PLCOutput.from_results()` skips `None` codes — missing cameras don't drag combined result to Error. Only if ALL cameras are None → Error(3). |
| 2026-02-13 | Removed all disk writes from inspection cycle | Removed 4 disk write sections: tube debug crop (`Master/tube-debug/`), tube debug text (`Master/tube/*.txt`), inspection log text (`Master/inspection-log/*.txt`), VL thumbnail (`Master/inspection-log/*.jpg`). No files written during inspection. |
| 2026-02-13 | Re-teaching overwrites DB, keeps old .npz | Teaching with existing material_id + new master_name: creates new `.npz`, updates DB via `upsert_material()`. Old `.npz` remains on disk (harmless — NN just has extra candidates). No cleanup needed now. |
| 2026-02-13 | Capture cycle PLC echo writes | Capture cycle now writes all PLC echo values (result=0, basket_no, material_no, loader_no, defect_type=0) via `write_output()` before ack, matching inspection cycle behavior. Frontend report also includes `basketid` from PLC data. |
| 2026-02-13 | Trigger issue documentation | Created `docs/trigger_issue.md` — comprehensive analysis of all trigger/sync issues, root causes, code changes, timeout history, multiple trigger analysis, and current defense layers. |
| 2026-02-13 | Frontend streaming review | Created `docs/frontend.md` — review of Socket.IO streaming, image rendering, counter/table logic. Found 5 issues: MIME type mismatch (JPEG as PNG), pie chart 1 cycle behind (stale ref), table resets on reload, start_status commented out, no connection indicator. |
| 2026-02-17 | Color-only tube pattern decision | Removed OR logic (Color OR ResNet). Color NN is now the sole decision maker — production data showed 99.85% vs ResNet's 95.05%. ResNet still runs for monitoring/logging but does NOT affect pass/fail. Added `max_bhatt_distance=0.35` distance gate to reject untaught patterns. Changed `TubePatternResult.passed` from `color_match or resnet_match` to `color_match` only. Config: `tube_pattern.max_bhatt_distance`. |
| 2026-02-17 | FFT spatial features for tube pattern | Added 1D FFT magnitude as shift-invariant spatial feature. Combined distance: `(1-fft_weight) * bhatt + fft_weight * fft_cosine` (fft_weight=0.3). FFT discriminates same-color patterns (VIOLET_TRIANGLE vs VIOLET_CHECKED: bhatt=0.190, fft=0.299). Ring linearized via `warpPolar()` → clean strip → mean intensity → FFT magnitude → 64 coefficients. Perfectly shift-invariant (rotation-proof). Teaching saves `fft_feats`/`fft_mean_feat` in .npz. Backward compatible: old .npz without FFT falls back to color-only. Config: `tube_pattern.fft_weight`. |
| 2026-02-25 | Decoupled material_id from master_name — SQLite → JSON recipes | Replaced SQLite `materials.db` with JSON recipe files in `data/recipes/{material_id}.json`. Created `RecipeStore` (drop-in for `MaterialDatabase`, same `get_material_specs()` interface). Teaching now writes only `.npz` files (no DB). Recipes are user-created mappings: PLC number → master_name + dimensions + tolerances. Added REST endpoints: `GET/POST/DELETE /recipes`, `GET /masters`. `TubeTeacher` no longer depends on DB — `list_references()`, `get_reference_info()`, `delete_reference()` use filesystem (.npz glob). Automatic one-time migration in `init_app.py`: reads SQLite → writes JSON → renames `.db.migrated`. 7 recipes migrated (materials 2,3,4,5,6,9,10). `inspection_service.py` unchanged (same `get_material_specs()` interface). |
| 2026-02-25 | Capture filter by material ID list | `start_inspection` now accepts `material_id` as a list (e.g. `[2, 5]`) for capture mode. Added `capture_material_ids: set` to `ServiceState`. During capture, only cones whose PLC `material_no` is in the set get images saved to disk. Other cones still complete the full PLC handshake (trigger/clear/capture/ack) and stream to UI — conveyor keeps moving. Empty set = save all (backward compat). String `material_id` still works as before. Cycle DONE log shows `saved` or `SKIPPED` status. |
| 2026-02-25 | Trigger debounce + trigger counter + stream statistics | Added hardware trigger debounce (`trigger_debounce_us` config, default 200ms) — FPGA-level filter for sensor bounce. Added Counter0 hardware trigger counter on Line0 RisingEdge — compares triggers vs delivered frames. Added `get_stream_statistics()` / `log_stream_statistics()` reading SDK DataStream counters (delivered, dropped, lost, incomplete, underrun). Stats logged on `stop_inspection`. Per-camera buffer flush added before each capture in `capture.py`. |
| 2026-02-25 | Result label + check label fixes | Fixed `result_code=3` showing "DEFECT" → now shows "ERROR". Fixed all check labels (Stain, Pattern, ConeDia, TubeDia, Tail, Mixup) showing green "OK" when no VL inspection ran → now show red "ERROR". |
| 2026-02-25 | Camera diagnostics documentation | Updated `documentation/camera_acquisition.md` with 5 new sections: 3.8 Trigger Debounce, 3.9 Hardware Trigger Counter, 3.10 Stream Statistics, 3.11 Buffer Flushing Strategy, updated Error Matrix and Config Schema with actual production values. |
| 2026-02-28 | UV PatchCore model trained + deployed | Fixed anomalib 2.2.0 + pandas 3.0 compatibility (str Enum comparison broken, .loc[] on new columns rejected). Monkey-patch in `training/patchcore_uv/train.py`. Trained wide_resnet50_2 PatchCore on 188 train / 47 test good images. Model deployed to `models/patchcore_uv/weights/torch/model.pt` (208MB). |
| 2026-02-28 | UV: Replaced PatchCore with row variance | PatchCore failed on UV data — good images scored 0.31–1.00 (mean 0.60), no usable threshold. Root causes: model squashes 720x256→256x256, UV images inherently dim (mean 18-30), CLAHE amplifies noise differently across images. Replaced with detrended row-mean std: measures fluorescent band intensity variation in unwrapped strip. Normal: 1.7–7.6, Defect: 26.8–33.2, threshold=15.0 gives 100% separation (>3x gap). Also cleaned 18 black frames + 6 outliers from Master/raw dataset (199→175 images). Removed anomalib dependency from UV. Updated `UVResult` dataclass (removed `anomaly_score`, `model_loaded`, `heatmap`; added `detrend_std`, `row_cv`). Config: `inspection.uv_inspection.detrend_std_threshold=15.0`, `row_cv_threshold=0.15`. |
| 2026-03-06 | Hybrid capture+inspection mode | **Problem:** Capture mode wrote `result_code=0` for ALL cones including non-captured materials. No real result given → PLC doesn't sort/unload → conveyor chokes. **Solution:** `_run_capture_cycle()` now splits into two paths after camera capture. **Path A** (material in capture list): save raw images + write `result=0` (cone stays on conveyor). **Path B** (material NOT in capture list): run full inspection via `_inspect_and_report()` helper + write real result (1/2/3) to PLC. Extracted inspection steps 4–7 from `_run_inspection_cycle()` into reusable `_inspect_and_report()` method (VL/UV/Tail pipelines, combined result, PLC write, annotated report, UI streaming). Both `_run_inspection_cycle()` and `_run_capture_cycle()` (Path B) call this helper. Empty `capture_material_ids` = save all (Path A for everything, backward compatible). `result_code=0` means "do not unload" — intentionally used for captured cones. |
| 2026-03-06 | `start_cv.sh` — production/development port modes | Added `--production` (default) and `--development`/`--dev` flags. Ports are now read from `service_config.json` via `jq` instead of being hardcoded. Production uses deployment ports (API=5002, Inspection=5004), development uses dev ports (API=6002, Inspection=6004). Dev mode uses separate PID files (`api-dev.pid`, `inspection-dev.pid`) and log files (`api-dev.log`, `inspection-dev.log`) — safe to run alongside production. `--stop` and `--status` are environment-aware: `--development --stop` only stops dev services. All flags are combinable (e.g. `--development --mock --follow`). Requires `jq` for JSON parsing. |
| 2026-03-06 | Stain detection: circular cone crop replaces polar unwrap | **Problem:** Polar unwrapping lost edge regions (5-15% radial crop). Foreign materials, threads, and labels at edges were either cropped away or caused artifacts. With only 20 training images, PatchCore on unwrapped strips was unreliable. **Solution:** PatchCore now runs on the rectangular YOLO cone crop directly (no unwrapping). An annular mask (donut) is applied post-inference to the anomaly heatmap — only yarn surface pixels are scored (black corners and tube hole ignored). `anomaly_score = max(heatmap[mask > 0])`. PatchCore naturally learns black background as "normal" (consistent across all training images), so no pre-masking needed. **Any deviation from normal yarn texture is flagged**: stains, dirt, foreign materials, threads, labels. `stain_detector.py`: `detect()` now accepts optional `center`, `inner_r`, `outer_r` for annular masking. `visible.py`: Step 4 passes cone crop + geometry directly (no `unwarp_cone` call). `prepare_dataset.py`: saves rectangular cone crops instead of unwrapped strips. Requires PatchCore retraining on circular cone crops. |
