# Chapter 15: Logging

## 15.1 Overview

The system uses centralized structured logging with Azure Monitor-compatible JSON output, color-coded console output, and performance tracking.

**Source:** `src/logging_config.py`

## 15.2 Log Handlers

`setup_logging()` configures four handlers:

| Handler | Format | File | Purpose |
|---------|--------|------|---------|
| Console | Colored human-readable | — | Developer/operator terminal |
| JSON file | Azure Monitor JSON | `logs/sieger.json.log` | Structured log ingestion |
| Text file | Plain text | `logs/sieger.log` | Fallback readable logs |
| Error file | JSON | `logs/sieger.error.json.log` | Errors only |

All file handlers use `RotatingFileHandler` with configurable `max_bytes` (default 10 MB) and `backup_count` (default 10 rotated files).

## 15.3 Console Format

Color-coded by level:

| Level | Color |
|-------|-------|
| DEBUG | Cyan |
| INFO | Green |
| WARNING | Yellow |
| ERROR | Red |
| CRITICAL | Magenta |

Format:
```
2026-04-01 10:30:00.123 | INFO  | abc12345 | inspection.visible | Detected 2 objects | detections=2
```

Fields: timestamp, level, correlation_id (8 chars), logger name, message, extra fields.

## 15.4 JSON Format (Azure Monitor)

Each log line is a JSON object:

```json
{
    "timestamp": "2026-04-01T10:30:00.123Z",
    "level": "INFO",
    "logger": "inspection.visible",
    "message": "Detected 2 objects",
    "correlation_id": "abc12345-def6-7890",
    "service": "sieger-inspection",
    "hostname": "ghcl-ips",
    "process_id": 12345,
    "thread_id": 67890,
    "thread_name": "InspectionWorker",
    "detections": 2
}
```

For ERROR+, source location is added (`source_file`, `source_line`, `source_func`). Exception info is included if present.

## 15.5 Correlation ID Tracking

Every request/inspection cycle gets a correlation ID (UUID) that propagates through all log messages:

```python
# Set correlation ID for request scope
with RequestContext(material_id="42") as ctx:
    logger.info("Processing cone")  # Includes correlation_id

# Or manually
set_correlation_id("custom-id-123")
logger.info("Manual correlation")
clear_correlation_id()
```

The FastAPI middleware (`add_request_context`) reads `X-Correlation-ID` from request headers or generates a new UUID.

## 15.6 Performance Logging

`PerformanceLogger` context manager tracks operation duration:

```python
with PerformanceLogger("yolo_detection", logger) as perf:
    result = detector.detect(frame)
    perf.add_metric("detections", len(result))
# Logs: "yolo_detection completed in 32.1ms | detections=2"
```

- Warns if duration exceeds `warn_threshold_ms`
- Logs success/failure
- Used for: YOLO inference, PatchCore, tube pattern, PLC reads, camera capture

## 15.7 Inspection Event Logger

`InspectionEventLogger` provides structured logging for inspection-specific events:

| Method | Event |
|--------|-------|
| `log_frame_received()` | Camera frame received |
| `log_detection_result()` | YOLO detection results with duration |
| `log_inspection_result()` | Final pass/fail with all module scores |
| `log_teaching_started()` | Teaching session initiated |
| `log_teaching_completed()` | Teaching session finished |
| `log_plc_communication()` | PLC read/write with success flag |
| `log_camera_event()` | Camera connect/disconnect/error |
| `log_health_check()` | System health status |

## 15.8 Library Noise Reduction

`setup_logging()` sets these libraries to WARNING level to reduce noise:

- `urllib3`
- `PIL`
- `ultralytics`
- `torch`
- `pyModbusTCP`

## 15.9 Configuration

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

## 15.10 Viewing Logs

```bash
# Live service logs via systemd
journalctl -u sieger-inspection -f

# JSON log file (structured)
tail -f logs/sieger.json.log | python -m json.tool

# Errors only
tail -f logs/sieger.error.json.log

# Plain text (fallback)
tail -f logs/sieger.log
```
