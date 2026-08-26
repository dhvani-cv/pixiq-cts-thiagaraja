"""
Data types for the visible image inspection module.
"""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass(slots=True)
class MaterialSpecs:
    """Reference dimensions fetched from the database for a material ID."""
    material_id: str
    height_mm: float
    top_diameter_mm: float
    bottom_diameter_mm: float       # Cone outer diameter
    tube_diameter_mm: float = 0.0   # Tube outer diameter (0 = not specified)
    tolerance_mm: float = 2.0       # Legacy single tolerance (fallback)
    cone_tolerance_mm: float = 0.0  # Cone-specific tolerance (0 = use tolerance_mm)
    tube_tolerance_mm: float = 0.0  # Tube-specific tolerance (0 = use tolerance_mm)
    master_name: str = ""           # Tube pattern class name (from master.json)


@dataclass(slots=True)
class Detection:
    """Single YOLO detection result."""
    class_name: str        # "yarn_cone" or "yarn_tube"
    bbox: tuple            # (x1, y1, x2, y2) in pixels
    confidence: float


@dataclass(slots=True)
class Dimensions:
    """Measured dimensions in millimeters."""
    cone_diameter_mm: float = 0.0   # Cone outer diameter
    tube_diameter_mm: float = 0.0   # Tube outer diameter


@dataclass(slots=True)
class DimensionResult:
    """Result of dimension verification against reference specs."""
    measured: Dimensions
    reference: MaterialSpecs
    cone_diameter_match: bool = True
    tube_diameter_match: bool = True

    @property
    def all_match(self) -> bool:
        """Both cone and tube diameters must match (if specified)."""
        return self.cone_diameter_match and self.tube_diameter_match


@dataclass(slots=True)
class StainResult:
    """Result of PatchCore anomaly detection."""
    anomaly_score: float
    has_stain: bool
    heatmap: Optional[np.ndarray] = None   # HxW float32 anomaly map


@dataclass(slots=True)
class TubePatternResult:
    """Result of tube pattern verification using Nearest Neighbor classification.

    Decision logic: Combined distance (Color + FFT) finds nearest class.
    combined_distance = color_weight * bhatt + (1 - color_weight) * fft_cosine

    Color provides dominant signal (99.85% production accuracy).
    FFT adds shift-invariant spatial discrimination for same-color patterns.
    ResNet NN runs for monitoring/logging only — does not affect pass/fail.
    """
    color_nearest: str           # Class with minimum Bhattacharyya distance
    color_distance: float        # Bhatt distance to nearest class
    color_match: bool            # True if combined_nearest == expected_class (+ gates)
    resnet_nearest: str          # Class with minimum cosine distance (monitoring only)
    resnet_distance: float       # Distance to nearest class (monitoring only)
    resnet_match: bool           # True if resnet_nearest == expected_class (monitoring only)
    expected_class: str = ""
    reference_loaded: bool = True
    combined_nearest: str = ""   # Class with minimum combined distance
    combined_distance: float = 0.0  # Weighted bhatt + fft cosine distance
    fft_distance: float = 0.0   # FFT cosine distance to combined_nearest

    @property
    def passed(self) -> bool:
        """Combined (Color+FFT) decides. ResNet is monitoring only."""
        return self.reference_loaded and self.color_match


@dataclass(slots=True)
class UVResult:
    """Result of UV polymer mixup detection via radial log(G/B) profile analysis.

    UV illumination causes yarn to fluoresce. Different polymers have different
    fluorescence responses (chemistry-based). A mixed polymer wound in layers
    creates concentric bands of different fluorescence — visible as a local dip
    in the radial log(G/B) profile from tube (inner) to cone edge (outer).

    Detection:
        Compute radial log(G/B) profile (100 bins, tube→outer edge).
        Fit degree-2 polynomial baseline (captures natural radial gradient).
        max_dip = max negative deviation from baseline.
        has_mixup = max_dip > radial_dip_threshold (default 0.024).

    Validated on 1950 good + 9 polymer-mixup defect images (excluding outer-band
    borderline cases 4037/4374):
        Good:   max_dip p99 = 0.0195
        Defect: max_dip p1  = 0.0374
        Clean separation gap = +0.018 in log(G/B) domain.

    Scope: detects POLYMER mixup only (chemistry/fluorescence difference).
    Appearance defects (wrong color dye, fading) are NOT UV's responsibility —
    handled by visible light inspection.

    Result code mapping (done by caller):
        has_mixup == True  → 2 (Defect — polymer mixup detected)
        has_mixup == False → 1 (Good)
    """
    has_mixup: bool
    radial_dip: float = 0.0         # Max negative dip in radial log(G/B) profile (defect > 0.024)
    gb_ratio: float = 0.0           # Mean G/B across annular pixels (logged for monitoring)
    detection_failed: bool = False  # True = YOLO/compute failed → caller treats as skip (uv_code=None)
    cone_bbox: Optional[tuple] = None  # (x1, y1, x2, y2) of cone in UV frame — for stream crop


