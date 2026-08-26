"""
UV Inspection — Polymer mixup detection via blue-channel radial profile
+ u-space-normalized hand-feature classifier.

Ported from Indorama loop1's illumination-invariant pipeline
(uv_lamp_investigation/, handfeature_logreg_illum_invariant_uspace model),
adapted for KPR's tube-only UV lighting and kpr_uv.pt detector.

Physics:
    UV light causes yarn to fluoresce. Different polymers fluoresce differently
    (chemistry-based response). A mixed polymer wound in layers creates
    concentric bands of different fluorescence intensity — visible as a local
    dip in the radial brightness profile from tube (inner) to cone edge (outer).

    Blue channel is used, same as Indorama's pipeline this was ported from.
    NOT YET independently re-verified against KPR's own material (polyester+
    cotton) — Indorama's material may differ. Re-check channel choice once
    real KPR labeled defect data exists (see conversation record — a same-
    methodology channel sweep on Indorama's real 08-18 data found red/
    normRGB_r beating blue there; this has not been repeated on genuine KPR
    defects, only on a material-confounded test cone which gave unreliable
    results).

Detection pipeline:
    YOLO detect (cone + tube)
    → annular region (inner=tube edge × inner_scale, outer=cone edge ×
      (1 - outer_margin))
    → polar-unwarp the blue channel around the annulus center into a
      (720 angle bins, radius) image
    → average across all 720 angles -> 1D radial brightness profile
    → subtract a reflect-padded moving-average baseline (high-pass filter)
    → depth_pct = most prominent dip's depth, as % of local baseline
    → coverage_pct = angular coverage of that dip radius, per-frame
      calibrated against control radii on the same image
    → bin_vote_count = how many of 16 angular sectors independently agree
      on a dip at (approximately) the same radius, in u-space (normalized
      per-cone radial position, NOT fixed pixels — see module note below)
    → longest_run_deg = longest contiguous angular arc showing a dip
    → hand-feature classifier (logistic regression on the 4 features above)
      decides has_mixup, falling back to a simple depth/coverage OR-heuristic
      if the classifier isn't configured or fails to score.

u-space note on bin_vote_count:
    The radius-agreement test that turns 16 independent per-sector dip
    detections into a single "how many sectors agree" count needs a
    tolerance window. A FIXED PIXEL tolerance (e.g. 20px) makes the test's
    strictness silently depend on cone diameter — the same absolute window
    covers a bigger fraction of a small cone's annulus than a big cone's,
    inflating vote agreement on smaller cones independent of whether a real
    defect is present. This was found by comparing an artificial small test
    cone (annulus ~273px) against normal-size good cones (annulus ~370px) at
    KPR. Fix: express radial position as u = (r - inner_r) / (outer_r -
    inner_r) (per-cone normalized, 0=tube edge, 1=cone edge) and the
    tolerance as RADIUS_TOL_U (a fraction of that cone's own annulus width),
    so the agreement test's strictness stays constant across cone sizes.
    Validated on Indorama's real 08-18 labeled data (201 good/61 bad, where
    annulus width does NOT vary between classes, so this is a clean
    same-material check): single-feature AUC improves from 0.9315 (fixed
    20px) to 0.9389 (u-space, eps=0.05).

Model / threshold status (as of this port):
    Model weights trained/validated ONLY on Indorama real labeled data
    (201 good + 61 bad, 08-18, plus 08-20 good batches). NOT retrained on any
    KPR data. Threshold (0.84) was set using KPR's own real good-cone score
    distribution (16 tube-only good captures, max observed 0.6686 -- 0.84
    clears with margin) but KPR's only "bad" test data was found to be a
    DIFFERENT BASE MATERIAL from the good cones, not a real mixup defect --
    so recall at this threshold is UNVALIDATED for KPR. This is a staged,
    best-effort starting point, not a confirmed-safe production threshold.
    tasks.uv_inspection is deliberately left disabled in config.json after
    this port -- do not enable without explicit confirmation.

Scope: polymer mixup ONLY. Appearance defects (wrong dye, fading) are not
UV's responsibility — handled by visible light camera.

Usage:
    inspector = UVInspection(config)
    result = inspector.process_frame(uv_frame)
    if result.has_mixup:
        plc_code = 2  # Defect
    else:
        plc_code = 1  # Good
"""

