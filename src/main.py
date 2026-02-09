from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from src.api.routes_alerts import router as alerts_router
from src.api.routes_brands import router as brands_router
from src.api.routes_dashboard import router as dashboard_router
from src.api.routes_products import router as products_router
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

app.mount("/static", StaticFiles(directory="src/static"), name="static")

app.include_router(dashboard_router)
app.include_router(brands_router)
app.include_router(products_router)
app.include_router(alerts_router)


@app.get("/health")
async def health_check():
    return JSONResponse({"status": "ok", "version": "0.1.0"})
