"""
YOLO12 detector — wraps Ultralytics YOLO for yarn inspection.

Returns structured Detection objects with bounding boxes and confidence
scores.  Supports three separate models:

    - **Visible**: yarn_cone + yarn_tube (requires 1.6 AR padding)
    - **UV**: cone + tube (requires 1.6 AR padding)
    - **Tail**: yarn_tail (no padding)

Supports both PyTorch (.pt) and TensorRT (.engine) model formats.
If a .engine file exists alongside the configured .pt file, the
TensorRT engine is used automatically for faster inference on Jetson.

Aspect-ratio padding and class-name aliasing are configurable per
instance so every model can use the same detector class.

Usage:
    # Visible / UV — padded to 1.6
    detector = YOLODetector("weights/visible_yolo.pt")

    # Tail — no padding
    detector = YOLODetector("weights/tail_yolo.pt", use_padding=False)

    # Explicit TensorRT engine
    detector = YOLODetector("weights/visible_yolo.engine")
"""

import logging
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from ultralytics import YOLO

from .data_types import Detection

logger = logging.getLogger(__name__)

# Default class aliases — covers visible + UV naming conventions.
# Instances can supply their own via the *class_aliases* parameter.
_DEFAULT_CLASS_ALIASES: dict[str, str] = {
    "cone": "yarn_cone",
    "tube": "yarn_tube",
    "yarn_cone": "yarn_cone",
    "yarn_tube": "yarn_tube",
    "tail": "yarn_tail",
    "yarn_tail": "yarn_tail",
}

# Training aspect ratio — must match the ratio used during dataset preparation
_TARGET_ASPECT_RATIO = 1.6


