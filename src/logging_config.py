"""
Centralized Logging Configuration — Azure Monitor Compatible.

Provides structured JSON logging for all Sieger v2.0 services.
Logs are formatted for easy ingestion by Azure Monitor / Log Analytics.

Features:
- JSON structured logging (Azure Monitor compatible)
- Console + rotating file handlers
- Correlation ID tracking for request tracing
- Performance metric logging
- Configurable log levels per module

Usage:
    from logging_config import setup_logging, get_logger

    # At application startup
    setup_logging(config)

    # In any module
    logger = get_logger(__name__)
    logger.info("Processing started", extra={"material_id": "MAT-001"})
"""

import json
import logging
import logging.handlers
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Thread-local storage for correlation IDs
_context = threading.local()


# =============================================================================
# Correlation ID Management
# =============================================================================

def get_correlation_id() -> str:
    """Get the current correlation ID for request tracing."""
    return getattr(_context, "correlation_id", None) or str(uuid.uuid4())


def set_correlation_id(correlation_id: str = None) -> str:
    """Set correlation ID for the current thread/request.

    Args:
        correlation_id: Optional ID to set. Generates new UUID if not provided.

    Returns:
        The correlation ID that was set.
    """
    if correlation_id is None:
        correlation_id = str(uuid.uuid4())
    _context.correlation_id = correlation_id
    return correlation_id


def clear_correlation_id():
    """Clear the correlation ID for the current thread."""
    if hasattr(_context, "correlation_id"):
        delattr(_context, "correlation_id")


# =============================================================================
# JSON Formatter (Azure Monitor Compatible)
# =============================================================================

class AzureMonitorJsonFormatter(logging.Formatter):
    """JSON formatter compatible with Azure Monitor / Log Analytics.

    Output format matches Azure Monitor's expected schema for custom logs.
    Each log entry is a single JSON line with standardized fields.
    """

    # Standard fields for Azure Monitor
    STANDARD_FIELDS = {
        "timestamp",
        "level",
        "logger",
        "message",
        "correlation_id",
        "service",
        "hostname",
        "process_id",
        "thread_id",
    }

    def __init__(self, service_name: str = "sieger-v2"):
        super().__init__()
        self.service_name = service_name
        self.hostname = os.uname().nodename if hasattr(os, "uname") else "unknown"

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        # Build base log entry
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
            "service": self.service_name,
            "hostname": self.hostname,
            "process_id": record.process,
            "thread_id": record.thread,
            "thread_name": record.threadName,
        }

        # Add source location for errors and above
        if record.levelno >= logging.ERROR:
            log_entry["source"] = {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            }

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "stacktrace": self.formatException(record.exc_info),
            }

        # Add any extra fields from the log call
        for key, value in record.__dict__.items():
            if key not in logging.LogRecord.__dict__ and not key.startswith("_"):
                if key not in self.STANDARD_FIELDS:
                    # Ensure value is JSON serializable
                    try:
                        json.dumps(value)
                        log_entry[key] = value
                    except (TypeError, ValueError):
                        log_entry[key] = str(value)

        return json.dumps(log_entry, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable formatter for console output."""

    COLORS = {
        "DEBUG": "\033[36m",      # Cyan
        "INFO": "\033[32m",       # Green
        "WARNING": "\033[33m",    # Yellow
        "ERROR": "\033[31m",      # Red
        "CRITICAL": "\033[35m",   # Magenta
    }
    RESET = "\033[0m"

    def __init__(self, use_colors: bool = True):
        super().__init__()
        self.use_colors = use_colors and sys.stdout.isatty()

    def format(self, record: logging.LogRecord) -> str:
        """Format log record for console."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        # Color the level name
        level = record.levelname
        if self.use_colors:
            color = self.COLORS.get(level, "")
            level = f"{color}{level:8}{self.RESET}"
        else:
            level = f"{level:8}"

        # Format the message
        message = record.getMessage()

        # Add correlation ID if available
        corr_id = get_correlation_id()
        if corr_id and hasattr(_context, "correlation_id"):
            corr_short = corr_id[:8]
        else:
            corr_short = "--------"

        # Build the log line
        log_line = f"{timestamp} | {level} | {corr_short} | {record.name} | {message}"

        # Add user-supplied extra fields only (skip internal LogRecord attributes)
        _INTERNAL_KEYS = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "taskName",
        }
        extras = []
        for key, value in record.__dict__.items():
            if key not in _INTERNAL_KEYS and not key.startswith("_"):
                extras.append(f"{key}={value}")
        if extras:
            log_line += f" | {' '.join(extras)}"

        # Add exception info
        if record.exc_info:
            log_line += f"\n{self.formatException(record.exc_info)}"

        return log_line


# =============================================================================
# Logging Setup
# =============================================================================

