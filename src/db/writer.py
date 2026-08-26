"""
SQLite inspection result writer.

Called once per inspection cycle immediately after _inspect_and_report().
Writes one row to the inspections table and checks the rolling rejection
rate — alerts if > 30% of last 20 cones for the same material failed.

Usage:
    writer = InspectionWriter(conn)
    alert = writer.write(record)
    if alert:
        logger.warning("High rejection rate for %s", record["material_id"])
"""

import logging
import sqlite3
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Alert threshold: if more than this fraction of last N cones fail → alert
REJECTION_ALERT_THRESHOLD = 0.30
REJECTION_WINDOW = 20


@dataclass
class InspectionRecord:
    """One row of inspection data — populated after each cone."""
    timestamp: str          # ISO-8601 UTC
    material_id: str
    master_id: Optional[str]
    basket_no: Optional[int]
    loader_id: Optional[int]
    sample_counter: Optional[int]
    result_code: int        # 1=Good 2=Defect 3=Error
    defect_type: Optional[str]
    cone_dia_mm: Optional[float]
    tube_dia_mm: Optional[float]
    stain_score: Optional[float]
    stain_ok: Optional[bool]
    uv_radial_dip: Optional[float]
    uv_ok: Optional[bool]
    tail_confidence: Optional[float]
    tail_ok: Optional[bool]
    tube_pattern: Optional[str]
    tube_distance: Optional[float]
    tube_ok: Optional[bool]
    trial_mode: bool = False
    audit_image: Optional[str] = None
    tube_teaching_id: Optional[str] = None    # FK → teaching_sessions
    stain_teaching_id: Optional[str] = None   # FK → teaching_sessions
    uv_teaching_id: Optional[str] = None      # FK → teaching_sessions (threshold config)
    tail_teaching_id: Optional[str] = None    # FK → teaching_sessions (YOLO weights version)


class InspectionWriter:
    """Writes inspection results to SQLite and monitors rejection rate."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def write(self, record: InspectionRecord) -> tuple[int, bool]:
        """Write one inspection result and check rolling rejection rate.

        Args:
            record: Populated InspectionRecord for this cone.

        Returns:
            (row_id, alert) — row_id is the SQLite AUTOINCREMENT id of the
            inserted row (used to name the audit image file).
            alert is True if rejection rate exceeds threshold.
        """
        cur = self._conn.execute(
            """
            INSERT INTO inspections (
                timestamp, material_id, master_id, basket_no, loader_id,
                sample_counter, result_code, defect_type,
                cone_dia_mm, tube_dia_mm,
                stain_score, stain_ok,
                uv_radial_dip, uv_ok,
                tail_confidence, tail_ok,
                tube_pattern, tube_distance, tube_ok,
                trial_mode, audit_image,
                tube_teaching_id, stain_teaching_id,
                uv_teaching_id, tail_teaching_id
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?
            )
            """,
            (
                record.timestamp, record.material_id, record.master_id,
                record.basket_no, record.loader_id,
                record.sample_counter, record.result_code, record.defect_type,
                record.cone_dia_mm, record.tube_dia_mm,
                record.stain_score, int(record.stain_ok) if record.stain_ok is not None else None,
                record.uv_radial_dip, int(record.uv_ok) if record.uv_ok is not None else None,
                record.tail_confidence, int(record.tail_ok) if record.tail_ok is not None else None,
                record.tube_pattern, record.tube_distance,
                int(record.tube_ok) if record.tube_ok is not None else None,
                int(record.trial_mode), record.audit_image,
                record.tube_teaching_id, record.stain_teaching_id,
                record.uv_teaching_id, record.tail_teaching_id,
            ),
        )
        row_id = cur.lastrowid
        self._conn.commit()

        alert = self._check_rejection_rate(record.material_id)
        return row_id, alert

    def _check_rejection_rate(self, material_id: str) -> bool:
        """Check rolling rejection rate for the last N cones of this material.

        Counts result_code=2 (Defect) in the last REJECTION_WINDOW rows
        for this material_id. Excludes trial_mode runs.

        Returns:
            True if rejection rate exceeds REJECTION_ALERT_THRESHOLD.
        """
        cur = self._conn.execute(
            """
            SELECT result_code FROM inspections
            WHERE material_id = ? AND trial_mode = 0
            ORDER BY id DESC
            LIMIT ?
            """,
            (material_id, REJECTION_WINDOW),
        )
        rows = cur.fetchall()
        if len(rows) < REJECTION_WINDOW:
            # Not enough data yet — no alert
            return False

        n_defect = sum(1 for (rc,) in rows if rc == 2)
        rate = n_defect / len(rows)

        if rate > REJECTION_ALERT_THRESHOLD:
            logger.warning(
                "HIGH REJECTION RATE: material=%s  %d/%d = %.0f%% in last %d cones",
                material_id, n_defect, len(rows), rate * 100, REJECTION_WINDOW,
            )
            return True

        return False

    def get_rejection_rate(self, material_id: str) -> Optional[float]:
        """Return current rolling rejection rate for a material (0.0–1.0).

        Returns None if fewer than REJECTION_WINDOW results available.
        """
        cur = self._conn.execute(
            """
            SELECT result_code FROM inspections
            WHERE material_id = ? AND trial_mode = 0
            ORDER BY id DESC
            LIMIT ?
            """,
            (material_id, REJECTION_WINDOW),
        )
        rows = cur.fetchall()
        if len(rows) < REJECTION_WINDOW:
            return None
        n_defect = sum(1 for (rc,) in rows if rc == 2)
        return n_defect / len(rows)
