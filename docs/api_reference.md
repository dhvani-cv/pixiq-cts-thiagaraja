# API Reference — Sieger GHCL Yarn Cone Inspection v3.0.0

> **FastAPI backend** runs on port **5002**.
> **Socket.io inspection service** runs on port **5004**.
> Last updated: 2026-04-01

---

## Base URL

```
http://192.168.1.x:5002
```

All endpoints return `application/json`. All request bodies are `application/json` unless noted.

---

## Teaching Endpoints

### POST /teaching/tube

Manually trigger tube pattern teaching for a specific material. Use when autonomous teaching needs to be force-retriggered or when extending an existing template.

**Request body:**
```json
{
  "material_id": "42"
}
```

**Response:**
```json
{
  "status": "started",
  "material_id": "42",
  "message": "Tube teaching started. System will auto-teach after 20 captures."
}
```

---

### POST /teaching/tube/extend

Extend an existing tube template with more samples. Useful when a template has poor coverage (high false positive rate).

**Request body:**
```json
{
  "material_id": "42",
  "additional_samples": 20
}
```

**Response:**
```json
{
  "status": "extended",
  "material_id": "42",
  "total_samples": 40
}
```

---

### POST /teaching/tube/capture/start

Start manual tube capture mode (without autonomous trigger).

**Request body:**
```json
{
  "material_id": "42"
}
```

**Response:**
```json
{
  "status": "capturing",
  "session_id": "abc123",
  "material_id": "42"
}
```

---

### POST /teaching/tube/capture/stop

Stop manual tube capture and trigger teaching.

**Request body:** (empty `{}`)

**Response:**
```json
{
  "status": "teaching_complete",
  "material_id": "42",
  "samples_used": 25,
  "threshold": 0.312
}
```

---

### POST /teaching/stain

Trigger stain (PatchCore) model reload after cloud training is complete and new model has been downloaded.

**Request body:**
```json
{
  "model_path": "models/patchcore_v2/"
}
```

**Response:**
```json
{
  "status": "loaded",
  "model_path": "models/patchcore_v2/",
  "message": "PatchCore model reloaded successfully."
}
```

---

### POST /teaching/dimension

Set global dimension tolerances and calibration factor. Applied immediately — no restart required.

**Request body:**
```json
{
  "cone_diameter_mm": 230.0,
  "cone_tolerance_mm": 5.0,
  "tube_diameter_mm": 42.0,
  "tube_tolerance_mm": 2.0,
  "pixels_per_mm": 1.45
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cone_diameter_mm` | float | yes | Nominal cone outer diameter in mm |
| `cone_tolerance_mm` | float | yes | Allowed deviation ± in mm |
| `tube_diameter_mm` | float | yes | Nominal tube diameter in mm |
| `tube_tolerance_mm` | float | yes | Allowed deviation ± in mm |
| `pixels_per_mm` | float | yes | Calibration factor from calibration board |

**Response:**
```json
{
  "status": "saved",
  "pixels_per_mm": 1.45,
  "cone_diameter_mm": 230.0,
  "tube_diameter_mm": 42.0
}
```

---

### POST /teaching/uv

Set or recalibrate UV radial dip threshold. Use after UV camera replacement.

**Request body:**
```json
{
  "radial_dip_threshold": 0.024
}
```

**Response:**
```json
{
  "status": "saved",
  "radial_dip_threshold": 0.024
}
```

---

### POST /teaching/tail

Trigger YOLO tail detector retraining using captured tail images.

**Request body:**
```json
{
  "session_id": "abc123"
}
```

**Response:**
```json
{
  "status": "training_started",
  "session_id": "abc123",
  "sample_count": 150
}
```

---

### GET /teaching/alerts

Get pending autonomous teaching alerts (tube teaching progress, completion events).

**Response:**
```json
{
  "alerts": [
    {
      "material_id": "42",
      "type": "tube_teaching_progress",
      "captured": 15,
      "required": 20,
      "message": "Captured 15/20 for material 42"
    },
    {
      "material_id": "43",
      "type": "tube_teaching_complete",
      "threshold": 0.287,
      "message": "Template 43.npz created. Inspecting now."
    }
  ]
}
```

---

### POST /teaching/annotate

Annotate a previously captured image (for tail YOLO training ground truth).

**Request body:**
```json
{
  "image_id": "abc123_0042.jpg",
  "labels": [
    {"class": "yarn_tail", "x": 0.5, "y": 0.3, "w": 0.2, "h": 0.4}
  ]
}
```

