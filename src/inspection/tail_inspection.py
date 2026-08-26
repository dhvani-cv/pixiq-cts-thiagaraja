"""
Tail Inspection — YOLO-based yarn tail detection.

A properly wound yarn cone should have a visible tail end.
This module uses a dedicated YOLO model to detect the yarn tail.
If the tail is detected the cone passes; if missing it is a defect.

Pipeline:
    Camera frame → YOLO detect (yarn_tail) → TailResult

Usage:
    inspector = TailInspection(config)
    result = inspector.process_frame(frame)
    if not result.model_loaded:
        plc_code = 3  # Error
    elif not result.tail_detected:
        plc_code = 2  # Defect — missing tail
    else:
        plc_code = 1  # Good
"""

import logging

import numpy as np

from .data_types import TailResult
from .yolo_detector import YOLODetector

logger = logging.getLogger(__name__)


class TailInspection:
    """Yarn tail detector using a dedicated YOLO model.

    The YOLO model detects a single class (``yarn_tail``).  If the
    class is found with sufficient confidence the cone passes;
    otherwise the tail is considered missing (defect).

    This model does **not** require 1.6 aspect-ratio padding — it uses
    ``YOLODetector`` with ``use_padding=False``.
    """

    def __init__(self, config: dict):
        """Initialize tail inspection from config.

        Args:
            config: The ``tail_inspection`` config section.
                Expected keys (all optional with defaults):
                    yolo_weights (str): Path to tail YOLO weights.
                    yolo_conf (float): YOLO confidence threshold.
        """
        weights = config.get("yolo_weights", "weights/tail_yolo.pt")
        conf = config.get("yolo_conf", 0.5)

        try:
            self.detector = YOLODetector(
                model_path=weights,
                conf_threshold=conf,
                use_padding=False,
            )
            self._model_loaded = True
        except Exception:
            logger.warning("Tail YOLO model not found at %s", weights)
            self.detector = None
            self._model_loaded = False

        logger.info(
            "TailInspection initialized | model_loaded=%s | weights=%s",
            self._model_loaded,
            weights,
        )

    def process_frame(self, frame: np.ndarray) -> TailResult:
        """Run tail detection on one frame.

        Args:
            frame: BGR camera frame.

        Returns:
            TailResult. If the YOLO model is not loaded, returns
            model_loaded=False so the caller can map to result_code=3.
        """
        if self.detector is None:
            return TailResult(
                tail_detected=False,
                confidence=0.0,
                model_loaded=False,
            )

        try:
            logger.debug("Tail: running YOLO on %dx%d frame", frame.shape[1], frame.shape[0])
            detections = self.detector.detect(frame)
            tail_det = self.detector.get_detection_by_class(detections, "yarn_tail")

            if tail_det is None:
                logger.info("Tail: not detected — missing tail")
                return TailResult(tail_detected=False, confidence=0.0)

            logger.info("Tail: detected (conf=%.2f)", tail_det.confidence)
            logger.debug("  Tail bbox=(%d,%d,%d,%d)", *tail_det.bbox)
            return TailResult(
                tail_detected=True,
                confidence=tail_det.confidence,
                bbox=tail_det.bbox,
            )
        except Exception:
            logger.exception("Unexpected error in Tail process_frame — returning model_loaded=False")
            return TailResult(tail_detected=False, confidence=0.0, model_loaded=False)
