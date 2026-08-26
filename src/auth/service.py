"""Core authentication service — login, logout, session management.

All state lives in SQLite (users + sessions tables).
Passwords hashed with bcrypt.  Tokens are plain uuid4 strings.

Contract: user-management-requirements.md (Sieger-UI Users page).
Role codes on the wire: superAdmin | engineer | operator ("admin" accepted
as an alias for superAdmin on write).  Services is a 4-key boolean map:
report | master | settings | activityLog.

Usage:
    auth = AuthService(conn, default_session_hours=8.0)
    session = auth.login("operator1", "password123")
    user = auth.validate_session(session.token)
    auth.logout(session.token)
"""

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt

logger = logging.getLogger(__name__)

# Wire role codes (user-management-requirements.md §2)
VALID_ROLES = {"superAdmin", "engineer", "operator"}
ROLE_ALIASES = {"admin": "superAdmin"}

# The four service keys that gate UI pages (§3)
DEFAULT_SERVICES = {
    "report": False,
    "master": False,
    "settings": False,
    "activityLog": False,
}

# Single source of truth for user row shape — keep in sync with User dataclass
_USER_COLS = (
    "id, username, role, services, email, empid, name, active, "
    "department, shift, require_password_change, "
    "phone, address_line1, address_line2, country, state, district, pincode"
)


def normalize_role(role: str) -> str:
    """Map aliases to canonical role codes; raise ValueError on unknown roles."""
    role = ROLE_ALIASES.get(role, role)
    if role not in VALID_ROLES:
        raise ValueError(
            f"Invalid role '{role}' — must be one of: {', '.join(sorted(VALID_ROLES))}"
        )
    return role


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class User:
    id: int
    username: str
    role: str
    services: dict
    email: Optional[str] = None
    empid: Optional[str] = None
    name: Optional[str] = None
    active: bool = True
    department: Optional[str] = None
    shift: Optional[str] = None
    require_password_change: bool = False
    phone: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    country: Optional[str] = None
    state: Optional[str] = None
    district: Optional[str] = None
    pincode: Optional[str] = None


@dataclass
class Session:
    token: str
    user_id: int
    username: str
    role: str
    services: dict
    created_at: str
    expires_at: str


