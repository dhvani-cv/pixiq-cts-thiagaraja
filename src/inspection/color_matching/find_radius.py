from . import find_object


def find_radius(img):
    """
    Find the radius and center of a colored object, crop the image tightly,
    and return the cropped image with adjusted center coordinates.

    Args:
        img: Input image (BGR or grayscale)

    Returns:
        cropped_img: Cropped image containing only the object
        center: (x, y) tuple of the object center relative to cropped image
        radius: Approximate radius of the object
    """

    # Get bounding box from grayscale
    result = find_object.find_object(img)

    if result[0] is None:
        return None, None, None

    (x_min, x_max), (y_min, y_max) = result

    # Crop the original color image (not grayscale)
    cropped_img = img[y_min : y_max + 1, x_min : x_max + 1]

    # Calculate center relative to cropped image (use actual cropped dimensions)
    crop_height, crop_width = cropped_img.shape[:2]
    center = (float(crop_width / 2), float(crop_height / 2))

    # Calculate radius as max inscribed circle: half of the shorter dimension
    radius = int(min(crop_width, crop_height) // 2)

    return cropped_img, center, radius
