"""
Camera module — wraps Basler pypylon SDK for GigE cameras.

One Camera instance per physical camera. Instances are created once at app
startup and reused across start/stop cycles.

Supported cameras:
    - acA1920-40gc   (ace classic, UV + Tail stations)
    - a2A2600-20gcPRO (ace 2 PRO, VL station)

Both are GigE Vision cameras identified by static IP address on the
factory floor network. Hardware trigger on Line1 (proximity sensor,
confirmed on both the original rig and the new 2x40GC+1x20GC site).

SDK library lifecycle is managed by pypylon internally (no explicit
Initialize/Close needed).

NOTE — GigE grabber wedge stability fixes (all 3 cameras):
    All 3 cameras (VL, UV, Tail) on this rig were observed to wedge under
    the original GrabStrategy_LatestImageOnly + default OutputQueueSize
    setup (the SDK grabber thread would stop pulling from GenTL transport
    and never recover without an explicit StartGrabbing/StopGrabbing
    cycle). A verified fix, originally from a separate production
    deployment of the same acA1920-40gc model, is applied to every camera
    via self._needs_wedge_fix:
        - GrabStrategy_OneByOne (explicit per-frame drain) instead of
          LatestImageOnly
        - OutputQueueSize=1 to stop unconsumed frames piling up
        - smaller BUFFER_COUNT (2 instead of 5), consistent with explicit
          draining instead of SDK auto-discard
        - grabber-restart-in-place if IsGrabbing() is False at the top of
          capture() — recovers from the wedge if it still occurs
        - flush_buffers() + consecutive-timeout tracking on capture timeout
        - GevHeartbeatTimeout extended to 10000ms (control channel was
          observed dropping under brief network load with the 3s default)
        - Counter1/Counter2 hardware trigger counters disabled
        - LineDebouncer NOT configured — debounce is handled upstream by
          the PLC trigger pulse shaping on this rig (confirmed for all 3
          cameras; see change.txt)

    Independently re-verified directly on real hardware (not just inherited
    from the separate deployment):
        - acA1920-40gc (UV/Tail, SN 25371621 / 25375096): CounterReset
          raises AccessException ("Node is not writable") — counters
          confirmed non-functional on this firmware.
        - a2A2600-20gcPRO (VL, SN 41888019): with CounterEventSource
          explicitly set to FrameStart and 3 software triggers fired,
          Counter2 stayed at 0 — counters confirmed non-functional on this
          unit too (not just assumed by analogy anymore).
        - VL's DeviceTemperatureSelector does NOT accept "Sensor" on this
          unit (InvalidArgumentException) — get_temperature() now tries
          "Coreboard" first, matching the verified working reference, with
          a bare-attempt fallback if that also fails.

    A real bug was found and fixed during hardware testing: connect()'s
    original exposure-setting code used genicam.IsAvailable() to probe
    ExposureTime directly, which crashes with "Node not existing" on ace
    classic — IsAvailable() only guards nodes that exist but are
    disabled/locked, not nodes absent from the nodemap entirely. Exposure
    setting (and set_exposure()) now use an exception-based try/fallback
    instead of any availability probe, removing dependence on
    self._is_ace2 being detected correctly.
"""

import logging

import numpy as np
from pypylon import pylon, genicam


logger = logging.getLogger(__name__)


BUFFER_COUNT = 5              # unused now that wedge fix applies to all cameras — kept for reference
BUFFER_COUNT_40GC = 2         # used by all cameras when the wedge fix is active (OneByOne drain sizing)


