"""
Polar unwrap of yarn cone surface to a flat texture strip.

Converts the circular cone view (from top-down camera) into a rectangular
texture strip using YOLO-detected bboxes for geometry. Used at inference
time by the stain detection pipeline to match the training data format.

See training/patchcore/unwarp.py for the original implementation and
detailed algorithm documentation.
"""

import cv2
import numpy as np


def find_geometry(
    cone_bbox: tuple,
    tube_bbox: tuple,
) -> tuple:
    """Derive cone geometry purely from YOLO bboxes.

    Args:
        cone_bbox: (x1, y1, x2, y2) of yarn_cone in full-frame pixels.
        tube_bbox: (x1, y1, x2, y2) of yarn_tube in full-frame pixels.

    Returns:
        (center, inner_r, outer_r) where center is (cx, cy) in cone-crop
        coordinates (relative to cone_bbox top-left corner).
    """
    cx1, cy1, cx2, cy2 = cone_bbox
    tx1, ty1, tx2, ty2 = tube_bbox

    # Tube center in cone-crop coordinates
    tube_cx = (tx1 + tx2) / 2 - cx1
    tube_cy = (ty1 + ty2) / 2 - cy1
    center = (int(tube_cx), int(tube_cy))

    # Inner radius = max inscribed circle in tube bbox
    tube_w = tx2 - tx1
    tube_h = ty2 - ty1
    inner_r = float(min(tube_w, tube_h)) / 2

    # Outer radius = max inscribed circle in cone bbox
    cone_w = cx2 - cx1
    cone_h = cy2 - cy1
    outer_r = float(min(cone_w, cone_h)) / 2

    return center, inner_r, outer_r


def unwarp_cone(
    image: np.ndarray,
    center: tuple,
    inner_r: float,
    outer_r: float,
    angular_res: int = 1024,
    radial_crop: float = 0.05,
) -> np.ndarray:
    """Polar-to-rectangular transform of the cone annulus.

    Args:
        image: BGR cone crop image.
        center: (cx, cy) center of the cone in crop coordinates.
        inner_r: Inner radius (tube hole boundary).
        outer_r: Outer radius (cone edge).
        angular_res: Number of angular steps (output height). Default 1024.
        radial_crop: Fraction to trim from each edge (default 0.05 = 5%).

    Returns:
        Flat texture strip (angular_res x radial_extent x 3), BGR.
    """
    radial_res = int(outer_r)

    unwrapped = cv2.warpPolar(
        image,
        dsize=(radial_res, angular_res),
        center=center,
        maxRadius=outer_r,
        flags=cv2.WARP_POLAR_LINEAR,
    )

    # Column range for the annulus
    col_inner = int(inner_r / outer_r * radial_res)
    col_outer = radial_res

    # Apply radial crop (trim both edges)
    annulus_width = col_outer - col_inner
    crop_px = int(annulus_width * radial_crop)
    col_inner += crop_px
    col_outer -= crop_px

    return unwrapped[:, col_inner:col_outer]