@dataclass(slots=True)
class TailResult:
    """Result of yarn tail detection via YOLO.

    A properly wound cone should have a visible yarn tail.
    tail_detected=True means the tail is present (Good).
    tail_detected=False means the tail is missing (Defect).

    Result code mapping (done by caller):
        model_loaded == False  → 3 (Error)
        tail_detected == False → 2 (Defect — missing tail)
        tail_detected == True  → 1 (Good)
    """
    tail_detected: bool
    confidence: float                    # YOLO detection confidence (0 if not detected)
    bbox: Optional[tuple] = None         # (x1, y1, x2, y2) if detected
    model_loaded: bool = True


@dataclass(slots=True)
class InspectionResult:
    """Complete inspection result for one frame."""
    material_id: str
    detections: list = field(default_factory=list)        # list[Detection]
    dimension_result: Optional[DimensionResult] = None
    stain_result: Optional[StainResult] = None
    tube_pattern_result: Optional[TubePatternResult] = None
    cone_crop: Optional[np.ndarray] = None
    tube_crop: Optional[np.ndarray] = None
    annotated_frame: Optional[np.ndarray] = None
    material_not_found: bool = False                       # True if material_id has no DB specs/templates
    tasks_enabled: dict = field(default_factory=lambda: {
        "dimension_check": True,
        "stain_detection": True,
        "tube_pattern": True,
    })

    @property
    def passed(self) -> bool:
        """Overall pass based on enabled tasks only.

        Disabled tasks are treated as OK (not checked = not failed).
        If material_id not found in DB, always fails.
        """
        if self.material_not_found:
            return False

        # Dimension check: OK if disabled, else must match
        if self.tasks_enabled.get("dimension_check", True):
            dims_ok = self.dimension_result.all_match if self.dimension_result else False
        else:
            dims_ok = True

        # Stain detection: OK if disabled, else must have no stain
        if self.tasks_enabled.get("stain_detection", True):
            stain_ok = not self.stain_result.has_stain if self.stain_result else False
        else:
            stain_ok = True

        # Tube pattern: OK if disabled, else must pass both checks
        if self.tasks_enabled.get("tube_pattern", True):
            tube_ok = self.tube_pattern_result.passed if self.tube_pattern_result else False
        else:
            tube_ok = True

        return dims_ok and stain_ok and tube_ok

    @property
    def result_code(self) -> int:
        """PLC result code: 1=Good, 2=Defect, 3=Error.

        material_not_found → 2 (Defect) with defect_type=7.
        Error (3) only if an enabled task has no result (incomplete).
        Disabled tasks with None results are not treated as errors.
        """
        # Material not found → Defect (not Error — cone should be ejected)
        if self.material_not_found:
            return 2

        # Check if any enabled task is missing its result
        if self.tasks_enabled.get("dimension_check", True) and self.dimension_result is None:
            return 3  # Error — dimension check enabled but no result
        if self.tasks_enabled.get("stain_detection", True) and self.stain_result is None:
            return 3  # Error — stain detection enabled but no result
        if self.tasks_enabled.get("tube_pattern", True) and self.tube_pattern_result is None:
            return 3  # Error — tube pattern enabled but no result

        # Must have at least one detection
        if not self.detections:
            return 3

        if self.passed:
            return 1  # Good
        return 2      # Defect
