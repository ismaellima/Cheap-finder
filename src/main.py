from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func
from starlette.middleware.sessions import SessionMiddleware

from src.api.routes_alerts import router as alerts_router
from src.api.routes_brands import router as brands_router, export_router
from src.api.routes_dashboard import router as dashboard_router
from src.api.routes_products import router as products_router
from src.auth import AuthMiddleware, router as auth_router
from src.brands.registry import seed_all
from src.config import settings
from src.db.models import Product, Retailer
from src.db.session import async_session, init_db
from src.tracking.scheduler import setup_scheduler, setup_keep_alive

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _fix_scraper_types() -> None:
    """Auto-fix retailer scraper_type for retailers with dedicated scrapers.

    This runs on every startup so that when a new scraper is deployed,
    the DB is updated automatically without manual SQL.
    """
    # Map base_url patterns to correct scraper_type
    url_to_scraper: dict[str, str] = {
        "bluebuttonshop.com": "bluebuttonshop",
    }

    async with async_session() as session:
        for url_pattern, correct_type in url_to_scraper.items():
            result = await session.execute(
                select(Retailer).where(
                    Retailer.base_url.contains(url_pattern),
                    Retailer.scraper_type != correct_type,
                )
            )
            retailer = result.scalar_one_or_none()
            if retailer:
                old_type = retailer.scraper_type
                retailer.scraper_type = correct_type
                await session.commit()
                logger.info(
                    f"Auto-fixed scraper type for {retailer.name}: "
                    f"{old_type} -> {correct_type}"
                )


async def _run_discovery_if_needed() -> None:
    """Run brand discovery on startup if the DB has no products.

    This ensures data is populated after a fresh deploy on Render
    (PostgreSQL starts empty) and on first-ever startup.
    """
    async with async_session() as session:
        result = await session.execute(select(func.count(Product.id)))
        product_count = result.scalar() or 0

    if product_count > 0:
        logger.info(f"DB already has {product_count} products — skipping startup discovery")
        return

    logger.info("No products in DB — running startup discovery...")

    # Import here to avoid circular imports and keep startup fast when not needed
    from src.brands.discovery import discover_and_store
    from src.retailers import get_all_scrapers

    scrapers = get_all_scrapers()

    # Skip known-broken scrapers to avoid wasting time on startup
    skip = {"simons", "ssense", "nordstrom"}
    scrapers = {k: v for k, v in scrapers.items() if k not in skip}

    async with async_session() as session:
        stats = await discover_and_store(session, scrapers)

    logger.info(
        f"Startup discovery complete: {stats['new_products']} products, "
        f"{stats['mappings_created']} brand-retailer mappings"
    )

    # Close scraper HTTP clients
    for scraper in scrapers.values():
        await scraper.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Cheap Finder")
    await init_db()

    async with async_session() as session:
        await seed_all(session)

    # Auto-fix retailer scraper types (e.g. Blue Button Shop: generic -> bluebuttonshop)
    await _fix_scraper_types()

    # Discover products if DB is empty (first deploy / fresh PostgreSQL)
    # Run as background task so the app starts serving /health immediately
    asyncio.create_task(_run_discovery_if_needed())

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
app.include_router(export_router)
app.include_router(products_router)
app.include_router(alerts_router)


@app.get("/health")
async def health_check():
    return JSONResponse({"status": "ok", "version": "0.1.0"})
