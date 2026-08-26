# Chapter 12: Teaching

## 12.1 Overview

Teaching configures each inspection module with site-specific reference data. Modules require different effort levels:

| Module | When | Effort | Training? | Duration |
|--------|------|--------|-----------|----------|
| Tube | Autonomous | None | Yes (on-device) | Auto-triggered |
| Dimension | Installation | 5 min | No | Calibration only |
| UV | Installation | 10 min | No | Threshold only |
| Stain | Installation + periodic | 30-60 min | Yes (cloud A100) | 10-20 min training |
| Tail | Installation + on-demand | 20-40 min | Yes (YOLO) | 5-30 min training |

## 12.2 Tube Teaching — Fully Autonomous

**Source:** `src/teaching/tube_teacher.py` — `TubeTeacher` class

### Auto-Teaching Flow

```
PLC sends material_no=42
        │
        ▼
System checks masters/42.npz → NOT FOUND
        │
        ▼
Enter teaching mode (result=0 to PLC, no pass/fail)
Save 256×256 annular tube crops to captures/tube/auto_42/
        │
        ▼
After 20 crops: background thread fires TubeTeacher.teach()
        │
        ▼
Extract features per sample:
  ├── LAB a*b* histogram (32×32)
  ├── HSV H-S histogram (32×32)
  ├── Mean L* (lightness)
  ├── Shannon entropy
  ├── ResNet50 features (2048-dim)
  └── FFT intensity features (64-dim)
        │
        ▼
Compute mean features (normalized)
Compute pairwise distances → p99 × 1.5 = threshold
        │
        ▼
Save 42.npz atomically (temp file → os.replace)
Hot-load template (no restart needed)
Clear matcher cache
        │
        ▼
Emit teaching_alert Socket.IO event: "Template 42.npz created"
Next cone: scored normally
```

### teach() Method

```python
TubeTeacher.teach(frames, material_id, save_crops_dir=None, pre_cropped=False) → dict
```

- `pre_cropped=True`: frames are already 256×256 annular crops (auto-teaching)
- `pre_cropped=False`: YOLO extracts annular tube crop from full frames

Returns: `{material_id, n_frames, n_tubes_detected, template_path, color_threshold, ...}`

### Extend Workflow

```python
TubeTeacher.extend(frames, material_id) → dict
```

Appends new samples to existing `.npz` (max 3 extends):
1. Load existing features
2. Extract features from new frames
3. Concatenate arrays
4. Recompute means and threshold
5. Save updated `.npz` atomically

### Per-Pattern Threshold

```
threshold = p99(pairwise_distances) × 1.5
```

Pairwise distance formula:
```
color_dist = 0.7 × bhatt(LAB) + 0.3 × bhatt(HSV)
l_penalty = 0.50 × |mean_L[i] - mean_L[j]| / 100.0
combined = color_dist + l_penalty
```

### Configuration

```json
{
    "tube_teaching": {
        "tube_min_capture": 20
    }
}
```

- `tube_min_capture`: samples before auto-teach triggers (default 20)
- Reduce to 10 for short production runs
- Increase to 30-40 for more robust templates

## 12.3 Stain Teaching — Operator Triggered

### When to Teach

- Installation (minimum 200 good cones)
- Major production change (new yarn type, lighting change)
- False positive rate > 2%

### Flow

1. Enable teach mode: set `inspection.teach.stain_detection = true` in config via `PUT /config/teach { "stain_detection": true }`. System auto-captures stain crops during inspection.
2. Run 200-500 defect-free cones (256×256 annular crops saved automatically)
3. Disable teach mode: `PUT /config/teach { "stain_detection": false }`
4. `POST /cloud/upload` — uploads crops to Azure Blob (`sieger-training/{module}/{session_id}/`)
5. Train PatchCore on A100 cloud (10-20 min for 500 samples)
6. Download trained model to IPS host
7. `POST /teaching/stain` with `model_path`
8. `POST /restart` to load new model
9. Verify: good cones pass, stained cones fail

### Minimum Samples

