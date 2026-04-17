"""Simple cookie-based authentication middleware."""

import hashlib
import hmac
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from backend.config import SECRET_KEY

# Paths that don't require auth
PUBLIC_PATHS = {"/ui/login", "/docs", "/openapi.json"}
TOKEN_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def _sign(value: str) -> str:
    return hmac.new(SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def make_auth_token(username: str) -> str:
    ts = str(int(time.time()))
    sig = _sign(f"{username}:{ts}")
    return f"{username}:{ts}:{sig}"


def verify_auth_token(token: str) -> str | None:
    try:
        username, ts, sig = token.rsplit(":", 2)
        if _sign(f"{username}:{ts}") != sig:
            return None
        if time.time() - int(ts) > TOKEN_MAX_AGE:
            return None
        return username
    except (ValueError, TypeError):
        return None


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Allow public paths
        if path in PUBLIC_PATHS:
            return await call_next(request)

        # Allow static/favicon
        if path.startswith("/favicon"):
            return await call_next(request)

        # Check auth cookie
        token = request.cookies.get("session")
        if token and verify_auth_token(token):
            return await call_next(request)

        # Not authenticated — redirect to login
        if path.startswith("/ui") or path == "/":
            return RedirectResponse("/ui/login")

        # API calls get 401
        from starlette.responses import JSONResponse
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