class YOLODetector:
    """YOLO object detector for yarn inspection.

    Keeps the highest-confidence detection per class and provides ROI
    extraction helpers.  Aspect-ratio padding is enabled by default
    (required for visible and UV models) but can be turned off for
    models trained without padding (e.g. tail detection).
    """

    def __init__(
        self,
        model_path: str,
        conf_threshold: float = 0.6,
        imgsz: int = 640,
        use_padding: bool = True,
        class_aliases: Optional[dict[str, str]] = None,
    ):
        """Load YOLO model (.pt or .engine).

        If model_path points to a .pt file and a .engine file exists in
        the same directory, the TensorRT engine is loaded instead. This
        allows config to always reference .pt paths — the engine is used
        automatically when available.

        Args:
            model_path: Path to the YOLO weights (.pt or .engine).
            conf_threshold: Minimum confidence to accept a detection.
            imgsz: Input image size for inference.
            use_padding: If True, pad to 1.6 aspect ratio before inference.
                Set to False for models trained without padding.
            class_aliases: Optional mapping of raw class names to canonical
                names (e.g. ``{"cone": "yarn_cone"}``).  When *None*, the
                built-in defaults are used.  Pass an empty dict to disable
                aliasing and accept all class names as-is.
        """
        resolved_path = self._resolve_model_path(model_path)
        self.model = YOLO(str(resolved_path), task="detect")
        
        # JETSON FIX: Disable ONNX simplification to avoid ARM64 C++ core dumps
        self.model.overrides['simplify'] = False
        
        self.conf_threshold = conf_threshold
        self.imgsz = imgsz
        self.use_padding = use_padding
        self._class_aliases = class_aliases if class_aliases is not None else _DEFAULT_CLASS_ALIASES
        self._class_names = self.model.names  # {0: 'yarn_cone', 1: 'yarn_tube', ...}

        backend = "TensorRT" if resolved_path.suffix == ".engine" else "PyTorch"
        logger.info(
            f"YOLODetector loaded: {resolved_path} ({backend}) | "
            f"classes={list(self._class_names.values())} | "
            f"conf={conf_threshold} | padding={use_padding}"
        )

    @staticmethod
    def _resolve_model_path(model_path: str) -> Path:
        """Resolve model path, preferring .engine over .pt when available.

        If the given path is a .pt file and a corresponding .engine file
        exists in the same directory, returns the .engine path. This allows
        config to always reference .pt paths — TensorRT engines are picked
        up automatically after running scripts/export_tensorrt.py.

        Args:
            model_path: Path to model file (str or Path).

        Returns:
            Resolved Path object.
        """
        path = Path(model_path)
        if path.suffix == ".pt":
            engine_path = path.with_suffix(".engine")
            if engine_path.exists():
                logger.info(
                    "TensorRT engine found: %s → using %s",
                    path.name, engine_path.name,
                )
                return engine_path
        return path

    def detect(self, image: np.ndarray) -> list[Detection]:
        """Run YOLO inference on a BGR image.

        When *use_padding* is enabled the image is padded to 1.6 aspect
        ratio and bbox coordinates are mapped back to the original space.

        Args:
            image: BGR numpy array from camera.

        Returns:
            List of Detection objects, one per detected class (highest conf).
        """
        if self.use_padding:
            input_img, pad_left, pad_top = self.pad_to_aspect_ratio(image)
            logger.debug(
                "YOLO: padded %dx%d → %dx%d (pad_left=%d, pad_top=%d)",
                image.shape[1], image.shape[0],
                input_img.shape[1], input_img.shape[0],
                pad_left, pad_top,
            )
        else:
            input_img, pad_left, pad_top = image, 0, 0
            logger.debug("YOLO: no padding, input %dx%d", image.shape[1], image.shape[0])

        results = self.model(input_img, conf=self.conf_threshold, imgsz=self.imgsz, verbose=False)

        if not results or len(results[0].boxes) == 0:
            logger.info("YOLO: no detections")
            return []

        orig_h, orig_w = image.shape[:2]

        # Group by class, keep highest confidence per class
        best_per_class: dict[str, Detection] = {}
        for box in results[0].boxes:
            cls_id = int(box.cls[0])
            cls_name = self._class_names.get(cls_id, f"class_{cls_id}")
            conf = float(box.conf[0])

            # Map bbox from padded space → original image space
            px1, py1, px2, py2 = box.xyxy[0].tolist()
            x1 = int(px1 - pad_left)
            y1 = int(py1 - pad_top)
            x2 = int(px2 - pad_left)
            y2 = int(py2 - pad_top)

            # Clamp to original image bounds
            x1 = max(0, min(x1, orig_w))
            y1 = max(0, min(y1, orig_h))
            x2 = max(0, min(x2, orig_w))
            y2 = max(0, min(y2, orig_h))

            # Normalize to canonical name if alias exists, otherwise keep as-is
            canonical_name = self._class_aliases.get(cls_name, cls_name)

            det = Detection(
                class_name=canonical_name,
                bbox=(x1, y1, x2, y2),
                confidence=conf,
            )

            if canonical_name not in best_per_class or conf > best_per_class[canonical_name].confidence:
                best_per_class[canonical_name] = det

        detections = list(best_per_class.values())
        logger.info(
            f"YOLO: {len(detections)} detection(s) — "
            + ", ".join(f"{d.class_name}({d.confidence:.2f})" for d in detections)
        )
        return detections

    def get_detection_by_class(
        self, detections: list[Detection], class_name: str
    ) -> Optional[Detection]:
        """Find a specific class in the detection list.

        Args:
            detections: List from detect().
            class_name: "yarn_cone" or "yarn_tube".

        Returns:
            Detection if found, None otherwise.
        """
        for det in detections:
            if det.class_name == class_name:
                return det
        return None

    @staticmethod
    def pad_to_aspect_ratio(
        img: np.ndarray,
        target_ratio: float = _TARGET_ASPECT_RATIO,
        pad_color: tuple = (0, 0, 0),
    ) -> tuple[np.ndarray, int, int]:
        """Pad an image so that width / height == target_ratio.

        Must match the padding used during YOLO training. The training
        pipeline used ratio=1.6 with black padding.

        Args:
            img: Input BGR image (H, W, C).
            target_ratio: Desired width/height ratio (default 1.6).
            pad_color: BGR pad color (default black).

        Returns:
            Tuple of (padded_image, pad_left, pad_top) so bbox coordinates
            can be mapped back to the original image space.
        """
        h, w = img.shape[:2]
        current_ratio = w / h

        if abs(current_ratio - target_ratio) < 1e-6:
            return img, 0, 0

        if current_ratio < target_ratio:
            # Need to pad width
            new_w = int(round(h * target_ratio))
            pad_total = new_w - w
            pad_left = pad_total // 2
            pad_right = pad_total - pad_left
            pad_top = pad_bottom = 0
        else:
            # Need to pad height
            new_h = int(round(w / target_ratio))
            pad_total = new_h - h
            pad_top = pad_total // 2
            pad_bottom = pad_total - pad_top
            pad_left = pad_right = 0

        padded = cv2.copyMakeBorder(
            img, pad_top, pad_bottom, pad_left, pad_right,
            borderType=cv2.BORDER_CONSTANT,
            value=pad_color,
        )

        return padded, pad_left, pad_top

    @staticmethod
    def extract_roi(image: np.ndarray, detection: Detection) -> np.ndarray:
        """Crop the detected region from the original (unpadded) frame.

        Returns the full rectangular bbox crop including background corners.
        Use extract_circular_roi() when you need only the circular object
        with background masked to black.

        Args:
            image: Full BGR frame (original, not padded).
            detection: Detection with bbox in original image coordinates.

        Returns:
            Cropped BGR image of the detected object.
        """
        x1, y1, x2, y2 = detection.bbox
        h, w = image.shape[:2]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        return image[y1:y2, x1:x2].copy()

    @staticmethod
    def extract_circular_roi(
        image: np.ndarray, detection: Detection, margin: float = 0.05,
    ) -> np.ndarray:
        """Crop the detected region and apply a circular mask.

        Both yarn_cone and yarn_tube are circular objects in rectangular
        bboxes. This method masks out the corner pixels (background, metal
        housing, surrounding yarn) by inscribing a circle within the bbox
        and setting everything outside to black.

        The radius is reduced by ``margin`` (default 5%) to strip edge
        pixels where background or adjacent objects leak in due to
        off-center positioning on the conveyor. This is the same concept
        as the radial crop in polar unwarp, but applied directly on the
        circular mask for consumers that don't use polar unwarp (e.g.
        tube pattern matching with color + patch voting).

        Args:
            image: Full BGR frame (original, not padded).
            detection: Detection with bbox in original image coordinates.
            margin: Fraction to shrink the radius (default 0.05 = 5%).

        Returns:
            Cropped BGR image with pixels outside the inscribed circle
            set to black (0, 0, 0).
        """
        x1, y1, x2, y2 = detection.bbox
        h, w = image.shape[:2]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        crop = image[y1:y2, x1:x2].copy()

        ch, cw = crop.shape[:2]
        cx, cy = cw // 2, ch // 2
        radius = int(min(cw, ch) // 2 * (1.0 - margin))

        mask = np.zeros((ch, cw), dtype=np.uint8)
        cv2.circle(mask, (cx, cy), radius, 255, -1)
        crop[mask == 0] = 0

        return crop

    @staticmethod
    def extract_annular_roi(
        image: np.ndarray,
        detection: Detection,
        inner_ratio: float = 0.30,
        outer_margin: float = 0.05,
        exclude_clock_range: Optional[tuple[float, float]] = None,
    ) -> np.ndarray:
        """Crop the detected region and apply an annular (ring) mask.

        Used for tube pattern matching where the tube has:
        - Outer boundary (the label/paper edge) → yarn_tube bbox
        - Inner hole (dark center where yarn passes) → fixed ratio of outer

        This method masks BOTH:
        - Outer corners (outside the tube's outer circle) → black
        - Inner hole (inside the tube's inner circle) → black

        The result is a ring-shaped region containing only the tube label.

        ```
        Before:                     After:
        ┌─────────────────┐         ┌─────────────────┐
        │## ┌─────────┐ ##│         │   ┌─────────┐   │
        │#  │         │  #│         │   │█████████│   │
        │#  │  ┌───┐  │  #│  ───►   │   │██     ██│   │
        │#  │  │ ⬤ │  │  #│         │   │██     ██│   │
        │#  │  └───┘  │  #│         │   │█████████│   │
        │#  │         │  #│         │   │         │   │
        │## └─────────┘ ##│         │   └─────────┘   │
        └─────────────────┘         └─────────────────┘
          corners + hole              ring only
        ```

        Args:
            image: Full BGR frame (original, not padded).
            detection: Detection with bbox in original image coordinates.
            inner_ratio: Inner hole radius as fraction of outer radius.
                Default 0.30 means inner hole is 30% of outer diameter.
            outer_margin: Fraction to shrink outer radius (default 0.05 = 5%).
            exclude_clock_range: Optional (start, end) sector to additionally
                black out, given as clock-face minute positions (0-60, where
                0/60 = 12 o'clock/top and the sector runs clockwise — e.g.
                the minute hand at 15 = 3 o'clock, 30 = 6 o'clock). Wraps
                around 60 if end < start. Use this to mask a region of the
                ring that's unreliable due to a fixed camera/mounting offset
                (e.g. (20, 40) blacks out the ~4-to-8-o'clock lower sector).

        Returns:
            Cropped BGR image with annular mask applied (corners, inner
            hole, and optional excluded sector set to black).
        """
        x1, y1, x2, y2 = detection.bbox
        h, w = image.shape[:2]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        crop = image[y1:y2, x1:x2].copy()

        ch, cw = crop.shape[:2]
        cx, cy = cw // 2, ch // 2

        # Outer radius (with margin to avoid edge artifacts)
        outer_radius = int(min(cw, ch) // 2 * (1.0 - outer_margin))

        # Inner radius (hole to be blacked out)
        inner_radius = int(outer_radius * inner_ratio)

        # Create annular mask: outer circle minus inner circle
        mask = np.zeros((ch, cw), dtype=np.uint8)
        cv2.circle(mask, (cx, cy), outer_radius, 255, -1)  # Fill outer
        cv2.circle(mask, (cx, cy), inner_radius, 0, -1)    # Cut out inner

        if exclude_clock_range is not None:
            start_min, end_min = exclude_clock_range
            start_deg = (start_min / 60.0) * 360.0
            end_deg = (end_min / 60.0) * 360.0

            yy, xx = np.mgrid[0:ch, 0:cw]
            # Angle clockwise from top (12 o'clock), matching clock-minute convention
            angle = (np.degrees(np.arctan2(xx - cx, cy - yy)) + 360.0) % 360.0

            if start_deg <= end_deg:
                sector = (angle >= start_deg) & (angle <= end_deg)
            else:
                sector = (angle >= start_deg) | (angle <= end_deg)  # wraps past 0/60

            mask[sector] = 0

        # Apply mask (everything outside the ring, or in the excluded sector → black)
        crop[mask == 0] = 0

        return crop
