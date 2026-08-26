# Sieger GHCL HMI — Complete Page-by-Page Reference

> **Who this is for:** Frontend developers building the new HMI who need to understand every page, workflow, and feature of the existing system.
>
> **How to use this doc:** Read `frontend_v3_guide.md` first for the new v3 architecture (Zustand stores, TypeScript, FastAPI backend). Then read this document to understand what each page actually does, what the operator sees, and what features must be carried forward.
>
> **Last updated:** 2026-04-01

---

## 1. System Overview & Architecture

### 1.1 Backend Services (Old HMI — 6 services)

| Service | Port | Responsibility |
|---------|------|----------------|
| **LOGIN_URL** | 5000 | Authentication, user CRUD, activity logging (Node.js + MongoDB) |
| **REPORT_URL** | 5001 | Analytics data, reports, shift config, email settings |
| **PythonBackend** | 5002 | CV/AI processing — stain, tube, dimension, yarn tail, shutdown/restart |
| **TEACH_URL** | 5003 | Teaching data, config files (config.json, plc.json, inspect_config.json), image storage |
| **PythonSocket** | 5004 | Socket.IO — real-time image stream, camera control, PLC status, light control |
| **TICKET_URL** | 5005 | Support ticket system |

> **New v3 consolidates** all 6 services into FastAPI :5002 (REST + auth) + Socket.IO :5004 (real-time). MongoDB removed — SQLite only. See `frontend_v3_guide.md` for v3 architecture.

### 1.2 Connection Architecture (Old HMI)

```
HMI (React + Electron)
    │
    ├── REST (axios) ──► LOGIN_URL :5000   (auth, users, activity)
    ├── REST (axios) ──► REPORT_URL :5001  (analytics, reports, shifts, email)
    ├── REST (axios) ──► PythonBackend :5002 (CV processing, recipes, teaching)
    ├── REST (axios) ──► TEACH_URL :5003   (images, folders, config files)
    ├── REST (axios) ──► TICKET_URL :5005  (support tickets)
    │
    └── Socket.IO ────► PythonSocket :5004
        Events: send_image, stop_inspection, connect_cam,
                light_status, on_light, off_light,
                check_plc, plc_status, get_plc_info,
                error_proof, id_to_inspect, sorting
```

---

## 2. Authentication & Role-Based Access (Old HMI)

> **Note:** This documents the old HMI auth (Node.js + MongoDB + JWT on port 5000). For the new v3 auth (session-based, SQLite, no JWT), see `frontend_v3_guide.md` section 2.

### 2.1 Login Flow

1. User enters username + password on `/` (login page)
2. `POST LOGIN_URL:5000/auth/login` → returns JWT
3. JWT stored in `localStorage` as `'userData'`
4. `MainApp` component decodes JWT with `jwtDecode` to extract `role` and `services`

### 2.2 JWT Token Payload

```json
{
  "username": "operator1",
  "role": "superAdmin",
  "services": {
    "live": true,
    "master": true,
    "settings": true,
    "report": true,
    "activityLog": true,
    "inspection": true,
    "email": true
  }
}
```

### 2.3 Roles

| Role | Access |
|------|--------|
| `superAdmin` | All pages + Admin panel + Edit Profile |
| `admin` | All pages gated by services object |
| `operator` | Limited by services object |

### 2.4 Route Gating Logic

Every route in `MainApp.js` checks the relevant `services` flag. If the flag is `false`, the route redirects to `/analyticsnew`. Example:

```jsx
{services.live === true ? (
  <Route path="/inspect" element={<InspectN />} />
) : (
  <Route path="/inspect" element={<Navigate to="/analyticsnew" replace />} />
)}
```

---

## 3. Navigation & Routing

### 3.1 Sidebar Navigation (9 items)

| # | Label | Route | Icon | Permission |
|---|-------|-------|------|------------|
| 1 | Analytics | `/analytics` → `/analyticsnew` | AnalyticsIcon | Default landing |
| 2 | Inspect | `/live` → `/inspect` | LiveTvTwoToneIcon | `services.live` |
| 3 | DataCapture | `/datacollection` → `/collection` | CollectionsIcon | `services.master` |
| 4 | Teaching | `/mastersetup` → `/teaching` | TuneIcon | `services.master` |
| 5 | Annotation | (external) | CollectionsIcon | Always visible (launches Docker container via POST to 172.17.0.1:8080) |
| 6 | Settings | `/settings` → `/setting` | SettingsIcon | `services.settings` |
| 7 | Report | `/newreport` → `/reportnew` | DataThresholdingIcon | `services.report` |
| 8 | Activity Log | `/activitylog` → `/activity` | ManageHistoryIcon | `services.activityLog` |
| 9 | ManageUser | `/admin` → `/adminn` | PersonAddAltRoundedIcon | `role === 'superAdmin'` |

> **Note:** Sidebar label routes (column 3, left of →) differ from actual component routes (right of →). The new HMI should unify these.

### 3.2 Complete Route Table

