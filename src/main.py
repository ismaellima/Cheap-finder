from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from src.api.routes_alerts import router as alerts_router
from src.api.routes_brands import router as brands_router
from src.api.routes_dashboard import router as dashboard_router
from src.api.routes_products import router as products_router
from src.auth import AuthMiddleware, router as auth_router
from src.brands.registry import seed_all
from src.config import settings
from src.db.session import async_session, init_db
from src.tracking.scheduler import setup_scheduler, setup_keep_alive

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Cheap Finder")
    await init_db()

    async with async_session() as session:
        await seed_all(session)

    scheduler = setup_scheduler()

    # Keep-alive ping to prevent Render free tier from sleeping
    if settings.RENDER_EXTERNAL_URL:
        setup_keep_alive(scheduler, settings.RENDER_EXTERNAL_URL)
        logger.info(f"Keep-alive ping enabled for {settings.RENDER_EXTERNAL_URL}")

    scheduler.start()
    logger.info(f"Scheduler started — daily check at {settings.PRICE_CHECK_HOUR}:00 UTC")

    yield

    scheduler.shutdown()
    logger.info("Cheap Finder stopped")


app = FastAPI(
    title="Cheap Finder",
    description="Price tracking for fashion brands across Canadian retailers",
    version="0.1.0",
    lifespan=lifespan,
)

# --- Middleware (LIFO order: last added runs first) ---

# Auth middleware — protects routes when DASHBOARD_PASSWORD is set
app.add_middleware(AuthMiddleware)

# Session middleware — provides request.session backed by signed cookies
session_secret = settings.SESSION_SECRET_KEY or secrets.token_hex(32)
if not settings.SESSION_SECRET_KEY:
    logger.warning(
        "SESSION_SECRET_KEY not set — using random key. "
        "Sessions will not survive server restarts."
    )

# Determine if we should enforce HTTPS-only cookies
https_only = settings.RENDER_EXTERNAL_URL.startswith("https://") if settings.RENDER_EXTERNAL_URL else False

app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    session_cookie="cf_session",
    max_age=60 * 60 * 24 * 7,  # 7 days
    same_site="lax",
    https_only=https_only,
)

# --- Static files ---
app.mount("/static", StaticFiles(directory="src/static"), name="static")

# --- Routers ---
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(brands_router)
app.include_router(products_router)
app.include_router(alerts_router)


@app.get("/health")
async def health_check():
    return JSONResponse({"status": "ok", "version": "0.1.0"})
