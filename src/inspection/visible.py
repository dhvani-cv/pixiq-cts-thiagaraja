"""
Visible Image Process Module — master orchestrator for visible-light inspection.

This is the main entry point for Module 3 of the Sieger v2 pipeline.
It coordinates:
    1. YOLO12 object detection (yarn_cone, yarn_tube)
    2. Extract Cone ROI + Tube ROI
    3. Dimension verification (outer diameter only, compare vs DB specs)
    4. PatchCore anomaly detection for stain/defect inspection
    5. Tube pattern verification (Color+FFT NN classification, ResNet monitoring)
    6. Visualization of all results on a single composite frame

Tube Pattern Matching uses Nearest Neighbor classification:
    - Color NN: Bhattacharyya distance on LAB a*b* histogram (dominant signal)
    - FFT NN: Cosine distance on 1D FFT magnitude (shift-invariant spatial)
    - Combined: weighted sum finds nearest class (fft_weight=0.3)
    - ResNet NN: Monitoring/logging only (does NOT affect pass/fail)

Performance:
    - Color NN alone: 99.85% production accuracy
    - Color+FFT: 100% on master data, +40% margin improvement

Individual tasks can be enabled/disabled per customer via the "tasks"
key in config.json. YOLO detection always runs (it's the core); the
optional tasks are dimension_check, stain_detection, tube_pattern,
and uv_inspection.

Usage:
    inspector = VisibleInspection(config)
    result = inspector.process_frame(vl_image, material_id="MAT-001")
    if result.result_code == 2:
        signal_plc("DEFECT")

Configuration (in config.json):
    "inspection": {
        "weights": {
            "visible": "weights/visible_yolo.pt",
            "uv": "weights/uv_yolo.pt"
        },
        "patchcore_model": "models/patchcore_yarn_cone",
        "database": "materials.db",
        "pixels_per_mm": 5.0,
        "yolo_conf": 0.6,
        "stain_threshold": 0.5,
        "tasks": {
            "dimension_check": true,
            "stain_detection": true,
            "tube_pattern": true,
            "uv_inspection": false
        },
        "tube_pattern": {
            "template_dir": "templates/tube",
            "bilateral_d": 9,
            "bilateral_sigma_color": 75,
            "bilateral_sigma_space": 75,
            "inner_crop_pct": 0.10,
            "outer_crop_pct": 0.10
        }
    }
"""

import logging
from typing import Optional

import numpy as np

from .yolo_detector import YOLODetector
from .dimension_check import DimensionChecker
from .stain_detector import StainDetector
from .tube_pattern import TubePatternMatcher
from .visualization import draw_inspection_result
from .data_types import InspectionResult
from .recipe_store import RecipeStore

logger = logging.getLogger(__name__)


