"""
Capture sequence module — orchestrates sequential image capture from 3 cameras.

Each camera is triggered by its own proximity sensor (hardware trigger on Line1).
Captures are sequential: UV → VL → Tail, each blocking until its sensor fires.

IMPORTANT: Every camera operation is wrapped in try/except so that one camera
crashing (disconnect, SDK error) never affects the other cameras.

════════════════════════════════════════════════════════════════════════
CHANGE — Flush moved OUT of capture_part(), to the end / caller side
════════════════════════════════════════════════════════════════════════
capture_part() used to flush all buffers as its FIRST step, i.e. AFTER
the PLC trigger for this cone may already have been read. The sensors on
this rig are close to the release point, so if that read happened late,
the cone could already have tripped a sensor — its real frame would be
sitting in the buffer, and the up-front flush would discard it, forcing
a timeout (camera shown EMPTY).

FIX (this file):
    1. REMOVED the flush-all loop from the top of capture_part().
    2. Flushing now happens ONLY via flush_buffers(), which the caller
       (inspection_service.py) already calls BEFORE writing
       cycle_start=1 — at that point the next cone has NOT been
       released yet, so anything in a buffer is guaranteed stale. Any
       frame arriving after that belongs to the new cone and is
       returned instantly by capture_latest() instead of being
       discarded.
    3. KEPT the Tail post-capture flush at the very end of the sequence
       (sensor-bounce insurance) — safe because Tail already delivered
       its frame and the next cone has not been released yet.

PER-CYCLE SEQUENCE (service + this module):
    flush_buffers()  ── before cycle_start=1 (service, safe point)
    cycle_start=1    ── PLC may release cone any time after this
    wait PLC trigger ── material data ready
    capture_part()   ── UV → VL → Tail, NO flush at the top
                        └─ after Tail: flush Tail once (bounce), at the last step
════════════════════════════════════════════════════════════════════════
"""

import logging
import time

from .camera import Camera
from .data_types import CapturedImages


logger = logging.getLogger(__name__)


