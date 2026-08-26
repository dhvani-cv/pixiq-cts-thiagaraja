# Chapter 16: Testing

## 16.1 Overview

Testing covers unit tests, integration tests with mock hardware, and production verification. The mock system (`MockCamera`, `MockPLCClient`, `MockInspectionService`) enables full pipeline testing without cameras or PLC.

## 16.2 Mock System

### Mock Camera

`src/camera/mock_camera.py` — reads images from a folder in cyclic order:

- Configured via `config.json → cameras → folder_path` per camera
- Simulates 50 ms capture delay
- Cycles through images alphabetically (loops back to start)
- Returns blank 1920×1080 image if folder is empty

### Mock Capture Sequence

`src/camera/mock_capture.py` — same interface as `CaptureSequence`:

- `capture_part()` returns `CapturedImages(vl=, uv=, tail=)` from mock folders
- `flush_all()` is a no-op

### Mock PLC Client

`src/plc/mock_plc_client.py` — simulates PLC without Modbus:

- `poll_trigger_and_read()` returns fake material data after a delay
- Write methods are logged but have no effect
- Configurable `c2c_start` mode

### Mock Inspection Service

`src/services/mock_inspection_service.py`:

- Inherits from `InspectionService`
- Overrides `_init_cameras()` → mock cameras
- Overrides `_init_plc()` → mock PLC
- Adds 2s delay per cycle to simulate conveyor timing

## 16.3 Running Mock Service

```bash
# Set up test image folders
mkdir -p test_data/vl test_data/uv test_data/tail
# Copy sample images into each folder

# Run mock service (edit config.json cameras.*.folder_path first)
uv run python -m src.services.mock_inspection_service
```

The mock service starts the full Socket.IO server on port 5004, runs the inspection pipeline on test images, and streams results to the HMI — identical to production except hardware is simulated.

## 16.4 Unit Testing

### Test Framework

- `pytest` for test discovery and execution
- `pytest --cov` for coverage reporting

### Running Tests

```bash
# All tests
uv run pytest

# With coverage
uv run pytest --cov=src

# Specific module
uv run pytest tests/test_tube_pattern.py -v
```

## 16.5 Testing Individual Modules

### Single Image Inspection

```bash
curl -X POST http://localhost:5002/inspect \
    -H "Content-Type: application/json" \
    -d '{"material_id": "42", "image_path": "/path/to/test_image.jpg"}'
```

Returns `result_code`, `passed`, per-module results, and `annotated_image_base64`.

### Stain Detection

```bash
curl -X POST http://localhost:5002/stain \
    -H "Content-Type: application/json" \
    -d '{"training_id": "test", "mode": "detect", "image_folder": "/path/to/images"}'
```

### Tube Pattern

Test via the teaching API (port 8001):

```bash
curl -X POST http://localhost:8001/teach/tube \
    -F "material_id=42" \
    -F "images=@frame1.jpg" \
    -F "images=@frame2.jpg"
```

## 16.6 Production Verification

After deployment, verify each module:

### Health Checks

```bash
curl http://localhost:5002/health           # Quick check
curl http://localhost:5002/health/system     # Detailed system health
curl http://localhost:5002/health/plc        # PLC connection
curl http://localhost:5002/health/cameras    # All cameras
```

### Inspection Verification

1. Run 10 known-good cones → all should pass (`result_code=1`)
2. Run 2-3 known-defective cones → should fail (`result_code=2`)
3. Check `GET /results` for correct scores and defect types
4. Check `GET /results/{id}/audit` for correct annotations

### Per-Module Checks

| Module | Verify | Expected |
|--------|--------|----------|
| Dimension | Good cone → cone_diameter_mm within ±tolerance | result=1 |
| Stain | Clean cone → stain_score < threshold | stain_ok=1 |
| Stain | Stained cone → stain_score > threshold | stain_ok=0, defect_type=1 |
| Tube | Matching pattern → tube_distance < threshold | tube_ok=1 |
| Tube | Wrong pattern → tube_distance > threshold | tube_ok=0, defect_type=2 |
| UV | Good cone → radial_dip < 0.024 | uv_ok=1 |
| Tail | Tail present → confidence > threshold | tail_ok=1 |
| Tail | Tail absent → no detection | tail_ok=0, defect_type=5 |

## 16.7 Linting & Type Checking

```bash
# Lint
uv run ruff check src/

# Format
uv run ruff format src/

# Type check (production mode)
uv run mypy --strict src/
```

## 16.8 Profiling

### CPU Profiling

```bash
uv run python -m cProfile -s cumulative src/pipeline.py
```

### GPU Monitoring

```bash
nvidia-smi                    # One-shot GPU status
watch -n 1 nvidia-smi         # Continuous monitoring
```

### Memory Profiling

```bash
uv run python -m memray run src/pipeline.py
uv run memray flamegraph memray-*.bin
```

## 16.9 Camera Stream Statistics

The inspection service logs camera stream statistics (delivered, dropped, lost, incomplete frames) at the end of each session:

```python
capture_sequence.log_stream_statistics()
```

If `triggers > delivered`, the system is missing frames — investigate buffer underrun, CPU load, or network drops.