class VisibleInspection:
    """Master orchestrator for visible-light image inspection.

    Owns all sub-modules (YOLO, dimensions, PatchCore, DB) and
    exposes a single process_frame() method that returns a complete
    InspectionResult.

    Sub-modules are only initialized if their task is enabled in config.
    YOLO detection always runs — it provides the core yarn_cone/yarn_tube
    bounding boxes needed by all downstream tasks.
    """

    def __init__(self, config: dict):
        """Initialize inspection sub-modules based on enabled tasks.

        Args:
            config: The "inspection" section of config.json.
                Required keys:
                    weights.visible (str): Path to visible YOLO12 weights.
                    database (str): Path to SQLite DB file.
                Optional keys:
                    weights.uv (str): Path to UV YOLO weights (for future UV module).
                    patchcore_model (str): Path to PatchCore model dir.
                    pixels_per_mm (float): Calibration constant (default 5.0).
                    yolo_conf (float): YOLO confidence threshold (default 0.6).
                    stain_threshold (float): Anomaly score threshold (default 0.5).
                    tasks (dict): Per-task enable/disable flags.
        """
        # Read task flags (default: all enabled)
        tasks = config.get("tasks", {})
        self.tasks_enabled = {
            "dimension_check": tasks.get("dimension_check", True),
            "stain_detection": tasks.get("stain_detection", True),
            "tube_pattern": tasks.get("tube_pattern", True),
            "uv_inspection": tasks.get("uv_inspection", False),
        }

        enabled = [k for k, v in self.tasks_enabled.items() if v]
        disabled = [k for k, v in self.tasks_enabled.items() if not v]
        logger.info(f"Tasks ENABLED: {enabled}")
        if disabled:
            logger.info(f"Tasks DISABLED: {disabled}")
        self.dimension_outer_class = str(config.get("dimension_outer_class", "surface")).lower()

        # YOLO detector — always needed (core detection)
        weights = config.get("weights", {})
        visible_model = weights.get("visible", config.get("yolo_model", ""))
        self.detector = YOLODetector(
            model_path=visible_model,
            conf_threshold=config.get("yolo_conf", 0.6),
        )

        # Recipe store — needed for material-wise dimension specs and tube class lookup.
        self.db = None
        if self.tasks_enabled["dimension_check"] or self.tasks_enabled["tube_pattern"]:
            self.db = RecipeStore(config.get("recipe_dir", "data/recipes"))

        # Dimension checker — only if enabled
        self.dim_checker = None
        if self.tasks_enabled["dimension_check"]:
            self.dim_checker = DimensionChecker(
                pixels_per_mm=config.get("pixels_per_mm", 5.0),
            )

        # Stain detector — only if enabled
        self.stain_detector = None
        if self.tasks_enabled["stain_detection"]:
            self.stain_detector = StainDetector(
                model_path=config.get("patchcore_model", ""),
                threshold=config.get("stain_threshold", 0.5),
            )

        # Tube pattern matcher — only if enabled. Verification-only: compares
        # the live crop against the ONE expected material's template, never
        # against other materials (no classification).
        self.tube_matcher = None
        self.tube_inner_ratio = 0.80  # hole_dia / tube_outer_dia
        self.tube_exclude_clock_range = None
        if self.tasks_enabled["tube_pattern"]:
            tube_cfg = config.get("tube_pattern", {})
            self.tube_inner_ratio = tube_cfg.get("inner_ratio", 0.80)
            # Fixed camera/mounting offset makes one sector of the tube ring
            # unreliable — exclude it from the annular crop before matching.
            # Given as clock-face minutes (0-60, 0=12 o'clock/top, clockwise).
            exclude_cfg = tube_cfg.get("exclude_clock_range")
            exclude_enabled = tube_cfg.get("exclude_clock_range_enabled", False)
            self.tube_exclude_clock_range = (
                tuple(exclude_cfg) if exclude_enabled and exclude_cfg else None
            )
            self.tube_matcher = TubePatternMatcher(
                template_dir=tube_cfg.get("template_dir", "templates/tube"),
                bilateral_d=tube_cfg.get("bilateral_d", 9),
                bilateral_sigma_color=tube_cfg.get("bilateral_sigma_color", 75),
                bilateral_sigma_space=tube_cfg.get("bilateral_sigma_space", 75),
                inner_crop_pct=tube_cfg.get("inner_crop_pct", 0.10),
                outer_crop_pct=tube_cfg.get("outer_crop_pct", 0.10),
                inner_ratio=self.tube_inner_ratio,
                fft_weight=tube_cfg.get("fft_weight", 0.3),
                threshold_config=tube_cfg.get("threshold_config", ""),
                default_threshold=tube_cfg.get("default_threshold", 0.25),
                pattern_verifier_model_path=tube_cfg.get("pattern_verifier_model_path", ""),
            )
            # Load all per-material templates (verification-only, expected-template match)
            n_templates = self.tube_matcher.load_all_references()
            logger.info(f"Tube pattern: loaded {n_templates} templates (verification-only)")
            logger.info(f"Tube inner_ratio={self.tube_inner_ratio} (inner hole masked)")

        logger.info("VisibleInspection initialized")

    def close(self):
        """Release resources."""
        if self.db is not None:
            self.db.close()
        logger.info("VisibleInspection closed")

    def process_frame(
        self,
        frame: np.ndarray,
        material_id: str,
        tasks_override: dict | None = None,
    ) -> InspectionResult:
        """Run the visible-light inspection pipeline on one frame.

        Flow: YOLO → extract Cone + Tube ROIs → dimension check →
        stain check → tube pattern check. Each step runs independently
        if its ROI is available and the task is enabled. A missing cone
        does not prevent the tube pattern check from running.

        Args:
            frame: BGR visible-light image from the VL camera.
            material_id: PLC-supplied material identifier for DB lookup.
            tasks_override: Optional per-call task flags (e.g. per-material
                overrides). Merged over the init-time global flags. Can only
                effectively disable — a task whose sub-module was not
                initialized (globally off at startup) stays skipped even if
                overridden to True.

        Returns:
            InspectionResult with results for enabled tasks only.
        """
        tasks = dict(self.tasks_enabled)
        if tasks_override:
            tasks.update({k: bool(v) for k, v in tasks_override.items() if k in tasks})

        result = InspectionResult(
            material_id=material_id,
            annotated_frame=None,
            tasks_enabled=tasks.copy(),
        )

        try:
            return self._process_frame_inner(result, frame, material_id, tasks)
        except Exception:
            logger.exception(
                "Unexpected error in process_frame for material '%s' — returning result_code=3",
                material_id,
            )
            return result  # result_code=3 because all task results are None

    def _process_frame_inner(
        self,
        result: InspectionResult,
        frame: np.ndarray,
        material_id: str,
        tasks: dict | None = None,
    ) -> InspectionResult:
        """Inner implementation of process_frame — wrapped by top-level exception handler."""
        if tasks is None:
            tasks = self.tasks_enabled

        # --- Step 1: YOLO detection (always runs) ---
        logger.debug("Step 1: YOLO detection on frame %dx%d", frame.shape[1], frame.shape[0])
        detections = self.detector.detect(frame)
        result.detections = detections

        if not detections:
            logger.warning("No objects detected — skipping inspection")
            return result

        for det in detections:
            logger.debug(
                "  Detection: %s  bbox=(%d,%d,%d,%d)  conf=%.3f  size=%dx%d",
                det.class_name, *det.bbox, det.confidence,
                det.bbox[2] - det.bbox[0], det.bbox[3] - det.bbox[1],
            )

        # Fetch material-specific specs using the PLC material number.
        specs = None
        if self.db is not None:
            logger.debug("Step 1b: Looking up specs for material_id='%s'", material_id)
            specs = self.db.get_material_specs(material_id)
            if specs is None:
                logger.error("No specs for material '%s' — marking as defect", material_id)
                result.material_not_found = True
                return result
            logger.debug(
                "  Specs found: cone_dia=%.1fmm tube_dia=%.1fmm cone_tol=%.1f tube_tol=%.1f master=%s",
                specs.bottom_diameter_mm, specs.tube_diameter_mm,
                specs.cone_tolerance_mm, specs.tube_tolerance_mm,
                specs.master_name,
            )

        # --- Step 2: Extract Cone ROI + Tube ROI ---
        logger.debug("Step 2: Extracting ROIs")
        cone_det = self.detector.get_detection_by_class(detections, "yarn_cone")
        tube_det = self.detector.get_detection_by_class(detections, "yarn_tube")
        surface_det = self.detector.get_detection_by_class(detections, "surface")

        # Rectangular crops (for dimension check which needs background contrast)
        cone_crop = self.detector.extract_roi(frame, cone_det) if cone_det else None
        tube_crop = self.detector.extract_roi(frame, tube_det) if tube_det else None

        if cone_crop is not None:
            logger.debug("  Cone crop: %dx%d", cone_crop.shape[1], cone_crop.shape[0])
        if tube_crop is not None:
            logger.debug("  Tube crop: %dx%d", tube_crop.shape[1], tube_crop.shape[0])

        # Circular-masked crops
        # Tube pattern: annular mask on TUBE bbox — zeros out outer corners
        # AND inner hole. inner_ratio = hole_dia / tube_outer_dia (from config).
        tube_masked = (
            self.detector.extract_annular_roi(
                frame, tube_det, inner_ratio=self.tube_inner_ratio,
                exclude_clock_range=self.tube_exclude_clock_range,
            )
            if tube_det else None
        )

        if tube_masked is not None:
            logger.debug("  Tube annular crop: %dx%d (inner_ratio=%.2f)", tube_masked.shape[1], tube_masked.shape[0], self.tube_inner_ratio)

        result.cone_crop = cone_crop
        result.tube_crop = tube_crop

        if cone_det is None and surface_det is None:
            logger.warning("yarn_cone/surface not detected")
        if tube_det is None:
            logger.warning("yarn_tube not detected")

        # --- Step 3: Dimension check (cone + tube outer diameters) ---
        # Measures both cone and tube diameters from their respective bboxes.
        if tasks["dimension_check"] and self.dim_checker is not None and specs is not None:
            logger.debug("Step 3: Dimension check")
            # Prefer surface bbox for cone diameter when configured, falling
            # back to the original cone bbox if surface is unavailable.
            dimension_outer_det = cone_det
            if self.dimension_outer_class == "surface":
                dimension_outer_det = surface_det or cone_det
                if surface_det is not None:
                    logger.debug("  Dimension cone diameter source: surface bbox")
                elif cone_det is not None:
                    logger.debug("  Dimension cone diameter source: cone bbox fallback")
            else:
                logger.debug("  Dimension cone diameter source: cone bbox")

            cone_bbox = dimension_outer_det.bbox if dimension_outer_det else None
            tube_bbox = tube_det.bbox if tube_det else None

            # Measure both diameters
            measured = self.dim_checker.measure_both(cone_bbox, tube_bbox)
            dim_result = self.dim_checker.verify(measured, specs)
            result.dimension_result = dim_result
        else:
            logger.debug("Step 3: Dimension check SKIPPED (enabled=%s, specs=%s)", tasks["dimension_check"], specs is not None)

        # --- Step 4: Stain detection (circular cone crop + annular mask) ---
        # PatchCore runs on the rectangular cone crop. An annular mask (donut)
        # restricts scoring to the yarn surface only — black corners and tube
        # hole are excluded. Any deviation from learned normal = defect.
        if tasks["stain_detection"] and self.stain_detector is not None and cone_crop is not None and cone_det and tube_det:
            logger.debug("Step 4: Stain detection (circular cone crop)")
            from .polar_unwarp import find_geometry
            center, inner_r, outer_r = find_geometry(cone_det.bbox, tube_det.bbox)
            logger.debug("  Cone crop: %dx%d, center=%s, inner_r=%.0f, outer_r=%.0f",
                         cone_crop.shape[1], cone_crop.shape[0], center, inner_r, outer_r)
            stain_result = self.stain_detector.detect(
                cone_crop, center=center, inner_r=inner_r, outer_r=outer_r,
            )
            result.stain_result = stain_result
        else:
            logger.debug("Step 4: Stain detection SKIPPED (enabled=%s, cone=%s, tube=%s)", tasks["stain_detection"], cone_det is not None, tube_det is not None)

        # --- Step 5: Tube pattern check (needs tube — independent of cone) ---
        # Uses circular-masked crop — no yarn leaking into color/ResNet features.
        # master_name is the tube pattern class (from master.json); falls back to material_id
        if tasks["tube_pattern"] and self.tube_matcher is not None and tube_masked is not None:
            tube_class = specs.master_name if specs and specs.master_name else material_id
            logger.debug("Step 5: Tube pattern check — expected class='%s'", tube_class)
            tube_result = self.tube_matcher.verify(tube_masked, tube_class)
            result.tube_pattern_result = tube_result
        else:
            logger.debug("Step 5: Tube pattern SKIPPED (enabled=%s, tube=%s)", tasks["tube_pattern"], tube_masked is not None)

        # --- Log overall result ---
        parts = []
        if result.dimension_result is not None:
            parts.append(f"dims={'OK' if result.dimension_result.all_match else 'MISMATCH'}")
        if result.stain_result is not None:
            parts.append(f"stain={'YES' if result.stain_result.has_stain else 'NO'}")
        if result.tube_pattern_result is not None:
            parts.append(f"tube={'OK' if result.tube_pattern_result.passed else 'FAIL'}")
        logger.info(
            f"Inspection complete for {material_id}: "
            + ", ".join(parts)
            + f", result_code={result.result_code}"
        )

        return result

    def process_frame_with_visualization(
        self,
        frame: np.ndarray,
        material_id: str,
    ) -> tuple[InspectionResult, np.ndarray]:
        """Run inspection and produce annotated visualization.

        Convenience method that calls process_frame() followed by
        draw_inspection_result().

        Args:
            frame: BGR visible-light image.
            material_id: PLC-supplied material ID.

        Returns:
            Tuple of (InspectionResult, annotated_composite_frame).
        """
        result = self.process_frame(frame, material_id)
        annotated = draw_inspection_result(result)
        return result, annotated
