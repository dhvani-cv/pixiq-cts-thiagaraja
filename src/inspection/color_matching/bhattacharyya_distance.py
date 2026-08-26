import cv2
import numpy as np


def compute_bhattacharyya_distance(hist1, hist2):
    """
    Compute Bhattacharyya distance between two histograms.

    Args:
        hist1: First L1-normalized histogram
        hist2: Second L1-normalized histogram

    Returns:
        Distance value (0 = identical, 1 = no overlap)
    """
    return cv2.compareHist(
        hist1.astype(np.float32),
        hist2.astype(np.float32),
        cv2.HISTCMP_BHATTACHARYYA
    )