| Route | Component | Permission | Description |
|-------|-----------|------------|-------------|
| `/` | Login-N | Public | Login page |
| `/analyticsnew` | Analytics-N | Default | Analytics dashboard |
| `/inspect` | Inspectn | `services.live` | Live inspection |
| `/collection` | CollectionN | `services.master` | Data capture |
| `/teaching` | MasterIDSelect | `services.master` | Teaching hub — master list |
| `/teaching/select` | SelectTeaching | `services.master` | Teaching type selection |
| `/teaching/conen` | ConeN | `services.master` | Cone dimension teaching |
| `/teaching/tuben` | Tube | `services.master` | Tube pattern teaching |
| `/teaching/stainn` | Stain | `services.master` | Stain teaching |
| `/teaching/yarnn` | yarntailn | `services.master` | Yarn tail / thread mix teaching |
| `/teaching/tubeverify` | TubeVerification | `services.master` | Tube verification |
| `/material` | MaterialIdSelect | `services.master` | Material ID management |
| `/verification` | VerificationIDSelect | `services.master` | Verification hub |
| `/verification/select` | VerificationSetupSelect | `services.master` | Verification type selection |
| `/verification/conen` | VerificationConeN | `services.master` | Verification — cone |
| `/verification/tuben` | VerificationTubeN | `services.master` | Verification — tube |
| `/verification/stainn` | VerificationStainN | `services.master` | Verification — stain |
| `/verification/yarnn` | VerificationYarnN | `services.master` | Verification — yarn |
| `/setting` | Setting_N | `services.settings` | Settings hub |
| `/setting/camera` | Camara_N | `services.settings` | Camera configuration |
| `/setting/plc` | Plc_N | `services.settings` | PLC register configuration |
| `/setting/shift` | Shift_N | `services.settings` | Shift timing configuration |
| `/setting/lights` | Lights_N | `services.settings` | Light hardware control |
| `/setting/configure` | config | `services.settings` | Defect selection per master |
| `/setting/illumination` | Illumination | `services.settings` | Illumination validation |
| `/setting/email` | emailSetting | `services.settings` | Email notification setup |
| `/setting/errorproof` | errorproof | Always | Error proofing capture |
| `/reportnew` | Report_N | `services.report` | Reports with filters |
| `/activity` | Activity_N | `services.activityLog` | User activity log |
| `/adminn` | AdminN | `role === 'superAdmin'` | User management |
| `/profile` | Profile | Authenticated | View profile |
| `/profile/edit` | EditProfile | `role === 'superAdmin'` | Edit profile |
| `/support` | Ticket | Authenticated | Support ticket list |
| `/support/create` | CreateTicket | Authenticated | Create new ticket |
| `/support/edit` | EditTicket | Authenticated | Edit ticket |
| `/chart` | Chart | Authenticated | SPC control charts |
| `/sample` | Gallery | Authenticated | Image gallery browser |
| `/tutorial` | Tutorials | Authenticated | Video tutorials |

---

## 4. Page-by-Page Guide

### 4.1 Login Page (`/`)

**What the operator sees:** A centered login card with the Sieger logo, username field, password field, and a login button.

**Workflow:**
1. Enter username and password
2. Click Login
3. System authenticates via `POST LOGIN_URL/auth/login`
4. JWT stored in localStorage → redirect to analytics dashboard

**API:** `POST LOGIN_URL/auth/login` → `{ token }`

---

### 4.2 Analytics Dashboard (`/analyticsnew`)

**What the operator sees:** The default home page after login. A high-level production overview.

**Layout:**
- **Top row:** 4 KPI cards with icons
  - Overall Count (total inspected)
  - Good Products (passed)
  - Rejected Products (failed)
  - Stain Count (stain defects specifically)
- **Middle row:**
  - **Line chart** (left): Yesterday's hourly inspection report — two lines (Good vs Rejected) plotted by hour
  - **Pie chart** (right): Yesterday's Good vs Rejected ratio
- **Bottom row:**
  - **Bar chart:** Shift-wise product counts across 3 shifts
  - **Data table:** Recent records with columns: S.No, DateTime, Status, Machine_id, Material_id, Defect_Type (sortable/filterable)

**API calls:**
- `GET REPORT_URL/analyticsdata` — all inspection records
- `GET REPORT_URL/dailyreport` — daily product counts for charts
- `GET REPORT_URL/shiftreport` — shift-wise breakdown

**Modals:** HelpModal (context-sensitive help button in header)

---

### 4.3 Live Inspection (`/inspect`) — Most Important Page

**What the operator sees:** The real-time production monitoring screen. This is the page operators stare at all day.

**Layout:**
```
┌─────────────────────────────────────────────────┐
│ Header: Material ID | Master ID | Machine ID    │
│         Basket ID   | Sample Counter | Shift     │
├──────────┬──────────────────────┬───────────────┤
│          │                      │               │
│  Donut   │   Camera Feed        │  Defect       │
│  Chart   │   (VL / UV image)    │  Breakdown    │
│  Good/   │   Base64 JPEG        │  Panel        │
│  Reject  │                      │               │
│          │   PASS / FAIL        │  stain: ✓/✗   │
│          │   (large colored     │  tube: ✓/✗    │
│          │    indicator)        │  cone_dia: ✓/✗│
│          │                      │  tube_dia: ✓/✗│
│          │                      │  yarn_res: ✓/✗│
│          │                      │  thread_mix:✓/✗│
├──────────┴──────────────────────┴───────────────┤
│  Reports Table: Per-Master-ID defect counts      │
│  Play/Stop buttons (floating)                    │
└─────────────────────────────────────────────────┘
```

**Data source:** Socket.IO `send_image` event (fires every 5–7 seconds, one per cone inspected)

**`send_image` payload:**
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
  "visible": "<base64 JPEG — VL camera 1280x720>",
  "uv": "<base64 JPEG — UV camera 1280x720>",
  "yarntail": "<base64 JPEG — Tail camera 640x256>",
  "stain": true,
  "tube_pattern": true,
  "cone_diameter": true,
  "tube_diameter": true,
  "yarn_res": true,
  "thread_mix": true,
  "analytics": {
    "shift": { "start": "...", "total": 312, "good": 298, "defect": 14, "error": 0, "rejection_rate_pct": 4.5 },
    "defect_breakdown": { "stain": 6, "tube_mismatch": 4, "uv_mixup": 2, "tail": 1, "dimension": 1 },
    "per_material": { "42": { "total": 120, "good": 115, "defect": 5 } },
    "session_total": 312,
    "session_good": 298,
    "session_defect": 14
  }
}
```

**Result indicator colors:**
| Result | Color | Display |
|--------|-------|---------|
| Good | `#dcfce7` (green) | Large "PASS" text |
| Defect | `#ffe2e5` (red) | Large "FAIL" text + defect_type labels |
| Error | orange | "ERROR" |
| Teach | `#d1edfc` (blue) | "Teaching new material..." |

