"""
Application Initialization — Creates required folders, migrates DB to recipes.

Run once before first use, or automatically on service startup.

Usage:
    uv run python -m src.init_app

    Or from code:
    from init_app import initialize_app
    initialize_app()
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# Project root (parent of src/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ============================================================================
# Folder Structure
# ============================================================================

REQUIRED_FOLDERS = [
    "data/db",              # SQLite database (kept for migration)
    "data/recipes",         # JSON recipe files (replaces SQLite)
    "data/templates/tube",  # .npz files for tube patterns
    "weights",              # YOLO weights (pre-deployed)
    "models",               # PatchCore model (pre-deployed)
    "logs",                 # Log files (JSON + text, rotated)
]


def create_folders():
    """Create required folder structure."""
    for folder in REQUIRED_FOLDERS:
        folder_path = PROJECT_ROOT / folder
        folder_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Folder ready: {folder_path}")


# ============================================================================
# Database Schema (kept for migration reads)
# ============================================================================

# Materials table — stores reference dimensions for each material
CREATE_MATERIALS_TABLE = """
CREATE TABLE IF NOT EXISTS materials (
    material_id     TEXT PRIMARY KEY,
    height_mm       REAL NOT NULL DEFAULT 0.0,
    top_dia_mm      REAL NOT NULL DEFAULT 0.0,
    bottom_dia_mm   REAL NOT NULL DEFAULT 0.0,
    tube_dia_mm     REAL NOT NULL DEFAULT 0.0,
    tolerance_mm    REAL NOT NULL DEFAULT 2.0,
    pixels_per_mm   REAL DEFAULT NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
"""

# Teaching references table — stores metadata for .npz template files
CREATE_TEACHING_TABLE = """
CREATE TABLE IF NOT EXISTS teaching_references (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id     TEXT NOT NULL UNIQUE,
    template_path   TEXT NOT NULL,
    n_images        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
"""

CREATE_TEACHING_INDEX = """
CREATE INDEX IF NOT EXISTS idx_teaching_material
ON teaching_references(material_id);
"""

CREATE_UPDATE_TRIGGER_MATERIALS = """
CREATE TRIGGER IF NOT EXISTS update_materials_timestamp
AFTER UPDATE ON materials
BEGIN
    UPDATE materials SET updated_at = datetime('now') WHERE material_id = NEW.material_id;
END;
"""

CREATE_UPDATE_TRIGGER_TEACHING = """
CREATE TRIGGER IF NOT EXISTS update_teaching_timestamp
AFTER UPDATE ON teaching_references
BEGIN
    UPDATE teaching_references SET updated_at = datetime('now') WHERE id = NEW.id;
END;
"""


def init_database(db_path: Path):
    """Initialize the SQLite database with required tables.

    Kept for migration — new installs don't need it.

    Args:
        db_path: Path to the database file.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute(CREATE_MATERIALS_TABLE)
    cursor.execute(CREATE_TEACHING_TABLE)
    cursor.execute(CREATE_TEACHING_INDEX)

    try:
        cursor.execute(CREATE_UPDATE_TRIGGER_MATERIALS)
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute(CREATE_UPDATE_TRIGGER_TEACHING)
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()

    logger.info(f"Database initialized: {db_path}")


# ============================================================================
# Migration: SQLite → JSON recipes
# ============================================================================

def migrate_db_to_recipes(db_path: Path, recipe_dir: Path):
    """Migrate materials from SQLite DB to JSON recipe files.

    One-time, idempotent migration:
    1. Check if db exists AND recipe_dir is empty
    2. Read all rows from materials table → write as {material_id}.json
    3. Rename materials.db → materials.db.migrated (backup)

    Args:
        db_path: Path to SQLite database file.
        recipe_dir: Path to recipe directory.
    """
    if not db_path.exists():
        logger.info("No SQLite DB found at %s — skipping migration", db_path)
        return

    # Check if recipes already exist (migration already ran)
    existing_recipes = list(recipe_dir.glob("*.json"))
    if existing_recipes:
        logger.info(
            "Recipe dir already has %d files — migration not needed",
            len(existing_recipes),
        )
        return

    logger.info("Migrating SQLite DB → JSON recipes: %s → %s", db_path, recipe_dir)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Read all materials
        cursor = conn.execute(
            "SELECT material_id, height_mm, top_dia_mm, bottom_dia_mm, "
            "tube_dia_mm, tolerance_mm, "
            "COALESCE(cone_tolerance_mm, 0.0) as cone_tolerance_mm, "
            "COALESCE(tube_tolerance_mm, 0.0) as tube_tolerance_mm, "
            "COALESCE(master_name, '') as master_name, "
            "created_at, updated_at "
            "FROM materials"
        )
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            logger.info("No materials found in DB — nothing to migrate")
            return

        recipe_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()

        for row in rows:
            material_id = row["material_id"]
            recipe = {
                "material_id": material_id,
                "master_name": row["master_name"],
                "cone_diameter_mm": row["bottom_dia_mm"],
                "tube_diameter_mm": row["tube_dia_mm"],
                "cone_tolerance_mm": row["cone_tolerance_mm"],
                "tube_tolerance_mm": row["tube_tolerance_mm"],
                "created_at": row["created_at"] or now,
                "updated_at": row["updated_at"] or now,
            }

            recipe_path = recipe_dir / f"{material_id}.json"
            with open(recipe_path, "w") as f:
                json.dump(recipe, f, indent=2)
            logger.info("  Migrated material '%s' → %s", material_id, recipe_path)

        # Rename DB to .migrated backup
        migrated_path = db_path.with_suffix(".db.migrated")
        db_path.rename(migrated_path)
        logger.info(
            "Migration complete: %d recipes written, DB backed up to %s",
            len(rows), migrated_path,
        )

    except Exception as e:
        logger.error("Migration failed: %s", e)
        logger.info("SQLite DB left in place — will retry on next startup")


# ============================================================================
# Main Initialization
# ============================================================================

def initialize_app(config_path: Path = None):
    """Initialize the application — create folders, migrate DB to recipes.

    Args:
        config_path: Path to config.json. Defaults to src/config.json.
    """
    logger.info("Initializing application...")

    # Load config
    if config_path is None:
        config_path = PROJECT_ROOT / "src" / "config.json"

    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
    else:
        logger.warning(f"Config not found: {config_path}, using defaults")
        config = {}

    # Create folders
    create_folders()

    # Also create template_dir from config if different
    template_dir = config.get("teaching", {}).get("template_dir")
    if template_dir:
        template_path = PROJECT_ROOT / template_dir
        template_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Template folder ready: {template_path}")

    # Migrate SQLite → JSON recipes (one-time, idempotent)
    insp_cfg = config.get("inspection", {})
    db_path_str = insp_cfg.get("database", "data/db/materials.db")
    db_path = PROJECT_ROOT / db_path_str
    recipe_dir_str = insp_cfg.get("recipe_dir", "data/recipes")
    recipe_dir = PROJECT_ROOT / recipe_dir_str
    recipe_dir.mkdir(parents=True, exist_ok=True)

    migrate_db_to_recipes(db_path, recipe_dir)

    logger.info("Application initialization complete!")

    return {
        "project_root": str(PROJECT_ROOT),
        "recipe_dir": str(recipe_dir),
        "template_dir": str(PROJECT_ROOT / (template_dir or "data/templates/tube")),
    }


if __name__ == "__main__":
    result = initialize_app()
    print("\nInitialization Summary:")
    for key, value in result.items():
        print(f"  {key}: {value}")