class Camera:
    """Wrapper for a single Basler GigE camera via pypylon.

    Lifecycle:
        1. __init__()   — stores config (name, ip, exposure). No SDK calls.
        2. connect()    — opens device, configures trigger, starts acquisition.
        3. capture()    — blocks until sensor trigger, returns BGR image.
        4. disconnect() — stops acquisition, releases handle.
        Steps 2-4 can repeat. Instance stays alive.
    """

    def __init__(
        self,
        name: str,
        exposure: int,
        timeout: int = 30000,
        ip: str | None = None,
        serial: str | None = None,
        trigger_debounce_us: int = 0,
    ):
        """Create a camera instance. No SDK calls — just stores config.

        Args:
            name: Human-readable name ("VL", "UV", "Tail").
            exposure: Exposure time in microseconds.
            timeout: Default capture timeout in milliseconds.
            ip: Static IP address (GigE cameras).
            serial: Serial number (alternative identification).
            trigger_debounce_us: Accepted for interface parity but NOT applied.
                LineDebouncer is intentionally not configured on any of the
                3 cameras (UV, Tail, VL) — debounce is handled upstream by
                the PLC trigger pulse shaping on this rig (confirmed for
                both the original and new 2x40GC+1x20GC sites). Kept on the
                constructor so call sites and config don't need to change.
        """
        if not ip and not serial:
            raise ValueError(f"Camera '{name}': must provide either ip or serial")
        self.name = name
        self.ip = ip
        self.serial = serial
        self.exposure = exposure
        self.timeout = timeout
        self.trigger_debounce_us = trigger_debounce_us

        # pypylon handles — set on connect(), cleared on disconnect()
        self._camera: pylon.InstantCamera | None = None
        self._converter: pylon.ImageFormatConverter | None = None
        self._trigger_mode: str | None = None

        # Observability — software-side counters (camera-side via CounterSelector)
        self._frames_delivered: int = 0
        self._frames_skipped: int = 0
        self._last_block_id: int = 0
        self._block_id_gaps: int = 0
        self._last_timestamp: int = 0
        self._is_ace2: bool = False
        self._is_40gc_classic: bool = False  # acA1920-40gc — verified wedge-fix camera
        self._needs_wedge_fix: bool = False  # True for any camera the wedge fix applies to
        self._counter_available: bool = False
        self._line_counter_available: bool = False
        self._consecutive_timeouts: int = 0  # tracked for wedge diagnostics
        self._sg_stat_nodes: dict = {}  # discovered at connect() — see _discover_sg_stat_nodes

    @property
    def connected(self) -> bool:
        return self._camera is not None and self._camera.IsOpen()

    # ── Connection lifecycle ──────────────────────────────────────────

    def connect(self, devices=None):
        """Open the camera by IP or serial, configure trigger, start acquisition.

        Args:
            devices: Unused — kept for interface compatibility.
                pypylon discovers devices internally via TlFactory.

        Raises:
            RuntimeError: If the camera is not found on the network.
            genicam.GenericException: If SDK calls fail during setup.
        """
        if self.connected:
            logger.warning(f"Camera '{self.name}' already connected — disconnecting first")
            self.disconnect()

        tl_factory = pylon.TlFactory.GetInstance()

        # Find device by IP or serial
        di = pylon.DeviceInfo()
        if self.ip:
            di.SetIpAddress(self.ip)
        elif self.serial:
            di.SetSerialNumber(self.serial)

        try:
            self._camera = pylon.InstantCamera(tl_factory.CreateFirstDevice(di))
        except genicam.GenericException as e:
            ident = self.ip or self.serial
            raise RuntimeError(
                f"Camera '{self.name}' at {ident} not found — check network/power"
            ) from e

        self._camera.Open()

        ident = self.ip or self.serial
        model = self._camera.GetDeviceInfo().GetModelName()
        logger.info(f"Camera '{self.name}' opened: {model} [{ident}]")

        # Detect ace 2 vs ace classic — ace 2 model names start with "a2A".
        # Must be set BEFORE buffer pool / debounce / counters / exposure
        # because behavior (and SFNC node names) differ between generations.
        self._is_ace2 = model.startswith("a2A")
        # Detect the specific acA1920-40gc unit (UV, Tail) — the VERIFIED
        # wedge-fix camera, confirmed against a separate working production
        # deployment of this exact model.
        self._is_40gc_classic = model.startswith("acA1920-40gc")

        # Apply the wedge fix to every camera. Originally scoped to
        # acA1920-40gc only (verified); extended to a2A2600-20gcPRO (VL)
        # because VL was reported to show the same wedge/freeze symptom.
        # IMPORTANT: the VL extension is UNVERIFIED on real hardware —
        # in particular, Counter1/Counter2 misbehavior was confirmed on
        # the 40gc but only assumed (by analogy) on this 20gcPRO unit.
        # See change.txt before trusting this on the production line.
        self._needs_wedge_fix = self._is_40gc_classic or self._is_ace2

        # Buffer pool — smaller pool when the wedge fix is active, sized
        # for explicit draining (GrabStrategy_OneByOne) instead of the
        # SDK's auto-discard.
        buffer_count = BUFFER_COUNT_40GC if self._needs_wedge_fix else BUFFER_COUNT
        self._camera.MaxNumBuffer.Value = buffer_count

        # Cap the user-visible output queue at 1 frame when the wedge fix
        # is active. With OneByOne grab strategy, late-arriving frames
        # (e.g. trigger pulse arrives after RetrieveResult's timeout has
        # expired) would otherwise pile up in the output queue until
        # MaxNumBuffer is hit and the grabber wedges. OutputQueueSize=1
        # makes the SDK auto-drop older unconsumed frames before they
        # accumulate. Applied to every camera the wedge fix covers.
        if self._needs_wedge_fix:
            try:
                self._camera.OutputQueueSize = 1
                logger.info(f"Camera '{self.name}': OutputQueueSize=1 (wedge fix)")
            except genicam.GenericException as e:
                logger.warning(f"Camera '{self.name}': OutputQueueSize not supported ({e})")

        # Trigger setup — hardware Line1, RisingEdge, FrameStart
        self._camera.TriggerSelector.Value = "FrameStart"
        self._camera.TriggerMode.Value = "On"
        self.set_trigger("hardware")

        if genicam.IsAvailable(self._camera.TriggerActivation):
            current = self._camera.TriggerActivation.Value
            self._camera.TriggerActivation.Value = "RisingEdge"
            logger.info(f"Camera '{self.name}': TriggerActivation {current} → RisingEdge")

        # Trigger debounce — INTENTIONALLY NOT CONFIGURED on any of the 3
        # cameras (UV, Tail, VL). Debounce is handled upstream by the PLC
        # trigger pulse shaping on this rig — confirmed for all 3 cameras at
        # both the original site and the new 2x40GC+1x20GC site. This
        # matches the verified working reference deployment's approach for
        # its UV camera, now extended to all 3 here.
        #
        # Historical note: a previous version of this code attempted to
        # configure LineDebouncerTime/LineDebouncerTimeAbs directly on the
        # camera, with a fallback between the two SFNC node names. On
        # hardware testing this was found to fail outright on VL
        # (a2A2600-20gcPRO, SN 41888019) — neither node exists on that
        # unit's firmware ("Node not existing"). Rather than carry forward
        # a per-camera, partially-working attempt, debounce configuration
        # is now skipped entirely and consistently across all 3 cameras.
        if self.trigger_debounce_us > 0:
            logger.info(
                f"Camera '{self.name}': trigger_debounce_us={self.trigger_debounce_us} "
                f"requested but ignored (debounce handled upstream by PLC, "
                f"not configured on the camera for any of the 3 cameras on this rig)"
            )

        # Hardware trigger counters (Counter1/Counter2) — count Line1 rising
        # edges and compare against delivered frames to detect missed
        # triggers / debounce filtering.
        #
        # Disabled for every camera the wedge fix covers. CONFIRMED on the
        # acA1920-40gc (UV/Tail): writes silently no-op and reads return
        # invalid values there. NOT independently confirmed on the
        # a2A2600-20gcPRO (VL) — disabled here on the assumption that the
        # same wedge symptom implies the same counter issue, which has not
        # been verified on this specific unit. If you confirm VL's counters
        # actually work fine, this can be narrowed back to 40gc-only.
        self._counter_available = False
        self._line_counter_available = False
        if self._needs_wedge_fix:
            logger.info(
                f"Camera '{self.name}': Counter1/Counter2 disabled (wedge fix; "
                f"confirmed non-functional on this model via direct hardware test — "
                f"{'CounterReset raises AccessException' if self._is_40gc_classic else 'Counter2 did not increment after 3 forced triggers with EventSource=FrameStart'})"
            )
        else:
            try:
                self._camera.CounterSelector.Value = "Counter1"
                self._camera.CounterEventSource.Value = "FrameStart"
                if self._is_ace2:
                    # ace 2 requires explicit trigger source config
                    self._camera.CounterTriggerSource.Value = "Off"
                self._camera.CounterReset.Execute()
                self._counter_available = True
                logger.info(f"Camera '{self.name}': trigger counter (Counter1) enabled on FrameStart")
            except Exception as e:
                logger.debug(f"Camera '{self.name}': trigger counter not supported ({e})")

            # Second counter — count raw Line1 edges (trigger input, before debounce).
            # Comparing Counter2 (raw triggers) vs Counter1 (frames) shows debounce filtering.
            try:
                self._camera.CounterSelector.Value = "Counter2"
                self._camera.CounterEventSource.Value = "Line1"
                if self._is_ace2:
                    self._camera.CounterTriggerSource.Value = "Off"
                    self._camera.CounterEventActivation.Value = "RisingEdge"
                self._camera.CounterReset.Execute()
                self._line_counter_available = True
                logger.info(f"Camera '{self.name}': line counter (Counter2) enabled on Line1 edges")
            except Exception as e:
                logger.debug(f"Camera '{self.name}': line counter not supported ({e})")

        # Reset software-side observability counters
        self._frames_delivered = 0
        self._frames_skipped = 0
        self._last_block_id = 0
        self._block_id_gaps = 0
        self._last_timestamp = 0

        # Exposure — ace classic uses ExposureTimeAbs, ace 2 uses ExposureTime.
        # NOTE: genicam.IsAvailable() only safely guards nodes that EXIST but
        # are currently disabled/locked — it does NOT safely guard against a
        # node that's absent from the nodemap entirely (confirmed via
        # inspect_camera.py: ExposureTime raises "Node not existing" on ace
        # classic before IsAvailable() ever gets to run). Try
        # ExposureTimeAbs first, fall back to ExposureTime on exception —
        # this also removes any dependence on self._is_ace2 having
        # correctly detected the model generation.
        exposure_set = False
        try:
            self._camera.ExposureTimeAbs.Value = float(self.exposure)
            exposure_set = True
        except genicam.GenericException:
            try:
                self._camera.ExposureTime.Value = float(self.exposure)
                exposure_set = True
            except genicam.GenericException as e:
                logger.error(f"Camera '{self.name}': failed to set exposure ({e})")
        if exposure_set:
            logger.info(f"Camera '{self.name}': exposure = {self.exposure}us")

        # GigE transport — optimize packet size for throughput
        try:
            self._camera.GevSCPSPacketSize.Value = 8192
        except genicam.GenericException:
            logger.info(f"Camera '{self.name}': jumbo frame packet size not supported, using default")

        # Inter-packet delay — spread packets to avoid NIC overflow
        try:
            if genicam.IsAvailable(self._camera.GevSCPD):
                self._camera.GevSCPD.Value = 1000
        except genicam.GenericException:
            pass

        # Enable packet resend for reliability
        try:
            stream_grabber = self._camera.GetStreamGrabberNodeMap()
            stream_grabber["EnableResend"].Value = True
        except Exception:
            pass

        # Extend GVCP heartbeat timeout to 10s (default is 3s) for every
        # camera the wedge fix covers. The host sends a heartbeat every
        # ~1s; if the NIC or OS delays it past the camera's timeout the
        # camera closes the control channel (seen as Line1=None, temp=-1.0
        # in logs on the 40gc). 10s gives enough headroom for ARP
        # re-resolution or brief network load spikes.
        if self._needs_wedge_fix:
            try:
                tl_nodemap = self._camera.GetTLNodeMap()
                tl_nodemap.GetNode("HeartbeatTimeout").SetValue(10000)
                logger.info(f"Camera '{self.name}': GevHeartbeatTimeout set to 10000ms (wedge fix)")
            except Exception as e:
                logger.warning(f"Camera '{self.name}': could not set GevHeartbeatTimeout ({e})")

        # Image format converter — Bayer/Mono → BGR for OpenCV
        self._converter = pylon.ImageFormatConverter()
        self._converter.OutputPixelFormat = pylon.PixelType_BGR8packed
        self._converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned

        # Grab strategy: OneByOne for every camera the wedge fix covers —
        # every frame stays queued in order, host-side drain is explicit
        # via flush_buffers() before each capture. Avoids the
        # LatestImageOnly wedge where the SDK grabber thread stops pulling
        # from GenTL transport and never recovers without an explicit
        # StartGrabbing/StopGrabbing cycle. Any camera not covered by the
        # wedge fix keeps the original LatestImageOnly auto-discard.
        grab_strategy = (
            pylon.GrabStrategy_OneByOne if self._needs_wedge_fix
            else pylon.GrabStrategy_LatestImageOnly
        )
        self._camera.StartGrabbing(grab_strategy)

        debounce_str = f", debounce={self.trigger_debounce_us}us (requested, NOT applied)" if self.trigger_debounce_us > 0 else ""
        logger.info(
            f"Camera '{self.name}' ready — trigger={self._trigger_mode}, "
            f"exposure={self.exposure}us, buffers={buffer_count}{debounce_str}"
        )

        # Discover which stream grabber stat nodes are actually readable on
        # this camera/driver combination, instead of hardcoding node names
        # and silently getting -1 back for ones that don't exist. Cached so
        # get_stream_statistics() only queries nodes confirmed to exist.
        self._sg_stat_nodes = self._discover_sg_stat_nodes()

    def _discover_sg_stat_nodes(self) -> dict:
        """Enumerate stream grabber stat nodes actually readable on this camera.

        Logs all available node names at INFO so the correct names for this
        camera/driver combination can be identified if stats still come
        back as -1. Returns key -> node_name for nodes confirmed readable.
        """
        candidates = {
            "missed": "Statistic_Total_Missed_Frame_Count",
            "failed": "Statistic_Failed_Frame_Count",
            "buffer_underruns": "Statistic_Buffer_Underrun_Count",
            "resend_requests": "Statistic_Resend_Request_Count",
            "resend_packets": "Statistic_Resend_Packet_Count",
        }
        found: dict = {}
        try:
            sg = self._camera.GetStreamGrabberNodeMap()
            all_nodes = []
            for node in sg.GetNodes():
                try:
                    all_nodes.append(node.GetName())
                except Exception:
                    pass
            logger.info(f"Camera '{self.name}': stream grabber nodes: {sorted(all_nodes)}")
            for key, node_name in candidates.items():
                try:
                    val = sg[node_name].Value
                    found[key] = node_name
                    logger.info(f"Camera '{self.name}': stat node '{node_name}' = {val} (readable)")
                except Exception:
                    logger.debug(f"Camera '{self.name}': stat node '{node_name}' not readable")
        except Exception as e:
            logger.warning(f"Camera '{self.name}': stream grabber nodemap unavailable: {e}")
        return found

    def disconnect(self):
        """Stop acquisition and release device handle.

        Safe to call even if not connected or partially initialized.
        """
        if self._camera is None:
            return

        name = self.name

        try:
            if self._camera.IsGrabbing():
                self._camera.StopGrabbing()
        except genicam.GenericException as e:
            logger.warning(f"Camera '{name}': StopGrabbing failed: {e}")

        try:
            if self._camera.IsOpen():
                self._camera.Close()
        except genicam.GenericException as e:
            logger.warning(f"Camera '{name}': Close failed: {e}")

        self._camera = None
        self._converter = None
        self._trigger_mode = None

        logger.info(f"Camera '{name}' disconnected")

    def stop_acquisition(self):
        """Stop acquisition. Device stays open.

        After this call, no frames will be captured until
        start_acquisition() is called.
        """
        if not self.connected or self._camera is None:
            return

        try:
            if self._camera.IsGrabbing():
                self._camera.StopGrabbing()
        except genicam.GenericException as e:
            logger.warning(f"Camera '{self.name}': StopGrabbing failed: {e}")

        logger.info(f"Camera '{self.name}': acquisition stopped")

    def start_acquisition(self):
        """Restart acquisition after stop_acquisition().

        Safe to call if acquisition is already running (no-op).
        """
        if not self.connected or self._camera is None:
            return

        if self._camera.IsGrabbing():
            logger.debug(f"Camera '{self.name}': acquisition already running, skipping start")
            return

        grab_strategy = (
            pylon.GrabStrategy_OneByOne if self._needs_wedge_fix
            else pylon.GrabStrategy_LatestImageOnly
        )
        self._camera.StartGrabbing(grab_strategy)
        self.reset_trigger_count()
        logger.info(f"Camera '{self.name}': acquisition restarted")

    def reconnect(self, device_manager=None, settle_seconds: float = 0.0) -> bool:
        """Disconnect, optionally wait, then reconnect. Returns True on success.

        Args:
            device_manager: Unused — kept for interface compatibility.
            settle_seconds: Seconds to wait after disconnect before
                reconnecting. Useful if the camera needs time to fully
                drop off and re-register on the GigE network (ARP/DHCP
                settling) before a fresh connection attempt will succeed.
                0 = no wait (default, same as before this parameter existed).
        """
        logger.info(f"Camera '{self.name}': attempting reconnect (settle={settle_seconds}s)...")
        try:
            self.disconnect()
        except Exception as e:
            logger.warning(f"Camera '{self.name}': disconnect during reconnect failed: {e}")
            self._camera = None
            self._converter = None
            self._trigger_mode = None

        if settle_seconds > 0:
            import time
            logger.info(f"Camera '{self.name}': waiting {settle_seconds}s for GigE re-registration...")
            time.sleep(settle_seconds)

        try:
            self.connect()
            logger.info(f"Camera '{self.name}': reconnect successful")
            return True
        except Exception as e:
            logger.error(f"Camera '{self.name}': reconnect failed: {e}")
            return False

    # ── Configuration ─────────────────────────────────────────────────

    def set_trigger(self, mode: str):
        """Set trigger mode.

        Args:
            mode: 'hardware' for Line1 (proximity sensor) or 'software' for
                  software-triggered capture (calibration/live feed).
        """
        mode = mode.lower()
        if mode == "hardware":
            self._camera.TriggerSource.Value = "Line1"
        elif mode == "software":
            self._camera.TriggerSource.Value = "Software"
        else:
            raise ValueError(f"Unknown trigger mode: {mode}")
        self._trigger_mode = mode

    def set_exposure(self, microseconds: int):
        """Set exposure time in microseconds.

        Tries ExposureTimeAbs (ace classic) first, falls back to
        ExposureTime (ace 2) on exception — matches connect()'s approach,
        does not depend on self._is_ace2 being correctly detected.
        """
        try:
            self._camera.ExposureTimeAbs.Value = float(microseconds)
            self.exposure = microseconds
        except genicam.GenericException:
            try:
                self._camera.ExposureTime.Value = float(microseconds)
                self.exposure = microseconds
            except genicam.GenericException as e:
                logger.error(f"Camera '{self.name}': failed to set exposure ({e})")

    # ── Capture ───────────────────────────────────────────────────────

    def capture(self, timeout_ms: int | None = None) -> np.ndarray:
        """Capture a single frame, return as BGR numpy array.

        In hardware trigger mode: blocks until the proximity sensor fires.
        In software trigger mode: sends trigger command, then waits.

        Args:
            timeout_ms: Max wait time in ms. Defaults to self.timeout.

        Returns:
            BGR image as numpy array (uint8).

        Raises:
            TimeoutError: If no frame arrives within timeout.
            RuntimeError: If camera is not connected.
        """
        if not self.connected:
            raise RuntimeError(f"Camera '{self.name}' is not connected")

        if timeout_ms is None:
            timeout_ms = self.timeout

        # If the SDK grabber thread has explicitly stopped (IsGrabbing=
        # False), restart in place. This covers the case where pypylon
        # internally stopped the grabber thread due to an SDK error (the
        # observed wedge). Normal conveyor pauses produce timeouts but the
        # grabber stays running — no recovery needed for those. Applies to
        # every camera the wedge fix covers.
        if self._needs_wedge_fix:
            try:
                if not self._camera.IsGrabbing():
                    logger.warning(
                        f"Camera '{self.name}': grabber stopped — restarting acquisition in place"
                    )
                    self._camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
                    self._consecutive_timeouts = 0
            except genicam.GenericException as e:
                logger.warning(f"Camera '{self.name}': IsGrabbing/StartGrabbing failed: {e}")

        if self._trigger_mode == "software":
            if self._camera.WaitForFrameTriggerReady(1000, pylon.TimeoutHandling_Return):
                self._camera.ExecuteSoftwareTrigger()

        try:
            grab_result = self._camera.RetrieveResult(
                timeout_ms, pylon.TimeoutHandling_ThrowException
            )
        except genicam.TimeoutException:
            if self._needs_wedge_fix:
                self._consecutive_timeouts += 1
                # Flush any late-arriving frame so it does not corrupt the next cycle.
                self.flush_buffers()
            raise TimeoutError(
                f"Camera '{self.name}' capture timeout ({timeout_ms}ms) — "
                f"part may not have arrived at sensor"
            )
        except genicam.GenericException as e:
            raise RuntimeError(f"Camera '{self.name}' grab error: {e}") from e

        try:
            bgr = self._grab_to_bgr(grab_result)
            self._track_grab_result(grab_result)
            if self._needs_wedge_fix:
                self._consecutive_timeouts = 0
            return bgr
        finally:
            grab_result.Release()

    def _grab_to_bgr(self, grab_result) -> np.ndarray:
        """Convert a grab result to a BGR numpy array."""
        if not grab_result.GrabSucceeded():
            raise RuntimeError(
                f"Camera '{self.name}' grab failed: "
                f"{grab_result.ErrorCode} — {grab_result.ErrorDescription}"
            )

        if not self._converter.ImageHasDestinationFormat(grab_result):
            image = self._converter.Convert(grab_result)
            return image.GetArray().copy()
        else:
            return grab_result.Array.copy()

    def capture_latest(self, timeout_ms: int | None = None) -> np.ndarray:
        """Capture the latest frame, discarding any stale buffered frames.

        With GrabStrategy_LatestImageOnly (any camera not covered by the
        wedge fix), pypylon automatically keeps only the most recent frame,
        so this is equivalent to capture(). With GrabStrategy_OneByOne
        (every wedge-fix camera), capture() already calls flush_buffers()
        on timeout — this method is still equivalent to capture() since
        capture.py is responsible for draining stale frames before calling
        it, not this method itself.

        Args:
            timeout_ms: Max wait time in ms if buffer is empty.

        Returns:
            BGR image as numpy array (uint8) — the most recent frame.

        Raises:
            TimeoutError: If no frame arrives within timeout.
            RuntimeError: If camera is not connected.
        """
        return self.capture(timeout_ms)

    def flush_buffers(self):
        """Drain and discard any stale finished buffers.

        With GrabStrategy_LatestImageOnly (camera not covered by the wedge
        fix), stale frames are auto-discarded by the SDK; this drains
        anything still remaining in the output queue. With
        GrabStrategy_OneByOne (every wedge-fix camera — VL, UV, Tail as of
        the current fix scope), every frame stays queued until consumed, so
        this is the only thing that clears stale frames before the next
        capture.
        """
        if not self.connected or self._camera is None:
            return
        flushed = 0
        while True:
            try:
                grab_result = self._camera.RetrieveResult(0, pylon.TimeoutHandling_Return)
                if grab_result and grab_result.GrabSucceeded():
                    grab_result.Release()
                    flushed += 1
                else:
                    if grab_result:
                        grab_result.Release()
                    break
            except Exception:
                break
        if flushed:
            logger.info(f"Camera '{self.name}': flushed {flushed} stale buffer(s)")

    # ── Observability ────────────────────────────────────────────────

    def _track_grab_result(self, grab_result):
        """Extract per-frame observability from grab result metadata.

        Tracks:
        - block_id gaps: camera assigned frame N but we received N+2 → 1 transport drop
        - skipped images: frames pypylon discarded (LatestImageOnly strategy)
        - delivered count: total frames successfully delivered to application
        - timestamp: camera-side exposure timestamp (ticks)
        """
        self._frames_delivered += 1

        try:
            block_id = grab_result.GetBlockID()
            if self._last_block_id > 0 and block_id > self._last_block_id + 1:
                gap = block_id - self._last_block_id - 1
                self._block_id_gaps += gap
                logger.warning(
                    f"Camera '{self.name}': BlockID gap — expected {self._last_block_id + 1}, "
                    f"got {block_id} ({gap} frame(s) lost in transport)"
                )
            self._last_block_id = block_id
        except Exception:
            pass

        try:
            skipped = grab_result.GetNumberOfSkippedImages()
            if skipped > 0:
                self._frames_skipped += skipped
                logger.debug(
                    f"Camera '{self.name}': {skipped} frame(s) skipped (LatestImageOnly)"
                )
        except Exception:
            pass

        try:
            self._last_timestamp = grab_result.GetTimeStamp()
        except Exception:
            pass

    def health_check(self) -> bool:
        """Check if the camera is still reachable.

        Returns:
            True if connected and device is open, False otherwise.
        """
        if not self.connected:
            return False
        try:
            return self._camera.IsOpen()
        except Exception:
            return False

    def get_temperature(self) -> float:
        """Read camera sensor temperature in °C. Returns -1.0 if unavailable.

        DeviceTemperature is the canonical SFNC node and works on both
        generations. CONFIRMED ON HARDWARE: on VL (a2A2600-20gcPRO, SN
        41888019), DeviceTemperatureSelector="Sensor" raises
        InvalidArgumentException — this unit's firmware wants "Coreboard"
        instead. Try DeviceTemperature (with Coreboard selector) first,
        fall back to the legacy TemperatureAbs alias on exception — does
        not depend on self._is_ace2 being correctly detected.
        """
        if not self.connected:
            return -1.0
        try:
            try:
                self._camera.DeviceTemperatureSelector.Value = "Coreboard"
            except genicam.GenericException:
                pass
            return float(self._camera.DeviceTemperature.Value)
        except genicam.GenericException:
            pass
        try:
            return float(self._camera.TemperatureAbs.Value)
        except Exception:
            return -1.0

    def get_line_status(self) -> bool | None:
        """Read current state of trigger input line (Line1).

        Returns:
            True if line is high, False if low, None if unavailable.
        """
        if not self.connected:
            return None
        try:
            self._camera.LineSelector.Value = "Line1"
            return self._camera.LineStatus.Value
        except Exception:
            return None

    def get_line_status_all(self) -> int:
        """Read bitfield of all I/O line states in one call. -1 if unavailable.

        bit 0 = Line1, bit 1 = Line2, ... — useful for diagnosing wrong-line
        wiring without selecting each line individually.
        """
        if not self.connected:
            return -1
        try:
            return int(self._camera.LineStatusAll.Value)
        except Exception:
            return -1

    def get_acquisition_status(self) -> str:
        """Return the camera's current acquisition phase, e.g.
        'FrameTriggerWait', 'Idle'. Empty string if unavailable.

        'FrameTriggerWait' means the camera is armed and waiting for the
        next hardware trigger. If this is never seen during normal
        operation, capture is stuck somewhere before the trigger stage.
        """
        if not self.connected:
            return ""
        try:
            self._camera.AcquisitionStatusSelector.Value = "FrameTriggerWait"
            return "FrameTriggerWait" if self._camera.AcquisitionStatus.Value else "Idle"
        except Exception:
            return ""

    def get_trigger_count(self) -> int:
        """Read hardware frame counter (Counter1 — FrameStart events).

        This counts frames the camera actually produced. Compare against
        frames_delivered to detect transport-layer drops.

        Returns -1 if counter not available.
        """
        if not self.connected or not self._counter_available:
            return -1
        try:
            self._camera.CounterSelector.Value = "Counter1"
            return self._camera.CounterValue.Value
        except Exception:
            return -1

    def get_line_trigger_count(self) -> int:
        """Read raw trigger line counter (Counter2 — Line1 rising edges).

        This counts every rising edge on Line1, including edges
        suppressed by debounce. Compare against Counter1 to see how
        many spurious triggers the debouncer filtered.

        Returns -1 if counter not available.
        """
        if not self.connected or not self._line_counter_available:
            return -1
        try:
            self._camera.CounterSelector.Value = "Counter2"
            return self._camera.CounterValue.Value
        except Exception:
            return -1

    def reset_trigger_count(self):
        """Reset both hardware counters and software counters to zero."""
        if not self.connected:
            return
        if self._counter_available:
            try:
                self._camera.CounterSelector.Value = "Counter1"
                self._camera.CounterReset.Execute()
            except Exception:
                pass
        if self._line_counter_available:
            try:
                self._camera.CounterSelector.Value = "Counter2"
                self._camera.CounterReset.Execute()
            except Exception:
                pass
        self._frames_delivered = 0
        self._frames_skipped = 0
        self._last_block_id = 0
        self._block_id_gaps = 0

    def get_stream_statistics(self) -> dict:
        """Full observability snapshot — camera counters + transport stats + software stats.

        Returns a dict with:

        Camera-side (hardware counters):
            - frame_count: Counter1 — frames the camera produced (FrameStart)
            - line_trigger_count: Counter2 — raw Line1 edges (before debounce)
            - debounced: line_trigger_count - frame_count (triggers filtered by debounce)
            NOTE: both will read -1 on every camera on this rig — Counter1/
            Counter2 are confirmed non-functional on both acA1920-40gc
            (AccessException on reset) and a2A2600-20gcPRO (counter does
            not increment even with EventSource set correctly).

        Buffer pool depth (live, host-side — leading wedge indicator):
            - num_ready_buffers: frames sitting in the output queue, awaiting
              RetrieveResult. Should be 0-1 in steady state with
              OutputQueueSize=1; sustained growth is the wedge forming.
            - num_empty_buffers: buffers free for new frames
            - num_queued_buffers: buffers queued to GenTL for filling

        Transport-layer (stream grabber, queried only against nodes
        confirmed readable at connect() time via _discover_sg_stat_nodes —
        no more silent -1 from guessing wrong node names):
            - missed: frames camera sent but pylon never received (network drops)
            - failed: frames received but incomplete/corrupt
            - buffer_underruns: frames lost because no buffer was available
            - resend_requests: GigE packet resend requests (reliability indicator)
            - resend_packets: actual packets resent

        Application-side (software counters):
            - delivered: frames successfully converted to BGR and returned
            - skipped: frames pypylon discarded
            - block_id_gaps: gaps in camera BlockID sequence (transport drops)
            - temperature_c: camera sensor temperature

        Leakage analysis:
            frame_count > delivered + skipped → frames lost in transport
            line_trigger_count > frame_count → debouncer filtered spurious triggers
            block_id_gaps > 0 → frames dropped between camera and application
            num_ready_buffers > 1 sustained → wedge forming right now
        """
        frame_count = self.get_trigger_count()
        line_count = self.get_line_trigger_count()

        stats = {
            "camera": self.name,
            # Camera-side
            "frame_count": frame_count,
            "line_trigger_count": line_count,
            "debounced": (line_count - frame_count) if line_count >= 0 and frame_count >= 0 else -1,
            # Buffer pool depth
            "num_ready_buffers": -1,
            "num_empty_buffers": -1,
            "num_queued_buffers": -1,
            # Transport-layer
            "missed": -1,
            "failed": -1,
            "buffer_underruns": -1,
            "resend_requests": -1,
            "resend_packets": -1,
            # Application-side
            "delivered": self._frames_delivered,
            "skipped": self._frames_skipped,
            "block_id_gaps": self._block_id_gaps,
            "temperature_c": self.get_temperature(),
        }

        if not self.connected or self._camera is None:
            return stats

        # Buffer pool depth — read from InstantCamera attributes directly
        # (these are pypylon Python-binding attributes, not GenICam nodes,
        # so they're safe without try/except gymnastics — but kept defensive
        # anyway in case a given pypylon version doesn't expose them).
        try:
            stats["num_ready_buffers"] = int(self._camera.NumReadyBuffers.Value)
        except Exception:
            pass
        try:
            stats["num_empty_buffers"] = int(self._camera.NumEmptyBuffers.Value)
        except Exception:
            pass
        try:
            stats["num_queued_buffers"] = int(self._camera.NumQueuedBuffers.Value)
        except Exception:
            pass

        # Query only nodes confirmed readable at connect() time — every key
        # here is guaranteed to exist for this camera, no more silent -1
        # returns from guessing a node name that this driver doesn't expose.
        if self._sg_stat_nodes:
            try:
                sg = self._camera.GetStreamGrabberNodeMap()
                for key, node_name in self._sg_stat_nodes.items():
                    try:
                        stats[key] = sg[node_name].Value
                    except Exception:
                        continue
            except Exception:
                pass

        return stats

    def log_stream_statistics(self):
        """Log full observability snapshot at INFO level.

        Warns on any leakage: debounced triggers, transport drops,
        buffer underruns, or block ID gaps.
        """
        stats = self.get_stream_statistics()
        problems = []

        # Camera → transport leakage
        if stats["missed"] > 0:
            problems.append(f"missed={stats['missed']}")
        if stats["failed"] > 0:
            problems.append(f"failed={stats['failed']}")
        if stats["buffer_underruns"] > 0:
            problems.append(f"underrun={stats['buffer_underruns']}")
        if stats["block_id_gaps"] > 0:
            problems.append(f"block_id_gaps={stats['block_id_gaps']}")

        # Trigger leakage
        fc = stats["frame_count"]
        d = stats["delivered"]
        sk = stats["skipped"]
        if fc >= 0 and d >= 0:
            transport_loss = fc - (d + sk)
            if transport_loss > 0:
                problems.append(f"transport_loss={transport_loss}")

        if stats["debounced"] > 0:
            problems.append(f"debounced={stats['debounced']}")

        # Buffer-fill leading indicator — with OutputQueueSize=1 we should
        # never see ready_buffers > 1 in steady state on a wedge-fix camera.
        # Sustained growth here is the wedge forming, often visible before
        # capture() ever times out.
        if stats["num_ready_buffers"] > 1:
            problems.append(f"ready_buffers={stats['num_ready_buffers']}")

        # Temperature warning
        temp = stats["temperature_c"]
        if temp > 70.0:
            problems.append(f"TEMP={temp:.1f}°C")

        status = " ISSUES: " + ", ".join(problems) if problems else " OK"
        logger.info(
            f"Camera '{self.name}' stats: "
            f"triggers(line={stats['line_trigger_count']} frame={stats['frame_count']} "
            f"debounced={stats['debounced']}) "
            f"delivery(delivered={d} skipped={sk} gaps={stats['block_id_gaps']}) "
            f"buffers(ready={stats['num_ready_buffers']} empty={stats['num_empty_buffers']} "
            f"queued={stats['num_queued_buffers']}) "
            f"transport(missed={stats['missed']} failed={stats['failed']} "
            f"underrun={stats['buffer_underruns']} resend={stats['resend_requests']}) "
            f"temp={temp:.1f}°C{status}"
        )

    def get_incomplete_frame_count(self) -> int:
        """Get the count of incomplete/failed frames. Returns -1 if unavailable."""
        stats = self.get_stream_statistics()
        return stats.get("failed", -1)

    # ── Internal ──────────────────────────────────────────────────────

    def __repr__(self):
        status = "connected" if self.connected else "disconnected"
        ident = f"ip='{self.ip}'" if self.ip else f"serial='{self.serial}'"
        return f"Camera(name='{self.name}', {ident}, exposure={self.exposure}, {status})"