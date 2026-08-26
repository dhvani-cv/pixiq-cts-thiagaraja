"""
Stain detector — PatchCore anomaly detection on annular-masked yarn cone crop.

Replaces the previous Gabor PCA detector with PatchCore (anomalib).
Uses WideResNet50 backbone + coreset memory bank for anomaly detection.

Approach:
    1. Receive rectangular cone crop + geometry (center, inner_r, outer_r)
    2. Apply annular mask (black out tube hole + background corners)
    3. Resize to model input size (256x256)
    4. Run PatchCore inference via anomalib TorchInferencer
    5. Mask anomaly heatmap with annular mask to ignore non-surface regions
    6. Score = max anomaly value in valid (yarn surface) region

Detects: stains, dirt, threads, labels, foreign materials, color variation.

Usage:
    detector = StainDetector("models/patchcore", threshold=0.5)
    result = detector.detect(cone_crop, center=(660, 660), inner_r=130, outer_r=650)
    if result.has_stain:
        show_heatmap(result.heatmap)
"""

import logging
import os
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .data_types import StainResult

logger = logging.getLogger(__name__)


def _make_annular_mask(
    shape: tuple[int, int],
    center: tuple[int, int],
    inner_r: float,
    outer_r: float,
) -> np.ndarray:
    """Create binary annular mask — 255 for yarn surface, 0 for tube/background.

    Args:
        shape: (height, width) of the image.
        center: (cx, cy) in image coordinates.
        inner_r: Tube hole radius.
        outer_r: Cone outer radius.

    Returns:
        uint8 mask (255=valid yarn surface, 0=invalid).
    """
    h, w = shape
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(mask, center, int(outer_r), 255, -1)
    cv2.circle(mask, center, int(inner_r), 0, -1)
    return mask


