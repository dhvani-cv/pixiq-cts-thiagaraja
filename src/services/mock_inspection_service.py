"""
Mock Inspection Service for local testing without hardware.

Inherits from the real InspectionService and overrides hardware
initialization methods to use mock cameras and a mock PLC.
"""
import logging
import time

from .inspection_service import InspectionService, InspectionState

# Import mock hardware classes
from ..camera.mock_camera import MockCamera
from ..camera.mock_capture import MockCaptureSequence
from ..plc.mock_plc_client import MockPLCClient

logger = logging.getLogger(__name__)


class MockInspectionService(InspectionService):
    """
    An inspection service that uses simulated hardware (cameras and PLC)
    by reading images from folders and simulating PLC logic.
    """

    def __init__(self, config: dict):
        # Call the parent constructor
        super().__init__(config)
        logger.info("--- MockInspectionService Initialized ---")

    def _init_cameras(self):
        """
        Overrides the real camera initialization to set up mock cameras
        that read from local folders.
        """
        try:
            cams_cfg = self.config.get("cameras", {})
            if not cams_cfg:
                raise ValueError("Mock camera configuration is missing in 'cameras' section.")

            for cam_name, cam_cfg in cams_cfg.items():
                folder_path = cam_cfg.get("folder_path")
                if not folder_path:
                    raise ValueError(f"Camera '{cam_name}' missing 'folder_path' in config")
                
                cam = MockCamera(
                    name=cam_name.upper(),
                    folder_path=folder_path,
                )
                cam.connect()
                self._cameras[cam_name.upper()] = cam

            if all(k in self._cameras for k in ("VL", "UV", "TAIL")):
                self._capture_seq = MockCaptureSequence(
                    cam_vl=self._cameras["VL"],
                    cam_uv=self._cameras["UV"],
                    cam_tail=self._cameras["TAIL"],
                )
                logger.info("MockCaptureSequence is ready with all 3 mock cameras.")
            else:
                raise RuntimeError("Failed to initialize all three mock cameras.")

        except Exception as e:
            logger.exception(f"Mock camera initialization failed: {e}")

    def _init_plc(self):
        """Overrides the real PLC initialization to set up a mock PLC."""
        try:
            plc_cfg = self.config.get("plc", {})
            self._plc = MockPLCClient(plc_cfg)
            connected = self._plc.connect()
            self.state.plc_connected = connected
            if connected:
                logger.info("MockPLCClient connected (simulated).")
            else:
                logger.warning("MockPLCClient connection failed (simulated).")
        except Exception as e:
            logger.exception(f"Mock PLC initialization failed: {e}")
    
    def _cleanup_cameras(self):
        """Disconnects all mock cameras."""
        for cam in self._cameras.values():
            cam.disconnect()
        self._cameras.clear()
        self._capture_seq = None
        logger.info("Mock cameras cleaned up.")

    def run(self):
        """
        Starts the Socket.IO server after initializing mock hardware.
        This overrides the parent `run` method to ensure mock hardware is used.
        """
        logger.info("Starting MockInspectionService on http://%s:%d", self.host, self.port)

        # Initialize mock hardware
        self._init_cameras()
        self._init_plc()

        import eventlet
        import eventlet.wsgi
        eventlet.wsgi.server(
            eventlet.listen((self.host, self.port)),
            self.app,
            log_output=False,
        )

    def _run_inspection_cycle(self):
        """
        Overrides the inspection cycle to better work with the mock PLC.
        The main difference is that we don't need a polling loop for the trigger,
        as the mock PLC can signal it immediately.
        """
        # In mock mode, we add a simple delay to simulate time between inspections
        if self.state.get_state() == InspectionState.INSPECT:
            logger.info("--- Simulating new inspection cycle ---")
            time.sleep(2) # Wait 2 seconds before starting the next mock inspection
        
        # Call the original inspection cycle logic from the parent class
        super()._run_inspection_cycle()

    def _run_capture_cycle(self):
        """
        Overrides the capture cycle for mock mode.
        """
        if self.state.get_state() == InspectionState.CAPTURE:
            logger.info("--- Simulating new capture cycle ---")
            time.sleep(2)

        super()._run_capture_cycle()


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path
    
    parser = argparse.ArgumentParser(description="Mock Inspection Service")
    parser.add_argument("--config", type=str, default="src/config.json", help="Config file path")
    parser.add_argument("--host", type=str, help="Host to bind to")
    parser.add_argument("--port", type=int, help="Port to bind to")
    args = parser.parse_args()
    
    config_path = Path(args.config)
    config = {}
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
    else:
        logger.warning("Config not found: %s", config_path)
    
    if "service" not in config:
        config["service"] = {}
    if args.host:
        config["service"]["host"] = args.host
    if args.port:
        config["service"]["port"] = args.port
    
    service = MockInspectionService(config)
    try:
        service.run()
    except KeyboardInterrupt:
        service.stop()

