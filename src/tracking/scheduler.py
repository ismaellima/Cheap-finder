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

SKIP_SCRAPERS = {"simons", "ssense", "nordstrom"}


async def scheduled_price_check() -> None:
    logger.info("Starting scheduled price check batch")
    async with async_session() as session:
        from src.retailers import get_all_scrapers

        scrapers = get_all_scrapers()
        scrapers = {k: v for k, v in scrapers.items() if k not in SKIP_SCRAPERS}
        try:
            stats = await check_all_prices(session, scrapers)
            logger.info(
                f"Scheduled batch complete: {stats['checked']} updated, "
                f"{stats['removed']} removed, {stats['failed']} failed; "
                f"{stats['remaining']} still stale"
            )
        except Exception:
            logger.exception("Scheduled price check batch failed")
        finally:
            for scraper in scrapers.values():
                await scraper.close()


async def scheduled_discovery() -> None:
    """Weekly discovery: search all brands at all retailers for new products."""
    logger.info("Starting scheduled weekly discovery")
    from src.brands.discovery import discover_and_store
    from src.retailers import get_all_scrapers

    scrapers = get_all_scrapers()
    scrapers = {k: v for k, v in scrapers.items() if k not in SKIP_SCRAPERS}

    try:
        async with async_session() as session:
            stats = await discover_and_store(session, scrapers)
        logger.info(
            f"Scheduled discovery complete: {stats['new_products']} new products, "
            f"{stats['mappings_created']} mappings"
        )
    except Exception:
        logger.exception("Scheduled discovery failed")
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
    """Add a 5-minute interval job to keep the Render instance awake."""
    sched.add_job(
        keep_alive_ping,
        trigger=IntervalTrigger(minutes=5),
        args=[external_url.rstrip("/")],
        id="keep_alive_ping",
        name="Keep Render instance alive",
        replace_existing=True,
    )


def setup_scheduler() -> AsyncIOScheduler:
    # Rolling price-check batches. A single daily sweep of the whole catalogue
    # ran for hours and never survived a free-tier restart, so it silently
    # stopped covering most products. Small frequent batches finish well inside
    # their own interval and resume where the last one stopped.
    scheduler.add_job(
        scheduled_price_check,
        trigger=IntervalTrigger(minutes=settings.PRICE_CHECK_INTERVAL_MINUTES),
        id="rolling_price_check",
        name="Rolling price-check batch (least-recently-checked first)",
        replace_existing=True,
        # If a batch overruns its slot, skip the missed runs rather than
        # stacking concurrent sweeps on a 0.15-CPU instance.
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    # Weekly discovery (Sundays at 4:00 UTC — before daily price check)
    scheduler.add_job(
        scheduled_discovery,
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=0),
        id="weekly_discovery",
        name="Weekly brand discovery across all retailers",
        replace_existing=True,
    )

    return scheduler
