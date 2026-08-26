"""
Pydantic models for API request/response validation.

Matches the old Flask API request formats for frontend compatibility.
"""

from typing import Optional
from typing import Annotated
from pydantic import BaseModel, BeforeValidator, Field


def _empty_str_to_none(v: object) -> object:
    """Convert empty strings to None so Pydantic can use the default."""
    if v == "":
        return None
    return v


FlexFloat = Annotated[Optional[float], BeforeValidator(_empty_str_to_none)]


# ============================================================================
# Tube Teaching
# ============================================================================

class TubeTeachRequest(BaseModel):
    """Request for /tube endpoint - teach tube pattern."""
    image_folder: str = Field(..., alias="folder", description="Folder path up to material_id level (good/ auto-appended)")
    result_folder: str = Field(..., description="Output folder — validation images saved with scores overlaid")

    class Config:
        populate_by_name = True


class TubeTeachResponse(BaseModel):
    """Response from /tube endpoint.

    Tube pattern uses Color NN + FFT with per-pattern threshold.
    color_threshold = p99_self_distance * 1.5, computed during teaching
    and stored in the .npz template file.
    """
    status: str
    training_id: str
    n_images: int
    template_path: str
    result_folder: str
    color_threshold: Optional[float] = None
    message: Optional[str] = None


# ============================================================================
# Stain Detection (Evaluation)
# ============================================================================

class StainDetectRequest(BaseModel):
    """Request for /stain endpoint - teach or detect stains.

    Modes:
        - "teach": Train PatchCore model on good cone images, save to production path.
        - "detect" (default): Run stain detection on images using existing model.
    """
    training_id: str = Field(..., description="Unique ID for this training/detection session (for traceability)")
    mode: str = Field("detect", description="teach or detect")
    image_folder: Optional[str] = Field(None, alias="folder", description="Single folder path")
    image_folders: Optional[list[str]] = Field(None, alias="folders", description="Multiple folder paths for teach mode")
    image_path: Optional[str] = Field(None, description="Single image path")
    image_base64: Optional[str] = Field(None, description="Base64 encoded image")
    k_sigma: float = Field(3.0, description="Threshold = mean + k_sigma * std (teach mode only)")

    class Config:
        populate_by_name = True


class StainDetectResult(BaseModel):
    """Stain detection result for a single image."""
    image_name: str
    has_stain: bool
    anomaly_score: float
    heatmap_base64: Optional[str] = None


class StainDetectResponse(BaseModel):
    """Response from /stain endpoint."""
    status: str
    training_id: str
    n_images: int
    results: list[StainDetectResult] = []
    good_count: int = 0
    stain_count: int = 0
    threshold: Optional[float] = None
    mean_score: Optional[float] = None
    std_score: Optional[float] = None
    model_path: Optional[str] = None
    message: Optional[str] = None


# ============================================================================
# Color Detection (Teaching)
# ============================================================================

class ColorDetectRequest(BaseModel):
    """Request for /color_detection endpoint."""
    material_id: str = Field(..., alias="mat_id")
    color_type: Optional[str] = None

    class Config:
        populate_by_name = True


class ColorDetectResponse(BaseModel):
    """Response from /color_detection endpoint."""
    status: str
    message: str


# ============================================================================
# Delete Master
# ============================================================================

class DeleteMasterRequest(BaseModel):
    """Request for /delete_master endpoint."""
    material_id: str = Field(..., alias="mat_id")
    master_id: Optional[str] = Field(None, alias="master_id")

    class Config:
        populate_by_name = True


class DeleteMasterResponse(BaseModel):
    """Response from /delete_master endpoint."""
    status: str
    message: str


# ============================================================================
# Get Teaching Data
# ============================================================================

class TeachingDataItem(BaseModel):
    """Single item in teaching data list."""
    mat_id: str
    master: str
    n_images: Optional[int] = None
    created_at: Optional[str] = None


