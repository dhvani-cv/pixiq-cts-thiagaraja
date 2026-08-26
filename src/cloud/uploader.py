"""
Azure Blob Storage uploader for Sieger teaching data.

Uploads captured images for a module (stain/uv/tail) to Azure Blob Storage
for cloud-based training pipeline.

Blob path structure:
    {container}/{jetson_unit_id}/{session_id}/
        manifest.json        ← session manifest from local capture folder
        images/{filename}    ← captured PNG images

Usage:
    uploader = BlobUploader(cloud_config)
    result = uploader.upload_session(
        module="stain",
        session_id="abc-123",
        image_paths=[Path("captures/stain/.../img.png"), ...],
        metadata={"site": "ghcl", "n_images": 200, ...},
        progress_cb=lambda n, total: print(f"{n}/{total}"),
    )
"""

import json
import logging
import subprocess
import shutil
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class BlobUploader:
    """Uploads teaching session data to Azure Blob Storage.

    Args:
        config: The 'cloud' section of config.json.
            Required keys:
                connection_string (str): Azure Storage connection string
                container (str): Blob container name
    """
    RCLONE_REMOTE = "sieger_azure"  # must match name given in `rclone config`
    def __init__(self, config: dict):
        if not shutil.which("rclone"):
            raise RuntimeError(
                "rclone not found on PATH. Install via: sudo apt install rclone"
            )
        connection_string = (
            config.get("connection_string")
            or config.get("azure_connection_string")
            or config.get("AZURE_STORAGE_CONNECTION_STRING")
        )
        if not connection_string:
            raise ValueError("cloud.connection_string not configured")
        if connection_string.startswith("COCDefaultEndpointsProtocol="):
            logger.warning(
                "Azure connection string has unexpected COC prefix; using corrected value"
            )
            connection_string = connection_string[3:]

        self.container = (
            config.get("container")
            or config.get("azure_container")
            or config.get("AZURE_STORAGE_CONTAINER")
        )
        if not self.container:
            raise ValueError("cloud.container not configured")

        try:
            from azure.storage.blob import BlobServiceClient

            self._client = BlobServiceClient.from_connection_string(
                connection_string
            )
            self._container_client = self._client.get_container_client(self.container)
        except ImportError:
            raise ImportError(
                "azure-storage-blob not installed. Run: uv add azure-storage-blob"
            )

        try:
            self._container_client.create_container()
        except Exception:
            # Existing containers and auth-specific create denials are fine here;
            # upload_blob will surface a real auth/container failure.
            pass

        logger.info(
            "BlobUploader initialized: container=%s",
            self.container,
        )

    def _blob_prefix(
        self,
        module: str,
        session_id: str,
        manifest_path: Optional[Path] = None,
    ) -> str:
        """Return the Azure blob prefix for this session.

        Prefer the device-specific prefix recorded in the local manifest.
        Fall back to the historical local-style prefix if the manifest is
        missing or does not contain device.jetson_unit_id.
        """
        if manifest_path and manifest_path.exists():
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)
                jetson_unit_id = (
                    manifest.get("device", {}).get("jetson_unit_id", "")
                    if isinstance(manifest, dict)
                    else ""
                )
                jetson_unit_id = str(jetson_unit_id).strip().strip("/")
                if jetson_unit_id:
                    return f"{jetson_unit_id}/{session_id}"
            except Exception as e:
                logger.warning(
                    "Could not read jetson_unit_id from manifest %s: %s",
                    manifest_path,
                    e,
                )
        return f"captures/sessions/{module}/{session_id}"

    def upload_session(
        self,
        module: str,
        session_id: str,
        image_paths: list,
        metadata: dict,
        data_root: Path,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> dict:
        """Upload all images + manifest for a teaching session.

        Args:
            module: Teaching module — 'stain', 'uv', 'tail'
            session_id: Capture session UUID
            image_paths: List of relative paths (relative to data_root)
            metadata: Fallback dict written if local manifest.json is missing
            data_root: Local data root path
            progress_cb: Optional callback(n_uploaded, total) for progress reporting

        Returns:
            Dict with upload stats: n_uploaded, n_failed, blob_prefix, container
        """
        # Derive local session folder (contains manifest.json + images/)
        local_session_dir = data_root / "captures" / "sessions" / module / session_id
        manifest_path = local_session_dir / "manifest.json"
 
        # Write metadata as manifest.json locally if it doesn't exist yet
        if not manifest_path.exists():
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(manifest_path, "w") as f:
                json.dump(metadata, f, indent=2)
 
        # Resolve blob prefix (reuse existing _blob_prefix logic)
        prefix = self._blob_prefix(module, session_id, manifest_path)
        destination = f"{self.RCLONE_REMOTE}:{self.container}/{prefix}"
        total = len(image_paths)
 
        command = [
            "rclone", "sync",
            str(local_session_dir),    # source: local session folder (manifest + images/)
            destination,               # destination: azure blob prefix path
            "--transfers", "1",        # parallel file transfers
            "--retries", "2",         # auto retry on transient network failures
            "--retries-sleep", "10s",  # wait between retries
            "--checksum",              # skip files already in Azure by checksum match
            "--log-file", "/var/log/rclone_sieger.log",
            "--log-level", "INFO",
        ]
 
        logger.info(
            "rclone sync start: module=%s session=%s src=%s dst=%s n_images=%d",
            module, session_id, local_session_dir, destination, total,
        )
 
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=7200,   # 2hr hard timeout
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("rclone upload exceeded 2hr timeout")
 
        if result.returncode != 0:
            logger.error("rclone sync failed: %s", result.stderr)
            raise RuntimeError(
                f"rclone exited with code {result.returncode}: {result.stderr}"
            )
 
        logger.info(
            "rclone sync complete: module=%s session=%s uploaded=%d",
            module, session_id, total,
        )
 
        return {
            "n_uploaded": total,
            "n_failed": 0,
            "total": total,
            "blob_prefix": prefix,
            "container": self.container,
            "manifest_blob": f"{prefix}/manifest.json",
        }