class CaptureSequence:
    """Captures one complete part across 3 cameras sequentially.

    Each call to capture_part() blocks on 3 hardware triggers in
    physical conveyor order:
        Sensor 1 → UV image   (Ultraviolet)
        Sensor 2 → VL image   (Visible Light)
        Sensor 3 → Tail image (Yarn Tail)

    The timing between captures is determined by the conveyor speed and
    station spacing, not by software.

    All camera operations are isolated — if one camera crashes or
    disconnects, the others continue working independently.
    """

    def __init__(self, cam_vl: "Camera | None", cam_uv: "Camera | None",
                 cam_tail: "Camera | None", device_manager=None):
        """Any slot may be None — a camera disabled via config
        (cameras.<name>.enabled = false). Disabled slots are skipped in
        every operation: no trigger wait, no timeout, no flush/stats."""
        self.cam_vl = cam_vl
        self.cam_uv = cam_uv
        self.cam_tail = cam_tail
        self._device_manager = device_manager  # needed for reconnect

    def _active_cams(self) -> list:
        """(label, camera) for every enabled slot, in capture order."""
        return [
            (label, cam)
            for label, cam in (("UV", self.cam_uv), ("VL", self.cam_vl), ("Tail", self.cam_tail))
            if cam is not None
        ]

    def capture_part(self) -> CapturedImages:
        """Capture one complete part — UV, VL, and Tail images.

        Flush strategy — capture_part() does NOT flush before capturing.
        The caller (inspection service) MUST call flush_buffers() BEFORE
        writing cycle_start=1. That is the only safe flush point: the
        cone has not been released yet, so anything buffered is stale.
        Once cycle_start=1 is written the cone can trip any sensor at
        any time — a frame found in a buffer here is this cone's REAL
        frame (possibly arrived before the PLC trigger was even read)
        and capture_latest() returns it immediately. Flushing here would
        discard exactly those early frames and force a spurious timeout.
        So:

            1. (Service, before cycle_start=1) flush_buffers() — clears
               previous cycle bounce / idle-time noise.
            2. Capture UV → VL → Tail with NO flushes before or between.
               A frame already waiting in a downstream buffer is this
               cone's legitimate frame and is returned immediately.
            3. After the final (Tail) capture, flush Tail once more (the
               LAST step of the sequence) to clear any sensor-bounce
               double-trigger so it doesn't sit stale into the next
               cycle. (OutputQueueSize=1 already auto-drops older
               frames, so this is cheap insurance for the case where
               that node write failed at connect time.)

        With cycle_start gating, PLC only releases one cone at a time, so
        any frame that arrives after the pre-cycle_start flush belongs to
        this cone.

        Each camera uses its own configured timeout (from config.json).
        Errors are handled per camera — if one camera crashes, the others
        still capture. The inspection pipeline handles None frames gracefully.

        Returns:
            CapturedImages with BGR numpy arrays (or None per camera
            if that camera timed out or errored).
        """
        t_start = time.perf_counter()
        vl = None
        uv = None
        tail = None
        active = self._active_cams()

        # NO flush here — flushing happens before cycle_start=1 is written
        # (see inspection_service.py). By the time capture_part() runs, the
        # cone may have ALREADY tripped a sensor; a buffered frame is its
        # real frame and must be consumed, not discarded.

        # Camera 1: UV (first station on conveyor)
        if self.cam_uv is None:
            logger.debug("UV camera disabled — skipping capture")
        else:
            try:
                logger.debug("Capture: waiting for UV trigger (timeout=%dms)...", self.cam_uv.timeout)
                uv = self.cam_uv.capture_latest()
                t_uv = time.perf_counter()
                logger.info(f"UV captured in {t_uv - t_start:.3f}s")
                logger.debug("  UV frame: %dx%d dtype=%s", uv.shape[1], uv.shape[0], uv.dtype)
            except TimeoutError:
                logger.warning("UV camera timeout (%dms) — cone may have missed sensor", self.cam_uv.timeout)
            except Exception as e:
                logger.error("UV camera error: %s — camera may be disconnected", e)

        # Camera 2: Visible Light (second station on conveyor)
        # NO flush here — VL's sensor may already have fired during UV
        # capture (sensors are physically close). Any buffered frame is
        # this cone's legitimate VL image; capture_latest() returns it
        # immediately.
        if self.cam_vl is None:
            logger.debug("VL camera disabled — skipping capture")
        else:
            try:
                t_vl_start = time.perf_counter()
                logger.debug("Capture: waiting for VL trigger (timeout=%dms)...", self.cam_vl.timeout)
                vl = self.cam_vl.capture_latest()
                t_vl = time.perf_counter()
                logger.info(f"VL captured in {t_vl - t_vl_start:.3f}s")
                logger.debug("  VL frame: %dx%d dtype=%s", vl.shape[1], vl.shape[0], vl.dtype)
            except TimeoutError:
                logger.warning("VL camera timeout (%dms) — cone may have missed sensor", self.cam_vl.timeout)
            except Exception as e:
                logger.error("VL camera error: %s — camera may be disconnected", e)

        # Camera 3: Yarn Tail (third station on conveyor)
        # NO flush before capture — same rationale as VL.
        if self.cam_tail is None:
            logger.debug("Tail camera disabled — skipping capture")
        else:
            try:
                t_tail_start = time.perf_counter()
                logger.debug("Capture: waiting for Tail trigger (timeout=%dms)...", self.cam_tail.timeout)
                tail = self.cam_tail.capture_latest()
                t_tail = time.perf_counter()
                logger.info(f"Tail captured in {t_tail - t_tail_start:.3f}s")
                logger.debug("  Tail frame: %dx%d dtype=%s", tail.shape[1], tail.shape[0], tail.dtype)
                # Flush any extra frames (sensor bounce, double trigger) so they
                # don't sit stale in the buffer for the next cycle. This is the
                # LAST step of capture_part() — the only flush in this method.
                self.cam_tail.flush_buffers()
            except TimeoutError:
                logger.warning("Tail camera timeout (%dms) — cone may have missed sensor", self.cam_tail.timeout)
            except Exception as e:
                logger.error("Tail camera error: %s — camera may be disconnected", e)

        t_end = time.perf_counter()
        captured = sum(1 for f in (vl, uv, tail) if f is not None)
        logger.info(f"Total capture: {t_end - t_start:.3f}s ({captured}/{len(active)} cameras)")

        return CapturedImages(vl=vl, uv=uv, tail=tail)

    def stop_acquisition(self):
        """Stop acquisition on all 3 cameras and flush buffers.

        Call on inspection stop. After this, proximity sensor triggers
        are ignored and no frames accumulate in the buffer.
        Per-camera errors are logged but don't stop the other cameras.
        """
        for cam in (self.cam_vl, self.cam_uv, self.cam_tail):
            if cam is None:
                continue
            try:
                cam.stop_acquisition()
            except Exception as e:
                logger.warning(f"Camera '{cam.name}': stop_acquisition failed: {e}")

    def start_acquisition(self):
        """Restart acquisition on all 3 cameras.

        Call on inspection start. Buffer is empty — first capture()
        will block until a real hardware trigger fires.

        If a camera fails to start, attempts reconnect once. If reconnect
        also fails, that camera is skipped — capture_part() will return
        None for it (handled gracefully by the inspection pipeline).
        """
        for cam in (self.cam_vl, self.cam_uv, self.cam_tail):
            if cam is None:
                continue
            try:
                cam.start_acquisition()
            except Exception as e:
                logger.error(
                    f"Camera '{cam.name}': start_acquisition failed: {e} — "
                    f"attempting reconnect"
                )
                if self._device_manager is not None:
                    try:
                        cam.reconnect(self._device_manager)
                    except Exception as re:
                        logger.error(
                            f"Camera '{cam.name}': reconnect failed: {re} — "
                            f"camera unavailable this session"
                        )
                else:
                    logger.warning(
                        f"Camera '{cam.name}': no device manager for reconnect — "
                        f"camera unavailable this session"
                    )

    def flush_buffers(self):
        """Flush stale frames from all camera buffers (non-blocking).

        MUST be called BEFORE writing cycle_start=1 to the PLC — i.e.
        before the next cone can be released. At that point anything in
        a buffer is guaranteed stale (previous cycle sensor bounce,
        idle-time noise). Calling this after cycle_start=1 (or inside
        the capture path) risks discarding the current cone's real
        frame — see module header.
        """
        for cam in (self.cam_vl, self.cam_uv, self.cam_tail):
            if cam is None:
                continue
            try:
                cam.flush_buffers()
            except Exception as e:
                logger.warning(f"Camera '{cam.name}': flush_buffers failed: {e}")

    def flush_all(self, timeout_ms: int = 2000):
        """Consume and discard triggers from all 3 cameras.

        Used when a part is rejected (mat_no=0 or master not found) but
        is already on the conveyor — sensors WILL fire regardless, and
        we must consume those triggers to stay in sync.

        Args:
            timeout_ms: Max wait time per camera.
        """
        for cam in (self.cam_vl, self.cam_uv, self.cam_tail):
            if cam is None:
                continue
            try:
                cam.capture(timeout_ms)
            except TimeoutError:
                logger.warning(
                    f"Flush timeout on camera '{cam.name}' — "
                    f"conveyor may have stopped"
                )
            except Exception as e:
                logger.warning(
                    f"Camera '{cam.name}': flush_all failed: {e}"
                )

    def log_stream_statistics(self):
        """Log stream statistics for all 3 cameras. Call periodically or on stop."""
        for cam in (self.cam_vl, self.cam_uv, self.cam_tail):
            if cam is None:
                continue
            try:
                cam.log_stream_statistics()
            except Exception as e:
                logger.warning(f"Camera '{cam.name}': stream stats failed: {e}")

    def get_stream_statistics(self) -> dict:
        """Get stream statistics for all 3 cameras.

        Returns:
            Dict mapping camera name to stats dict.
        """
        result = {}
        for cam in (self.cam_vl, self.cam_uv, self.cam_tail):
            if cam is None:
                continue
            try:
                result[cam.name] = cam.get_stream_statistics()
            except Exception:
                result[cam.name] = {"camera": cam.name, "error": "unavailable"}
        return result

    def health_check(self) -> dict:
        """Check connection status of all 3 cameras.

        Returns:
            Dict mapping camera name to health status (bool).
        """
        result = {}
        for cam in (self.cam_vl, self.cam_uv, self.cam_tail):
            if cam is None:
                continue
            try:
                result[cam.name] = cam.health_check()
            except Exception:
                result[cam.name] = False
        return result