**Defect booleans:** `true` = pass, `false` = fail, `null` = not run for this material

**Controls:**
- Play button → starts inspection (InspectSetupModel modal to select master/material IDs first)
- Stop button → emits `stop_inspection` socket event

**API calls:**
- `GET REPORT_URL/setting/shift` — shift configuration
- `GET LOGIN_URL/user/alluser` — user details from JWT

**Images:** Render as `<img src={`data:image/jpeg;base64,${visible}`} />`

---

### 4.4 Data Collection (`/collection`)

**What the operator sees:** A file browser + live capture tool for building training datasets.

**Layout:**
- **Left panel:** Folder tree (Master IDs → subfolders → images)
- **Center:** Image thumbnail grid with selection checkboxes
- **Right/Top:** Controls — Play/Stop for live capture, label dropdown, zoom controls
- **Tabs:** Raw images vs Labeled images

**Operator workflow:**
1. Click Play → DataCaptureSetupModel modal opens
2. Enter Material IDs and Inspection IDs (chip input)
3. Click Start → camera streams images to disk
4. Progress bar + timer shows capture progress
5. Click Stop → review captured images
6. Select images → apply labels from dropdown
7. Browse existing folders/images with zoom (TransformWrapper)

**Socket events:**
- Listen: `send_image` — live camera stream during capture
- Listen: `sorting` — sorting progress notifications
- Emit: `stop_inspection` — stop capture

**API calls:**
- `GET TEACH_URL/folders/{visible}` — folder structure
- `GET TEACH_URL/foldernames` — master folder list
- `GET TEACH_URL/{visible}/images/{image}` — individual image data

**Modals:** DataCaptureSetupModel (before starting capture)

---

### 4.5 Teaching Hub (`/teaching`)

**What the operator sees:** A grid of Master ID cards — the entry point for all teaching operations.

**Layout:**
- **Toggle:** "Manage Masters" vs "Library" view at top
- **Grid:** Cards for each Master ID, each with:
  - Checkbox for multi-select
  - Master ID name/label
  - Delete icon
- **Buttons:** Delete (enabled when items selected), Create New

**Operator workflow:**
1. View existing Master IDs
2. Click "Create" → TeachMaterialIdModel modal opens:
   - Master ID (text input)
   - Material ID (text input)
   - Tube Diameter (mm)
   - Cone Diameter (mm)
   - Tube Tolerance (±mm)
   - Cone Tolerance (±mm)
3. Click Save → creates new master recipe
4. Click a Master ID card → navigates to `/teaching/select`

**API calls:**
- `GET TEACH_URL/teaching/folders/VL` — existing master list
- `GET TEACH_URL/teaching/library/VL` — master library
- `POST PythonBackend/recipes` — create new material recipe
- `POST PythonBackend/delete_master` — delete from recipes
- `POST TEACH_URL/deletefolder/{ids}` — delete master folder

**Modals:** TeachMaterialIdModel, AlertModal (delete confirmation)

---

### 4.6 Teaching Type Selection (`/teaching/select`)

**What the operator sees:** 4 large navigation cards to choose which defect module to teach.

**Layout:**
- Breadcrumb: Teaching > {master_id}
- 4 cards in a row:
  1. **Extraction** (Cone Dimension) → `/teaching/conen`
  2. **Tube Pattern** → `/teaching/tuben`
  3. **Stain/Shade** → `/teaching/stainn`
  4. **Thread Mix** (Yarn Tail) → `/teaching/yarnn`
- Each card has an icon image and label

**Data:** Master ID read from `localStorage`

---

### 4.7 Cone Dimension Teaching (`/teaching/conen`)

**What the operator sees:** An image editor for defining cone dimensions by drawing circles on images.

**Layout:**
```
┌──────────┬────────────────────────────────┐
│ Thumbnail│  Main Image Viewer             │
│ Carousel │  (with circular ReactCrop)     │
│ (left)   │                                │
│          │  [Raw Image] [Configure] [Output]
│ img 1    │                                │
│ img 2    │  Configure tab:                │
│ img 3    │    Cone Diameter: [___] mm     │
│ ...      │    Outer Tolerance: [___] ±mm  │
│          │    [Crop] [Reset] [Save]       │
│          │    [< Prev]  [Next >]          │
│          │                                │
│          │  Footer: "Define >= 4 images"  │
│          │  [Apply] (enabled at 4+)       │
└──────────┴────────────────────────────────┘
```

**Operator workflow:**
1. Select an image from the left carousel
2. Switch to "Configure" tab
3. Draw a circular crop around the cone outer edge
4. Enter Cone Diameter (mm) and Outer Tolerance (±mm)
5. Click Save for this image
6. Repeat for at least 4 images
7. Click Apply → system processes all saved images through AI

**Data captured per image:** `[circleX, circleY, radius]`, cone diameter, tolerance, filename

**API calls:**
- `GET TEACH_URL/getimagedata/{folderid}` — material metadata
- `GET TEACH_URL/getimages/VL/{folderid}` — VL camera images
- `POST TEACH_URL/threshold` — save HSV thresholds
- `POST PythonBackend/extract` — run AI dimension extraction

---

### 4.8 Tube Pattern Teaching (`/teaching/tuben`)

**What the operator sees:** A two-mode editor for teaching tube label recognition.

**Mode 1 — Color Pattern Detection:**
- Image display with color overlay
- Click on image to pick foreground color
- Click again to pick background color
- Radio selector: Single color vs Two-color pattern
- Save per image

**Mode 2 — OCR (Tube Text):**
- Form fields:
  - PC Number
  - Title Name
  - Title Count
  - Lot Number
- These are the text strings printed on the inner tube label

**Operator workflow:**
1. Toggle between Color and OCR modes
2. For Color: pick colors from sample images
3. For OCR: enter the expected text values
4. Save configuration

