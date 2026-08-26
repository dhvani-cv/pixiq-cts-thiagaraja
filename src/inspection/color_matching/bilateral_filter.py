import cv2


def apply_bilateral_filter(bgr_image, d=9, sigma_color=75, sigma_space=75):
    """
    Apply bilateral filter to remove texture noise while preserving edges.

    Args:
        bgr_image: Input BGR image
        d: Diameter of each pixel neighborhood
        sigma_color: Filter sigma in the color space
        sigma_space: Filter sigma in the coordinate space

    Returns:
        Filtered BGR image
    """
    return cv2.bilateralFilter(bgr_image, d, sigma_color, sigma_space)
