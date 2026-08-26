"""
Visualization — draws inspection results onto a single output frame.

Combines bounding boxes, dimension annotations, and stain heatmap
into one composite image for the UI / operator display.

Usage:
    annotated = draw_inspection_result(
        frame, detections, dimension_result, stain_result, cone_crop
    )
    cv2.imshow("Inspection", annotated)
"""

import cv2
import numpy as np

from .data_types import (
    Detection,
    DimensionResult,
    InspectionResult,
    StainResult,
    TubePatternResult,
)

# Colors (BGR)
_GREEN = (0, 200, 0)
_RED = (0, 0, 220)
_YELLOW = (0, 220, 220)
_WHITE = (255, 255, 255)
_BLACK = (0, 0, 0)
_CYAN = (220, 220, 0)

_FONT = cv2.FONT_HERSHEY_SIMPLEX
_FONT_SCALE = 0.55
_THICKNESS = 2


def draw_inspection_result(result: InspectionResult) -> np.ndarray:
    """Draw complete inspection results onto the frame.

    Produces a single composite image with:
        - Left: Full frame with bounding boxes and dimension labels
        - Right-top: Cone crop with stain heatmap overlay
        - Right-bottom: Pass/Fail status panel with dimension table

    Args:
        result: Complete InspectionResult from VisibleInspection.

    Returns:
        Annotated BGR image ready for display.
    """
    if result.annotated_frame is None:
        return np.zeros((480, 640, 3), dtype=np.uint8)

    frame = result.annotated_frame.copy()

    # Draw bounding boxes and class labels
    for det in result.detections:
        _draw_detection(frame, det)

    # Draw dimension annotations on the frame
    if result.dimension_result is not None:
        cone_det = None
        tube_det = None
        for det in result.detections:
            if det.class_name == "yarn_cone":
                cone_det = det
            elif det.class_name == "yarn_tube":
                tube_det = det
        if cone_det is not None:
            _draw_cone_dimensions(frame, cone_det, result.dimension_result)
        if tube_det is not None:
            _draw_tube_dimensions(frame, tube_det, result.dimension_result)

    # Build the composite output
    composite = _build_composite(
        frame, result.cone_crop, result.tube_crop,
        result.stain_result, result.dimension_result,
        result.tube_pattern_result, result,
    )

    return composite


def _draw_detection(frame: np.ndarray, det: Detection):
    """Draw a bounding box with class label and confidence."""
    x1, y1, x2, y2 = det.bbox
    color = _GREEN if det.class_name == "yarn_cone" else _CYAN
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

    label = f"{det.class_name} {det.confidence:.2f}"
    (tw, th), _ = cv2.getTextSize(label, _FONT, _FONT_SCALE, 1)
    cv2.rectangle(frame, (x1, y1 - th - 8), (x1 + tw + 4, y1), color, -1)
    cv2.putText(frame, label, (x1 + 2, y1 - 4), _FONT, _FONT_SCALE, _BLACK, 1)


def _draw_cone_dimensions(
    frame: np.ndarray, det: Detection, dim_result: DimensionResult
):
    """Draw cone outer diameter annotation at the widest point of the bounding box."""
    x1, y1, x2, y2 = det.bbox
    m = dim_result.measured

    if m.cone_diameter_mm <= 0:
        return

    # Outer diameter label at the widest point (mid-height)
    mid_y = (y1 + y2) // 2
    d_label = f"Cone: {m.cone_diameter_mm}mm"
    d_color = _GREEN if dim_result.cone_diameter_match else _RED

    # Horizontal arrows showing the width
    cv2.arrowedLine(frame, (x1, mid_y), (x2, mid_y), _YELLOW, 2, tipLength=0.03)
    cv2.arrowedLine(frame, (x2, mid_y), (x1, mid_y), _YELLOW, 2, tipLength=0.03)
    cv2.putText(frame, d_label, (x1, mid_y - 10), _FONT, _FONT_SCALE, d_color, _THICKNESS)


def _draw_tube_dimensions(
    frame: np.ndarray, det: Detection, dim_result: DimensionResult
):
    """Draw tube outer diameter annotation on the tube bounding box."""
    x1, y1, x2, y2 = det.bbox
    m = dim_result.measured
    r = dim_result.reference

    if m.tube_diameter_mm <= 0 or r.tube_diameter_mm <= 0:
        return

    # Tube diameter label at the center
    mid_x = (x1 + x2) // 2
    mid_y = (y1 + y2) // 2
    d_label = f"Tube: {m.tube_diameter_mm}mm"
    d_color = _GREEN if dim_result.tube_diameter_match else _RED

    # Horizontal arrows showing the width
    cv2.arrowedLine(frame, (x1, mid_y), (x2, mid_y), _CYAN, 2, tipLength=0.05)
    cv2.arrowedLine(frame, (x2, mid_y), (x1, mid_y), _CYAN, 2, tipLength=0.05)
    cv2.putText(frame, d_label, (x1, y1 - 10), _FONT, _FONT_SCALE, d_color, _THICKNESS)