**API calls:**
- `GET TEACH_URL/getimages/VL/{folderid}` — tube images
- `GET TEACH_URL/material_id/{folderid}` — material metadata
- `POST PythonBackend/tube_ocr` — save OCR form data
- `POST PythonBackend/color_detection` — save color detection data

---

### 4.9 Stain Teaching (`/teaching/stainn`)

**What the operator sees:** An image browser with AI processing for stain/shade defects.

**Layout:**
- Left: Image thumbnail carousel
- Center: Main image viewer
- Tabs: Raw Image | Final Output
- Apply button at bottom

**Operator workflow:**
1. Browse sample images (already captured)
2. Click Apply → backend processes all images through stain detection AI
3. Switch to "Final Output" tab to see stain mask overlay results
4. Breadcrumb: Teaching > {folderid} > Stain

**API calls:**
- `GET TEACH_URL/threshold` — fetch existing HSV thresholds
- `POST PythonBackend/stain` — process images for stain detection

---

### 4.10 Yarn Tail / Thread Mix Teaching (`/teaching/yarnn`)

**What the operator sees:** Similar to cone teaching but for UV camera images with HSV tuning.

**Key difference:** Uses **UV camera images** (not visible light).

**Layout:**
- Left: Image carousel (UV images)
- Center: Main image viewer
- Configure tab: H/S/V sliders (Hue, Saturation, Value ranges)
- Save per image, Apply when >= 4 saved
- Breadcrumb navigation

**Operator workflow:**
1. Select UV image from carousel
2. Adjust H/S/V sliders to isolate yarn tail features
3. Save for each image (minimum 4)
4. Click Apply → AI processes with configured thresholds

**API calls:**
- `GET TEACH_URL/getimages/UV/{folderid}` — UV camera images
- `POST PythonBackend/thread_mix` — process yarn tail data

---

### 4.11 Tube Verification (`/teaching/tubeverify`)

**What the operator sees:** A verification step after tube teaching — tests the teaching on new samples.

**Layout:**
- Image thumbnails with "Saved" overlay badges (green checkmarks on processed images)
- Main image viewer
- Tabs: Raw | Output
- Apply button

**Operator workflow:**
1. View new test images (not the same ones used for teaching)
2. Click Apply → backend processes all images through tube pattern AI
3. Review output — system shows pass/fail per image
4. If acceptable, system marks teaching as complete (`isTeached: true`)

**Two-stage processing:**
1. `POST PythonBackend/tube` — process images
2. `POST TEACH_URL/save_tube_config` — mark teaching complete

**Modals:** AlertModal (success/error feedback)

---

### 4.12 Verification Flow (`/verification/*`)

**What the operator sees:** Identical UI to the Teaching flow, but using a separate verification dataset.

The verification flow mirrors teaching exactly:
- `/verification` → Master ID selection (same as `/teaching`)
- `/verification/select` → Type selection (same 4 cards)
- `/verification/conen` → Cone verification
- `/verification/tuben` → Tube verification
- `/verification/stainn` → Stain verification
- `/verification/yarnn` → Yarn tail verification

**Key difference:** Images are loaded from a verification folder, not the training folder. This ensures the model is tested on unseen data before deployment.

---

### 4.13 Settings Hub (`/setting`)

**What the operator sees:** A 2×4 grid of settings category cards, plus system control buttons.

**Cards (8 total):**
1. **Camera** → `/setting/camera` — camera parameters & live preview
2. **PLC** → `/setting/plc` — PLC register configuration
3. **Shift** → `/setting/shift` — work shift timings
4. **Lights** → `/setting/lights` — hardware light control
5. **Configure** → `/setting/configure` — select which defects to inspect per master
6. **Illumination** → `/setting/illumination` — lighting validation
7. **Email Setting** → `/setting/email` — email notification setup
8. **Error Proof** → `/setting/errorproof` — defect example capture

**System controls:**
- **Power Off** button → PowerOffModel confirmation → `POST PythonBackend/shutdown`
- **Restart** button → RestartModel confirmation → `POST PythonBackend/restart`

**Modals:** PowerOffModel, RestartModel, HelpModal

---

### 4.14 Camera Settings (`/setting/camera`)

**What the operator sees:** Camera hardware configuration with live preview.

**UI elements:**
- Manufacturer dropdown (e.g., Basler, FLIR)
- Camera count selector
- Camera type selector
- Exposure slider (range: 50–100)
- Live image preview area (shows camera feed when connected)
- Play button → connects camera, starts preview
- Stop button → disconnects camera
- Save button → persists settings (with confirmation modal)

**Socket events:**
- Emit: `connect_cam` with `{ exposure, manufacturer }` — connect camera
- Emit: `stop_inspection` — disconnect camera
- Listen: `send_image` — live camera feed for preview

**API:** `GET TEACH_URL/config.json` — load saved camera config

---

### 4.15 PLC Settings (`/setting/plc`)

**What the operator sees:** A register mapping table for the PLC (Programmable Logic Controller).

**UI elements:**
- PLC IP address input field
- Data table with columns:
  - Property (register name, e.g., "trigger", "result", "basket_id")
  - Register (address number)
  - Type (read / write)
  - Value (current value)
  - Edit button
- Test Connection button → shows connection status indicator
- Modal for editing individual register mappings

**Socket events:**
- Emit: `check_plc` — test PLC connection
- Listen: `plc_status` — connection status response
- Emit: `get_plc_info` — fetch live PLC values
- Listen: `send_image` — config data feedback

**API:**
- `GET TEACH_URL/plc.json` — load PLC configuration
- `PUT TEACH_URL/api/plc` — update PLC config

---

### 4.16 Shift Settings (`/setting/shift`)

**What the operator sees:** Shift timing configuration for production scheduling.

**UI elements:**
- Shift selector dropdown: Shift 1, Shift 2, Shift 3
- Start Time picker (Ant Design TimePicker, 24-hour format)
- End Time picker
- Add Shift button
- Display of current shift configurations
- Confirmation modal with warning icon before saving

