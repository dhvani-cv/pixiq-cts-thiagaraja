import numpy as np
import cv2


def find_object(img):
    # Create binary mask of non-black pixels

    if len(img.shape) == 3:
        # Check if it's LAB image (black pixels have L*=0, a*=128, b*=128)
        # Use only the first channel (L* for LAB, or B for BGR)
        first_channel = img[:, :, 0]
        mask = first_channel > 0
    else:
        mask = img > 0

    # Find rows and columns that contain the object
    rows_with_object = np.any(mask, axis=1)
    cols_with_object = np.any(mask, axis=0)

    # Get the bounding indices
    row_indices = np.where(rows_with_object)[0]
    col_indices = np.where(cols_with_object)[0]

    if len(row_indices) == 0 or len(col_indices) == 0:
        return None, None, None

    # Calculate bounding box
    y_min, y_max = row_indices[0], row_indices[-1]
    x_min, x_max = col_indices[0], col_indices[-1]
    return (x_min, x_max), (y_min, y_max)
