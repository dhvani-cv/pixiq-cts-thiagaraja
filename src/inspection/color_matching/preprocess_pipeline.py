from .bilateral_filter import apply_bilateral_filter
from .convert_lab import convert_to_lab
from .unrolled import unroll_cone_tip
from .crop_sweet_spot import crop_polar_sweet_spot


def preprocess_cone_tip(bgr_image, center, radius,
                        inner_crop_pct=0.10, outer_crop_pct=0.10,
                        bilateral_d=9, bilateral_sigma_color=75,
                        bilateral_sigma_space=75):
    """
    Full preprocessing pipeline: Filter -> LAB -> Polar Warp -> Crop

    Args:
        bgr_image: Input BGR image (cropped to cone region)
        center: (cx, cy) tuple for cone tip center
        radius: Radius of the circular ROI
        inner_crop_pct: Percentage of inner region to discard
        outer_crop_pct: Percentage of outer region to discard
        bilateral_*: Bilateral filter parameters

    Returns:
        lab_patch: Cropped LAB patch ready for signature extraction
    """
    # Step 1: Bilateral filter (on BGR)
    filtered = apply_bilateral_filter(
        bgr_image, bilateral_d, bilateral_sigma_color, bilateral_sigma_space
    )

    # Re-apply mask: bilateral bleeds color into black regions (hole/corners).
    # Force them back to zero so polar unwrap + find_object can cleanly separate.
    mask = (bgr_image > 0).any(axis=2)
    filtered[~mask] = 0

    # Step 2: Convert to CIELAB
    lab = convert_to_lab(filtered)

    # Step 3: Polar warp
    polar = unroll_cone_tip(lab, center, radius)

    # Step 4: Crop the sweet spot
    lab_patch = crop_polar_sweet_spot(polar, inner_crop_pct, outer_crop_pct)

    return lab_patch
