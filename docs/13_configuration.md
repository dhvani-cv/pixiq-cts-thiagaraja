# Chapter 13: Configuration

## 13.1 Overview

All configuration lives in `src/config.json`. Operator settings are now managed via config.json write API. Shift hours, inspection tasks, and teach toggles all live in config.json and are read/written via `PUT /config/*` endpoints.

## 13.2 Config File Structure

```json
{
    "data_root": "/home/msiegerips/sieger_data",
    "logging": { ... },
    "plc": { ... },
    "cameras": { ... },
    "inspection": { ... },
    "teaching": { ... },
    "api": { ... },
    "service": { ... },
    "reportservice": { ... }
}
```

## 13.3 data_root

```json
"data_root": "/home/msiegerips/sieger_data"
```

Machine-specific path to all runtime data: masters, captures, audit images, SQLite database. Never committed to git — differs per deployment.

## 13.4 Logging

```json
{
    "logging": {
        "level": "INFO",
        "console_level": "INFO",
        "directory": "logs",
        "json_logs": true,
        "max_bytes": 10485760,
        "backup_count": 10
    }
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `level` | INFO | Root log level (file + JSON) |
| `console_level` | INFO | Console output level |
| `directory` | logs | Log file directory |
| `json_logs` | true | Enable Azure Monitor JSON log files |
| `max_bytes` | 10 MB | Max size before rotation |
| `backup_count` | 10 | Number of rotated log files to keep |

## 13.5 PLC

```json
{
    "plc": {
        "host": "192.168.1.110",
        "port": 502,
        "unit_id": 1,
        "timeout": 3.0,
        "poll_interval": 0.1,
        "registers": {
            "input": { "sample_counter": 0, "trigger": 1, "c2c_start": 7, "material_no": 8, "basket_no": 11, "loader_id": 12 },
            "output": { "result": 2, "camera_error": 14, "ips_status": 15, "basket_no_echo": 16, "material_no_echo": 17, "loader_no_echo": 18, "cycle_start": 9, "defect_type": 19, "ack": 20 },
            "light": { "uv": 4, "vl": 5, "yarntail": 6 }
        }
    }
}
```

See [Chapter 3: PLC Communication](03_plc_communication.md) for register details.

**Note:** `poll_trigger_and_read()` uses hardcoded array indices (0, 1, 7, 8, 11, 12) that assume the standard register layout.

## 13.6 Cameras

```json
{
    "cameras": {
        "VL": { "serial": "4108843636", "exposure": 11000, "timeout": 30000, "trigger_debounce_us": 200000 },
        "UV": { "serial": "4110025657", "ip": "10.0.0.10", "exposure": 70000, "timeout": 30000 },
        "Tail": { "serial": "4108843650", "exposure": 8000, "timeout": 30000 }
    }
}
```

Exposure is in microseconds. Timeout is frame grab timeout in milliseconds.

## 13.7 Inspection

```json
{
    "inspection": {
        "weights": { "visible": "weights/visible_yolo.pt", "uv": "weights/uv_yolo.pt" },
        "patchcore_model": "models/patchcore",
        "database": "data/db/materials.db",
        "recipe_dir": "data/recipes",
        "pixels_per_mm": 4.3,
        "yolo_conf": 0.6,
        "stain_threshold": 0.5,
        "tasks": {
            "dimension_check": true,
            "stain_detection": true,
            "tube_pattern": true,
            "uv_inspection": true,
            "tail_inspection": true
        },
        "uv_inspection": { ... },
        "tail_inspection": { ... },
        "tube_pattern": { ... }
    }
}
```

### Task Enable/Disable

Set any task to `false` to skip it during inspection. Disabled tasks are treated as OK (not failed).

### UV Inspection

| Key | Default | Description |
|-----|---------|-------------|
| `yolo_weights` | weights/uv_yolo.pt | UV YOLO model |
| `yolo_conf` | 0.3 | Detection confidence (lower — UV is noisier) |
| `radial_dip_threshold` | 0.024 | Max radial dip before flagging mixup |
| `outer_margin` | 0.1 | Fraction of radius to exclude at edge |

### Tail Inspection

| Key | Default | Description |
|-----|---------|-------------|
| `yolo_weights` | weights/yarn_tail_v3.pt | Tail YOLO model |
| `yolo_conf` | 0.5 | Detection confidence |

### Tube Pattern

| Key | Default | Description |
|-----|---------|-------------|
| `template_dir` | data/templates/tube | .npz template directory |
| `bilateral_d` | 9 | Bilateral filter diameter |
| `bilateral_sigma_color` | 75 | Color sigma |
| `bilateral_sigma_space` | 75 | Space sigma |
| `inner_crop_pct` | 0.10 | Inner radius crop (edge artifacts) |
| `outer_crop_pct` | 0.10 | Outer radius crop |
| `inner_ratio` | 0.80 | Inner hole as fraction of outer radius |
| `fft_weight` | 0.3 | FFT contribution (0.3 = 30% FFT, 70% color) |
| `verification_mode` | false | true = distance to expected; false = nearest-neighbor |
| `max_entropy_delta` | 0 | Entropy gate (0 = disabled) |
| `max_bhatt_distance` | 0.6 | Distance gate (classification mode) |

## 13.8 Teaching

```json
{
    "teaching": {
        "template_dir": "data/templates/tube",
        "device": "auto"
    }
}
```

`device`: "auto" (CUDA if available), "cuda", or "cpu".

## 13.9 Service Ports

```json
{
    "api": { "host": "0.0.0.0", "port": 5002, "cors_origins": ["*"] },
    "service": { "host": "0.0.0.0", "port": 5004, "cors_origins": "*",
        "stream": {
            "report_width": 1280, "report_height": 720, "report_quality": 80,
            "live_width": 640, "live_height": 480, "live_quality": 70, "live_fps": 10
        }
    },
    "reportservice": { "url": "http://localhost:5001", "enabled": true }
}
```

## 13.10 Config Write Endpoints

Operator-editable settings are managed via config.json write API:

| Endpoint | Description |
|----------|-------------|
| `PUT /config/tasks` | Enable/disable inspection tasks (`dimension_check`, `stain_detection`, `tube_pattern`, `uv_inspection`, `tail_inspection`) |
| `PUT /config/teach` | Toggle teach mode per module (`stain_detection`, `uv_inspection`, `tail_inspection`, `dimension_check`) |
| `PUT /config/shift` | Set shift hours (e.g., `{ "shift_hours": 8.0 }`) |
| `PUT /config/cameras` | Update camera exposure, timeout, etc. |
| `PUT /config/plc` | Update PLC host, port, register map |

### Teach Section in config.json

```json
{
    "inspection": {
        "teach": {
            "stain_detection": false,
            "uv_inspection": false,
            "tail_inspection": false,
            "dimension_check": false
        }
    }
}
```

When a teach toggle is `true`, the system auto-captures crops for that module during normal inspection. Tube teaching is fully autonomous and does not use this toggle.

## 13.11 Config Change Workflow

For operator-editable settings, use the `PUT /config/*` endpoints — changes are applied immediately, no restart required.

For other config sections (logging, service ports, etc.), edit `src/config.json` manually:

```bash
# 1. Edit config.json
nano src/config.json

# 2. Validate JSON
python -c "import json; json.load(open('src/config.json'))"

# 3. Restart affected service
sudo systemctl restart sieger-api
# or
sudo systemctl restart sieger-inspection
```

## 13.12 Recipe Files

Material recipes are stored as JSON files in `data/recipes/{material_id}.json`:

```json
{
    "material_id": "42",
    "master_name": "BLUE_DIAMOND",
    "cone_diameter_mm": 60.0,
    "tube_diameter_mm": 32.0,
    "cone_tolerance_mm": 2.0,
    "tube_tolerance_mm": 1.5,
    "created_at": "2026-01-15T10:30:00+00:00",
    "updated_at": "2026-01-15T11:45:00+00:00"
}
```

Managed via `RecipeStore` (`src/inspection/recipe_store.py`). Atomic writes (temp file + `os.replace()`) prevent corruption from mid-write crashes.
