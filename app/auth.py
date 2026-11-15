"""JWT authentication and role-based access control (RBAC)."""
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import current_app, g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db

ROLES = ("OPERATOR", "PRODUCTION", "QA", "ADMIN")

# Which document access levels each role may read.
ROLE_ACCESS = {
    "OPERATOR":   {"ALL"},
    "PRODUCTION": {"ALL"},
    "QA":         {"ALL", "QA_ONLY"},
    "ADMIN":      {"ALL", "QA_ONLY", "RESTRICTED"},
}


def hash_password(pw: str) -> str:
    return generate_password_hash(pw)


def verify_password(pw_hash: str, pw: str) -> bool:
    return check_password_hash(pw_hash, pw)


def issue_token(user) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user["id"]),
        "username": user["username"],
        "role": user["role"],
        "iat": now,
        "exp": now + timedelta(minutes=current_app.config["TOKEN_TTL_MIN"]),
    }
    return jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")


def _load_user_from_token():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    user = get_db().execute(
        "SELECT * FROM users WHERE id = ? AND is_active = 1", (int(payload["sub"]),)
    ).fetchone()
    return user


def login_required(*allowed_roles):
    """Decorator: require a valid token, optionally restricted to roles."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = _load_user_from_token()
            if user is None:
                return jsonify(error="authentication required"), 401
            if allowed_roles and user["role"] not in allowed_roles:
                return jsonify(error=f"role {user['role']} not permitted"), 403
            g.user = user
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def allowed_access_levels(role: str) -> set:
    return ROLE_ACCESS.get(role, {"ALL"})
