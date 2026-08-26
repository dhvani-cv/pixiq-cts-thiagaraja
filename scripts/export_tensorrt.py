"""
Export YOLO models to TensorRT FP16 engines.

MUST be run on the target device (Jetson Orin NX) — TensorRT engines are
not portable across GPU architectures. The export reads .pt weights and
produces .engine files in the same directory.

Prerequisites:
    - JetPack 6.x with TensorRT installed
    - ultralytics >= 8.4
    - YOLO .pt weights in the weights/ directory

Usage:
    # Export all 3 models
    uv run python scripts/export_tensorrt.py

    # Export a specific model
    uv run python scripts/export_tensorrt.py --model weights/visible_yolo.pt

    # Export with custom image size
    uv run python scripts/export_tensorrt.py --imgsz 640

    # FP32 instead of FP16 (debugging, not recommended for production)
    uv run python scripts/export_tensorrt.py --no-half
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# Jetson PyTorch (nv24.05) doesn't support the `dynamo` kwarg added in
# torch.onnx.export for stock PyTorch 2.1+. Strip it transparently.
import torch as _torch
_real_onnx_export = _torch.onnx.export
def _patched_onnx_export(*args, **kwargs):
    kwargs.pop("dynamo", None)
    return _real_onnx_export(*args, **kwargs)
_torch.onnx.export = _patched_onnx_export


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Default models to export — matches config.json weight paths
DEFAULT_MODELS = [
    "weights/visible_yolo.pt",
    "weights/uv_yolo.pt",
    "weights/yarn_tail_v3.pt",
]


def check_tensorrt() -> bool:
    """Verify TensorRT is available."""
    try:
        import tensorrt  # noqa: F401
        logger.info("TensorRT %s available", tensorrt.__version__)
        return True
    except ImportError:
        logger.error(
            "TensorRT not found. On Jetson, install via: "
            "sudo apt install python3-libnvinfer python3-libnvinfer-dev"
        )
        return False


def check_gpu() -> bool:
    """Verify CUDA GPU is available."""
    try:
        import torch
        if not torch.cuda.is_available():
            logger.error("CUDA not available — TensorRT export requires a GPU")
            return False
        device = torch.cuda.get_device_name(0)
        logger.info("GPU: %s", device)
        return True
    except ImportError:
        logger.error("PyTorch not installed")
        return False


def export_model(model_path: Path, imgsz: int, half: bool) -> Path:
    """Export a single YOLO model to TensorRT .engine format.

    Args:
        model_path: Path to the .pt weights file.
        imgsz: Input image size for the engine.
        half: If True, export as FP16 (recommended for Jetson).

    Returns:
        Path to the exported .engine file.

    Raises:
        FileNotFoundError: If the .pt file doesn't exist.
        RuntimeError: If export fails.
    """
    from ultralytics import YOLO

    if not model_path.exists():
        raise FileNotFoundError(f"Model not found: {model_path}")

    engine_path = model_path.with_suffix(".engine")

    if engine_path.exists():
        logger.warning("Engine already exists: %s — skipping (delete to re-export)", engine_path)
        return engine_path

    logger.info("Exporting: %s → %s (imgsz=%d, half=%s)", model_path, engine_path, imgsz, half)

    t_start = time.perf_counter()
    model = YOLO(str(model_path))
    model.export(format="engine", imgsz=imgsz, half=half, simplify=False)
    t_elapsed = time.perf_counter() - t_start

    if not engine_path.exists():
        raise RuntimeError(f"Export completed but .engine file not found at {engine_path}")

    size_mb = engine_path.stat().st_size / (1024 * 1024)
    logger.info(
        "Exported: %s (%.1f MB) in %.1f seconds",
        engine_path.name, size_mb, t_elapsed,
    )

    # Run a warm-up inference to verify the engine works
    logger.info("Verifying engine with warm-up inference...")
    import numpy as np
    engine_model = YOLO(str(engine_path))
    dummy = np.zeros((imgsz, imgsz, 3), dtype=np.uint8)
    engine_model(dummy, verbose=False)
    logger.info("Engine verification passed")

    return engine_path


def main():
    parser = argparse.ArgumentParser(
        description="Export YOLO models to TensorRT FP16 engines"
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help="Path to a specific .pt model (relative to project root). "
             "If not specified, exports all default models.",
    )
    parser.add_argument(
        "--imgsz", type=int, default=640,
        help="Input image size for the engine (default: 640)",
    )
    parser.add_argument(
        "--no-half", action="store_true",
        help="Export as FP32 instead of FP16 (not recommended for production)",
    )
    args = parser.parse_args()

    half = not args.no_half

    # Preflight checks
    if not check_gpu():
        sys.exit(1)
    if not check_tensorrt():
        sys.exit(1)

    # Determine which models to export
    if args.model:
        models = [PROJECT_ROOT / args.model]
    else:
        models = [PROJECT_ROOT / m for m in DEFAULT_MODELS]

    logger.info("Exporting %d model(s) to TensorRT %s", len(models), "FP16" if half else "FP32")

    results = []
    for model_path in models:
        try:
            engine_path = export_model(model_path, args.imgsz, half)
            results.append((model_path.name, "OK", engine_path))
        except Exception as e:
            logger.error("Failed to export %s: %s", model_path, e)
            results.append((model_path.name, "FAILED", str(e)))

    # Summary
    print("\n" + "=" * 60)
    print("Export Summary")
    print("=" * 60)
    for name, status, detail in results:
        print(f"  {name:30s} {status:8s} {detail}")
    print("=" * 60)

    failed = sum(1 for _, s, _ in results if s == "FAILED")
    if failed:
        logger.error("%d model(s) failed to export", failed)
        sys.exit(1)
    else:
        logger.info("All models exported successfully")


if __name__ == "__main__":
    main()
