# Chapter 10: REST API

## 10.1 Overview

The FastAPI backend runs on port 5002 and provides REST endpoints for teaching, recipe management, inspection, health checks, capture sessions, analytics, configuration, and authentication.

**Source:** `src/api/main.py`

**Framework:** FastAPI + Uvicorn

**Base URL:** `http://192.168.1.x:5002`

## 10.2 Teaching Endpoints

### POST /teaching/tube

Manually trigger tube pattern teaching for a material.

```json
// Request
{"material_id": "42"}

// Response
{"status": "started", "message": "Tube teaching started. System will auto-teach after 20 captures."}
```

### POST /teaching/tube/extend

Extend existing tube template with additional samples (capped at 3 extends).

```json
// Request
{"material_id": "42", "additional_samples": 20}

// Response
{"status": "extended", "material_id": "42", "total_samples": 40}
```

### POST /teaching/tube/capture/start

Start manual tube capture mode.

```json
// Request
{"material_id": "42"}
// Response
{"status": "capturing", "session_id": "uuid", "material_id": "42"}
```

### POST /teaching/tube/capture/stop

Stop capture and trigger teaching.

### POST /teaching/stain

Reload PatchCore model after cloud training.

```json
{"model_path": "models/patchcore_v2/"}
```

### POST /teaching/dimension

Set global dimension tolerances and calibration.

```json
{
    "cone_diameter_mm": 230.0,
    "cone_tolerance_mm": 5.0,
    "tube_diameter_mm": 42.0,
    "tube_tolerance_mm": 2.0,
    "pixels_per_mm": 1.45
}
```

### POST /teaching/uv

Set UV radial dip threshold.

```json
{"radial_dip_threshold": 0.024}
```

### POST /teaching/tail

Trigger YOLO tail detector retraining.

```json
{"session_id": "abc123"}
```

### POST /teaching/annotate

Annotate captured images with ground truth labels.

### GET /teaching/alerts

Get pending autonomous teaching alerts (e.g., auto-teach progress).

## 10.3 Recipe Management

### POST /recipes

Create or update a material recipe. Accepts multiple field aliases for frontend compatibility:

```json
{
    "material_id": "42",
    "master_name": "BLUE_DIAMOND",
    "cone_diameter_mm": 60.0,
    "tube_diameter_mm": 32.0,
    "cone_tolerance_mm": 2.0,
    "tube_tolerance_mm": 1.5
}
```

Aliases: `materialid`/`id`, `masterid`/`master`, `conedia`, `tubedia`, `conetol`, `tubetol`.

### GET /recipes

List all recipes (JSON files from `data/recipes/`).

### DELETE /recipes/{material_id}

Delete a recipe.

### GET /masters

List all taught tube masters (`.npz` files with material_id and threshold).

## 10.4 Capture Session Endpoints

> **Note:** Data capture is controlled by the teach toggle in config.json (`PUT /config/teach`). See docs/frontend_v3_guide.md for the teach workflow.

### GET /capture/status

Current active session info: `active`, `session_id`, `module`, `sample_count`, `started_at`.

### GET /capture/sessions

List past capture sessions. Filter by `module`, `limit` (default 50).

### GET /capture/images

List images in a session. Requires `session_id` query parameter.

## 10.5 Inspection Endpoints

### POST /inspect

Run inspection on a single image (for testing).

```json
{
    "material_id": "42",
    "image_path": "/path/to/image.jpg"
    // or "image_base64": "base64..."
}
```

Response includes: `result_code`, `passed`, `dimensions_ok`, `stain_detected`, `tube_pattern_ok`, measured diameters, `annotated_image_base64`.

## 10.6 Results & Audit

### GET /results

List inspection results with filters.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 50 | Max results |
| `offset` | int | 0 | Pagination offset |
| `material_id` | str | — | Filter by material |
| `result` | int | — | 1=Good, 2=Defect, 3=Error |
| `from_dt` | str | — | ISO-8601 start date |
| `to_dt` | str | — | ISO-8601 end date |

### GET /results/{id}

Single inspection result by ID.

### GET /results/{id}/audit

Audit JPEG image (`image/jpeg` response) with YOLO bboxes, per-module results, timestamp annotations.

### GET /alerts

Recent inspection alerts. Filter by `limit` (default 20), `unread_only`.

Alert types: `defect`, `error`, `camera_fault`.

## 10.7 Analytics Endpoints

### GET /analytics

Without query params: returns live in-memory shift snapshot.
With `from_ts`/`to_ts`: queries SQLite for historical aggregate.

Response:
```json
{
    "shift": {
        "start": "2026-04-01T06:00:00",
        "total": 450, "good": 420, "defect": 25, "error": 5,
        "rejection_rate_pct": 5.6
    },
    "defect_breakdown": {"stain": 10, "tube_mismatch": 8, "uv_mixup": 2, "tail": 3, "dimension": 2},
    "per_material": {
        "42": {"total": 200, "good": 195, "defect": 5, "defect_types": {"stain": 3, "tube_mismatch": 2}}
    },
    "session_total": 1200, "session_good": 1150, "session_defect": 40
}
```

### GET /analytics/hourly

Hourly Good/Defect/Error counts.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `date` | str | today | Date in YYYY-MM-DD format |
| `material_id` | str | — | Filter by material |

```json
// Response
{
    "date": "2026-04-01",
    "hours": [
        {"hour": 6, "good": 45, "defect": 3, "error": 0},
        {"hour": 7, "good": 52, "defect": 1, "error": 1}
    ]
}
```

### POST /analytics/reset

Reset in-memory shift counters manually.

## 10.8 Settings Endpoints

> **Note:** Operator settings (shift_hours, inspection tasks, teach toggles) are now managed via config.json write API — see `PUT /config/*` endpoints below.

