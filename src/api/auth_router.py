"""FastAPI auth router — session-based, no JWT.

Implements the Sieger-UI contract in user-management-requirements.md:
    POST /auth/login          — authenticate, return session token
    POST /auth/logout         — invalidate session
    POST /auth/verify-action  — re-auth confirm (any valid user) for Start/Stop, logs on success only
    GET  /auth/me             — current user + profile from token
    PUT  /auth/me             — update own profile
    GET  /auth/users          — list all users (superAdmin) → {"users": [...]}
    POST /auth/users          — create user (superAdmin)
    PUT  /auth/users/{username} — update user (superAdmin)
    DELETE /auth/users/{username} — delete user (superAdmin, guards: self / last admin)
    POST /auth/users/{username}/reset-password — reset password (superAdmin)
    GET  /auth/activity       — activity log → {"activity": [...]}

Auth dependency:
    from api.auth_router import get_current_user, require_role
    user = Depends(get_current_user)        # 401 if not logged in
    user = Depends(require_role("superAdmin"))  # 403 if wrong role

Token scheme: the raw session token travels in the Authorization header —
no "Bearer " prefix (contract §1/Q3).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from auth.service import AuthService, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Separate router — Sieger-UI's Header.jsx calls /user/*, not /auth/*.
user_router = APIRouter(prefix="/user", tags=["user"])

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str
    role: str
    services: dict
    expires_at: str


class VerifyActionRequest(BaseModel):
    username: str
    password: str
    action: str


class VerifyActionResponse(BaseModel):
    verified: bool
    username: str
    role: str


class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str = "operator"
    services: Optional[dict] = None
    # Contract-named optional fields (Q1) — legacy names accepted too
    fullName: Optional[str] = None
    employeeId: Optional[str] = None
    department: Optional[str] = None
    shift: Optional[str] = None
    requirePasswordChange: Optional[bool] = None
    email: Optional[str] = None
    empid: Optional[str] = None
    name: Optional[str] = None


class UserUpdateRequest(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    services: Optional[dict] = None
    # Contract-named optional fields (Q1) — legacy names accepted too
    fullName: Optional[str] = None
    employeeId: Optional[str] = None
    department: Optional[str] = None
    shift: Optional[str] = None
    accountEnabled: Optional[bool] = None
    requirePasswordChange: Optional[bool] = None
    email: Optional[str] = None
    empid: Optional[str] = None
    name: Optional[str] = None
    active: Optional[bool] = None


class ResetPasswordRequest(BaseModel):
    new_password: str


class UserDetailsRequest(BaseModel):
    """POST /user/details body — Header.jsx logo lookup."""
    username: str


class LogoTextRequest(BaseModel):
    """POST /user/logotext body."""
    username: str
    text: str


class LogoImageRequest(BaseModel):
    """POST /user/logoimage body — image is base64, no data: URI prefix."""
    username: str
    image: str


class ProfileUpdateRequest(BaseModel):
    """PUT /auth/me body — contract §6.2 (snake_case)."""
    email: Optional[str] = None
    phone: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    country: Optional[str] = None
    state: Optional[str] = None
    district: Optional[str] = None
    pincode: Optional[str] = None


class UserResponse(BaseModel):
    """User object shape per contract §4.1 (plus legacy aliases)."""
    id: int
    username: str
    role: str
    services: dict
    # Contract field names
    fullName: Optional[str] = None
    employeeId: Optional[str] = None
    department: Optional[str] = None
    shift: Optional[str] = None
    accountEnabled: bool = True
    status: str = "active"
    requirePasswordChange: bool = False
    # Legacy aliases (kept for backward compatibility)
    email: Optional[str] = None
    empid: Optional[str] = None
    name: Optional[str] = None
    active: bool = True


class MeResponse(UserResponse):
    """GET /auth/me — user + profile fields (contract §6.1)."""
    phone: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    country: Optional[str] = None
    state: Optional[str] = None
    district: Optional[str] = None
    pincode: Optional[str] = None


class UsersListResponse(BaseModel):
    users: list[UserResponse]


class MessageResponse(BaseModel):
    message: str


def _user_response(u: User, cls=UserResponse, **extra) -> UserResponse:
    return cls(
        id=u.id,
        username=u.username,
        role=u.role,
        services=u.services,
        fullName=u.name,
        employeeId=u.empid,
        department=u.department,
        shift=u.shift,
        accountEnabled=u.active,
        status="active" if u.active else "inactive",
        requirePasswordChange=u.require_password_change,
        email=u.email,
        empid=u.empid,
        name=u.name,
        active=u.active,
        **extra,
    )


def _me_response(u: User) -> MeResponse:
    return _user_response(
        u,
        cls=MeResponse,
        phone=u.phone,
        address_line1=u.address_line1,
        address_line2=u.address_line2,
        country=u.country,
        state=u.state,
        district=u.district,
        pincode=u.pincode,
    )


# ---------------------------------------------------------------------------
# Auth dependency — extracts token from Authorization header
# ---------------------------------------------------------------------------

def _get_auth_service(request: Request) -> AuthService:
    """Pull AuthService from app state (set during startup)."""
    return request.app.state.auth_service


def get_optional_user(
    authorization: Optional[str] = Header(None),
    auth: AuthService = Depends(_get_auth_service),
) -> Optional[User]:
    """Return current user if valid token provided, else None.

    Use this for endpoints that work for both authed and anonymous users.
    """
    if not authorization:
        return None
    return auth.validate_session(authorization)


def get_current_user(
    authorization: Optional[str] = Header(None),
    auth: AuthService = Depends(_get_auth_service),
) -> User:
    """Require valid session. Returns User or raises 401."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    user = auth.validate_session(authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user


