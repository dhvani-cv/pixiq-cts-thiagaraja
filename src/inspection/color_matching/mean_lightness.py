def compute_mean_lightness(lab_patch):
    """
    Compute mean L* value for illumination monitoring.

    Args:
        lab_patch: LAB image patch

    Returns:
        Mean L* value (float)
    """
    return float(lab_patch[:, :, 0].mean())
