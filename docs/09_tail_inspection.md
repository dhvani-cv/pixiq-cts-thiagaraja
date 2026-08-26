# Chapter 9: Tail Inspection

## 9.1 Overview

Tail inspection verifies that the yarn tail (free end) is present at the base of the cone. A missing tail means the operator must re-thread the machine. Detection uses a dedicated YOLO model on the tail camera (top-down view with dedicated lighting).

**Source:** `src/inspection/tail_inspection.py` — `TailInspection` class

## 9.2 Algorithm

```
Tail Frame (1920×1200, top-down view)
        │
        ▼
YOLO (tail model) → yarn_tail detection
        │
        ├── No detection → DEFECT (missing tail)
        │
        └── Detection found
                │
                ├── confidence < threshold → DEFECT (treat as no detection)
                │
                └── confidence ≥ threshold → PASS (tail present)
```

Simple binary detection — no complex image processing. The dedicated camera angle and lighting make this a clean YOLO task.

## 9.3 Result Fields

```python
@dataclass
class TailResult:
    tail_detected: bool          # Tail found with confidence > threshold
    confidence: float            # Detection confidence (0 if not detected)
    bbox: Optional[tuple]        # (x1, y1, x2, y2) if detected
    model_loaded: bool           # False if YOLO model failed to load
```

### Result Code Mapping

| Condition | result_code | defect_type |
|-----------|-------------|-------------|
| `model_loaded=False` | 3 (Error) | — |
| `tail_detected=False` | 2 (Defect) | 5 (Missing Tail) |
| `tail_detected=True` | 1 (Good) | 0 |

## 9.4 Configuration

```json
{
    "tail_inspection": {
        "yolo_weights": "weights/yarn_tail_v3.pt",
        "yolo_conf": 0.5
    }
}
```

Note: `use_padding=False` — the tail YOLO model does not use 1.6:1 aspect ratio padding (unlike the VL and UV models).

## 9.5 Consecutive Failure Guard

After 5 consecutive `detection_failed` results, `logger.error()` fires — this indicates a hardware or model issue, not a real defect (unlikely that 5 consecutive cones genuinely have no tail).

## 9.6 Training

### When to Train

- Initial installation
- False positive rate increases (detecting tail when absent)
- False negative rate increases (tail present but not detected)

### Training Data

| Label | Minimum | Description |
|-------|---------|-------------|
| With tail | 50+ cones | Normal production cones |
| Without tail | 20+ cones | Tails manually removed |

### Training Flow

1. Enable teach mode: `PUT /config/teach { "tail_inspection": true }` — run cones with tails
2. Disable teach mode: `PUT /config/teach { "tail_inspection": false }`
3. Re-enable teach mode with defect label: `PUT /config/teach { "tail_inspection": true }` — run cones without tails
4. Disable teach mode: `PUT /config/teach { "tail_inspection": false }`
5. Optionally annotate with `POST /teaching/annotate` (YOLO bbox labels)
6. `POST /teaching/tail` — triggers YOLO retraining
7. `POST /restart` to deploy new weights

### Crop Saving

Top 60% of the tail frame is saved to `sieger_data/captures/tail/{session_id}/` during capture sessions. The bottom 40% is cropped out (conveyor belt, not relevant).

## 9.7 Troubleshooting

| Issue | Check |
|-------|-------|
| All cones flagged missing tail | Camera connected? (`GET /health/camera/tail`), tail light on? |
| 5+ consecutive detection_failed | YOLO confidence below threshold — check lighting, model loading |
| Good cones randomly flagged | Tail moves out of frame — adjust camera angle or raise conf threshold |
| Model not loading | Check `weights/yarn_tail_v3.pt` exists |
