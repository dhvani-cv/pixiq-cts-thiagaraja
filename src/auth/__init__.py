"""Session-based authentication for Sieger HMI.

No JWT — tokens are random uuid4 strings stored in SQLite.
Read endpoints (monitoring) are public; write endpoints require auth.
"""

from auth.service import AuthService

__all__ = ["AuthService"]