**API:**
- `GET REPORT_URL/setting/getsettings` — fetch all shifts
- `POST REPORT_URL/setting/shift` — save shift timing

---

### 4.17 Lights Control (`/setting/lights`)

**What the operator sees:** 4 toggle switches to control physical lighting hardware.

**Toggle switches:**
1. **Master Switch** — turns ALL lights on/off simultaneously
2. **Visible Lights** — VL camera illumination
3. **UV Lights** — UV camera illumination
4. **Yarn Tail Lights** — tail camera illumination

Each toggle shows ON/OFF text label and visual state indicator. Uses rsuite Toggle component (large size).

**Socket events:**
- Emit: `light_status` — query current light state
- Emit: `on_light` / `off_light` — toggle specific light
- Listen: `send_image` with `type=light_status` — receive current state

---

### 4.18 Config / Defect Selection (`/setting/configure`)

**What the operator sees:** A checklist to enable/disable specific inspection algorithms per Master ID.

**UI elements:**
- Master ID list with checkboxes
- Click a Master ID → modal opens with defect checkboxes:
  - `yarntail` — toggleable
  - `conedia` (cone diameter) — toggleable
  - `stain` — toggleable
  - `tubepattern` — **always enabled, cannot be disabled**
  - `tubedia` (tube diameter) — toggleable
  - `threadmix` — toggleable
- Submit button in modal

**Socket:** Emit `id_to_inspect` — broadcast which defects are active per master

**API:** `GET TEACH_URL/inspect_config.json` — load current defect config

---

### 4.19 Illumination Validation (`/setting/illumination`)

**What the operator sees:** A test tool to verify lighting is adequate for inspection.

**UI elements:**
- Illumination Type dropdown: UV, Visible
- Check button — captures test image and evaluates
- Save button — persist if lighting is adequate
- Status display: Pass / Fail indicator
- Test image preview (base64 encoded)

**Operator workflow:**
1. Select illumination type (UV or Visible)
2. Click Check → system captures image and analyzes brightness/uniformity
3. Status shows Pass or Fail
4. If Pass → click Save to confirm settings

**Socket events:**
- Emit: `error_proof` with `{ type: "check" }` or `{ type: "save" }`
- Listen: `send_image` with `type=illumination_check` — receive test image + result

---

### 4.20 Email Settings (`/setting/email`)

**What the operator sees:** SMTP configuration and email recipient management.

**Layout:**
- **SMTP Configuration form:**
  - Service selector (Custom, Gmail, Outlook, etc.)
  - SMTP Server address
  - Port number
  - Username
  - Password
  - Report frequency dropdown: Day / Week / Month
  - Save Configuration button
- **Email Recipients table:**
  - Columns: S.No, Email Address, Delete button
  - Add Email button → input field for new email
  - Delete button per row

**API:**
- `GET REPORT_URL/setting/mailget` — fetch SMTP config
- `GET REPORT_URL/senderemail/getemail` — get recipient list
- `POST REPORT_URL/setting/mailconfig` — save SMTP config
- `POST REPORT_URL/senderemail/newemail` — add recipient
- `DELETE REPORT_URL/senderemail/delete` — remove recipient

---

### 4.21 Error Proofing (`/setting/errorproof`)

**What the operator sees:** A two-step tool for capturing defect example images.

**Page 1 — Select Defect Type:**
- 4 large cards in a grid:
  - Stain/Shade
  - Threadmix
  - Tube Pattern
  - Yarn

**Page 2 — Capture (after selecting defect):**
- Master ID dropdown
- Material ID dropdown
- Live camera preview (auto-connects based on defect type — UV camera for threadmix, VL for others)
- Capture button — saves current frame as defect example

**Socket events:**
- Emit: `connect_cam` — connect camera with appropriate exposure
- Emit: `error_proof_defect` — capture and save example
- Listen: `send_image` with `type=error_proof` — receive captured image

**API:**
- `GET TEACH_URL/config.json` — camera exposure values
- `POST PythonBackend/get_teaching_data` — fetch available master IDs

---

### 4.22 Reports (`/reportnew`)

**What the operator sees:** A filterable report viewer with PDF export.

**Layout:**
- **Filter panel (top):**
  - Shift dropdown
  - Date range pickers (From / To)
  - Machine ID selector
  - Material ID selector
  - Status selector (Good / Defect / All)
- **Summary charts:** Pie chart + bar chart for filtered data
- **Data table:** Rows with columns — timestamp, material_id, result (colored chip), defect_type, image viewer button
- **Export:** PDF button (generates report with html2canvas + jsPDF)

**Operator workflow:**
1. Set filters (date range, shift, material, status)
2. View filtered results in table and charts
3. Click image icon on any row → opens image in modal
4. Click Export PDF → downloads formatted report

**API:** `GET REPORT_URL/analyticsdata` — records for filtering (client-side filtering)

---

### 4.23 Activity Log (`/activity`)

**What the operator sees:** A simple table of user login/logout sessions.

**Columns:**
- Name (username)
- Start Time (formatted date/time)
- End Time (formatted, or "Currently Active" if session ongoing)
- Activity (action type — login, logout, etc.)

Sortable and filterable table.

**API:** `GET LOGIN_URL/activity` — all activity records

---

### 4.24 Admin / User Management (`/adminn`)

**What the operator sees:** User CRUD interface (superAdmin only).

**Layout:**
- Search bar (real-time user search)
- User list with columns:
  - Avatar (first letter circle)
  - Username
  - Email
  - Role
  - Services/Permissions (badge list)
- Action buttons per user: Edit, Delete, Reset Password
- Add User button

**Add/Edit User modal form:**
- Username
- Password
- Email
- Employee ID
- Role dropdown (superAdmin, admin, operator)
- Service toggles (7 switches):
  - Live inspection
  - Master setup
  - Reports
  - Activity log
  - Settings
  - Inspection
  - Email sending

