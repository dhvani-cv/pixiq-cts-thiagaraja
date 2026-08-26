from .bhattacharyya_distance import compute_bhattacharyya_distance


def match_pattern(live_hist, live_entropy, live_L,
                  master_hist, master_entropy, master_L,
                  bhatt_threshold=0.15, entropy_threshold=0.5,
                  L_warning_threshold=10.0):
    """
    Compare live signature against master template using Bhattacharyya distance.

    Args:
        live_hist: L1-normalized histogram from live image
        live_entropy: 2D joint entropy from live image
        live_L: Mean L* from live image
        master_hist: Stored master histogram
        master_entropy: Stored master entropy
        master_L: Stored master mean L*
        bhatt_threshold: Maximum allowed Bhattacharyya distance
        entropy_threshold: Maximum allowed entropy difference
        L_warning_threshold: L* drift percentage to trigger warning

    Returns:
        dict with pass/fail, confidence, distances, and warnings
    """
    # Bhattacharyya Distance: 0 (identical) to 1 (no overlap)
    bhatt_dist = compute_bhattacharyya_distance(live_hist, master_hist)

    # Entropy difference
    entropy_delta = abs(live_entropy - master_entropy)

    # Illumination drift check
    L_drift_pct = abs(live_L - master_L) / (master_L + 1e-7) * 100
    illumination_warning = L_drift_pct > L_warning_threshold

    # Confidence calculation
    color_conf = max(0.0, 1.0 - (bhatt_dist / bhatt_threshold))
    pattern_conf = max(0.0, 1.0 - (entropy_delta / entropy_threshold))
    overall_conf = 0.7 * color_conf + 0.3 * pattern_conf

    # Decision based on Bhattacharyya distance (primary) and entropy (secondary)
    passed = (bhatt_dist < bhatt_threshold) and (entropy_delta < entropy_threshold)

    return {
        'pass': passed,
        'confidence': round(overall_conf * 100, 1),
        'bhattacharyya_distance': round(float(bhatt_dist), 4),
        'entropy_delta': round(float(entropy_delta), 4),
        'illumination_warning': illumination_warning,
        'L_drift_percent': round(float(L_drift_pct), 1)
    }