def require_role(*roles: str):
    """Dependency factory — require user to have one of the given roles."""
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail=f"Requires role: {', '.join(roles)}")
        return user
    return dependency


def require_service(service_name: str):
    """Dependency factory — require a service to be enabled (superAdmin always passes).

    Matches the services model in user-management-requirements.md §3:
    services gate pages; operators can be granted them individually.
    """
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role == "superAdmin":
            return user
        if not user.services.get(service_name, False):
            raise HTTPException(status_code=403, detail=f"Service '{service_name}' not enabled for this user")
        return user
    return dependency


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    request: Request,
    auth: AuthService = Depends(_get_auth_service),
):
    client_ip = request.client.host if request.client else None
    try:
        session = auth.login(body.username, body.password, ip=client_ip)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    return LoginResponse(
        token=session.token,
        username=session.username,
        role=session.role,
        services=session.services,
        expires_at=session.expires_at,
    )


@router.post("/logout", response_model=MessageResponse)
def logout(
    authorization: Optional[str] = Header(None),
    auth: AuthService = Depends(_get_auth_service),
):
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    if auth.logout(authorization):
        return MessageResponse(message="Logged out")
    raise HTTPException(status_code=401, detail="Invalid session")


# Actions the Start/Stop confirmation popup (or similar re-auth prompts) may
# write to the activity log. Kept as an allow-list so this endpoint can't be
# used to log arbitrary action strings via the request body.
VERIFY_ACTIONS = {"start_inspection", "stop_inspection"}


@router.post("/verify-action", response_model=VerifyActionResponse)
def verify_action(
    body: VerifyActionRequest,
    session_user: User = Depends(get_current_user),
    auth: AuthService = Depends(_get_auth_service),
):
    """Re-auth confirmation for Start/Stop — logs *only* on success.

    Requires the browser's existing session (any logged-in user) so this
    can't be hit anonymously, but the confirming username/password can be
    *any* valid, active user (not necessarily `session_user`) — e.g. on a
    shared kiosk a different operator badges in per action. The confirming
    user's identity is what gets written to activity_log, not the session's.

    Never touches `sessions` — this is deliberately not a login, so it can't
    swap out the browser's current token/role/services.
    """
    if body.action not in VERIFY_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported action: {body.action}")

    confirmed_user = auth.verify_credentials(body.username, body.password)
    if confirmed_user is None:
        # Generic message (no enumeration), no activity_log write — only
        # successes are logged. Frontend leaves the dialog open so the
        # operator can just try again; no lockout/rate-limit here.
        raise HTTPException(status_code=401, detail="Invalid username or password")

    details = {"role": confirmed_user.role}
    if confirmed_user.username != session_user.username:
        details["session_username"] = session_user.username

    auth.log_activity(confirmed_user.username, body.action, details)

    return VerifyActionResponse(
        verified=True,
        username=confirmed_user.username,
        role=confirmed_user.role,
    )


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user)):
    return _me_response(user)