## 10.9 Health Endpoints

### GET /health

Quick check: `{status: "ok", plc: connected, cameras: all_ok, inspection_running: bool}`.

### GET /health/system

Detailed: CPU%, memory (used/total GB), GPU (util%, memory), disk free, uptime.

Status: `healthy` / `degraded` / `unhealthy`.

### GET /health/plc

PLC connection: `connected`, `host`, `port`, `last_read_at`, register values.

### GET /health/cameras

All 3 cameras: `name`, `connected`, `serial`, `last_frame_ms`.

### GET /health/camera/{name}

Single camera: `name`, `connected`, `serial`, `exposure_ms`, `last_frame_at`, `consecutive_failures`.

## 10.10 System Endpoints

### GET /config

Read-only — returns full `config.json` contents.

### PUT /config/tasks

Toggle inspection modules on/off. No restart required.

```json
// Request
{"uv_inspection": false}

// Response
{"status": "ok", "updated": {"uv_inspection": false}}
```

### PUT /config/teach

Toggle teach mode per module. No restart required.

Valid keys: `stain_detection`, `uv_inspection`, `tail_inspection`, `dimension_check`.

```json
// Request
{"stain_detection": true}

// Response
{"status": "ok", "updated": {"stain_detection": true}}
```

### PUT /config/shift

Update shift hours. No restart required.

```json
// Request
{"shift_hours": 12.0}

// Response
{"status": "ok", "updated": {"shift_hours": 12.0}}
```

### PUT /config/cameras

Update camera settings (ip, serial, exposure, timeout, trigger_debounce_us per camera). **Restart required.**

```json
// Request
{
    "top_camera": {"ip": "192.168.1.160", "serial": "ABC123", "exposure": 5000, "timeout": 3000, "trigger_debounce_us": 1000}
}

// Response
{"status": "ok", "restart_required": true}
```

### PUT /config/plc

Update PLC settings (host, port, unit_id, timeout, poll_interval, registers with input/output/light groups). **Restart required.**

```json
// Request
{
    "host": "192.168.1.110",
    "port": 502,
    "unit_id": 1,
    "timeout": 2.0,
    "poll_interval": 0.05,
    "registers": {
        "input": {"cone_present": 100},
        "output": {"eject": 200},
        "light": {"top_light": 300}
    }
}

// Response
{"status": "ok", "restart_required": true}
```

### GET /status

Operational status: `mode`, `inspection_running`, `active_material_id`, `cones_today`, `defects_today`, `uptime_hours`.

### POST /restart

Restart inspection service (background task).

### POST /shutdown

Gracefully shutdown service.

## 10.11 Cloud Upload

### POST /cloud/upload

Upload capture session to Azure Blob Storage.

```json
{"session_id": "abc123", "module": "stain"}
```

Uploads `metadata.json` + `images/{filename}` to blob prefix `{customer_id}/{module}/{session_id}/`.

## 10.12 Tube Teaching API (Port 8001)

A separate lightweight teaching API runs on port 8001 (`src/teaching/api.py`), providing dedicated tube teaching endpoints:

### POST /teach/tube

Upload images + material_id via multipart form. Returns template info including color_threshold.

### GET /teach/tube

List all taught materials.

### GET /teach/tube/{material_id}

Get metadata for a specific material reference.

### POST /teach/tube/{material_id}/extend

Append new samples to existing template (max 3 extends).

### DELETE /teach/tube/{material_id}

Delete a material reference.

## 10.13 Authentication Endpoints

### POST /auth/login

Authenticate user and receive JWT token.

```json
// Request
{"username": "operator1", "password": "secret"}

// Response
{
    "token": "eyJ...",
    "username": "operator1",
    "role": "operator",
    "services": ["inspection", "teaching"],
    "expires_at": "2026-04-02T06:00:00Z"
}
```

### POST /auth/logout

Invalidate current session token.

Header: `Authorization: Bearer <token>`

### GET /auth/me

Returns current authenticated user info (username, role, services).

### GET /auth/users

List all users. **Requires superAdmin role.**

### POST /auth/users

Create a new user. **Requires superAdmin role.**

```json
// Request
{"username": "new_operator", "password": "initial_pass", "role": "operator", "services": ["inspection"]}
```

### PUT /auth/users/{username}

Update user role or services. **Requires superAdmin role.**

```json
// Request
{"role": "engineer", "services": ["inspection", "teaching", "config"]}
```

### DELETE /auth/users/{username}

Delete a user. **Requires superAdmin role.**

### POST /auth/users/{username}/reset-password

Reset a user's password. **Requires superAdmin role.**

```json
// Request
{"new_password": "reset_pass"}
```

### GET /auth/activity

Activity log with login/logout/action events. **Requires superAdmin or engineer role.**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 100 | Max entries |
| `username` | str | — | Filter by user |
| `from_dt` | str | — | ISO-8601 start date |
| `to_dt` | str | — | ISO-8601 end date |

## 10.14 Request Tracing

All requests include a correlation ID (`X-Correlation-ID` header) for tracing through logs. If not provided by the client, a UUID is generated.

## 10.15 Pydantic Models

Request/response models are defined in `src/api/models.py`. Key models:

| Model | Purpose |
|-------|---------|
| `TubeTeachRequest/Response` | Tube teaching with image folders |
| `StainDetectRequest/Response` | Stain teaching (teach mode) or detection (detect mode) |
| `RecipeRequest/Response` | Recipe CRUD with field aliases |
| `InspectRequest/Response` | Single-image inspection |
| `SystemHealthResponse` | Full system health with PLC, cameras, models |
| `InspectionRecord` | Database row for inspection results |
