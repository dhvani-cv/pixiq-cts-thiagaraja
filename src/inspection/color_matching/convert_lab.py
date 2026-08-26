import cv2


def convert_to_lab(bgr_image):
    """
    Convert BGR image to CIELAB color space.

    Args:
        bgr_image: Input BGR image

    Returns:
        LAB image (L*, a*, b* channels)
    """
    return cv2.cvtColor(bgr_image, cv2.COLOR_BGR2LAB)
