#!/usr/bin/env python3
"""Debug UV inspection failures on a folder of images.

This mirrors the UVInspection radial log(G/B) logic and saves the intermediate
artifacts that explain why a frame passes, fails, or gets skipped:

- YOLO overlay with cone/tube boxes
- cone crop
- annular mask
- valid-pixel mask after B/G filtering
- masked UV annulus
- radial profile chart
- CSV/JSONL summary

Example:
    uv run python scripts/debug_uv_inspection.py \
      --input images \
      --output scripts/uv_debug \
      --model weights/uv_v1.pt
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from inspection.data_types import Detection  # noqa: E402
from inspection.yolo_detector import YOLODetector  # noqa: E402


LOG = logging.getLogger("debug_uv_inspection")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
RADIAL_BINS = 100


def _find_detection(detections: list[Detection], class_name: str) -> Detection | None:
    for det in detections:
        if det.class_name == class_name:
            return det
    return None


def _bbox_to_list(det: Detection | None) -> list[int] | None:
    return [int(v) for v in det.bbox] if det is not None else None


def _clamp_bbox(bbox: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = (int(v) for v in bbox)
    x1 = max(0, min(x1, width))
    y1 = max(0, min(y1, height))
    x2 = max(0, min(x2, width))
    y2 = max(0, min(y2, height))
    return x1, y1, x2, y2


def _iter_images(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    return sorted(p for p in input_path.rglob("*") if p.suffix.lower() in IMAGE_EXTS)


def _draw_overlay(frame: np.ndarray, detections: list[Detection], path: Path) -> None:
    overlay = frame.copy()
    colors = {
        "yarn_cone": (0, 255, 255),
        "yarn_tube": (255, 0, 255),
        "cone": (0, 255, 255),
        "tube": (255, 0, 255),
    }
    for det in detections:
        x1, y1, x2, y2 = (int(v) for v in det.bbox)
        color = colors.get(det.class_name, (0, 255, 0))
        cv2.rectangle(overlay, (x1, y1), (x2, y2), color, 2)
        label = f"{det.class_name} {det.confidence:.2f}"
        cv2.putText(
            overlay,
            label,
            (x1, max(16, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
            cv2.LINE_AA,
        )
    cv2.imwrite(str(path), overlay)


def _save_profile_chart(
    bin_profile: np.ndarray,
    baseline: np.ndarray | None,
    valid_bins: np.ndarray,
    max_dip: float | None,
    threshold: float,
    path: Path,
) -> None:
    chart_h, chart_w = 360, 720
    margin = 40
    chart = np.full((chart_h, chart_w, 3), 255, dtype=np.uint8)
    cv2.rectangle(chart, (margin, margin), (chart_w - margin, chart_h - margin), (220, 220, 220), 1)

    y_values = bin_profile[valid_bins]
    if y_values.size == 0:
        cv2.putText(chart, "No valid radial bins", (60, 180), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        cv2.imwrite(str(path), chart)
        return

    if baseline is not None and baseline.size:
        y_values = np.concatenate([y_values, baseline])

    y_min = float(np.nanmin(y_values))
    y_max = float(np.nanmax(y_values))
    if abs(y_max - y_min) < 1e-6:
        y_max = y_min + 1.0

    def to_point(idx: int, value: float) -> tuple[int, int]:
        x = margin + int(idx / (RADIAL_BINS - 1) * (chart_w - 2 * margin))
        y = chart_h - margin - int((value - y_min) / (y_max - y_min) * (chart_h - 2 * margin))
        return x, y

    valid_indices = np.where(valid_bins)[0]
    profile_points = [to_point(int(i), float(bin_profile[i])) for i in valid_indices]
    if len(profile_points) >= 2:
        cv2.polylines(chart, [np.array(profile_points, dtype=np.int32)], False, (0, 120, 255), 2)

    if baseline is not None and baseline.size:
        baseline_points = [to_point(int(i), float(v)) for i, v in zip(valid_indices, baseline)]
        if len(baseline_points) >= 2:
            cv2.polylines(chart, [np.array(baseline_points, dtype=np.int32)], False, (0, 160, 0), 2)

    label = f"max_dip={max_dip:.4f} threshold={threshold:.4f}" if max_dip is not None else "max_dip=N/A"
    cv2.putText(chart, label, (margin, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (30, 30, 30), 2)
    cv2.putText(chart, "orange=log(G/B), green=baseline", (margin, chart_h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (80, 80, 80), 1)
    cv2.imwrite(str(path), chart)


def _debug_radial_dip(
    frame: np.ndarray,
    cone_bbox: tuple[int, int, int, int],
    tube_bbox: tuple[int, int, int, int],
    *,
    outer_margin: float,
) -> dict[str, Any]:
    h, w = frame.shape[:2]
    cx1, cy1, cx2, cy2 = _clamp_bbox(cone_bbox, w, h)
    cone_crop = frame[cy1:cy2, cx1:cx2].copy()

    result: dict[str, Any] = {
        "reason": "",
        "cone_crop": cone_crop,
        "annular_mask": None,
        "valid_mask": None,
        "masked_annulus": None,
        "profile": np.full(RADIAL_BINS, np.nan, dtype=np.float32),
        "valid_bins_mask": np.zeros(RADIAL_BINS, dtype=bool),
        "baseline": None,
        "max_dip": None,
        "gb_mean": None,
        "inner_r": None,
        "outer_r": None,
        "annular_pixels": 0,
        "valid_pixels": 0,
        "valid_bins": 0,
    }

    if cone_crop.size == 0:
        result["reason"] = "empty cone crop"
        return result

    tx1, ty1, tx2, ty2 = (int(v) for v in tube_bbox)
    tube_cx = (tx1 + tx2) // 2 - cx1
    tube_cy = (ty1 + ty2) // 2 - cy1
    inner_r = float(min(tx2 - tx1, ty2 - ty1)) / 2.0
    outer_r = float(min(cx2 - cx1, cy2 - cy1)) / 2.0 * (1.0 - outer_margin)
    result["inner_r"] = round(inner_r, 2)
    result["outer_r"] = round(outer_r, 2)

    if outer_r <= inner_r or outer_r <= 0:
        result["reason"] = f"invalid geometry inner={inner_r:.1f} outer={outer_r:.1f}"
        return result

    ch, cw = cone_crop.shape[:2]
    y_grid, x_grid = np.ogrid[:ch, :cw]
    dist = np.sqrt((x_grid - tube_cx) ** 2 + (y_grid - tube_cy) ** 2).astype(np.float32)

    annular = (dist >= inner_r) & (dist <= outer_r)
    result["annular_pixels"] = int(annular.sum())

    b = cone_crop[:, :, 0].astype(np.float32)
    g = cone_crop[:, :, 1].astype(np.float32)
    valid = annular & (b > 5) & (g > 0)
    result["valid_pixels"] = int(valid.sum())

    annular_mask = np.zeros((ch, cw), dtype=np.uint8)
    annular_mask[annular] = 255
    valid_mask = np.zeros((ch, cw), dtype=np.uint8)
    valid_mask[valid] = 255
    masked_annulus = np.zeros_like(cone_crop)
    masked_annulus[valid] = cone_crop[valid]

    result["annular_mask"] = annular_mask
    result["valid_mask"] = valid_mask
    result["masked_annulus"] = masked_annulus

    if valid.sum() < 100:
        result["reason"] = f"too few valid annular pixels ({int(valid.sum())})"
        return result

    dist_norm = (dist[valid] - inner_r) / (outer_r - inner_r)
    b_valid = b[valid]
    g_valid = g[valid]
    log_gb = np.log(g_valid / b_valid)

    bin_edges = np.linspace(0.0, 1.0, RADIAL_BINS + 1)
    bin_profile = np.full(RADIAL_BINS, np.nan, dtype=np.float32)
    bin_counts = np.zeros(RADIAL_BINS, dtype=np.int32)
    for i in range(RADIAL_BINS):
        mask = (dist_norm >= bin_edges[i]) & (dist_norm < bin_edges[i + 1])
        bin_counts[i] = int(mask.sum())
        if mask.sum() < 10:
            continue
        bin_profile[i] = float(log_gb[mask].mean())

    valid_bins = ~np.isnan(bin_profile)
    result["profile"] = bin_profile
    result["bin_counts"] = bin_counts
    result["valid_bins_mask"] = valid_bins
    result["valid_bins"] = int(valid_bins.sum())

    if valid_bins.sum() < 20:
        result["reason"] = f"too few valid radial bins ({int(valid_bins.sum())})"
        return result

    x = np.arange(RADIAL_BINS)[valid_bins].astype(np.float32)
    y = bin_profile[valid_bins]
    coeffs = np.polyfit(x, y, deg=2)
    baseline = np.polyval(coeffs, x)
    deviation = y - baseline
    max_dip = float(-deviation.min())
    gb_mean = float((g_valid / b_valid).mean())

    result["baseline"] = baseline
    result["max_dip"] = max_dip
    result["gb_mean"] = gb_mean
    result["reason"] = "ok"
    return result


def _write_profile_csv(path: Path, profile: np.ndarray, bin_counts: np.ndarray | None, valid_bins: np.ndarray) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["bin", "pixel_count", "valid", "log_gb"])
        writer.writeheader()
        for i, value in enumerate(profile):
            writer.writerow(
                {
                    "bin": i,
                    "pixel_count": int(bin_counts[i]) if bin_counts is not None else 0,
                    "valid": bool(valid_bins[i]),
                    "log_gb": "" if np.isnan(value) else float(value),
                }
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Debug UV radial dip inspection on image folders.")
    parser.add_argument(
        "--input",
        default=str(REPO_ROOT / "images"),
        help="Input image file/folder. Default: cone-transport-system-pixiq/images",
    )
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "scripts" / "uv_debug"),
        help="Output debug folder. Default: scripts/uv_debug",
    )
    parser.add_argument(
        "--model",
        default=str(REPO_ROOT / "weights" / "uv_v1.pt"),
        help="UV YOLO model path. Default: weights/uv_v1.pt",
    )
    parser.add_argument("--conf", type=float, default=0.3, help="YOLO confidence threshold.")
    parser.add_argument("--imgsz", type=int, default=640, help="YOLO inference image size.")
    parser.add_argument("--outer-margin", type=float, default=0.10, help="Outer radius shrink fraction.")
    parser.add_argument("--radial-dip-threshold", type=float, default=0.024, help="Mixup threshold.")
    parser.add_argument("--no-padding", action="store_true", help="Disable 1.6 aspect-ratio padding.")
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    args = _build_parser().parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output)
    overlay_dir = output_dir / "overlays"
    crop_dir = output_dir / "cone_crops"
    mask_dir = output_dir / "masks"
    profile_dir = output_dir / "profiles"
    result_dir = output_dir / "results"
    for directory in (overlay_dir, crop_dir, mask_dir, profile_dir, result_dir):
        directory.mkdir(parents=True, exist_ok=True)

    images = _iter_images(input_path)
    if not images:
        LOG.error("No images found under %s", input_path)
        return 1

    detector = YOLODetector(
        model_path=args.model,
        conf_threshold=args.conf,
        imgsz=args.imgsz,
        use_padding=not args.no_padding,
    )

    rows: list[dict[str, Any]] = []
    for idx, image_path in enumerate(images, start=1):
        frame = cv2.imread(str(image_path))
        if frame is None:
            LOG.warning("Could not read %s", image_path)
            continue

        detections = detector.detect(frame)
        cone_det = _find_detection(detections, "yarn_cone")
        tube_det = _find_detection(detections, "yarn_tube")

        stem = image_path.stem
        _draw_overlay(frame, detections, overlay_dir / f"{stem}_overlay.jpg")

        row: dict[str, Any] = {
            "image": str(image_path),
            "cone_conf": round(cone_det.confidence, 4) if cone_det else None,
            "tube_conf": round(tube_det.confidence, 4) if tube_det else None,
            "cone_bbox": _bbox_to_list(cone_det),
            "tube_bbox": _bbox_to_list(tube_det),
            "inner_r": None,
            "outer_r": None,
            "annular_pixels": 0,
            "valid_pixels": 0,
            "valid_bins": 0,
            "radial_dip": None,
            "gb_mean": None,
            "threshold": args.radial_dip_threshold,
            "result": "SKIP",
            "reason": "",
        }

        if cone_det is None:
            row["reason"] = "no cone detected"
        elif tube_det is None:
            row["reason"] = "no tube detected"
        else:
            debug = _debug_radial_dip(
                frame,
                cone_det.bbox,
                tube_det.bbox,
                outer_margin=args.outer_margin,
            )

            if debug["cone_crop"] is not None and debug["cone_crop"].size:
                cv2.imwrite(str(crop_dir / f"{stem}_cone_crop.jpg"), debug["cone_crop"])
            if debug["annular_mask"] is not None:
                cv2.imwrite(str(mask_dir / f"{stem}_annular_mask.png"), debug["annular_mask"])
            if debug["valid_mask"] is not None:
                cv2.imwrite(str(mask_dir / f"{stem}_valid_mask.png"), debug["valid_mask"])
            if debug["masked_annulus"] is not None:
                cv2.imwrite(str(mask_dir / f"{stem}_masked_annulus.jpg"), debug["masked_annulus"])

            _write_profile_csv(
                profile_dir / f"{stem}_profile.csv",
                debug["profile"],
                debug.get("bin_counts"),
                debug["valid_bins_mask"],
            )
            _save_profile_chart(
                debug["profile"],
                debug["baseline"],
                debug["valid_bins_mask"],
                debug["max_dip"],
                args.radial_dip_threshold,
                profile_dir / f"{stem}_profile.jpg",
            )

            row.update(
                {
                    "inner_r": debug["inner_r"],
                    "outer_r": debug["outer_r"],
                    "annular_pixels": debug["annular_pixels"],
                    "valid_pixels": debug["valid_pixels"],
                    "valid_bins": debug["valid_bins"],
                    "radial_dip": round(debug["max_dip"], 6) if debug["max_dip"] is not None else None,
                    "gb_mean": round(debug["gb_mean"], 6) if debug["gb_mean"] is not None else None,
                    "reason": debug["reason"],
                }
            )
            if debug["max_dip"] is not None:
                row["result"] = "MIXUP" if debug["max_dip"] > args.radial_dip_threshold else "OK"

        rows.append(row)
        LOG.info(
            "%03d/%03d %s | cone=%s tube=%s inner=%s outer=%s valid_px=%s valid_bins=%s dip=%s result=%s reason=%s",
            idx,
            len(images),
            image_path.name,
            row["cone_conf"],
            row["tube_conf"],
            row["inner_r"],
            row["outer_r"],
            row["valid_pixels"],
            row["valid_bins"],
            row["radial_dip"],
            row["result"],
            row["reason"],
        )

    csv_path = result_dir / "uv_debug_results.csv"
    jsonl_path = result_dir / "uv_debug_results.jsonl"
    fieldnames = [
        "image",
        "cone_conf",
        "tube_conf",
        "cone_bbox",
        "tube_bbox",
        "inner_r",
        "outer_r",
        "annular_pixels",
        "valid_pixels",
        "valid_bins",
        "radial_dip",
        "gb_mean",
        "threshold",
        "result",
        "reason",
    ]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    with jsonl_path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    skipped = sum(1 for row in rows if row["result"] == "SKIP")
    mixup = sum(1 for row in rows if row["result"] == "MIXUP")
    ok = sum(1 for row in rows if row["result"] == "OK")
    LOG.info("Done: %s images | OK=%s MIXUP=%s SKIP=%s", len(rows), ok, mixup, skipped)
    LOG.info("CSV: %s", csv_path)
    LOG.info("Debug images: %s", output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
