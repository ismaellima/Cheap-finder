"""Dashboard password protection.

Simple single-password auth using an env var (DASHBOARD_PASSWORD).
When the password is set, all routes except /health and /login require
a valid session cookie. When not set, the app is fully open.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.status import HTTP_303_SEE_OTHER

from src.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="src/templates")

# Paths that never require authentication
PUBLIC_PATHS = {"/health", "/login", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def is_auth_enabled() -> bool:
    """Auth is enabled only when DASHBOARD_PASSWORD is set to a non-empty value."""
    return bool(settings.DASHBOARD_PASSWORD)


def verify_password(plain: str) -> bool:
    """Timing-safe password comparison."""
    expected = settings.DASHBOARD_PASSWORD
    if not expected:
        return False
    return hmac.compare_digest(plain.encode("utf-8"), expected.encode("utf-8"))


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware that protects mutating routes when DASHBOARD_PASSWORD is set.

    GET requests to dashboard pages are public (read-only browsing).
    POST/PUT/DELETE requests require authentication (admin actions).
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        # If no password configured, pass through (open access)
        if not is_auth_enabled():
            return await call_next(request)

        path = request.url.path
        method = request.method

        # Allow public endpoints
        if path in PUBLIC_PATHS:
            return await call_next(request)

        # Allow static files
        if path.startswith("/static"):
            return await call_next(request)

        # Already authenticated — allow everything
        if request.session.get("authenticated"):
            return await call_next(request)

        # GET requests are public (read-only dashboard browsing)
        if method == "GET":
            return await call_next(request)

        # Not authenticated + mutating request (POST/PUT/DELETE) — block
        if path.startswith("/api/"):
            # API routes return 401 JSON
            return JSONResponse(
                {"detail": "Authentication required"},
                status_code=401,
            )

        # HTMX requests need HX-Redirect for proper full-page redirect
        if request.headers.get("HX-Request"):
            response = Response(status_code=200)
            response.headers["HX-Redirect"] = "/login"
            return response

        # Dashboard POST routes redirect to login
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)


@router.get("/login")
async def login_page(request: Request):
    """Render the login page."""
    # If no password set, redirect to dashboard
    if not is_auth_enabled():
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    # If already authenticated, redirect to dashboard
    if request.session.get("authenticated"):
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
async def login_submit(request: Request):
    """Validate password and create session."""
    form = await request.form()
    password = form.get("password", "")

    if verify_password(str(password)):
        request.session["authenticated"] = True
        logger.info("Dashboard login successful")
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    logger.warning("Dashboard login failed — incorrect password")
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Incorrect password"},
        status_code=401,
    )


@router.post("/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    request.session.clear()
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
