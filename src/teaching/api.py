"""
Teaching REST API — FastAPI server for tube pattern enrollment.

Runs on the Jetson alongside the inspection pipeline. The web UI
(on the All-in-One PC) uploads full-frame images. The server runs
YOLO to extract tube ROI, then extracts features and saves references.

Endpoints:
    POST   /teach/tube              Upload N full-frame images + material_id
    GET    /teach/tube               List all taught materials
    GET    /teach/tube/{material_id} Get reference info for a material
    DELETE /teach/tube/{material_id} Remove a taught reference

Start:
    cd src && uvicorn teaching.api:app --host 0.0.0.0 --port 8001
"""

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .tube_teacher import TubeTeacher

logger = logging.getLogger(__name__)

teacher: Optional[TubeTeacher] = None


def _load_config() -> dict:
    """Load config from src/config.json."""
    config_paths = [
        Path(__file__).parent.parent / "config.json",
        Path("src/config.json"),
        Path("config.json"),
    ]
    for p in config_paths:
        if p.exists():
            with open(p) as f:
                return json.load(f)
    return {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize teacher on startup."""
    global teacher

    config = _load_config()
    insp_cfg = config.get("inspection", {})
    weights = insp_cfg.get("weights", {})
    tube_cfg = insp_cfg.get("tube_pattern", {})
    teach_cfg = config.get("teaching", {})
    exclude_cfg = tube_cfg.get("exclude_clock_range")
    exclude_enabled = tube_cfg.get("exclude_clock_range_enabled", False)

    teacher = TubeTeacher(
        yolo_weights=weights.get("visible", "weights/visible_yolo.pt"),
        yolo_conf=insp_cfg.get("yolo_conf", 0.6),
        template_dir=tube_cfg.get("template_dir", "templates/tube"),
        bilateral_d=tube_cfg.get("bilateral_d", 9),
        bilateral_sigma_color=tube_cfg.get("bilateral_sigma_color", 75),
        bilateral_sigma_space=tube_cfg.get("bilateral_sigma_space", 75),
        device=teach_cfg.get("device", "auto"),
        exclude_clock_range=tuple(exclude_cfg) if exclude_enabled and exclude_cfg else None,
        outlier_rejection_enabled=teach_cfg.get("outlier_rejection_enabled", True),
        augmentation_enabled=teach_cfg.get("augmentation_enabled", False),
        augment_count=teach_cfg.get("augment_count", 0),
    )
    logger.info("Teaching API started")
    yield
    logger.info("Teaching API shutting down")


app = FastAPI(
    title="Sieger v2 Teaching API",
    description="Tube pattern enrollment — upload full-frame images, YOLO extracts tube automatically",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Response Models ---

class TeachResponse(BaseModel):
    material_id: str
    n_frames: int
    n_tubes_detected: int
    tube_sizes: list[str]
    template_path: str
    file_size_bytes: int
    color_hist_shape: list[int]
    color_threshold: float
    resnet_n_features: int
    fft_n_features: int


class ReferenceInfo(BaseModel):
    material_id: str
    n_images: int
    template_path: str
    color_threshold: Optional[float] = None
    extend_count: int = 0
    created_at: str
    updated_at: str


class ExtendResponse(BaseModel):
    material_id: str
    n_references_before: int
    n_references_after: int
    n_new_samples: int
    old_threshold: Optional[float]
    new_threshold: float
    threshold_delta: Optional[float]
    extend_count: int
    extends_remaining: int
    template_path: str


class MessageResponse(BaseModel):
    message: str


# --- Endpoints ---

@app.post("/teach/tube", response_model=TeachResponse)
async def teach_tube(
    material_id: str = Form(..., description="Material ID from PLC"),
    images: list[UploadFile] = File(..., description="Full-frame camera images (typically 5)"),
):
    """Teach a tube pattern from uploaded full-frame images.

    YOLO runs on each image to extract the yarn_tube ROI automatically.
    Frames where YOLO fails to detect a tube are skipped.
    """
    if len(images) < 2:
        raise HTTPException(400, "Need at least 2 images")

    frames = []
    for i, upload in enumerate(images):
        contents = await upload.read()
        arr = np.frombuffer(contents, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise HTTPException(400, f"Cannot decode image {i+1}: {upload.filename}")
        frames.append(img)

    try:
        result = teacher.teach(
            frames=frames,
            material_id=material_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    return TeachResponse(**result)


@app.get("/teach/tube", response_model=list[ReferenceInfo])
async def list_references():
    """List all taught tube materials."""
    return [ReferenceInfo(**r) for r in teacher.list_references()]


@app.get("/teach/tube/{material_id}", response_model=ReferenceInfo)
async def get_reference(material_id: str):
    """Get reference info for a specific material."""
    info = teacher.get_reference_info(material_id)
    if info is None:
        raise HTTPException(404, f"No reference for material '{material_id}'")
    return ReferenceInfo(**info)


@app.post("/teach/tube/{material_id}/extend", response_model=ExtendResponse)
async def extend_tube(
    material_id: str,
    images: list[UploadFile] = File(..., description="New full-frame images to append to existing reference"),
):
    """Append new samples to an existing tube pattern reference.

    Use this when the system is producing false rejections for a known-good
    pattern — capture a few more production frames and extend the reference.
    The per-pattern threshold is recomputed from all samples (old + new).

    Capped at 3 extensions. After that, a full re-teach is required.
    Returns 409 if the extend cap has been reached.
    """
    if len(images) < 1:
        raise HTTPException(400, "Need at least 1 image")

    frames = []
    for i, upload in enumerate(images):
        contents = await upload.read()
        arr = np.frombuffer(contents, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise HTTPException(400, f"Cannot decode image {i+1}: {upload.filename}")
        frames.append(img)

    try:
        result = teacher.extend(frames=frames, material_id=material_id)
    except ValueError as e:
        msg = str(e)
        if "Extend limit reached" in msg:
            raise HTTPException(409, msg)
        raise HTTPException(400, msg)

    return ExtendResponse(**result)


@app.delete("/teach/tube/{material_id}", response_model=MessageResponse)
async def delete_reference(material_id: str):
    """Delete a taught tube reference."""
    deleted = teacher.delete_reference(material_id)
    if not deleted:
        raise HTTPException(404, f"No reference for material '{material_id}'")
    return MessageResponse(message=f"Deleted reference for '{material_id}'")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    import uvicorn
    logging.basicConfig(level=logging.INFO)

    config = _load_config()
    teach_cfg = config.get("teaching", {})

    uvicorn.run(
        "teaching.api:app",
        host=teach_cfg.get("host", "0.0.0.0"),
        port=teach_cfg.get("port", 8001),
        reload=False,
    )