**Response:**
```json
{
  "status": "saved",
  "image_id": "abc123_0042.jpg"
}
```

---

## Capture Endpoints

> **Note:** `POST /capture/start` and `POST /capture/stop` have been removed. Use `PUT /config/teach` to toggle teach mode for individual modules instead.

### GET /capture/status

Get status of the current active capture session.

**Response:**
```json
{
  "active": true,
  "session_id": "abc123",
  "module": "stain",
  "sample_count": 87,
  "started_at": "2026-03-25T10:00:00Z"
}
```

---

### GET /capture/sessions

List all past capture sessions.

**Query params:** `module` (optional filter), `limit` (default 50)

**Response:**
```json
{
  "sessions": [
    {
      "session_id": "abc123",
      "module": "stain",
      "material_id": "42",
      "started_at": "2026-03-25T10:00:00Z",
      "stopped_at": "2026-03-25T10:45:00Z",
      "sample_count": 247,
      "status": "complete"
    }
  ]
}
```

---

### GET /capture/images

List images in a capture session.

**Query params:** `session_id` (required)

**Response:**
```json
{
  "session_id": "abc123",
  "images": [
    "sieger_data/captures/stain/abc123/0001.jpg",
    "sieger_data/captures/stain/abc123/0002.jpg"
  ],
  "count": 247
}
```

---

## Results Endpoints

### GET /results

Get recent inspection results.

**Query params:**
| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 50 | Max results to return |
| `offset` | 0 | Pagination offset |
| `material_id` | — | Filter by material |
| `result` | — | Filter by result code (1/2/3) |
| `from_dt` | — | ISO-8601 datetime filter (start) |
| `to_dt` | — | ISO-8601 datetime filter (end) |

**Response:**
```json
{
  "total": 1024,
  "results": [
    {
      "id": 1024,
      "timestamp": "2026-03-25T14:32:01Z",
      "material_id": "42",
      "basket_no": 7,
      "loader_id": 3,
      "result": 1,
      "defect_type": 0,
      "tube_result": 1,
      "stain_result": 1,
      "uv_result": 1,
      "tail_result": 1,
      "dimension_result": 1,
      "stain_score": 0.12,
      "tube_distance": 0.24,
      "radial_dip": 0.008,
      "cone_diameter_mm": 229.5,
      "tube_diameter_mm": 41.8
    }
  ]
}
```

---

### GET /results/{id}

Get a single inspection result by ID.

**Path param:** `id` — integer inspection row ID

**Response:** Single result object (same schema as items in `/results`)

---

### GET /results/{id}/audit

Get the audit JPEG image for an inspection.

**Response:** `image/jpeg` binary — annotated frame with YOLO bboxes, per-module results, timestamp.

---

### GET /alerts

Get recent inspection alerts (defects, errors, camera faults).

**Query params:** `limit` (default 20), `unread_only` (bool)

**Response:**
```json
{
  "alerts": [
    {
      "id": 5,
      "timestamp": "2026-03-25T14:31:55Z",
      "type": "defect",
      "material_id": "42",
      "defect_type": 1,
      "defect_name": "Stain",
      "inspection_id": 1023
    }
  ]
}
```

---

## Health Endpoints

### GET /health

Overall system health (quick check).

**Response:**
```json
{
  "status": "ok",
  "plc": "connected",
  "cameras": "all_ok",
  "inspection_running": true
}
```

---

### GET /health/system

Detailed system resource usage.

**Response:**
```json
{
  "cpu_percent": 12.4,
  "memory_used_gb": 4.2,
  "memory_total_gb": 32.0,
  "gpu_util_percent": 35,
  "gpu_memory_used_gb": 2.1,
  "disk_free_gb": 180.0,
  "uptime_hours": 72.3
}
```

---

### GET /health/plc

PLC connection status and last-read register values.

**Response:**
```json
{
  "connected": true,
  "host": "192.168.1.110",
  "port": 502,
  "last_read_at": "2026-03-25T14:32:00Z",
  "registers": {
    "trigger": 0,
    "material_no": 42,
    "basket_no": 7,
    "c2c_start": 1,
    "ips_status": 1
  }
}
```

---

### GET /health/cameras

Status of all 3 cameras.

**Response:**
```json
{
  "vl": {"connected": true, "serial": "4108843636", "last_frame_ms": 45},
  "uv": {"connected": true, "serial": "4110025657", "last_frame_ms": 45},
  "tail": {"connected": true, "serial": "4108843650", "last_frame_ms": 45}
}
```

