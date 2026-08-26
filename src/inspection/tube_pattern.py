"""
Tube Pattern Matcher — verifies tube label against expected template.

Verification-only: compares the live crop against the ONE expected material's
template, never against other materials' templates (no classification). This
is required because materials rotate every ~2 weeks — a nearest-neighbor
classifier would need retraining/rebalancing on every rotation, while
verification-against-expected only needs that one material's template.

Combined distance = (1 - fft_weight) * color_bhatt + fft_weight * fft_cosine
  where color_bhatt = 0.7 * LAB_bhatt + 0.3 * HSV_bhatt

  1. Color: Bhattacharyya distance on LAB a*b* histogram (dominant signal)
  2. FFT: Cosine distance on 1D FFT magnitude of intensity profile
     (shift-invariant spatial feature — discriminates same-color patterns)
  3. Combined: weighted sum vs per-class threshold → pass/fail decision
  4. ResNet: optional third signal, fused via a trained PatternVerifier
     (logistic regression over [color_dist, fft_dist, resnet_dist]) when
     pattern_verifier_model_path is configured — replaces the hand-weighted
     combined_distance formula with a learned, generic same-vs-different
     model. Validated via leave-one-material-out CV (mean AUC 0.9996).

Reference data is created by the teaching module from N tube images.
All class templates are loaded at startup.

Usage:
    matcher = TubePatternMatcher(template_dir="templates/tube")
    matcher.load_all_references()  # Load all class templates
    result = matcher.verify(tube_crop, material_id="MAT-001")
    if result.passed:
        print("Tube pattern OK")
"""

import json
import logging
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import torch
import torch.nn as nn
import torchvision.models as models
import torchvision.transforms as T

from .data_types import TubePatternResult

# Color matching pipeline
from .color_matching.find_radius import find_radius
from .color_matching.preprocess_pipeline import preprocess_cone_tip
from .color_matching.get_signature import get_statistical_signature
from .color_matching.bhattacharyya_distance import compute_bhattacharyya_distance
from .color_matching.entropy_2d import compute_2d_entropy
from .color_matching.hsv_histogram import compute_hs_histogram

logger = logging.getLogger(__name__)


def _cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine distance between two L2-normalized vectors. 0 = identical."""
    return 1.0 - float(np.dot(a, b))


class PatternVerifier:
    """Loads a trained logistic-regression bundle (JSON: features, scaler_mean/
    scale, coef, intercept, threshold) and scores (color_dist, fft_dist,
    resnet_dist) -> match probability.

    Generic same-vs-different verifier: inputs are DISTANCES from a live crop
    to whatever template it's being compared against, never the material
    identity itself. This is what lets it generalize to materials it has
    never seen — validated via leave-one-material-out cross-validation
    (mean AUC 0.9996 across 7 folds, 0.9995 across 5-fold with 1-2 materials
    held out per fold) on real KPR crops.

    Mirrors HandFeatureClassifier in uv_inspection.py — same architecture,
    same JSON bundle shape, same scaled-logistic-regression scoring.
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
        logger.info(
            "PatternVerifier loaded from %s | features=%s | threshold=%.4f",
            model_path, self.features, self.threshold,
        )

    def score(self, color_dist: float, fft_dist: float, resnet_dist: float) -> float:
        """Return match probability in [0, 1] given the three template distances."""
        x = np.array([color_dist, fft_dist, resnet_dist], dtype=np.float64)
        xs = (x - self.scaler_mean) / self.scaler_scale
        z = float(np.dot(self.coef, xs) + self.intercept)
        # np.exp on a Python float still returns numpy.float64, which then
        # taints every downstream comparison (color_match, .passed) with
        # numpy.bool_ instead of a plain bool -- numpy.bool_ is NOT JSON
        # serializable, which crashes the HMI socket.io emit every cycle.
        # Cast back to a plain Python float here so nothing downstream needs
        # to know this function ever touched numpy.
        return float(1.0 / (1.0 + np.exp(-z)))