**API:**
- `GET LOGIN_URL/user/alluser` — list all users
- `POST LOGIN_URL/user/searchuser` — search users
- `POST LOGIN_URL/auth/adduser` — create user
- `PUT LOGIN_URL/auth/edituser/{username}` — update user
- `DELETE LOGIN_URL/user/{username}` — delete user
- `POST LOGIN_URL/user/reset-password` — reset password

---

### 4.25 Profile (`/profile`)

**What the operator sees:** Read-only view of their profile.

**Display fields:**
- Avatar circle (first letter of username)
- Username
- Email
- Phone number
- Role
- Address (combined lines)
- Services section (bullet list): Reports, Activity Log, Settings, Inspection, Email

**Edit button** → navigates to `/profile/edit` (superAdmin only)

**API:** `POST LOGIN_URL/user/details` — fetch user details by username

---

### 4.26 Edit Profile (`/profile/edit`)

**What the operator sees:** Profile editing form (superAdmin only).

**Form fields (3-column layout):**
- Address Line 1, Address Line 2
- Country, State, District
- Pincode
- Phone Number
- Email Address
- Avatar preview
- Upload Image button (currently disabled)
- Cancel / Save buttons

**API:** `POST LOGIN_URL/user/update` — save updated profile

---

### 4.27 Support Tickets (`/support`)

**What the operator sees:** Internal support ticket system.

**Ticket List page:**
- Table: Ticket ID, Title, Status (Open/Closed with color badge), Created By, Date Reported
- View button per row → opens ticket details
- Pagination
- "Create Ticket" button → `/support/create`

**Ticket Detail view:**
- Full ticket description
- Comments section with chronological messages
- Add comment input field
- Attachment viewer (opens AttachmentViewerModal)

**API:**
- `GET TICKET_URL/api/ticket/getall` — list all tickets
- `GET TICKET_URL/api/ticket/get/{ticketId}` — ticket details
- `POST TICKET_URL/api/ticket/get/{ticketId}/comments` — add comment

---

### 4.28 Create Ticket (`/support/create`)

**What the operator sees:** New ticket form.

**Form:**
- Title input
- Description textarea
- File upload area:
  - Browse button
  - Drag & drop zone
  - Uploaded file list with delete buttons
- Breadcrumbs: Support > Create Ticket
- Submit / Back buttons

**API:** `POST TICKET_URL/api/ticket` — multipart/form-data with attachments

**Modals:** TicketSuccessModal (after submission), ErrorModal (on failure)

---

### 4.29 SPC Charts (`/chart`)

**What the operator sees:** Statistical Process Control (Shewhart) charts.

**5 chart selector buttons:**
1. Bobbin Diameter Control Chart
2. Bobbin Strain Control Chart
3. Cone Diameter Control Chart
4. Yarn Tail Control Chart
5. Tube Color Strain Control Chart

Each chart is a line chart with upper/lower control limits. Click a button to switch chart type.

> **Note:** This page currently uses static/demo data. The new HMI should wire this to real analytics data from the `/analytics` endpoint.

---

### 4.30 Gallery (`/sample`)

**What the operator sees:** A 3-level hierarchical image browser.

**Navigation:**
1. Level 1: Folder type (VL, UV, etc.)
2. Level 2: Category / Master ID
3. Level 3: Individual images

**Image viewer:** Zoom in/out + pan controls (TransformWrapper). Back button to navigate up levels.

**API:**
- `GET TEACH_URL/gallery` — gallery folder structure
- `GET TEACH_URL/gallery/{folder}/{subfolder}/{filename}` — individual image

---

### 4.31 Tutorials (`/tutorial`)

**What the operator sees:** In-app video training library.

**Layout:**
- **Left sidebar:** 10 topic buttons stacked vertically:
  1. Introduction
  2. Analytics
  3. Inspect
  4. Data_Capture
  5. Teaching
  6. Settings
  7. Reports
  8. Activity_Log
  9. Manage_User
  10. Conclusion
- **Right panel:** ReactPlayer video player

Click a topic → loads `/seiger_videos/{topic}.mp4` in the player.

---

## 5. Teaching Workflow — Complete Operator Journey

This is the most complex multi-page workflow in the HMI. Here's the full path an operator follows to teach a new yarn cone type:

### Step 1: Create Master
1. Go to `/teaching` (Teaching Hub)
2. Click "Create" button
3. Fill TeachMaterialIdModel: Master ID, Material ID, diameters, tolerances
4. Click Save → new master appears in grid

### Step 2: Select Master & Teaching Type
1. Click the new Master ID card
2. Navigates to `/teaching/select`
3. Choose which module to teach (4 options)

### Step 3: Teach Each Module

**Cone Dimension** (`/teaching/conen`):
1. Browse VL camera images
2. Draw circular crop on cone edges (minimum 4 images)
3. Enter physical dimensions and tolerances
4. Click Apply → AI extracts dimension model

**Tube Pattern** (`/teaching/tuben`):
1. Color mode: Pick foreground/background colors from tube images
2. OCR mode: Enter expected tube label text
3. Save configuration

**Stain/Shade** (`/teaching/stainn`):
1. Browse images
2. Click Apply → AI generates stain detection model

**Yarn Tail / Thread Mix** (`/teaching/yarnn`):
1. Browse UV camera images
2. Adjust H/S/V sliders for each image (minimum 4)
3. Click Apply → AI generates yarn tail model

### Step 4: Verify Tube Teaching
1. Go to `/teaching/tubeverify`
2. System processes verification images
3. If results are good → marks `isTeached: true`

### Step 5: Verification (Optional)
1. Go to `/verification` → select same Master ID
2. Run same teaching modules on verification dataset
3. Confirms model works on unseen data

---

## 6. Socket.IO Events — Complete Reference

### Events Listened To (Server → Client)