class StainDetector:
    """PatchCore anomaly detector for yarn cone surface inspection.

    Loads a trained PatchCore model (anomalib) and runs inference on the
    annular-masked cone crop. The model learns the normal texture distribution
    from good cone images; anything deviating from learned normal = defect.

    The annular mask ensures only the yarn surface (donut ring between tube
    and cone edge) is scored — tube hole and background corners are ignored.
    """

    def __init__(
        self,
        model_path: str,
        threshold: float = 0.5,
        input_size: tuple = (256, 256),
    ):
        """Load PatchCore model for anomaly detection.

        Args:
            model_path: Path to model directory containing
                weights/torch/model.pt.
            threshold: Anomaly score threshold. Images with max anomaly
                score above this are flagged as defective.
            input_size: Model input size (must match training).
        """
        self.model_path = model_path
        self.threshold = threshold
        self.input_size = input_size
        self._model = None
        self._device = None

        self._load_model(model_path)

    def _load_model(self, model_path: str) -> None:
        """Load PatchCore model directly via torch.load with normalization disabled.

        Uses the same approach as sieger-parkdale-loop3-cv/StainInspector:
        load checkpoint directly, disable post_processor.enable_normalization,
        run raw forward pass. This avoids anomalib's internal min-max
        normalization which saturates scores to 1.0 when test set is small.
        """
        import torch

        model_dir = Path(model_path)
        torch_model = model_dir / "weights" / "torch" / "model.pt"

        if not torch_model.exists():
            candidates = list(model_dir.rglob("model.pt"))
            if not candidates:
                logger.warning(
                    "No model.pt found at %s. Stain detection will use fallback.",
                    model_path,
                )
                return
            torch_model = candidates[0]

        try:
            os.environ["TRUST_REMOTE_CODE"] = "1"
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            ckpt = torch.load(str(torch_model), map_location=device, weights_only=False)
            model = ckpt["model"] if isinstance(ckpt, dict) else ckpt
            model.eval()
            model.to(device)
            if hasattr(model, "post_processor") and model.post_processor is not None:
                model.post_processor.enable_normalization = False
            self._model = model
            self._device = device
            logger.info("PatchCore model loaded from %s (normalization disabled)", torch_model)
        except Exception as e:
            logger.error("Failed to load PatchCore model: %s", e)

    def detect(
        self,
        image: np.ndarray,
        center: tuple = None,
        inner_r: float = None,
        outer_r: float = None,
    ) -> StainResult:
        """Run anomaly detection on a cone crop.

        Args:
            image: Cropped BGR image of the yarn_cone region.
            center: (cx, cy) cone center in crop coordinates.
            inner_r: Inner radius (tube hole boundary).
            outer_r: Outer radius (cone edge).

        Returns:
            StainResult with anomaly score, stain flag, and heatmap.
        """
        if image is None or image.size == 0:
            logger.warning("Empty image passed to StainDetector")
            return StainResult(anomaly_score=0.0, has_stain=False)

        if center is None or inner_r is None or outer_r is None:
            logger.warning("Geometry not provided — cannot run stain detection")
            return StainResult(anomaly_score=0.0, has_stain=False)

        if self._model is not None:
            return self._detect_patchcore(image, center, inner_r, outer_r)
        return self._detect_fallback(image, center, inner_r, outer_r)

    def _detect_patchcore(
        self,
        image: np.ndarray,
        center: tuple,
        inner_r: float,
        outer_r: float,
    ) -> StainResult:
        """Run PatchCore inference on annular-masked cone crop.

        Steps:
            1. Create annular mask (donut: tube hole + corners = black)
            2. Apply mask to cone crop
            3. Resize to model input size
            4. Run PatchCore inference
            5. Resize heatmap back to original, mask invalid regions
            6. Score = max anomaly in valid (yarn surface) region
        """
        h, w = image.shape[:2]

        # Step 1-2: Create and apply annular mask
        mask = _make_annular_mask((h, w), center, inner_r, outer_r)
        masked_image = image.copy()
        masked_image[mask == 0] = 0

        # Step 3: Resize for inference
        resized = cv2.resize(masked_image, self.input_size)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

        # Step 4: Run PatchCore — raw forward pass, normalization disabled at load
        import torch
        with torch.no_grad():
            inp = torch.from_numpy(rgb).permute(2, 0, 1).float().unsqueeze(0) / 255.0
            inp = inp.to(self._device)
            output = self._model(inp)

        # Step 5: Extract raw anomaly map (unnormalized patch distances)
        raw_map = getattr(output, "anomaly_map", None)
        if raw_map is None and isinstance(output, dict):
            raw_map = output.get("anomaly_map")
        heatmap = None

        if raw_map is not None:
            if hasattr(raw_map, "cpu"):
                raw_map = raw_map.cpu().numpy()
            if raw_map.ndim == 4:
                raw_map = raw_map[0, 0]
            elif raw_map.ndim == 3:
                raw_map = raw_map[0]
            # Resize heatmap back to original cone crop size
            heatmap = cv2.resize(raw_map.astype(np.float32), (w, h))

            # Mask out invalid regions (tube + background)
            heatmap[mask == 0] = 0.0

            # Step 6: Score from valid (yarn surface) region only
            valid_pixels = heatmap[mask > 0]
            score = float(valid_pixels.max()) if len(valid_pixels) > 0 else 0.0
        else:
            score = 0.0

        has_stain = score > self.threshold

        logger.info(
            "PatchCore: score=%.4f, threshold=%.4f, stain=%s",
            score, self.threshold, "YES" if has_stain else "NO",
        )

        return StainResult(
            anomaly_score=score,
            has_stain=has_stain,
            heatmap=heatmap,
        )

    def _detect_fallback(
        self,
        image: np.ndarray,
        center: tuple,
        inner_r: float,
        outer_r: float,
    ) -> StainResult:
        """Fallback stain detection using HSV thresholding.

        Used when PatchCore model is not available.
        """
        h, w = image.shape[:2]
        mask = _make_annular_mask((h, w), center, inner_r, outer_r)

        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        v_channel = hsv[:, :, 2]

        _, dark_mask = cv2.threshold(v_channel, 80, 255, cv2.THRESH_BINARY_INV)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        dark_mask = cv2.morphologyEx(dark_mask, cv2.MORPH_OPEN, kernel)
        dark_mask = cv2.morphologyEx(dark_mask, cv2.MORPH_CLOSE, kernel)
        dark_mask = cv2.bitwise_and(dark_mask, mask)

        total_pixels = cv2.countNonZero(mask)
        defect_pixels = cv2.countNonZero(dark_mask)
        anomaly_score = defect_pixels / total_pixels if total_pixels > 0 else 0.0

        heatmap = dark_mask.astype(np.float32) / 255.0
        has_stain = anomaly_score > self.threshold

        logger.info(
            "Fallback stain: score=%.3f, threshold=%.3f, stain=%s",
            anomaly_score, self.threshold, "YES" if has_stain else "NO",
        )

        return StainResult(
            anomaly_score=anomaly_score,
            has_stain=has_stain,
            heatmap=heatmap,
        )
    