---

### GET /health/camera/{name}

Status of a single camera. `name` is `vl`, `uv`, or `tail`.

**Response:**
```json
{
  "name": "vl",
  "connected": true,
  "serial": "4108843636",
  "exposure_ms": 11,
  "last_frame_at": "2026-03-25T14:32:00Z",
  "consecutive_failures": 0
}
```

---

## System Endpoints

### GET /config

Get current active configuration (read-only).

**Response:** Full `config.json` contents as JSON object.

> Note: There is no PUT /config for the full config. Use the granular `PUT /config/*` endpoints below.

---

### PUT /config/tasks

Toggle inspection modules on or off. No restart required.

**Request body:**
```json
{
  "uv_inspection": false
}
```

**Response:**
```json
{ "ok": true, "updated": { "uv_inspection": false } }
```

---

### PUT /config/teach

Toggle teach mode for individual modules. No restart required.

**Request body:**
```json
{
  "stain_detection": true
}
```

**Response:**
```json
{ "ok": true, "updated": { "stain_detection": true } }
```

---

### PUT /config/shift

Update shift hours. No restart required.

**Request body:**
```json
{
  "shift_hours": 12.0
}
```

**Response:**
```json
{ "ok": true, "updated": { "shift_hours": 12.0 } }
```

---

### PUT /config/cameras

Update camera configuration. **Restart required** — the service will prompt for restart.

**Request body:** Camera config object (serial numbers, exposure settings, etc.)

**Response:**
```json
{ "ok": true, "restart_required": true }
```

---

### PUT /config/plc

Update PLC configuration including register mappings. **Restart required** — the service will prompt for restart.

**Request body:** PLC config object (host, port, register map, etc.)

**Response:**
```json
{ "ok": true, "restart_required": true }
```

---

### POST /restart

Restart the inspection service. Use after config.json changes.

**Request body:** (empty `{}`)

**Response:**
```json
{
  "status": "restarting",
  "message": "Service will restart in 2 seconds."
}
```

---

### POST /shutdown

Gracefully shut down the inspection service.

**Request body:** (empty `{}`)

**Response:**
```json
{
  "status": "shutting_down"
}
```

---

### GET /status

Get current operational status of the inspection service.

**Response:**
```json
{
  "mode": "inspection",
  "inspection_running": true,
  "active_material_id": "42",
  "cones_today": 1847,
  "defects_today": 12,
  "uptime_hours": 72.3
}
```

---

## Cloud Endpoints

### POST /cloud/upload

Upload captured session crops to Azure Blob Storage for cloud training.

**Request body:**
```json
{
  "session_id": "abc123",
  "module": "stain"
}
```

**Response:**
```json
{
  "status": "uploaded",
  "session_id": "abc123",
  "module": "stain",
  "files_uploaded": 247,
  "blob_prefix": "stain/abc123/"
}
```

---

## Legacy Endpoints

These endpoints exist for backwards compatibility. Prefer the v3 endpoints above.

| Method | Path | Notes |
|--------|------|-------|
| POST | /tube | Legacy tube teaching trigger |
| POST | /stain | Legacy stain check (single image) |
| POST | /extract | Legacy crop extraction |
| POST | /color_detection | Legacy color check |
| POST | /delete_master | Delete a material template |
| GET | /recipes | List all material recipes |
| POST | /recipes | Create recipe |
| DELETE | /recipes/{material_id} | Delete recipe |
| GET | /masters | List all .npz templates |
| POST | /inspect | Legacy single-cone inspection |

---

## Socket.io Events (Port 5004)

WebSocket endpoint: `ws://192.168.1.x:5004`

### Events: Client → Server

#### `start_inspection`

Start the inspection loop (begin polling PLC trigger).

**Payload:** `{}` or `{ "trial": true }` for trial mode (no PLC writes).

---

#### `stop_inspection`

Stop the inspection loop.

**Payload:** `{}`

---

#### `connect_cam`

Request live camera frames (for HMI preview).

**Payload:**
```json
{ "camera": "vl" }
```
`camera` is one of `vl`, `uv`, `tail`.

---

#### `disconnect_cam`

Stop live camera frame stream.

**Payload:**
```json
{ "camera": "vl" }
```

---

### Events: Server → Client

#### `frame`

Emitted after each inspection cycle (or periodically during live preview).

