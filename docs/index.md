# Sieger pixIQ — Yarn Cone Inspection System (CV Module)

Industrial yarn cone quality inspection system. Inspects yarn cones in real time using three Basler GigE cameras on a Jetson Orin NX 16GB, communicating pass/fail results to a Siemens PLC over Modbus TCP. Inference via TensorRT FP16.

**Five inspection checks:** tube pattern identity, stain/contamination, UV thread mixup, yarn tail presence, cone/tube dimensions.

---

## Architecture

Three native services — no Docker.

| Service | Port | Protocol | Entry Point |
|---------|------|----------|-------------|
| **CV API** | 5002 | HTTP/REST (FastAPI) | `run_api.py` |
| **Inspection Service** | 5004 | WebSocket (Socket.IO) | `run_inspection.py` |
| **Report Service** (Node.js) | 5001 | HTTP | `reportservice/` |

HMI: React web app served as static files from nginx — connects to port 5002 (API) and 5004 (live frames). Any browser on the same LAN can access it.

Results stored in SQLite (`sieger_data/sieger.db`). No MongoDB, no daemon required.

---

## Hardware

| Component | Spec |
|-----------|------|
| Compute | NVIDIA Jetson Orin NX 16GB (ARM64, JetPack 6.x) |
| OS | Ubuntu 22.04 LTS (L4T) |
| VL Camera | Basler a2A2600-20gcPRO (2600×2048, 25mm) — 192.168.1.160 |
| UV Camera | Basler acA1920-40gc (1920×1200, 16mm) — 192.168.1.161 |
| Tail Camera | Basler a2A1920-40gc (1920×1200, 25mm) — 192.168.1.162 |
| Camera SDK | pypylon (Basler pylon) |
| PLC | Siemens S7, Modbus TCP at 192.168.1.110:502 |
| HMI | Separate all-in-one touchscreen desktop |

---

## Inspection Pipeline

```
PLC trigger
    │
    ▼
Sequential camera fire: VL → Tail → UV
    │
    ├── VL frame ──► YOLO12 (cone + tube bbox)
    │                    ├── Tube pattern  (Color NN + FFT NN, per-pattern threshold)
    │                    ├── Stain         (PatchCore WideResNet50, annular cone crop)
    │                    └── Dimension     (pixels_per_mm → cone/tube diameter vs tolerance)
    │
    ├── UV frame ──► Radial log(G/B) dip > 0.024 → thread mixup defect
    │
    └── Tail frame ► YOLO yarn_tail detector → no detection → defect
                         │
                         ▼
                 result: 1=Good  2=Defect  3=Error  0=Teaching
                         │
                         ├── PLC result register (40003)
                         ├── SQLite inspection row
                         └── Socket.IO → HMI (live frames + result)
```

---

## Teaching System

| Module | Scope | Triggered by | Training location |
|--------|-------|-------------|-------------------|
| Tube pattern | Per material_id | **Automatic** on unknown material | On-device |
| Stain | Global | Operator (installation) | Cloud (A100) |
| UV | Global | Operator (installation) | None — threshold only |
| Tail | Global | Operator (on degradation) | Cloud (A100) |
| Dimension | Global | Operator (installation) | On-device (IQR filter) |

**Autonomous tube teaching:** When PLC sends a `material_no` with no existing template → system saves 256×256 annular tube crops → at 20 captures → background thread trains Color NN + FFT NN → saves `{material_id}.npz` → hot-loads → begins inspecting. No operator action needed.

---

## Quick Start

```bash
# Create venv with system site packages (for TensorRT access on Jetson)
uv venv --python 3.10 --system-site-packages

# Install dependencies
uv sync

# Initialize data directories + SQLite schema (first run only)
uv run python src/init_app.py

# Start CV API
uv run python run_api.py

# Start inspection service
uv run python run_inspection.py
```

Or via systemd (production):
```bash
sudo systemctl start sieger-inspection sieger-api
sudo systemctl status sieger-api
```

---

## Project Structure

```
sieger-ghcl-cv/
├── src/
│   ├── api/
│   │   └── main.py              # FastAPI — teaching, results, config, health
│   ├── inspection/
│   │   ├── visible.py           # VL master orchestrator
│   │   ├── uv_inspection.py     # UV thread mixup (radial log G/B)
│   │   ├── tail_inspection.py   # Yarn tail (YOLO)
│   │   ├── stain_detector.py    # PatchCore stain detection
│   │   ├── tube_pattern.py      # Color NN + FFT NN tube verification
│   │   ├── dimension_check.py   # Cone/tube diameter measurement
│   │   ├── yolo_detector.py     # YOLO12 + annular ROI extraction
│   │   ├── color_matching/      # Color histogram + entropy pipeline
│   │   └── data_types.py        # Shared dataclasses
│   ├── teaching/
│   │   └── tube_teacher.py      # Tube pattern enrollment (p99 threshold)
│   ├── services/
│   │   └── inspection_service.py # Socket.IO service, PLC loop, auto-teaching
│   ├── cloud/
│   │   └── uploader.py          # Azure Blob upload for training data
│   ├── camera/                  # Basler pypylon camera wrappers
│   ├── plc/                     # Modbus TCP client
│   ├── db/                      # SQLite schema + writer
│   ├── logging_config.py
│   ├── init_app.py
│   └── config.json              # Runtime config (not in git — contains SAS token)
├── weights/                     # YOLO model weights (DVC tracked)
├── models/                      # PatchCore models (DVC tracked)
├── deploy/
│   ├── systemd/                 # sieger-api.service, sieger-inspection.service
│   └── nginx/                   # sieger.conf
├── docs/
│   ├── project_context.md       # Full technical reference ← start here
│   ├── api_reference.md         # All REST + socket.io endpoints
│   ├── teaching_guide.md        # Operator teaching guide (all 5 modules)
│   ├── deployment.md            # Installation and deployment
│   ├── timing.md                # Cycle time analysis
│   ├── plc_handshake_flow.md    # PLC register map + handshake
│   ├── stain_detection.md       # PatchCore stain module details
│   ├── storage_layout.md        # sieger_data/ directory layout
│   └── CHANGELOG.md             # Version history
├── pyproject.toml
└── CLAUDE.md
```

---

## Configuration

All runtime config in `src/config.json` (not committed — contains Azure SAS token).

Copy from template on new machine:
```bash
cp src/config.json.example src/config.json
# Edit: PLC IP, camera serials, data_root, cloud.sas_token, cloud.customer_id
```

See `docs/deployment.md` for complete setup instructions.

---

## Documentation

| Doc | Contents |
|-----|----------|
| `docs/project_context.md` | Full pipeline reference — read this first |
| `docs/api_reference.md` | All API endpoints with request/response schemas |
| `docs/teaching_guide.md` | How to teach each module (operator + developer) |
| `docs/deployment.md` | Site installation, systemd, SAS tokens |
| `docs/timing.md` | Cycle time budget and camera timing analysis |
| `docs/plc_handshake_flow.md` | PLC register map and sequence diagrams |
| `docs/stain_detection.md` | PatchCore stain module — training and tuning |
| `docs/CHANGELOG.md` | Version history |
