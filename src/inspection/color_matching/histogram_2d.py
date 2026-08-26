import cv2

HIST_BINS = 32
HIST_RANGE = [0, 256, 0, 256]


def compute_2d_histogram(lab_patch, bins=HIST_BINS, hist_range=HIST_RANGE):
    """
    Compute 2D joint histogram on a* and b* channels.

    Args:
        lab_patch: LAB image patch
        bins: Number of bins per channel
        hist_range: [a_min, a_max, b_min, b_max]

    Returns:
        2D histogram (bins x bins)
    """
    hist = cv2.calcHist(
        [lab_patch],
        [1, 2],  # a* and b* channels
        None,
        [bins, bins],
        hist_range
    )
    return hist