200 good cones for a reliable PatchCore coreset.

## 12.4 UV Teaching — Installation Only

### Flow

1. Enable teach mode: `PUT /config/teach { "uv_inspection": true }` — run 10+ good cones
2. Check `radial_dip` values via `GET /results`
3. Typical good cone dip: 0.003-0.015
4. If good cones all < 0.018 → default threshold 0.024 is correct
5. If good cones higher → `threshold = max(good_dips) + 0.005`
6. `POST /teaching/uv` with new threshold
7. Disable teach mode: `PUT /config/teach { "uv_inspection": false }`

No model training. Physics-based algorithm. Recalibrate only if UV camera is replaced.

## 12.5 Tail Teaching — Installation + On-Demand

### Flow

1. Enable teach mode: `PUT /config/teach { "tail_inspection": true }` — run 50+ cones with tails
2. Disable teach mode: `PUT /config/teach { "tail_inspection": false }`
3. Re-enable teach mode with defect label: `PUT /config/teach { "tail_inspection": true }` — run 20+ cones without tails
4. Disable teach mode: `PUT /config/teach { "tail_inspection": false }`
5. Optionally annotate with `POST /teaching/annotate`
6. `POST /teaching/tail` — triggers YOLO retraining (5-30 min)
7. `POST /restart` to deploy new weights

Crops saved: top 60% of tail frame in `captures/tail/{session_id}/`.

## 12.6 Dimension Teaching — Installation Only

### Flow

1. Place calibration board in VL camera field of view
2. Measure `pixels_per_mm = reference_width_px / reference_width_mm`
3. `POST /teaching/dimension` with:
   ```json
   {
       "cone_diameter_mm": 230.0,
       "cone_tolerance_mm": 5.0,
       "tube_diameter_mm": 42.0,
       "tube_tolerance_mm": 2.0,
       "pixels_per_mm": 1.45
   }
   ```
4. Applied immediately, no restart required

Global scope — one calibration covers all materials. Camera mounting must not change after calibration.

## 12.7 Teaching Database

Teaching events are tracked in the `teaching_sessions` SQLite table:

| Column | Type | Description |
|--------|------|-------------|
| `teaching_id` | TEXT (UUID) | Primary key |
| `module` | TEXT | tube, stain, dimension, uv, tail |
| `scope_key` | TEXT | material_id for tube/dimension, "global" for stain/uv/tail |
| `status` | TEXT | training, active, superseded, failed |
| `n_samples` | INTEGER | Training image count |
| `model_path` | TEXT | .npz or model.pt path |
| `threshold` | REAL | Computed threshold |
| `extend_count` | INTEGER | Times /extend called (tube only, max 3) |
| `validation_json` | TEXT | JSON validation report |

## 12.8 Quick Installation Sequence

### Day 1 — Hardware + Calibration (1 hour)

1. Mount cameras, run cables
2. `GET /health/cameras` — all 3 connected
3. `GET /health/plc` — PLC connected
4. Dimension calibration: measure pixels_per_mm → `POST /teaching/dimension`
5. UV calibration: run 10 good cones → verify threshold or `POST /teaching/uv`

### Day 1-2 — Stain Teaching (200+ cones)

1. Enable teach mode: `PUT /config/teach { "stain_detection": true }`
2. Run 200+ good cones (system auto-captures stain crops)
3. Disable teach mode: `PUT /config/teach { "stain_detection": false }` → `POST /cloud/upload`
4. Train on A100 → download model → `POST /teaching/stain` → restart

### Day 2 — Tail Teaching (1 hour)

1. Enable teach mode: `PUT /config/teach { "tail_inspection": true }` — capture 50+ with tail, then 20+ without
2. Disable teach mode: `PUT /config/teach { "tail_inspection": false }`
3. `POST /teaching/tail` → `POST /restart`

### Day 2+ — Tube (autonomous)

- Start production — system auto-teaches each material_id as it appears
- Monitor HMI for `teaching_alert` events
- Verify first 10 cones after each template creation
