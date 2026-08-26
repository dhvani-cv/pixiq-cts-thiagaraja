"""
Recipe Store — JSON-file-based replacement for MaterialDatabase.

Each recipe is a JSON file in data/recipes/{material_id}.json that maps
a PLC material number to a master_name + dimensions + tolerances.

Drop-in replacement for MaterialDatabase: same get_material_specs() interface
returning MaterialSpecs.

Usage:
    store = RecipeStore("data/recipes")
    specs = store.get_material_specs("5")
    # → MaterialSpecs(material_id="5", master_name="FANTA_SOLID", ...)
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .data_types import MaterialSpecs

logger = logging.getLogger(__name__)


class RecipeStore:
    """JSON-file-based store for material recipes.

    Each recipe is stored as {recipe_dir}/{material_id}.json.
    All recipes are cached in memory on init and refreshed on writes.
    """

    def __init__(self, recipe_dir: str = "data/recipes"):
        self.recipe_dir = Path(recipe_dir)
        self.recipe_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict] = {}
        self._load_all()
        logger.info("RecipeStore loaded %d recipes from %s", len(self._cache), self.recipe_dir)

    def _load_all(self):
        """Load all *.json files from recipe_dir into memory cache."""
        self._cache.clear()
        for path in self.recipe_dir.glob("*.json"):
            try:
                with open(path) as f:
                    data = json.load(f)
                mid = data.get("material_id", path.stem)
                self._cache[str(mid)] = data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load recipe %s: %s", path, e)

    def reload_recipe(self, material_id: str):
        """Reload a single recipe from disk into cache."""
        path = self.recipe_dir / f"{material_id}.json"
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                self._cache[str(material_id)] = data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to reload recipe %s: %s", path, e)
        else:
            self._cache.pop(str(material_id), None)

    def _save_recipe(self, material_id: str, data: dict):
        """Write a recipe dict to disk atomically and update cache.

        Uses write-to-tmp + os.replace() so a crash mid-write never
        leaves a corrupted or empty JSON file on disk.
        """
        path = self.recipe_dir / f"{material_id}.json"
        tmp_path = path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, path)  # atomic on POSIX
        except Exception:
            # Clean up orphaned .tmp if the replace failed
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self._cache[str(material_id)] = data

    def get_material_specs(self, material_id: str) -> Optional[MaterialSpecs]:
        """Fetch material specs by ID — same interface as MaterialDatabase.

        Args:
            material_id: The PLC-supplied material identifier (e.g. "5").

        Returns:
            MaterialSpecs if found, None otherwise.
        """
        # Recipes can be edited from the API/UI while inspection is running.
        # Reload only the requested material so the next cone sees the update
        # without requiring a service restart.
        self.reload_recipe(str(material_id))
        data = self._cache.get(str(material_id))
        if data is None:
            logger.warning("Recipe '%s' not found", material_id)
            return None

        return MaterialSpecs(
            material_id=str(data.get("material_id", material_id)),
            height_mm=float(data.get("height_mm", 0.0)),
            top_diameter_mm=float(data.get("top_diameter_mm", 0.0)),
            bottom_diameter_mm=float(data.get("cone_diameter_mm", 0.0)),
            tube_diameter_mm=float(data.get("tube_diameter_mm", 0.0)),
            tolerance_mm=float(data.get("tolerance_mm", 2.0)),
            cone_tolerance_mm=float(data.get("cone_tolerance_mm", 0.0)),
            tube_tolerance_mm=float(data.get("tube_tolerance_mm", 0.0)),
            master_name=str(data.get("master_name", "")),
        )

    def upsert_recipe(
        self,
        material_id: str,
        master_name: str,
        cone_dia: float = 0.0,
        tube_dia: float = 0.0,
        cone_tol: float = 0.0,
        tube_tol: float = 0.0,
    ) -> dict:
        """Create or update a recipe.

        Args:
            material_id: PLC material number (e.g. "5").
            master_name: Tube pattern class name (e.g. "FANTA_SOLID").
            cone_dia: Cone diameter in mm.
            tube_dia: Tube diameter in mm.
            cone_tol: Cone tolerance in mm.
            tube_tol: Tube tolerance in mm.

        Returns:
            The saved recipe dict.
        """
        now = datetime.now(timezone.utc).isoformat()
        # Reload from disk first so cross-process edits (e.g. per-material
        # task toggles written by the API) are never clobbered.
        self.reload_recipe(str(material_id))
        existing = self._cache.get(str(material_id))

        # Merge into the existing dict so extra keys (e.g. "tasks") survive.
        data = dict(existing) if existing else {}
        data.update({
            "material_id": str(material_id),
            "master_name": master_name,
            "cone_diameter_mm": cone_dia,
            "tube_diameter_mm": tube_dia,
            "cone_tolerance_mm": cone_tol,
            "tube_tolerance_mm": tube_tol,
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
        })

        self._save_recipe(str(material_id), data)
        logger.info("Recipe '%s' upserted (master_name='%s')", material_id, master_name)
        return data

    def get_material_tasks(self, material_id: str) -> dict:
        """Return the per-material task overrides for a material, fresh from disk.

        Returns the recipe's "tasks" dict (e.g. {"stain_detection": false}),
        or {} when the recipe doesn't exist or has no overrides — meaning
        the material inherits the global inspection.tasks settings.
        """
        self.reload_recipe(str(material_id))
        data = self._cache.get(str(material_id))
        if not data:
            return {}
        tasks = data.get("tasks")
        return dict(tasks) if isinstance(tasks, dict) else {}

    def update_recipe_tasks(self, material_id: str, tasks: dict) -> Optional[dict]:
        """Set the per-material task overrides on an existing recipe.

        Args:
            material_id: PLC material number (e.g. "4").
            tasks: Dict of task-name → bool overrides.

        Returns:
            The saved recipe dict, or None if no recipe exists for this
            material (per-material toggles are only allowed for taught
            materials — callers should surface this as 404).
        """
        self.reload_recipe(str(material_id))
        existing = self._cache.get(str(material_id))
        if existing is None:
            return None

        data = dict(existing)
        merged = dict(data.get("tasks") or {})
        merged.update({k: bool(v) for k, v in tasks.items()})
        data["tasks"] = merged
        data["updated_at"] = datetime.now(timezone.utc).isoformat()

        self._save_recipe(str(material_id), data)
        logger.info("Recipe '%s' tasks updated: %s", material_id, merged)
        return data

    def delete_recipe(self, material_id: str) -> bool:
        """Delete a recipe file and remove from cache.

        Returns:
            True if deleted, False if not found.
        """
        path = self.recipe_dir / f"{material_id}.json"
        existed = path.exists()
        if existed:
            path.unlink()
        self._cache.pop(str(material_id), None)

        if existed:
            logger.info("Recipe '%s' deleted", material_id)
        return existed

    def list_recipes(self) -> list[dict]:
        """List all recipes sorted by material_id."""
        self._load_all()
        return sorted(self._cache.values(), key=lambda r: str(r.get("material_id", "")))

    def list_master_names(self) -> list[str]:
        """List unique master_names from all recipes."""
        names = {r.get("master_name", "") for r in self._cache.values()}
        names.discard("")
        return sorted(names)

    def close(self):
        """No-op for API compatibility with MaterialDatabase."""
        pass