class GetTeachingDataResponse(BaseModel):
    """Response from /get_teaching_data endpoint."""
    status: str
    data: list[TeachingDataItem]


# ============================================================================
# Tube OCR (Placeholder)
# ============================================================================

class TubeOCRRequest(BaseModel):
    """Request for /tube_ocr endpoint."""
    material_id: str = Field(..., alias="mat_id")
    pc_number: Optional[str] = Field(None, alias="pcNumber")
    title_name: Optional[str] = Field(None, alias="titleName")
    title_count: Optional[int] = Field(None, alias="titleCount")
    lot: Optional[str] = None

    class Config:
        populate_by_name = True


class TubeOCRResponse(BaseModel):
    """Response from /tube_ocr endpoint."""
    status: str
    message: str


# ============================================================================
# Extract (Cone dimensions teaching)
# ============================================================================

class ExtractRequest(BaseModel):
    """Request for /extract endpoint - cone dimension teaching."""
    material_id: str = Field(..., alias="id")
    image_folder: str = Field(..., alias="folder")
    scale: Optional[float] = Field(None, description="Pixels per mm")
    outer_tolerance: Optional[float] = Field(None, alias="outer_tol")

    class Config:
        populate_by_name = True


class ExtractResponse(BaseModel):
    """Response from /extract endpoint."""
    status: str
    material_id: str
    cone_diameter_mm: Optional[float] = None
    tube_diameter_mm: Optional[float] = None
    pixels_per_mm: Optional[float] = None
    message: Optional[str] = None


# ============================================================================
# Runtime Inspection
# ============================================================================

class InspectRequest(BaseModel):
    """Request for /inspect endpoint - run inspection on single frame."""
    material_id: str
    image_path: Optional[str] = None
    image_base64: Optional[str] = None


class InspectResponse(BaseModel):
    """Response from /inspect endpoint."""
    status: str
    result_code: int  # 1=Good, 2=Defect, 3=Error
    passed: bool
    material_id: str
    dimensions_ok: Optional[bool] = None
    stain_detected: Optional[bool] = None
    tube_pattern_ok: Optional[bool] = None
    cone_diameter_mm: Optional[float] = None
    tube_diameter_mm: Optional[float] = None
    annotated_image_base64: Optional[str] = None
    message: Optional[str] = None


# ============================================================================
# Retrain All
# ============================================================================

class RetrainResult(BaseModel):
    """Result of retraining a single material."""
    material_id: str
    master_name: str
    folder: str
    status: str  # "success" or "failed"
    n_images: int = 0
    message: Optional[str] = None


class RetrainAllResponse(BaseModel):
    """Response from /retrain_all endpoint."""
    status: str
    total: int
    success: int
    failed: int
    results: list[RetrainResult]
    message: Optional[str] = None


# ============================================================================
# Recipes
# ============================================================================

