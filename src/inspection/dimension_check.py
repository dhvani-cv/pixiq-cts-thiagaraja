"""
Dimension verification — converts bounding box pixels to millimeters
and compares cone and tube outer diameters against database reference specs.

Uses max inscribed circle in the YOLO bbox: diameter = min(width, height).
YOLO gives a tight bbox around the circular cone/tube, so the shorter side
is the true diameter — robust to slight positioning shifts between captures.

The calibration constant K (pixels per mm) is determined during camera
setup by imaging a known-size calibration target.

Usage:
    checker = DimensionChecker(pixels_per_mm=5.0)
    measured = checker.measure_both(cone_bbox, tube_bbox)
    result = checker.verify(measured, specs)
"""

import logging
from typing import Optional

from .data_types import Dimensions, DimensionResult, MaterialSpecs

logger = logging.getLogger(__name__)


class DimensionChecker:
    """Converts pixel measurements to mm and validates against reference specs.

    Measures both:
    - Cone outer diameter: from yarn_cone bbox width
    - Tube outer diameter: from yarn_tube bbox width
    """

    def __init__(self, pixels_per_mm: float = 5.0):
        """Initialize with calibration constant.

        Args:
            pixels_per_mm: Camera calibration factor K.
                Determined by: K = known_object_pixels / known_object_mm.
                Default 5.0 is a placeholder — must be calibrated per camera.
        """
        self.pixels_per_mm = pixels_per_mm
        logger.info(f"DimensionChecker initialized: K={pixels_per_mm} px/mm")

    def measure_cone(self, bbox: tuple) -> float:
        """Calculate cone outer diameter from bounding box.

        Uses max inscribed circle in the YOLO bbox: min(width, height).
        The YOLO bbox is tight around the circular cone, so the shorter
        side gives the true diameter — immune to slight positioning shifts.

        Args:
            bbox: (x1, y1, x2, y2) in pixels.

        Returns:
            Diameter in millimeters.
        """
        x1, y1, x2, y2 = bbox
        w_px, h_px = x2 - x1, y2 - y1
        diameter_px = min(w_px, h_px)
        diameter_mm = diameter_px / self.pixels_per_mm
        logger.debug("  Cone bbox: (%d,%d,%d,%d) w=%dpx h=%dpx → dia=%dpx → %.1fmm (K=%.1f)",
                     x1, y1, x2, y2, w_px, h_px, diameter_px, diameter_mm, self.pixels_per_mm)
        return round(diameter_mm, 1)

    def measure_tube(self, bbox: tuple) -> float:
        """Calculate tube outer diameter from bounding box.

        Uses max inscribed circle in the YOLO bbox: min(width, height).
        The YOLO bbox is tight around the circular tube hole, so the
        shorter side gives the true diameter.

        Args:
            bbox: (x1, y1, x2, y2) in pixels.

        Returns:
            Diameter in millimeters.
        """
        x1, y1, x2, y2 = bbox
        w_px, h_px = x2 - x1, y2 - y1
        diameter_px = min(w_px, h_px)
        diameter_mm = diameter_px / self.pixels_per_mm
        logger.debug("  Tube bbox: (%d,%d,%d,%d) w=%dpx h=%dpx → dia=%dpx → %.1fmm (K=%.1f)",
                     x1, y1, x2, y2, w_px, h_px, diameter_px, diameter_mm, self.pixels_per_mm)
        return round(diameter_mm, 1)

    def measure_both(
        self,
        cone_bbox: Optional[tuple] = None,
        tube_bbox: Optional[tuple] = None,
    ) -> Dimensions:
        """Measure both cone and tube outer diameters.

        Args:
            cone_bbox: Cone (x1, y1, x2, y2) in pixels. None if not detected.
            tube_bbox: Tube (x1, y1, x2, y2) in pixels. None if not detected.

        Returns:
            Dimensions with both measurements (0.0 if not available).
        """
        cone_dia = self.measure_cone(cone_bbox) if cone_bbox else 0.0
        tube_dia = self.measure_tube(tube_bbox) if tube_bbox else 0.0

        dims = Dimensions(
            cone_diameter_mm=cone_dia,
            tube_diameter_mm=tube_dia,
        )

        logger.info(f"Measured: cone_D={dims.cone_diameter_mm}mm, tube_D={dims.tube_diameter_mm}mm")
        return dims

    def verify(self, measured: Dimensions, specs: MaterialSpecs) -> DimensionResult:
        """Compare measured diameters against reference specs.

        Rejection happens only if measured diameter is outside spec ± tolerance.
        Each dimension has its own tolerance; falls back to specs.tolerance_mm
        if the per-dimension tolerance is 0.

        Args:
            measured: Dimensions from measure_both().
            specs: Reference dimensions from the database.

        Returns:
            DimensionResult with match booleans for each dimension.
        """
        # Per-dimension tolerances (fall back to single tolerance_mm if 0)
        cone_tol = specs.cone_tolerance_mm if specs.cone_tolerance_mm > 0 else specs.tolerance_mm
        tube_tol = specs.tube_tolerance_mm if specs.tube_tolerance_mm > 0 else specs.tolerance_mm
        logger.debug("Dimension verify: cone_tol=%.1fmm tube_tol=%.1fmm (fallback=%.1fmm)",
                     cone_tol, tube_tol, specs.tolerance_mm)

        # Cone diameter check: reject only if outside spec ± tolerance
        if measured.cone_diameter_mm > 0 and specs.bottom_diameter_mm > 0:
            cone_match = abs(measured.cone_diameter_mm - specs.bottom_diameter_mm) <= cone_tol
            logger.debug("  Cone: measured=%.1fmm ref=%.1fmm diff=%.1fmm tol=%.1fmm → %s",
                         measured.cone_diameter_mm, specs.bottom_diameter_mm,
                         abs(measured.cone_diameter_mm - specs.bottom_diameter_mm),
                         cone_tol, "OK" if cone_match else "MISMATCH")
        else:
            # If not measured or not specified, treat as OK
            cone_match = True

        # Tube diameter check: reject only if outside spec ± tolerance
        if measured.tube_diameter_mm > 0 and specs.tube_diameter_mm > 0:
            tube_match = abs(measured.tube_diameter_mm - specs.tube_diameter_mm) <= tube_tol
            logger.debug("  Tube: measured=%.1fmm ref=%.1fmm diff=%.1fmm tol=%.1fmm → %s",
                         measured.tube_diameter_mm, specs.tube_diameter_mm,
                         abs(measured.tube_diameter_mm - specs.tube_diameter_mm),
                         tube_tol, "OK" if tube_match else "MISMATCH")
        else:
            # If not measured or not specified in DB, treat as OK
            tube_match = True

        result = DimensionResult(
            measured=measured,
            reference=specs,
            cone_diameter_match=cone_match,
            tube_diameter_match=tube_match,
        )

        if result.all_match:
            logger.info(f"Dimensions OK for {specs.material_id}")
        else:
            parts = []
            if not cone_match:
                parts.append(
                    f"cone: {measured.cone_diameter_mm}mm vs {specs.bottom_diameter_mm}±{cone_tol}mm"
                )
            if not tube_match:
                parts.append(
                    f"tube: {measured.tube_diameter_mm}mm vs {specs.tube_diameter_mm}±{tube_tol}mm"
                )
            logger.warning(
                f"Dimension MISMATCH for {specs.material_id}: "
                + ", ".join(parts)
            )

        return result
