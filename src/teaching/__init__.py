"""
Teaching module — REST API for enrolling tube pattern references.

Separate from the real-time inspection pipeline. Runs as a FastAPI
server on the Jetson, receives teaching requests from the web UI.
"""

from .tube_teacher import TubeTeacher

__all__ = ["TubeTeacher"]