def setup_logging(
    config: dict = None,
    service_name: str = "sieger-v2",
    log_dir: str = "logs",
    log_level: str = "INFO",
    console_level: str = None,
    json_logs: bool = True,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 10,
) -> None:
    """Configure logging for the application.

    Args:
        config: Optional config dict (overrides other params if present).
        service_name: Service name for log entries.
        log_dir: Directory for log files.
        log_level: Default log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        console_level: Console log level (defaults to log_level).
        json_logs: Enable JSON formatted logs (Azure Monitor compatible).
        max_bytes: Max size per log file before rotation.
        backup_count: Number of rotated log files to keep.
    """
    # Load settings from config if provided
    if config:
        log_config = config.get("logging", {})
        log_level = log_config.get("level", log_level)
        console_level = log_config.get("console_level", console_level)
        log_dir = log_config.get("directory", log_dir)
        json_logs = log_config.get("json_logs", json_logs)
        max_bytes = log_config.get("max_bytes", max_bytes)
        backup_count = log_config.get("backup_count", backup_count)

    if console_level is None:
        console_level = log_level

    # Create log directory
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # Capture all, handlers filter

    # Remove existing handlers
    root_logger.handlers.clear()

    # --- Console Handler ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, console_level.upper()))
    console_handler.setFormatter(ConsoleFormatter(use_colors=True))
    root_logger.addHandler(console_handler)

    # --- JSON File Handler (for Azure Monitor) ---
    if json_logs:
        json_file = log_path / f"{service_name}.json.log"
        json_handler = logging.handlers.RotatingFileHandler(
            json_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        json_handler.setLevel(getattr(logging, log_level.upper()))
        json_handler.setFormatter(AzureMonitorJsonFormatter(service_name))
        root_logger.addHandler(json_handler)

    # --- Plain Text File Handler (for debugging) ---
    text_file = log_path / f"{service_name}.log"
    text_handler = logging.handlers.RotatingFileHandler(
        text_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    text_handler.setLevel(getattr(logging, log_level.upper()))
    text_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root_logger.addHandler(text_handler)

    # --- Error File Handler (errors only) ---
    error_file = log_path / f"{service_name}.error.log"
    error_handler = logging.handlers.RotatingFileHandler(
        error_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(AzureMonitorJsonFormatter(service_name))
    root_logger.addHandler(error_handler)

    # Reduce noise from third-party libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("ultralytics").setLevel(logging.WARNING)
    logging.getLogger("torch").setLevel(logging.WARNING)
    logging.getLogger("pyModbusTCP").setLevel(logging.WARNING)
    logging.getLogger("pyModbusTCP.client").setLevel(logging.WARNING)

    # Log startup
    logger = logging.getLogger(__name__)
    logger.info(
        "Logging initialized",
        extra={
            "log_level": log_level,
            "log_dir": str(log_path.absolute()),
            "json_logs": json_logs,
        }
    )


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the given name.

    Args:
        name: Logger name (typically __name__).

    Returns:
        Configured logger instance.
    """
    return logging.getLogger(name)


# =============================================================================
# Performance Logging
# =============================================================================

class PerformanceLogger:
    """Context manager for logging operation performance.

    Usage:
        with PerformanceLogger("yolo_detection", logger) as perf:
            result = detector.detect(frame)
            perf.add_metric("detections", len(result))
    """

    def __init__(
        self,
        operation: str,
        logger: logging.Logger,
        level: int = logging.INFO,
        warn_threshold_ms: float = None,
    ):
        self.operation = operation
        self.logger = logger
        self.level = level
        self.warn_threshold_ms = warn_threshold_ms
        self.start_time = None
        self.metrics = {}

    def __enter__(self):
        self.start_time = datetime.now(timezone.utc)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = (datetime.now(timezone.utc) - self.start_time).total_seconds() * 1000

        log_data = {
            "operation": self.operation,
            "duration_ms": round(elapsed, 2),
            **self.metrics,
        }

        # Check if operation was slow
        level = self.level
        if self.warn_threshold_ms and elapsed > self.warn_threshold_ms:
            level = logging.WARNING
            log_data["slow_operation"] = True

        if exc_type:
            log_data["success"] = False
            log_data["error"] = str(exc_val)
            self.logger.error(f"Operation '{self.operation}' failed", extra=log_data)
        else:
            log_data["success"] = True
            self.logger.log(
                level,
                f"Operation '{self.operation}' completed in {elapsed:.2f}ms",
                extra=log_data
            )

        return False  # Don't suppress exceptions

    def add_metric(self, name: str, value: Any):
        """Add a metric to be logged."""
        self.metrics[name] = value


# =============================================================================
# Inspection Event Logger
# =============================================================================

class InspectionEventLogger:
    """Structured logger for inspection events.

    Provides consistent logging format for all inspection-related events,
    making it easy to query in Azure Monitor / Log Analytics.
    """

    def __init__(self, logger: logging.Logger = None):
        self.logger = logger or get_logger("inspection.events")

    def log_frame_received(self, frame_id: int, material_id: str, camera: str = "vl"):
        """Log when a frame is received for inspection."""
        self.logger.info(
            "Frame received",
            extra={
                "event_type": "frame_received",
                "frame_id": frame_id,
                "material_id": material_id,
                "camera": camera,
            }
        )

    def log_detection_result(
        self,
        frame_id: int,
        detections: list,
        duration_ms: float,
    ):
        """Log YOLO detection results."""
        self.logger.info(
            f"Detection completed: {len(detections)} objects",
            extra={
                "event_type": "detection_result",
                "frame_id": frame_id,
                "detection_count": len(detections),
                "duration_ms": round(duration_ms, 2),
                "classes": [d.get("class_name") for d in detections] if detections else [],
            }
        )

    def log_inspection_result(
        self,
        frame_id: int,
        material_id: str,
        passed: bool,
        result_code: int,
        dimensions_ok: bool = None,
        stain_detected: bool = None,
        tube_pattern_ok: bool = None,
        duration_ms: float = None,
    ):
        """Log final inspection result."""
        self.logger.info(
            f"Inspection {'PASS' if passed else 'FAIL'} (code={result_code})",
            extra={
                "event_type": "inspection_result",
                "frame_id": frame_id,
                "material_id": material_id,
                "passed": passed,
                "result_code": result_code,
                "dimensions_ok": dimensions_ok,
                "stain_detected": stain_detected,
                "tube_pattern_ok": tube_pattern_ok,
                "duration_ms": round(duration_ms, 2) if duration_ms else None,
            }
        )

    def log_teaching_started(self, material_id: str, n_images: int):
        """Log when teaching process starts."""
        self.logger.info(
            f"Teaching started for '{material_id}'",
            extra={
                "event_type": "teaching_started",
                "material_id": material_id,
                "n_images": n_images,
            }
        )

    def log_teaching_completed(
        self,
        material_id: str,
        n_images: int,
        n_tubes_detected: int,
        duration_ms: float,
    ):
        """Log when teaching process completes."""
        self.logger.info(
            f"Teaching completed for '{material_id}'",
            extra={
                "event_type": "teaching_completed",
                "material_id": material_id,
                "n_images": n_images,
                "n_tubes_detected": n_tubes_detected,
                "duration_ms": round(duration_ms, 2),
            }
        )

    def log_plc_communication(
        self,
        operation: str,
        success: bool,
        result_code: int = None,
        error: str = None,
    ):
        """Log PLC communication events."""
        level = logging.INFO if success else logging.ERROR
        self.logger.log(
            level,
            f"PLC {operation}: {'success' if success else 'failed'}",
            extra={
                "event_type": "plc_communication",
                "operation": operation,
                "success": success,
                "result_code": result_code,
                "error": error,
            }
        )

    def log_camera_event(
        self,
        camera: str,
        event: str,
        success: bool = True,
        error: str = None,
    ):
        """Log camera-related events."""
        level = logging.INFO if success else logging.ERROR
        self.logger.log(
            level,
            f"Camera '{camera}' {event}",
            extra={
                "event_type": "camera_event",
                "camera": camera,
                "event": event,
                "success": success,
                "error": error,
            }
        )

    def log_health_check(
        self,
        status: str,
        components: dict,
    ):
        """Log health check results."""
        self.logger.info(
            f"Health check: {status}",
            extra={
                "event_type": "health_check",
                "status": status,
                **components,
            }
        )


# =============================================================================
# Request Context Manager
# =============================================================================

class RequestContext:
    """Context manager for request-scoped logging context.

    Usage:
        with RequestContext(material_id="MAT-001") as ctx:
            # All logs in this block will include the correlation ID
            logger.info("Processing request")
    """

    def __init__(self, correlation_id: str = None, **kwargs):
        self.correlation_id = correlation_id
        self.extra_context = kwargs
        self._previous_id = None

    def __enter__(self):
        self._previous_id = getattr(_context, "correlation_id", None)
        set_correlation_id(self.correlation_id)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._previous_id:
            _context.correlation_id = self._previous_id
        else:
            clear_correlation_id()
        return False

    @property
    def id(self) -> str:
        """Get the correlation ID for this context."""
        return get_correlation_id()


# =============================================================================
# Module Initialization
# =============================================================================

# Create default event logger instance
event_logger = InspectionEventLogger()


if __name__ == "__main__":
    # Demo/test the logging configuration
    setup_logging(log_level="DEBUG", service_name="sieger-test")

    logger = get_logger(__name__)

    # Test basic logging
    logger.debug("Debug message")
    logger.info("Info message")
    logger.warning("Warning message")
    logger.error("Error message")

    # Test with extra fields
    logger.info("Processing frame", extra={"frame_id": 1, "material_id": "MAT-001"})

    # Test with correlation ID
    with RequestContext(material_id="MAT-001") as ctx:
        logger.info(f"Request started with ID: {ctx.id}")
        logger.info("Processing...")

    # Test performance logging
    import time
    with PerformanceLogger("test_operation", logger, warn_threshold_ms=100) as perf:
        time.sleep(0.05)
        perf.add_metric("items_processed", 42)

    # Test event logger
    event_logger.log_inspection_result(
        frame_id=1,
        material_id="MAT-001",
        passed=True,
        result_code=1,
        dimensions_ok=True,
        stain_detected=False,
        tube_pattern_ok=True,
        duration_ms=85.5,
    )

    print("\nCheck logs/ directory for output files")