@router.put("/me", response_model=MeResponse)
def update_me(
    body: ProfileUpdateRequest,
    user: User = Depends(get_current_user),
    auth: AuthService = Depends(_get_auth_service),
):
    try:
        updated = auth.update_profile(
            user.username,
            email=body.email,
            phone=body.phone,
            address_line1=body.address_line1,
            address_line2=body.address_line2,
            country=body.country,
            state=body.state,
            district=body.district,
            pincode=body.pincode,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return _me_response(updated)


# -- User management (superAdmin only) -------------------------------------

@router.get("/users", response_model=UsersListResponse)
def list_users(
    _user: User = Depends(require_role("superAdmin")),
    auth: AuthService = Depends(_get_auth_service),
):
    return UsersListResponse(users=[_user_response(u) for u in auth.list_users()])


@router.post("/users", response_model=UserResponse, status_code=201)
def create_user(
    body: UserCreateRequest,
    admin: User = Depends(require_role("superAdmin")),
    auth: AuthService = Depends(_get_auth_service),
):
    try:
        u = auth.create_user(
            username=body.username,
            password=body.password,
            role=body.role,
            services=body.services,
            email=body.email,
            empid=body.employeeId if body.employeeId is not None else body.empid,
            name=body.fullName if body.fullName is not None else body.name,
            department=body.department,
            shift=body.shift,
            require_password_change=bool(body.requirePasswordChange),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    auth.log_activity(admin.username, "user mgmt", {"created": u.username, "role": u.role})
    return _user_response(u)


@router.put("/users/{username}", response_model=UserResponse)
def update_user(
    username: str,
    body: UserUpdateRequest,
    admin: User = Depends(require_role("superAdmin")),
    auth: AuthService = Depends(_get_auth_service),
):
    try:
        u = auth.update_user(
            username,
            password=body.password,
            role=body.role,
            services=body.services,
            email=body.email,
            empid=body.employeeId if body.employeeId is not None else body.empid,
            name=body.fullName if body.fullName is not None else body.name,
            active=body.accountEnabled if body.accountEnabled is not None else body.active,
            department=body.department,
            shift=body.shift,
            require_password_change=body.requirePasswordChange,
        )
    except ValueError as e:
        status = 404 if "not found" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e))

    auth.log_activity(admin.username, "user mgmt", {"updated": username})
    return _user_response(u)


@router.delete("/users/{username}", response_model=MessageResponse)
def delete_user(
    username: str,
    current_user: User = Depends(require_role("superAdmin")),
    auth: AuthService = Depends(_get_auth_service),
):
    if username == current_user.username:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    try:
        deleted = auth.delete_user(username)
    except ValueError as e:
        # e.g. "Cannot delete the last admin"
        raise HTTPException(status_code=400, detail=str(e))

    if not deleted:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    auth.log_activity(current_user.username, "user mgmt", {"deleted": username})
    return MessageResponse(message=f"User '{username}' deleted")


@router.post("/users/{username}/reset-password", response_model=MessageResponse)
def reset_password(
    username: str,
    body: ResetPasswordRequest,
    admin: User = Depends(require_role("superAdmin")),
    auth: AuthService = Depends(_get_auth_service),
):
    try:
        auth.update_user(username, password=body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    auth.log_activity(admin.username, "user mgmt", {"password_reset": username})
    return MessageResponse(message=f"Password reset for '{username}'")


# -- Activity log -----------------------------------------------------------

@router.get("/activity")
def activity_log(
    username: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    user: User = Depends(get_current_user),
    auth: AuthService = Depends(_get_auth_service),
):
    # superAdmin/engineer by role, or anyone granted the activityLog service (§3)
    if user.role not in ("superAdmin", "engineer") and not user.services.get("activityLog", False):
        raise HTTPException(status_code=403, detail="Activity log access not enabled for this user")

    return {"activity": auth.get_activity_log(username=username, limit=limit, offset=offset)}


# -- Header logo (Sieger-UI Header.jsx "Add Logo") ---------------------------
#
# Contract is body-carried username (not path), matching the existing
# Header.jsx calls verbatim. A valid session is still required (cvApi attaches
# the token to every request after login), and the body username is checked
# against the session so one user can't read/write another user's logo by
# editing the request — self-service only, superAdmin exempted.

def _require_self_or_admin(body_username: str, user: User) -> None:
    if body_username != user.username and user.role != "superAdmin":
        raise HTTPException(status_code=403, detail="Cannot access another user's logo")


@user_router.post("/details")
def user_details(
    body: UserDetailsRequest,
    user: User = Depends(get_current_user),
    auth: AuthService = Depends(_get_auth_service),
):
    """Return the logo for a user as a single-element array (Header.jsx reads res.data[0])."""
    _require_self_or_admin(body.username, user)
    logo_image, logo_text = auth.get_logo(body.username)
    return [{"logoImage": logo_image, "logoText": logo_text}]


@user_router.post("/logotext", response_model=MessageResponse)
def set_logo_text(
    body: LogoTextRequest,
    user: User = Depends(get_current_user),
    auth: AuthService = Depends(_get_auth_service),
):
    _require_self_or_admin(body.username, user)
    try:
        auth.set_logo_text(body.username, body.text)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    auth.log_activity(user.username, "settings", {"logo_text": True})
    return MessageResponse(message="Logo text saved")


@user_router.post("/logoimage", response_model=MessageResponse)
def set_logo_image(
    body: LogoImageRequest,
    user: User = Depends(get_current_user),
    auth: AuthService = Depends(_get_auth_service),
):
    _require_self_or_admin(body.username, user)
    try:
        auth.set_logo_image(body.username, body.image)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    auth.log_activity(user.username, "settings", {"logo_image": True})
    return MessageResponse(message="Logo image saved")
