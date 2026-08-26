import numpy as np


def compute_2d_entropy(hist_normalized):
    """
    Compute Shannon entropy of a normalized 2D histogram.

    Args:
        hist_normalized: L1-normalized histogram (must sum to 1.0)

    Returns:
        Entropy value (float)
    """
    hist_flat = hist_normalized.flatten()
    hist_flat = hist_flat[hist_flat > 0]  # Avoid log(0)
    entropy = -np.sum(hist_flat * np.log2(hist_flat))
    return float(entropy)