| Event | Payload Type | Used In | Description |
|-------|-------------|---------|-------------|
| `send_image` | JSON (type varies) | Inspect, DataCollection, Camera, PLC, Lights, Illumination, ErrorProof | Multi-purpose event — `type` field determines context: `"report"` (inspection), `"light_status"`, `"illumination_check"`, `"error_proof"` |
| `plc_status` | JSON | PLC Settings | PLC connection test result |
| `sorting` | JSON | DataCollection | Sorting progress notifications |

### Events Emitted (Client → Server)

| Event | Payload | Used In | Description |
|-------|---------|---------|-------------|
| `stop_inspection` | none | Inspect, DataCollection, Camera | Stop live camera/inspection |
| `connect_cam` | `{ exposure, manufacturer }` | Camera, ErrorProof | Connect camera for preview |
| `check_plc` | none | PLC Settings | Test PLC connection |
| `get_plc_info` | none | PLC Settings | Fetch live PLC register values |
| `light_status` | none | Lights | Query current light states |
| `on_light` / `off_light` | light type | Lights | Toggle physical lights |
| `error_proof` | `{ type: "check"/"save" }` | Illumination | Test/save illumination |
| `error_proof_defect` | defect data | ErrorProof | Capture defect example |
| `id_to_inspect` | master/material config | Config | Set active inspection config |

> **Note:** The `send_image` event is overloaded — the `type` field in the payload determines what the data represents. In the new HMI, consider using separate events for clarity.

---

## 7. API Endpoint Reference — Complete

### LOGIN_URL (Port 5000)

| Method | Endpoint | Used In | Description |
|--------|----------|---------|-------------|
| POST | `/auth/login` | Login | Authenticate, returns JWT |
| POST | `/auth/adduser` | Admin | Create new user |
| PUT | `/auth/edituser/{username}` | Admin | Update user |
| GET | `/user/alluser` | Admin, Inspect | List all users |
| POST | `/user/searchuser` | Admin | Search users |
| DELETE | `/user/{username}` | Admin | Delete user |
| POST | `/user/reset-password` | Admin | Reset user password |
| POST | `/user/details` | Profile | Fetch user by username |
| POST | `/user/update` | EditProfile | Update profile |
| GET | `/activity` | ActivityLog | Fetch all activity records |
| GET | `/inspection/setup` | InspectSetupModal | Load saved inspection setup |

### REPORT_URL (Port 5001)

| Method | Endpoint | Used In | Description |
|--------|----------|---------|-------------|
| GET | `/analyticsdata` | Analytics, Reports | All inspection records |
| GET | `/dailyreport` | Analytics | Daily product counts |
| GET | `/shiftreport` | Analytics | Shift-wise breakdown |
| GET | `/setting/shift` | Inspect | Shift configuration |
| GET | `/setting/getsettings` | ShiftSettings | All shift timings |
| POST | `/setting/shift` | ShiftSettings | Save shift timing |
| GET | `/setting/mailget` | EmailSettings | SMTP configuration |
| POST | `/setting/mailconfig` | EmailSettings | Save SMTP config |
| GET | `/senderemail/getemail` | EmailSettings | Email recipient list |
| POST | `/senderemail/newemail` | EmailSettings | Add email recipient |
| DELETE | `/senderemail/delete` | EmailSettings | Remove email recipient |

### PythonBackend (Port 5002)

| Method | Endpoint | Used In | Description |
|--------|----------|---------|-------------|
| POST | `/extract` | ConeTeaching | Run AI dimension extraction |
| POST | `/stain` | StainTeaching | Process stain detection |
| POST | `/tube` | TubeVerification | Process tube pattern |
| POST | `/thread_mix` | YarnTeaching | Process yarn tail |
| POST | `/tube_ocr` | TubeTeaching | Save OCR form data |
| POST | `/color_detection` | TubeTeaching | Save color detection data |
| POST | `/recipes` | TeachingHub | Create/read material recipes |
| POST | `/delete_master` | TeachingHub | Delete master from recipes |
| POST | `/get_teaching_data` | ErrorProof | Fetch available master IDs |
| POST | `/shutdown` | SettingsHub | System power off |
| POST | `/restart` | SettingsHub | System restart |

### TEACH_URL (Port 5003)

| Method | Endpoint | Used In | Description |
|--------|----------|---------|-------------|
| GET | `/getimagedata/{id}` | ConeTeaching | Material metadata |
| GET | `/getimages/VL/{id}` | ConeTeaching, TubeTeaching | VL camera images |
| GET | `/getimages/UV/{id}` | YarnTeaching | UV camera images |
| GET | `/material_id/{id}` | TubeTeaching | Material metadata |
| GET | `/threshold` | StainTeaching | HSV threshold data |
| POST | `/threshold` | ConeTeaching | Save HSV thresholds |
| GET | `/folders/{type}` | DataCollection, Config | Folder structure |
| GET | `/foldernames` | DataCollection | Master folder list |
| GET | `/teaching/folders/VL` | TeachingHub | Teaching master list |
| GET | `/teaching/library/VL` | TeachingHub | Master library |
| POST | `/deletefolder/{ids}` | TeachingHub | Delete folder |
| POST | `/save_tube_config` | TubeVerification | Mark teaching complete |
| GET | `/config.json` | Camera, ErrorProof | Camera config |
| GET | `/plc.json` | PLC Settings | PLC register map |
| PUT | `/api/plc` | PLC Settings | Update PLC config |
| GET | `/inspect_config.json` | Config | Active defect config |
| GET | `/gallery` | Gallery | Gallery folder structure |
| GET | `/gallery/{f}/{sf}/{file}` | Gallery | Individual image |

### TICKET_URL (Port 5005)

| Method | Endpoint | Used In | Description |
|--------|----------|---------|-------------|
| GET | `/api/ticket/getall` | Tickets | List all tickets |
| GET | `/api/ticket/get/{id}` | Tickets | Ticket details |
| POST | `/api/ticket` | CreateTicket | Create ticket (multipart) |
| POST | `/api/ticket/get/{id}/comments` | Tickets | Add comment |

---

## 8. Modals Reference

