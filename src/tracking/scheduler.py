from __future__ import annotations

import logging

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.config import settings
from src.db.session import async_session
from src.tracking.price_checker import check_all_prices

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def scheduled_price_check() -> None:
    logger.info("Starting scheduled price check")
    async with async_session() as session:
        # Import scrapers lazily to avoid circular imports
        from src.retailers import get_all_scrapers

        scrapers = get_all_scrapers()
        try:
            count = await check_all_prices(session, scrapers)
            logger.info(f"Scheduled check complete: {count} products updated")
        finally:
            for scraper in scrapers.values():
                await scraper.close()


async def keep_alive_ping(url: str) -> None:
    """Ping our own /health endpoint to prevent Render free tier from sleeping."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{url}/health", timeout=10)
            logger.debug(f"Keep-alive ping: {resp.status_code}")
    except Exception:
        logger.warning("Keep-alive ping failed")


def setup_keep_alive(sched: AsyncIOScheduler, external_url: str) -> None:
    """Add a 10-minute interval job to keep the Render instance awake."""
    sched.add_job(
        keep_alive_ping,
        trigger=IntervalTrigger(minutes=10),
        args=[external_url.rstrip("/")],
        id="keep_alive_ping",
        name="Keep Render instance alive",
        replace_existing=True,
    )


def setup_scheduler() -> AsyncIOScheduler:
    scheduler.add_job(
        scheduled_price_check,
        trigger=CronTrigger(hour=settings.PRICE_CHECK_HOUR, minute=0),
        id="daily_price_check",
        name="Daily price check across all retailers",
        replace_existing=True,
    )
    return scheduler
