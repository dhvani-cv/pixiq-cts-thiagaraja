"""
Inspection Service — Real-time inspection engine with Socket.IO.

Handles:
- Start/stop inspection commands from UI
- Camera capture and live feed
- CV/ML inspection pipeline (Visible + UV + Tail)
- PLC communication (read sensors, write results)
- Real-time streaming of results to UI (1280x720 JPEG)

Port: 5004 (configurable)

Socket.IO Events:
    Incoming (UI → Service):
        - start_inspection: Start inspection (type=capture or type=inspect)
        - stop_inspection: Stop inspection
        - connect_cam: Camera live feed / exposure change
        - on_light / off_light: Control lights via PLC
        - light_status: Read light status
        - check_plc: Test PLC connection
        - get_plc_info: Read all PLC registers
        - health_check: System health
        - check_cameras: Camera health
        - error_proof: Illumination check/save
        - error_proof_defect: Error proofing

    Outgoing (Service → UI):
        - send_image: Stream results (images, defects, status)
        - plc_status: PLC connection status
        - error: Error messages

Image Streaming:
    - Report images: 1280x720, JPEG quality 80 (~150 KB)
    - Live feed: 640x480, JPEG quality 70 (~40 KB at ~10 FPS)
    - Encoding: base64 string in JSON payload

Usage:
    uv run python run_inspection.py
"""

import base64
import json
import queue
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Optional
import os

import cv2
import eventlet
import numpy as np
import socketio

eventlet.monkey_patch()

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Centralized logging
from logging_config import (
    setup_logging,
    get_logger,
    set_correlation_id,
    PerformanceLogger,
    InspectionEventLogger,
    RequestContext,
)

# Will be initialized in __init__ with config
logger = get_logger(__name__)
event_logger = InspectionEventLogger()

from db.schema import init_db
from db.writer import InspectionWriter, InspectionRecord

DEFECT_TYPE_KEYS = (
    "stain",
    "tube_mismatch",
    "cone_dia",
    "tail",
    "tube_dia",
    "uv_mixup",
)

DEFECT_KEY_MAP = {
    "Stain": "stain",
    "Wrong Pattern": "tube_mismatch",
    "Wrong Cone Diameter": "cone_dia",
    "Missing Tail": "tail",
    "Wrong Tube Diameter": "tube_dia",
    "Thread Mixup": "uv_mixup",
}


def _empty_defect_types() -> dict[str, int]:
    return {key: 0 for key in DEFECT_TYPE_KEYS}


def _defect_key(defect_name: str) -> str | None:
    return DEFECT_KEY_MAP.get((defect_name or "").strip())


@dataclass
class AnalyticsState:
    """In-memory running analytics updated on every inspection write.

    Tracks current-shift and session-total counters.
    Reset at shift boundary. Thread-safe via a dedicated lock.
    """
    lock: threading.Lock = field(default_factory=threading.Lock)
    shift_start: str = ""
    shift_hours: float = 8.0
    shift_total: int = 0
    shift_good: int = 0
    shift_defect: int = 0
    shift_error: int = 0
    shift_defects: dict = field(default_factory=dict)
    shift_per_material: dict = field(default_factory=dict)
    session_total: int = 0
    session_good: int = 0
    session_defect: int = 0

    def update(self, record: "InspectionRecord") -> None:
        """Update counters from a written InspectionRecord. Thread-safe."""
        from datetime import datetime, timezone as _tz
        with self.lock:
            now = datetime.now(_tz.utc).isoformat()
            if not self.shift_start:
                self.shift_start = now
            try:
                start_dt = datetime.fromisoformat(self.shift_start)
                elapsed_h = (datetime.now(_tz.utc) - start_dt).total_seconds() / 3600
                if elapsed_h >= self.shift_hours:
                    self._reset_shift(now)
            except Exception:
                pass
            self.session_total += 1
            if record.result_code == 1:
                self.session_good += 1
            elif record.result_code == 2:
                self.session_defect += 1
            if record.trial_mode:
                return
            self.shift_total += 1
            if record.result_code == 1:
                self.shift_good += 1
            elif record.result_code == 2:
                self.shift_defect += 1
                dtype = record.defect_type or "unknown"
                for d in dtype.split(","):
                    key = _defect_key(d)
                    if key:
                        self.shift_defects[key] = self.shift_defects.get(key, 0) + 1
            elif record.result_code == 3:
                self.shift_error += 1
            mid = record.material_id
            if mid not in self.shift_per_material:
                self.shift_per_material[mid] = {
                    "total": 0,
                    "good": 0,
                    "defect": 0,
                    "defect_types": _empty_defect_types(),
                }
            self.shift_per_material[mid]["total"] += 1
            if record.result_code == 1:
                self.shift_per_material[mid]["good"] += 1
            elif record.result_code == 2:
                self.shift_per_material[mid]["defect"] += 1
                dtype = record.defect_type or ""
                for d in dtype.split(","):
                    key = _defect_key(d)
                    if key:
                        self.shift_per_material[mid]["defect_types"][key] += 1

    def _reset_shift(self, now: str) -> None:
        """Reset shift counters. Must be called with lock held."""
        self.shift_start = now
        self.shift_total = 0
        self.shift_good = 0
        self.shift_defect = 0
        self.shift_error = 0
        self.shift_defects = {}
        self.shift_per_material = {}

    def snapshot(self) -> dict:
        """Return a JSON-serialisable analytics snapshot. Thread-safe."""
        with self.lock:
            rejection_pct = (
                round(self.shift_defect * 100.0 / self.shift_total, 2)
                if self.shift_total > 0 else 0.0
            )
            return {
                "shift": {
                    "start": self.shift_start,
                    "total": self.shift_total,
                    "good": self.shift_good,
                    "defect": self.shift_defect,
                    "error": self.shift_error,
                    "rejection_rate_pct": rejection_pct,
                },
                "defect_breakdown": dict(self.shift_defects),
                "per_material": {
                    mid: dict(v) for mid, v in self.shift_per_material.items()
                },
                "session_total": self.session_total,
                "session_good": self.session_good,
                "session_defect": self.session_defect,
            }




class InspectionState(IntEnum):
    """State machine states."""
    IDLE = 0
    CAPTURE = 1           # Data capture mode (save images, no inspection)
    INSPECT = 2           # Full inspection mode
    LIVE_FEED = 3         # Stream camera feed to UI
    EXPOSURE = 4          # Change camera exposure
    LIGHT_ON = 5
    LIGHT_OFF = 6
    LIGHT_STATUS = 7
    ILLUM_CHECK_VL = 8
    ILLUM_CHECK_UV = 9
    ILLUM_CHECK_TAIL = 10
    ILLUM_SAVE_VL = 11
    ILLUM_SAVE_UV = 12
    ILLUM_SAVE_TAIL = 13
    PLC_CONFIG = 14
    ERROR_PROOF = 15


@dataclass
class ServiceState:
    """Shared state for the inspection service."""
    state: InspectionState = InspectionState.IDLE
    camera_id: int = 1
    exposure: int = 11000
    save_exposure: bool = False
    light_id: int = 0
    material_id: str = ""
    capture_material_ids: set = field(default_factory=set)  # empty = save all
    capture_session_id: str = ""       # active capture session UUID (empty = no active session)
    capture_module: str = ""          # module being captured: tube|stain|uv|tail|dimension
    inspection_material_ids: set = field(default_factory=set)  # empty = inspect all
    machine_id: str = ""
    loader_id: int = 0
    trial_mode: bool = False
    error_proof_data: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # Auto-teaching state — tube pattern only
    # capture_counts: per material_id image count during auto-capture
    # auto_teaching_triggered: materials where background training already fired (avoid double-trigger)
    capture_counts: dict = field(default_factory=dict)
    auto_teaching_triggered: set = field(default_factory=set)

    # Connection status tracking
    plc_connected: bool = False
    cam_vl_connected: bool = False
    cam_uv_connected: bool = False
    cam_tail_connected: bool = False
    models_loaded: bool = False

    # Teach capture session tracking (manifest-driven workflow)
    teach_session_start_time: float = 0.0   # monotonic time when teach session started
    teach_capture_count: int = 0            # images captured in current teach session

    def set_state(self, new_state: InspectionState):
        with self.lock:
            self.state = new_state

    def get_state(self) -> InspectionState:
        with self.lock:
            return self.state


