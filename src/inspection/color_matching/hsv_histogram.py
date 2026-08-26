import cv2
import numpy as np


def compute_hsv_hue_histogram(bgr_patch: np.ndarray, bins: int = 32) -> np.ndarray:
    """Compute 1D histogram on HSV Hue channel.

    Hue is the strongest discriminator for violet vs white —
    white has low saturation (hue undefined), violet has distinct hue ~130-150.

    Args:
        bgr_patch: BGR image patch (already preprocessed/cropped).
        bins: Number of histogram bins (default 32).

    Returns:
        L1-normalized 1D hue histogram.
    """
    hsv = cv2.cvtColor(bgr_patch, cv2.COLOR_BGR2HSV)

    # Mask out black pixels (invalid regions from polar unwarp)
    mask = (bgr_patch > 0).any(axis=2).astype(np.uint8) * 255

    # Compute hue histogram only on valid pixels
    hist = cv2.calcHist([hsv], [0], mask, [bins], [0, 180])
    hist = hist.flatten()

    # L1 normalize
    total = hist.sum()
    if total > 0:
        hist = hist / total

    return hist.astype(np.float32)


def compute_hs_histogram(bgr_patch: np.ndarray, bins: int = 32) -> np.ndarray:
    """Compute 2D histogram on HSV H+S channels.

    Captures both hue and saturation — white has low S, violet has high S + distinct H.

    Args:
        bgr_patch: BGR image patch.
        bins: Number of bins per channel.

    Returns:
        L1-normalized 2D H-S histogram (bins x bins).
    """
    hsv = cv2.cvtColor(bgr_patch, cv2.COLOR_BGR2HSV)

    mask = (bgr_patch > 0).any(axis=2).astype(np.uint8) * 255

    hist = cv2.calcHist([hsv], [0, 1], mask, [bins, bins], [0, 180, 0, 256])

    total = hist.sum()
    if total > 0:
        hist = hist / total

    return hist.astype(np.float32)
