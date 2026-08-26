from .histogram_2d import compute_2d_histogram, HIST_BINS, HIST_RANGE
from .normalize_histogram import normalize_histogram_l1
from .entropy_2d import compute_2d_entropy
from .mean_lightness import compute_mean_lightness


def get_statistical_signature(lab_patch, bins=HIST_BINS, hist_range=HIST_RANGE):
    """
    Generate complete statistical signature from LAB patch.

    Args:
        lab_patch: Preprocessed LAB image patch
        bins: Histogram bins per channel
        hist_range: Histogram range

    Returns:
        dict with keys: histogram, entropy, mean_L
    """
    # 2D histogram on a* and b*
    hist = compute_2d_histogram(lab_patch, bins, hist_range)

    # L1 normalize
    hist_norm = normalize_histogram_l1(hist)

    # 2D joint entropy
    entropy = compute_2d_entropy(hist_norm)

    # Mean lightness for illumination monitoring
    mean_L = compute_mean_lightness(lab_patch)

    return {
        'histogram': hist_norm,
        'entropy': entropy,
        'mean_L': mean_L
    }
