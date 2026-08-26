# Frontend Developer Guide — Sieger GHCL HMI v3.1.0

> **Who this is for:** Frontend developers implementing the visual design of the HMI React app.
> The backend team has already wired up all API calls, Zustand stores, and socket.io.
> Your job: implement the visual design for each page. The data is already flowing.
>
> **Related docs:**
> - `docs/frontend_hmi_reference.md` — Old HMI complete page-by-page reference (what existed before)
> - This file — New v3 architecture and page specs
>
> **Last updated:** 2026-04-01

---

## 1. What to Read (in order)

Start here, then read in this order:

| Document | What you need from it | Location |
|----------|----------------------|----------|
| **This guide** | Full HMI context | `docs/frontend_v3_guide.md` |
| **`docs/frontend_hmi_reference.md`** | Old HMI — all pages, flows, APIs (understand what we're replacing) | CV repo `docs/` |
| **`docs/api_reference.md`** | All endpoints — request/response schemas | CV repo `docs/` |
| **`docs/project_context.md`** — sections 1 and 2 only | What the system does, inspection pipeline | CV repo `docs/` |
| **`docs/12_teaching.md`** — operator perspective sections | What operators do during teaching — so UI flows make sense | CV repo `docs/` |

**Skip:** `docs/project_context.md` sections 3–10 (algorithm internals, PLC registers, camera hardware) — you don't need these to build the UI.

---

## 2. Architecture — What You Need to Know

```
PLC C2C ──────────────────► CV Backend (auto-starts inspection, no HMI needed)
                                │
HMI (React + Electron)          │
    │                           │
    ├── REST API calls ────────►│  FastAPI  :5002
    │   axios: cvApi            │  - /auth/*       (login, users, activity)
    │                           │  - /teaching/*   (tube, stain, dimension)
    │                           │  - /results      (inspection history)
    │                           │  - /analytics    (shift stats)
    │                           │  - /config/*     (settings via config.json)
    │                           │
    └── Live frames ───────────►│  Socket.IO :5004  (inspection service)
        socket.io-client        │  - send_image, teaching_alert
        ↓                       │  - start/stop, camera, PLC, lights
        inspectStore (Zustand)
```

**Key design principle:** The inspection loop runs independently via PLC C2C. The HMI is a monitoring/configuration tool — if no one opens it, inspection still runs.

**Two connections, already set up:**
- `src/api/client.ts` — `cvApi` (axios, port 5002). Use for all REST calls.
- `src/api/socket.ts` — `getSocket()` (socket.io, port 5004). Already connected, auto-reconnects.
- `src/store/inspectStore.ts` — already subscribed to `send_image` events. Just read from it.
- `src/store/authStore.ts` — token, username, role, services. Persisted to localStorage.

**One backend, one database:** All data (auth, inspection results, teaching sessions, settings) lives in SQLite (`sieger.db`). No MongoDB, no separate login service.

**You do not need to understand:** PLC, Modbus, cameras, YOLO, PatchCore, Python services.

---

## 2.1 Authentication — Session-Based (No JWT)

Auth uses simple session tokens stored in SQLite. No JWT decoding needed on the frontend.

### Login Flow
1. `POST /auth/login { username, password }` → `{ token, username, role, services, expires_at }`
2. Store `token`, `role`, `services` in localStorage
3. Set `Authorization: <token>` header on all subsequent requests
4. Backend validates by looking up token in `sessions` table

### Auth Tiers

| Access | Requires Login? | Pages |
|--------|----------------|-------|
| Public monitoring | No | `/inspect` (read-only), `/analytics` (read-only) |
| Authenticated | Yes | Reports, activity log, teaching, settings |
| Admin | Yes + `superAdmin` role | User management |

### Auth Store (`src/store/authStore.ts`)

```typescript
// Login
const res = await cvApi.post('/auth/login', { username, password })
const { token, username, role, services, expires_at } = res.data
localStorage.setItem('token', token)
cvApi.defaults.headers.common['Authorization'] = token

// Check session
const me = await cvApi.get('/auth/me')  // returns user or 401

// Logout
await cvApi.post('/auth/logout')
localStorage.removeItem('token')
```

### Session Expiry
- Default: **8 hours** (= one shift). Configurable server-side.
- Frontend should handle 401 by redirecting to `/login`.
- Sessions survive system restarts (stored in SQLite).

### Auth API Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/auth/login` | No | Returns session token + user info |
| POST | `/auth/logout` | Token | Invalidates session |
| GET | `/auth/me` | Token | Current user info |
| GET | `/auth/users` | superAdmin | List all users |
| POST | `/auth/users` | superAdmin | Create user |
| PUT | `/auth/users/{username}` | superAdmin | Update user |
| DELETE | `/auth/users/{username}` | superAdmin | Delete user |
| POST | `/auth/users/{username}/reset-password` | superAdmin | Reset password |
| GET | `/auth/activity` | superAdmin/engineer | Activity log |

### Roles & Services

| Role | Description |
|------|-------------|
| `superAdmin` | Full access + user management |
| `engineer` | Teaching, settings, reports |
| `operator` | Limited by `services` flags |

Service flags in user object: `live`, `master`, `settings`, `report`, `activityLog`, `inspection`, `email`

### Default Admin
On first boot: `admin` / `admin` with all services. **Change password after deployment.**

---

## 2.2 Settings — Configuration via UI

All configuration is managed through the UI. `config.json` is developer-only — no engineer or operator edits it directly. Everything is stored in `config.json` and read/written via API. Frontend calls `GET /config` on page load to populate all fields.

> **Platform note:** From the next system onwards, this runs on **Jetson Orin NX** with an all-in-one HMI touchscreen. Application engineers configure everything through the UI — no terminal access, no file editing.

### Settings Architecture

| What | API | Restart needed? |
|------|-----|-----------------|
| Inspection tasks | `PUT /config/tasks` | **No** — next cone |
| Teach toggles | `PUT /config/teach` | **No** — next cone |
| Shift hours | `PUT /config/shift` | **No** — immediate |
| Camera config | `PUT /config/cameras` | **Yes** |
| PLC config | `PUT /config/plc` | **Yes** |
| Power off / Restart | `POST /shutdown`, `POST /restart` | — |

---

### Inspection Tasks (operator/engineer)

Each inspection module has two toggles: **Inspect** (run inference, pass/fail) and **Teach** (save crops for cloud training). Both live in config.json and are read by the inspection service each cycle.

**How the two toggles interact per module:**

| `tasks.X` (Inspect) | `teach.X` (Teach) | Behavior |
|---------------------|-------------------|----------|
| ON | OFF | **Inspect** — run inference, pass/fail |
| ON | ON | **Teach** — save lossless numpy crops, skip inference for this module |
| OFF | (ignored) | **Off** — no inference, no capture |

**UI layout:**

```
Inspection Tasks
──────────────────────────────────────────────────
Module              Inspect     Teach
──────────────────────────────────────────────────
Stain Detection     [ON]        [OFF]
UV Inspection       [ON]        [OFF]
Yarn Tail           [ON]        [OFF]
Tube Pattern        [ON]        (auto)
Dimension Check     [ON]        [OFF]
──────────────────────────────────────────────────
```

> **Tube Pattern** has no teach toggle — it auto-teaches autonomously when an unknown material arrives. Teach column shows "(auto)" as a label, not a toggle.

**Read:** `GET /config` → `response.config.inspection.tasks` and `response.config.inspection.teach`

**Write (inspect toggles):**
```json
PUT /config/tasks
{ "uv_inspection": false, "tail_inspection": false }
→ { "ok": true, "tasks": {...}, "restart_required": false }
```

**Write (teach toggles):**
```json
PUT /config/teach
{ "stain_detection": true, "uv_inspection": true }
→ { "ok": true, "teach": {...}, "restart_required": false }
```

Both take effect on the **next cone** — no restart.

**Teaching workflow:** When teach is toggled ON for a module, the system saves crops during live inspection. The teaching page shows capture progress (127/200). When enough images are collected, the operator triggers training from the teaching page (upload to cloud for stain, local for UV/tail).

---

### Shift Hours (operator)

Single numeric input (float, 0–24) — shift duration before analytics counters auto-reset.

**Read:** `GET /config` → `response.config.shift_hours`
**Write:**
```json
PUT /config/shift
{ "shift_hours": 12.0 }
→ { "ok": true, "shift_hours": 12.0, "restart_required": false }
```

---

### Camera Configuration (application engineer)

Per-camera settings for VL, UV, and Tail. All cameras are Basler GigE (pypylon).

| Field | Type | Description | Default |
|-------|------|-------------|---------|
| `ip` | string | GigE camera static IP | VL: `192.168.1.160`, UV: `192.168.1.161`, Tail: `192.168.1.162` |
| `serial` | string | Camera serial number (for USB3 fallback) | Per device |
| `exposure` | int | Exposure time in microseconds | VL: `11000`, UV: `70000`, Tail: `8000` |
| `timeout` | int | Capture timeout in milliseconds | `2000` |
| `trigger_debounce_us` | int | Hardware trigger debounce in microseconds | `200000` |

**Read:** `GET /config` → `response.config.cameras`

```json
{
  "config": {
    "cameras": {
      "VL":   { "ip": "192.168.1.160", "serial": "4108843636", "exposure": 11000, "timeout": 2000, "trigger_debounce_us": 200000 },
      "UV":   { "ip": "192.168.1.161", "serial": "4110025657", "exposure": 70000, "timeout": 2000, "trigger_debounce_us": 200000 },
      "Tail": { "ip": "192.168.1.162", "serial": "4108843650", "exposure": 8000,  "timeout": 2000, "trigger_debounce_us": 200000 }
    }
  }
}
```

**Write** (send only fields that changed):
```json
PUT /config/cameras
{ "VL": { "ip": "192.168.1.160", "exposure": 12000 }, "UV": { "exposure": 65000 } }
→ { "ok": true, "cameras": {...}, "restart_required": true }
```

**UI layout:** 3 cards (VL, UV, Tail), each with IP, serial, exposure, timeout, debounce fields. Show "Restart required to apply" banner after save.

---

### PLC Configuration (application engineer)

Full PLC connection and register mapping. All values are configurable for different PLC programs.

**Connection fields:**

| Field | Type | Description | Default |
|-------|------|-------------|---------|
| `host` | string | PLC IP address | `192.168.1.110` |
| `port` | int | Modbus TCP port | `502` |
| `unit_id` | int | Modbus unit ID | `1` |
| `timeout` | float | Connection timeout (seconds) | `3.0` |
| `poll_interval` | float | Trigger polling interval (seconds) | `0.1` |

**Register addresses** (0-based Modbus addresses):

**Input registers (PLC → Vision) — values read from PLC:**

| Register | Address | Description |
|----------|---------|-------------|
| `sample_counter` | 0 | Part counter |
| `trigger` | 1 | Inspection start — vision clears to 0 after read |
| `c2c_start` | 7 | PLC display mode: 0=Disabled, 1=Normal, 2=Trial |
| `material_no` | 8 | Numeric material identifier |
| `basket_no` | 11 | Basket/sorting identifier |
| `loader_id` | 12 | Loader identifier |

**Output registers (Vision → PLC) — values written by vision:**

| Register | Address | Description |
|----------|---------|-------------|
| `result` | 2 | 1=Good, 2=Defect, 3=Error |
| `cycle_start` | 9 | Vision ready for next cone |
| `camera_error` | 14 | Error code, 0=OK |
| `ips_status` | 15 | IPS system status |
| `basket_no_echo` | 16 | Echo basket_no back to PLC |
| `material_no_echo` | 17 | Echo material_no back to PLC |
| `loader_no_echo` | 18 | Echo loader_id back to PLC |
| `defect_type` | 19 | 0=Good, 1=Stain, 2=Wrong Pattern, 3=Wrong Cone Dia, 4=Wrong Tube Dia, 5=Missing Tail, 6=Thread Mixup |
| `ack` | 20 | Vision sets 1, PLC clears to 0 |

**Light registers (Vision → PLC) — light control:**

| Register | Address | Description |
|----------|---------|-------------|
| `uv` | 4 | UV light on/off |
| `vl` | 5 | Visible light (LED) on/off |
| `yarntail` | 6 | Yarn tail light on/off |

**Read:** `GET /config` → `response.config.plc`

```json
{
  "config": {
    "plc": {
      "host": "192.168.1.110",
      "port": 502,
      "unit_id": 1,
      "timeout": 3.0,
      "poll_interval": 0.1,
      "registers": {
        "input":  { "sample_counter": 0, "trigger": 1, "c2c_start": 7, "material_no": 8, "basket_no": 11, "loader_id": 12 },
        "output": { "result": 2, "cycle_start": 9, "camera_error": 14, "ips_status": 15, "basket_no_echo": 16, "material_no_echo": 17, "loader_no_echo": 18, "defect_type": 19, "ack": 20 },
        "light":  { "uv": 4, "vl": 5, "yarntail": 6 }
      }
    }
  }
}
```

**Write** (send only fields that changed — merges with existing):
```json
PUT /config/plc
{
  "host": "192.168.1.110",
  "registers": {
    "input": { "material_no": 10 },
    "output": { "result": 3 }
  }
}
→ { "ok": true, "plc": {...}, "restart_required": true }
```

**UI layout:** Connection fields at top (host, port, unit_id, timeout, poll_interval). Below that, 3 collapsible sections for Input / Output / Light register tables — each row has register name (read-only label) and address (editable number input). Show "Restart required to apply" banner after save.

---

### System Controls

- **Restart** button → `POST /restart` (with confirmation modal)
- **Power Off** button → `POST /shutdown` (with confirmation modal)

---

### System Status (read-only)

Show current health:
- PLC: connected / disconnected, IP
- Cameras: VL / UV / Tail — connected / disconnected
- Service uptime
- Current inspection state (IDLE / INSPECT / CAPTURE)

---

### 4.3 Camera Diagnostics Panel (Settings Page)

**Data source:** Socket.IO `check_cameras` event → `camera_status` response
**Trigger:** Emit `check_cameras` on page load + poll every 10 seconds while on settings page.
**Access:** Engineer / superAdmin only.

```typescript
// Request
socket.emit('check_cameras', { cam_id: 'all' })

// Response (on 'camera_status' event)
{
  cameras: [
    {
      name: "vl",
      connected: true,
      ip: "192.168.1.160",
      exposure: 11000,
      health: "ok",       // "ok" | "warning" | "error"
      stats: {
        // Camera-side (hardware counters)
        frame_count: 281,         // Counter1 — FrameStart events
        line_trigger_count: 285,  // Counter2 — raw Line1 edges (before debounce)
        debounced: 4,             // triggers filtered by debounce

        // Transport-layer (GigE)
        missed: 0,                // frames lost in network
        failed: 0,                // incomplete/corrupt frames
        buffer_underruns: 0,      // no buffer available when frame arrived
        resend_requests: 12,      // packet resend requests
        resend_packets: 12,       // packets retransmitted

        // Application-side
        delivered: 281,           // frames successfully returned to app
        skipped: 0,               // frames discarded by LatestImageOnly
        block_id_gaps: 0,         // gaps in camera BlockID sequence
        temperature_c: 42.5       // sensor temperature
      }
    },
    // ... UV, Tail
  ],
  all_connected: true
}
```

**UI layout:**

```
Camera Diagnostics
══════════════════════════════════════════════════════════════

  VL (a2A2600-20gcPRO)          192.168.1.160         🟢 OK
  ├── Temperature:              42.5°C
  ├── Triggers:                 line=285  frame=281  debounced=4
  ├── Delivery:                 delivered=281  skipped=0  gaps=0
  └── Transport:                missed=0  failed=0  resend=12

  UV (acA1920-40gc)             192.168.1.161         🟡 WARNING
  ├── Temperature:              61.2°C ⚠
  ├── Triggers:                 line=280  frame=280  debounced=0
  ├── Delivery:                 delivered=280  skipped=0  gaps=0
  └── Transport:                missed=0  failed=0  resend=45

  Tail (a2A1920-40gc)           192.168.1.162         🔴 ERROR
  └── Not connected

══════════════════════════════════════════════════════════════
```

**Health indicator colors:**

| Health | Color | Icon | Meaning |
|--------|-------|------|---------|
| `ok` | Green | 🟢 | All counters clean |
| `warning` | Yellow | 🟡 | Debounced triggers, packet resends, or temp 60–75°C |
| `error` | Red | 🔴 | Frame loss, transport drops, disconnect, or temp > 75°C |

**Key metrics to highlight:**

| Metric | What it means for the operator/engineer |
|--------|----------------------------------------|
| `debounced` | Proximity sensor bouncing — may need mechanical adjustment |
| `missed` | Network issue — check cables, switch, NIC |
| `block_id_gaps` | Frames lost between camera and app — CPU too slow or buffer too small |
| `temperature_c > 60` | Camera running hot — check ventilation, ambient temp |
| `resend_requests` | Network quality indicator — high = noisy link |
| `delivered` vs `frame_count` | Should match — mismatch = leakage |

**Inspect page status bar (lightweight):**

The inspect page gets a simplified version from `camera_health` in every `send_image` event (section 3.1). Show as colored dots in the bottom status bar:

```typescript
const { cameraHealth } = useInspectStore()

// Render per camera: dot + name + temp
// 🟢 VL 42°C   🟡 UV 61°C   🔴 Tail --
```

No need to poll — updates automatically with each inspection cycle (~5-7 seconds).

---

### What's NOT on this page

| Removed from old HMI | Reason |
|----------------------|--------|
| Lights / Illumination | PLC controls lights directly |
| Error proofing | Removed |
| Email settings | Not implemented in v3 (future) |
| Per-master defect selection | Replaced by global task toggles — autonomous teaching handles per-material logic |

---

## 3. The `send_image` Event — Full Payload

Every 5–7 seconds (one cone inspected), the socket.io server emits `send_image`. The `inspectStore` handles this automatically. Here is the full payload — understand it so you know what data is available:

```json
{
  "type": "report",
  "material_id": "42",
  "basketid": "12",
  "sample_counter": 281,
  "frame_number": 281,
  "date_time": "2026-03-26T10:00:00Z",

  "result": "Good",
  "defect_type": "stain,tube_mismatch",

  "visible": "<base64 JPEG — VL camera 1280×720>",
  "uv": "<base64 JPEG — UV camera 1280×720>",
  "yarntail": "<base64 JPEG — Tail camera 640×256>",

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
      "42": { "total": 120, "good": 115, "defect": 5 }
    },
    "session_total": 312,
    "session_good": 298,
    "session_defect": 14
  }
}
```

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `result` | string | `"Good"` \| `"Defect"` \| `"Error"` \| `"Teach"` |
| `defect_type` | string | Comma-separated defect names e.g. `"stain,tube_mismatch"`. Empty string if Good. |
| `stain` | bool\|null | `true`=pass, `false`=fail, `null`=not run |
| `tube_pattern` | bool\|null | Tube label identity check |
| `cone_diameter` | bool\|null | Cone size within tolerance |
| `tube_diameter` | bool\|null | Tube size within tolerance |
| `yarn_res` | bool\|null | Yarn tail present |
| `thread_mix` | bool\|null | UV thread mixup check — `true`=no mixup (good) |
| `visible` | string | Base64 JPEG — render as `<img src={\`data:image/jpeg;base64,${visible}\`} />` |
| `uv` | string | Base64 JPEG — UV camera |
| `yarntail` | string | Base64 JPEG — Tail camera |
| `analytics` | object | Full shift analytics — update dashboard on every event |
| `camera_health` | object | Per-camera health summary (see section 3.1) |

**`result === "Teach"`** means the system is auto-teaching a new material — PLC gets ACK but no pass/fail. Show a "Teaching..." indicator instead of PASS/FAIL.

### 3.1 Camera Health in `send_image`

Every `send_image` event includes a `camera_health` object — no polling needed for basic status. Use this for the status bar indicators on the inspect page.

```json
{
  "camera_health": {
    "vl": {
      "connected": true,
      "health": "ok",
      "temperature_c": 42.5,
      "delivered": 281,
      "missed": 0,
      "frame_count": 281
    },
    "uv": {
      "connected": true,
      "health": "warning",
      "temperature_c": 61.2,
      "delivered": 280,
      "missed": 0,
      "frame_count": 281
    },
    "tail": {
      "connected": false,
      "health": "error",
      "temperature_c": -1.0,
      "delivered": 0,
      "missed": 0,
      "frame_count": -1
    }
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `connected` | bool | Camera is reachable on the network |
| `health` | string | `"ok"` \| `"warning"` \| `"error"` — derived from full stats |
| `temperature_c` | float | Camera sensor temperature in °C. `-1` = unavailable |
| `delivered` | int | Frames delivered to application this session |
| `missed` | int | Frames lost in GigE transport (network drops) |
| `frame_count` | int | Camera-side trigger counter (FrameStart events) |

**Health level derivation:**

| Level | Conditions |
|-------|------------|
| `ok` | No issues |
| `warning` | Debounced triggers, packet resends, or temperature 60–75°C |
| `error` | Frame loss, transport drops, buffer underruns, disconnect, or temperature > 75°C |

For full diagnostics (debounce stats, block ID gaps, resend counts), use the `check_cameras` Socket.IO event — see section 4.3.

---

## 4. Pages — Data Sources and Requirements

### `/login` — Login Page
**Data:** `useAuthStore` — `login()`, `logout()`
**API:** `POST /auth/login` → `{ token, username, role, services, expires_at }`
**UI:** Username + password form. "Skip Login (Dev)" button — remove before production.
**On success:** Store token in localStorage, set axios default header, redirect to `/inspect` or `/analytics`.

---

### `/inspect` — Inspect Page ⭐ most important

This is the page operators watch during production. Updates every 5–7 seconds (one cone inspected).

**Data source:** Socket.IO `send_image` event → `useInspectStore()`

```typescript
const { vlImage, uvImage, tailImage, resultCode, defectType,
        materialId, sampleCounter, connected } = useInspectStore()
```

**What the images actually are:**

These are NOT raw camera feeds. The CV pipeline processes each frame before sending:

| Image field | Source | Processing | Size |
|------------|--------|-----------|------|
| `visible` | VL camera (1920×1200) | YOLO detects cone → crops to bounding box → resizes. Text overlay: result (GOOD/DEFECT/ERROR), per-check labels (Stain:OK, Pattern:FAIL, etc.), material ID, master ID | 640×640 |
| `uv` | UV camera (1920×1200) | YOLO detects cone → crops to UV cone bbox → resizes | 640×640 |
| `yarntail` | Tail camera | Bottom 40% crop (tail is at cone base) → resizes | 640×256 |

If a camera times out or is missing, a black frame with "EMPTY" text is sent instead.

**Rendering:**
```tsx
<img src={`data:image/jpeg;base64,${vlImage}`} alt="VL Inspection" />
<img src={`data:image/jpeg;base64,${uvImage}`} alt="UV Inspection" />
<img src={`data:image/jpeg;base64,${tailImage}`} alt="Tail Inspection" />
```

**UI layout:**

```
┌─────────────────────────────────────────────────────────┐
│ Material ID: 42  │  Master: Blue_Diamond  │  Cone #281  │
├─────────┬─────────────────────┬─────────────────────────┤
│         │                     │                         │
│  VL     │   UV                │  Tail                   │
│  640×640│   640×640           │  640×256                │
│ (cone   │  (cone crop,        │ (bottom crop,           │
│  crop,  │   UV frame)         │  tail region)           │
│  with   │                     │                         │
│  text   │                     │                         │
│  overlay│                     │                         │
│         │                     │                         │
├─────────┴─────────────────────┴─────────────────────────┤
│                                                         │
│   ┌──────────┐    Defect checks:                        │
│   │          │    Stain:      ✓ OK  /  ✗ FAIL           │
│   │  PASS    │    Pattern:    ✓ OK  /  ✗ FAIL           │
│   │  or FAIL │    Cone Dia:   ✓ OK  /  ✗ FAIL           │
│   │  (large) │    Tube Dia:   ✓ OK  /  ✗ FAIL           │
│   │          │    Tail:       ✓ OK  /  ✗ FAIL           │
│   └──────────┘    Mixup:      ✓ OK  /  ✗ FAIL           │
│                                                         │
│   Shift: 312 total │ 298 good │ 14 defect │ 4.5%       │
│   Socket: 🟢  VL: 🟢 42°C  UV: 🟡 61°C  Tail: 🔴 --   │
└─────────────────────────────────────────────────────────┘
```

**Result indicator colors:**

| `resultCode` | `result` string | Color | Display |
|-------------|----------------|-------|---------|
| 1 | `"Good"` | `#dcfce7` (green) | Large "PASS" |
| 2 | `"Defect"` | `#ffe2e5` (red) | Large "FAIL" + defect type labels |
| 3 | `"Error"` | orange | "ERROR" |
| — | `"Teach"` | `#d1edfc` (blue) | "Teaching new material..." |

**Per-check booleans** (from `send_image` payload):

| Field | Meaning | Values |
|-------|---------|--------|
| `stain` | Stain/shade check | `true`=pass, `false`=fail, `null`=not run |
| `tube_pattern` | Tube label identity | `true`=pass, `false`=fail, `null`=not run |
| `cone_diameter` | Cone size in tolerance | `true`=pass, `false`=fail, `null`=not run |
| `tube_diameter` | Tube size in tolerance | `true`=pass, `false`=fail, `null`=not run |
| `yarn_res` | Yarn tail present | `true`=pass, `false`=fail, `null`=not run |
| `thread_mix` | UV thread mixup | `true`=no mixup (good), `false`=mixup detected, `null`=not run |

A check shows `null` when the corresponding inspection task is toggled off in settings, or the camera frame was unavailable.

**`defect_type`** is a comma-separated string of failed checks: `"Stain,Wrong Pattern"`. Empty string or `"Good"` when all pass.

**Real-time analytics** (from `send_image` → `data.analytics`):
- Shift totals: total / good / defect
- Rejection rate % — highlight red if > 5%
- Defect breakdown (chip list or small bar)

**Socket connection status:** Show `connected` boolean as a green/red indicator. If disconnected, show "Reconnecting..." banner.

**`result === "Teach"`** means the system is auto-teaching a new material — PLC gets ACK but no pass/fail verdict. Show a "Teaching..." indicator instead of PASS/FAIL.

---

### `/teaching` — Teaching Page

Teaching and data capture are unified into one page. There is no separate data collection page in v3.

**Key design principle:** Inspection and teaching share the same system but behave differently per module:

| Module | When | How capture works | How training works | Operator action |
|--------|------|-------------------|-------------------|-----------------|
| **Tube** | Automatically during production | Auto-saves 20 lossless numpy crops when unknown material arrives | Auto-trains (~50ms, pure CPU: LAB histogram + HSV + FFT). Auto-loads `.npz` into matcher. No restart. | **None** — fully automatic. Informational alert only. |
| **Stain** | Installation or retraining | Toggle "Teach" in settings → system saves crops during inspection until 200 reached | Upload to cloud → train on A100 (offline) | Toggle teach mode, then trigger training when ready |
| **UV / Tail** | Installation | Same as stain — toggle "Teach", auto-captures to 200 | Local training on device (when inspection stopped) | Toggle teach mode, then trigger training when ready |
| **Dimension** | Installation | No capture needed | No training — just enter mm values | Fill form + save |

---

#### Tube Teaching — Fully Automatic (No Operator Action)

This happens transparently during live inspection:

```
PLC sends material_no=55 (no .npz template exists)
    │
    ▼
System detects unknown material
    → result=0 to PLC (no pass/fail verdict)
    → inspect page shows "Teaching..." (blue indicator)
    → auto-saves 20 × 256×256 annular tube crops (numpy, lossless — not JPEG)
    → teaching_alert: "Capturing 15/20 for material 55..."
    │
    ▼
20 crops collected
    → auto-trains in background thread (~50ms, pure CPU)
    → Color histogram (LAB a*b*, 32×32) mean
    → HSV histogram (H-S) mean
    → FFT 1D magnitude mean
    → computes threshold: p99(self_distances) × 1.5
    → saves material_id.npz
    │
    ▼
Hot-loads new template into matcher (no restart)
    → teaching_alert: "Material 55 taught successfully ✓"
    → next cone with material 55 → normal inspection (pass/fail)
```

**No operator approval needed.** The alert is informational — it appears on the teaching page activity feed so the operator knows it happened. If teaching fails (too few valid crops, all blurry), the alert shows an error and the system keeps sending `result=0` for that material until teaching succeeds on a retry.

**Force retrain** (if tube model for existing material needs to be redone):
```
POST /teaching/tube/capture/start { "material_id": "42" }
→ system re-captures 20 crops for material 42

POST /teaching/tube/capture/stop { "material_id": "42" }
→ triggers training with new samples, replaces old .npz
```

---

#### Stain / UV / Tail Teaching — Toggle Teach Mode + Auto-Capture

These modules require the operator to enable teach mode. Once enabled, the system auto-captures crops during normal inspection until the target count (200) is reached.

**Flow:**

```
Operator enables teach mode for a module:
    Settings page → Inspection Tasks → Stain → Teach toggle ON
    (PUT /config/teach { "stain_detection": true })
    │
    ▼
During inspection (PLC C2C running):
    → cones are inspected normally for all OTHER modules
    → for the "teach" module: crops are saved to disk (lossless numpy)
    → no pass/fail for this module (result based on other modules only)
    → counter increments: 47/200, 48/200...
    → teaching_alert events fire with progress
    │
    ▼
200 images reached:
    → teaching_alert: "Stain: 200 images ready for material 42"
    → badge/notification on Teaching nav item
    │
    ▼
Operator stops inspection (shift end or deliberate stop):
    → Teaching page becomes active
    → Shows: "Stain: 200 images ready" [Train]
    │
    ▼
Operator clicks Train:
    → Stain: POST /cloud/upload → train on A100 (cloud)
    → UV/Tail: local training on device
    → teaching_alert shows training progress
    │
    ▼
Training complete:
    → Model loaded
    → Operator toggles Teach OFF for stain in settings
    → PUT /config/teach { "stain_detection": false }
    → Resumes normal stain inspection with new model
```

**How to start/stop capture:**

There is NO separate "Start Capture" / "Stop Capture" button. Capture is controlled entirely by the teach toggle in settings:

```
Start capture:  PUT /config/teach { "stain_detection": true }
                → system begins saving stain crops during inspection

Stop capture:   PUT /config/teach { "stain_detection": false }
                → system stops saving, resumes normal stain inspection
```

**API endpoints used by teaching page:**

| Endpoint | Purpose | When |
|----------|---------|------|
| `PUT /config/teach` | Toggle module to teach/inspect/off | Operator enables teach mode |
| `GET /capture/status` | Image counts per module per material (127/200) | Poll on teaching page |
| `GET /capture/sessions` | Past capture sessions (audit trail) | Teaching page history |
| `GET /capture/images` | Browse captured images | Review before training |
| `POST /teaching/stain` | Trigger stain training (when inspection stopped) | Operator clicks Train |
| `POST /teaching/uv` | Trigger UV training | Operator clicks Train |
| `POST /teaching/tail` | Trigger tail training | Operator clicks Train |
| `POST /cloud/upload` | Upload stain images to Azure for A100 training | Stain only |
| `POST /teaching/tube/capture/start` | Force re-capture for existing tube material | Tube retrain only |
| `POST /teaching/tube/capture/stop` | Stop tube re-capture + trigger training | Tube retrain only |
| `GET /teaching/alerts` | Teaching event history (last 100) | Page load |
| `GET /teaching/sessions/{id}/validate` | Validation report for a teaching session | After training completes |

---

#### Dimension Calibration — Installation-Time Form

Done once by the application engineer. No capture, no training — just enter physical measurements.

**API:** `POST /teaching/dimension`

```json
{
  "cone_diameter_mm": 60.0,
  "tube_diameter_mm": 32.0,
  "cone_tolerance_mm": 2.0,
  "tube_tolerance_mm": 1.5
}
```

**UI:** Simple form with 4 numeric fields + Save button. Show current values from `GET /config` → `config.inspection.dimension`.

---

#### Teaching Page UI Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Teaching                                                   │
├──────────────┬──────────────┬───────────────┬───────────────┤
│  Tube        │  Stain       │  UV / Tail    │  Dimension    │
│  Pattern     │  Detection   │  Thread Mix   │  Calibration  │
│              │              │               │               │
│  AUTOMATIC   │  TEACH MODE  │  TEACH MODE   │  FORM         │
│              │              │               │               │
│  Last:       │  Status:     │  Status:      │  Last calib:  │
│  2h ago ✓    │  Capturing   │  Off          │  2026-03-01   │
│  Materials:  │  127/200     │               │               │
│  42,55,18    │  ████████░░  │               │               │
│              │              │               │               │
│  [Retrain]   │  [Train]     │  [Start]      │  [Configure]  │
│              │  (when ready)│               │               │
└──────────────┴──────────────┴───────────────┴───────────────┘

┌─────────────────────────────────────────────────────────────┐
│  Live Activity Feed (from teaching_alert socket events)     │
│                                                             │
│  10:05:32  Tube   material=55  ✓ Taught successfully        │
│  10:05:31  Tube   material=55  Training... (50ms)           │
│  10:04:18  Tube   material=55  Capturing 20/20 ██████████  │
│  10:03:01  Stain  material=42  Capturing 127/200 ████████░░│
│  10:02:45  Tube   material=55  Capturing 15/20 ████████░░  │
│  09:58:00  Stain  material=42  Capturing 126/200           │
└─────────────────────────────────────────────────────────────┘
```

**Socket event:** `teaching_alert`
```json
{
  "module": "tube",
  "material_id": "55",
  "stage": "capturing",
  "message": "Captured 15/20 samples for material 55",
  "count": 15,
  "total": 20,
  "timestamp": "2026-04-01T10:04:18Z"
}
```

Stage flow: `capturing` (count increments) → `training` → `complete` ✓ or `error` ✗

---

### `/results` — Results Page

**Data sources:**
- `GET /results?limit=100&offset=0` — inspection history
- `GET /results/{id}/audit` — audit JPEG for a specific row
- `GET /analytics?from_ts=...&to_ts=...` — summary for date range filter

**UI:**
- Date range filter (from/to)
- Table: timestamp, material_id, result (chip), defect_type, thumbnails
- Click row → show audit JPEG in modal/drawer
- Summary bar above table: total / good / defect / rejection_rate for selected range (from `/analytics?from_ts=...&to_ts=...`)

---

### `/analytics` — Analytics Dashboard

**Data sources:**

| API | Purpose | When to call |
|-----|---------|-------------|
| `GET /analytics` | Live shift totals, defect breakdown, per-material | On page mount |
| `GET /analytics?from_ts=...&to_ts=...` | Historical range query | When user selects date range |
| `GET /analytics/hourly?date=YYYY-MM-DD` | Hourly Good/Defect/Error for line chart | On page mount (today), on date picker change |
| `POST /analytics/reset` | Reset shift counters | Operator button click |
| Socket.IO `send_image` → `data.analytics` | Real-time update every cone | Automatic — no polling |

**Data flow:**
1. On mount: `GET /analytics` → shift totals, defect breakdown, per-material
2. On mount: `GET /analytics/hourly` → hourly chart data for today
3. On every `send_image` socket event: read `data.analytics` → update shift totals live
4. No polling needed — `send_image` fires every 5–7 seconds

**UI layout:**

```
┌────────────┬────────────┬────────────┬────────────┐
│   Total    │    Good    │   Defect   │ Rejection  │
│   312      │    298     │    14      │   4.5%     │
│            │            │            │ (red >5%)  │
└────────────┴────────────┴────────────┴────────────┘

┌──────────────────────────────┬───────────────────────┐
│  Hourly Line Chart           │  Defect Breakdown     │
│  (Good vs Defect by hour)    │  (pie or bar chart)   │
│                              │  stain: 6             │
│  GET /analytics/hourly       │  tube_mismatch: 4     │
│                              │  uv_mixup: 2          │
│                              │  tail: 1              │
│                              │  dimension: 1         │
└──────────────────────────────┴───────────────────────┘

┌───────────────────────────────────────────────────────────────────────┐
│  Per-Material Table                                                   │
│  material_id │ total │ good │ defect │ rejection % │ defect types     │
│  42          │ 120   │ 115  │ 5      │ 4.2%        │ stain:3 tube:2   │
│  15          │ 192   │ 183  │ 9      │ 4.7%        │ stain:4 uv:3 t:2 │
└───────────────────────────────────────────────────────────────────────┘

[Reset Shift] button → POST /analytics/reset (with confirmation modal)
```

**`GET /analytics` response:**
```json
{
  "shift": {
    "total": 312, "good": 298, "defect": 14, "error": 0,
    "rejection_rate_pct": 4.5
  },
  "defect_breakdown": {
    "stain": 6, "tube_mismatch": 4, "uv_mixup": 2, "tail": 1, "dimension": 1
  },
  "per_material": {
    "42": { "total": 120, "good": 115, "defect": 5, "defect_types": { "stain": 3, "tube_mismatch": 2 } },
    "15": { "total": 192, "good": 183, "defect": 9, "defect_types": { "stain": 4, "uv_mixup": 3, "tail": 2 } }
  }
}
```

**`GET /analytics/hourly?date=2026-04-01` response:**
```json
{
  "date": "2026-04-01",
  "hours": [
    { "hour": 6, "good": 45, "defect": 3, "error": 0 },
    { "hour": 7, "good": 52, "defect": 1, "error": 0 },
    { "hour": 8, "good": 48, "defect": 2, "error": 0 }
  ]
}
```

**Rejection rate highlight:** Show rejection rate in red if > 5%.

**Do not poll.** The `send_image` event fires every cone — use it for live updates. Only call REST endpoints on page mount and date range changes.

---

## 5. Analytics Data Flow — Detailed

```
Page mount
    → GET /analytics          initialize analyticsStore with current shift data

Every cone (5-7s)
    → send_image event
    → inspectStore.setResult() already called by backend team
    → read data.analytics     update analyticsStore

Operator resets shift
    → POST /analytics/reset   server resets in-memory counters
    → next send_image          carries fresh analytics with shift.total=0

Operator changes shift hours
    → PUT /config/shift { "shift_hours": 6 }
    → saved to SQLite
    → takes effect on next auto shift reset (after current shift completes)
```

**Recommended store:**
```typescript
// src/store/analyticsStore.ts (new file — create this)
import { create } from 'zustand'

interface ShiftStats {
  start: string
  total: number
  good: number
  defect: number
  error: number
  rejection_rate_pct: number
}

interface AnalyticsState {
  shift: ShiftStats | null
  defect_breakdown: Record<string, number>
  per_material: Record<string, { total: number; good: number; defect: number }>
  session_total: number
  setAnalytics: (data: any) => void
}

export const useAnalyticsStore = create<AnalyticsState>()((set) => ({
  shift: null,
  defect_breakdown: {},
  per_material: {},
  session_total: 0,
  setAnalytics: (data) => set({
    shift: data.shift,
    defect_breakdown: data.defect_breakdown,
    per_material: data.per_material,
    session_total: data.session_total,
  })
}))
```

Then in `socket.ts` or wherever `send_image` is handled — add:
```typescript
socket.on('send_image', (data) => {
  // existing inspectStore update ...
  if (data.analytics) {
    useAnalyticsStore.getState().setAnalytics(data.analytics)
  }
})
```

---

## 6. Teaching Alert Events

The `teaching_alert` socket.io event fires during autonomous tube teaching. Show progress on the Tube teaching page.

```typescript
socket.on('teaching_alert', (data: {
  module: string        // 'tube' | 'stain' | 'uv' | 'tail' | 'dimension'
  material_id: string   // e.g. '42'
  stage: string         // 'capturing' | 'training' | 'complete' | 'error'
  message: string       // human-readable e.g. "Captured 15/20 samples for material 42"
  count: number         // current count
  total: number         // target count (20 for tube)
  timestamp: string     // ISO-8601
}) => {
  // update teaching progress UI
})
```

**Stage flow:**
```
capturing (count: 1..20) → training → complete
                                    → error
```

Show a progress bar during `capturing`: `count / total * 100%`
Show a spinner during `training`.
Show success/error chip on `complete`/`error`.

---

## 7. Do Not Modify

These files are wired by the backend team. Do not edit them:

| File | Reason |
|------|--------|
| `src/App.tsx` | Route definitions |
| `src/api/client.ts` | axios instances (cvApi, teachingApi) |
| `src/api/socket.ts` | socket.io client singleton |
| `src/store/authStore.ts` | Auth state + persistence |
| `src/store/inspectStore.ts` | Live inspection frame state |
| `src/main.tsx` | MUI theme + providers |

**You can and should create new stores:**
- `src/store/analyticsStore.ts` — shift analytics state
- `src/store/settingsStore.ts` — operator settings state (shift_hours)

Do not add analytics or settings state to `inspectStore` — keep stores single-purpose.

---

## 8. Quick API Reference — Page by Page

| Page | GET | POST/PUT |
|------|-----|----------|
| Login | — | `POST /auth/login` → `{ token, username, role, services, expires_at }` |
| Logout | — | `POST /auth/logout` |
| Profile | `GET /auth/me` | — |
| User Mgmt | `GET /auth/users` | `POST /auth/users`, `PUT /auth/users/{u}`, `DELETE /auth/users/{u}` |
| Activity Log | `GET /auth/activity` | — |
| Inspect | — | `POST start_inspection` (socket) |
| Teaching | `GET /teaching/alerts`, `GET /capture/status`, `GET /capture/sessions` | Tube: `POST /teaching/tube/capture/start`/`stop`. Stain/UV/Tail: `POST /teaching/stain`, `/uv`, `/tail`, `POST /cloud/upload`. Dimension: `POST /teaching/dimension` |
| Results | `GET /results`, `GET /results/{id}/audit`, `GET /analytics?from_ts&to_ts` | — |
| Analytics | `GET /analytics`, `GET /analytics/hourly?date=YYYY-MM-DD` | `POST /analytics/reset` |
| Settings | `GET /config` | `PUT /config/tasks`, `PUT /config/teach`, `PUT /config/shift`, `PUT /config/cameras`, `PUT /config/plc` |

---

## 9. Design Tokens (from customer-approved old UI)

| Token | Value |
|-------|-------|
| Page background | `rgb(250, 251, 252)` |
| Sidebar background | `#0b0f1f` |
| Sidebar active link | `#3457cc` |
| Primary blue | `#3457cc` |
| Font | `'Montserrat', sans-serif` |
| Header background | `#ffffff` |
| Card shadow | `0px 0px 5px 0px rgba(0,0,0,0.25)` |
| Card border radius | `6px` (small), `15px` (large) |
| Sidebar width | `300px` |

**Result colors:**

| Result | Color |
|--------|-------|
| Good / Pass | `#dcfce7` |
| Defect / Fail | `#ffe2e5` |
| Tube check | `#d1edfc` |
| Stain check | `#ffe2e5` |
| Yarn Tail | `#f2f5a6` |
| Dimension | `#fff4de` |
| UV / Tube Pattern | `#e3fcc3` |

---

## 10. Running the App

```bash
cd sieger-ghcl-hmi
npm install
npm run dev        # Electron window + hot reload

# Dev: skip login
# Click "Skip Login (Dev)" on the login page
# Also accessible at http://localhost:5173 in browser
```

Backend must be running for real data. For UI-only development, the skip login + mock data approach works for all pages except live camera feed.