**Payload:**
```json
{
  "camera": "vl",
  "image": "<base64-encoded JPEG>",
  "result": 1,
  "defect_type": 0,
  "material_id": "42",
  "timestamp": "2026-03-25T14:32:01Z",
  "modules": {
    "tube": {"result": 1, "distance": 0.24, "threshold": 0.31},
    "stain": {"result": 1, "score": 0.12},
    "uv": {"result": 1, "radial_dip": 0.008},
    "tail": {"result": 1},
    "dimension": {
      "result": 1,
      "cone_diameter_mm": 229.5,
      "tube_diameter_mm": 41.8
    }
  }
}
```

---

#### `teaching_alert`

Emitted during autonomous tube teaching to report progress or completion.

**Payload (progress):**
```json
{
  "type": "tube_teaching_progress",
  "material_id": "42",
  "captured": 15,
  "required": 20,
  "message": "Captured 15/20 for material 42"
}
```

**Payload (complete):**
```json
{
  "type": "tube_teaching_complete",
  "material_id": "42",
  "threshold": 0.287,
  "message": "Template 42.npz created. Now inspecting material 42."
}
```

---

## Result and Defect Type Codes

### Result Codes (register 40003)

| Code | Meaning |
|------|---------|
| 0 | Teaching cone — no pass/fail verdict |
| 1 | Good — all checks passed |
| 2 | Defect — one or more checks failed |
| 3 | Error — pipeline exception |

### Defect Type Codes (register 40020)

| Code | Meaning |
|------|---------|
| 0 | Good / No defect |
| 1 | Stain |
| 2 | Wrong pattern (tube) |
| 3 | Wrong cone diameter |
| 4 | Wrong tube diameter |
| 5 | Missing tail |
| 6 | Thread mixup (UV) |

---

*Last updated: 2026-04-01*

---

> **Note:** `GET /settings` and `PUT /settings` have been removed. Use the `PUT /config/*` endpoints below instead.

---

## Analytics Endpoints

### GET /analytics

Returns aggregated inspection analytics.

**Without query params:** returns the live in-memory shift snapshot — same data pushed via socket.io `send_image` event on every cone. Use this on page mount to initialize the analytics dashboard.

**With `from_ts` / `to_ts`:** queries SQLite for historical aggregate. Use this for the Results page date-range summary.

**Query params:**

| Param | Type | Description |
|-------|------|-------------|
| `from_ts` | string (ISO-8601) | Start of range e.g. `2026-03-26T06:00:00Z` |
| `to_ts` | string (ISO-8601) | End of range |
| `material_id` | string | Filter by material (optional) |

**Response:**
```json
{
  "shift": {
    "start": "2026-03-26T06:00:00Z",
    "total": 312,
    "good": 298,
    "defect": 14,
    "error": 0,
    "rejection_rate_pct": 4.5
  },
  "defect_breakdown": {
    "stain": 6,
    "tube_mismatch": 4,
    "uv_mixup": 2,
    "tail": 1,
    "dimension": 1
  },
  "per_material": {
    "42": { "total": 120, "good": 115, "defect": 5, "defect_types": { "stain": 3, "tube_mismatch": 2 } },
    "7":  { "total": 192, "good": 183, "defect": 9, "defect_types": { "stain": 4, "uv_mixup": 3, "tail": 2 } }
  },
  "session_total": 312,
  "session_good": 298,
  "session_defect": 14
}
```

---

### POST /analytics/reset

Reset in-memory shift counters manually. Use at the start of a new shift if the service did not auto-reset.

**Response:**
```json
{ "ok": true, "message": "Shift counters reset" }
```

---

### GET /analytics/hourly?date=YYYY-MM-DD

Returns hourly breakdown of inspection counts for a given date.

**Query params:**

| Param | Type | Description |
|-------|------|-------------|
| `date` | string | Date in `YYYY-MM-DD` format |

**Response:**
```json
{
  "date": "2026-03-26",
  "hours": [
    { "hour": 6, "total": 45, "good": 42, "defect": 3, "error": 0 },
    { "hour": 7, "total": 52, "good": 50, "defect": 2, "error": 0 }
  ]
}
```

---

## Auth Endpoints

### POST /auth/login

Authenticate and receive a session token.

**Request body:**
```json
{
  "username": "operator1",
  "password": "changeme"
}
```

**Response:**
```json
{ "ok": true, "token": "eyJ...", "username": "operator1", "role": "operator" }
```

