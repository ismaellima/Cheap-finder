from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.alerts.notifier import send_alert
from src.alerts.rules import check_price_alert
from src.config import settings
from src.db.models import AlertEvent, PriceRecord, Product, Retailer
from src.retailers.base import RetailerBase

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    record: PriceRecord | None = None
    removed: bool = False


async def _product_page_is_gone(url: str) -> bool:
    """Confirm a product's own URL returns a definitive 404 (page removed).

    Used only as a secondary check when a scraper fails to return data, so a
    layout change or a transient network error doesn't get misread as a
    delisted product — we only trust an explicit 404 on the exact stored URL.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            return resp.status_code == 404
    except Exception:
        return False


async def _delete_removed_product(session: AsyncSession, product: Product) -> None:
    """Delete a product that's confirmed gone, along with its history.

    Price records cascade via the ORM relationship, but alert events (and
    their notifications) reference products with a NOT NULL FK and no
    cascade, so they have to be removed explicitly first.
    """
    events_result = await session.execute(
        select(AlertEvent)
        .where(AlertEvent.product_id == product.id)
        .options(selectinload(AlertEvent.notifications))
    )
    for event in events_result.scalars().all():
        await session.delete(event)

    await session.delete(product)
    await session.commit()


async def check_product_price(
    session: AsyncSession,
    product: Product,
    scraper: RetailerBase,
) -> CheckResult:
    try:
        result = await scraper.get_price(product.url)
    except Exception:
        logger.exception(f"Failed to scrape price for {product.name} ({product.url})")
        result = None

    if result is None:
        if await _product_page_is_gone(product.url):
            logger.warning(f"Product removed (404), deleting: {product.name} ({product.url})")
            await _delete_removed_product(session, product)
            return CheckResult(removed=True)
        logger.warning(f"Could not get price for {product.name} ({product.url})")
        # Stamp the attempt even though nothing was scraped. Batches are drawn
        # oldest-checked-first, so a product left unstamped would sit at the head
        # of the queue and be retried on every run forever, starving everything
        # behind it.
        product.last_checked = dt.datetime.utcnow()
        await session.commit()
        return CheckResult()

    if not result.available:
        logger.info(f"Product out of stock, keeping: {product.name}")
        product.last_checked = dt.datetime.utcnow()
        await session.commit()
        return CheckResult()

    old_price = product.current_price or 0

    record = PriceRecord(
        product_id=product.id,
        price=result.price,
        original_price=result.original_price,
        on_sale=result.on_sale,
        currency=result.currency,
    )
    session.add(record)

    product.current_price = result.price
    product.original_price = result.original_price
    product.on_sale = result.on_sale
    product.last_checked = dt.datetime.utcnow()

    if old_price > 0 and result.price < old_price:
        events = await check_price_alert(session, product, old_price, result.price)
        for event in events:
            await send_alert(event)

    await session.commit()
    logger.info(f"Checked {product.name}: ${result.price / 100:.2f}")
    return CheckResult(record=record)


async def check_all_prices(
    session: AsyncSession,
    scrapers: dict[str, RetailerBase],
    batch_size: int | None = None,
) -> dict[str, int]:
    """Check one batch of products, least-recently-checked first.

    This deliberately does not sweep the whole catalogue in one pass. At ~2s
    per request a full sweep runs for hours, and on a 512 MB free-tier instance
    it reliably died partway through — taking all its progress with it, since
    the next run restarted from the same end of the list. Anything past the
    first few thousand products was never reached at all.

    Ordering by last_checked instead makes progress durable without any
    checkpoint state: whatever a crashed run failed to reach is exactly what
    sits at the front of the queue next time. Every terminal outcome stamps
    last_checked (see check_product_price), so the queue always advances.
    """
    limit = batch_size if batch_size is not None else settings.PRICE_CHECK_BATCH_SIZE

    # Only pull products we can actually scrape. Retailers with no registered
    # scraper (or one excluded as known-broken) would otherwise occupy batch
    # slots on every run and never make progress.
    scrapable_types = list(scrapers.keys())
    if not scrapable_types:
        logger.warning("No scrapers available, skipping price check")
        return {"checked": 0, "removed": 0, "failed": 0, "batch": 0, "remaining": 0}

    base_filters = (
        Product.tracked.is_(True),
        Retailer.scraper_type.in_(scrapable_types),
    )

    result = await session.execute(
        select(Product)
        .join(Retailer, Product.retailer_id == Retailer.id)
        .where(*base_filters)
        .options(selectinload(Product.brand), selectinload(Product.retailer))
        .order_by(Product.last_checked.asc().nullsfirst())
        .limit(limit)
    )
    products = list(result.scalars().all())

    checked = 0
    removed = 0
    failed = 0
    for product in products:
        scraper = scrapers.get(product.retailer.scraper_type)
        if scraper is None:  # defensive — the query filter should prevent this
            product.last_checked = dt.datetime.utcnow()
            await session.commit()
            continue

        outcome = await check_product_price(session, product, scraper)
        if outcome.removed:
            removed += 1
        elif outcome.record:
            checked += 1
        else:
            failed += 1

    # How many scrapable products are still stale (never checked, or not checked
    # within a full cycle). This is the number to watch: if it trends down to ~0
    # the batches are keeping up, if it plateaus high they are not.
    stale_cutoff = dt.datetime.utcnow() - dt.timedelta(hours=24)
    remaining = (
        await session.execute(
            select(func.count(Product.id))
            .join(Retailer, Product.retailer_id == Retailer.id)
            .where(
                *base_filters,
                or_(
                    Product.last_checked.is_(None),
                    Product.last_checked < stale_cutoff,
                ),
            )
        )
    ).scalar() or 0

    logger.info(
        f"Price check batch complete: {checked} updated, {removed} removed, "
        f"{failed} failed (batch of {len(products)}); {remaining} products "
        f"still awaiting a check this cycle"
    )
    return {
        "checked": checked,
        "removed": removed,
        "failed": failed,
        "batch": len(products),
        "remaining": remaining,
    }
