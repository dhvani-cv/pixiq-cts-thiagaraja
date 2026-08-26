"""
Inspection package — visible-light image processing for Sieger v2.

Module 3 of the pipeline: YOLO12 detection, dimension verification,
PatchCore stain detection, tube pattern matching, and visualization.

Import policy (production rule):
    - __init__.py only re-exports lightweight data types (pure dataclasses,
      zero external dependencies).
    - Heavy modules (YOLO, PatchCore, ResNet, VisibleInspection) are NOT
      imported here. Import them directly from their submodule:

        from inspection.visible import VisibleInspection
        from inspection.yolo_detector import YOLODetector
        from inspection.stain_detector import StainDetector
        from inspection.tube_pattern import TubePatternMatcher
        from inspection.uv_inspection import UVInspection
        from inspection.tail_inspection import TailInspection
        from inspection.visualization import draw_inspection_result

    Rationale: eagerly loading YOLO/PatchCore/ResNet50 at package import
    time causes slow startup, risks circular imports during module
    initialization, and loads GPU models even when not needed.
"""

# Only lightweight data types — pure dataclasses, no external deps
from .data_types import (
    MaterialSpecs,
    Detection,
    Dimensions,
    DimensionResult,
    StainResult,
    TubePatternResult,
    UVResult,
    TailResult,
    InspectionResult,
)

__all__ = [
    "MaterialSpecs",
    "Detection",
    "Dimensions",
    "DimensionResult",
    "StainResult",
    "TubePatternResult",
    "UVResult",
    "TailResult",
    "InspectionResult",
]

# Suppress harmless matplotlib warnings caused by system/venv duplication on Jetson
import warnings
warnings.filterwarnings("ignore", message="Unable to import Axes3D")