---

### POST /auth/logout

Invalidate the current session token.

**Headers:** `Authorization: Bearer <token>`

**Response:**
```json
{ "ok": true }
```

---

### GET /auth/me

Get the currently authenticated user.

**Headers:** `Authorization: Bearer <token>`

**Response:**
```json
{ "username": "operator1", "role": "operator" }
```

---

### GET /auth/users

List all users. Requires admin role.

**Headers:** `Authorization: Bearer <token>`

**Response:**
```json
{
  "users": [
    { "username": "admin", "role": "admin" },
    { "username": "operator1", "role": "operator" }
  ]
}
```

---

### POST /auth/users

Create a new user. Requires admin role.

**Headers:** `Authorization: Bearer <token>`

**Request body:**
```json
{
  "username": "operator2",
  "password": "changeme",
  "role": "operator"
}
```

**Response:**
```json
{ "ok": true, "username": "operator2", "role": "operator" }
```

---

### PUT /auth/users/{username}

Update a user's role. Requires admin role.

**Headers:** `Authorization: Bearer <token>`

**Request body:**
```json
{ "role": "admin" }
```

**Response:**
```json
{ "ok": true, "username": "operator2", "role": "admin" }
```

---

### DELETE /auth/users/{username}

Delete a user. Requires admin role.

**Headers:** `Authorization: Bearer <token>`

**Response:**
```json
{ "ok": true, "deleted": "operator2" }
```

---

### POST /auth/users/{username}/reset-password

Reset a user's password. Requires admin role.

**Headers:** `Authorization: Bearer <token>`

**Request body:**
```json
{ "new_password": "newpass123" }
```

**Response:**
```json
{ "ok": true, "username": "operator2" }
```

---

### GET /auth/activity

Get recent authentication activity log.

**Headers:** `Authorization: Bearer <token>`

**Query params:** `limit` (default 50)

**Response:**
```json
{
  "activity": [
    {
      "username": "operator1",
      "action": "login",
      "timestamp": "2026-03-26T10:00:00Z",
      "ip": "192.168.1.100"
    }
  ]
}
```

---

## Socket.IO Events — Updated

### send_image (server → client) — updated payload

The `analytics` key is now included in every `send_image` event. The HMI analytics dashboard updates from this — no polling needed.

**Full payload:**
```json
{
  "type": "report",
  "material_id": "42",
  "machine_id": "",
  "basketid": "12",
  "sample_counter": 281,
  "frame_number": 281,
  "date_time": "2026-03-26T10:00:00Z",
  "result": "Good",
  "defect_type": "",
  "visible": "<base64 JPEG — VL camera>",
  "uv": "<base64 JPEG — UV camera>",
  "yarntail": "<base64 JPEG — Tail camera>",
  "stain": true,
  "tube_pattern": true,
  "cone_diameter": true,
  "tube_diameter": true,
  "yarn_res": true,
  "thread_mix": true,
  "analytics": {
    "shift": {
      "start": "2026-03-26T06:00:00Z",
      "total": 312,
      "good": 298,
      "defect": 14,
      "error": 0,
      "rejection_rate_pct": 4.5
    },
    "defect_breakdown": {
      "stain": 6,
      "tube_mismatch": 4,
      "uv_mixup": 2,
      "tail": 1,
      "dimension": 1
    },
    "per_material": {
      "42": { "total": 120, "good": 115, "defect": 5, "defect_types": { "stain": 3, "tube_mismatch": 2 } }
    },
    "session_total": 312,
    "session_good": 298,
    "session_defect": 14
  }
}
```

**`result` values:** `"Good"` | `"Defect"` | `"Error"` | `"Teach"` (during autonomous tube teaching — no pass/fail)

**Check fields** (`stain`, `tube_pattern`, `cone_diameter`, `tube_diameter`, `yarn_res`, `thread_mix`): `true` = passed, `false` = failed, `null` = check not run (module disabled or no detection).

---

### get_analytics (client → server)

Request current analytics snapshot. Server responds with `analytics_snapshot` event.

```json
// emit
{ }

// response event: analytics_snapshot
{ "ok": true, "data": { /* same as GET /analytics response */ } }
```

---

### reset_analytics (client → server)

Reset shift counters. Server responds with `analytics_reset` event.

```json
// emit
{ }

// response event: analytics_reset
{ "ok": true, "shift_start": "2026-03-26T14:00:00Z" }
```