class _ResNetFeatureExtractor:
    """ResNet50 feature extractor for tube pattern discrimination."""

    def __init__(self, device: str = "auto", inner_ratio: float = 0.30):
        """Initialize ResNet50 feature extractor.

        Args:
            device: PyTorch device ("auto", "cuda", "cpu").
            inner_ratio: Inner hole radius as fraction of outer radius.
                Used for annular masking to black out the tube's inner hole.
                Set to 0 to use circular mask (no inner hole).
        """
        if device == "auto":
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else:
            self.device = torch.device(device)

        self.inner_ratio = inner_ratio

        model = models.resnet50(weights=models.ResNet50_Weights.IMAGENET1K_V2)
        self.model = nn.Sequential(*list(model.children())[:-1])
        self.model.eval()
        self.model.to(self.device)

        self.transform = T.Compose([
            T.ToPILImage(),
            T.Resize((224, 224)),
            T.ToTensor(),
            T.Normalize(mean=[0.485, 0.456, 0.406],
                        std=[0.229, 0.224, 0.225]),
        ])

        logger.info(f"ResNet50 feature extractor initialized on {self.device} (inner_ratio={inner_ratio})")

    @torch.no_grad()
    def extract(self, bgr_image: np.ndarray, apply_mask: bool = True) -> np.ndarray:
        """Extract L2-normalized 2048-dim feature vector from BGR image.

        Args:
            bgr_image: BGR image (already masked or raw).
            apply_mask: If True, apply annular mask. Set to False if input
                is already masked (e.g., from extract_annular_roi).

        Returns:
            L2-normalized 2048-dim feature vector.
        """
        rgb = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB)

        if apply_mask:
            # Apply annular mask (outer circle minus inner hole)
            h, w = rgb.shape[:2]
            cx, cy = w // 2, h // 2
            outer_r = min(cx, cy) - 2
            inner_r = int(outer_r * self.inner_ratio)

            mask = np.zeros((h, w), dtype=np.uint8)
            cv2.circle(mask, (cx, cy), outer_r, 255, -1)  # Fill outer
            if inner_r > 0:
                cv2.circle(mask, (cx, cy), inner_r, 0, -1)  # Cut out inner
            rgb = cv2.bitwise_and(rgb, rgb, mask=mask)

        tensor = self.transform(rgb).unsqueeze(0).to(self.device)
        feat = self.model(tensor).squeeze()
        feat = feat / (feat.norm() + 1e-8)
        return feat.cpu().numpy()


