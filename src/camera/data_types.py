"""
Data types for the pythoncam inspection system.
"""

from dataclasses import dataclass
from typing import Optional
import numpy as np


@dataclass(slots=True)
class CapturedImages:
    """Holds the 3 images from one part capture cycle.

    Any field may be None if that camera timed out (cone missed
    the sensor). The inspection pipeline handles missing frames
    gracefully — each step checks if its frame is available.
    """
    vl: Optional[np.ndarray] = None       # Visible light image (BGR)
    uv: Optional[np.ndarray] = None       # UV light image (BGR)
    tail: Optional[np.ndarray] = None     # Yarn tail image (BGR)