class InspectionService:
    """Real-time inspection service with Socket.IO interface.

    Architecture:
        - Main thread: Socket.IO server (eventlet)
        - Worker thread: Inspection loop (camera + CV/ML + PLC)
        - Queue: Worker → Socket.IO for results streaming

    Image streaming:
        - Report: 1280x720 JPEG Q80 (~150 KB per frame)
        - Live feed: 640x480 JPEG Q70 (~40 KB per frame)
    """

    def __init__(self, config: dict):
        self.config = config
        self.service_config = config.get("service", {})
        self.host = self.service_config.get("host", "0.0.0.0")
        self.port = self.service_config.get("port", 5004)

        # Initialize centralized logging
        setup_logging(
            config=config,
            service_name="sieger-inspection-service",
            log_dir=config.get("logging", {}).get("directory", "logs"),
        )

        self._start_time = time.time()

        # Shared state
        self.state = ServiceState()

        # Frame counter
        self._frame_counter = 0
        self._last_plc_counter = None  # PLC sample_counter from previous cycle (sync check)

        # c2c_start=0 stall tracking (see _hold_ready_while_c2c_disabled)
        self._c2c_disabled_since = None
        self._c2c_last_reassert = 0.0

        # Result queue (worker → socket server)
        self.result_queue: queue.Queue = queue.Queue(maxsize=100)
        self._stream_worker_started = False

        # Stream settings
        stream_cfg = self.service_config.get("stream", {})
        self._report_width = stream_cfg.get("report_width", 1280)
        self._report_height = stream_cfg.get("report_height", 720)
        self._report_quality = stream_cfg.get("report_quality", 80)
        self._live_width = stream_cfg.get("live_width", 640)
        self._live_height = stream_cfg.get("live_height", 480)
        self._live_quality = stream_cfg.get("live_quality", 70)
        self._live_fps = stream_cfg.get("live_fps", 10)

        # Socket.IO server
        self.sio = socketio.Server(
            cors_allowed_origins=self.service_config.get("cors_origins", "*"),
            async_mode="eventlet",
        )
        self.app = socketio.WSGIApp(self.sio)

        # Connected clients
        self.clients: list[str] = []

        # Hardware — initialized lazily or on run()
        self._cameras = {}       # {"VL": Camera, "UV": Camera, "Tail": Camera}
        self._capture_seq = None  # CaptureSequence
        self._plc = None          # PLCClient

        # Cameras disabled via config (cameras.<name>.enabled = false).
        # A disabled camera is never connected, never waited on in the
        # capture sequence, and its inspection task(s) are force-disabled
        # (see _apply_camera_task_mask). Uppercase names: {"VL","UV","TAIL"}.
        self._disabled_cameras: set = {
            name.upper()
            for name, cam_cfg in config.get("cameras", {}).items()
            if cam_cfg.get("enabled", True) is False
        }
        if self._disabled_cameras:
            logger.warning(
                "Cameras disabled via config: %s — their inspection tasks are forced off",
                sorted(self._disabled_cameras),
            )
        if config.get("cameras") and self._disabled_cameras >= set(
            n.upper() for n in config.get("cameras", {})
        ):
            logger.error(
                "ALL cameras are disabled via config — no frames will be "
                "captured and every cycle will return empty results"
            )
        # Mask the in-memory tasks too, so VisibleInspection constructs its
        # sub-modules consistently with the runtime task decisions.
        insp_cfg = self.config.setdefault("inspection", {})
        insp_cfg["tasks"] = self._apply_camera_task_mask(insp_cfg.get("tasks", {}))

        # PLC reconnect backoff — prevents rapid retry spam on flaky networks.
        # After each failed reconnect attempt, wait _plc_reconnect_interval seconds
        # before trying again. Doubles on each failure, capped at 30s. Resets on success.
        self._plc_reconnect_interval: float = 2.0   # current wait interval (seconds)
        self._plc_reconnect_next_at: float = 0.0    # monotonic time of next allowed attempt

        # Inspection modules — lazy load
        self._vl_inspector = None
        self._uv_inspector = None
        self._tail_inspector = None

        # Capture log — maps frame_id to material_id for sorting on stop
        self._capture_log: list[dict] = []

        # Worker thread
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_worker = threading.Event()
        self._pause_runtime_after_cycle_reason: str = ""

        # Data root — machine-specific storage (never in git)
        self._data_root = config.get("data_root", "/home/msiegerips/sieger_data")

        # SQLite inspection results database
        db_path = str(Path(self._data_root) / "sieger.db")
        self._db_conn = init_db(db_path)
        self._db_writer = InspectionWriter(self._db_conn)

        # Active rejection rate alerts: material_id → bool
        self._rejection_alerts: dict[str, bool] = {}

        # In-memory analytics — updated on every inspection write
        self._analytics = AnalyticsState(
            shift_hours=float(self.config.get("shift_hours", 8.0))
        )

        # Auto-teaching: TubeTeacher instance for background tube training
        # Lazy-init on first use to avoid import cost at startup
        self._tube_teacher = None
        teach_cfg = config.get("teaching", {})

        tube_cfg = config.get("inspection", {}).get("tube_pattern", {})
        _tmpl = Path(tube_cfg.get("template_dir", "data/templates/tube"))

        if not _tmpl.is_absolute():
            _tmpl = Path(config.get("data_root", Path(__file__).resolve().parents[2])) / _tmpl

        self._tube_template_dir = str(_tmpl)

        # Drift alarm: N consecutive tube-pattern rejects for the SAME material
        # is a much stronger signal of miscalibration/drift than any single
        # reject — genuinely defective product is very unlikely to occur in an
        # unbroken streak this long. Per-material counter, reset on any pass.
        self._tube_drift_consecutive_fails: dict[str, int] = {}
        self._tube_drift_alarm_threshold: int = tube_cfg.get("drift_alarm_consecutive_fails", 20)

        self._tube_min_capture: int = teach_cfg.get("tube_min_capture", 20)
        self._installation_min_capture: int = teach_cfg.get("installation_min_capture", 200)
        # Tolerance written into auto-created recipes (dims measured during auto-teach)
        self._auto_recipe_tolerance_mm: float = float(teach_cfg.get("auto_recipe_tolerance_mm", 20.0))

        # Register socket events
        self._register_events()

        logger.info(
            "InspectionService initialized",
            extra={
                "host": self.host,
                "port": self.port,
                "stream_report": f"{self._report_width}x{self._report_height} Q{self._report_quality}",
                "stream_live": f"{self._live_width}x{self._live_height} Q{self._live_quality}",
                "event_type": "service_init",
            }
        )

    # ── Hardware initialization ──────────────────────────────────────

    def _init_cameras(self):
        """Initialize cameras via Basler pypylon SDK. Safe to call if SDK not installed."""
        try:
            from camera import Camera, CaptureSequence

            cams_cfg = self.config.get("cameras", {})

            for cam_name, cam_cfg in cams_cfg.items():
                if cam_name.upper() in self._disabled_cameras:
                    logger.info("Camera '%s' disabled via config — skipping", cam_name)
                    continue
                try:
                    cam = Camera(
                        name=cam_name,
                        exposure=cam_cfg.get("exposure", 11000),
                        timeout=cam_cfg.get("timeout", 30000),
                        ip=cam_cfg.get("ip"),
                        serial=cam_cfg.get("serial"),
                        trigger_debounce_us=cam_cfg.get("trigger_debounce_us", 0),
                    )
                    self._connect_camera_with_retry(cam, cam_name)
                    self._cameras[cam_name.upper()] = cam
                    ident = cam_cfg.get("ip") or cam_cfg.get("serial")
                    logger.info("Camera '%s' connected [%s]", cam_name, ident)
                except Exception as e:
                    logger.error("Camera '%s' failed to connect: %s", cam_name, e)

            # Create capture sequence when every ENABLED camera is connected.
            # Disabled cameras ride along as None slots — CaptureSequence
            # skips them (no trigger wait, no timeout).
            enabled = [k for k in ("VL", "UV", "TAIL") if k not in self._disabled_cameras]
            if enabled and all(k in self._cameras for k in enabled):
                self._capture_seq = CaptureSequence(
                    cam_vl=self._cameras.get("VL"),
                    cam_uv=self._cameras.get("UV"),
                    cam_tail=self._cameras.get("TAIL"),
                )
                if self._disabled_cameras:
                    logger.info(
                        "CaptureSequence ready — cameras %s connected (%s disabled via config)",
                        enabled, sorted(self._disabled_cameras),
                    )
                else:
                    logger.info("CaptureSequence ready — all 3 cameras connected")
            elif not enabled:
                logger.error("CaptureSequence not created — all cameras disabled via config")
            else:
                connected = list(self._cameras.keys())
                logger.warning("CaptureSequence not available — only cameras: %s", connected)

            # Update state
            self.state.cam_vl_connected = "VL" in self._cameras
            self.state.cam_uv_connected = "UV" in self._cameras
            self.state.cam_tail_connected = "TAIL" in self._cameras

        except ImportError:
            logger.warning("pypylon not installed — cameras unavailable")
        except Exception as e:
            logger.error("Camera initialization failed: %s", e)

    # After an unclean shutdown the camera firmware keeps the previous
    # process's GigE control-channel lock alive until GevHeartbeatTimeout
    # (10 s wedge fix) expires — a quick service restart then fails with
    # "The device is controlled by another application" (0xE1018006).
    # Retry long enough to outlive that window. See docs/restart.txt.
    _CAM_RETRY_TOTAL_S = 15.0
    _CAM_RETRY_DELAY_S = 2.0

    def _connect_camera_with_retry(self, cam, cam_name: str):
        """Connect a camera, retrying while its GigE control lock expires."""
        deadline = time.monotonic() + self._CAM_RETRY_TOTAL_S
        while True:
            try:
                cam.connect()
                return
            except Exception as e:
                msg = str(e)
                locked = "controlled by another application" in msg or "0xE1018006" in msg
                if not locked or time.monotonic() >= deadline:
                    raise
                logger.warning(
                    "Camera '%s' still locked by previous process (heartbeat "
                    "not yet expired) — retrying in %.0fs",
                    cam_name, self._CAM_RETRY_DELAY_S,
                )
                time.sleep(self._CAM_RETRY_DELAY_S)

    def _init_plc(self):
        """Initialize PLC client."""
        try:
            from plc import PLCClient

            plc_cfg = self.config.get("plc", {})
            self._plc = PLCClient(plc_cfg)
            connected = self._plc.connect()
            self.state.plc_connected = connected
            if connected:
                logger.info("PLC connected at %s:%d", plc_cfg.get("host"), plc_cfg.get("port"))
            else:
                logger.warning("PLC connection failed — will retry on demand")
        except ImportError:
            logger.warning("pyModbusTCP not installed — PLC unavailable")
        except Exception as e:
            logger.error("PLC initialization failed: %s", e)

    def _cleanup_cameras(self):
        """Disconnect all cameras and close SDK."""
        for cam in self._cameras.values():
            try:
                cam.disconnect()
            except Exception as e:
                logger.warning("Camera '%s' disconnect error: %s", cam.name, e)
        self._cameras.clear()
        self._capture_seq = None

        # pypylon handles cleanup internally — no Library.Close() needed

    def _update_camera_status(self):
        """Update camera connected flags from actual camera state."""
        if self._capture_seq:
            health = self._capture_seq.health_check()
            self.state.cam_vl_connected = health.get("VL", False)
            self.state.cam_uv_connected = health.get("UV", False)
            self.state.cam_tail_connected = health.get("Tail", False)
            logger.info(
                "Camera status: VL=%s UV=%s Tail=%s",
                self.state.cam_vl_connected,
                self.state.cam_uv_connected,
                self.state.cam_tail_connected,
            )

    def _camera_status_dict(self) -> dict:
        """Return camera status with health summary for Socket.IO payloads.

        Included in every send_image event so the UI always has current
        camera health without needing to poll check_cameras separately.
        """
        cam_health = {}
        for cam_name, cam in self._cameras.items():
            if cam and cam.connected:
                stats = cam.get_stream_statistics()
                cam_health[cam_name.lower()] = {
                    "connected": True,
                    "health": self._derive_camera_health_level(stats),
                    "temperature_c": stats.get("temperature_c", -1.0),
                    "delivered": stats.get("delivered", 0),
                    "missed": stats.get("missed", 0),
                    "frame_count": stats.get("frame_count", -1),
                }
            else:
                cam_health[cam_name.lower()] = {
                    "connected": False,
                    "health": "error",
                    "temperature_c": -1.0,
                    "delivered": 0,
                    "missed": 0,
                    "frame_count": -1,
                }

        return {
            "cam_vl": self.state.cam_vl_connected,
            "cam_uv": self.state.cam_uv_connected,
            "cam_tail": self.state.cam_tail_connected,
            "camera_health": cam_health,
        }

    # ── Inspection module loaders ────────────────────────────────────

    def _get_vl_inspector(self):
        """Lazy load the visible-light inspection module and warm up GPU kernels."""
        if self._vl_inspector is None:
            from inspection.visible import VisibleInspection
            self._vl_inspector = VisibleInspection(self.config.get("inspection", {}))
            self.state.models_loaded = True
            logger.info("VisibleInspection module loaded — running warm-up inference")
            self._warm_up_inspector(self._vl_inspector, shape=(1200, 1920, 3), name="VL")
        return self._vl_inspector

    def _get_uv_inspector(self):
        """Lazy load the UV inspection module and warm up GPU kernels."""
        if self._uv_inspector is None:
            from inspection.uv_inspection import UVInspection
            uv_cfg = self.config.get("inspection", {}).get("uv_inspection", {})
            self._uv_inspector = UVInspection(uv_cfg)
            logger.info("UVInspection module loaded — running warm-up inference")
            self._warm_up_inspector(self._uv_inspector, shape=(1200, 1920, 3), name="UV")
        return self._uv_inspector

    def _get_tail_inspector(self):
        """Lazy load the tail inspection module and warm up GPU kernels."""
        if self._tail_inspector is None:
            from inspection.tail_inspection import TailInspection
            tail_cfg = self.config.get("inspection", {}).get("tail_inspection", {})
            self._tail_inspector = TailInspection(tail_cfg)
            logger.info("TailInspection module loaded — running warm-up inference")
            self._warm_up_inspector(self._tail_inspector, shape=(1200, 1920, 3), name="Tail")
        return self._tail_inspector

    @staticmethod
    def _warm_up_inspector(inspector, shape: tuple, name: str):
        """Run one dummy inference to pre-compile GPU kernels and allocate memory.

        Prevents first-cone latency spike from cold model load. The dummy frame
        is all-black so YOLO finds no objects and the pipeline returns early —
        no PLC or HMI side effects.

        Args:
            inspector: Any inspection module with a process_frame() method.
            shape: (H, W, C) shape of the dummy frame.
            name: Label for log messages.
        """
        try:
            import inspect as _inspect
            dummy = np.zeros(shape, dtype=np.uint8)
            start = time.monotonic()
            sig = _inspect.signature(inspector.process_frame)
            if "material_id" in sig.parameters:
                inspector.process_frame(dummy, material_id="__warmup__")
            else:
                inspector.process_frame(dummy)
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.info("Warm-up complete for %s inspector in %.0f ms", name, elapsed_ms)
        except Exception:
            logger.warning("Warm-up for %s raised an exception (non-fatal)", name, exc_info=True)

    def _preload_inspectors(self):
        """Load + warm up every globally-enabled inspection module at boot.

        Uses the same lazy getters (so teach/debug paths are unaffected) —
        this only moves WHEN the load happens, from first-use to boot.
        A failure of one module is non-fatal: it will simply retry on
        first use like before.
        """
        tasks = self._read_tasks_config()
        preload_start = time.monotonic()
        vl_enabled = any(
            tasks.get(k, self.TASK_DEFAULTS[k])
            for k in ("dimension_check", "stain_detection", "tube_pattern")
        )
        plan = [
            ("VL", vl_enabled, self._get_vl_inspector),
            ("UV", tasks.get("uv_inspection", self.TASK_DEFAULTS["uv_inspection"]), self._get_uv_inspector),
            ("Tail", tasks.get("tail_inspection", self.TASK_DEFAULTS["tail_inspection"]), self._get_tail_inspector),
        ]
        for name, enabled, getter in plan:
            if not enabled:
                logger.info("Preload: %s inspector skipped (task disabled)", name)
                continue
            try:
                getter()  # lazy getter loads + warms up on first call
            except Exception:
                logger.warning(
                    "Preload: %s inspector failed to load (will retry on first use)",
                    name, exc_info=True,
                )
        logger.info(
            "Model preload complete in %.1fs — Start is now instant",
            time.monotonic() - preload_start,
        )

    # ── Image encoding ───────────────────────────────────────────────

    def _encode_frame(
        self,
        frame: np.ndarray,
        max_w: int = 1280,
        max_h: int = 720,
        quality: int = 80,
    ) -> str:
        """Resize frame and encode as base64 JPEG for streaming.

        Args:
            frame: BGR numpy array.
            max_w: Maximum width.
            max_h: Maximum height.
            quality: JPEG quality (1-100).

        Returns:
            Base64-encoded JPEG string.
        """
        h, w = frame.shape[:2]
        scale = min(max_w / w, max_h / h, 1.0)
        if scale < 1.0:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
        _, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return base64.b64encode(buffer).decode("utf-8")

    def _safe_path_part(self, value: object, fallback: str = "unknown") -> str:
        """Return a filesystem-safe path segment for material IDs / labels."""
        text = str(value or fallback).strip() or fallback
        safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in text)
        return safe or fallback

    def _save_debug_frames(
        self,
        material_id: object,
        sample_counter: object,
        vl_frame: "np.ndarray | None",
        uv_frame: "np.ndarray | None",
        tail_frame: "np.ndarray | None",
        source: str,
    ) -> None:
        """Save one raw frame per camera/counter for traceability.

        Idempotent layout:
        debug/<date>/<material_id>/<vl|uv|tail>/<sample_counter>.png

        Gated by debug.save_frames (config.json), read fresh every call so
        toggling it takes effect from the next cone onward — no restart.
        """
        if not self._read_debug_save_enabled():
            logger.debug("Debug frame saving disabled via config — skipping (source=%s)", source)
            return
        date_folder = datetime.now().strftime("%Y-%m-%d")
        debug_material_id = self._safe_path_part(material_id)
        debug_counter = self._safe_path_part(sample_counter, "sample")

        for cam_folder, frame in (("vl", vl_frame), ("uv", uv_frame), ("tail", tail_frame)):
            if frame is None:
                continue
            try:
                debug_dir = (
                    Path(self._data_root)
                    / "debug"
                    / date_folder
                    / debug_material_id
                    / cam_folder
                )
                debug_dir.mkdir(parents=True, exist_ok=True)
                debug_path = debug_dir / f"{debug_counter}.png"
                if debug_path.exists():
                    logger.debug("Debug image already exists source=%s path=%s", source, debug_path)
                    continue
                if not cv2.imwrite(str(debug_path), frame):
                    logger.warning("Failed to save %s debug image: %s", cam_folder, debug_path)
            except Exception as e:
                logger.warning("Failed to save %s debug image source=%s: %s", cam_folder, source, e)

    def _save_rejection_outputs(
        self,
        report: dict,
        vl_frame: "np.ndarray | None",
        uv_frame: "np.ndarray | None",
        tail_frame: "np.ndarray | None",
    ) -> None:
        """Save original camera frames only for rejected cones.

        Layout: output/rejections/<date>/<material_id>/<defect_type>/<counter>_<frame>.jpg
        (date first so old days can be pruned with a single folder delete).
        """
        if report.get("result") != "Defect":
            return

        material_id = self._safe_path_part(report.get("material_id"))
        sample_counter = self._safe_path_part(report.get("sample_counter"), "sample")
        date_folder = datetime.now().strftime("%Y-%m-%d")
        filename = f"{sample_counter}_{self._frame_counter}.jpg"

        checks: list[tuple[str, bool, "np.ndarray | None"]] = [
            ("stain", bool(report.get("stain", True)), vl_frame),
            ("wrong_pattern", bool(report.get("tube_pattern", True)), vl_frame),
            ("wrong_cone_diameter", bool(report.get("cone_diameter", True)), vl_frame),
            ("wrong_tube_diameter", bool(report.get("tube_diameter", True)), vl_frame),
            ("missing_tail", bool(report.get("yarn_res", True)), tail_frame),
            ("thread_mix", bool(report.get("thread_mix", True)), uv_frame),
        ]
        if "No Material ID" in str(report.get("defect_type", "")):
            checks.append(("no_material_id", False, vl_frame))

        saved = []
        for inspection_type, passed, frame in checks:
            if passed or frame is None:
                continue
            out_dir = (
                Path(self._data_root)
                / "output"
                / "rejections"
                / date_folder
                / material_id
                / inspection_type
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / filename
            if cv2.imwrite(str(out_path), frame):
                saved.append(str(out_path))
            else:
                logger.warning("Failed to save rejection image: %s", out_path)

        if saved:
            logger.info("Saved rejection output image(s): %s", ", ".join(saved))

    def _queue_result(self, result: dict):
        """Put a result on the queue, dropping oldest if full."""
        try:
            self.result_queue.put_nowait(result)
        except queue.Full:
            try:
                self.result_queue.get_nowait()
                self.result_queue.put_nowait(result)
            except queue.Empty:
                pass

    # ── Worker methods ───────────────────────────────────────────────

    def _run_inspection_cycle(self):
        """Run one full inspection cycle: cycle_start → trigger → capture → inspect → ack.

        Handshake:
          1. Write cycle_start=1 — tell PLC we're ready for next cone
          2. PLC releases cone, sets trigger=1 with material data
          3. Poll trigger, clear it, read material data
          4. Capture 3 images (cameras triggered by proximity sensors)
          5. Run inspection pipelines → write results → set ack=1
          6. PLC reads results → PLC clears ack → repeat

        Teach capture workflow:
          When config.inspection.teach has a toggle active, normal known-material
          inspection still runs. The active teach image is saved from the same
          capture frames inside _inspect_and_report(). If tube .npz is missing,
          _run_capture_for_material() handles tube auto-teach and also saves the
          active teach image from that same capture.
        """
        poll_interval = self.config.get("plc", {}).get("poll_interval", 0.1)

        # Flush all camera buffers before signalling PLC.
        # Between ack (end of previous cycle) and cycle_start (now), no cone
        # is on the conveyor. Any frame in any buffer is stale — e.g. a late
        # frame that arrived after the previous cycle's timeout expired.
        # Without this flush, that stale frame causes a permanent 1-cycle lag.
        if self._capture_seq:
            self._capture_seq.flush_buffers()

        # Inter-cycle camera reconnect: if any camera dropped its GigE link
        # during the previous cycle, attempt reconnect now — before signalling
        # the PLC. No cone is in flight here so the reconnect won't block capture.
        # On success the camera re-enters service for the next cycle.
        # On failure it stays disconnected (returns None in capture_part).
        if self._capture_seq:
            for cam in (self._capture_seq.cam_vl, self._capture_seq.cam_uv, self._capture_seq.cam_tail):
                if cam is None:
                    continue  # camera disabled via config — nothing to reconnect
                if not cam.connected or not cam.health_check():
                    logger.warning(
                        "Camera '%s' disconnected between cycles — attempting reconnect", cam.name
                    )
                    try:
                        ok = cam.reconnect()
                        if ok:
                            logger.info("Camera '%s': reconnect successful — back in service", cam.name)
                        else:
                            logger.error(
                                "Camera '%s': reconnect failed — will return None this cycle", cam.name
                            )
                    except Exception as e:
                        logger.error("Camera '%s': reconnect exception: %s", cam.name, e)

        # 0. Ensure the PLC session is alive BEFORE signalling readiness.
        #    Previously cycle_start was written before the reconnect check, so
        #    a dead session silently skipped it and the handshake deadlocked:
        #    vision polls for trigger, PLC waits for cycle_start=1 forever
        #    while cones pass uninspected (observed 2026-07-17 18:01).
        logger.debug("Inspect cycle: checking PLC connection...")
        self._try_plc_reconnect()

        #    Signal PLC: "ready for next cone"
        #    PLC will only release a cone after seeing cycle_start=1.
        #    This guarantees only 1 cone is between PLC and cameras.
        cycle_start_ok = False
        if self._plc and self._plc.connected:
            cycle_start_ok = self._plc.write_cycle_start()
            if cycle_start_ok:
                logger.info("cycle_start=1 written — ready for next cone")
            else:
                logger.warning("cycle_start write FAILED — will retry during trigger poll")

        # 1. Wait for PLC trigger (material data ready)
        #    Also check c2c_start (40008) every cycle — PLC display can
        #    enable/disable inspection at any time.
        #    c2c_start: 0=disabled, 1=normal, 2=trial run
        if self._plc and self._plc.connected:
            logger.debug("Inspect cycle: polling for PLC trigger (interval=%.2fs)...", poll_interval)
            while not self._stop_worker.is_set():
                if self.state.get_state() != InspectionState.INSPECT:
                    return
                # Self-heal the handshake: if cycle_start never reached the
                # PLC, it will never trigger — retry until the write lands.
                # Never re-write after a success (PLC could double-release).
                if not cycle_start_ok:
                    cycle_start_ok = self._plc.write_cycle_start()
                    if cycle_start_ok:
                        logger.info("cycle_start=1 written on retry — ready for next cone")
                # Single bulk read: trigger + all data in one Modbus call
                plc_input_early = self._plc.poll_trigger_and_read()
                if plc_input_early is not None:
                    # c2c_start: 0=disabled, 1=normal, 2=trial
                    if plc_input_early.c2c_start == 0:
                        self._plc.clear_trigger()
                        self._hold_ready_while_c2c_disabled()
                        time.sleep(poll_interval)
                        continue
                    self._clear_c2c_disabled()
                    if plc_input_early.c2c_start == 2:
                        self.state.trial_mode = True
                    logger.debug("Inspect cycle: trigger received! Clearing trigger...")
                    self._plc.clear_trigger()
                    break
                time.sleep(poll_interval)
            if self._stop_worker.is_set():
                return
        else:
            plc_input_early = None
            # No PLC — wait briefly (development/simulation mode)
            logger.debug("Inspect cycle: no PLC — simulating 1s delay")
            time.sleep(1.0)
            if self.state.get_state() != InspectionState.INSPECT:
                return

        self._frame_counter += 1
        set_correlation_id(f"insp-{self._frame_counter}")

        # 2. Use PLC input read before trigger clear (data is valid while trigger=1)
        material_id = self.state.material_id or "unknown"
        machine_id = self.state.machine_id
        basket_id = ""
        sample_counter = 0
        loader_id = self.state.loader_id
        material_no = 0
        basket_no = 0
        plc_input = plc_input_early
        if plc_input:
            material_no = plc_input.material_no
            if plc_input.material_no:
                material_id = str(plc_input.material_no)
            sample_counter = plc_input.sample_counter
            basket_no = plc_input.basket_no
            basket_id = str(plc_input.basket_no) if plc_input.basket_no else basket_id
            loader_id = plc_input.loader_id
            machine_id = str(plc_input.loader_id) if plc_input.loader_id else machine_id
        # material_id is the single truth — no master_name indirection
        master_id = material_id

        # ── Sync check: PLC counter (logging only) ──
        if sample_counter and self._last_plc_counter is not None:
            expected = self._last_plc_counter + 1
            gap = sample_counter - expected
            if gap > 0:
                logger.warning(
                    "SYNC: PLC counter jumped %d→%d (expected %d) — %d missed trigger(s)",
                    self._last_plc_counter, sample_counter, expected, gap,
                )
            elif gap < 0:
                logger.warning(
                    "SYNC: PLC counter went backwards %d→%d (reset or rollover)",
                    self._last_plc_counter, sample_counter,
                )
        self._last_plc_counter = sample_counter

        logger.info(
            "═══ Inspection #%d START ═══ material=%s master=%s counter=%d basket=%s loader=%s trial=%s",
            self._frame_counter, material_id, master_id, sample_counter,
            basket_id, loader_id, self.state.trial_mode,
        )

        # ── Auto-teaching gate: if material has no tube template, route to capture ──
        # If an install-site teach toggle is also active, _run_capture_for_material()
        # saves both the tube auto-teach crop and the active teach session image
        # from the same capture sequence.
        if material_id and material_id != "unknown":
            npz_path = Path(self._tube_template_dir) / f"{material_id}.npz"
            if not npz_path.exists() and material_id not in self.state.auto_teaching_triggered:
                _teach_cfg = self._read_teach_config()
                if self._any_teach_active(_teach_cfg):
                    logger.info(
                        "AUTO-TEACH PARALLEL: material=%s has no .npz and teach toggles "
                        "are active (%s) — saving tube crop plus active teach image",
                        material_id,
                        [m for m in ("tail", "uv", "stain", "dimension") if _teach_cfg.get(m)],
                    )

                # No master and training not yet triggered — add to auto-capture set
                # if material_id not in self.state.capture_material_ids:
                #     self.state.capture_material_ids.add(material_id)
                #     self.state.capture_module = "tube"
                #     logger.info(
                #         "AUTO-TEACH: material=%s has no master — added to auto-capture set",
                #         material_id,
                #     )
                #     self._emit_teaching_alert(
                #         module="tube",
                #         material_id=material_id,
                #         stage="capturing",
                #         message=f"New material {material_id} — capturing images for auto-teaching",
                #         count=0,
                #         total=self._tube_min_capture,
                #     )

                existing_count = self._count_tube_crops(material_id)

                if existing_count >= self._tube_min_capture:
                    with self.state.lock:
                        self.state.auto_teaching_triggered.add(material_id)

                    self._emit_teaching_alert(
                        module="tube",
                        material_id=material_id,
                        stage="training_started",
                        message=f"Found {existing_count} existing crops — training started",
                        count=existing_count,
                        total=self._tube_min_capture,
                    )

                    t = threading.Thread(
                        target=self._auto_teach_tube,
                        args=(material_id,),
                        daemon=True,
                        name=f"auto-teach-{material_id}",
                    )
                    t.start()
                    return


                # Redirect this cone to capture cycle instead of inspection
                self._run_capture_for_material(
                    material_id=material_id,
                    basket_no=basket_no,
                    loader_id=loader_id,
                    material_no=material_no,
                    cap_counter=sample_counter,
                )
                return

        # 3. Capture images
        # capture_latest() — drains stale frames, keeps freshest.
        # cycle_start=1 gating ensures only 1 cone in-flight,
        # so the latest frame is always the correct one.
        logger.debug("Step 3: Capturing images...")
        vl_frame = None
        uv_frame = None
        tail_frame = None

        if self._capture_seq:
            try:
                with PerformanceLogger("capture", logger) as perf:
                    images = self._capture_seq.capture_part()
                    vl_frame = images.vl
                    uv_frame = images.uv
                    tail_frame = images.tail
                    if vl_frame is not None:
                        perf.add_metric("vl_shape", str(vl_frame.shape))
            except Exception as e:
                logger.exception("Capture failed: %s", e)
        else:
            # CaptureSequence not available — capture from individual cameras
            for name, key in [("VL", "VL"), ("UV", "UV"), ("Tail", "TAIL")]:
                cam = self._cameras.get(key)
                if cam is None:
                    continue
                try:
                    frame = cam.capture()
                    if key == "VL":
                        vl_frame = frame
                    elif key == "UV":
                        uv_frame = frame
                    else:
                        tail_frame = frame
                    logger.info("Captured %s: %s", name, frame.shape)
                except TimeoutError:
                    logger.warning("Capture timeout for %s — no trigger received", name)
                except Exception as e:
                    logger.warning("Capture failed for %s: %s", name, e)
            if vl_frame is None and uv_frame is None and tail_frame is None:
                logger.warning("No frames captured from any camera")

        # Run inspection pipeline, write PLC results, and stream report to UI
        self._inspect_and_report(
            vl_frame, uv_frame, tail_frame,
            material_id, master_id, machine_id, basket_id,
            sample_counter, loader_id, material_no, basket_no,
        )
        self._pause_runtime_if_requested()


    @staticmethod
    def _crop_for_stream(
        frame: np.ndarray,
        bbox: tuple = None,
        padding: float = 0.10,
        out_w: int = 640,
        out_h: int = 640,
    ) -> np.ndarray:
        """Crop frame to bbox + padding and resize to out_w x out_h.

        If bbox is None or invalid, returns the full frame resized.

        Args:
            frame: BGR image.
            bbox: (x1, y1, x2, y2) in pixels, or None.
            padding: Fractional padding around bbox (0.10 = 10%).
            out_w: Output width in pixels.
            out_h: Output height in pixels.

        Returns:
            BGR image of shape (out_h, out_w, 3).
        """
        h, w = frame.shape[:2]
        if bbox is not None:
            try:
                x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
                bw, bh = x2 - x1, y2 - y1
                pad_x = int(bw * padding)
                pad_y = int(bh * padding)
                x1 = max(0, x1 - pad_x)
                y1 = max(0, y1 - pad_y)
                x2 = min(w, x2 + pad_x)
                y2 = min(h, y2 + pad_y)
                if x2 > x1 and y2 > y1:
                    frame = frame[y1:y2, x1:x2]
            except Exception:
                pass  # fall through to full-frame resize
        return cv2.resize(frame, (out_w, out_h))

    def _inspect_and_report(
        self,
        vl_frame,
        uv_frame,
        tail_frame,
        material_id,
        master_id,
        machine_id,
        basket_id,
        sample_counter,
        loader_id,
        material_no,
        basket_no,
    ):
        """Run full inspection pipeline, write results to PLC, and stream report to UI.

        Shared helper used by both _run_inspection_cycle() and _run_capture_cycle()
        (for non-captured materials in hybrid mode). Handles:
        - Saving tail training images
        - Running VL / UV / Tail inspection pipelines
        - Computing combined result + defect_type_code
        - Writing results to PLC (respects trial mode)
        - Building annotated report with overlays
        - Queuing report for UI streaming
        - Memory cleanup (explicit del of large temporaries)
        """
        # Guard: ack_complete() must reach PLC on ALL exit paths.
        # Without this the PLC hangs waiting for ack=1 forever → conveyor stop.
        _ack_sent = False
        try:
            self._save_debug_frames(
                material_id=material_id,
                sample_counter=sample_counter,
                vl_frame=vl_frame,
                uv_frame=uv_frame,
                tail_frame=tail_frame,
                source="inspect",
            )

            # 4. Run inspection pipelines
            logger.debug("Step 4: Running inspection pipelines...")
            # Global toggles fresh from disk (UI edits apply next cone, no
            # restart) AND-combined with this material's recipe overrides.
            tasks = self._effective_tasks(material_id)
            logger.debug("  Effective tasks for material '%s': %s", material_id,
                         {k: v for k, v in tasks.items() if v})

            # ── Teach × tasks gate (inspection-mode teach capture) ───────
            # Saves from original captured frames; normal inspection continues.
            self._save_active_teach_session_image(
                material_id=material_id,
                sample_counter=sample_counter,
                vl_frame=vl_frame,
                uv_frame=uv_frame,
                tail_frame=tail_frame,
                source="inspect",
            )
            # ── end teach gate ──────────────────────────────────────────

            # 4a. Visible Light inspection (runs if we have a frame, skipped if empty)
            vl_result = None
            vl_code = None  # None = skipped (camera timeout / no frame)

            if vl_frame is not None:
                try:
                    logger.debug("Step 4a: VL inspection on %dx%d frame, material_id='%s'",
                                 vl_frame.shape[1], vl_frame.shape[0], material_id)
                    with PerformanceLogger("vl_inspection", logger):
                        inspector = self._get_vl_inspector()
                        vl_result = inspector.process_frame(
                            vl_frame, material_id,
                            tasks_override={
                                k: tasks[k]
                                for k in ("dimension_check", "stain_detection", "tube_pattern")
                            },
                        )
                        vl_code = vl_result.result_code
                    logger.debug("  VL result: code=%d passed=%s", vl_code, vl_result.passed)

                except Exception as e:
                    logger.exception("VL inspection failed: %s", e)
            else:
                logger.debug("Step 4a: VL inspection SKIPPED (no frame)")

            # 4b. UV inspection (if enabled and frame available)
            uv_code = None
            uv_result_obj = None
            if tasks.get("uv_inspection", False) and uv_frame is not None:
                try:
                    logger.debug("Step 4b: UV inspection on %dx%d frame", uv_frame.shape[1], uv_frame.shape[0])
                    with PerformanceLogger("uv_inspection", logger):
                        uv_inspector = self._get_uv_inspector()
                        uv_result_obj = uv_inspector.process_frame(uv_frame)

                        if uv_result_obj.detection_failed:
                            # YOLO/compute failed — skip UV for this cone.
                            # uv_code stays None so it doesn't affect the final verdict.
                            # Consecutive failures are already logged as error by UVInspection.
                            uv_code = None
                        elif uv_result_obj.has_mixup:
                            uv_code = 2
                        else:
                            uv_code = 1
                    logger.debug(
                        "  UV result: code=%s mixup=%s detection_failed=%s",
                        uv_code,
                        uv_result_obj.has_mixup if uv_result_obj else "N/A",
                        uv_result_obj.detection_failed if uv_result_obj else "N/A",
                    )
                except Exception as e:
                    logger.exception("UV inspection failed: %s", e)
                    uv_code = 3
            else:
                logger.debug("Step 4b: UV inspection SKIPPED (enabled=%s, frame=%s)",
                             tasks.get("uv_inspection", False), uv_frame is not None)

            # 4c. Tail inspection (if enabled and frame available)
            tail_code = None
            tail_result_obj = None
            if tasks.get("tail_inspection", True) and tail_frame is not None:
                try:
                    logger.debug("Step 4c: Tail inspection on %dx%d frame", tail_frame.shape[1], tail_frame.shape[0])
                    with PerformanceLogger("tail_inspection", logger):
                        tail_inspector = self._get_tail_inspector()
                        tail_result_obj = tail_inspector.process_frame(tail_frame)

                        if not tail_result_obj.model_loaded:
                            tail_code = 3
                        elif not tail_result_obj.tail_detected:
                            tail_code = 2
                        else:
                            tail_code = 1
                    logger.debug("  Tail result: code=%d detected=%s", tail_code, tail_result_obj.tail_detected if tail_result_obj else "N/A")
                except Exception as e:
                    logger.exception("Tail inspection failed: %s", e)
                    tail_code = 3
            else:
                logger.debug("Step 4c: Tail inspection SKIPPED (enabled=%s, frame=%s)",
                             tasks.get("tail_inspection", True), tail_frame is not None)

            # 5. Compute combined result
            logger.debug("Step 5: Computing combined result (vl=%s uv=%s tail=%s)", vl_code, uv_code, tail_code)
            from plc.data_types import PLCOutput
            plc_output = PLCOutput.from_results(
                vl_code, uv_code, tail_code,
                material_no=material_no,
                basket_no=basket_no,
                loader_id=loader_id,
            )

            # 5b. Compute defect_type_code for PLC register 40020
            # 0=Good, 1=Stain, 2=Wrong Pattern, 3=Wrong Cone Dia,
            # 4=Wrong Tube Dia, 5=Missing Tail, 6=Thread Mixup, 7=No Material ID
            defect_type_code = 0
            if plc_output.result_code == 2:
                material_missing = vl_result and vl_result.material_not_found
                stain_failed = vl_result and vl_result.stain_result and vl_result.stain_result.has_stain
                pattern_failed = vl_result and vl_result.tube_pattern_result and not vl_result.tube_pattern_result.passed
                cone_dia_failed = vl_result and vl_result.dimension_result and not vl_result.dimension_result.cone_diameter_match
                tube_dia_failed = vl_result and vl_result.dimension_result and not vl_result.dimension_result.tube_diameter_match
                tail_failed = tail_result_obj and not tail_result_obj.tail_detected
                mixup_failed = uv_result_obj and uv_result_obj.has_mixup
                if material_missing:
                    defect_type_code = 7
                elif stain_failed:
                    defect_type_code = 1
                elif pattern_failed:
                    defect_type_code = 2
                elif cone_dia_failed:
                    defect_type_code = 3
                elif tube_dia_failed:
                    defect_type_code = 4
                elif tail_failed:
                    defect_type_code = 5
                elif mixup_failed:
                    defect_type_code = 6
            plc_output.defect_type_code = defect_type_code
            logger.debug("  Combined: result_code=%d defect_type=%d", plc_output.result_code, defect_type_code)

            # 5c. Drift alarm: track consecutive tube-pattern rejects per material.
            # reference_loaded=False means "material has no template" (a setup
            # issue, not drift) — excluded so it can't feed this counter.
            tpr = vl_result.tube_pattern_result if vl_result else None
            if tpr is not None and tpr.reference_loaded:
                if tpr.passed:
                    self._tube_drift_consecutive_fails.pop(material_id, None)
                else:
                    fails = self._tube_drift_consecutive_fails.get(material_id, 0) + 1
                    self._tube_drift_consecutive_fails[material_id] = fails
                    if (
                        self._tube_drift_alarm_threshold > 0
                        and fails >= self._tube_drift_alarm_threshold
                        and fails % self._tube_drift_alarm_threshold == 0
                    ):
                        self._emit_drift_alert(material_id, fails)

            # 6. Write results to PLC (skip in trial mode) + always ack
            logger.debug("Step 6: Writing results to PLC (trial=%s)", self.state.trial_mode)
            if self._plc and self._plc.connected:
                if not self.state.trial_mode:
                    if not self._plc.write_output(plc_output):
                        logger.error(
                            "PLC write_output failed (partial write) — ack still sent, "
                            "PLC may read stale registers for this cone"
                        )
                else:
                    logger.info("Trial mode — skipping PLC result write (result=%d, defect=%d)",
                                plc_output.result_code, plc_output.defect_type_code)
                self._plc.ack_complete()
                _ack_sent = True

            # 7. Build and queue report for UI
            # Per-check booleans: True = pass, False = defect
            stain_ok = (
                not vl_result.stain_result.has_stain
                if vl_result and vl_result.stain_result else True
            )
            tube_pattern_ok = (
                vl_result.tube_pattern_result.passed
                if vl_result and vl_result.tube_pattern_result else True
            )
            cone_diameter_ok = (
                vl_result.dimension_result.cone_diameter_match
                if vl_result and vl_result.dimension_result else True
            )
            tube_diameter_ok = (
                vl_result.dimension_result.tube_diameter_match
                if vl_result and vl_result.dimension_result else True
            )
            yarn_tail_ok = (
                tail_result_obj.tail_detected
                if tail_result_obj else True
            )
            thread_mix_ok = (
                not uv_result_obj.has_mixup
                if uv_result_obj else True
            )

            # Use YOLO cone crop if available, else full frame. Resize to 640x480,
            # then overlay text so it's readable at frontend display resolution.
            # If VL frame is missing (timeout), show black frame with error text.
            if vl_frame is not None:
                # Use cone_crop if YOLO detected cone, else fall back to full frame
                src = (
                    vl_result.cone_crop
                    if vl_result and vl_result.cone_crop is not None
                    else vl_frame
                )
                vl_display = cv2.resize(src, (640, 640))
            else:
                vl_display = np.zeros((640, 640, 3), dtype=np.uint8)
                cv2.putText(vl_display, "EMPTY", (240, 320),
                            cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 255), 3)

            if plc_output.result_code == 1:
                res_label, res_color = "GOOD", (0, 255, 0)
            elif plc_output.result_code == 2:
                res_label, res_color = "DEFECT", (0, 0, 255)
            else:
                res_label, res_color = "ERROR", (0, 0, 255)
            font = cv2.FONT_HERSHEY_SIMPLEX
            cv2.putText(vl_display, f"PLC:{material_id}  Master:{master_id}  {res_label}",
                        (10, 35), font, 0.9, res_color, 2)
            y = 70
            # If no VL inspection ran (camera timeout / no frame), show ERROR for all checks
            no_inspection = vl_result is None
            for name, ok in [("Stain", stain_ok), ("Pattern", tube_pattern_ok),
                             ("ConeDia", cone_diameter_ok), ("TubeDia", tube_diameter_ok),
                             ("Tail", yarn_tail_ok), ("Mixup", thread_mix_ok)]:
                if no_inspection:
                    cv2.putText(vl_display, f"{name}:ERROR", (10, y),
                                font, 0.8, (0, 0, 255), 2)
                else:
                    c = (0, 255, 0) if ok else (0, 0, 255)
                    cv2.putText(vl_display, f"{name}:{'OK' if ok else 'FAIL'}", (10, y),
                                font, 0.8, c, 2)
                y += 30

            # Show tube pattern result as OK/NOK + match probability. This is
            # a verifier, not a classifier — it never identifies "which
            # pattern this is", only whether the live crop matches the ONE
            # expected material. Showing a material/pattern ID here would
            # misleadingly imply classification. Raw color/fft/resnet
            # distances are internal signal, not for the floor operator.
            if vl_result and vl_result.tube_pattern_result:
                tpr = vl_result.tube_pattern_result
                pattern_c = (0, 255, 0) if tpr.color_match else (0, 0, 255)
                status = "OK" if tpr.color_match else "NOK"
                cv2.putText(vl_display, f"Pattern:{status} ({tpr.verifier_probability:.1%})",
                            (10, y), font, 0.8, pattern_c, 2)

            _, buffer = cv2.imencode(".jpg", vl_display, [cv2.IMWRITE_JPEG_QUALITY, self._report_quality])
            vl_b64 = base64.b64encode(buffer).decode("utf-8")
            if uv_frame is not None:
                # Crop to YOLO cone bbox if available (1920x1200 → tight crop → 640x640)
                # Falls back to full frame downscaled if cone not detected
                uv_bbox = (
                    uv_result_obj.cone_bbox
                    if uv_result_obj and uv_result_obj.cone_bbox
                    else None
                )
                uv_cropped = self._crop_for_stream(uv_frame, bbox=uv_bbox, out_w=640, out_h=640)
                _, buf = cv2.imencode(".jpg", uv_cropped, [cv2.IMWRITE_JPEG_QUALITY, self._report_quality])
                uv_b64 = base64.b64encode(buf).decode("utf-8")
            else:
                empty_uv = np.zeros((640, 640, 3), dtype=np.uint8)
                cv2.putText(empty_uv, "EMPTY", (240, 320),
                            cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 255), 3)
                _, buf = cv2.imencode(".jpg", empty_uv, [cv2.IMWRITE_JPEG_QUALITY, self._report_quality])
                uv_b64 = base64.b64encode(buf).decode("utf-8")
            if tail_frame is not None:
                # Fixed bottom-40% crop — tail always at cone base,
                # bbox too small (2-5% of frame) to be useful for display
                # th = tail_frame.shape[0]
                # tail_roi = tail_frame[int(th * 0.60):, :]
                tail_cropped = cv2.resize(tail_frame, (640, 640))
                _, buf = cv2.imencode(".jpg", tail_cropped, [cv2.IMWRITE_JPEG_QUALITY, self._report_quality])
                tail_b64 = base64.b64encode(buf).decode("utf-8")
            else:
                empty_tail = np.zeros((256, 640, 3), dtype=np.uint8)
                cv2.putText(empty_tail, "EMPTY", (240, 128),
                            cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 255), 3)
                _, buf = cv2.imencode(".jpg", empty_tail, [cv2.IMWRITE_JPEG_QUALITY, self._report_quality])
                tail_b64 = base64.b64encode(buf).decode("utf-8")

            # Result string: "Good" / "Defect" / "None"
            result_code = plc_output.result_code
            if result_code == 1:
                result_str = "Good"
            elif result_code == 2:
                result_str = "Defect"
            else:
                result_str = "None"

            # Build defect_type string (comma-separated list of failed checks)
            material_missing = vl_result and vl_result.material_not_found
            defect_parts = []
            if material_missing:
                defect_parts.append("No Material ID")
            if not stain_ok:
                defect_parts.append("Stain")
            if not tube_pattern_ok:
                defect_parts.append("Wrong Pattern")
            if not cone_diameter_ok:
                defect_parts.append("Wrong Cone Diameter")
            if not tube_diameter_ok:
                defect_parts.append("Wrong Tube Diameter")
            if not yarn_tail_ok:
                defect_parts.append("Missing Tail")
            if not thread_mix_ok:
                defect_parts.append("Thread Mixup")
            defect_type = ",".join(defect_parts) if defect_parts else "Good"

            diameter = None
            tolerance = None
            if vl_result and vl_result.dimension_result:
                dim_result = vl_result.dimension_result
                diameter = dim_result.measured.cone_diameter_mm
                ref = dim_result.reference
                tolerance = (
                    ref.cone_tolerance_mm
                    if ref.cone_tolerance_mm > 0
                    else ref.tolerance_mm
                )

            report = {
                "type": "report",
                "material_id": material_id,
                "master_id": master_id,
                "machine_id": machine_id,
                "basketid": basket_id,
                "sample_counter": sample_counter,
                "frame_number": self._frame_counter,
                "date_time": datetime.now(timezone.utc).isoformat(),
                "result": result_str,
                "defect_type": defect_type,
                "diameter": diameter,
                "tolerance": tolerance,
                "visible": vl_b64,
                "uv": uv_b64,
                "yarntail": tail_b64,
                "stain": stain_ok,
                "tube_pattern": tube_pattern_ok,
                "cone_diameter": cone_diameter_ok,
                "tube_diameter": tube_diameter_ok,
                "yarn_res": yarn_tail_ok,
                "thread_mix": thread_mix_ok,
                **self._camera_status_dict(),
            }

            self._save_rejection_outputs(report, vl_frame, uv_frame, tail_frame)

            # Write to SQLite and check rejection rate
            try:
                db_record = InspectionRecord(
                    timestamp=report["date_time"],
                    material_id=report["material_id"],
                    master_id=report.get("master_id"),
                    basket_no=report.get("basketid"),
                    loader_id=report.get("loader_id"),
                    sample_counter=report.get("sample_counter"),
                    result_code=2 if report["result"] == "Defect" else (3 if report["result"] == "Error" else 1),
                    defect_type=report.get("defect_type"),
                    # Dimension
                    cone_dia_mm=(
                        vl_result.dimension_result.measured.cone_diameter_mm
                        if vl_result and vl_result.dimension_result
                        else None
                    ),
                    tube_dia_mm=(
                        vl_result.dimension_result.measured.tube_diameter_mm
                        if vl_result and vl_result.dimension_result
                        else None
                    ),
                    # Stain
                    stain_score=(
                        vl_result.stain_result.anomaly_score
                        if vl_result and vl_result.stain_result
                        else None
                    ),
                    stain_ok=report.get("stain"),
                    # UV (thread mixup)
                    uv_radial_dip=(
                        uv_result_obj.radial_dip
                        if uv_result_obj and not uv_result_obj.detection_failed
                        else None
                    ),
                    uv_ok=(
                        not uv_result_obj.has_mixup
                        if uv_result_obj and not uv_result_obj.detection_failed
                        else None
                    ),
                    # Tail
                    tail_confidence=(
                        tail_result_obj.confidence
                        if tail_result_obj
                        else None
                    ),
                    tail_ok=report.get("yarn_res"),
                    # Tube pattern
                    tube_pattern=(
                        vl_result.tube_pattern_result.combined_nearest
                        if vl_result and vl_result.tube_pattern_result
                        else None
                    ),
                    tube_distance=(
                        vl_result.tube_pattern_result.combined_distance
                        if vl_result and vl_result.tube_pattern_result
                        else None
                    ),
                    tube_ok=report.get("tube_pattern"),
                    trial_mode=self.state.trial_mode,
                )
                row_id, alert = self._db_writer.write(db_record)
                self._rejection_alerts[report["material_id"]] = alert
                self._analytics.update(db_record)

                # Save audit JPEGs — one per camera view, named by row_id so
                # GET /results/:id/audit can serve them directly. VL keeps its
                # original filename (no _vl suffix) for backward compatibility
                # with audit_image paths already stored in existing rows. UV
                # and Tail are raw frames, saved only if that camera actually
                # fired this cycle (frequently None — see camera timeout
                # debugging in docs/criticalfix/audit_gallery_uv_tail_images.md).
                try:
                    audit_date = datetime.now().strftime("%Y-%m-%d")
                    audit_dir = Path(self._data_root) / "audit" / audit_date
                    audit_dir.mkdir(parents=True, exist_ok=True)

                    vl_rel_path = f"audit/{audit_date}/{row_id}.jpg"
                    cv2.imwrite(str(audit_dir / f"{row_id}.jpg"), vl_display)

                    uv_rel_path = None
                    if uv_frame is not None:
                        uv_rel_path = f"audit/{audit_date}/{row_id}_uv.jpg"
                        cv2.imwrite(str(audit_dir / f"{row_id}_uv.jpg"), uv_frame)

                    tail_rel_path = None
                    if tail_frame is not None:
                        tail_rel_path = f"audit/{audit_date}/{row_id}_tail.jpg"
                        cv2.imwrite(str(audit_dir / f"{row_id}_tail.jpg"), tail_frame)

                    self._db_conn.execute(
                        "UPDATE inspections SET audit_image=?, audit_image_uv=?, audit_image_tail=? WHERE id=?",
                        (vl_rel_path, uv_rel_path, tail_rel_path, row_id),
                    )
                    self._db_conn.commit()
                except Exception as _audit_exc:
                    logger.warning("Audit image save failed (non-fatal): %s", _audit_exc)
            except Exception as _db_exc:
                logger.warning("SQLite write failed (non-fatal): %s", _db_exc)

            logger.debug("Step 7: Queuing report for UI streaming")
            report["analytics"] = self._analytics.snapshot()
            self._queue_result(report)

            logger.info(
                "═══ Inspection #%d DONE ═══ result=%s defect=%s | "
                "VL=%s UV=%s Tail=%s | PLC: material=%s basket=%s loader=%s counter=%d | "
                "Checks: stain=%s pattern=%s cone_dia=%s tube_dia=%s tail=%s mixup=%s | "
                "trial=%s ack=1",
                self._frame_counter,
                result_str, defect_type or "None",
                vl_code if vl_code is not None else "empty",
                uv_code if uv_code is not None else "empty",
                tail_code if tail_code is not None else "empty",
                material_id, basket_id, loader_id, sample_counter,
                "OK" if stain_ok else "FAIL",
                "OK" if tube_pattern_ok else "FAIL",
                "OK" if cone_diameter_ok else "FAIL",
                "OK" if tube_diameter_ok else "FAIL",
                "OK" if yarn_tail_ok else "FAIL",
                "OK" if thread_mix_ok else "FAIL",
                self.state.trial_mode,
            )

            # Explicitly delete large temporaries so the GC can reclaim them promptly
            del vl_result, vl_b64, uv_b64, tail_b64, report

        except Exception:
            logger.exception(
                "Unhandled exception in _inspect_and_report — sending Error(3) to PLC"
            )
            if self._plc and self._plc.connected and not _ack_sent:
                from plc.data_types import PLCOutput
                _err_output = PLCOutput(result_code=3, camera_error=1)
                if not self.state.trial_mode:
                    if not self._plc.write_output(_err_output):
                        logger.error("PLC write_output failed in exception handler — ack still sent")
                self._plc.ack_complete()
                _ack_sent = True
            raise


    def _sort_captures_by_material(self):
        """Post-acquisition cleanup. Images are already saved under
        sieger_data/captures/{material_id}/{camera}/ during capture, so no
        file moves are needed — just clear the capture log.
        """
        count = len(self._capture_log)
        if count:
            logger.info("Acquisition complete — %d frames saved to sieger_data/captures/<material_id>/", count)
        self._capture_log.clear()
        return count

    def _sort_and_notify(self, total: int):
        """Sort captures in background thread and emit progress to UI."""
        try:
            sorted_count = self._sort_captures_by_material()
            self.sio.emit("send_image", {
                "type": "sorting",
                "status": "complete",
                "total": total,
                "sorted": sorted_count,
            })
        except Exception as e:
            logger.exception("Sorting failed: %s", e)
            self.sio.emit("send_image", {
                "type": "sorting",
                "status": "error",
                "error": str(e),
            })

    def _run_capture_cycle(self):
        """Hybrid capture+inspection mode.

        Same trigger+ack handshake as inspection cycle, then splits:
        - Path A (material in capture list): save raw images, write result=0
          to PLC (cone stays on conveyor), stream raw images to UI.
        - Path B (material NOT in capture list): run full inspection pipeline
          via _inspect_and_report(), write real result (1/2/3) to PLC, stream
          annotated report to UI. This keeps the conveyor flowing for all cones.

        When capture_material_ids is empty, all materials go through Path A
        (save all — original behavior).
        """
        poll_interval = self.config.get("plc", {}).get("poll_interval", 0.1)

        # Flush all camera buffers before signalling PLC.
        # Between ack (end of previous cycle) and cycle_start (now), no cone
        # is on the conveyor. Any frame in any buffer is stale.
        if self._capture_seq:
            self._capture_seq.flush_buffers()

        # Ensure PLC session is alive BEFORE signalling readiness — same
        # cycle_start-before-reconnect deadlock as the inspect cycle.
        self._try_plc_reconnect()

        # Signal PLC: ready for next cone
        cycle_start_ok = False
        if self._plc and self._plc.connected:
            cycle_start_ok = self._plc.write_cycle_start()
            if cycle_start_ok:
                logger.info("cycle_start=1 written — ready for next cone (capture)")
            else:
                logger.warning("cycle_start write FAILED (capture) — will retry during trigger poll")

        # Wait for PLC trigger (material data ready)
        # c2c_start: 0=disabled, 1=normal, 2=trial — capture runs on 1 or 2
        # Trigger interlock is always active: read trigger → clear trigger
        if self._plc and self._plc.connected:
            while not self._stop_worker.is_set():
                if self.state.get_state() not in (InspectionState.CAPTURE, InspectionState.INSPECT):
                    return
                # Self-heal: retry cycle_start until it lands; never re-write
                # after a success (PLC could double-release a cone).
                if not cycle_start_ok:
                    cycle_start_ok = self._plc.write_cycle_start()
                    if cycle_start_ok:
                        logger.info("cycle_start=1 written on retry — ready for next cone (capture)")
                # Single bulk read: trigger + all data in one Modbus call
                plc_input = self._plc.poll_trigger_and_read()
                if plc_input is not None:
                    if plc_input.c2c_start == 0:
                        self._plc.clear_trigger()
                        self._hold_ready_while_c2c_disabled()
                        time.sleep(poll_interval)
                        continue
                    self._clear_c2c_disabled()
                    self._plc.clear_trigger()
                    break
                time.sleep(poll_interval)
            if self._stop_worker.is_set():
                return
        else:
            plc_input = None
            time.sleep(1.0)
            if self.state.get_state() not in (InspectionState.CAPTURE, InspectionState.INSPECT):
                return

        # Use PLC data read before trigger clear
        material_id = self.state.material_id or "unknown"
        if plc_input:
            if plc_input.material_no:
                material_id = str(plc_input.material_no)
        elif self._plc and self._plc.connected:
            logger.warning("PLC read_input returned None")

        self._frame_counter += 1

        # ── Sync check: PLC counter (logging only) ──
        cap_counter = plc_input.sample_counter if plc_input else 0
        if cap_counter and self._last_plc_counter is not None:
            expected = self._last_plc_counter + 1
            gap = cap_counter - expected
            if gap > 0:
                logger.warning(
                    "SYNC: PLC counter jumped %d→%d (expected %d) — %d missed trigger(s)",
                    self._last_plc_counter, cap_counter, expected, gap,
                )
            elif gap < 0:
                logger.warning(
                    "SYNC: PLC counter went backwards %d→%d (reset or rollover)",
                    self._last_plc_counter, cap_counter,
                )
        self._last_plc_counter = cap_counter

        if plc_input:
            logger.info(
                "═══ Capture #%d START ═══ material=%s counter=%d basket=%d loader=%d c2c=%d",
                self._frame_counter,
                material_id, plc_input.sample_counter,
                plc_input.basket_no, plc_input.loader_id, plc_input.c2c_start,
            )
        else:
            logger.info("═══ Capture #%d START ═══ material=%s (no PLC data)", self._frame_counter, material_id)

        if not self._capture_seq:
            logger.warning("No cameras — cannot capture")
            return

        # capture_latest() drains stale frames and keeps the freshest.
        # Per-cycle flush above (before cycle_start) clears any leftovers.
        try:
            images = self._capture_seq.capture_part()
        except Exception as e:
            logger.exception("Capture failed: %s", e)
            return

        # ── Hybrid capture+inspection: decide Path A vs Path B ──
        # Path A: material IS in capture list → save images, result=0 (cone stays)
        # Path B: material NOT in capture list → run full inspection, real PLC result
        # Empty capture list = save all (Path A for everything)
        capture_this = True
        inspect_this = False
        if self.state.capture_material_ids:
            if material_id not in self.state.capture_material_ids:
                capture_this = False
        # Check if this material should be inspected
        if self.state.inspection_material_ids:
            if material_id in self.state.inspection_material_ids:
                inspect_this = True
        elif not capture_this:
            # Legacy: no inspection_ids set, non-captured materials get inspected
            inspect_this = True

        basket_no = plc_input.basket_no if plc_input else 0
        loader_id = plc_input.loader_id if plc_input else 0
        material_no = plc_input.material_no if plc_input else 0

        if inspect_this:
            # ── Path B: material in inspection list → full inspection ──
            master_id = material_id
            machine_id = str(loader_id) if loader_id else ""
            basket_id = str(basket_no) if basket_no else ""

            logger.info(
                "Capture mode: material=%s in inspection list %s — running full inspection",
                material_id, self.state.inspection_material_ids,
            )

            self._inspect_and_report(
                images.vl, images.uv, images.tail,
                material_id, master_id, machine_id, basket_id,
                cap_counter if cap_counter else self._frame_counter,
                loader_id, material_no, basket_no,
            )

            del images
            return

        if not capture_this:
            # ── Path C: not in capture list and not in inspection list → skip ──
            logger.info(
                "Capture mode: material=%s not in capture or inspection list — skipping (ack only)",
                material_id,
            )
            # Still need to ack PLC so conveyor keeps moving
            if self._plc and self._plc.connected:
                from plc.data_types import PLCOutput
                plc_output = PLCOutput(
                    result_code=0,
                    camera_error=0,
                    basket_no=basket_no,
                    material_no=material_no,
                    loader_no=loader_id,
                    defect_type_code=0,
                )
                if not self.state.trial_mode:
                    if not self._plc.write_output(plc_output):
                        logger.error("PLC write_output failed in capture skip path — ack still sent")
                    self._plc.ack_complete()
            del images
            return

        # ── Path A: material IS in capture list → save images + result=0 ──
        logger.info(
            "Capture mode: material=%s in capture list — saving images, result=0",
            material_id,
        )

        capture_module = self.state.capture_module or "tube"
        saved_paths: dict[str, str] = {}  # cam_key -> relative path

        # Extract and save processed crop per module:
        #   stain     → VL annular cone crop 256×256 (same as tube but cone not tube)
        #   uv        → UV annular cone crop 256×256 (UV YOLO detector)
        #   tail      → Tail frame top-60% crop (tail always in upper portion)
        #   dimension → VL full frame (YOLO runs at calibration time, not here)
        #   tube      → handled by _run_capture_for_material(), not this path

        def _save_crop(crop, cam_key: str) -> None:
            if crop is None or crop.size == 0:
                return
            try:
                session_id = self.state.capture_session_id
                is_teach_session = self.state.teach_session_start_time > 0 and session_id

                if is_teach_session:
                    # Teach session: save to captures/sessions/<module>/<session_id>/images/<sample_counter>.png
                    images_dir = Path(self._data_root) / "captures" / "sessions" / capture_module / session_id / "images"
                    images_dir.mkdir(parents=True, exist_ok=True)
                    capture_index = cap_counter if cap_counter else self._frame_counter
                    fname = f"{capture_index}.png"
                    rel_path = str(Path("captures") / "sessions" / capture_module / session_id / "images" / fname)
                    cv2.imwrite(str(images_dir / fname), crop)
                    saved_paths[cam_key] = rel_path
                else:
                    # Legacy: save to captures/<module>/<material_id>/<cam_key>/<ts>.png
                    rel_dir = Path("captures") / capture_module / material_id / cam_key
                    save_dir = Path(self._data_root) / rel_dir
                    save_dir.mkdir(parents=True, exist_ok=True)
                    ts_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
                    fname = f"{ts_str}_{self._frame_counter}.png"
                    cv2.imwrite(str(save_dir / fname), crop)
                    saved_paths[cam_key] = str(rel_dir / fname)
            except Exception as e:
                logger.warning("Failed to save %s crop for module=%s: %s", cam_key, capture_module, e)

        if capture_module == "stain":
            # VL annular cone crop — same YOLO path as tube but saves cone surface
            crop = self._extract_cone_annular_crop(images.vl)
            if crop is None:
                logger.warning("Stain capture: YOLO no detection frame=%d — skipping", self._frame_counter)
            else:
                _save_crop(crop, "VL")

        elif capture_module == "uv":
            # UV annular cone crop — use UV YOLO detector
            crop = self._extract_uv_annular_crop(images.uv)
            if crop is None:
                logger.warning("UV capture: YOLO no detection frame=%d — skipping", self._frame_counter)
            else:
                _save_crop(crop, "UV")

        elif capture_module == "tail":
            # Tail frame top-60% crop — tail yarn always hangs from top of cone
            if images.tail is not None:
                h = images.tail.shape[0]
                crop = images.tail[:int(h * 0.6), :]
                _save_crop(crop, "Tail")
            else:
                logger.warning("Tail capture: no tail frame frame=%d", self._frame_counter)

        elif capture_module == "dimension":
            # Full VL frame — YOLO bbox measurement runs at calibration time
            if images.vl is not None:
                _save_crop(images.vl, "VL")

        else:
            # Fallback — save VL full frame
            if images.vl is not None:
                _save_crop(images.vl, "VL")

        # Write captured image record to SQLite
        import uuid as _uuid
        session_id = self.state.capture_session_id
        if session_id and saved_paths:
            try:
                image_id = str(_uuid.uuid4())
                self._db_conn.execute(
                    """INSERT INTO captured_images
                       (image_id, session_id, material_id, module, captured_at,
                        vl_path, uv_path, tail_path)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        image_id, session_id, material_id, capture_module,
                        datetime.now(timezone.utc).isoformat(),
                        saved_paths.get("VL"), saved_paths.get("UV"), saved_paths.get("Tail"),
                    ),
                )
                self._db_conn.execute(
                    "UPDATE capture_sessions SET images_saved = images_saved + 1 WHERE session_id = ?",
                    (session_id,),
                )
                self._db_conn.commit()

                # ── Teach session: emit progress + auto-stop ──
                if self.state.teach_session_start_time > 0:
                    with self.state.lock:
                        self.state.teach_capture_count += 1
                        count = self.state.teach_capture_count
                    self._emit_teaching_alert(
                        module=capture_module,
                        material_id=material_id,
                        stage="capturing",
                        message=f"Teach capture: {count}/{self._installation_min_capture} images",
                        count=count,
                        total=self._installation_min_capture,
                    )
                    # Auto-stop when target count reached
                    if count >= self._installation_min_capture:
                        logger.info(
                            "Teach auto-stop: session=%s reached %d images",
                            session_id, count,
                        )
                        self._finalize_teach_session(
                            session_id=session_id,
                            module=capture_module,
                            count=count,
                            stopped_by="auto",
                        )

            except Exception as e:
                logger.warning("Failed to write captured_images record to SQLite: %s", e)

        # Stream raw images to UI in report format (frontend expects type "report")
        vl_b64 = self._encode_frame(
            images.vl,
            max_w=self._report_width,
            max_h=self._report_height,
            quality=self._report_quality,
        ) if images.vl is not None else ""
        uv_b64 = self._encode_frame(
            images.uv,
            max_w=self._report_width,
            max_h=self._report_height,
            quality=self._report_quality,
        ) if images.uv is not None else ""
        tail_b64 = self._encode_frame(
            images.tail,
            max_w=self._report_width,
            max_h=self._report_height,
            quality=self._report_quality,
        ) if images.tail is not None else ""

        self._queue_result({
            "type": "report",
            "material_id": material_id,
            "master_id": "",
            "machine_id": "",
            "basketid": str(basket_no) if basket_no else "",
            "sample_counter": cap_counter if cap_counter else self._frame_counter,
            "frame_number": self._frame_counter,
            "date_time": datetime.now(timezone.utc).isoformat(),
            "result": "None",
            "defect_type": "",
            "visible": vl_b64,
            "uv": uv_b64,
            "yarntail": tail_b64,
            "stain": True,
            "tube_pattern": True,
            "cone_diameter": True,
            "tube_diameter": True,
            "yarn_res": True,
            "thread_mix": True,
            **self._camera_status_dict(),
        })

        # Write result=0 to PLC — cone stays on conveyor (capture, not inspected)
        if self._plc and self._plc.connected:
            from plc.data_types import PLCOutput
            plc_output = PLCOutput(
                result_code=0,
                camera_error=0,
                basket_no=basket_no,
                material_no=material_no,
                loader_no=loader_id,
                defect_type_code=0,
            )
            if not self._plc.write_output(plc_output):
                logger.error("PLC write_output failed in capture save path — ack still sent")
            self._plc.ack_complete()

        self._pause_runtime_if_requested()

        # ── Auto-teaching: increment capture count + trigger training at threshold ──
        if capture_module == "tube" and material_id and material_id != "unknown":
            with self.state.lock:
                self.state.capture_counts[material_id] = (
                    self.state.capture_counts.get(material_id, 0) + 1
                )
                count = self.state.capture_counts[material_id]

            self._emit_teaching_alert(
                module="tube",
                material_id=material_id,
                stage="capturing",
                message=f"Auto-capture: {count}/{self._tube_min_capture} images",
                count=count,
                total=self._tube_min_capture,
            )

            if (count >= self._tube_min_capture
                    and material_id not in self.state.auto_teaching_triggered):
                with self.state.lock:
                    self.state.auto_teaching_triggered.add(material_id)
                    # Remove from capture set — no more saving needed
                    self.state.capture_material_ids.discard(material_id)

                logger.info(
                    "AUTO-TEACH: material=%s reached %d images — triggering background training",
                    material_id, count,
                )
                self._emit_teaching_alert(
                    module="tube",
                    material_id=material_id,
                    stage="training_started",
                    message=f"Minimum {self._tube_min_capture} images reached — training started",
                    count=count,
                    total=self._tube_min_capture,
                )
                t = threading.Thread(
                    target=self._auto_teach_tube,
                    args=(material_id,),
                    daemon=True,
                    name=f"auto-teach-{material_id}",
                )
                t.start()

        # Cycle summary
        saved_cams = [n for n, f in [("VL", images.vl), ("UV", images.uv), ("Tail", images.tail)] if f is not None]
        plc_summary = ""
        if plc_input:
            plc_summary = (
                f"PLC: material={plc_input.material_no} basket={plc_input.basket_no} "
                f"loader={plc_input.loader_id} counter={plc_input.sample_counter} c2c={plc_input.c2c_start}"
            )
        else:
            plc_summary = "PLC: no data"
        logger.info(
            "═══ Capture #%d DONE ═══ material=%s cameras=%s | %s | saved | result=0 ack=1",
            self._frame_counter, material_id, ",".join(saved_cams), plc_summary,
        )

        del images, vl_b64, uv_b64, tail_b64

    def _extract_cone_annular_crop(self, frame: np.ndarray) -> "np.ndarray | None":
        """Run VL YOLO on frame and return 256x256 annular-masked CONE crop.

        Used for stain capture — saves the cone surface (not tube hole).
        Same YOLO detector as tube, but extracts cone bbox + applies annular mask
        using tube bbox for inner radius.
        Returns None if YOLO does not detect cone + tube.
        """
        try:
            inspector = self._get_vl_inspector()
            detector = inspector.detector
            detections = detector.detect(frame)
            cone_det = None
            tube_det = None
            for det in detections:
                label = str(getattr(det, "label", "") or getattr(det, "class_name", "")).lower()
                if "cone" in label and cone_det is None:
                    cone_det = det
                elif "tube" in label and tube_det is None:
                    tube_det = det

            if cone_det is None:
                return None

            annular_cfg = self.config.get("inspection", {}).get("stain_annular", {})
            outer_scale = float(
                annular_cfg.get("outer_scale", annular_cfg.get("surface_outer_scale", 1.0)) or 1.0
            )
            inner_scale = float(
                annular_cfg.get("inner_scale", annular_cfg.get("surface_inner_scale", 1.0)) or 1.0
            )

            # Extract cone crop
            cx1, cy1, cx2, cy2 = (int(v) for v in cone_det.bbox)
            cone_crop = frame[cy1:cy2, cx1:cx2]
            if cone_crop.size == 0:
                return None

            # Apply annular mask: inner radius from tube bbox, outer from cone bbox
            if tube_det is not None:
                tx1, ty1, tx2, ty2 = (int(v) for v in tube_det.bbox)
                inner_r = float(min(tx2 - tx1, ty2 - ty1)) / 2 * inner_scale
            else:
                inner_r = float(min(cx2 - cx1, cy2 - cy1)) * 0.15 * inner_scale  # fallback 15%

            outer_r = float(min(cx2 - cx1, cy2 - cy1)) / 2 * outer_scale
            if outer_r <= inner_r or outer_r <= 0:
                logger.warning(
                    "_extract_cone_annular_crop invalid annulus: inner=%.1f outer=%.1f",
                    inner_r,
                    outer_r,
                )
                return None

            center_x = int((cx2 - cx1) / 2)
            center_y = int((cy2 - cy1) / 2)

            import numpy as _np
            h, w = cone_crop.shape[:2]
            Y, X = _np.ogrid[:h, :w]
            dist = _np.sqrt((X - center_x) ** 2 + (Y - center_y) ** 2)
            mask = ((dist >= inner_r) & (dist <= outer_r)).astype(_np.uint8) * 255
            masked = cone_crop.copy()
            masked[mask == 0] = 0
            return cv2.resize(masked, (256, 256))
        except Exception as e:
            logger.warning("_extract_cone_annular_crop failed: %s", e)
            return None

    def _extract_uv_annular_crop(self, frame: np.ndarray) -> "np.ndarray | None":
        """Run UV YOLO on frame and return 256x256 annular-masked cone crop.

        Used for UV capture — same annular approach but using UV YOLO detector
        and UV camera frame. UV detector trained on UV images.
        Returns None if YOLO does not detect cone + tube.
        """
        try:
            inspector = self._get_uv_inspector()
            detector = inspector.detector
            detections = detector.detect(frame)
            cone_det = None
            tube_det = None
            for det in detections:
                label = str(getattr(det, "label", "") or getattr(det, "class_name", "")).lower()
                if "cone" in label and cone_det is None:
                    cone_det = det
                elif "tube" in label and tube_det is None:
                    tube_det = det

            if cone_det is None:
                return None

            cx1, cy1, cx2, cy2 = (int(v) for v in cone_det.bbox)
            cone_crop = frame[cy1:cy2, cx1:cx2]
            if cone_crop.size == 0:
                return None

            if tube_det is not None:
                tx1, ty1, tx2, ty2 = (int(v) for v in tube_det.bbox)
                inner_r = float(min(tx2 - tx1, ty2 - ty1)) / 2
            else:
                inner_r = float(min(cx2 - cx1, cy2 - cy1)) * 0.15

            outer_r = float(min(cx2 - cx1, cy2 - cy1)) / 2
            center_x = int((cx2 - cx1) / 2)
            center_y = int((cy2 - cy1) / 2)

            import numpy as _np
            h, w = cone_crop.shape[:2]
            Y, X = _np.ogrid[:h, :w]
            dist = _np.sqrt((X - center_x) ** 2 + (Y - center_y) ** 2)
            mask = ((dist >= inner_r) & (dist <= outer_r)).astype(_np.uint8) * 255
            masked = cone_crop.copy()
            masked[mask == 0] = 0
            return cv2.resize(masked, (256, 256))
        except Exception as e:
            logger.warning("_extract_uv_annular_crop failed: %s", e)
            return None

    def _extract_tube_annular_crop(
        self, frame: np.ndarray
    ) -> "tuple[np.ndarray | None, float, float]":
        """Run YOLO on VL frame and return (256x256 annular tube crop, cone_mm, tube_mm).

        Mirrors the inspection pipeline's extract_annular_roi() path exactly.
        Crop is None if YOLO does not detect cone + tube; diameters are 0.0
        when not measurable. The same YOLO pass yields both bboxes, so the
        cone/tube diameters come for free — used to build real (not fabricated)
        auto-created recipes.
        Called during auto-capture to save crops instead of full frames —
        reduces disk usage and eliminates re-detection at train time.
        """
        try:
            inspector = self._get_vl_inspector()
            detector = inspector.detector
            detections = detector.detect(frame)
            cone_det = None
            tube_det = None
            for det in detections:
                label = str(getattr(det, "label", "") or getattr(det, "class_name", "")).lower()
                if "cone" in label and cone_det is None:
                    cone_det = det
                elif "tube" in label and tube_det is None:
                    tube_det = det

            if cone_det is None or tube_det is None:
                return None, 0.0, 0.0

            # Measure both diameters from the bboxes we already have.
            # dim_checker is None when dimension_check is disabled — fall back
            # to the same calibration constant from config.
            cone_mm = tube_mm = 0.0
            try:
                dim_checker = getattr(inspector, "dim_checker", None)
                if dim_checker is not None:
                    cone_mm = dim_checker.measure_cone(cone_det.bbox)
                    tube_mm = dim_checker.measure_tube(tube_det.bbox)
                else:
                    ppm = float(self.config.get("inspection", {}).get("pixels_per_mm", 0) or 0)
                    if ppm > 0:
                        cx1, cy1, cx2, cy2 = cone_det.bbox
                        tx1, ty1, tx2, ty2 = tube_det.bbox
                        cone_mm = round(min(cx2 - cx1, cy2 - cy1) / ppm, 1)
                        tube_mm = round(min(tx2 - tx1, ty2 - ty1) / ppm, 1)
            except Exception as e:
                logger.warning("Auto-teach dimension measurement failed: %s", e)

            inner_ratio = getattr(
                inspector,
                "tube_inner_ratio",
                self.config.get("inspection", {}).get("tube_pattern", {}).get("inner_ratio", 0.8),
            )

            # Use existing extract_annular_roi from YOLODetector — same as inspection path
            crop = detector.extract_annular_roi(frame, tube_det, inner_ratio=inner_ratio)
            if crop is None or crop.size == 0:
                return None, cone_mm, tube_mm

            return cv2.resize(crop, (256, 256)), cone_mm, tube_mm
        except Exception as e:
            logger.warning("_extract_tube_annular_crop failed: %s", e)
            return None, 0.0, 0.0

    def _emit_teaching_alert(
        self,
        module: str,
        material_id: str,
        stage: str,
        message: str,
        count: int = 0,
        total: int = 0,
    ) -> None:
        """Emit a teaching_alert socket event to all connected clients.

        Args:
            module: Teaching module — 'tube', 'stain', 'uv', 'tail', 'dimension'
            material_id: Material being taught (empty string for global modules like stain)
            stage: Current stage — 'capturing', 'training_started', 'training_complete',
                   'training_failed', 'ready'
            message: Human-readable status message shown on HMI alert panel
            count: Current image count (for capturing stage progress)
            total: Target image count (for capturing stage progress)
        """
        payload = {
            "module": module,
            "material_id": material_id,
            "stage": stage,
            "message": message,
            "count": count,
            "total": total,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.sio.emit("teaching_alert", payload)
        except Exception as e:
            logger.warning("Failed to emit teaching_alert: %s", e)
        logger.info(
            "TEACHING [%s] material=%s stage=%s: %s",
            module, material_id, stage, message,
        )

    def _emit_drift_alert(self, material_id: str, consecutive_fails: int) -> None:
        """Emit a drift_alert socket event when tube pattern rejects a material
        N times in a row (see _tube_drift_alarm_threshold).

        Genuinely defective product occurring in an unbroken streak this long
        is very unlikely — a long consecutive-reject run for one material is a
        much stronger signal of drift (print quality, lighting, miscalibrated
        threshold) than any single reject, and needs an operator's attention
        regardless of root cause.

        Args:
            material_id: Material currently failing tube pattern repeatedly.
            consecutive_fails: Current length of the reject streak.
        """
        message = (
            f"Tube pattern for material '{material_id}' has failed "
            f"{consecutive_fails} consecutive cones — possible drift "
            f"(lighting, print quality, or threshold miscalibration), not "
            f"random defects. Recommend checking the line and this "
            f"material's threshold."
        )
        payload = {
            "material_id": material_id,
            "consecutive_fails": consecutive_fails,
            "threshold": self._tube_drift_alarm_threshold,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.sio.emit("drift_alert", payload)
        except Exception as e:
            logger.warning("Failed to emit drift_alert: %s", e)
        logger.error(
            "DRIFT ALARM: material=%s consecutive_fails=%d (threshold=%d): %s",
            material_id, consecutive_fails, self._tube_drift_alarm_threshold, message,
        )

    # ── Manifest-driven teach capture helpers ───────────────────────

    MODULE_TO_CAMERA = {
        "tail":      "Tail",
        "uv":        "UV",
        "stain":     "VL",
        "dimension": "VL",
    }

    def _get_device_identity(self) -> dict:
        """Read Jetson serial number and derive a short unit ID.

        Reads /proc/device-tree/serial-number on Jetson hardware.
        Falls back to config.device.serial on dev machines.
        Returns dict with jetson_unit_id, jetson_serial, app_version.
        """
        import hashlib
        try:
            with open("/proc/device-tree/serial-number") as f:
                serial = f.read().strip().rstrip("\x00")
        except Exception:
            serial = self.config.get("device", {}).get("serial", "0000000000000")
        unit_id = f"JX-{hashlib.sha256(serial.encode()).hexdigest()[:6].upper()}"
        return {
            "jetson_unit_id": unit_id,
            "jetson_serial": serial,
            "app_version": self.config.get("app_version", "3.0.0"),
        }

    def _write_manifest(self, session_id: str, module: str,
                        operator_id: str) -> None:
        """Create the initial manifest.json for a teach capture session.

        Written to <data_root>/sessions/<module>/<session_id>/manifest.json
        with status OPEN, captured_count 0, and device identity.
        """
        session_dir = Path(self._data_root) / "captures" / "sessions" / module / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        images_dir = session_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc).isoformat()
        device = self._get_device_identity()

        camera_name = self.MODULE_TO_CAMERA.get(module, "VL")

        manifest = {
            "schema_version": "1.0",
            "session_id": session_id,
            "created_at": now,
            "capture_status": "OPEN",
            "device": device,
            "operator": {
                "operator_id": operator_id or "",
            },
            "part": {
                "inspection_type": module.upper(),
                "part_id": f"GLOBAL-{module.upper()}",
            },
            "capture": {
                "camera": camera_name,
                "captured_count": 0,
                "capture_duration_sec": 0,
            },
            "roi_config": {
                "enabled": False,
                "x": None,
                "y": None,
                "width": None,
                "height": None,
                "source": "capture_roi_config",
                "applied_at": now,
            },
            "blob": {
                "prefix": f"{device['jetson_unit_id']}/{session_id}/",
            },
        }

        manifest_path = session_dir / "manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        logger.info(
            "Manifest written: %s (status=OPEN, module=%s, operator=%s)",
            manifest_path, module, operator_id,
        )

    def _finalize_teach_session(self, session_id: str, module: str,
                                count: int, stopped_by: str = "auto",
                                pause_runtime: bool = True) -> None:
        """Finalize a teach capture session: update manifest.json and DB.

        Sets capture_status to PENDING_REVIEW, writes final captured_count
        and capture_duration_sec, updates the DB row, clears state, and
        notifies the UI.

        Args:
            session_id: The session to finalize.
            module: Capture module (tail, stain, uv).
            count: Final number of images captured.
            stopped_by: Who stopped it — 'auto', 'operator', 'plc_stop'.
            pause_runtime: Stop acquisition / set ips_status=3 after finalizing.
        """
        duration_sec = int(time.monotonic() - self.state.teach_session_start_time) \
            if self.state.teach_session_start_time > 0 else 0

        # 1. Update manifest.json on disk
        manifest_path = (
            Path(self._data_root) / "captures" / "sessions" / module
            / session_id / "manifest.json"
        )
        if manifest_path.exists():
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)
                manifest["capture_status"] = "PENDING_REVIEW"
                manifest["capture"]["captured_count"] = count
                manifest["capture"]["capture_duration_sec"] = duration_sec
                with open(manifest_path, "w") as f:
                    json.dump(manifest, f, indent=2)
                logger.info("Manifest updated: %s → PENDING_REVIEW (%d images, %ds)",
                            manifest_path, count, duration_sec)
            except Exception as e:
                logger.warning("Failed to update manifest.json for session %s: %s", session_id, e)

        # 2. Update capture_sessions in SQLite
        try:
            self._db_conn.execute(
                """UPDATE capture_sessions
                   SET capture_status = 'PENDING_REVIEW',
                       stopped_at = ?,
                       stopped_by = ?
                   WHERE session_id = ?""",
                (datetime.now(timezone.utc).isoformat(), stopped_by, session_id),
            )
            self._db_conn.commit()
        except Exception as e:
            logger.warning("Failed to update capture_sessions for session %s: %s", session_id, e)

        # 3. Clear teach capture state
        saved_module = module  # save before clearing
        with self.state.lock:
            self.state.capture_session_id = ""
            self.state.capture_module = ""
            self.state.teach_capture_count = 0
            self.state.teach_session_start_time = 0.0
        if pause_runtime:
            self._request_pause_runtime_after_cycle("teach complete")
        else:
            logger.info(
                "Teach session finalized without pausing runtime "
                "(parallel data-capture workflow)"
            )

        # 4. Notify UI
        self._emit_teaching_alert(
            module=saved_module,
            material_id="GLOBAL-" + saved_module.upper(),
            stage="complete",
            message=f"Capture complete — {count} images ready for review",
            count=count,
            total=self._installation_min_capture,
        )

        logger.info(
            "Teach session finalized: session=%s module=%s count=%d stopped_by=%s",
            session_id, saved_module, count, stopped_by,
        )

        # 5. Reset teach config flag to false (auto-stop)
        try:
            config_path = Path(__file__).parent.parent / "config.json"
            if config_path.exists():
                with open(config_path) as f:
                    cfg = json.load(f)
                cfg.setdefault("inspection", {}).setdefault("teach", {})[saved_module] = False
                with open(config_path, "w") as f:
                    json.dump(cfg, f, indent=4)
                logger.info("Teach config reset: %s = false (auto-stop after %d images)", saved_module, count)
        except Exception as e:
            logger.warning("Failed to reset teach config for %s: %s", saved_module, e)

    def _get_tube_teacher(self):
        """Lazy-load TubeTeacher instance (shared across auto-teach calls)."""
        if self._tube_teacher is None:
            from teaching.tube_teacher import TubeTeacher

            insp_cfg = self.config.get("inspection", {})
            weights = insp_cfg.get("weights", {})
            tube_cfg = insp_cfg.get("tube_pattern", {})
            teach_cfg = self.config.get("teaching", {})
            exclude_cfg = tube_cfg.get("exclude_clock_range")
            exclude_enabled = tube_cfg.get("exclude_clock_range_enabled", False)

            self._tube_teacher = TubeTeacher(
                yolo_weights=weights.get("visible", "weights/visible_yolo.pt"),
                yolo_conf=insp_cfg.get("yolo_conf", 0.6),
                template_dir=self._tube_template_dir,
                bilateral_d=tube_cfg.get("bilateral_d", 9),
                bilateral_sigma_color=tube_cfg.get("bilateral_sigma_color", 75),
                bilateral_sigma_space=tube_cfg.get("bilateral_sigma_space", 75),
                device=teach_cfg.get("device", "auto"),
                exclude_clock_range=tuple(exclude_cfg) if exclude_enabled and exclude_cfg else None,
                outlier_rejection_enabled=teach_cfg.get("outlier_rejection_enabled", True),
                augmentation_enabled=teach_cfg.get("augmentation_enabled", False),
                augment_count=teach_cfg.get("augment_count", 0),
            )

            logger.info("TubeTeacher lazy-loaded: template_dir=%s", tube_cfg.get("template_dir", self._tube_template_dir))

        return self._tube_teacher

    # The five toggleable inspection modules and their defaults when the key
    # is absent — must mirror the gate defaults in this file and visible.py.
    TASK_DEFAULTS = {
        "dimension_check": True,
        "stain_detection": True,
        "tube_pattern": True,
        "uv_inspection": False,
        "tail_inspection": True,
    }

    # Which inspection tasks each camera feeds — used to force tasks off
    # when their camera is disabled via cameras.<name>.enabled = false.
    CAMERA_TASKS = {
        "VL": ("dimension_check", "stain_detection", "tube_pattern"),
        "UV": ("uv_inspection",),
        "TAIL": ("tail_inspection",),
    }

    def _apply_camera_task_mask(self, tasks: dict) -> dict:
        """Force off every task whose camera is disabled via config.

        Applied to every global-tasks read (disk or in-memory), so neither
        a UI toggle (PUT /config/tasks) nor a material recipe override can
        run an inspection whose camera never captures a frame.
        """
        if not self._disabled_cameras:
            return tasks
        masked = dict(tasks)
        for cam_name in self._disabled_cameras:
            for task in self.CAMERA_TASKS.get(cam_name, ()):
                if masked.get(task, self.TASK_DEFAULTS.get(task, True)):
                    logger.debug(
                        "Task '%s' forced off — camera '%s' is disabled", task, cam_name
                    )
                masked[task] = False
        return masked

    def _read_debug_save_enabled(self) -> bool:
        """Read debug.save_frames fresh from config.json on disk.

        Same live-reload pattern as _read_tasks_config(): flipping the flag
        in config.json takes effect on the next cone, no restart needed.
        Falls back to in-memory config if the disk read fails. Default true
        (unchanged behavior) when the key is absent.
        """
        try:
            config_path = Path(__file__).parent.parent / "config.json"
            if config_path.exists():
                with open(config_path) as f:
                    cfg = json.load(f)
                return bool(cfg.get("debug", {}).get("save_frames", True))
        except Exception as e:
            logger.warning("Failed to read debug config from disk: %s — using in-memory", e)
        return bool(self.config.get("debug", {}).get("save_frames", True))

    def _read_tasks_config(self) -> dict:
        """Read global inspection.tasks fresh from config.json on disk.

        Mirrors _read_teach_config(): reading from disk ensures UI toggles
        (PUT /config/tasks on the API service) apply on the next cone
        without restarting this service. Falls back to in-memory config
        if the disk read fails. Tasks whose camera is disabled
        (cameras.<name>.enabled = false) are always forced off.
        """
        try:
            config_path = Path(__file__).parent.parent / "config.json"
            if config_path.exists():
                with open(config_path) as f:
                    cfg = json.load(f)
                return self._apply_camera_task_mask(cfg.get("inspection", {}).get("tasks", {}))
        except Exception as e:
            logger.warning("Failed to read tasks config from disk: %s — using in-memory", e)
        return self._apply_camera_task_mask(self.config.get("inspection", {}).get("tasks", {}))

    def _read_material_tasks(self, material_id) -> dict:
        """Read per-material task overrides from data/recipes/{id}.json.

        Returns the recipe's "tasks" dict, or {} when the material has no
        recipe / no overrides (= inherit global). Read fresh from disk so
        UI edits (PUT /config/materials/{id}/tasks) apply on the next cone.
        """
        mid = str(material_id or "").strip()
        if not mid or mid == "unknown":
            return {}
        try:
            recipe_dir = self.config.get("inspection", {}).get("recipe_dir", "data/recipes")
            path = Path(recipe_dir) / f"{mid}.json"
            if path.exists():
                with open(path) as f:
                    recipe = json.load(f)
                tasks = recipe.get("tasks")
                if isinstance(tasks, dict):
                    return tasks
        except Exception as e:
            logger.warning("Failed to read material tasks for '%s': %s — using global only", mid, e)
        return {}

    def _effective_tasks(self, material_id) -> dict:
        """Effective task flags for this cone: global AND per-material.

        Global is the master switch (globally off = off for everyone);
        a material override can only disable a task for that material.
        Always returns all five task keys.
        """
        global_tasks = self._read_tasks_config()
        overrides = self._read_material_tasks(material_id)
        effective = {}
        for key, default in self.TASK_DEFAULTS.items():
            enabled = bool(global_tasks.get(key, default)) and bool(overrides.get(key, True))
            effective[key] = enabled
            if not enabled and key in overrides and not overrides[key] and bool(global_tasks.get(key, default)):
                logger.debug("Task '%s' disabled by material '%s' override", key, material_id)
        return effective

    def _read_teach_config(self) -> dict:
        """Read teach config fresh from config.json on disk every cycle.

        Returns the inspection.teach dict, e.g.:
            {"stain": true, "uv": false, "tail": true, "dimension": false}

        Falls back to in-memory config if disk read fails.
        Reading from disk ensures that UI toggle changes (PUT /config/teach)
        are picked up immediately without restarting the service.
        """
        try:
            config_path = Path(__file__).parent.parent / "config.json"
            if config_path.exists():
                with open(config_path) as f:
                    cfg = json.load(f)
                return cfg.get("inspection", {}).get("teach", {})
        except Exception as e:
            logger.warning("Failed to read teach config from disk: %s — using in-memory", e)
        return self.config.get("inspection", {}).get("teach", {})

    def _any_teach_active(self, teach_cfg: dict = None) -> bool:
        """Check if any installation-site teach toggle is active.

        Teach modules: tail, uv, stain, dimension.
        NOTE: 'tube' is NOT a valid teach toggle — tube teaches autonomously
        via auto-teach when .npz is missing. This method intentionally excludes
        tube from the check.

        Args:
            teach_cfg: Pre-read teach config dict. If None, reads from disk.

        Returns:
            True if any of tail/uv/stain/dimension is True in config.
        """
        if teach_cfg is None:
            teach_cfg = self._read_teach_config()
        return any(
            teach_cfg.get(mod, False)
            for mod in ("tail", "uv", "stain", "dimension")
        )

    def _active_teach_session_from_config(self):
        """Return active teach session from disk config + runtime session state.

        The config is the source of truth for whether saving should happen on
        this cycle. Runtime state supplies the session_id created by /config/teach.
        """
        teach_cfg = self._read_teach_config()
        active_modules = [
            mod for mod in ("tail", "uv", "stain", "dimension")
            if teach_cfg.get(mod, False)
        ]
        if not active_modules:
            return None, None

        module = active_modules[0]
        with self.state.lock:
            session_id = self.state.capture_session_id
            start_time = self.state.teach_session_start_time
            state_module = self.state.capture_module
            if session_id and state_module != module:
                logger.info(
                    "Teach config module=%s differs from runtime module=%s — using config module",
                    module,
                    state_module,
                )
                self.state.capture_module = module

        if not session_id or start_time <= 0:
            logger.debug(
                "Teach config active for %s but no active capture session is registered",
                module,
            )
            return None, None

        return session_id, module

    def _resume_teach_session_if_any(self):
        """Re-attach an OPEN teach session after an inspection stop/start.

        A mid-session Stop pauses the session (DB row stays OPEN) but clears
        the runtime registration. Without re-attaching, config says teach ON
        while nothing captures — the operator sees "2/200 then silence".
        Resumes the counter from the DB's images_saved so auto-stop still
        fires at exactly the target total.
        See docs/critical_fixes/teach_session_stop_resume_plan.md.
        """
        teach_cfg = self._read_teach_config()
        active_modules = [
            m for m in ("tail", "uv", "stain", "dimension")
            if teach_cfg.get(m, False)
        ]
        if not active_modules:
            return
        module = active_modules[0]

        with self.state.lock:
            if self.state.capture_session_id:
                return  # freshly toggled ON — already registered

        try:
            row = self._db_conn.execute(
                """SELECT session_id, images_saved FROM capture_sessions
                   WHERE module = ? AND capture_status = 'OPEN'
                   ORDER BY started_at DESC LIMIT 1""",
                (module,),
            ).fetchone()
        except Exception as e:
            logger.warning("Teach session resume lookup failed: %s", e)
            return

        if row is None:
            # Stale flag: config says teach ON but no OPEN session exists
            # (reviewed/uploaded/deleted meanwhile). Reset so the toggle can
            # never point at a session that is gone.
            logger.warning(
                "Teach config '%s' is ON but no OPEN session exists — resetting flag to false",
                module,
            )
            self._reset_teach_config_flag(module)
            return

        session_id = row[0]
        images_saved = int(row[1] or 0)
        with self.state.lock:
            self.state.capture_session_id = session_id
            self.state.capture_module = module
            self.state.teach_capture_count = images_saved
            self.state.teach_session_start_time = time.monotonic()

        logger.info(
            "Teach session resumed: module=%s session=%s at %d/%d",
            module, session_id, images_saved, self._installation_min_capture,
        )

    def _reset_teach_config_flag(self, module: str):
        """Set inspection.teach.<module> = false in config.json on disk."""
        try:
            config_path = Path(__file__).parent.parent / "config.json"
            if config_path.exists():
                with open(config_path) as f:
                    cfg = json.load(f)
                cfg.setdefault("inspection", {}).setdefault("teach", {})[module] = False
                with open(config_path, "w") as f:
                    json.dump(cfg, f, indent=4)
                logger.info("Teach config reset: %s = false (stale flag)", module)
        except Exception as e:
            logger.warning("Failed to reset teach config for %s: %s", module, e)

    def _save_active_teach_session_image(
        self,
        *,
        material_id: str,
        sample_counter: int,
        vl_frame,
        uv_frame,
        tail_frame,
        source: str,
    ) -> None:
        """Save one active install-site teach image from already captured frames.

        This is used by normal inspection and tube auto-teach so teach data is
        captured from the same original frames that the conveyor cycle produced.
        """
        session_id, module = self._active_teach_session_from_config()
        if not session_id or not module:
            return

        try:
            teach_frame = None
            cam_key = "VL"

            if module == "tail":
                cam_key = "Tail"
                teach_frame = tail_frame
            elif module == "uv":
                cam_key = "UV"
                teach_frame = uv_frame
            elif module == "stain":
                cam_key = "VL"
                if vl_frame is not None:
                    teach_frame = self._extract_cone_annular_crop(vl_frame)
            elif module == "dimension":
                cam_key = "VL"
                teach_frame = vl_frame
            else:
                logger.warning("Unknown teach module '%s' — skipping teach save", module)
                return

            if teach_frame is None or getattr(teach_frame, "size", 0) == 0:
                logger.warning(
                    "Teach capture skipped source=%s module=%s material=%s frame=%d — no frame/crop",
                    source,
                    module,
                    material_id,
                    self._frame_counter,
                )
                return

            images_dir = (
                Path(self._data_root) / "captures" / "sessions"
                / module / session_id / "images"
            )
            images_dir.mkdir(parents=True, exist_ok=True)
            capture_index = sample_counter if sample_counter else self._frame_counter
            fname = f"{capture_index}.png"
            rel_path = Path("captures") / "sessions" / module / session_id / "images" / fname
            full_path = images_dir / fname

            if not cv2.imwrite(str(full_path), teach_frame):
                logger.warning("Teach capture failed to write %s", full_path)
                return

            saved_paths = {"VL": None, "UV": None, "Tail": None}
            saved_paths[cam_key] = str(rel_path)
            try:
                import uuid as _uuid

                image_id = str(_uuid.uuid4())
                self._db_conn.execute(
                    """INSERT INTO captured_images
                       (image_id, session_id, material_id, module, captured_at,
                        vl_path, uv_path, tail_path)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        image_id, session_id, material_id, module,
                        datetime.now(timezone.utc).isoformat(),
                        saved_paths.get("VL"),
                        saved_paths.get("UV"),
                        saved_paths.get("Tail"),
                    ),
                )
                self._db_conn.execute(
                    "UPDATE capture_sessions SET images_saved = images_saved + 1 WHERE session_id = ?",
                    (session_id,),
                )
                self._db_conn.commit()
            except Exception as e:
                logger.warning("Failed to write teach captured_images record: %s", e)

            with self.state.lock:
                self.state.teach_capture_count += 1
                count = self.state.teach_capture_count

            logger.info(
                "Teach capture saved source=%s module=%s material=%s path=%s",
                source,
                module,
                material_id,
                full_path,
            )
            self._emit_teaching_alert(
                module=module,
                material_id=material_id,
                stage="capturing",
                message=f"Teach capture: {count}/{self._installation_min_capture} images",
                count=count,
                total=self._installation_min_capture,
            )

            if count >= self._installation_min_capture:
                logger.info(
                    "Teach auto-stop (%s): session=%s reached %d images",
                    source,
                    session_id,
                    count,
                )
                self._finalize_teach_session(
                    session_id=session_id,
                    module=module,
                    count=count,
                    stopped_by="auto",
                    pause_runtime=False,
                )
        except Exception as e:
            logger.warning("Teach frame save failed source=%s: %s", source, e)
    
    def _count_tube_crops(self, material_id: str) -> int:
        capture_dir = Path(self._data_root) / "captures" / "tube" / material_id / "crops"
        if not capture_dir.exists():
            return 0
        return len(list(capture_dir.glob("*.png")))

    def _run_capture_for_material(
        self,
        material_id: str,
        basket_no: int,
        loader_id: int,
        material_no: int,
        cap_counter: int,
    ) -> None:
        """Capture and save one cone for auto-teaching (called from _run_inspection_cycle).

        Reuses the same camera capture + save logic as _run_capture_cycle Path A,
        but called inline from the inspection cycle when a new material is detected.
        """
        if not self._capture_seq:
            logger.warning("AUTO-TEACH: no CaptureSequence — cannot capture for material=%s", material_id)
            return

        try:
            images = self._capture_seq.capture_part()
        except Exception as e:
            logger.exception("AUTO-TEACH: capture failed for material=%s: %s", material_id, e)
            return

        self._save_debug_frames(
            material_id=material_id,
            sample_counter=cap_counter,
            vl_frame=images.vl,
            uv_frame=images.uv,
            tail_frame=images.tail,
            source="auto-teach",
        )

        # Save 256x256 annular tube crop (not full frame) — tube teaching only needs the crop.
        # YOLO + annular extraction runs here so TubeTeacher.teach() just loads crops directly,
        # no re-detection needed at train time. Mirrors the inspection pipeline exactly.
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        if images.vl is not None:
            try:
                crop, cone_mm, tube_mm = self._extract_tube_annular_crop(images.vl)

                if crop is not None:
                    rel_dir = Path("captures") / "tube" / material_id / "crops"
                    save_dir = Path(self._data_root) / rel_dir
                    save_dir.mkdir(parents=True, exist_ok=True)

                    fname = f"{ts_str}_{self._frame_counter}.png"
                    crop_path = save_dir / fname

                    ok = cv2.imwrite(str(crop_path), crop)

                    if ok:
                        logger.info("AUTO-TEACH: saved tube crop to %s", crop_path)
                        # Persist per-cone measured dims beside the crops so
                        # _auto_teach_tube() can build the recipe from real values.
                        # Lives inside crops/ so reteach cleanup removes it too;
                        # training only globs *.png so it never sees this file.
                        if cone_mm > 0 or tube_mm > 0:
                            try:
                                with open(save_dir / "dims.jsonl", "a") as f:
                                    f.write(json.dumps(
                                        {"ts": ts_str, "cone_mm": cone_mm, "tube_mm": tube_mm}
                                    ) + "\n")
                                logger.info(
                                    "AUTO-TEACH: measured dims material=%s cone=%.1fmm tube=%.1fmm",
                                    material_id, cone_mm, tube_mm,
                                )
                            except Exception as e:
                                logger.warning("AUTO-TEACH: failed to record dims: %s", e)
                    else:
                        logger.warning("AUTO-TEACH: failed to write crop %s", crop_path)

                else:
                    logger.warning(
                        "AUTO-TEACH: YOLO no detection for material=%s frame=%d — skipping save",
                        material_id,
                        self._frame_counter,
                    )
            except Exception as e:
                logger.warning("AUTO-TEACH: failed to save annular crop for material=%s: %s", material_id, e)

        self._save_active_teach_session_image(
            material_id=material_id,
            sample_counter=cap_counter,
            vl_frame=images.vl,
            uv_frame=images.uv,
            tail_frame=images.tail,
            source="auto-teach",
        )

        # # Increment counter + check threshold
        # with self.state.lock:
        #     self.state.capture_counts[material_id] = (
        #         self.state.capture_counts.get(material_id, 0) + 1
        #     )
        #     count = self.state.capture_counts[material_id]

        count = self._count_tube_crops(material_id)

        with self.state.lock:
            self.state.capture_counts[material_id] = count

        self._emit_teaching_alert(
            module="tube",
            material_id=material_id,
            stage="capturing",
            message=f"Auto-capture: {count}/{self._tube_min_capture} images",
            count=count,
            total=self._tube_min_capture,
        )

        if (count >= self._tube_min_capture
                and material_id not in self.state.auto_teaching_triggered):
            with self.state.lock:
                self.state.auto_teaching_triggered.add(material_id)
                self.state.capture_material_ids.discard(material_id)

            self._emit_teaching_alert(
                module="tube",
                material_id=material_id,
                stage="training_started",
                message=f"Minimum {self._tube_min_capture} images reached — training started",
                count=count,
                total=self._tube_min_capture,
            )
            t = threading.Thread(
                target=self._auto_teach_tube,
                args=(material_id,),
                daemon=True,
                name=f"auto-teach-{material_id}",
            )
            t.start()

        # Write result=Good to PLC (pass-through — cone not inspected yet)
        if self._plc and self._plc.connected:
            from plc.data_types import PLCOutput
            plc_output = PLCOutput(
                result_code=0,  # Teaching — no inspection decision
                camera_error=0,
                basket_no=basket_no,
                material_no=material_no,
                loader_no=loader_id,
                defect_type_code=0,
            )
            if not self.state.trial_mode:
                if not self._plc.write_output(plc_output):
                    logger.error("AUTO-TEACH: PLC write_output failed — ack still sent")
                self._plc.ack_complete()

        del images

    def _auto_teach_tube(self, material_id: str) -> None:
        """Background thread: train tube pattern master for material_id.

        Runs TubeTeacher.teach() on all captured VL images for this material.
        On success: hot-loads the new master, removes from auto_teaching_triggered,
        emits teaching_alert stage='ready'.
        On failure: removes from auto_teaching_triggered so operator can retry,
        emits teaching_alert stage='training_failed'.
        """
        logger.info("AUTO-TEACH: background training started for material=%s", material_id)
        try:
            teacher = self._get_tube_teacher()

            # Load pre-extracted 256x256 annular crops — no YOLO needed at train time
            capture_dir = Path(self._data_root) / "captures" / "tube" / material_id / "crops"
            if not capture_dir.exists():
                raise FileNotFoundError(f"Crop dir not found: {capture_dir}")

            image_paths = sorted(capture_dir.glob("*.png"))
            if len(image_paths) < self._tube_min_capture:
                raise ValueError(
                    f"Only {len(image_paths)} crops found, need {self._tube_min_capture}"
                )

            logger.info(
                "AUTO-TEACH: training material=%s with %d pre-extracted crops from %s",
                material_id, len(image_paths), capture_dir,
            )

            import cv2 as _cv2
            frames = []
            for p in image_paths:
                frame = _cv2.imread(str(p))
                if frame is not None:
                    frames.append(frame)

            if len(frames) < self._tube_min_capture:
                raise ValueError(f"Could only load {len(frames)} valid crops")

            # Run teaching with pre-extracted crops — TubeTeacher.teach() accepts crops directly
            result = teacher.teach(material_id=material_id, frames=frames, pre_cropped=True)

            if not result.get("success", False):
                raise RuntimeError(result.get("error", "TubeTeacher.teach() returned failure"))

            logger.info(
                "AUTO-TEACH: training complete for material=%s | threshold=%.4f n_refs=%d",
                material_id,
                result.get("color_threshold", 0),
                result.get("n_references", 0),
            )

            # Create recipe file for this material (auto-teach creates template but not recipe).
            # Never overwrite an existing recipe — operator-corrected values must survive reteach.
            recipe_dir = Path(self.config.get("inspection", {}).get("recipe_dir", "data/recipes"))
            recipe_dir.mkdir(parents=True, exist_ok=True)
            recipe_path = recipe_dir / f"{material_id}.json"

            if recipe_path.exists():
                logger.info(
                    "AUTO-TEACH: recipe already exists for material=%s — keeping existing values",
                    material_id,
                )
            else:
                # Use average of per-cone dims measured during auto-capture (dims.jsonl).
                # Average (not max): one or two defective/oversized cones among the
                # 20 teach samples would set max to that outlier, while the mean
                # dilutes them 1/N — residual noise is absorbed by the ±tol.
                cone_vals: list = []
                tube_vals: list = []
                dims_path = capture_dir / "dims.jsonl"
                try:
                    if dims_path.exists():
                        with open(dims_path) as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                rec = json.loads(line)
                                c = float(rec.get("cone_mm", 0) or 0)
                                t = float(rec.get("tube_mm", 0) or 0)
                                if c > 0:
                                    cone_vals.append(c)
                                if t > 0:
                                    tube_vals.append(t)
                except Exception as e:
                    logger.warning("AUTO-TEACH: failed to read dims.jsonl for material=%s: %s", material_id, e)

                cone_dia = sum(cone_vals) / len(cone_vals) if cone_vals else 0.0
                tube_dia = sum(tube_vals) / len(tube_vals) if tube_vals else 0.0

                if cone_dia > 0 and tube_dia > 0:
                    tol = self._auto_recipe_tolerance_mm
                    cone_tol, tube_tol = tol, tol
                    logger.info(
                        "AUTO-TEACH: recipe from measured dims material=%s "
                        "cone=%.1fmm (avg of %d) tube=%.1fmm (avg of %d) tol=±%.1fmm",
                        material_id, cone_dia, len(cone_vals), tube_dia, len(tube_vals), tol,
                    )
                else:
                    # No measurements (e.g. crops from a manual teach session) — fall back to defaults
                    cone_dia, tube_dia = 357.0, 66.0
                    cone_tol, tube_tol = 10.0, 5.0
                    logger.warning(
                        "AUTO-TEACH: no measured dims for material=%s — recipe uses placeholder defaults, "
                        "correct via PATCH /recipes/%s", material_id, material_id,
                    )

                new_recipe = {
                    "material_id": material_id,
                    "master_name": material_id,
                    "cone_diameter_mm": round(cone_dia, 1),
                    "tube_diameter_mm": round(tube_dia, 1),
                    "cone_tolerance_mm": cone_tol,
                    "tube_tolerance_mm": tube_tol,
                    "tasks": {
                        "dimension_check": True,
                        "stain_detection": True,
                        "tube_pattern": True,
                        "uv_inspection": True,
                        "tail_inspection": True,
                    },
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }

                try:
                    with open(recipe_path, "w") as f:
                        json.dump(new_recipe, f, indent=2)
                    logger.info("AUTO-TEACH: created recipe file for material=%s at %s", material_id, recipe_path)
                except Exception as e:
                    logger.warning("AUTO-TEACH: failed to create recipe file for material=%s: %s", material_id, e)

            # Hot-load: reload VL inspector to pick up new .npz
            # Done between cycles — inspector checks template on each call
            try:
                inspector = self._get_vl_inspector()
                if hasattr(inspector, "reload_tube_templates"):
                    inspector.reload_tube_templates()
                else:
                    # Force re-init on next inspection
                    self._vl_inspector = None
            except Exception as e:
                logger.warning("AUTO-TEACH: hot-reload failed (will load on next inspection start): %s", e)

            # Clean up state
            with self.state.lock:
                self.state.auto_teaching_triggered.discard(material_id)
                self.state.capture_counts.pop(material_id, None)

            self._emit_teaching_alert(
                module="tube",
                material_id=material_id,
                stage="ready",
                message=f"material {material_id} taught successfully — now inspecting",
                count=len(frames),
                total=len(frames),
            )

        except Exception as e:
            logger.exception("AUTO-TEACH: training failed for material=%s: %s", material_id, e)

            # Remove from triggered set so operator can retry via reteach
            with self.state.lock:
                self.state.auto_teaching_triggered.discard(material_id)

            self._emit_teaching_alert(
                module="tube",
                material_id=material_id,
                stage="training_failed",
                message=f"Auto-teaching failed for {material_id}: {e}",
                count=0,
                total=self._tube_min_capture,
            )

    def _run_live_feed(self):
        """Stream live camera feed to UI at configured FPS."""
        cam_map = {1: "VL", 2: "UV", 3: "TAIL"}
        cam_name = cam_map.get(self.state.camera_id, "VL")
        cam = self._cameras.get(cam_name)

        if cam is None:
            logger.warning("Camera '%s' not available for live feed", cam_name)
            self._queue_result({
                "type": "error",
                "message": f"Camera '{cam_name}' not connected",
            })
            self.state.set_state(InspectionState.IDLE)
            return

        # Switch to software trigger for continuous capture
        try:
            cam.set_trigger("software")
        except Exception as e:
            logger.error("Failed to set software trigger on '%s': %s", cam_name, e)
            self.state.set_state(InspectionState.IDLE)
            return

        frame_interval = 1.0 / self._live_fps

        try:
            while not self._stop_worker.is_set():
                if self.state.get_state() != InspectionState.LIVE_FEED:
                    break

                t_start = time.perf_counter()

                try:
                    frame = cam.capture(timeout_ms=5000)
                except TimeoutError:
                    continue
                except Exception as e:
                    logger.error("Live feed capture error: %s", e)
                    break

                image_b64 = self._encode_frame(
                    frame,
                    max_w=self._live_width,
                    max_h=self._live_height,
                    quality=self._live_quality,
                )

                self._queue_result({
                    "type": "live_feed",
                    "camera_id": self.state.camera_id,
                    "camera_name": cam_name,
                    "timestamp": time.time(),
                    "image_base64": image_b64,
                })

                # Frame rate control
                elapsed = time.perf_counter() - t_start
                sleep_time = frame_interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
        finally:
            # Restore hardware trigger
            try:
                cam.set_trigger("hardware")
            except Exception:
                pass

    def _change_exposure(self):
        """Change camera exposure and optionally save to config."""
        cam_map = {1: "VL", 2: "UV", 3: "TAIL"}
        cam_name = cam_map.get(self.state.camera_id, "VL")
        cam = self._cameras.get(cam_name)

        if cam is None:
            logger.warning("Camera '%s' not available for exposure change", cam_name)
            return

        try:
            cam.set_exposure(self.state.exposure)
            logger.info("Exposure changed: %s → %d us", cam_name, self.state.exposure)
        except Exception as e:
            logger.error("Failed to set exposure on '%s': %s", cam_name, e)
            return

        if self.state.save_exposure:
            try:
                config_path = Path(__file__).parent.parent / "config.json"
                if config_path.exists():
                    with open(config_path) as f:
                        cfg = json.load(f)
                    # Find camera in config (case-insensitive)
                    for name in cfg.get("cameras", {}):
                        if name.upper() == cam_name:
                            cfg["cameras"][name]["exposure"] = self.state.exposure
                            break
                    with open(config_path, "w") as f:
                        json.dump(cfg, f, indent=4)
                    logger.info("Exposure saved to config for '%s'", cam_name)
            except Exception as e:
                logger.error("Failed to save exposure config: %s", e)

    def _control_light(self, turn_on: bool):
        """Control PLC light output."""
        if not self._plc or not self._plc.connected:
            logger.warning("PLC not connected — cannot control light")
            return

        self._plc.control_light(self.state.light_id, turn_on)

    def _read_light_status(self):
        """Read light status from PLC and emit to UI."""
        lights = {}
        if self._plc and self._plc.connected:
            lights = self._plc.read_light_status()

        self._queue_result({
            "type": "light_status",
            "lights": lights if lights else {
                "uv": False, "vl": False, "yarntail": False, "master": False,
            },
        })

    def _read_plc_config(self):
        """Read all PLC registers and emit to UI."""
        registers = {}

        if self._plc and self._plc.connected:
            # Read input registers 40001-40013 (addresses 0-12)
            input_regs = self._plc.read_registers(0, 13)
            if input_regs:
                registers["sample_counter"] = input_regs[0]   # 40001
                registers["trigger"] = input_regs[1]           # 40002
                registers["c2c_start"] = input_regs[7]         # 40008
                registers["material_no"] = input_regs[8]       # 40009
                registers["basket_no"] = input_regs[11]        # 40012
                registers["loader_id"] = input_regs[12]        # 40013

            # Read output registers 40003-40021 (addresses 2-20)
            output_regs = self._plc.read_registers(2, 19)
            if output_regs:
                registers["result_code"] = output_regs[0]      # 40003
                registers["camera_error"] = output_regs[12]    # 40015
                registers["ips_status"] = output_regs[13]      # 40016
                registers["basket_no_echo"] = output_regs[14]  # 40017
                registers["material_no_echo"] = output_regs[15] # 40018
                registers["loader_no_echo"] = output_regs[16]  # 40019
                registers["defect_type"] = output_regs[17]     # 40020
                registers["ack"] = output_regs[18]             # 40021

            # Read light registers 40005-40007 (addresses 4-6)
            light_regs = self._plc.read_registers(4, 3)
            if light_regs:
                registers["light_uv"] = light_regs[0]          # 40005
                registers["light_vl"] = light_regs[1]          # 40006
                registers["light_yarntail"] = light_regs[2]    # 40007

        self._queue_result({
            "type": "plc_config",
            "registers": registers,
        })

        # Poll interval
        time.sleep(0.5)

    def _check_illumination(self, state: InspectionState):
        """Check illumination calibration by capturing and analyzing brightness."""
        light_map = {
            InspectionState.ILLUM_CHECK_VL: ("vl", "VL"),
            InspectionState.ILLUM_CHECK_UV: ("uv", "UV"),
            InspectionState.ILLUM_CHECK_TAIL: ("tail", "TAIL"),
        }
        light_name, cam_name = light_map.get(state, ("vl", "VL"))
        cam = self._cameras.get(cam_name)

        passed = False
        message = "Camera not available"
        image_b64 = ""

        if cam is not None:
            try:
                cam.set_trigger("software")
                frame = cam.capture(timeout_ms=5000)
                cam.set_trigger("hardware")

                # Check mean brightness (simple illumination check)
                mean_brightness = float(np.mean(frame))
                passed = mean_brightness > 30  # Minimum brightness threshold
                message = f"Mean brightness: {mean_brightness:.1f}"

                image_b64 = self._encode_frame(
                    frame,
                    max_w=self._live_width,
                    max_h=self._live_height,
                    quality=self._live_quality,
                )
            except Exception as e:
                message = f"Capture failed: {e}"
                try:
                    cam.set_trigger("hardware")
                except Exception:
                    pass

        self._queue_result({
            "type": "error_proof",
            "action": "check",
            "light": light_name,
            "passed": passed,
            "message": message,
            "image_base64": image_b64,
        })

    def _save_illumination(self, state: InspectionState):
        """Save illumination calibration baseline."""
        light_map = {
            InspectionState.ILLUM_SAVE_VL: ("vl", "VL"),
            InspectionState.ILLUM_SAVE_UV: ("uv", "UV"),
            InspectionState.ILLUM_SAVE_TAIL: ("tail", "TAIL"),
        }
        light_name, cam_name = light_map.get(state, ("vl", "VL"))
        cam = self._cameras.get(cam_name)

        success = False
        message = "Camera not available"

        if cam is not None:
            try:
                cam.set_trigger("software")
                frame = cam.capture(timeout_ms=5000)
                cam.set_trigger("hardware")

                # Save baseline image
                baseline_dir = Path("data/illumination_baseline")
                baseline_dir.mkdir(parents=True, exist_ok=True)
                path = baseline_dir / f"{light_name}_baseline.png"
                cv2.imwrite(str(path), frame)

                success = True
                message = f"Baseline saved: {path}"
            except Exception as e:
                message = f"Save failed: {e}"
                try:
                    cam.set_trigger("hardware")
                except Exception:
                    pass

        self._queue_result({
            "type": "error_proof",
            "action": "save",
            "light": light_name,
            "success": success,
            "message": message,
        })

    def _run_error_proof(self):
        """Run error proofing defect check."""
        data = self.state.error_proof_data
        logger.info("Running error proof: %s", data)

        self._queue_result({
            "type": "error_proof_defect",
            "error_type": data.get("type", ""),
            "mat_id": data.get("mat_id", ""),
            "master_id": data.get("master_id", ""),
            "success": True,
            "message": "Error proof check completed",
        })

    def _pause_inspection_runtime(self, reason: str = "") -> None:
        """Move runtime to idle and stop active camera/PLC production state."""
        self.state.set_state(InspectionState.IDLE)

        suffix = f" ({reason})" if reason else ""
        if self._capture_seq:
            self._capture_seq.log_stream_statistics()
            self._capture_seq.flush_buffers()
            self._capture_seq.stop_acquisition()
            logger.info("Camera buffers flushed, acquisition stopped%s", suffix)

        if self._plc and self._plc.connected:
            self._plc.write_ips_status(3)
            logger.info("PLC ips_status=3 (Disabled)%s", suffix)

    def _request_pause_runtime_after_cycle(self, reason: str) -> None:
        """Ask the active cone cycle to pause runtime after PLC ack/report work."""
        self._pause_runtime_after_cycle_reason = reason

    def _pause_runtime_if_requested(self) -> None:
        reason = self._pause_runtime_after_cycle_reason
        if not reason:
            return
        self._pause_runtime_after_cycle_reason = ""
        self._pause_inspection_runtime(reason)

    # ── Socket.IO event handlers ─────────────────────────────────────

    def _register_events(self):
        """Register Socket.IO event handlers."""

        @self.sio.event
        def connect(sid, environ):
            self.clients.append(sid)
            set_correlation_id(sid[:8])
            client_ip = environ.get("REMOTE_ADDR", "unknown")
            logger.info(
                "Client connected",
                extra={
                    "event_type": "client_connect",
                    "client_id": sid,
                    "client_ip": client_ip,
                    "total_clients": len(self.clients),
                }
            )
            # Only start one stream worker (not per-connection)
            if not self._stream_worker_started:
                self._stream_worker_started = True
                self.sio.start_background_task(self._stream_results)

        @self.sio.event
        def disconnect(sid):
            if sid in self.clients:
                self.clients.remove(sid)
            logger.info(
                "Client disconnected",
                extra={
                    "event_type": "client_disconnect",
                    "client_id": sid,
                    "total_clients": len(self.clients),
                }
            )

        @self.sio.event
        def start_inspection(sid, data):
            try:
                # Guard: ignore if already running inspection/capture
                current = self.state.get_state()
                if current in (InspectionState.INSPECT, InspectionState.CAPTURE):
                    logger.warning("start_inspection ignored — already in %s state", current.name)
                    return

                inspection_type = data.get("type", "inspect")

                # capture_id: material IDs to save images for
                # inspection_id: material IDs to run full inspection on
                # material_id: legacy fallback (used as single material for inspect mode)
                raw_capture = data.get("capture_id", "")
                raw_inspect = data.get("inspection_id", "")
                raw_mid = data.get("material_id", "")

                # Handle frontend sending dict: {captureIds: [...], inspectionIds: [...]}
                if isinstance(raw_capture, dict):
                    if not raw_inspect:
                        raw_inspect = raw_capture.get("inspectionIds", raw_capture.get("inspection_ids", []))
                    raw_capture = raw_capture.get("captureIds", raw_capture.get("capture_ids", []))
                if isinstance(raw_inspect, dict):
                    raw_inspect = raw_inspect.get("inspectionIds", raw_inspect.get("inspection_ids", []))

                # Parse capture_id
                if isinstance(raw_capture, list):
                    self.state.capture_material_ids = set(str(m) for m in raw_capture)
                elif raw_capture:
                    self.state.capture_material_ids = {str(raw_capture)}
                elif isinstance(raw_mid, list):
                    # Legacy: material_id as list → treat as capture list
                    self.state.capture_material_ids = set(str(m) for m in raw_mid)
                else:
                    self.state.capture_material_ids = set()

                # Parse inspection_id
                if isinstance(raw_inspect, list):
                    self.state.inspection_material_ids = set(str(m) for m in raw_inspect)
                elif raw_inspect:
                    self.state.inspection_material_ids = {str(raw_inspect)}
                else:
                    self.state.inspection_material_ids = set()  # empty = inspect all

                # Single material_id fallback for inspect mode
                if isinstance(raw_mid, list):
                    self.state.material_id = ""
                else:
                    self.state.material_id = str(raw_mid) if raw_mid else ""
                self.state.machine_id = data.get("machine_id", "")
                self._capture_log.clear()
                self._frame_counter = 0
                self._last_plc_counter = None

                # Trial mode: from frontend flag OR PLC c2c_start=2
                # Trial = run normally but skip PLC result writes
                # Accept both "trial" and "trail" (frontend typo)
                self.state.trial_mode = bool(data.get("trial", False) or data.get("trail", False))
                if self._plc and self._plc.connected:
                    c2c_mode = self._plc.read_c2c_start()
                    if c2c_mode == 2:
                        self.state.trial_mode = True

                if inspection_type == "capture":
                    self.state.set_state(InspectionState.CAPTURE)
                    logger.info(
                        "Starting CAPTURE mode — trial=%s",
                        self.state.trial_mode,
                    )
                    if self.state.capture_material_ids:
                        logger.info("Capture filter: saving capture_ids=%s", self.state.capture_material_ids)
                    if self.state.inspection_material_ids:
                        logger.info("Inspection filter: inspecting inspection_ids=%s", self.state.inspection_material_ids)
                else:
                    self.state.set_state(InspectionState.INSPECT)
                    logger.info(
                        "Starting INSPECT mode — material=%s trial=%s",
                        self.state.material_id, self.state.trial_mode,
                    )



                # Pre-warm YOLO model so first inference doesn't take 6s.
                # Without this, cones pass during model load → counter jump on first cycle.
                if inspection_type != "capture":
                    import numpy as np
                    inspector = self._get_vl_inspector()
                    dummy = np.zeros((640, 640, 3), dtype=np.uint8)
                    inspector.detector.detect(dummy)
                    logger.info("YOLO model warmed up")

                # Flush + restart camera acquisition — clean slate, no stale frames.
                # start_acquisition() auto-reconnects failed cameras.
                if self._capture_seq:
                    self._capture_seq.flush_buffers()
                    self._capture_seq.start_acquisition()
                    # Update camera status after reconnect attempts
                    self._update_camera_status()
                    logger.info("Camera buffers flushed, acquisition restarted")

                # Clear tube-debug and inspection-log folders from previous runs
                # so old images/logs don't mix with the new session.
                for folder_name in ("tube-debug", "inspection-log", "tube"):
                    folder = Path(self._data_root) / folder_name
                    if folder.exists():
                        shutil.rmtree(folder)
                        logger.info("Cleared %s folder on start", folder)

                # Write IPS status to PLC: 1=Active, 2=Trial
                # PLC needs ips_status to know we're running — always write it
                if self._plc and self._plc.connected:
                    ips_val = 2 if self.state.trial_mode else 1
                    self._plc.write_ips_status(ips_val)
                    logger.info("PLC ips_status=%d (%s)", ips_val, "Trial" if self.state.trial_mode else "Active")

                # Re-attach a paused (still-OPEN) teach session so captures
                # continue N/200 across operator stop/start.
                self._resume_teach_session_if_any()

                self._start_worker()
                # UI listens on send_image for start_status
                self.sio.emit("send_image", {
                    "type": "start_status",
                    "status": True,
                    **self._camera_status_dict(),
                }, room=sid)
            except Exception as e:
                logger.exception("start_inspection failed")
                self.sio.emit("send_image", {
                    "type": "error",
                    "data": str(e),
                }, room=sid)

        @self.sio.event
        def stop_inspection(sid, data=None):
            logger.info("Stopping inspection")
            self._pause_inspection_runtime("operator stop")
            # UI listens on send_image for start_status
            self.sio.emit("send_image", {
                "type": "start_status",
                "status": False,
            }, room=sid)
            # PAUSE (not close) any open teach session — the DB row stays
            # capture_status='OPEN' and is re-attached on the next start by
            # _resume_teach_session_if_any(). Only teach toggle OFF or
            # auto-stop at target count finalizes a session.
            # See docs/critical_fixes/teach_session_stop_resume_plan.md.
            with self.state.lock:
                session_id = self.state.capture_session_id
                teach_count = int(self.state.teach_capture_count or 0)
                if session_id:
                    try:
                        from datetime import datetime, timezone as _tz
                        self._db_conn.execute(
                            "UPDATE capture_sessions SET stopped_at=?, stopped_by=? WHERE session_id=? AND stopped_at IS NULL",
                            (datetime.now(_tz.utc).isoformat(), "plc_stop", session_id),
                        )
                        self._db_conn.commit()
                        logger.info(
                            "Teach session %s paused at %d/%d (stays OPEN — resumes on "
                            "next start; toggle teach OFF to end it)",
                            session_id, teach_count, self._installation_min_capture,
                        )
                    except Exception as e:
                        logger.warning("Failed to pause capture session on stop: %s", e)
                self.state.capture_material_ids = set()
                self.state.capture_session_id = ""
                self.state.capture_module = ""
                self.state.teach_capture_count = 0
                self.state.teach_session_start_time = 0.0

            # Sort captured images in background, notify UI when done
            if self._capture_log:
                total = len(self._capture_log)
                self.sio.emit("send_image", {
                    "type": "sorting",
                    "status": "started",
                    "total": total,
                })
                threading.Thread(
                    target=self._sort_and_notify,
                    args=(total,),
                    daemon=True,
                ).start()

        @self.sio.event
        def connect_cam(sid, data):
            try:
                self.state.camera_id = data.get("cam_id", 1)
                self.state.exposure = data.get("exposure", 11000)
                self.state.save_exposure = data.get("save", False)

                if self.state.save_exposure:
                    self.state.set_state(InspectionState.EXPOSURE)
                    logger.info(
                        "Changing exposure: cam=%d exp=%d",
                        self.state.camera_id, self.state.exposure,
                    )
                else:
                    self.state.set_state(InspectionState.LIVE_FEED)
                    logger.info("Starting live feed: cam=%d", self.state.camera_id)

                self._start_worker()
            except Exception as e:
                logger.exception("connect_cam failed")
                self.sio.emit("error", {"message": str(e)}, room=sid)

        @self.sio.event
        def on_light(sid, data):
            try:
                light = data.get("light", "vl")
                light_map = {"uv": 0, "vl": 1, "yarntail": 2, "master": 3}
                self.state.light_id = light_map.get(light, 1)
                self.state.set_state(InspectionState.LIGHT_ON)
                logger.info("Turning ON light: %s (id=%d)", light, self.state.light_id)
                self._start_worker()
            except Exception as e:
                logger.exception("on_light failed")
                self.sio.emit("error", {"message": str(e)}, room=sid)

        @self.sio.event
        def off_light(sid, data):
            try:
                light = data.get("light", "vl")
                light_map = {"uv": 0, "vl": 1, "yarntail": 2, "master": 3}
                self.state.light_id = light_map.get(light, 1)
                self.state.set_state(InspectionState.LIGHT_OFF)
                logger.info("Turning OFF light: %s (id=%d)", light, self.state.light_id)
                self._start_worker()
            except Exception as e:
                logger.exception("off_light failed")
                self.sio.emit("error", {"message": str(e)}, room=sid)

        @self.sio.event
        def light_status(sid, data=None):
            try:
                self.state.set_state(InspectionState.LIGHT_STATUS)
                self._start_worker()
            except Exception as e:
                logger.exception("light_status failed")
                self.sio.emit("error", {"message": str(e)}, room=sid)

        @self.sio.event
        def check_plc(sid, data=None):
            try:
                connected = False
                if self._plc:
                    connected = self._plc.connected
                    if not connected:
                        connected = self._plc.connect()
                        self.state.plc_connected = connected

                plc_cfg = self.config.get("plc", {})
                self.sio.emit("plc_status", {
                    "connected": connected,
                    "host": plc_cfg.get("host", ""),
                    "port": plc_cfg.get("port", 502),
                }, room=sid)
            except Exception as e:
                logger.exception("check_plc failed")
                self.sio.emit("error", {"message": str(e)}, room=sid)

        @self.sio.event
        def get_plc_info(sid, data=None):
            try:
                self.state.set_state(InspectionState.PLC_CONFIG)
                self._start_worker()
            except Exception as e:
                logger.exception("get_plc_info failed")
                self.sio.emit("error", {"message": str(e)}, room=sid)

        @self.sio.event
        def health_check(sid, data=None):
            try:
                status = self._get_system_health()
                self.sio.emit("health_status", status, room=sid)
            except Exception as e:
                logger.exception("health_check failed")
                self.sio.emit("error", {"message": str(e)}, room=sid)

        @self.sio.event
        def check_cameras(sid, data=None):
            try:
                cam_id = data.get("cam_id", "all") if data else "all"
                status = self._check_camera_health(cam_id)
                self.sio.emit("camera_status", status, room=sid)
            except Exception as e:
                logger.exception("check_cameras failed")
                self.sio.emit("error", {"message": str(e)}, room=sid)

        @self.sio.event
        def error_proof(sid, data):
            try:
                action = data.get("type", "check")
                light = data.get("lights", "vl")

                if action == "check":
                    state_map = {
                        "vl": InspectionState.ILLUM_CHECK_VL,
                        "uv": InspectionState.ILLUM_CHECK_UV,
                        "tail": InspectionState.ILLUM_CHECK_TAIL,
                    }
                else:
                    state_map = {
                        "vl": InspectionState.ILLUM_SAVE_VL,
                        "uv": InspectionState.ILLUM_SAVE_UV,
                        "tail": InspectionState.ILLUM_SAVE_TAIL,
                    }

                new_state = state_map.get(light, InspectionState.ILLUM_CHECK_VL)
                self.state.set_state(new_state)
                logger.info("Error proof: action=%s light=%s", action, light)
                self._start_worker()
            except Exception as e:
                logger.exception("error_proof failed")
                self.sio.emit("error", {"message": str(e)}, room=sid)

        @self.sio.event
        def error_proof_defect(sid, data):
            try:
                self.state.error_proof_data = {
                    "type": data.get("type", ""),
                    "mat_id": data.get("mat_id", ""),
                    "master_id": data.get("master_id", ""),
                }
                self.state.set_state(InspectionState.ERROR_PROOF)
                logger.info("Error proof defect: %s", self.state.error_proof_data)
                self._start_worker()
            except Exception as e:
                logger.exception("error_proof_defect failed")
                self.sio.emit("error", {"message": str(e)}, room=sid)


        @self.sio.event
        def set_capture_mode(sid, data=None):
            """Activate capture mode for selected material IDs and module.

            Data: {session_id: str, module: str, material_ids: [str],
                   operator_id: str (optional)}

            When operator_id is present, this is a manifest-driven teach
            capture session — writes manifest.json and starts the timer.
            """
            try:
                data = data or {}
                session_id = data.get('session_id', '')
                module = data.get('module', '')
                material_ids = set(str(m) for m in data.get('material_ids', []))
                operator_id = data.get('operator_id', '')
                # Resume of an existing OPEN session (PUT /config/teach on a
                # paused session): keep counting from images_saved instead of
                # restarting at 0, and don't rewrite the manifest.
                resume = bool(data.get('resume', False))
                images_saved = int(data.get('images_saved', 0) or 0)
                with self.state.lock:
                    self.state.capture_material_ids = material_ids
                    self.state.capture_session_id = session_id
                    self.state.capture_module = module
                    # Initialize teach session timer and counter for manifest sessions
                    if operator_id and session_id:
                        self.state.teach_session_start_time = time.monotonic()
                        self.state.teach_capture_count = images_saved if resume else 0
                logger.info(
                    'Capture mode %s: session=%s module=%s material_ids=%s operator=%s%s',
                    'resumed' if resume else 'activated',
                    session_id, module, material_ids, operator_id,
                    f' at {images_saved}/{self._installation_min_capture}' if resume else '',
                )

                # # If the system is IDLE when set_capture_mode fires (i.e. the
                # # operator triggered a standalone teach session without a prior
                # # start_inspection), transition to CAPTURE state and start the
                # # worker so incoming cones are actually processed.
                # _cur = self.state.get_state()
                # if _cur == InspectionState.IDLE:
                #     self.state.set_state(InspectionState.CAPTURE)
                #     self._start_worker()
                #     logger.info(
                #         'set_capture_mode: system was IDLE — transitioned to CAPTURE and started worker'
                #     )

                # Write manifest.json for teach sessions (operator-initiated).
                # Skipped on resume — the manifest already exists from the
                # original toggle ON and must not be reset.
                if operator_id and session_id and module and not resume:
                    try:
                        self._write_manifest(session_id, module, operator_id)
                    except Exception as e:
                        logger.exception('Failed to write manifest.json for session %s: %s',
                                         session_id, e)

                self.sio.emit('capture_status', {
                    'active': True,
                    'session_id': session_id,
                    'module': module,
                    'material_ids': list(material_ids),
                }, room=sid)
            except Exception as e:
                logger.exception('set_capture_mode failed')
                self.sio.emit('error', {'message': str(e)}, room=sid)

        @self.sio.event
        def clear_capture_mode(sid, data=None):
            """Deactivate capture mode and finalize any open teach session."""
            try:
                current_state = self.state.get_state()
                with self.state.lock:
                    session_id = self.state.capture_session_id
                    module = self.state.capture_module
                    count = int(self.state.teach_capture_count or 0)

                if session_id and module:
                    self._finalize_teach_session(
                        session_id=session_id,
                        module=module,
                        count=count,
                        stopped_by="operator",
                        pause_runtime=(current_state != InspectionState.INSPECT),
                    )
                    logger.info(
                        'Capture mode finalized: session=%s module=%s count=%d stopped_by=operator',
                        session_id, module, count,
                    )
                else:
                    with self.state.lock:
                        self.state.capture_material_ids = set()
                        self.state.capture_session_id = ''
                        self.state.capture_module = ''
                        self.state.teach_capture_count = 0
                        self.state.teach_session_start_time = 0.0
                    if current_state != InspectionState.INSPECT:
                        self._pause_inspection_runtime("capture mode cleared")
                    else:
                        logger.info('Capture mode cleared while inspection is running — runtime kept active')

                self.sio.emit('capture_status', {
                    'active': False,
                    'session_id': '',
                    'module': '',
                    'material_ids': [],
                }, room=sid)
            except Exception as e:
                logger.exception('clear_capture_mode failed')
                self.sio.emit('error', {'message': str(e)}, room=sid)

        @self.sio.event
        def get_capture_status(sid, data=None):
            """Return current capture mode state."""
            try:
                with self.state.lock:
                    active = bool(self.state.capture_session_id)
                    status = {
                        'active': active,
                        'session_id': self.state.capture_session_id,
                        'module': self.state.capture_module,
                        'material_ids': list(self.state.capture_material_ids),
                    }
                self.sio.emit('capture_status', status, room=sid)
            except Exception as e:
                logger.exception('get_capture_status failed')
                self.sio.emit('error', {'message': str(e)}, room=sid)




        @self.sio.event
        def get_teaching_status(sid, data=None):
            """Return live auto-teach progress snapshot.

            teaching_alert events are fire-and-forget broadcasts, so a UI that
            connects late (page reload, reconnect) has no history. This is the
            authoritative snapshot it reconciles against on load.
            """
            try:
                with self.state.lock:
                    counts = dict(self.state.capture_counts)
                    training = sorted(self.state.auto_teaching_triggered)
                    capture_module = self.state.capture_module
                    capture_session_id = self.state.capture_session_id
                    capture_material_ids = sorted(self.state.capture_material_ids)
                    teach_capture_count = int(self.state.teach_capture_count or 0)

                total = int(self._tube_min_capture)
                materials = []
                for mid in sorted(set(counts) | set(training)):
                    count = int(counts.get(mid, 0))
                    materials.append({
                        "material_id": mid,
                        "count": count,
                        "total": total,
                        "stage": "training" if mid in training else "capturing",
                        "percent": round(100.0 * count / total, 1) if total else 0.0,
                    })

                self.sio.emit("teaching_status", {
                    "tube_min_capture": total,
                    "installation_min_capture": int(self._installation_min_capture),
                    "materials": materials,
                    "training_material_ids": training,
                    "capture_active": bool(capture_session_id),
                    "capture_module": capture_module,
                    "capture_session_id": capture_session_id,
                    "capture_material_ids": capture_material_ids,
                    "teach_capture_count": teach_capture_count,
                }, room=sid)
            except Exception as e:
                logger.exception("get_teaching_status failed")
                self.sio.emit("error", {"message": str(e)}, room=sid)

        @self.sio.event
        def get_state(sid, data=None):
            """Return current InspectionState — used by API to gate config writes."""
            try:
                with self.state.lock:
                    current = int(self.state.state)
                self.sio.emit("state", {"state": current, "idle": current == 0}, room=sid)
            except Exception as e:
                logger.exception("get_state failed")
                self.sio.emit("error", {"message": str(e)}, room=sid)

    # ── Worker loop ──────────────────────────────────────────────────

    def _start_worker(self):
        """Start the worker thread if not already running."""
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._stop_worker.clear()
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()
            logger.info("Worker thread started")

    def _start_plc_keepalive(self, interval_s: float = 15.0):
        """Background PLC session keepalive while inspection is idle.

        Probes (and repairs) the Modbus session every interval so the
        PLC-side idle timeout never leaves a silently-dead session for the
        operator's next Stop/Start write. Skipped while INSPECT/CAPTURE is
        running — cycle traffic keeps the session alive there, and the
        probe must not contend with the handshake.
        """
        def _keepalive_loop():
            while True:
                time.sleep(interval_s)
                try:
                    if self._plc is None:
                        continue
                    if self.state.get_state() in (InspectionState.INSPECT, InspectionState.CAPTURE):
                        continue
                    self._plc.keepalive()
                except Exception:
                    logger.debug("PLC keepalive iteration failed", exc_info=True)

        t = threading.Thread(target=_keepalive_loop, daemon=True, name="plc-keepalive")
        t.start()
        logger.info("PLC idle keepalive started (every %.0fs)", interval_s)

    def _auto_start_inspection(self, trial: bool = False):
        """Start INSPECT mode without operator action (service.auto_start_inspection).

        Mirrors the start_inspection Socket.IO handler minus UI parsing:
        set state, restart camera acquisition, write ips_status, start the
        worker. cycle_start is written by the worker's first cycle (after
        the reconnect-first fix, so a dead PLC session cannot deadlock it).
        """
        try:
            if self.state.get_state() in (InspectionState.INSPECT, InspectionState.CAPTURE):
                return
            self.state.trial_mode = trial
            self.state.set_state(InspectionState.INSPECT)
            logger.info("AUTO-START: entering INSPECT mode after boot (trial=%s)", trial)

            if self._capture_seq:
                self._capture_seq.flush_buffers()
                self._capture_seq.start_acquisition()
                self._update_camera_status()

            if self._plc and self._plc.connected:
                ips_val = 2 if trial else 1
                self._plc.write_ips_status(ips_val)
                logger.info("AUTO-START: PLC ips_status=%d", ips_val)

            self._resume_teach_session_if_any()
            self._start_worker()
        except Exception:
            logger.exception("AUTO-START failed — waiting for operator Start instead")

    def _worker_loop(self):
        """Main worker loop — dispatches to handler based on current state."""
        logger.info("Worker loop started")

        while not self._stop_worker.is_set():
            try:
                current_state = self.state.get_state()

                if current_state == InspectionState.IDLE:
                    time.sleep(0.1)
                    continue

                logger.debug("Worker dispatch: state=%s", current_state.name)

                if current_state == InspectionState.INSPECT:
                    self._run_inspection_cycle()

                elif current_state == InspectionState.CAPTURE:
                    self._run_capture_cycle()

                elif current_state == InspectionState.LIVE_FEED:
                    self._run_live_feed()

                elif current_state == InspectionState.EXPOSURE:
                    self._change_exposure()
                    self.state.set_state(InspectionState.IDLE)

                elif current_state in (InspectionState.LIGHT_ON, InspectionState.LIGHT_OFF):
                    self._control_light(current_state == InspectionState.LIGHT_ON)
                    self.state.set_state(InspectionState.LIGHT_STATUS)

                elif current_state == InspectionState.LIGHT_STATUS:
                    self._read_light_status()
                    self.state.set_state(InspectionState.IDLE)

                elif current_state == InspectionState.PLC_CONFIG:
                    self._read_plc_config()

                elif current_state in (
                    InspectionState.ILLUM_CHECK_VL,
                    InspectionState.ILLUM_CHECK_UV,
                    InspectionState.ILLUM_CHECK_TAIL,
                ):
                    self._check_illumination(current_state)
                    self.state.set_state(InspectionState.IDLE)

                elif current_state in (
                    InspectionState.ILLUM_SAVE_VL,
                    InspectionState.ILLUM_SAVE_UV,
                    InspectionState.ILLUM_SAVE_TAIL,
                ):
                    self._save_illumination(current_state)
                    self.state.set_state(InspectionState.IDLE)

                elif current_state == InspectionState.ERROR_PROOF:
                    self._run_error_proof()
                    self.state.set_state(InspectionState.IDLE)

                else:
                    time.sleep(0.1)
            except Exception:
                logger.exception(
                    "Worker loop: unhandled exception — loop continues to prevent PLC deadlock"
                )
                time.sleep(1.0)  # Brief pause before retrying

        logger.info("Worker loop stopped")

    # ── Health checks ────────────────────────────────────────────────

    def _get_system_health(self) -> dict:
        plc_status = self._check_plc_health()
        camera_statuses = self._check_camera_health("all")

        all_cameras_ok = all(c["connected"] for c in camera_statuses["cameras"])

        if plc_status["connected"] and all_cameras_ok:
            overall = "healthy"
            message = "All systems operational"
        elif plc_status["connected"] or all_cameras_ok:
            overall = "degraded"
            issues = []
            if not plc_status["connected"]:
                issues.append("PLC disconnected")
            disconnected = [c["name"] for c in camera_statuses["cameras"] if not c["connected"]]
            if disconnected:
                issues.append(f"Cameras offline: {', '.join(disconnected)}")
            message = "; ".join(issues)
        else:
            overall = "unhealthy"
            message = "PLC and cameras disconnected"

        return {
            "status": overall,
            "plc": plc_status,
            "cameras": camera_statuses["cameras"],
            "models_loaded": self.state.models_loaded,
            "current_state": self.state.get_state().name,
            "message": message,
        }

    def _try_plc_reconnect(self) -> bool:
        """Attempt PLC reconnect with exponential backoff.

        Only attempts a reconnect if enough time has passed since the last
        failure. On success: resets backoff interval to 2s. On failure:
        doubles the interval (capped at 30s) to avoid Modbus TCP spam.

        Returns:
            True if PLC is now connected (either was already, or reconnect succeeded).
        """
        if self._plc is None:
            return False
        if self._plc.connected:
            return True

        now = time.monotonic()
        if now < self._plc_reconnect_next_at:
            logger.debug(
                "PLC reconnect backoff — next attempt in %.1fs",
                self._plc_reconnect_next_at - now,
            )
            return False

        logger.info("PLC not connected — attempting reconnect (backoff=%.1fs)", self._plc_reconnect_interval)
        if self._plc.connect():
            logger.info("PLC reconnected")
            self.state.plc_connected = True
            self._plc_reconnect_interval = 2.0   # reset backoff on success
            self._plc_reconnect_next_at = 0.0
            return True
        else:
            self._plc_reconnect_interval = min(self._plc_reconnect_interval * 2, 30.0)
            self._plc_reconnect_next_at = time.monotonic() + self._plc_reconnect_interval
            logger.warning(
                "PLC reconnect failed — next attempt in %.1fs", self._plc_reconnect_interval
            )
            return False

    def _hold_ready_while_c2c_disabled(self) -> None:
        """Keep cycle_start asserted while the PLC reports c2c_start=0.

        When the PLC disables the line it still emits one final trigger.
        Vision consumes it (clear_trigger) and skips the cone, so the cycle
        never completes and never loops back to write cycle_start again --
        cycle_start_ok is still True, so the self-heal retry never fires.
        The PLC has already cleared cycle_start to 0, so both sides then wait
        on each other forever (observed 2026-08-13 13:39 -> 14:25; PLC
        counter 1694 -> 1695, i.e. zero triggers in 46 minutes).

        Re-asserting cycle_start=1 restores the ready flag so the PLC resumes
        on its own the moment c2c_start returns to 1. cycle_start is a level
        flag the PLC clears itself, so re-writing 1 is idempotent -- no edge,
        no double-release.
        """
        now = time.monotonic()
        interval = self.config.get("plc", {}).get("c2c_reassert_interval", 5.0)

        if self._c2c_disabled_since is None:
            self._c2c_disabled_since = now
            self._c2c_last_reassert = 0.0
            logger.warning(
                "PLC c2c_start=0 -- line disabled from PLC side. "
                "Holding cycle_start=1; will resume automatically."
            )

        if (now - self._c2c_last_reassert) < interval:
            return
        self._c2c_last_reassert = now

        if not (self._plc and self._plc.connected):
            self._try_plc_reconnect()
            return

        if self._plc.write_cycle_start():
            logger.info(
                "c2c_start=0 for %.0fs -- cycle_start=1 re-asserted, waiting on PLC",
                now - self._c2c_disabled_since,
            )
        else:
            logger.warning("c2c_start=0 -- cycle_start re-assert FAILED, reconnecting")
            self._try_plc_reconnect()

    def _clear_c2c_disabled(self) -> None:
        """Reset stall tracking once the PLC re-enables the line."""
        if self._c2c_disabled_since is not None:
            logger.info(
                "PLC c2c_start re-enabled after %.0fs -- resuming",
                time.monotonic() - self._c2c_disabled_since,
            )
        self._c2c_disabled_since = None

    def _check_plc_health(self) -> dict:
        plc_cfg = self.config.get("plc", {})
        host = plc_cfg.get("host", "192.168.2.1")
        port = plc_cfg.get("port", 502)

        connected = False
        if self._plc:
            connected = self._plc.connected
        self.state.plc_connected = connected

        return {
            "connected": connected,
            "host": host,
            "port": port,
            "error": None if connected else "Not connected",
        }

    def _check_camera_health(self, cam_id: str = "all") -> dict:
        cameras_config = self.config.get("cameras", {})
        results = []

        for cam_name, cam_cfg in cameras_config.items():
            if cam_id != "all" and cam_name.lower() != cam_id.lower():
                continue

            cam = self._cameras.get(cam_name.upper())
            connected = cam.connected if cam else False

            # Update state
            if cam_name.upper() == "VL":
                self.state.cam_vl_connected = connected
            elif cam_name.upper() == "UV":
                self.state.cam_uv_connected = connected
            elif cam_name.upper() == "TAIL":
                self.state.cam_tail_connected = connected

            # Build camera info with observability stats
            cam_info = {
                "name": cam_name.lower(),
                "connected": connected,
                "ip": cam_cfg.get("ip", ""),
                "serial": cam_cfg.get("serial", ""),
                "exposure": cam_cfg.get("exposure", 0),
                "error": None if connected else "Camera not connected",
            }

            # Add full observability if camera is connected
            if cam and connected:
                stats = cam.get_stream_statistics()
                cam_info["stats"] = stats
                # Derive health level: ok / warning / error
                cam_info["health"] = self._derive_camera_health_level(stats)
            else:
                cam_info["stats"] = None
                cam_info["health"] = "error" if not connected else "ok"

            results.append(cam_info)

        return {
            "cameras": results,
            "all_connected": all(c["connected"] for c in results) if results else False,
        }

    @staticmethod
    def _derive_camera_health_level(stats: dict) -> str:
        """Derive a simple health level from camera observability stats.

        Returns:
            'ok' — no issues
            'warning' — minor issues (debounced triggers, resend requests, temp > 60°C)
            'error' — frame loss, transport drops, or overheating (> 75°C)
        """
        # Error conditions — frame loss
        if stats.get("missed", 0) > 0:
            return "error"
        if stats.get("failed", 0) > 0:
            return "error"
        if stats.get("block_id_gaps", 0) > 0:
            return "error"
        if stats.get("buffer_underruns", 0) > 0:
            return "error"
        temp = stats.get("temperature_c", -1.0)
        if temp > 75.0:
            return "error"

        # Warning conditions — non-critical
        fc = stats.get("frame_count", -1)
        d = stats.get("delivered", 0)
        sk = stats.get("skipped", 0)
        if fc >= 0 and d >= 0 and fc > (d + sk):
            return "warning"
        if stats.get("debounced", 0) > 0:
            return "warning"
        if stats.get("resend_requests", 0) > 0:
            return "warning"
        if temp > 60.0:
            return "warning"

        return "ok"



    # ── Analytics socket events ──────────────────────────────────────

    def _register_analytics_handlers(self) -> None:
        """Register socket.io handlers for analytics queries."""

        @self.sio.event
        def get_analytics(sid, data):
            """Client requests current analytics snapshot."""
            try:
                snap = self._analytics.snapshot()
                self.sio.emit("analytics_snapshot", {"ok": True, "data": snap}, room=sid)
            except Exception as e:
                self.sio.emit("analytics_snapshot", {"ok": False, "message": str(e)}, room=sid)

        @self.sio.event
        def reset_analytics(sid, data):
            """Reset shift counters — call at start of a new shift."""
            try:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).isoformat()
                self._analytics.shift_hours = float(self.config.get("shift_hours", 8.0))
                with self._analytics.lock:
                    self._analytics._reset_shift(now)
                self.sio.emit("analytics_reset", {"ok": True, "shift_start": now}, room=sid)
                logger.info("Analytics shift counters reset by client sid=%s", sid)
            except Exception as e:
                self.sio.emit("analytics_reset", {"ok": False, "message": str(e)}, room=sid)

    # ── Result streaming ─────────────────────────────────────────────

    def _stream_results(self):
        """Background task to stream results from queue to clients."""
        logger.info("Stream worker started — emitting results to %d client(s)", len(self.clients))
        while True:
            try:
                result = self.result_queue.get(timeout=0.02)
                rtype = result.get("type", "?")
                logger.info("Emitting send_image type=%s to %d client(s)", rtype, len(self.clients))
                self.sio.emit("send_image", result)
            except queue.Empty:
                pass
            except Exception:
                logger.exception("Error streaming results")
            eventlet.sleep(0)

    # ── Server lifecycle ─────────────────────────────────────────────

    def run(self):
        """Start the Socket.IO server with camera and PLC initialization."""
        logger.info("Starting InspectionService on http://%s:%d", self.host, self.port)

        # Register analytics socket handlers
        self._register_analytics_handlers()

        # Initialize hardware
        self._init_cameras()
        self._init_plc()

        # Preload all enabled inspection models at boot (default on).
        # Lazy loading put the ~11s VL load at Start-click time and the
        # UV/Tail loads inside cone #1's cycle — cones passed uninspected
        # while models loaded (observed 2026-07-17 18:00–18:02). Boot is
        # the right place to pay this cost: nobody is waiting, and Start
        # becomes instant.
        service_cfg = self.config.get("service", {})
        if service_cfg.get("preload_models", True):
            self._preload_inspectors()

        # Idle PLC keepalive: while inspection is stopped there is no Modbus
        # traffic, the PLC-side idle timeout kills the session silently, and
        # the operator's next Stop/Start write fails ('write register 15
        # failed'). Probe/repair the session every 15s during idle; during
        # inspection the cycle traffic itself is the keepalive.
        self._start_plc_keepalive()

        # Opt-in: resume inspection automatically after boot so a service
        # restart doesn't leave the line passing cones uninspected until an
        # operator presses Start (observed 17:57→18:00). Site decision —
        # default off.
        if service_cfg.get("auto_start_inspection", False):
            self._auto_start_inspection(
                trial=bool(service_cfg.get("auto_start_trial", False))
            )

        import eventlet.wsgi
        eventlet.wsgi.server(
            eventlet.listen((self.host, self.port)),
            self.app,
            log_output=False,
        )

    def stop(self, timeout: float = 8.0):
        """Stop the service and release all resources.

        Cleanup runs directly on the main thread, guarded by a SIGALRM —
        not a Python thread. Eventlet's monkey_patch() turns threading.Thread
        into a cooperative greenthread, which can't preempt a genuinely
        blocked native call (camera SDK / Modbus); SIGALRM is delivered by
        the kernel and isn't subject to that limitation.
        """
        import signal

        logger.info("Stopping InspectionService")
        self._stop_worker.set()
        if self._worker_thread:
            self._worker_thread.join(timeout=5.0)

        def _on_timeout(signum, frame):
            logger.error(
                "stop() cleanup did not finish within %.1fs — likely a wedged "
                "camera/PLC call. Forcing exit; camera lock will clear via "
                "GevHeartbeatTimeout instead.", timeout,
            )
            os._exit(1)

        old_handler = signal.signal(signal.SIGALRM, _on_timeout)
        signal.alarm(int(timeout))
        try:
            if self._vl_inspector:
                self._vl_inspector.close()
        except Exception as e:
            logger.warning("VL inspector close error: %s", e)
        try:
            if self._plc:
                self._plc.disconnect()
        except Exception as e:
            logger.warning("PLC disconnect error: %s", e)
        try:
            self._cleanup_cameras()
        except Exception as e:
            logger.warning("Camera cleanup error: %s", e)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

        logger.info("InspectionService stopped")


def main():
    """Run the inspection service."""
    import argparse

    parser = argparse.ArgumentParser(description="Run Inspection Service")
    parser.add_argument("--config", default="src/config.json", help="Path to config file")
    parser.add_argument("--host", default=None, help="Host to bind")
    parser.add_argument("--port", type=int, default=None, help="Port to bind")
    args = parser.parse_args()

    config_path = Path(args.config)
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
    else:
        config = {}
        logger.warning("Config not found: %s", config_path)

    if "service" not in config:
        config["service"] = {}
    if args.host:
        config["service"]["host"] = args.host
    if args.port:
        config["service"]["port"] = args.port

    service = InspectionService(config)

    # systemd sends SIGTERM on stop/restart. Without a clean camera Close()
    # the GigE control-channel lock survives the process by up to
    # GevHeartbeatTimeout (10 s wedge fix), making quick restarts fail with
    # 0xE1018006. Clean shutdown releases the lock immediately.
    import signal

    def _handle_sigterm(signum, frame):
        logger.info("SIGTERM received — shutting down gracefully")
        try:
            service.stop()
        finally:
            raise SystemExit(0)

    # signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        service.run()
    except KeyboardInterrupt:
        service.stop()


if __name__ == "__main__":
    main()