import json
import logging
from typing import Optional

import cv2
import numpy as np
from scipy.signal import find_peaks

from .data_types import UVResult
from .yolo_detector import YOLODetector

logger = logging.getLogger(__name__)

N_ANGLE_BINS = 720  # polar-unwrap angular resolution

BASELINE_WINDOW = 31   # px, moving-average width for the high-pass detrend
EDGE_TRIM = 5           # px, guard trimmed off both ends of the detrended
                        # profile against reflect-padding artifacts at the tips
MIN_PROMINENCE = 0.3   # minimum dip prominence (blue intensity units) to count
MIN_DIP_SPACING = 10    # px, min distance between candidate dips

COVERAGE_BAND = 25          # px, +/- window each side of a radius used for its local baseline
COVERAGE_SEARCH = 8         # px, +/- radius jitter tolerance per angle
COVERAGE_NULL_STEP = 40     # px, spacing between control radii used to build the null distribution
COVERAGE_NULL_PERCENTILE = 10  # "dark" = darker than this percentile of the null

MIN_ANNULUS_WIDTH = 60  # px, outer_r - inner_r must be at least this for a
                         # reliable radial profile (BASELINE_WINDOW + margins)

N_BINS = 16  # angular sectors for bin_vote_count / longest_run_deg

# Consecutive YOLO detection failures trigger an operator alert.
_UV_DETECTION_FAIL_THRESHOLD = 5


class HandFeatureClassifier:
    """Loads a trained hand-feature logistic regression bundle (JSON: features,
    scaler_mean/scale, coef, intercept, threshold, bin_vote_params) and scores
    (coverage_pct, depth_pct, bin_vote_count, longest_run_deg) -> probability.

    Illumination-invariant by construction (all 4 features are ratios/counts
    computed within a single image's own polar profile) -- no rolling
    per-session baseline needed, unlike the earlier p10-based classifier this
    replaces (see Indorama's uv_lamp_investigation/CLAUDE.md for that history).
    """

    def __init__(self, model_path: str):
        with open(model_path) as f:
            bundle = json.load(f)
        self.features = bundle["features"]
        self.scaler_mean = np.array(bundle["scaler_mean"], dtype=np.float64)
        self.scaler_scale = np.array(bundle["scaler_scale"], dtype=np.float64)
        self.coef = np.array(bundle["coef"], dtype=np.float64)
        self.intercept = float(bundle["intercept"])
        self.threshold = float(bundle["threshold"])
        bin_vote_params = bundle.get("bin_vote_params", {})
        self.n_bins = int(bin_vote_params.get("n_bins", N_BINS))
        self.radius_tol_u = float(bin_vote_params.get("radius_tol_u", 0.05))
        self.bin_depth_thresh = float(bin_vote_params.get("bin_depth_thresh_pct", 6.0))
        logger.info(
            "HandFeatureClassifier loaded from %s | threshold=%.4f | "
            "radius_tol_u=%.3f | bin_depth_thresh=%.1f%%",
            model_path, self.threshold, self.radius_tol_u, self.bin_depth_thresh,
        )

    def score(self, coverage_pct: float, depth_pct: float, votes: int, longest_run_deg: float) -> float:
        x = np.array([coverage_pct, depth_pct, votes, longest_run_deg], dtype=np.float64)
        xs = (x - self.scaler_mean) / self.scaler_scale
        z = float(np.dot(self.coef, xs) + self.intercept)
        return 1.0 / (1.0 + np.exp(-z))