def _row_to_user(row) -> User:
    """Build a User from a SELECT {_USER_COLS} row."""
    (uid, username, role, services_json, email, empid, name, active,
     department, shift, require_pw_change,
     phone, addr1, addr2, country, state, district, pincode) = row
    return User(
        id=uid,
        username=username,
        role=role,
        services=json.loads(services_json) if services_json else {},
        email=email,
        empid=empid,
        name=name,
        active=bool(active),
        department=department,
        shift=shift,
        require_password_change=bool(require_pw_change),
        phone=phone,
        address_line1=addr1,
        address_line2=addr2,
        country=country,
        state=state,
        district=district,
        pincode=pincode,
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class AuthService:
    """Session-based auth backed by SQLite."""

    def __init__(self, conn: sqlite3.Connection, default_session_hours: float = 8.0):
        self._conn = conn
        self._session_hours = default_session_hours

    # -- password helpers ---------------------------------------------------

    @staticmethod
    def hash_password(plain: str) -> str:
        return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_password(plain: str, hashed: str) -> bool:
        return bcrypt.checkpw(plain.encode(), hashed.encode())

    # -- login / logout -----------------------------------------------------

    def login(self, username: str, password: str, ip: Optional[str] = None) -> Session:
        """Authenticate and create a session.

        Raises ValueError on bad credentials or inactive user.
        """
        row = self._conn.execute(
            "SELECT id, username, password, role, services, active "
            "FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        if row is None:
            raise ValueError("Invalid username or password")

        (uid, uname, pw_hash, role, services_json, active) = row

        if not active:
            raise ValueError("Account is disabled")

        if not self.verify_password(password, pw_hash):
            raise ValueError("Invalid username or password")

        services = json.loads(services_json) if services_json else {}
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=self._session_hours)
        token = uuid.uuid4().hex

        self._conn.execute(
            "INSERT INTO sessions (token, user_id, username, role, services, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                token,
                uid,
                uname,
                role,
                json.dumps(services),
                now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )

        # Log activity (ip surfaced by get_activity_log for the UI)
        details = {"role": role}
        if ip:
            details["ip"] = ip
        self._conn.execute(
            "INSERT INTO activity_log (username, action, details) VALUES (?, 'login', ?)",
            (uname, json.dumps(details)),
        )
        self._conn.commit()

        logger.info("User '%s' logged in (role=%s, expires=%s)", uname, role, expires.isoformat())

        return Session(
            token=token,
            user_id=uid,
            username=uname,
            role=role,
            services=services,
            created_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires_at=expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def verify_credentials(self, username: str, password: str) -> Optional[User]:
        """Verify a username/password pair without creating a session.

        Used for re-auth confirmation flows (e.g. the Start/Stop popup) where
        the confirming user may be *any* valid user — not necessarily the
        browser's logged-in session user — and a new session/token must NOT
        be created (that would silently swap out the browser's session).
        Unknown user / wrong password / inactive account all collapse to
        None so callers can't use this to enumerate valid usernames.
        """
        row = self._conn.execute(
            f"SELECT password, {_USER_COLS} FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        if row is None:
            return None

        pw_hash = row[0]
        user = _row_to_user(row[1:])

        if not user.active:
            return None
        if not self.verify_password(password, pw_hash):
            return None

        return user

    def logout(self, token: str) -> bool:
        """Delete session and log the event. Returns True if session existed."""
        row = self._conn.execute(
            "SELECT username FROM sessions WHERE token = ?", (token,)
        ).fetchone()

        if row is None:
            return False

        username = row[0]
        self._conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        self._conn.execute(
            "INSERT INTO activity_log (username, action) VALUES (?, 'logout')",
            (username,),
        )
        self._conn.commit()
        logger.info("User '%s' logged out", username)
        return True

    # -- session validation -------------------------------------------------

    def validate_session(self, token: str) -> Optional[User]:
        """Look up session token, return the full User if valid and not expired.

        Hydrates from the users table (not the session cache) so profile
        fields (email, name, department, …) are populated on /auth/me.
        """
        row = self._conn.execute(
            f"SELECT s.expires_at, {', '.join('u.' + c.strip() for c in _USER_COLS.split(','))} "
            "FROM sessions s JOIN users u ON s.user_id = u.id "
            "WHERE s.token = ?",
            (token,),
        ).fetchone()

        if row is None:
            return None

        expires_at = row[0]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if self._session_hours > 0 and expires_at < now:
            # Expired — clean up
            self._conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            self._conn.commit()
            return None

        user = _row_to_user(row[1:])
        if not user.active:
            return None

        return user

    def cleanup_expired_sessions(self) -> int:
        """Delete all expired sessions. Returns count deleted."""
        if self._session_hours <= 0:
            return 0
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self._conn.execute(
            "DELETE FROM sessions WHERE expires_at < ?", (now,)
        )
        self._conn.commit()
        return cur.rowcount

    # -- user CRUD ----------------------------------------------------------

    def create_user(
        self,
        username: str,
        password: str,
        role: str = "operator",
        services: Optional[dict] = None,
        email: Optional[str] = None,
        empid: Optional[str] = None,
        name: Optional[str] = None,
        department: Optional[str] = None,
        shift: Optional[str] = None,
        require_password_change: bool = False,
    ) -> User:
        """Create a new user. Raises ValueError on duplicate username or bad role."""
        role = normalize_role(role)
        if services is None:
            services = dict(DEFAULT_SERVICES)

        pw_hash = self.hash_password(password)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            cur = self._conn.execute(
                "INSERT INTO users (username, password, role, services, email, empid, name, "
                "department, shift, require_password_change, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (username, pw_hash, role, json.dumps(services), email, empid,
                 name or username, department, shift,
                 1 if require_password_change else 0, now, now),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError(f"Username '{username}' already exists")

        logger.info("User '%s' created (role=%s)", username, role)
        return User(
            id=cur.lastrowid,
            username=username,
            role=role,
            services=services,
            email=email,
            empid=empid,
            name=name or username,
            active=True,
            department=department,
            shift=shift,
            require_password_change=require_password_change,
        )

    def update_user(
        self,
        username: str,
        *,
        password: Optional[str] = None,
        role: Optional[str] = None,
        services: Optional[dict] = None,
        email: Optional[str] = None,
        empid: Optional[str] = None,
        name: Optional[str] = None,
        active: Optional[bool] = None,
        department: Optional[str] = None,
        shift: Optional[str] = None,
        require_password_change: Optional[bool] = None,
    ) -> User:
        """Update user fields. Only non-None fields are changed."""
        if role is not None:
            role = normalize_role(role)

        current = self.get_user(username)
        if current is None:
            raise ValueError(f"User '{username}' not found")

        uid = current.id
        new_role = role if role is not None else current.role
        new_services = json.dumps(services if services is not None else current.services)
        new_email = email if email is not None else current.email
        new_empid = empid if empid is not None else current.empid
        new_name = name if name is not None else current.name
        new_active = (1 if active else 0) if active is not None else (1 if current.active else 0)
        new_department = department if department is not None else current.department
        new_shift = shift if shift is not None else current.shift
        new_require_pw = (
            (1 if require_password_change else 0)
            if require_password_change is not None
            else (1 if current.require_password_change else 0)
        )
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # A fresh password satisfies any pending force-change flag
        if password is not None and require_password_change is None:
            new_require_pw = 0

        sets = (
            "role=?, services=?, email=?, empid=?, name=?, active=?, "
            "department=?, shift=?, require_password_change=?, updated_at=?"
        )
        params = [new_role, new_services, new_email, new_empid, new_name, new_active,
                  new_department, new_shift, new_require_pw, now]

        if password is not None:
            sets = "password=?, " + sets
            params.insert(0, self.hash_password(password))

        self._conn.execute(f"UPDATE users SET {sets} WHERE id=?", (*params, uid))

        # If user is deactivated, kill all their sessions
        if active is False:
            self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))

        self._conn.commit()
        logger.info("User '%s' updated", username)

        return self.get_user(username)

    def update_profile(
        self,
        username: str,
        *,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        address_line1: Optional[str] = None,
        address_line2: Optional[str] = None,
        country: Optional[str] = None,
        state: Optional[str] = None,
        district: Optional[str] = None,
        pincode: Optional[str] = None,
    ) -> User:
        """Update the caller's own profile fields (PUT /auth/me contract §6.2)."""
        current = self.get_user(username)
        if current is None:
            raise ValueError(f"User '{username}' not found")

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._conn.execute(
            "UPDATE users SET email=?, phone=?, address_line1=?, address_line2=?, "
            "country=?, state=?, district=?, pincode=?, updated_at=? WHERE id=?",
            (
                email if email is not None else current.email,
                phone if phone is not None else current.phone,
                address_line1 if address_line1 is not None else current.address_line1,
                address_line2 if address_line2 is not None else current.address_line2,
                country if country is not None else current.country,
                state if state is not None else current.state,
                district if district is not None else current.district,
                pincode if pincode is not None else current.pincode,
                now,
                current.id,
            ),
        )
        self._conn.commit()
        logger.info("Profile updated for '%s'", username)
        return self.get_user(username)

    # -- header logo (Sieger-UI Header.jsx "Add Logo") -----------------------

    def get_logo(self, username: str) -> tuple[Optional[str], Optional[str]]:
        """Return (logo_image_base64, logo_text) for a user, (None, None) if unknown."""
        row = self._conn.execute(
            "SELECT logo_image, logo_text FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            return None, None
        return row[0], row[1]

    def set_logo_text(self, username: str, text: str) -> None:
        current = self.get_user(username)
        if current is None:
            raise ValueError(f"User '{username}' not found")

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._conn.execute(
            "UPDATE users SET logo_text = ?, updated_at = ? WHERE id = ?",
            (text, now, current.id),
        )
        self._conn.commit()
        logger.info("Logo text updated for '%s'", username)

    def set_logo_image(self, username: str, image_base64: str) -> None:
        current = self.get_user(username)
        if current is None:
            raise ValueError(f"User '{username}' not found")

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._conn.execute(
            "UPDATE users SET logo_image = ?, updated_at = ? WHERE id = ?",
            (image_base64, now, current.id),
        )
        self._conn.commit()
        logger.info("Logo image updated for '%s'", username)

    def delete_user(self, username: str) -> bool:
        """Delete user and all their sessions. Returns True if user existed.

        Raises ValueError when deleting the last active superAdmin —
        otherwise user management would be permanently locked out
        (seed_admin only runs on an empty users table).
        """
        user = self.get_user(username)
        if user is None:
            return False

        if user.role == "superAdmin" and user.active and self.count_active_admins() <= 1:
            raise ValueError("Cannot delete the last admin")

        self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))
        self._conn.execute("DELETE FROM users WHERE id = ?", (user.id,))
        self._conn.commit()
        logger.info("User '%s' deleted", username)
        return True

    def count_active_admins(self) -> int:
        """Number of active superAdmin accounts."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'superAdmin' AND active = 1"
        ).fetchone()[0]

    def get_user(self, username: str) -> Optional[User]:
        """Fetch a single user by username."""
        row = self._conn.execute(
            f"SELECT {_USER_COLS} FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return _row_to_user(row) if row else None

    def list_users(self) -> list[User]:
        """Return all users (no passwords)."""
        rows = self._conn.execute(
            f"SELECT {_USER_COLS} FROM users ORDER BY username"
        ).fetchall()
        return [_row_to_user(r) for r in rows]

    # -- activity log -------------------------------------------------------

    def log_activity(self, username: str, action: str, details: Optional[dict] = None) -> None:
        """Write an entry to the activity log."""
        self._conn.execute(
            "INSERT INTO activity_log (username, action, details) VALUES (?, ?, ?)",
            (username, action, json.dumps(details) if details else None),
        )
        self._conn.commit()

    def get_activity_log(
        self,
        username: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        """Fetch activity log entries, newest first.

        `ip` is surfaced as a top-level key (contract §6.3) when present
        in the details blob.
        """
        if username:
            rows = self._conn.execute(
                "SELECT id, username, action, details, timestamp "
                "FROM activity_log WHERE username = ? "
                "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (username, limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, username, action, details, timestamp "
                "FROM activity_log "
                "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()

        entries = []
        for r in rows:
            details = json.loads(r[3]) if r[3] else None
            entries.append({
                "id": r[0],
                "username": r[1],
                "action": r[2],
                "details": details,
                "ip": details.get("ip") if isinstance(details, dict) else None,
                "timestamp": r[4],
            })
        return entries

    # -- seed admin ---------------------------------------------------------

    def seed_admin(
        self,
        username: str = "admin",
        password: str = "admin",
        role: str = "superAdmin",
    ) -> None:
        """Create default admin if no users exist. Idempotent.

        Runs at every API startup — guarantees a fresh installation always
        has one superAdmin (config auth.admin_username / auth.admin_password)
        who can then create operators and other users via the Users page.
        """
        count = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count > 0:
            return

        all_services = {k: True for k in DEFAULT_SERVICES}
        self.create_user(
            username=username,
            password=password,
            role=role,
            services=all_services,
            name="Administrator",
        )
        logger.info("Seeded default admin user '%s'", username)
