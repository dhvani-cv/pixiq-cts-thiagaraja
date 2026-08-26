from .find_object import find_object


def crop_polar_sweet_spot(polar_image, inner_crop_pct=0.10, outer_crop_pct=0.10, debug=False):
    """
    Crop the polar-warped image to remove black/zero regions and distorted edges.

    The cone tip is circular, so after polar warp most of the inner region is black.
    This function first crops to non-zero regions, then applies inner/outer crop percentages.

    Args:
        polar_image: Polar-warped image (after rotation, height=angle, width=radius)
        inner_crop_pct: Percentage of non-zero region to discard from inner edge
        outer_crop_pct: Percentage of non-zero region to discard from outer edge
        debug: Print debug information

    Returns:
        Cropped polar image containing only the valid "sweet spot"
    """
    if debug:
        print(f"  Input polar shape: {polar_image.shape}")

    # First, find the non-zero region (remove black areas)
    result = find_object(polar_image)

    if result[0] is None:
        if debug:
            print("  Warning: No non-zero region found!")
        return polar_image

    (x_min, x_max), (y_min, y_max) = result

    if debug:
        print(f"  Non-zero region: x=[{x_min}, {x_max}], y=[{y_min}, {y_max}]")

    # Crop to non-zero region first
    cropped = polar_image[y_min:y_max + 1, x_min:x_max + 1]

    if debug:
        print(f"  After non-zero crop: {cropped.shape}")

    # Now apply crop percentages on the valid region
    height, width = cropped.shape[:2]

    # width = radius direction (after rotation in unroll_cone_tip)
    r_inner = int(width * inner_crop_pct)
    r_outer = int(width * (1 - outer_crop_pct))

    # height = angle direction — crop 10% top and bottom to remove edge artifacts
    h_top = int(height * 0.10)
    h_bottom = int(height * (1 - 0.10))

    if debug:
        print(f"  Radius crop: [{r_inner}:{r_outer}] out of {width}")
        print(f"  Angle crop: [{h_top}:{h_bottom}] out of {height}")

    final = cropped[h_top:h_bottom, r_inner:r_outer]

    if debug:
        print(f"  Final shape: {final.shape}")

    return final