| Modal | Trigger | Purpose | Key UI |
|-------|---------|---------|--------|
| **InspectSetupModel** | Play button on Inspect page | Select master/material IDs before starting inspection | Material count input, Master ID grid, Start/Cancel |
| **DataCaptureSetupModel** | Play button on DataCollection | Configure capture session | Chip inputs for Material IDs and Inspection IDs |
| **TeachMaterialIdModel** | Create button on Teaching Hub | Define new material recipe | Master ID, Material ID, Tube/Cone diameters, tolerances |
| **RestartModel** | Restart button on Settings Hub | Confirm system restart | "Are you sure?" + Yes/No |
| **PowerOffModel** | Power Off button on Settings Hub | Confirm system shutdown | "Are you sure?" + Yes/No |
| **AlertModal** | Various (delete, save confirmations) | Generic alert | Message text + OK button |
| **ErrorModal** | API errors, validation failures | Show error message | Error text + dismiss |
| **Loading_Modal** | During AI processing (Apply buttons) | Show processing state | Circular spinner + message |
| **HelpModal** | Help (?) button on various pages | Context-sensitive help | Help content based on `sectionName` prop |
| **AttachmentViewerModal** | Click attachment in ticket details | View ticket images | Image gallery/carousel |
| **TicketSuccessModal** | After ticket submission | Confirm success | Success message + Proceed button |

---

## 9. Design Tokens

### Colors

| Token | Value |
|-------|-------|
| Page background | `rgb(250, 251, 252)` |
| Sidebar background | `#0b0f1f` |
| Sidebar active link | `#3457cc` |
| Primary blue | `#3457cc` |
| Header background | `#ffffff` |
| Card shadow | `0px 0px 5px 0px rgba(0,0,0,0.25)` |
| Card border radius | `6px` (small), `15px` (large) |
| Sidebar width | `300px` |

### Result Colors

| Result | Background Color | Context |
|--------|-----------------|---------|
| Good / Pass | `#dcfce7` | Green indicator |
| Defect / Fail | `#ffe2e5` | Red indicator |
| Tube check | `#d1edfc` | Blue indicator |
| Stain check | `#ffe2e5` | Red indicator |
| Yarn Tail | `#f2f5a6` | Yellow indicator |
| Dimension | `#fff4de` | Amber indicator |
| UV / Tube Pattern | `#e3fcc3` | Light green indicator |

### Typography

- **Primary font:** `'Montserrat', sans-serif`
- Font loaded globally

### UI Framework Stack

| Library | Used For |
|---------|----------|
| Material-UI | Buttons, TextFields, Sliders, Icons, Modals |
| React Bootstrap | Tables, Forms, grid layout |
| React Data Table Component | Sortable/filterable data tables |
| ApexCharts | Line, bar, pie, donut charts |
| ReactCrop | Circular image cropping (teaching) |
| React Modal | Custom modal containers |
| Ant Design | DatePicker, TimePicker |
| rsuite | Toggle switches (lights) |
| styled-components | Sidebar theming |
| react-avatar | User avatar circles |
| ReactPlayer | Video tutorials |
| html2canvas + jsPDF | PDF report export |
| TransformWrapper | Image zoom/pan controls |

---

## 10. State Management Patterns

### Current (Old HMI)
- **No global state library** — no Redux, no Zustand
- React hooks (`useState`, `useRef`, `useEffect`) for all state
- `localStorage` for persistent data:
  - `userData` — JWT token
  - Master ID and Material ID passed between pages
- Component-level state for modals, forms, UI toggles
- Socket events update local component state directly

### New HMI (v3) — Recommended
- **Zustand** stores (already set up by backend team):
  - `authStore` — token, username, role
  - `inspectStore` — live inspection frame state
  - `analyticsStore` — shift analytics (new, create this)
  - `settingsStore` — operator settings (new, create this)
- Socket.IO events update Zustand stores, components subscribe reactively
- No localStorage for page-to-page data — use URL params or stores

---

## 11. Pages Missing from Current frontend_v3_guide.md

The following pages/features exist in the old HMI but are **not documented** in `frontend_v3_guide.md`. They need to be either:
- (a) Carried forward to v3 and documented, or
- (b) Explicitly marked as deprecated/removed.

| Page | Status | Notes |
|------|--------|-------|
| Data Collection (`/collection`) | Needs v3 spec | Training data capture from cameras |
| Camera Settings (`/setting/camera`) | Needs v3 spec | Camera config + live preview |
| PLC Settings (`/setting/plc`) | Needs v3 spec | PLC register mapping |
| Shift Settings (`/setting/shift`) | Partially covered | v3 guide has simplified shift_hours only |
| Lights Control (`/setting/lights`) | Needs v3 spec | Hardware light toggles |
| Config/Defect Selection (`/setting/configure`) | Needs v3 spec | Per-master defect enable/disable |
| Illumination Validation (`/setting/illumination`) | Needs v3 spec | Lighting adequacy test |
| Email Settings (`/setting/email`) | Needs v3 spec | SMTP + recipients |
| Error Proofing (`/setting/errorproof`) | Needs v3 spec | Defect example capture |
| Admin/User Management (`/adminn`) | Needs v3 spec | User CRUD + permissions |
| Profile / Edit Profile | Needs v3 spec | User profile view/edit |
| Support Tickets | Needs v3 spec | Internal ticketing |
| SPC Charts (`/chart`) | Needs v3 spec | Control charts (was demo data) |
| Gallery (`/sample`) | Needs v3 spec | Image browser |
| Tutorials (`/tutorial`) | Needs v3 spec | Video training |
| Verification flow | Needs v3 spec | Full verification workflow |
| Tube Verification (`/teaching/tubeverify`) | Needs v3 spec | Post-teaching verification |
| Annotation (external) | Decide | Was Docker container launch |
| InspectSetupModel | Needs v3 spec | Pre-inspection master selection |
| DataCaptureSetupModel | Needs v3 spec | Pre-capture configuration |