def _build_composite(
    frame: np.ndarray,
    cone_crop: np.ndarray | None,
    tube_crop: np.ndarray | None,
    stain_result: StainResult | None,
    dim_result: DimensionResult | None,
    tube_pattern_result: TubePatternResult | None,
    result: InspectionResult,
) -> np.ndarray:
    """Build a composite image: main frame (left) + info panels (right).

    Layout:
        +-------------------+------------------+
        |                   |  Cone + Heatmap  |
        |   Main Frame      +------------------+
        |   (with boxes     |  Tube ROI        |
        |    & dimensions)  +------------------+
        |                   |  Status Panel    |
        +-------------------+------------------+
    """
    h, w = frame.shape[:2]
    panel_w = max(300, w // 3)
    panel_h_top = h // 3
    panel_h_mid = h // 4
    panel_h_bot = h - panel_h_top - panel_h_mid

    # === Right-top: Cone crop with stain heatmap ===
    top_panel = np.zeros((panel_h_top, panel_w, 3), dtype=np.uint8)

    if cone_crop is not None and cone_crop.size > 0:
        crop_display = _resize_fit(cone_crop, panel_w - 20, panel_h_top - 30)

        if stain_result is not None and stain_result.heatmap is not None:
            crop_display = _overlay_heatmap(crop_display, stain_result.heatmap)

        ch, cw = crop_display.shape[:2]
        y_off = (panel_h_top - ch - 20) // 2 + 20
        x_off = (panel_w - cw) // 2
        top_panel[y_off : y_off + ch, x_off : x_off + cw] = crop_display

    cv2.putText(top_panel, "Cone ROI + Stain Map", (10, 16), _FONT, 0.5, _WHITE, 1)

    # === Right-middle: Tube crop ===
    mid_panel = np.zeros((panel_h_mid, panel_w, 3), dtype=np.uint8)

    if tube_crop is not None and tube_crop.size > 0:
        tube_display = _resize_fit(tube_crop, panel_w - 20, panel_h_mid - 30)
        th, tw = tube_display.shape[:2]
        y_off = (panel_h_mid - th - 20) // 2 + 20
        x_off = (panel_w - tw) // 2
        mid_panel[y_off : y_off + th, x_off : x_off + tw] = tube_display

    cv2.putText(mid_panel, "Tube ROI", (10, 16), _FONT, 0.5, _WHITE, 1)

    # === Right-bottom: Status panel ===
    bot_panel = np.zeros((panel_h_bot, panel_w, 3), dtype=np.uint8)
    _draw_status_panel(bot_panel, result, dim_result, stain_result, tube_pattern_result)

    # === Assemble ===
    right_side = np.vstack([top_panel, mid_panel, bot_panel])
    if right_side.shape[0] != h:
        right_side = cv2.resize(right_side, (panel_w, h))

    composite = np.hstack([frame, right_side])
    return composite


def _overlay_heatmap(
    image: np.ndarray, heatmap: np.ndarray, alpha: float = 0.4
) -> np.ndarray:
    """Overlay an anomaly heatmap onto an image.

    Args:
        image: BGR image.
        heatmap: Float32 anomaly map (0.0 = normal, 1.0 = anomaly).
        alpha: Blend factor for the heatmap overlay.

    Returns:
        BGR image with heatmap overlay.
    """
    h, w = image.shape[:2]
    hm_resized = cv2.resize(heatmap, (w, h))

    # Normalize to 0-255
    hm_norm = np.clip(hm_resized, 0, 1)
    hm_uint8 = (hm_norm * 255).astype(np.uint8)

    # Apply colormap (red = high anomaly)
    hm_color = cv2.applyColorMap(hm_uint8, cv2.COLORMAP_JET)

    # Blend only where anomaly score is significant
    mask = hm_norm > 0.3
    blended = image.copy()
    blended[mask] = cv2.addWeighted(image, 1 - alpha, hm_color, alpha, 0)[mask]

    return blended


def _draw_status_panel(
    panel: np.ndarray,
    result: InspectionResult,
    dim_result: DimensionResult | None,
    stain_result: StainResult | None,
    tube_pattern_result: TubePatternResult | None = None,
):
    """Draw the pass/fail status and dimension table on the bottom panel."""
    h, w = panel.shape[:2]
    y = 25

    # Overall status
    passed = result.passed
    status_text = "PASS" if passed else "FAIL"
    status_color = _GREEN if passed else _RED
    cv2.putText(panel, f"Result: {status_text}", (10, y), _FONT, 0.7, status_color, 2)
    y += 22

    cv2.putText(panel, f"Material: {result.material_id}", (10, y), _FONT, 0.42, _WHITE, 1)
    y += 18

    cv2.putText(panel, f"PLC Code: {result.result_code}", (10, y), _FONT, 0.42, _WHITE, 1)
    y += 25

    # Dimension table (only if task is enabled)
    dim_enabled = result.tasks_enabled.get("dimension_check", True)
    if not dim_enabled:
        cv2.putText(panel, "Dimensions: DISABLED", (10, y), _FONT, 0.38, (128, 128, 128), 1)
        y += 18
    elif dim_result is not None:
        cv2.putText(panel, "Dimensions (mm):", (10, y), _FONT, 0.42, _YELLOW, 1)
        y += 18

        m = dim_result.measured
        r = dim_result.reference

        cv2.putText(panel, "          Meas   Ref   OK?", (10, y), _FONT, 0.35, _WHITE, 1)
        y += 16

        # Cone diameter
        if m.cone_diameter_mm > 0:
            color = _GREEN if dim_result.cone_diameter_match else _RED
            check = "Y" if dim_result.cone_diameter_match else "N"
            line = f"{'Cone':>7s}  {m.cone_diameter_mm:6.1f} {r.bottom_diameter_mm:6.1f}  {check}"
            cv2.putText(panel, line, (10, y), _FONT, 0.35, color, 1)
            y += 16

        # Tube diameter
        if m.tube_diameter_mm > 0 and r.tube_diameter_mm > 0:
            color = _GREEN if dim_result.tube_diameter_match else _RED
            check = "Y" if dim_result.tube_diameter_match else "N"
            line = f"{'Tube':>7s}  {m.tube_diameter_mm:6.1f} {r.tube_diameter_mm:6.1f}  {check}"
            cv2.putText(panel, line, (10, y), _FONT, 0.35, color, 1)
            y += 16

    y += 8

    # Stain info (only if task is enabled)
    stain_enabled = result.tasks_enabled.get("stain_detection", True)
    if not stain_enabled:
        cv2.putText(panel, "Stain: DISABLED", (10, y), _FONT, 0.38, (128, 128, 128), 1)
        y += 18
    elif stain_result is not None:
        stain_color = _RED if stain_result.has_stain else _GREEN
        stain_label = "STAIN DETECTED" if stain_result.has_stain else "No stain"
        cv2.putText(panel, f"Stain: {stain_label}", (10, y), _FONT, 0.42, stain_color, 1)
        y += 18
        cv2.putText(
            panel,
            f"Anomaly score: {stain_result.anomaly_score:.3f}",
            (10, y), _FONT, 0.35, _WHITE, 1,
        )
        y += 18

    y += 8

    # Tube pattern info (only if task is enabled)
    # Uses Nearest Neighbor classification (Color NN OR ResNet NN)
    tube_enabled = result.tasks_enabled.get("tube_pattern", True)
    if not tube_enabled:
        cv2.putText(panel, "Tube Pattern: DISABLED", (10, y), _FONT, 0.38, (128, 128, 128), 1)
    elif tube_pattern_result is not None:
        if not tube_pattern_result.reference_loaded:
            cv2.putText(panel, "Tube: NO REFERENCE", (10, y), _FONT, 0.42, _RED, 1)
        else:
            # Color NN result
            c_color = _GREEN if tube_pattern_result.color_match else _RED
            c_symbol = "✓" if tube_pattern_result.color_match else "✗"
            cv2.putText(
                panel,
                f"Color NN: {tube_pattern_result.color_nearest} {c_symbol}",
                (10, y), _FONT, 0.38, c_color, 1,
            )
            y += 16

            # ResNet NN result
            r_color = _GREEN if tube_pattern_result.resnet_match else _RED
            r_symbol = "✓" if tube_pattern_result.resnet_match else "✗"
            cv2.putText(
                panel,
                f"ResNet NN: {tube_pattern_result.resnet_nearest} {r_symbol}",
                (10, y), _FONT, 0.38, r_color, 1,
            )
            y += 16

            # Overall result (OR logic)
            overall_color = _GREEN if tube_pattern_result.passed else _RED
            overall_label = "PASS" if tube_pattern_result.passed else "FAIL"
            cv2.putText(
                panel,
                f"Tube: {overall_label} (expect: {tube_pattern_result.expected_class})",
                (10, y), _FONT, 0.38, overall_color, 1,
            )


def _resize_fit(image: np.ndarray, max_w: int, max_h: int) -> np.ndarray:
    """Resize image to fit within max_w x max_h while preserving aspect ratio."""
    h, w = image.shape[:2]
    scale = min(max_w / w, max_h / h)
    if scale >= 1.0:
        return image
    new_w, new_h = int(w * scale), int(h * scale)
    return cv2.resize(image, (new_w, new_h))
