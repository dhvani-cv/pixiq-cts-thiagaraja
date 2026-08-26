def normalize_histogram_l1(hist):
    """
    L1 normalize histogram so it sums to 1.0 (probability distribution).

    Args:
        hist: Input histogram

    Returns:
        Normalized histogram
    """
    total = hist.sum()
    if total > 0:
        return hist / total
    return hist
