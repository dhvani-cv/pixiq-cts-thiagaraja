"""
Pipeline — main entry point for the Sieger v2 inspection system.

Orchestrates:
    1. PLC communication (read trigger + material_id, write results)
    2. Camera capture (VL, UV, Tail — sequential, hardware-triggered)
    3. Image processing (visible inspection, UV inspection, tail inspection)
    4. Result aggregation and PLC write

Flow per part:
    PLC read (wait for trigger)
        → VL capture → VL processing
        → UV capture → UV processing (if enabled)
        → Tail capture → Tail processing (if enabled)
        → Combine results → PLC write → PLC ack

Usage:
    python src/pipeline.py
    python src/pipeline.py --config src/config.json

Configuration (config.json):
    {
        "plc": {
            "host": "192.168.2.1",
            "port": 502,
            "unit_id": 1,
            "timeout": 3.0,
            "poll_interval": 0.1
        },
        "cameras": { ... },
        "inspection": { ... }
    }
"""

import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from camera import Camera, CaptureSequence
from inspection.visible import VisibleInspection
from plc import PLCClient, PLCInput, PLCOutput

logger = logging.getLogger(__name__)


class Pipeline:
    """Main application controller.

    Owns:
        - PLC client (Modbus TCP)
        - 3 Camera instances (Basler pypylon)
        - Inspection modules (visible, UV, tail)

    Lifecycle:
        1. __init__() — load config, create instances (no connections)
        2. start() — connect PLC, init SDK, connect cameras
        3. run() — main loop: wait for trigger → capture → inspect → write
        4. stop() — disconnect everything, close SDK
    """

    def __init__(self, config_path: str = None):
        """Load config and create all instances (no connections yet).

        Args:
            config_path: Path to config.json. Defaults to src/config.json.
        """
        if config_path is None:
            config_path = str(Path(__file__).parent / "config.json")

        with open(config_path) as f:
            self.config = json.load(f)

        # PLC client
        plc_cfg = self.config.get("plc", {})
        self.plc = PLCClient(
            host=plc_cfg.get("host", "192.168.2.1"),
            port=plc_cfg.get("port", 502),
            unit_id=plc_cfg.get("unit_id", 1),
            timeout=plc_cfg.get("timeout", 3.0),
        )
        self.plc_poll_interval = plc_cfg.get("poll_interval", 0.1)

        # Cameras
        cams = self.config["cameras"]
        self.cam_vl = Camera(
            name="VL",
            ip=cams["VL"]["ip"],
            exposure=cams["VL"]["exposure"],
            timeout=cams["VL"]["timeout"],
        )
        self.cam_uv = Camera(
            name="UV",
            ip=cams["UV"]["ip"],
            exposure=cams["UV"]["exposure"],
            timeout=cams["UV"]["timeout"],
        )
        self.cam_tail = Camera(
            name="Tail",
            ip=cams["Tail"]["ip"],
            exposure=cams["Tail"]["exposure"],
            timeout=cams["Tail"]["timeout"],
        )
        self.capture_seq = CaptureSequence(self.cam_vl, self.cam_uv, self.cam_tail)

        # Inspection modules
        inspection_config = self.config.get("inspection", {})
        self.tasks_enabled = inspection_config.get("tasks", {})

        # Visible light inspection (always needed)
        self.vl_inspector = VisibleInspection(inspection_config)

        # UV inspection (future)
        self.uv_inspector = None  # TODO: implement UV inspection module

        # Tail inspection (future)
        self.tail_inspector = None  # TODO: implement tail inspection module

        self._running = False
        logger.info("Pipeline initialized")

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self):
        """Connect PLC, initialize SDK, connect cameras."""
        # Connect PLC
        if not self.plc.connect():
            logger.warning("PLC connection failed — running without PLC")

        # Connect cameras (pypylon discovers devices internally)
        self.cam_vl.connect()
        self.cam_uv.connect()
        self.cam_tail.connect()
        logger.info("All 3 Basler cameras connected")

        self._running = True
        logger.info("Pipeline started — PLC + 3 cameras ready")

    def stop(self):
        """Disconnect everything and close SDK."""
        self._running = False

        # Disconnect cameras
        for cam in (self.cam_vl, self.cam_uv, self.cam_tail):
            try:
                cam.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting {cam.name}: {e}")

        # Close inspection modules
        if self.vl_inspector is not None:
            self.vl_inspector.close()

        # Disconnect PLC
        self.plc.disconnect()

        logger.info("Pipeline stopped")

    def run(self):
        """Main entry point: start → loop → stop."""
        _setup_logging()
        self._install_signal_handlers()

        self.start()
        try:
            logger.info("Entering main loop — waiting for PLC trigger")
            while self._running:
                self._run_once()
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            self.stop()

    # ── Main processing loop ───────────────────────────────────────────

    def _run_once(self):
        """One complete part cycle.

        Steps:
            1. Wait for PLC trigger (poll)
            2. Read material_id from PLC
            3. Capture all 3 images
            4. Process VL image
            5. Process UV image (if enabled)
            6. Process Tail image (if enabled)
            7. Combine results
            8. Write to PLC
            9. Ack to PLC
        """
        # Step 1: Wait for PLC trigger
        plc_input = self._wait_for_trigger()
        if plc_input is None:
            # PLC not connected — use mock data for testing
            plc_input = PLCInput(trigger=True, sample_counter=0, material_no=0)

        material_id = str(plc_input.material_no)
        logger.info(f"=== Part {plc_input.sample_counter}: material={material_id} ===")

        # Step 2-6: Capture and process all cameras
        vl_result = None
        uv_result = None
        tail_result = None

        try:
            # VL: Capture and process
            t0 = time.perf_counter()
            vl_image = self.cam_vl.capture()
            t_capture = time.perf_counter() - t0
            logger.info(f"VL captured: {vl_image.shape} in {t_capture:.3f}s")

            if self.vl_inspector is not None:
                vl_result = self.vl_inspector.process_frame(vl_image, material_id)
                logger.info(
                    f"VL result: code={vl_result.result_code} "
                    f"({'PASS' if vl_result.passed else 'FAIL'})"
                )

            # UV: Capture and process (if enabled)
            if self.tasks_enabled.get("uv_inspection", False):
                uv_image = self.cam_uv.capture()
                logger.info(f"UV captured: {uv_image.shape}")
                # TODO: UV processing
                # uv_result = self.uv_inspector.process_frame(uv_image, material_id)
            else:
                # Still need to capture to consume the trigger
                uv_image = self.cam_uv.capture()
                logger.info(f"UV captured (disabled): {uv_image.shape}")

            # Tail: Capture and process (if enabled)
            tail_image = self.cam_tail.capture()
            logger.info(f"Tail captured: {tail_image.shape}")
            # TODO: Tail processing

        except TimeoutError as e:
            logger.error(f"Capture timeout: {e}")
            # Write error result to PLC
            output = PLCOutput(result_code=3)
            self._write_and_ack(output)
            return

        # Step 7: Combine results
        vl_code = vl_result.result_code if vl_result else 3
        uv_code = None  # TODO: get from uv_result when implemented
        tail_code = None  # TODO: get from tail_result when implemented

        output = PLCOutput.from_results(
            vl_code, uv_code, tail_code,
            material_no=plc_input.material_no,
            basket_no=plc_input.basket_no,
            loader_id=plc_input.loader_id,
        )
        logger.info(
            f"Combined result: {output.result_code} "
            f"(material={plc_input.material_no})"
        )

        # Step 8-9: Write results and ack
        self._write_and_ack(output)

    def _wait_for_trigger(self) -> Optional[PLCInput]:
        """Poll PLC until trigger is set.

        Returns:
            PLCInput with material_id and trigger=True, or None if PLC not connected.
        """
        if not self.plc.connected:
            # No PLC — return immediately for testing
            time.sleep(0.5)  # Simulate trigger wait
            return None

        while self._running:
            if self.plc.read_trigger():
                # Trigger set — read full input
                plc_input = self.plc.read_input()
                if plc_input is not None and plc_input.trigger:
                    return plc_input

            time.sleep(self.plc_poll_interval)

        return None

    def _write_and_ack(self, output: PLCOutput):
        """Write results to PLC and set ack flag."""
        if self.plc.connected:
            self.plc.write_output(output)
            self.plc.ack_complete()
            # Wait for PLC to clear trigger before clearing ack
            time.sleep(0.05)
            self.plc.clear_ack()

    # ── Signal handling ────────────────────────────────────────────────

    def _install_signal_handlers(self):
        """Register SIGTERM/SIGINT for clean shutdown."""

        def _handle_signal(signum, _frame):
            name = signal.Signals(signum).name
            logger.info(f"Received {name} — shutting down")
            self._running = False

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)


def _setup_logging():
    """Configure root logger."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


if __name__ == "__main__":
    config = sys.argv[1] if len(sys.argv) > 1 else None
    pipeline = Pipeline(config_path=config)
    pipeline.run()