class RecipeRequest(BaseModel):
    """Request for POST /recipes — create or update a recipe.

    Accepts all frontend field name variants (str or int):
    - material_id / materialid / id
    - master_name / masterid / master
    - cone_diameter_mm / cone_diameter / conedia
    - tube_diameter_mm / tube_diameter / tubedia
    - cone_tolerance_mm / cone_tolerance / conetol
    - tube_tolerance_mm / tube_tolerance / tubetol
    """
    model_config = {"extra": "ignore"}

    material_id: Optional[str | int] = Field(None, description="PLC material number")
    materialid: Optional[str | int] = Field(None, description="Alias for material_id")
    id: Optional[str | int] = Field(None, description="Alias for material_id")
    master_name: Optional[str] = Field(None, description="Tube pattern class name")
    masterid: Optional[str] = Field(None, description="Alias for master_name")
    master: Optional[str] = Field(None, description="Alias for master_name")
    cone_diameter_mm: FlexFloat = Field(None, description="Cone diameter in mm")
    cone_diameter: FlexFloat = Field(None, description="Alias for cone_diameter_mm")
    conedia: FlexFloat = Field(None, description="Alias for cone_diameter_mm")
    tube_diameter_mm: FlexFloat = Field(None, description="Tube diameter in mm")
    tube_diameter: FlexFloat = Field(None, description="Alias for tube_diameter_mm")
    tubedia: FlexFloat = Field(None, description="Alias for tube_diameter_mm")
    cone_tolerance_mm: FlexFloat = Field(None, description="Cone tolerance in mm")
    cone_tolerance: FlexFloat = Field(None, description="Alias for cone_tolerance_mm")
    conetol: FlexFloat = Field(None, description="Alias for cone_tolerance_mm")
    tube_tolerance_mm: FlexFloat = Field(None, description="Tube tolerance in mm")
    tube_tolerance: FlexFloat = Field(None, description="Alias for tube_tolerance_mm")
    tubetol: FlexFloat = Field(None, description="Alias for tube_tolerance_mm")

    def get_material_id(self) -> str:
        raw = self.material_id or self.materialid or self.id or ""
        return str(raw) if raw else ""

    def get_master_name(self) -> str:
        return self.master_name or self.masterid or self.master or ""

    def get_cone_dia(self) -> float:
        return self.cone_diameter_mm or self.cone_diameter or self.conedia or 0.0

    def get_tube_dia(self) -> float:
        return self.tube_diameter_mm or self.tube_diameter or self.tubedia or 0.0

    def get_cone_tol(self) -> float:
        return self.cone_tolerance_mm or self.cone_tolerance or self.conetol or 0.0

    def get_tube_tol(self) -> float:
        return self.tube_tolerance_mm or self.tube_tolerance or self.tubetol or 0.0


class RecipeResponse(BaseModel):
    """Response from recipe endpoints."""
    status: str
    recipe: Optional[dict] = None
    message: Optional[str] = None
    restart_required: bool = False


class RecipeListResponse(BaseModel):
    """Response from GET /recipes."""
    status: str
    recipes: list[dict]
    pixels_per_mm: Optional[float] = None
    recipe_dir: Optional[str] = None


class MasterListResponse(BaseModel):
    """Response from GET /masters."""
    status: str
    masters: list[str]


# ============================================================================
# System
# ============================================================================

class StatusResponse(BaseModel):
    """Response from /status endpoint."""
    status: str
    yolo_loaded: bool
    templates_loaded: int
    uptime_seconds: float


class RestartResponse(BaseModel):
    """Response from /restart endpoint."""
    status: str
    message: str


class ShutdownResponse(BaseModel):
    """Response from /shutdown endpoint."""
    status: str
    message: str


# ============================================================================
# Health Check
# ============================================================================

class CameraHealthStatus(BaseModel):
    name: str
    connected: bool
    ip: Optional[str] = None
    serial: Optional[str] = None
    exposure: Optional[int] = None
    timeout: Optional[int] = None
    trigger_debounce_us: Optional[int] = None
    error: Optional[str] = None


class PLCHealthStatus(BaseModel):
    """Health status for PLC connection."""
    connected: bool
    host: Optional[str] = None
    port: Optional[int] = None
    error: Optional[str] = None


class SystemHealthResponse(BaseModel):
    """Response from /health/system endpoint - full system health."""
    status: str  # 'healthy', 'degraded', 'unhealthy'
    plc: PLCHealthStatus
    cameras: list[CameraHealthStatus]
    models_loaded: bool
    yolo_loaded: bool
    patchcore_loaded: bool
    uptime_seconds: float
    message: Optional[str] = None


class CameraHealthResponse(BaseModel):
    """Response from /health/cameras endpoint."""
    status: str
    cameras: list[CameraHealthStatus]
    all_connected: bool
    message: Optional[str] = None


class PLCHealthResponse(BaseModel):
    """Response from /health/plc endpoint."""
    status: str
    plc: PLCHealthStatus
    message: Optional[str] = None
