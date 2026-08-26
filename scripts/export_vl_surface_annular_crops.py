#!/usr/bin/env python3
"""Export VL annular crops for cone, tube, and surface detections.

This is an offline inspection/debug utility. It mirrors the annular crop
style used by the VL stain/data-capture code:

- cone: use cone bbox as the outer crop and tube bbox as the inner hole
- surface: use surface bbox as the outer crop and tube bbox as the inner hole
- tube: use YOLODetector.extract_annular_roi(), same as tube-pattern crop

Example:
    uv run python scripts/export_vl_surface_annular_crops.py \
      --input /path/to/vl_images \
      --output /tmp/vl_annular_debug
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

import cv2
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from inspection.data_types import Detection  # noqa: E402
from inspection.yolo_detector import YOLODetector  # noqa: E402


LOG = logging.getLogger("export_vl_surface_annular_crops")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}


def _find_detection(detections: list[Detection], needle: str) -> Detection | None:
    """Return the first detection whose class name contains *needle*."""
    needle = needle.lower()
    for det in detections:
        if needle in det.class_name.lower():
            return det
    return None


def _clamp_bbox(bbox: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = (int(v) for v in bbox)
    x1 = max(0, min(x1, width))
    x2 = max(0, min(x2, width))
    y1 = max(0, min(y1, height))
    y2 = max(0, min(y2, height))
    return x1, y1, x2, y2


def _bbox_to_list(det: Detection | None) -> list[int] | None:
    return [int(v) for v in det.bbox] if det is not None else None


def _bbox_diameter_mm(det: Detection | None, pixels_per_mm: float) -> float:
    if det is None or pixels_per_mm <= 0:
        return 0.0
    x1, y1, x2, y2 = (int(v) for v in det.bbox)
    diameter_px = min(x2 - x1, y2 - y1)
    if diameter_px <= 0:
        return 0.0
    return round(float(diameter_px) / pixels_per_mm, 1)


def _load_recipe(recipe_dir: Path, material_id: str) -> dict | None:
    if not material_id:
        return None
    path = recipe_dir / f"{material_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        LOG.warning("Could not read recipe %s: %s", path, exc)
        return None


def _within_tolerance(value: float, reference: float, tolerance: float) -> bool | None:
    if value <= 0 or reference <= 0 or tolerance <= 0:
        return None
    return abs(value - reference) <= tolerance


def _material_id_for_image(image_path: Path, input_dir: Path, args: argparse.Namespace) -> str:
    if args.material_id:
        return str(args.material_id)
    if not args.material_from_parent:
        return ""
    try:
        relative = image_path.relative_to(input_dir)
        if len(relative.parts) > 1:
            return relative.parts[0]
    except ValueError:
        pass
    return image_path.parent.name


def _crop_with_geometry(
    frame: np.ndarray,
    outer_det: Detection,
    tube_det: Detection | None,
    *,
    fallback_inner_ratio: float,
    outer_scale: float = 1.0,
    inner_scale: float = 1.0,
) -> tuple[np.ndarray, tuple[int, int], float, float] | None:
    """Return outer crop plus annular geometry for StainDetector.detect()."""
    frame_h, frame_w = frame.shape[:2]
    x1, y1, x2, y2 = _clamp_bbox(outer_det.bbox, frame_w, frame_h)
    if x2 <= x1 or y2 <= y1:
        return None

    crop = frame[y1:y2, x1:x2].copy()
    if crop.size == 0:
        return None

    crop_h, crop_w = crop.shape[:2]
    center = (int(crop_w / 2), int(crop_h / 2))
    outer_r = float(min(crop_w, crop_h)) / 2.0 * outer_scale

    if tube_det is not None:
        tx1, ty1, tx2, ty2 = (int(v) for v in tube_det.bbox)
        inner_r = float(min(tx2 - tx1, ty2 - ty1)) / 2.0 * inner_scale
    else:
        inner_r = float(min(crop_w, crop_h)) * fallback_inner_ratio * inner_scale

    return crop, center, inner_r, outer_r


def _annular_crop_from_outer(
    frame: np.ndarray,
    outer_det: Detection,
    tube_det: Detection | None,
    *,
    output_size: int,
    fallback_inner_ratio: float,
    outer_scale: float = 1.0,
    inner_scale: float = 1.0,
) -> np.ndarray | None:
    """Match the service stain crop logic for cone/surface annular crops."""
    frame_h, frame_w = frame.shape[:2]
    x1, y1, x2, y2 = _clamp_bbox(outer_det.bbox, frame_w, frame_h)
    if x2 <= x1 or y2 <= y1:
        return None

    crop = frame[y1:y2, x1:x2].copy()
    if crop.size == 0:
        return None

    crop_h, crop_w = crop.shape[:2]
    outer_r = float(min(crop_w, crop_h)) / 2.0 * outer_scale
    center_x = int(crop_w / 2)
    center_y = int(crop_h / 2)

    if tube_det is not None:
        tx1, ty1, tx2, ty2 = (int(v) for v in tube_det.bbox)
        inner_r = float(min(tx2 - tx1, ty2 - ty1)) / 2.0 * inner_scale
    else:
        inner_r = float(min(crop_w, crop_h)) * fallback_inner_ratio * inner_scale

    y_grid, x_grid = np.ogrid[:crop_h, :crop_w]
    dist = np.sqrt((x_grid - center_x) ** 2 + (y_grid - center_y) ** 2)
    mask = ((dist >= inner_r) & (dist <= outer_r)).astype(np.uint8) * 255

    crop[mask == 0] = 0
    return cv2.resize(crop, (output_size, output_size), interpolation=cv2.INTER_AREA)


def _draw_overlay(frame: np.ndarray, detections: list[Detection]) -> np.ndarray:
    overlay = frame.copy()
    colors = {
        "yarn_cone": (0, 255, 0),
        "yarn_tube": (255, 255, 0),
        "surface": (0, 128, 255),
    }
    for det in detections:
        x1, y1, x2, y2 = (int(v) for v in det.bbox)
        color = colors.get(det.class_name, (255, 255, 255))
        cv2.rectangle(overlay, (x1, y1), (x2, y2), color, 2)
        label = f"{det.class_name} {det.confidence:.2f}"
        cv2.putText(
            overlay,
            label,
            (x1, max(20, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
            cv2.LINE_AA,
        )
    return overlay


def export_crops(args: argparse.Namespace) -> int:
    input_dir = Path(args.input)
    output_dir = Path(args.output)
    model_path = Path(args.model)
    recipe_dir = Path(args.recipe_dir)
    template_dir = Path(args.template_dir)

    if not input_dir.exists():
        raise FileNotFoundError(f"Input folder does not exist: {input_dir}")
    if not model_path.exists():
        raise FileNotFoundError(f"Model file does not exist: {model_path}")

    crop_dirs = {
        "cone": output_dir / "cone_annular",
        "tube": output_dir / "tube_annular",
        "surface": output_dir / "surface_annular",
        "overlay": output_dir / "overlays",
        "results": output_dir / "results",
        "stain_heatmap": output_dir / "stain_heatmaps",
    }
    for path in crop_dirs.values():
        path.mkdir(parents=True, exist_ok=True)

    detector = YOLODetector(
        str(model_path),
        conf_threshold=args.conf,
        imgsz=args.imgsz,
        use_padding=not args.no_padding,
    )

    images = sorted(p for p in input_dir.rglob("*") if p.suffix.lower() in IMAGE_EXTS)
    if not images:
        LOG.warning("No images found in %s", input_dir)
        return 1

    stain_detector = None
    if not args.skip_stain:
        try:
            from inspection.stain_detector import StainDetector

            stain_detector = StainDetector(
                model_path=str(Path(args.stain_model)),
                threshold=args.stain_threshold,
            )
            LOG.info(
                "Stain detector initialized: model=%s threshold=%.4f",
                args.stain_model,
                args.stain_threshold,
            )
        except Exception as exc:
            LOG.warning("Stain detector unavailable, continuing without stain scores: %s", exc)

    counts = {"cone": 0, "tube": 0, "surface": 0, "skipped": 0}
    result_rows: list[dict] = []

    for image_path in images:
        frame = cv2.imread(str(image_path))
        if frame is None:
            LOG.warning("Could not read image: %s", image_path)
            counts["skipped"] += 1
            continue

        detections = detector.detect(frame)
        cone_det = _find_detection(detections, "cone")
        tube_det = _find_detection(detections, "tube")
        surface_det = _find_detection(detections, "surface")
        material_id = _material_id_for_image(image_path, input_dir, args)
        recipe = _load_recipe(recipe_dir, material_id)
        master_name = str(recipe.get("master_name", material_id)) if recipe else material_id
        template_path = template_dir / f"{master_name}.npz" if master_name else None

        dimension_det = cone_det
        dimension_source = "cone"
        if args.dimension_outer_class == "surface":
            dimension_det = surface_det or cone_det
            dimension_source = "surface" if surface_det is not None else "cone_fallback"

        cone_bbox_dia_mm = _bbox_diameter_mm(cone_det, args.pixels_per_mm)
        surface_bbox_dia_mm = _bbox_diameter_mm(surface_det, args.pixels_per_mm)
        selected_cone_dia_mm = _bbox_diameter_mm(dimension_det, args.pixels_per_mm)
        tube_dia_mm = _bbox_diameter_mm(tube_det, args.pixels_per_mm)
        recipe_cone_dia = float(recipe.get("cone_diameter_mm", 0.0) or 0.0) if recipe else 0.0
        recipe_tube_dia = float(recipe.get("tube_diameter_mm", 0.0) or 0.0) if recipe else 0.0
        recipe_cone_tol = float(recipe.get("cone_tolerance_mm", 0.0) or 0.0) if recipe else 0.0
        recipe_tube_tol = float(recipe.get("tube_tolerance_mm", 0.0) or 0.0) if recipe else 0.0
        cone_bbox_ok = _within_tolerance(cone_bbox_dia_mm, recipe_cone_dia, recipe_cone_tol)
        surface_bbox_ok = _within_tolerance(surface_bbox_dia_mm, recipe_cone_dia, recipe_cone_tol)
        selected_cone_ok = _within_tolerance(selected_cone_dia_mm, recipe_cone_dia, recipe_cone_tol)
        tube_ok = _within_tolerance(tube_dia_mm, recipe_tube_dia, recipe_tube_tol)
        stain_source = ""
        stain_score = None
        stain_has_stain = None

        if args.save_overlay:
            overlay = _draw_overlay(frame, detections)
            cv2.imwrite(str(crop_dirs["overlay"] / f"{image_path.stem}_overlay.jpg"), overlay)

        stem = image_path.stem

        if cone_det is not None:
            crop = _annular_crop_from_outer(
                frame,
                cone_det,
                tube_det,
                output_size=args.size,
                fallback_inner_ratio=args.fallback_inner_ratio,
            )
            if crop is not None:
                cv2.imwrite(str(crop_dirs["cone"] / f"{stem}_cone_annular.jpg"), crop)
                counts["cone"] += 1

        if tube_det is not None:
            crop = detector.extract_annular_roi(
                frame,
                tube_det,
                inner_ratio=args.tube_inner_ratio,
                outer_margin=args.tube_outer_margin,
            )
            if crop is not None and crop.size:
                crop = cv2.resize(crop, (args.size, args.size), interpolation=cv2.INTER_AREA)
                cv2.imwrite(str(crop_dirs["tube"] / f"{stem}_tube_annular.jpg"), crop)
                counts["tube"] += 1

        if surface_det is not None:
            crop = _annular_crop_from_outer(
                frame,
                surface_det,
                tube_det,
                output_size=args.size,
                fallback_inner_ratio=args.fallback_inner_ratio,
                outer_scale=args.surface_outer_scale,
                inner_scale=args.surface_inner_scale,
            )
            if crop is not None:
                cv2.imwrite(str(crop_dirs["surface"] / f"{stem}_surface_annular.jpg"), crop)
                counts["surface"] += 1

        if stain_detector is not None:
            stain_det = cone_det
            stain_source = "cone"
            stain_outer_scale = 1.0
            stain_inner_scale = 1.0
            if args.stain_source == "surface":
                stain_det = surface_det or cone_det
                stain_source = "surface" if surface_det is not None else "cone_fallback"
                stain_outer_scale = args.surface_outer_scale if surface_det is not None else 1.0
                stain_inner_scale = args.surface_inner_scale if surface_det is not None else 1.0

            if stain_det is not None:
                stain_input = _crop_with_geometry(
                    frame,
                    stain_det,
                    tube_det,
                    fallback_inner_ratio=args.fallback_inner_ratio,
                    outer_scale=stain_outer_scale,
                    inner_scale=stain_inner_scale,
                )
                if stain_input is not None:
                    crop, center, inner_r, outer_r = stain_input
                    try:
                        stain_result = stain_detector.detect(
                            crop,
                            center=center,
                            inner_r=inner_r,
                            outer_r=outer_r,
                        )
                        stain_score = float(stain_result.anomaly_score)
                        stain_has_stain = bool(stain_result.has_stain)
                        if stain_result.heatmap is not None:
                            heatmap = stain_result.heatmap.astype(np.float32)
                            heatmap -= float(heatmap.min())
                            max_val = float(heatmap.max())
                            if max_val > 0:
                                heatmap /= max_val
                            heatmap_u8 = np.uint8(heatmap * 255)
                            heatmap_color = cv2.applyColorMap(heatmap_u8, cv2.COLORMAP_JET)
                            cv2.imwrite(
                                str(crop_dirs["stain_heatmap"] / f"{stem}_stain_heatmap.jpg"),
                                heatmap_color,
                            )
                    except Exception as exc:
                        LOG.warning("Stain detection failed for %s: %s", image_path.name, exc)

        if cone_det is None and tube_det is None and surface_det is None:
            counts["skipped"] += 1
            LOG.warning("No cone/tube/surface detection: %s", image_path.name)

        row = {
            "image": str(image_path),
            "material_id": material_id,
            "master_name": master_name,
            "recipe_found": recipe is not None,
            "template_path": str(template_path) if template_path else "",
            "template_exists": bool(template_path and template_path.exists()),
            "cone_conf": round(cone_det.confidence, 4) if cone_det else None,
            "tube_conf": round(tube_det.confidence, 4) if tube_det else None,
            "surface_conf": round(surface_det.confidence, 4) if surface_det else None,
            "cone_bbox": _bbox_to_list(cone_det),
            "tube_bbox": _bbox_to_list(tube_det),
            "surface_bbox": _bbox_to_list(surface_det),
            "dimension_source": dimension_source if dimension_det is not None else "",
            "cone_bbox_diameter_mm": cone_bbox_dia_mm,
            "surface_bbox_diameter_mm": surface_bbox_dia_mm,
            "selected_cone_diameter_mm": selected_cone_dia_mm,
            "tube_diameter_mm": tube_dia_mm,
            "recipe_cone_diameter_mm": recipe_cone_dia,
            "recipe_tube_diameter_mm": recipe_tube_dia,
            "recipe_cone_tolerance_mm": recipe_cone_tol,
            "recipe_tube_tolerance_mm": recipe_tube_tol,
            "cone_bbox_recipe_ok": cone_bbox_ok,
            "surface_bbox_recipe_ok": surface_bbox_ok,
            "selected_cone_recipe_ok": selected_cone_ok,
            "tube_recipe_ok": tube_ok,
            "pixels_per_mm": args.pixels_per_mm,
            "stain_source": stain_source,
            "stain_score": round(stain_score, 4) if stain_score is not None else None,
            "stain_has_stain": stain_has_stain,
        }
        result_rows.append(row)
        LOG.info(
            "%s | material=%s template=%s | conf cone=%s tube=%s surface=%s | cone/tube=%.1f/%.1fmm surface/tube=%.1f/%.1fmm selected=%s %.1fmm | recipe cone=%.1f±%.1f tube=%.1f±%.1f ok cone=%s surface=%s selected=%s tube=%s | stain_source=%s score=%s stain=%s",
            image_path.name,
            material_id or "NA",
            "YES" if row["template_exists"] else "NO",
            f"{cone_det.confidence:.2f}" if cone_det else "NA",
            f"{tube_det.confidence:.2f}" if tube_det else "NA",
            f"{surface_det.confidence:.2f}" if surface_det else "NA",
            cone_bbox_dia_mm,
            tube_dia_mm,
            surface_bbox_dia_mm,
            tube_dia_mm,
            row["dimension_source"] or "NA",
            selected_cone_dia_mm,
            recipe_cone_dia,
            recipe_cone_tol,
            recipe_tube_dia,
            recipe_tube_tol,
            cone_bbox_ok,
            surface_bbox_ok,
            selected_cone_ok,
            tube_ok,
            stain_source or "NA",
            f"{stain_score:.4f}" if stain_score is not None else "NA",
            stain_has_stain if stain_has_stain is not None else "NA",
        )

    csv_path = crop_dirs["results"] / "vl_debug_results.csv"
    jsonl_path = crop_dirs["results"] / "vl_debug_results.jsonl"
    if result_rows:
        with csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(result_rows[0].keys()))
            writer.writeheader()
            writer.writerows(result_rows)
        with jsonl_path.open("w") as f:
            for row in result_rows:
                f.write(json.dumps(row) + "\n")

    LOG.info(
        "Done. images=%d cone=%d tube=%d surface=%d skipped=%d output=%s results=%s",
        len(images),
        counts["cone"],
        counts["tube"],
        counts["surface"],
        counts["skipped"],
        output_dir,
        csv_path,
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export VL annular crops for cone, tube, and surface classes.",
    )
    parser.add_argument("--input", required=True, help="Input folder containing VL images.")
    parser.add_argument(
        "--output",
        required=True,
        help="Output folder. Subfolders are created under this path.",
    )
    parser.add_argument(
        "--model",
        default=str(REPO_ROOT / "weights" / "vl_Surface.pt"),
        help="YOLO model path. Default: weights/vl_Surface.pt",
    )
    parser.add_argument(
        "--recipe-dir",
        default=str(REPO_ROOT / "data" / "recipes"),
        help="Recipe JSON directory for material-wise dimension comparison.",
    )
    parser.add_argument(
        "--template-dir",
        default=str(REPO_ROOT / "data" / "templates" / "tube"),
        help="Tube template directory used to check whether master/template exists.",
    )
    parser.add_argument(
        "--material-id",
        default="",
        help="Material ID to apply to all input images for recipe/template comparison.",
    )
    parser.add_argument(
        "--material-from-parent",
        action="store_true",
        help="Infer material ID from each image's parent folder under --input.",
    )
    parser.add_argument("--conf", type=float, default=0.30, help="YOLO confidence threshold.")
    parser.add_argument("--imgsz", type=int, default=640, help="YOLO inference image size.")
    parser.add_argument("--size", type=int, default=256, help="Saved crop size in pixels.")
    parser.add_argument(
        "--pixels-per-mm",
        type=float,
        default=4.52,
        help="VL calibration factor used for cone/tube diameter measurement.",
    )
    parser.add_argument(
        "--dimension-outer-class",
        choices=("surface", "cone"),
        default="surface",
        help="Class used for cone diameter measurement; surface falls back to cone.",
    )
    parser.add_argument(
        "--tube-inner-ratio",
        type=float,
        default=0.80,
        help="Inner hole ratio for tube annular crop.",
    )
    parser.add_argument(
        "--tube-outer-margin",
        type=float,
        default=0.05,
        help="Outer margin for tube annular crop.",
    )
    parser.add_argument(
        "--fallback-inner-ratio",
        type=float,
        default=0.15,
        help="Fallback inner radius ratio for cone/surface crops when tube is missing.",
    )
    parser.add_argument(
        "--surface-outer-scale",
        type=float,
        default=1.0,
        help="Scale surface outer radius. Lower than 1.0 removes outer background.",
    )
    parser.add_argument(
        "--surface-inner-scale",
        type=float,
        default=1.0,
        help="Scale surface inner radius. Higher than 1.0 removes more tube/center.",
    )
    parser.add_argument(
        "--stain-source",
        choices=("surface", "cone"),
        default="surface",
        help="Class used as stain outer crop; surface falls back to cone.",
    )
    parser.add_argument(
        "--stain-model",
        default=str(REPO_ROOT / "models" / "patchcore"),
        help="PatchCore model directory. Default: models/patchcore",
    )
    parser.add_argument(
        "--stain-threshold",
        type=float,
        default=0.5,
        help="PatchCore/fallback stain threshold.",
    )
    parser.add_argument(
        "--skip-stain",
        action="store_true",
        help="Skip stain detection and only export crops/dimensions.",
    )
    parser.add_argument(
        "--no-padding",
        action="store_true",
        help="Disable 1.6 aspect-ratio padding before YOLO inference.",
    )
    parser.add_argument(
        "--save-overlay",
        action="store_true",
        help="Also save detection overlays for debugging.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    return export_crops(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