class TubePatternMatcher:
    """Tube label verification — expected-template-only verification, never
    classification.

    Every cycle computes distance from the live crop to the ONE template the
    PLC says should be there (color, FFT, optionally ResNet via a learned
    PatternVerifier) and passes/fails against that template's threshold. It
    never compares against, or picks a nearest match among, other materials'
    templates.

    All class templates must be loaded at startup via load_all_references().
    """

    def __init__(
        self,
        template_dir: str,
        bilateral_d: int = 9,
        bilateral_sigma_color: int = 75,
        bilateral_sigma_space: int = 75,
        inner_crop_pct: float = 0.10,
        outer_crop_pct: float = 0.10,
        inner_ratio: float = 0.80,
        fft_weight: float = 0.3,
        threshold_config: str = "",
        default_threshold: float = 0.25,
        device: str = "auto",
        pattern_verifier_model_path: str = "",
    ):
        """Initialize tube pattern matcher.

        Args:
            template_dir: Directory containing per-material .npz reference files.
            bilateral_d: Bilateral filter diameter for color preprocessing.
            bilateral_sigma_color: Bilateral filter sigma in color space.
            bilateral_sigma_space: Bilateral filter sigma in coordinate space.
            inner_crop_pct: Inner edge crop for polar unwarp sweet spot.
            outer_crop_pct: Outer edge crop for polar unwarp sweet spot.
            inner_ratio: Inner hole radius as fraction of outer radius (for masking).
                Used to black out the tube's center hole in ResNet features.
            fft_weight: Weight for FFT cosine distance in combined distance.
                combined = (1 - fft_weight) * bhatt + fft_weight * fft_cosine.
                0 = color-only (no FFT). Default 0.3 (70% color, 30% FFT).
                Ignored when pattern_verifier_model_path is set (the learned
                model replaces the hand-weighted combined_distance formula).
            threshold_config: Path to tube_verify_config.json with per-class
                thresholds. If empty, looks for it in template_dir parent.
            default_threshold: Default combined distance threshold when no
                per-class threshold is available. Used as fallback.
            device: PyTorch device for ResNet ("auto", "cuda", "cpu").
            pattern_verifier_model_path: Path to a trained PatternVerifier
                JSON bundle. When set, the decision uses the learned
                logistic-regression fusion of (color_dist, fft_dist,
                resnet_dist) INSTEAD OF the hand-weighted combined_distance
                formula. Generic same-vs-different model — not tied to
                specific material identities, so it doesn't need retraining
                when materials rotate. Empty string = disabled (default),
                keeps the hand-weighted color+FFT formula.
        """
        self.template_dir = Path(template_dir)
        self.bilateral_d = bilateral_d
        self.bilateral_sigma_color = bilateral_sigma_color
        self.bilateral_sigma_space = bilateral_sigma_space
        self.inner_crop_pct = inner_crop_pct
        self.outer_crop_pct = outer_crop_pct
        self.inner_ratio = inner_ratio
        self.fft_weight = fft_weight
        self.default_threshold = default_threshold

        self.pattern_verifier: Optional[PatternVerifier] = None
        if pattern_verifier_model_path:
            try:
                self.pattern_verifier = PatternVerifier(pattern_verifier_model_path)
            except Exception:
                logger.exception(
                    "PatternVerifier failed to load from %s — falling back to "
                    "the hand-weighted combined_distance formula", pattern_verifier_model_path,
                )

        # Per-class thresholds for verification mode
        self._per_class_thresholds: dict[str, float] = {}
        self._load_thresholds(threshold_config)

        # ResNet50 feature extractor (with annular masking for inner hole)
        self._resnet = _ResNetFeatureExtractor(device=device, inner_ratio=inner_ratio)

        # All class templates (keyed by material_id, one loaded per taught material)
        self._templates: dict[str, dict] = {}

        logger.info(f"TubePatternMatcher initialized | template_dir={template_dir} | fft_weight={fft_weight}")

    def _load_thresholds(self, config_path: str) -> None:
        """Load per-class verification thresholds from JSON config.

        Looks for tube_verify_config.json in:
        1. Explicit config_path if provided
        2. template_dir parent directory
        3. template_dir itself

        The JSON must have a "classes" key with per-class entries containing
        "threshold" values (combined distance thresholds for 0% false accept).
        """
        search_paths = []
        if config_path:
            search_paths.append(Path(config_path))
        search_paths.append(self.template_dir.parent / "tube_verify_config.json")
        search_paths.append(self.template_dir / "tube_verify_config.json")

        for p in search_paths:
            if p.exists():
                try:
                    with open(p) as f:
                        data = json.load(f)
                    classes = data.get("classes", {})
                    for cls_name, info in classes.items():
                        if "threshold" in info:
                            self._per_class_thresholds[cls_name] = float(info["threshold"])
                    logger.info(
                        "Loaded %d per-class thresholds from %s",
                        len(self._per_class_thresholds), p,
                    )
                    return
                except Exception as e:
                    logger.warning("Failed to load thresholds from %s: %s", p, e)

        logger.warning(
            "No threshold config found — using default_threshold=%.4f for "
            "materials without a per-.npz color_threshold", self.default_threshold,
        )

    def load_all_references(self) -> int:
        """Load all reference templates from template_dir.

        Must be called before verify().

        Returns:
            Number of templates loaded.
        """
        self._templates.clear()

        if not self.template_dir.exists():
            logger.warning(f"Template directory does not exist: {self.template_dir}")
            return 0

        for ref_path in self.template_dir.glob("*.npz"):
            material_id = ref_path.stem

            # Guard against stray/malformed files being silently loaded as
            # phantom templates under a garbage material_id (e.g. a crashed
            # atomic-write leaving "tmpXXXXXX.npz.tmp.npz" in this directory
            # -- Path.stem only strips the LAST suffix, so that file's stem
            # is "tmpXXXXXX.npz.tmp", not a real material id). PLC material
            # numbers are always plain integers, so anything else is refused
            # loudly instead of silently becoming a real (wrong) NN candidate.
            if not material_id.isdigit():
                logger.error(
                    "Refusing to load '%s' as a tube pattern template -- "
                    "material_id '%s' is not a plain integer (PLC material "
                    "numbers always are). This is very likely a stray/corrupt "
                    "file, not a real template -- delete it from %s.",
                    ref_path, material_id, self.template_dir,
                )
                continue

            try:
                data = np.load(str(ref_path), allow_pickle=False)

                template = {}

                # Color histogram
                if "color_hist_mean" in data:
                    template["histogram"] = data["color_hist_mean"].astype(np.float32)
                elif "color_hist" in data:
                    template["histogram"] = data["color_hist"].astype(np.float32)
                else:
                    logger.warning(f"No color histogram in {ref_path}")
                    continue

                # Normalize histogram
                template["histogram"] = template["histogram"] / (template["histogram"].sum() + 1e-7)

                # Compute entropy from histogram for pattern structure matching
                template["entropy"] = compute_2d_entropy(template["histogram"])

                # HSV H-S histogram (optional — added for violet/white separation)
                if "hsv_hist_mean" in data:
                    template["hsv_histogram"] = data["hsv_hist_mean"].astype(np.float32)
                    template["hsv_histogram"] = template["hsv_histogram"] / (template["hsv_histogram"].sum() + 1e-7)
                elif "hsv_histogram" in data:
                    template["hsv_histogram"] = data["hsv_histogram"].astype(np.float32)
                    template["hsv_histogram"] = template["hsv_histogram"] / (template["hsv_histogram"].sum() + 1e-7)

                # ResNet features
                if "resnet_mean_feat" in data:
                    template["resnet_feat"] = data["resnet_mean_feat"].astype(np.float32)
                elif "resnet_feats" in data:
                    feats = data["resnet_feats"].astype(np.float32)
                    mean_feat = feats.mean(axis=0)
                    template["resnet_feat"] = mean_feat / (np.linalg.norm(mean_feat) + 1e-8)
                else:
                    logger.warning(f"No ResNet features in {ref_path}")
                    continue

                # FFT features (optional — backward compatible with old .npz)
                if "fft_mean_feat" in data:
                    template["fft_feat"] = data["fft_mean_feat"].astype(np.float32)
                elif "fft_feats" in data:
                    fft_feats = data["fft_feats"].astype(np.float32)
                    fft_mean = fft_feats.mean(axis=0)
                    template["fft_feat"] = (fft_mean / (np.linalg.norm(fft_mean) + 1e-8)).astype(np.float32)

                # Mean lightness for lightness-based disambiguation
                if "color_mean_L_mean" in data:
                    template["mean_L"] = float(data["color_mean_L_mean"])

                self._templates[material_id] = template

                # Per-pattern threshold: fall back to .npz color_threshold
                # if tube_verify_config.json has no entry for this pattern.
                # Priority: tube_verify_config.json > .npz color_threshold > global default
                if material_id not in self._per_class_thresholds:
                    if "color_threshold" in data:
                        self._per_class_thresholds[material_id] = float(data["color_threshold"])
                        logger.debug(
                            "Threshold for '%s': loaded from .npz (%.4f)",
                            material_id, float(data["color_threshold"]),
                        )

            except Exception as e:
                logger.error(f"Failed to load {ref_path}: {e}")
                continue

        n_fft = sum(1 for t in self._templates.values() if "fft_feat" in t)
        logger.info(f"Loaded {len(self._templates)} tube pattern templates ({n_fft} with FFT features)")
        return len(self._templates)

    def load_reference(self, material_id: str) -> Optional[dict]:
        """Get template for a specific material ID.

        Args:
            material_id: Material identifier.

        Returns:
            Template dict or None if not found.
        """
        if material_id in self._templates:
            return self._templates[material_id]

        # Try loading from file if not in memory
        ref_path = self.template_dir / f"{material_id}.npz"
        if ref_path.exists():
            self.load_all_references()
            return self._templates.get(material_id)

        return None

    def extract_resnet_features(self, bgr_image: np.ndarray, apply_mask: bool = True) -> np.ndarray:
        """Extract ResNet50 features from a tube crop.

        Args:
            bgr_image: BGR tube crop image.
            apply_mask: If True, apply annular mask to black out corners and
                inner hole. Set to False if image is already masked.

        Returns:
            L2-normalized 2048-dim feature vector.
        """
        return self._resnet.extract(bgr_image, apply_mask=apply_mask)

    def compute_color_signature(self, bgr_image: np.ndarray) -> Optional[dict]:
        """Extract color signature (LAB a*b* histogram + HSV H-S histogram)."""
        cropped_img, center, radius = find_radius(bgr_image)

        if cropped_img is None:
            logger.warning("Color signature: find_radius returned None")
            return None

        lab_patch = preprocess_cone_tip(
            cropped_img, center, radius,
            inner_crop_pct=self.inner_crop_pct,
            outer_crop_pct=self.outer_crop_pct,
            bilateral_d=self.bilateral_d,
            bilateral_sigma_color=self.bilateral_sigma_color,
            bilateral_sigma_space=self.bilateral_sigma_space,
        )

        sig = get_statistical_signature(lab_patch)

        # HSV H-S histogram on BGR polar patch (violet vs white separation)
        from .color_matching.bilateral_filter import apply_bilateral_filter
        from .color_matching.unrolled import unroll_cone_tip
        from .color_matching.crop_sweet_spot import crop_polar_sweet_spot

        filtered_bgr = apply_bilateral_filter(
            cropped_img, self.bilateral_d,
            self.bilateral_sigma_color, self.bilateral_sigma_space,
        )
        mask = (cropped_img > 0).any(axis=2)
        filtered_bgr[~mask] = 0
        bgr_polar = unroll_cone_tip(filtered_bgr, center, radius)
        bgr_patch = crop_polar_sweet_spot(bgr_polar, self.inner_crop_pct, self.outer_crop_pct)

        sig["hsv_histogram"] = compute_hs_histogram(bgr_patch)

        return sig

    @staticmethod
    def linearize_ring(bgr_image: np.ndarray) -> Optional[np.ndarray]:
        """Polar-unwrap the annular ring into a clean rectangular strip.

        Finds ring geometry from the annular-masked crop, unwraps via
        cv2.warpPolar(), crops to just the ring band, removes black rows/cols.

        Args:
            bgr_image: Annular-masked tube crop (donut shape, black bg).

        Returns:
            Clean BGR strip (~670x25) or None if ring cannot be found.
        """
        gray = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2GRAY)
        mask = (gray > 5).astype(np.uint8)
        coords = cv2.findNonZero(mask)
        if coords is None:
            return None

        x, y, w, h = cv2.boundingRect(coords)
        cx = x + w // 2
        cy = y + h // 2
        outer_r = max(w, h) // 2

        # Find inner radius (first non-zero ring from center outward)
        inner_r = 0
        for r in range(1, outer_r):
            for angle in np.linspace(0, 2 * np.pi, 16, endpoint=False):
                px = int(cx + r * np.cos(angle))
                py = int(cy + r * np.sin(angle))
                if 0 <= px < bgr_image.shape[1] and 0 <= py < bgr_image.shape[0]:
                    if mask[py, px] > 0:
                        inner_r = r
                        break
            if inner_r > 0:
                break

        if inner_r == 0 or inner_r >= outer_r:
            return None

        # Polar unwrap
        angular_res = int(2 * np.pi * (inner_r + outer_r) / 2)
        radial_res = outer_r + 5
        polar = cv2.warpPolar(
            bgr_image, dsize=(radial_res, angular_res),
            center=(cx, cy), maxRadius=radial_res,
            flags=cv2.WARP_POLAR_LINEAR,
        )
        strip = polar[:, inner_r:outer_r]

        # Remove black rows/columns
        gray_strip = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
        col_ok = np.where((gray_strip > 10).mean(axis=0) > 0.5)[0]
        row_ok = np.where((gray_strip > 10).mean(axis=1) > 0.5)[0]
        if len(col_ok) > 0 and len(row_ok) > 0:
            strip = strip[row_ok[0]:row_ok[-1] + 1, col_ok[0]:col_ok[-1] + 1]

        if strip.size == 0:
            return None

        return strip

    @staticmethod
    def extract_fft_intensity(strip_bgr: np.ndarray, n_coeffs: int = 64) -> np.ndarray:
        """Extract 1D FFT magnitude from mean intensity profile.

        Perfectly shift-invariant: rotation changes phase only, not magnitude.
        The strip's vertical axis (rows) corresponds to the angular direction
        around the ring, so the mean intensity profile captures periodic
        patterns (stripes, triangles, checks) along the ring.

        Args:
            strip_bgr: Clean linearized strip from linearize_ring().
            n_coeffs: Number of FFT coefficients to keep.

        Returns:
            L2-normalized FFT magnitude vector (n_coeffs,).
        """
        gray = cv2.cvtColor(strip_bgr, cv2.COLOR_BGR2GRAY).astype(np.float64)
        profile = gray.mean(axis=1)
        profile = profile - profile.mean()
        fft = np.fft.rfft(profile)
        magnitude = np.abs(fft)
        if len(magnitude) > n_coeffs:
            magnitude = magnitude[:n_coeffs]
        else:
            magnitude = np.pad(magnitude, (0, n_coeffs - len(magnitude)))
        return (magnitude / (np.linalg.norm(magnitude) + 1e-8)).astype(np.float32)

    def _verify_threshold(
        self, tube_crop: np.ndarray, material_id: str
    ) -> TubePatternResult:
        """Verification: compute distance to the expected template only.

        Never classifies against other templates. Computes combined distance
        (Color + FFT [+ ResNet via PatternVerifier if configured]) to the
        expected template and compares against the per-class threshold.

        Args:
            tube_crop: BGR tube crop with black background.
            material_id: Expected material ID from PLC.

        Returns:
            TubePatternResult with verification results.
        """
        template = self._templates[material_id]
        threshold = self._per_class_thresholds.get(
            material_id, self.default_threshold,
        )

        # --- 1. Color distance to expected template ---
        logger.debug("Tube verify (threshold): %dx%d crop, expected='%s'",
                     tube_crop.shape[1], tube_crop.shape[0], material_id)
        color_sig = self.compute_color_signature(tube_crop)

        color_distance = 1.0
        if color_sig is not None:
            lab_hist = color_sig["histogram"]
            lab_hist = lab_hist / (lab_hist.sum() + 1e-7)
            lab_dist = compute_bhattacharyya_distance(lab_hist, template["histogram"])

            hsv_hist = color_sig.get("hsv_histogram")
            if hsv_hist is not None and "hsv_histogram" in template:
                hsv_dist = compute_bhattacharyya_distance(hsv_hist, template["hsv_histogram"])
                color_distance = 0.7 * lab_dist + 0.3 * hsv_dist
            else:
                color_distance = lab_dist

            # Lightness penalty for disambiguation (e.g. violet vs white)
            sample_L = color_sig.get("mean_L")
            if sample_L is not None and "mean_L" in template:
                l_diff = abs(sample_L - template["mean_L"])
                l_penalty = 0.50 * (l_diff / 100.0)
                color_distance += l_penalty

            logger.debug("  Color: LAB=%.4f HSV=%.4f combined=%.4f",
                         lab_dist,
                         hsv_dist if hsv_hist is not None and "hsv_histogram" in template else 0.0,
                         color_distance)
        else:
            logger.warning("Could not extract color signature")

        # --- 2. FFT distance to expected template ---
        fft_feat = None
        fft_dist = 0.0
        strip = self.linearize_ring(tube_crop)
        if strip is not None:
            fft_feat = self.extract_fft_intensity(strip)
            if "fft_feat" in template:
                fft_dist = _cosine_distance(fft_feat, template["fft_feat"])
            logger.debug("  FFT: dist=%.4f", fft_dist)
        else:
            logger.warning("  FFT: could not linearize ring — color-only")

        # --- 3. Combined distance (hand-weighted fallback) ---
        if fft_feat is not None and "fft_feat" in template and self.fft_weight > 0:
            combined_distance = (1.0 - self.fft_weight) * color_distance + self.fft_weight * fft_dist
        else:
            combined_distance = color_distance

        # --- 4. ResNet distance to expected template (only computed if the
        # learned PatternVerifier is active — otherwise unused, skip the cost) ---
        resnet_nearest, resnet_distance, resnet_match = "", 1.0, False
        pattern_verifier_prob = None
        if self.pattern_verifier is not None and "resnet_feat" in template:
            resnet_feat = self.extract_resnet_features(tube_crop, apply_mask=False)
            resnet_distance = _cosine_distance(resnet_feat, template["resnet_feat"])
            pattern_verifier_prob = self.pattern_verifier.score(
                color_distance, fft_dist, resnet_distance,
            )

        # --- 5. Decision ---
        if pattern_verifier_prob is not None:
            # bool(...): defensive cast to a plain Python bool -- a numpy
            # scalar on either side of this comparison would otherwise leak
            # numpy.bool_ downstream into the JSON-serialized HMI payload.
            color_match = bool(pattern_verifier_prob >= self.pattern_verifier.threshold)
            logger.info(
                "Tube verify '%s': PatternVerifier prob=%.4f threshold=%.4f "
                "(color=%.4f fft=%.4f resnet=%.4f) → %s",
                material_id, pattern_verifier_prob, self.pattern_verifier.threshold,
                color_distance, fft_dist, resnet_distance,
                "PASS" if color_match else "FAIL",
            )
        else:
            color_match = bool(combined_distance <= threshold)
            logger.info(
                "Tube verify '%s': combined=%.4f threshold=%.4f → %s",
                material_id, combined_distance, threshold,
                "PASS" if color_match else "FAIL",
            )

        result = TubePatternResult(
            color_nearest=material_id,
            color_distance=color_distance,
            color_match=color_match,
            resnet_nearest=resnet_nearest,
            resnet_distance=resnet_distance,
            resnet_match=resnet_match,
            expected_class=material_id,
            reference_loaded=True,
            combined_nearest=material_id,
            combined_distance=combined_distance,
            fft_distance=fft_dist,
            verifier_probability=pattern_verifier_prob if pattern_verifier_prob is not None else 0.0,
        )

        return result

    def verify(
        self, tube_crop: np.ndarray, material_id: str
    ) -> TubePatternResult:
        """Run tube pattern verification against the expected material's
        template only. Never classifies against other materials' templates.

        Args:
            tube_crop: BGR tube crop with black background.
            material_id: Expected material ID from PLC.

        Returns:
            TubePatternResult.
        """
        # Ensure templates are loaded
        if not self._templates:
            logger.warning("No templates loaded — call load_all_references() first")
            self.load_all_references()

        if not self._templates:
            logger.error("Cannot verify tube — no templates available")
            return TubePatternResult(
                color_nearest="",
                color_distance=1.0,
                color_match=False,
                resnet_nearest="",
                resnet_distance=1.0,
                resnet_match=False,
                expected_class=material_id,
                reference_loaded=False,
            )

        if material_id not in self._templates:
            logger.error(f"Unknown material_id '{material_id}' — not in templates")
            return TubePatternResult(
                color_nearest="",
                color_distance=1.0,
                color_match=False,
                resnet_nearest="",
                resnet_distance=1.0,
                resnet_match=False,
                expected_class=material_id,
                reference_loaded=False,
            )

        return self._verify_threshold(tube_crop, material_id)

    def clear_cache(self, material_id: Optional[str] = None):
        """Clear cached templates.

        Args:
            material_id: Clear specific material. None clears all.
        """
        if material_id is None:
            self._templates.clear()
        else:
            self._templates.pop(material_id, None)