class UVInspection:
    """UV polymer mixup detector using YOLO + blue-channel radial profile analysis
    + a u-space hand-feature classifier (preferred) with a heuristic fallback.
    """

    def __init__(self, config: dict):
        """Initialize from the ``uv_inspection`` config section.

        Args:
            config: The ``uv_inspection`` section of the inspection config.
                Expected keys (all optional with defaults):
                    yolo_weights (str): Path to UV YOLO weights.
                        Default: "weights/kpr_uv.pt".
                    yolo_conf (float): YOLO confidence threshold. Default: 0.3.
                    radial_dip_threshold (float): Fallback heuristic dip-depth
                        threshold (%), used only if the classifier is disabled
                        or fails to load/score. Default: 1.33 (Indorama-
                        validated value; NOT independently validated for KPR).
                    coverage_threshold (float): Fallback heuristic angular-
                        coverage threshold (%). Default: 18.2 (same caveat).
                    inner_scale (float): Multiplier on tube radius for the
                        annulus inner edge. Default: 1.15 (Indorama-validated).
                    outer_margin (float): Fraction to shrink outer radius.
                        Default: 0.02 (Indorama-validated).
                    use_handfeature_model (bool): Enable the trained
                        classifier. Default: False -- explicit opt-in.
                    handfeature_model_path (str): Path to the model JSON
                        bundle (see HandFeatureClassifier).
        """
        self.detector = YOLODetector(
            model_path=config.get("yolo_weights", "weights/kpr_uv.pt"),
            conf_threshold=config.get("yolo_conf", 0.3),
        )

        # Fallback heuristic thresholds -- only used if the classifier is
        # disabled or fails to score. Carried over from Indorama's
        # jointly-optimized values; NOT independently validated on KPR data.
        self.radial_dip_threshold = config.get("radial_dip_threshold", 1.33)
        self.coverage_threshold = config.get("coverage_threshold", 18.2)

        self.inner_scale = config.get("inner_scale", 1.15)
        self.outer_margin = config.get("outer_margin", 0.02)

        self._handfeature: Optional[HandFeatureClassifier] = None
        if config.get("use_handfeature_model", False):
            model_path = config.get("handfeature_model_path")
            try:
                self._handfeature = HandFeatureClassifier(model_path)
                logger.info("UV hand-feature classifier enabled from %s", model_path)
            except Exception:
                logger.exception(
                    "UV hand-feature classifier failed to load from %s - "
                    "falling back to the radial-dip/coverage heuristic", model_path,
                )

        self._consecutive_detection_failures = 0

        logger.info(
            "UVInspection initialized | classifier=%s | radial_dip_threshold=%.2f%% | "
            "coverage_threshold=%.2f%% | inner_scale=%.2f | outer_margin=%.2f",
            "enabled" if self._handfeature is not None else "disabled (heuristic fallback)",
            self.radial_dip_threshold,
            self.coverage_threshold,
            self.inner_scale,
            self.outer_margin,
        )

    @staticmethod
    def _annular_crop(cone_crop: np.ndarray, center: tuple[int, int], inner_r: float, outer_r: float) -> np.ndarray:
        """Mask cone_crop down to just the annulus (inner_r..outer_r ring)."""
        mask = np.zeros(cone_crop.shape[:2], dtype=np.uint8)
        cv2.circle(mask, center, int(outer_r), 255, -1)
        cv2.circle(mask, center, int(inner_r), 0, -1)
        return cv2.bitwise_and(cone_crop, cone_crop, mask=mask)

    @staticmethod
    def _radial_profile(polar: np.ndarray, inner_r: float, outer_r: float, margin: float = 15.0):
        """Average a polar image over angle -> 1D profile vs radius, cropped away from edges."""
        profile = polar.mean(axis=0)
        radii = np.arange(polar.shape[1])
        mask = (radii > inner_r + margin) & (radii < outer_r - margin)
        return radii[mask], profile[mask]

    @staticmethod
    def _find_ring(radii: np.ndarray, profile: np.ndarray):
        """Detrend profile and locate the most prominent narrow dip.
        Returns (ring_radius_px, depth_pct) or (None, None) if nothing found.
        """
        pad = BASELINE_WINDOW // 2
        padded = np.pad(profile, pad, mode="reflect")
        baseline = np.convolve(padded, np.ones(BASELINE_WINDOW) / BASELINE_WINDOW, mode="valid")
        resid = profile - baseline

        core = slice(EDGE_TRIM, len(resid) - EDGE_TRIM)
        dips, props = find_peaks(-resid[core], prominence=MIN_PROMINENCE, distance=MIN_DIP_SPACING)
        if len(dips) == 0:
            return None, None

        best_local = dips[np.argmax(props["prominences"])]
        best = best_local + EDGE_TRIM
        depth = props["prominences"][np.argmax(props["prominences"])]
        ring_r = int(radii[best])
        depth_pct = 100 * depth / baseline[best]
        return ring_r, depth_pct

    @staticmethod
    def _local_min_zscore(polar: np.ndarray, r0: int, search: int) -> np.ndarray:
        """For every angle row, find the local minimum within r0 +/- search and
        z-score it against that row's own baseline.

        Fully vectorized (no per-angle Python loop) — validated bit-for-bit
        equivalent to the original per-row loop implementation (max abs diff
        ~4.78e-6, float rounding only) on real KPR frames, ~8x faster. This
        was the dominant cost in UV inspection (~74% of total cycle time
        across the ~8 calls per cycle from _angular_coverage's control-radii
        sweep).
        """
        n_angles, n_radii = polar.shape
        search_lo, search_hi = max(r0 - search, 0), min(r0 + search + 1, n_radii)
        local_min_r = search_lo + np.argmin(polar[:, search_lo:search_hi], axis=1)

        lo = np.clip(local_min_r - COVERAGE_BAND, 0, None)
        hi = np.clip(local_min_r + COVERAGE_BAND, None, n_radii)
        strip_lo = np.clip(local_min_r - 2, 0, None)
        strip_hi = np.clip(local_min_r + 3, None, n_radii)

        col = np.arange(n_radii)[None, :]
        band_mask = (
            ((col >= lo[:, None]) & (col < strip_lo[:, None]))
            | ((col >= strip_hi[:, None]) & (col < hi[:, None]))
        )
        strip_mask = (col >= strip_lo[:, None]) & (col < strip_hi[:, None])

        band_count = band_mask.sum(axis=1)
        band_sum = np.where(band_mask, polar, 0).sum(axis=1)
        band_mean = band_sum / band_count
        band_sqsum = np.where(band_mask, polar ** 2, 0).sum(axis=1)
        band_var = np.maximum(band_sqsum / band_count - band_mean ** 2, 0)
        band_std = np.maximum(np.sqrt(band_var), 1e-6)

        strip_count = strip_mask.sum(axis=1)
        strip_mean = np.where(strip_mask, polar, 0).sum(axis=1) / strip_count

        return (strip_mean - band_mean) / band_std

    def _angular_coverage(self, polar: np.ndarray, ring_r: int, inner_r: float, outer_r: float) -> float:
        """Fraction of the N_ANGLE_BINS angle bins where a dip is present near
        ring_r, using a threshold calibrated against THIS frame's own texture noise.
        """
        z_ring = self._local_min_zscore(polar, ring_r, COVERAGE_SEARCH)

        exclusion = COVERAGE_BAND + COVERAGE_SEARCH
        control_radii = [
            r for r in range(int(inner_r) + 40, int(outer_r) - 40, COVERAGE_NULL_STEP)
            if abs(r - ring_r) > exclusion
        ]
        null_z = (
            np.concatenate([self._local_min_zscore(polar, r, COVERAGE_SEARCH) for r in control_radii])
            if control_radii else z_ring
        )

        thresh = np.percentile(null_z, COVERAGE_NULL_PERCENTILE)
        dark_mask = z_ring < thresh
        return float(dark_mask.mean())

    @staticmethod
    def _contiguous_runs(mask: np.ndarray) -> float:
        """Longest contiguous run of True in a circular boolean mask, in degrees."""
        n = len(mask)
        if not mask.any():
            return 0.0
        idx = np.where(~mask)[0]
        if len(idx) == 0:
            return 360.0
        start = idx[0]
        rolled = np.roll(mask, -start)
        runs, cur = [], 0
        for v in rolled:
            if v:
                cur += 1
            else:
                if cur:
                    runs.append(cur)
                cur = 0
        if cur:
            runs.append(cur)
        return max(runs) * 360.0 / n

    def _bin_vote_count_uspace(self, polar: np.ndarray, inner_r: float, outer_r: float,
                                bin_depth_thresh: float, radius_tol_u: float) -> int:
        """16-sector radius-agreement vote, in u-space (per-cone normalized
        radial position) rather than fixed pixels -- see module docstring.
        """
        rows = N_ANGLE_BINS // N_BINS
        valid_u = []
        for b in range(N_BINS):
            sector = polar[b * rows:(b + 1) * rows]
            radii, profile = self._radial_profile(sector, inner_r, outer_r, margin=15.0)
            if len(radii) < 20:
                continue
            ring_r, depth_pct = self._find_ring(radii, profile)
            if ring_r is not None and depth_pct is not None and depth_pct > bin_depth_thresh:
                valid_u.append((ring_r - inner_r) / (outer_r - inner_r))
        if not valid_u:
            return 0
        valid_u = np.array(valid_u)
        return int(max(np.sum(np.abs(valid_u - c) <= radius_tol_u) for c in valid_u))

    def _compute_radial_dip(
        self,
        frame: np.ndarray,
        cone_bbox: tuple,
        tube_bbox: tuple,
    ) -> Optional[tuple[float, float, int, float, np.ndarray]]:
        """Compute blue-channel radial dip depth, angular coverage, sector-vote
        count, and longest angular dark run on the annular yarn surface.

        Returns:
            (depth_pct, coverage_pct, votes, longest_run_deg, annular_crop)
            or None if geometry invalid / region too small.
        """
        cx1, cy1, cx2, cy2 = map(int, cone_bbox)
        h, w = frame.shape[:2]
        cx1, cy1 = max(0, cx1), max(0, cy1)
        cx2, cy2 = min(w, cx2), min(h, cy2)
        cone_crop = frame[cy1:cy2, cx1:cx2]

        if cone_crop.size == 0:
            logger.warning("UV: empty cone crop")
            return None

        tx1, ty1, tx2, ty2 = map(int, tube_bbox)
        crop_cx = (cx2 - cx1) / 2.0
        crop_cy = (cy2 - cy1) / 2.0
        inner_r = float(min(tx2 - tx1, ty2 - ty1)) / 2 * self.inner_scale
        outer_r = float(min(cx2 - cx1, cy2 - cy1)) / 2 * (1.0 - self.outer_margin)

        if outer_r <= inner_r or outer_r <= 0:
            logger.warning("UV: invalid geometry inner=%.0f outer=%.0f", inner_r, outer_r)
            return None

        if outer_r - inner_r < MIN_ANNULUS_WIDTH:
            logger.warning(
                "UV: annulus too narrow for reliable radial profile (inner=%.0f outer=%.0f width=%.0f)",
                inner_r, outer_r, outer_r - inner_r,
            )
            return None

        annular_crop = self._annular_crop(cone_crop, (int(crop_cx), int(crop_cy)), inner_r, outer_r)
        blue = annular_crop[:, :, 0].astype(np.float32)

        max_r = int(outer_r) + 5
        polar = cv2.warpPolar(blue, (max_r, N_ANGLE_BINS), (crop_cx, crop_cy), max_r, cv2.WARP_POLAR_LINEAR)

        radii, profile = self._radial_profile(polar, inner_r, outer_r, margin=15.0)
        if len(radii) < 20:
            logger.warning("UV: too few valid radial samples (%d)", len(radii))
            return None

        ring_r, depth_pct = self._find_ring(radii, profile)
        if ring_r is None:
            return 0.0, 0.0, 0, 0.0, annular_crop

        coverage_pct = 100 * self._angular_coverage(polar, ring_r, inner_r, outer_r)

        bin_depth_thresh = self._handfeature.bin_depth_thresh if self._handfeature is not None else 6.0
        radius_tol_u = self._handfeature.radius_tol_u if self._handfeature is not None else 0.05
        votes = self._bin_vote_count_uspace(polar, inner_r, outer_r, bin_depth_thresh, radius_tol_u)

        z_ring = self._local_min_zscore(polar, ring_r, COVERAGE_SEARCH)
        exclusion = COVERAGE_BAND + COVERAGE_SEARCH
        control_radii = [
            r for r in range(int(inner_r) + 40, int(outer_r) - 40, COVERAGE_NULL_STEP)
            if abs(r - ring_r) > exclusion
        ]
        null_z = (
            np.concatenate([self._local_min_zscore(polar, r, COVERAGE_SEARCH) for r in control_radii])
            if control_radii else z_ring
        )
        thresh = np.percentile(null_z, COVERAGE_NULL_PERCENTILE)
        dark_mask = z_ring < thresh
        longest_run_deg = self._contiguous_runs(dark_mask)

        return depth_pct, coverage_pct, votes, longest_run_deg, annular_crop

    def _detection_failed(self, reason: str, cone_bbox: tuple | None = None) -> UVResult:
        """Record a detection failure, alert operator if threshold crossed, return skip result."""
        self._consecutive_detection_failures += 1
        if self._consecutive_detection_failures >= _UV_DETECTION_FAIL_THRESHOLD:
            logger.error(
                "UV: %d consecutive detection failures (latest: %s) — "
                "check UV camera, lighting, and YOLO model. UV check is being SKIPPED.",
                self._consecutive_detection_failures,
                reason,
            )
        else:
            logger.warning("UV: detection failed (%s) — skipping UV check for this cone", reason)
        return UVResult(has_mixup=False, detection_failed=True, cone_bbox=cone_bbox)

    def process_frame(self, frame: np.ndarray) -> UVResult:
        """Run UV polymer mixup inspection on one frame.

        Notes:
            detection_failed=True means YOLO or compute failed — UV check is
            skipped for this cone (treated as uv_code=None by the caller, not Good).
            VL + Tail results still decide the final verdict.
        """
        try:
            detections = self.detector.detect(frame)
            cone_det = self.detector.get_detection_by_class(detections, "yarn_cone")
            tube_det = self.detector.get_detection_by_class(detections, "yarn_tube")

            if cone_det is None:
                return self._detection_failed("no cone detected")

            if tube_det is None:
                return self._detection_failed("no tube detected", cone_bbox=cone_det.bbox)

            result = self._compute_radial_dip(frame, cone_det.bbox, tube_det.bbox)

            if result is None:
                return self._detection_failed("radial dip compute failed", cone_bbox=cone_det.bbox)

            self._consecutive_detection_failures = 0

            depth_pct, coverage_pct, votes, longest_run_deg, annular_crop = result

            heuristic_mixup = (
                (depth_pct > self.radial_dip_threshold)
                or (coverage_pct > self.coverage_threshold)
            )
            has_mixup = heuristic_mixup
            handfeature_score = None

            if self._handfeature is not None:
                handfeature_score = self._handfeature.score(coverage_pct, depth_pct, votes, longest_run_deg)
                has_mixup = handfeature_score >= self._handfeature.threshold

            logger.info(
                "UV depth_pct=%.2f%% coverage_pct=%.2f%% votes=%d longest_run=%.1f° "
                "handfeature=%s (thr=%.4f) heuristic=%s -> verdict=%s",
                depth_pct, coverage_pct, votes, longest_run_deg,
                "n/a" if handfeature_score is None else "%.4f" % handfeature_score,
                self._handfeature.threshold if self._handfeature is not None else 0.0,
                "MIXUP" if heuristic_mixup else "OK",
                "MIXUP" if has_mixup else "OK",
            )

            return UVResult(
                has_mixup=has_mixup,
                radial_dip=depth_pct,   # field name kept for API/UI compatibility
                gb_ratio=coverage_pct,  # repurposed to carry coverage_pct, field name kept
                cone_bbox=cone_det.bbox,
            )

        except Exception:
            logger.exception("Unexpected error in UV process_frame")
            return self._detection_failed("unexpected exception")
